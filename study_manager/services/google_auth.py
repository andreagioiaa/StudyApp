"""OAuth 2.0 per app desktop (PKCE + loopback) verso Google Calendar.

Flusso, solo standard library + ``requests``:

1. l'utente carica il ``credentials.json`` (client OAuth di tipo "App desktop")
   scaricato dalla Google Cloud Console;
2. StudyFlow apre un ``http.server`` su ``127.0.0.1`` con porta effimera, genera
   ``code_verifier``/``code_challenge`` (S256) e un ``state`` casuale, poi apre il
   browser sulla pagina di consenso Google;
3. Google reindirizza al loopback con il codice, che viene scambiato con
   ``access_token`` e ``refresh_token`` presso ``oauth2.googleapis.com/token``;
4. ``access_token``, ``refresh_token`` e ``client_secret`` vengono cifrati con
   Windows DPAPI (``CryptProtectData``, legato all'utente Windows) prima di
   finire nella tabella ``settings``; l'``access_token`` viene rinnovato in
   automatico quando scade.

Nessun segreto viene mai scritto nei log: i messaggi usano ``mask_secrets``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import socket
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from ..logging_setup import mask_secrets


LOGGER = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
HTTP_TIMEOUT = (8, 25)
_EXPIRY_MARGIN = timedelta(seconds=90)
_DPAPI_ENTROPY = b"StudyFlow.GoogleCalendar.v1"

SETTING_CLIENT_ID = "google_client_id"
SETTING_CLIENT_SECRET = "google_client_secret_enc"
SETTING_ACCESS_TOKEN = "google_access_token_enc"
SETTING_REFRESH_TOKEN = "google_refresh_token_enc"
SETTING_TOKEN_EXPIRY = "google_token_expiry"
SETTING_ACCOUNT_EMAIL = "google_account_email"


class GoogleAuthError(RuntimeError):
    """Errore di autenticazione mostrabile all'utente (senza segreti)."""


class GoogleAuthRequired(GoogleAuthError):
    """Account non collegato, oppure autorizzazione revocata/scaduta."""


class GoogleOfflineError(RuntimeError):
    """Rete non raggiungibile: le modifiche restano in coda."""


# ------------------------------------------------------------------- DPAPI
def _dpapi_available() -> bool:
    return os.name == "nt"


def protect_secret(value: str) -> str:
    """Cifra un segreto con DPAPI (utente Windows corrente).

    Fuori da Windows (solo test/sviluppo) usa un semplice base64 marcato
    ``plain:``, mai utilizzato nell'eseguibile distribuito.
    """
    data = str(value or "").encode("utf-8")
    if not data:
        return ""
    if not _dpapi_available():
        return "plain:" + base64.b64encode(data).decode("ascii")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes) -> tuple[DATA_BLOB, Any]:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    data_in, _keep_in = blob(data)
    entropy, _keep_entropy = blob(_DPAPI_ENTROPY)
    data_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_in),
        "StudyFlow",
        ctypes.byref(entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise GoogleAuthError("Cifratura DPAPI non riuscita.")
    try:
        encrypted = ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
    return "dpapi:" + base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(stored: str) -> str:
    text = str(stored or "")
    if not text:
        return ""
    if text.startswith("plain:"):
        return base64.b64decode(text[6:]).decode("utf-8")
    if not text.startswith("dpapi:"):
        raise GoogleAuthError("Formato del segreto salvato non riconosciuto.")
    if not _dpapi_available():
        raise GoogleAuthError("I segreti DPAPI sono leggibili solo su Windows.")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(text[6:])
    buffer_in = ctypes.create_string_buffer(raw, len(raw))
    data_in = DATA_BLOB(len(raw), ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_char)))
    buffer_entropy = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    entropy = DATA_BLOB(
        len(_DPAPI_ENTROPY), ctypes.cast(buffer_entropy, ctypes.POINTER(ctypes.c_char))
    )
    data_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(data_out),
    )
    if not ok:
        raise GoogleAuthError(
            "Impossibile decifrare le credenziali Google salvate "
            "(utente Windows diverso?). Ricollega l'account."
        )
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


# -------------------------------------------------------------------- PKCE
def make_pkce_pair() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` secondo RFC 7636 (metodo S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def parse_client_credentials(raw: str | bytes) -> dict[str, str]:
    """Estrae ``client_id``/``client_secret`` da un ``credentials.json``."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GoogleAuthError("Il file non e' un credentials.json valido.") from exc
    if not isinstance(data, dict):
        raise GoogleAuthError("Il file non e' un credentials.json valido.")
    section = data.get("installed")
    if not isinstance(section, dict):
        if isinstance(data.get("web"), dict):
            raise GoogleAuthError(
                "Il client OAuth e' di tipo 'Applicazione web'. In Google Cloud "
                "Console crea un ID client di tipo 'App desktop' e scarica di nuovo "
                "il file JSON."
            )
        raise GoogleAuthError("Nel file manca la sezione 'installed' del client OAuth.")
    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise GoogleAuthError("client_id o client_secret mancanti nel credentials.json.")
    return {"client_id": client_id, "client_secret": client_secret}


class _LoopbackHandler(BaseHTTPRequestHandler):
    server_version = "StudyFlowOAuth/1.0"

    def do_GET(self) -> None:  # noqa: N802 - API di http.server
        query = parse_qs(urlsplit(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        self.server.oauth_result = {key: values[0] for key, values in query.items()}  # type: ignore[attr-defined]
        ok = "code" in query
        title = "StudyFlow collegato a Google Calendar" if ok else "Autorizzazione annullata"
        body = (
            "Puoi chiudere questa scheda e tornare a StudyFlow."
            if ok
            else "StudyFlow non e' stato collegato. Puoi chiudere questa scheda."
        )
        page = (
            "<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title></head>"
            "<body style='font-family:Segoe UI,sans-serif;background:#0B1120;color:#F1F5F9;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            f"<div><h2>{html.escape(title)}</h2><p>{html.escape(body)}</p></div></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # L'URL di ritorno contiene il codice di autorizzazione: niente log.
        return


class GoogleAuth:
    """Gestione credenziali OAuth salvate nel database di StudyFlow."""

    def __init__(self, db: Any, session_factory: Callable[[], Any] | None = None):
        self.db = db
        self._session_factory = session_factory
        self._lock = threading.RLock()
        self._session: Any = None

    # ------------------------------------------------------------- stato
    def http(self) -> Any:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import requests

                self._session = requests.Session()
        return self._session

    def has_client(self) -> bool:
        return bool(
            self.db.get_setting(SETTING_CLIENT_ID) and self.db.get_setting(SETTING_CLIENT_SECRET)
        )

    def is_connected(self) -> bool:
        return self.has_client() and bool(self.db.get_setting(SETTING_REFRESH_TOKEN))

    def account_email(self) -> str:
        return self.db.get_setting(SETTING_ACCOUNT_EMAIL)

    def set_account_email(self, email: str) -> None:
        self.db.set_setting(SETTING_ACCOUNT_EMAIL, str(email or "")[:200])

    # ------------------------------------------------------- credentials
    def load_credentials_file(self, path: str | Path) -> str:
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size > 64 * 1024:
            raise GoogleAuthError("Seleziona il credentials.json scaricato da Google Cloud.")
        client = parse_client_credentials(file_path.read_bytes())
        with self._lock:
            previous = self.db.get_setting(SETTING_CLIENT_ID)
            values = {
                SETTING_CLIENT_ID: client["client_id"],
                SETTING_CLIENT_SECRET: protect_secret(client["client_secret"]),
            }
            if previous and previous != client["client_id"]:
                # Un client diverso invalida i token emessi per il precedente.
                values.update(
                    {
                        SETTING_ACCESS_TOKEN: "",
                        SETTING_REFRESH_TOKEN: "",
                        SETTING_TOKEN_EXPIRY: "",
                        SETTING_ACCOUNT_EMAIL: "",
                    }
                )
            self.db.set_settings(values)
        LOGGER.info("Credenziali OAuth Google caricate (client %s...).", client["client_id"][:12])
        return client["client_id"]

    def _client(self) -> tuple[str, str]:
        client_id = self.db.get_setting(SETTING_CLIENT_ID)
        secret = unprotect_secret(self.db.get_setting(SETTING_CLIENT_SECRET))
        if not client_id or not secret:
            raise GoogleAuthRequired("Carica prima il file credentials.json nelle Impostazioni.")
        return client_id, secret

    # ------------------------------------------------------- autorizzazione
    def authorize(
        self,
        timeout: float = 300.0,
        open_browser: Callable[[str], Any] = webbrowser.open,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Esegue il consenso nel browser. Bloccante: va chiamato da un thread."""
        client_id, client_secret = self._client()
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(24)
        server = HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        server.oauth_result = None  # type: ignore[attr-defined]
        server.timeout = 0.5
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{AUTH_URI}?{urlencode(params)}"
        try:
            LOGGER.info("Apertura consenso OAuth Google (loopback porta %s).", server.server_address[1])
            open_browser(url)
            deadline = time.monotonic() + max(10.0, float(timeout))
            while server.oauth_result is None:  # type: ignore[attr-defined]
                if cancel_event is not None and cancel_event.is_set():
                    raise GoogleAuthError("Collegamento annullato.")
                if time.monotonic() > deadline:
                    raise GoogleAuthError("Tempo scaduto in attesa del consenso nel browser.")
                try:
                    server.handle_request()
                except (OSError, socket.timeout):
                    continue
            result: dict[str, str] = server.oauth_result  # type: ignore[attr-defined]
        finally:
            server.server_close()
        if result.get("error"):
            raise GoogleAuthError(f"Google ha rifiutato l'autorizzazione: {result['error']}.")
        if not secrets.compare_digest(str(result.get("state", "")), state):
            raise GoogleAuthError("Risposta OAuth non valida (state diverso).")
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": result.get("code", ""),
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )
        if not tokens.get("refresh_token"):
            raise GoogleAuthError(
                "Google non ha restituito un refresh token. Revoca l'accesso di "
                "StudyFlow da myaccount.google.com/permissions e riprova."
            )
        self._store_tokens(tokens)
        LOGGER.info("Account Google collegato.")

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.http().post(TOKEN_URI, data=data, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            if _is_network_error(exc):
                raise GoogleOfflineError("Google non raggiungibile.") from exc
            raise
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            error = str(payload.get("error") or response.status_code)
            LOGGER.warning("Richiesta token Google fallita: %s", mask_secrets(error))
            if error in {"invalid_grant", "unauthorized_client", "invalid_client"}:
                raise GoogleAuthRequired(
                    "L'autorizzazione Google e' scaduta o revocata. Ricollega l'account."
                )
            raise GoogleAuthError(f"Errore OAuth Google: {error}.")
        if not payload.get("access_token"):
            raise GoogleAuthError("Risposta token Google incompleta.")
        return payload

    def _store_tokens(self, tokens: dict[str, Any]) -> None:
        expires_in = max(60, int(tokens.get("expires_in") or 3600))
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        values = {
            SETTING_ACCESS_TOKEN: protect_secret(str(tokens["access_token"])),
            SETTING_TOKEN_EXPIRY: expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if tokens.get("refresh_token"):
            values[SETTING_REFRESH_TOKEN] = protect_secret(str(tokens["refresh_token"]))
        with self._lock:
            self.db.set_settings(values)

    # -------------------------------------------------------------- token
    def access_token(self, force_refresh: bool = False) -> str:
        """Token valido, rinnovato automaticamente se scaduto o quasi scaduto."""
        with self._lock:
            refresh_stored = self.db.get_setting(SETTING_REFRESH_TOKEN)
            if not refresh_stored:
                raise GoogleAuthRequired("Account Google non collegato.")
            if not force_refresh:
                expiry_text = self.db.get_setting(SETTING_TOKEN_EXPIRY)
                stored = self.db.get_setting(SETTING_ACCESS_TOKEN)
                try:
                    expiry = datetime.strptime(expiry_text, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    expiry = None
                if stored and expiry and expiry - _EXPIRY_MARGIN > datetime.now(timezone.utc):
                    return unprotect_secret(stored)
            client_id, client_secret = self._client()
            tokens = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": unprotect_secret(refresh_stored),
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
            )
            self._store_tokens(tokens)
            LOGGER.info("Access token Google rinnovato.")
            return str(tokens["access_token"])

    def disconnect(self, revoke: bool = True) -> None:
        """Scollega l'account: revoca il token (se possibile) e cancella i segreti."""
        with self._lock:
            stored = self.db.get_setting(SETTING_REFRESH_TOKEN)
            if revoke and stored:
                try:
                    token = unprotect_secret(stored)
                    self.http().post(REVOKE_URI, data={"token": token}, timeout=HTTP_TIMEOUT)
                except Exception as exc:  # la revoca e' solo un "best effort"
                    LOGGER.warning("Revoca token Google non riuscita: %s", mask_secrets(exc))
            self.db.set_settings(
                {
                    SETTING_ACCESS_TOKEN: "",
                    SETTING_REFRESH_TOKEN: "",
                    SETTING_TOKEN_EXPIRY: "",
                    SETTING_ACCOUNT_EMAIL: "",
                }
            )
        LOGGER.info("Account Google scollegato.")

    def forget_client(self) -> None:
        self.disconnect(revoke=True)
        self.db.set_settings({SETTING_CLIENT_ID: "", SETTING_CLIENT_SECRET: ""})


def _is_network_error(exc: BaseException) -> bool:
    try:
        import requests

        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, OSError))
