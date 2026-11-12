"""Sincronizzazione bidirezionale StudyFlow <-> Google Calendar (REST API v3).

Ogni sincronizzazione esegue, sotto un unico ``threading.Lock``:

1. ``calendarList`` + ``colors``: calendari, colori e abbinamento automatico
   calendario -> materia per nome (case-insensitive);
2. **flush della coda locale** (StudyFlow -> Google): ``google_pending_create``
   (POST), ``google_pending_update`` (``move`` se il calendario e' cambiato, poi
   PATCH), ``google_pending_delete`` (DELETE; il tombstone sparisce su 204/410);
3. **pull** (Google -> StudyFlow) per ogni calendario abilitato nella finestra
   ``-past_days`` / ``+future_days``: upsert su ``(google_calendar_id,
   google_event_id)`` senza toccare le modifiche locali ancora in coda, poi
   **cancellazione speculare** degli eventi spariti da Google (sessioni e
   materiali vengono solo scollegati).

Le chiamate HTTP girano sempre in thread ``daemon``: la UI riceve gli
aggiornamenti tramite listener, da inoltrare al main thread con ``app.post``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import quote

from ..database import (
    EVENT_PENDING_CREATE,
    EVENT_PENDING_DELETE,
    EVENT_PENDING_UPDATE,
    Database,
    normalize_hex,
)
from ..logging_setup import mask_secrets
from ..utils import LOCAL_DATETIME_FMT, parse_datetime, safe_int
from .ical_feeds import ICalFeedStore, sync_ical_feeds
from .google_auth import (
    HTTP_TIMEOUT,
    GoogleAuth,
    GoogleAuthError,
    GoogleAuthRequired,
    GoogleOfflineError,
    _is_network_error,
)


LOGGER = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"

SETTING_LAST_SYNC = "last_google_sync_at"
SETTING_ENABLED = "google_enabled_calendars"
SETTING_CALENDAR_CACHE = "google_calendar_cache"
SETTING_COLOR_PALETTE = "google_color_palette"
SETTING_PAST_DAYS = "google_sync_past_days"
SETTING_FUTURE_DAYS = "google_sync_future_days"
SETTING_AUTO_CREATE = "google_auto_create_calendar"

DEFAULT_PAST_DAYS = 180
DEFAULT_FUTURE_DAYS = 365

# Palette eventi standard di Google (GET /colors), usata finche' non e'
# disponibile quella scaricata dall'account.
DEFAULT_EVENT_COLORS: dict[str, dict[str, str]] = {
    "1": {"background": "#A4BDFC", "name": "Lavanda"},
    "2": {"background": "#7AE7BF", "name": "Salvia"},
    "3": {"background": "#DBADFF", "name": "Vinaccia"},
    "4": {"background": "#FF887C", "name": "Fenicottero"},
    "5": {"background": "#FBD75B", "name": "Banana"},
    "6": {"background": "#FFB878", "name": "Mandarino"},
    "7": {"background": "#46D6DB", "name": "Pavone"},
    "8": {"background": "#E1E1E1", "name": "Grafite"},
    "9": {"background": "#5484ED", "name": "Mirtillo"},
    "10": {"background": "#51B749", "name": "Basilico"},
    "11": {"background": "#DC2127", "name": "Pomodoro"},
}

WRITABLE_ROLES = {"owner", "writer"}


class GoogleApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google Calendar {status}: {message}")
        self.status = int(status)
        self.message = message


# ------------------------------------------------------------------ helpers
def text_color_for(background: str | None) -> str:
    """Colore del testo leggibile sullo sfondo dato (luminanza relativa WCAG)."""
    color = normalize_hex(background)
    if color is None:
        return "#F1F5F9"

    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    # Contrasto con il bianco vs con il quasi-nero (#0B1120 ~ 0.006).
    contrast_white = 1.05 / (luminance + 0.05)
    contrast_dark = (luminance + 0.05) / 0.056
    return "#FFFFFF" if contrast_white >= contrast_dark else "#0B1120"


def format_elapsed(last_sync_iso: str | None, now: datetime | None = None) -> str:
    """Testo dell'indicatore accanto a "Sincronizza Ora"."""
    text = str(last_sync_iso or "").strip()
    if not text:
        return "Mai sincronizzato"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "Mai sincronizzato"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - moment).total_seconds()))
    if seconds < 60:
        return "Aggiornato ora"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min fa"
    hours = minutes // 60
    if hours < 24:
        return "1 ora fa" if hours == 1 else f"{hours} ore fa"
    days = hours // 24
    return "1 giorno fa" if days == 1 else f"{days} giorni fa"


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_from_google(value: dict[str, Any] | None) -> tuple[datetime | None, bool]:
    if not isinstance(value, dict):
        return None, False
    if value.get("dateTime"):
        return parse_datetime(str(value["dateTime"])), False
    if value.get("date"):
        try:
            day = date.fromisoformat(str(value["date"]))
        except ValueError:
            return None, True
        return datetime.combine(day, datetime.min.time()), True
    return None, False


def google_event_to_local(
    item: dict[str, Any],
    subject: str | None,
    calendar_color: str | None,
    event_palette: dict[str, Any],
) -> dict[str, Any] | None:
    """Converte una risorsa ``Event`` di Google nel formato di ``calendar_events``."""
    if str(item.get("status") or "") == "cancelled" or not item.get("id"):
        return None
    start, all_day = _local_from_google(item.get("start"))
    end, _ = _local_from_google(item.get("end"))
    if start is None:
        return None
    if end is None or end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    title = str(item.get("summary") or "Evento senza titolo").strip()[:500]
    color_id = str(item.get("colorId") or "").strip() or None
    color = None
    if color_id:
        entry = event_palette.get(color_id) or DEFAULT_EVENT_COLORS.get(color_id) or {}
        color = normalize_hex(entry.get("background"))
    return {
        "google_event_id": str(item["id"]),
        "title": title,
        # Calendario non abbinato: stesso fallback dell'import .ics (titolo).
        "subject": (subject or title)[:150],
        "start": start.strftime(LOCAL_DATETIME_FMT),
        "end": end.strftime(LOCAL_DATETIME_FMT),
        "location": str(item.get("location") or ""),
        "description": str(item.get("description") or ""),
        "all_day": all_day,
        "color_id": color_id,
        "color_hex": color or normalize_hex(calendar_color),
    }


def local_event_to_google(event: dict[str, Any], for_patch: bool = False) -> dict[str, Any]:
    """Corpo JSON per ``events.insert`` / ``events.patch``."""
    start = parse_datetime(str(event.get("start") or ""))
    end = parse_datetime(str(event.get("end") or ""))
    if start is None or end is None:
        raise ValueError("Evento con date non valide.")
    body: dict[str, Any] = {
        "summary": str(event.get("title") or ""),
        "description": str(event.get("description") or ""),
    }
    if event.get("location"):
        body["location"] = str(event["location"])
    if bool(event.get("all_day")):
        end_day = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(days=1)
        if end_day <= start.date():
            end_day = start.date() + timedelta(days=1)
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": end_day.isoformat()}
        if for_patch:
            body["start"]["dateTime"] = None
            body["end"]["dateTime"] = None
    else:
        # datetime naive = ora locale di Windows; astimezone() aggiunge l'offset.
        body["start"] = {"dateTime": start.astimezone().isoformat()}
        body["end"] = {"dateTime": end.astimezone().isoformat()}
        if for_patch:
            body["start"]["date"] = None
            body["end"]["date"] = None
    color_id = str(event.get("color_id") or "").strip()
    if color_id:
        body["colorId"] = color_id
    elif for_patch:
        body["colorId"] = None
    return body


# ------------------------------------------------------------------- client
class GoogleCalendarClient:
    """Wrapper minimale della Calendar API v3 basato su ``requests``."""

    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = API_BASE + path
        force_refresh = False
        for attempt in range(4):
            token = self.auth.access_token(force_refresh=force_refresh)
            try:
                response = self.auth.http().request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=HTTP_TIMEOUT,
                )
            except Exception as exc:
                if _is_network_error(exc):
                    raise GoogleOfflineError("Google Calendar non raggiungibile.") from exc
                raise
            status = int(response.status_code)
            if status == 401 and not force_refresh:
                force_refresh = True
                continue
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                continue
            payload: dict[str, Any] = {}
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
            if status == 401:
                raise GoogleAuthRequired("Autorizzazione Google non valida. Ricollega l'account.")
            if status >= 400:
                error = payload.get("error") if isinstance(payload, dict) else None
                message = error.get("message") if isinstance(error, dict) else str(error or "")
                raise GoogleApiError(status, mask_secrets(message or response.reason or ""))
            return status, payload if isinstance(payload, dict) else {}
        raise GoogleApiError(503, "Servizio Google temporaneamente non disponibile.")

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return quote(str(calendar_id), safe="")

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = None
        for _ in range(100):
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            _status, payload = self._request("GET", path, params=query)
            items.extend(item for item in payload.get("items", []) if isinstance(item, dict))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return items
        raise GoogleApiError(500, "Troppe pagine di risultati.")

    def list_calendars(self) -> list[dict[str, Any]]:
        return self._paged("/users/me/calendarList", {"maxResults": 250})

    def get_colors(self) -> dict[str, Any]:
        return self._request("GET", "/colors")[1]

    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
        return self._paged(
            f"/calendars/{self._cal(calendar_id)}/events",
            {
                "singleEvents": "true",
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": 2500,
                "showDeleted": "false",
            },
        )

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/calendars/{self._cal(calendar_id)}/events", body=body)[1]

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}",
            body=body,
        )[1]

    def move_event(self, calendar_id: str, event_id: str, destination: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}/move",
            params={"destination": destination},
        )[1]

    def delete_event(self, calendar_id: str, event_id: str) -> int:
        try:
            return self._request(
                "DELETE", f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}"
            )[0]
        except GoogleApiError as exc:
            if exc.status in (404, 410):
                return exc.status
            raise

    def create_calendar(self, summary: str) -> dict[str, Any]:
        created = self._request("POST", "/calendars", body={"summary": summary})[1]
        try:
            entry = self._request(
                "GET", f"/users/me/calendarList/{self._cal(created['id'])}"
            )[1]
            created.update({key: entry[key] for key in ("backgroundColor", "accessRole") if key in entry})
        except (GoogleApiError, KeyError):
            pass
        return created


# ------------------------------------------------------------------ service
class CalendarSyncService:
    """Coordina coda offline, pull e notifiche alla UI."""

    def __init__(
        self,
        db: Database,
        auth: GoogleAuth | None = None,
        client: Any | None = None,
        ical_fetch: Callable[[str], bytes] | None = None,
    ):
        self.db = db
        self.auth = auth or GoogleAuth(db)
        self.client = client or GoogleCalendarClient(self.auth)
        # Calendari in sola lettura tramite indirizzo iCal (nessuna API).
        self.ical = ICalFeedStore(db)
        self._ical_fetch = ical_fetch
        # Serializza sync e flush: mai due thread che parlano con Google insieme.
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._sync_running = False
        self._flush_requested = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self.last_error = ""
        self.offline = False

    # ------------------------------------------------------------ listeners
    def add_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._listeners.append(callback)

    def _notify(self, kind: str, **data: Any) -> None:
        for callback in list(self._listeners):
            try:
                callback(kind, dict(data))
            except Exception:
                LOGGER.exception("Listener sincronizzazione Google non riuscito.")

    # ---------------------------------------------------------------- stato
    def is_connected(self) -> bool:
        try:
            return self.auth.is_connected()
        except Exception:
            return False

    def has_sources(self) -> bool:
        """True se c'e' qualcosa da sincronizzare: account Google o indirizzi iCal."""
        try:
            return self.is_connected() or self.ical.has_feeds()
        except Exception:
            return False

    def source_label(self, calendar_id: str | None) -> str:
        """Nome del calendario di origine di un evento (Google o iCal)."""
        feed = self.ical.for_calendar_id(calendar_id)
        if feed is not None:
            return f"iCal · {feed['name'] or feed['url_hint']}"
        info = self.calendar_info(calendar_id)
        return f"Google · {info.get('summary', '')}" if info else ""

    @property
    def is_syncing(self) -> bool:
        with self._state_lock:
            return self._sync_running

    def last_sync_at(self) -> str:
        return self.db.get_setting(SETTING_LAST_SYNC)

    def _json_setting(self, key: str, default: Any) -> Any:
        try:
            value = json.loads(self.db.get_setting(key) or "null")
        except ValueError:
            return default
        return value if isinstance(value, type(default)) else default

    def enabled_calendars(self) -> list[str]:
        return [str(value) for value in self._json_setting(SETTING_ENABLED, []) if str(value)]

    def set_enabled_calendars(self, calendar_ids: Iterable[str]) -> None:
        unique = list(dict.fromkeys(str(value) for value in calendar_ids if str(value)))
        self.db.set_setting(SETTING_ENABLED, json.dumps(unique))

    def cached_calendars(self) -> list[dict[str, Any]]:
        return [item for item in self._json_setting(SETTING_CALENDAR_CACHE, []) if isinstance(item, dict)]

    def calendar_info(self, calendar_id: str | None) -> dict[str, Any] | None:
        if not calendar_id:
            return None
        for item in self.cached_calendars():
            if item.get("id") == calendar_id:
                return item
        return None

    def is_calendar_writable(self, calendar_id: str | None) -> bool:
        info = self.calendar_info(calendar_id)
        return info is None or str(info.get("accessRole") or "owner") in WRITABLE_ROLES

    def event_palette(self) -> dict[str, dict[str, str]]:
        palette = self._json_setting(SETTING_COLOR_PALETTE, {})
        events = palette.get("event") if isinstance(palette, dict) else None
        result: dict[str, dict[str, str]] = {key: dict(value) for key, value in DEFAULT_EVENT_COLORS.items()}
        if not isinstance(events, dict):
            return result
        for key, value in events.items():
            if isinstance(value, dict) and normalize_hex(value.get("background")):
                name = DEFAULT_EVENT_COLORS.get(str(key), {}).get("name", f"Colore {key}")
                result[str(key)] = {"background": normalize_hex(value["background"]) or "", "name": name}
        return dict(sorted(result.items(), key=lambda item: safe_int(item[0], 99)))

    def window_days(self) -> tuple[int, int]:
        past = safe_int(self.db.get_setting(SETTING_PAST_DAYS), DEFAULT_PAST_DAYS)
        future = safe_int(self.db.get_setting(SETTING_FUTURE_DAYS), DEFAULT_FUTURE_DAYS)
        return max(1, min(3650, past)), max(1, min(3650, future))

    # ------------------------------------------------------------ calendari
    def refresh_calendars(self) -> list[dict[str, Any]]:
        """Scarica calendari e palette, abbina le materie e aggiorna i colori."""
        calendars = self.client.list_calendars()
        try:
            colors = self.client.get_colors()
            self.db.set_setting(
                SETTING_COLOR_PALETTE,
                json.dumps({"calendar": colors.get("calendar", {}), "event": colors.get("event", {})}),
            )
        except GoogleApiError as exc:
            LOGGER.warning("Palette colori Google non disponibile: %s", exc)
            colors = {}
        calendar_palette = colors.get("calendar", {}) if isinstance(colors, dict) else {}
        cache: list[dict[str, Any]] = []
        for item in calendars:
            color = normalize_hex(item.get("backgroundColor"))
            if color is None and item.get("colorId"):
                color = normalize_hex((calendar_palette.get(str(item["colorId"])) or {}).get("background"))
            entry = {
                "id": str(item.get("id") or ""),
                "summary": str(item.get("summaryOverride") or item.get("summary") or item.get("id") or ""),
                "backgroundColor": color or "#4285F4",
                "accessRole": str(item.get("accessRole") or "reader"),
                "primary": bool(item.get("primary")),
            }
            if entry["id"]:
                cache.append(entry)
            if entry["primary"]:
                self.auth.set_account_email(entry["id"])
        self.db.set_setting(SETTING_CALENDAR_CACHE, json.dumps(cache, ensure_ascii=False))
        self._auto_link(cache)
        for entry in cache:
            self.db.set_calendar_color(entry["id"], entry["backgroundColor"])
        return cache

    def _auto_link(self, cache: list[dict[str, Any]]) -> None:
        """Abbina per nome (case-insensitive) i calendari alle materie libere.

        Un calendario abbinato automaticamente viene anche abilitato alla
        sincronizzazione; l'utente puo' disattivarlo dalle Impostazioni.
        """
        mapped = self.db.subject_calendar_map()
        mapped_subjects = {value["name"].casefold() for value in mapped.values()}
        subjects = {name.strip().casefold(): name for name in self.db.list_subject_tags()}
        enabled = self.enabled_calendars()
        changed = False
        for entry in cache:
            if entry["id"] in mapped:
                continue
            name = subjects.get(entry["summary"].strip().casefold())
            if name is None or name.casefold() in mapped_subjects:
                continue
            self.db.link_subject_calendar(name, entry["id"], entry["backgroundColor"])
            mapped_subjects.add(name.casefold())
            if entry["id"] not in enabled:
                enabled.append(entry["id"])
                changed = True
            LOGGER.info("Calendario Google abbinato alla materia '%s'.", name)
        if changed:
            self.set_enabled_calendars(enabled)

    def ensure_calendar_for_subject(self, subject: str) -> str | None:
        """Crea un calendario secondario per una nuova materia (se abilitato)."""
        if self.db.get_setting(SETTING_AUTO_CREATE) not in {"1", "true"}:
            return None
        if not self.is_connected():
            return None
        current, _color = self.db.get_subject_calendar(subject)
        if current:
            return current
        with self._lock:
            for entry in self.cached_calendars():
                if entry["summary"].strip().casefold() == subject.strip().casefold():
                    self.db.link_subject_calendar(subject, entry["id"], entry["backgroundColor"])
                    self.set_enabled_calendars([*self.enabled_calendars(), entry["id"]])
                    return str(entry["id"])
            created = self.client.create_calendar(subject.strip())
            calendar_id = str(created["id"])
            self.db.link_subject_calendar(subject, calendar_id, created.get("backgroundColor"))
            self.set_enabled_calendars([*self.enabled_calendars(), calendar_id])
            try:
                self.refresh_calendars()
            except Exception as exc:
                LOGGER.warning("Aggiornamento calendari dopo la creazione non riuscito: %s", exc)
        LOGGER.info("Creato calendario Google per la materia '%s'.", subject)
        return calendar_id

    # ----------------------------------------------------------------- coda
    def flush_queue(self) -> dict[str, int]:
        """Invia a Google le modifiche locali in coda (chiamare con ``_lock``)."""
        counts = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
        for event in self.db.list_pending_events():
            event_id = int(event["id"])
            source = str(event["source"])
            revision = safe_int(event.get("local_revision", 0))
            try:
                if source == EVENT_PENDING_CREATE:
                    self._push_create(event, revision)
                    counts["created"] += 1
                elif source == EVENT_PENDING_UPDATE:
                    self._push_update(event, revision)
                    counts["updated"] += 1
                elif source == EVENT_PENDING_DELETE:
                    self._push_delete(event)
                    counts["deleted"] += 1
            except (GoogleOfflineError, GoogleAuthError):
                raise
            except GoogleApiError as exc:
                if exc.status in (429, 500, 502, 503, 504):
                    raise
                counts["failed"] += 1
                LOGGER.warning("Evento %s non sincronizzato: %s", event_id, exc)
                self.db.set_event_sync_error(event_id, str(exc))
            except ValueError as exc:
                counts["failed"] += 1
                self.db.set_event_sync_error(event_id, str(exc))
        return counts

    def _push_create(self, event: dict[str, Any], revision: int) -> None:
        calendar_id = str(event.get("google_calendar_id") or "")
        if not calendar_id:
            raise ValueError("Nessun calendario Google di destinazione.")
        created = self.client.insert_event(calendar_id, local_event_to_google(event))
        google_event_id = str(created.get("id") or "")
        if not google_event_id:
            raise GoogleApiError(500, "Google non ha restituito l'id dell'evento.")
        outcome = self.db.mark_event_synced(int(event["id"]), google_event_id, calendar_id, revision)
        if outcome == "missing":
            # Eliminato in StudyFlow mentre veniva creato: niente orfani su Google.
            self.client.delete_event(calendar_id, google_event_id)

    def _push_update(self, event: dict[str, Any], revision: int) -> None:
        event_id = int(event["id"])
        calendar_id = str(event.get("google_calendar_id") or "")
        google_event_id = str(event.get("google_event_id") or "")
        previous = str(event.get("previous_google_calendar_id") or "")
        if not calendar_id or not google_event_id:
            raise ValueError("Evento Google senza identificativi.")
        if previous and previous != calendar_id:
            try:
                self.client.move_event(previous, google_event_id, calendar_id)
            except GoogleApiError as exc:
                if exc.status not in (404, 410):
                    raise
                LOGGER.info("Evento %s gia' assente dal calendario di origine.", event_id)
            self.db.mark_event_moved(event_id, calendar_id)
        try:
            self.client.patch_event(calendar_id, google_event_id, local_event_to_google(event, True))
        except GoogleApiError as exc:
            if exc.status not in (404, 410):
                raise
            # Eliminato su Google ma modificato qui: vince la modifica locale.
            self.db.requeue_as_create(event_id)
            fresh = self.db.get_event(event_id)
            if fresh is not None and fresh["source"] == EVENT_PENDING_CREATE:
                self._push_create(fresh, safe_int(fresh.get("local_revision", 0)))
            return
        self.db.mark_event_synced(event_id, google_event_id, calendar_id, revision)

    def _push_delete(self, event: dict[str, Any]) -> None:
        calendar_id = str(event.get("google_calendar_id") or "")
        google_event_id = str(event.get("google_event_id") or "")
        if calendar_id and google_event_id:
            status = self.client.delete_event(calendar_id, google_event_id)
            if status not in (200, 204, 404, 410):
                raise GoogleApiError(status, "Eliminazione non confermata.")
        self.db.purge_tombstone(int(event["id"]))

    # ----------------------------------------------------------------- pull
    def pull(self) -> dict[str, int]:
        """Scarica i calendari abilitati e applica upsert + cancellazione speculare."""
        past_days, future_days = self.window_days()
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(days=past_days)
        window_end = now_utc + timedelta(days=future_days)
        local_start = window_start.astimezone().replace(tzinfo=None).strftime(LOCAL_DATETIME_FMT)
        local_end = window_end.astimezone().replace(tzinfo=None).strftime(LOCAL_DATETIME_FMT)
        cache = {item["id"]: item for item in self.cached_calendars()}
        subjects = self.db.subject_calendar_map()
        palette = self.event_palette()
        enabled = [calendar_id for calendar_id in self.enabled_calendars() if calendar_id in cache]
        totals = {"inserted": 0, "updated": 0, "skipped": 0, "removed": 0, "calendars": 0}
        for calendar_id in enabled:
            mapping = subjects.get(calendar_id) or {}
            calendar_color = mapping.get("color_hex") or cache[calendar_id].get("backgroundColor")
            items = self.client.list_events(calendar_id, _rfc3339(window_start), _rfc3339(window_end))
            local_events = [
                converted
                for converted in (
                    google_event_to_local(item, mapping.get("name"), calendar_color, palette)
                    for item in items
                )
                if converted is not None
            ]
            result = self.db.upsert_google_events(calendar_id, local_events)
            removed = self.db.mirror_google_deletions(
                calendar_id,
                local_start,
                local_end,
                (event["google_event_id"] for event in local_events),
            )
            totals["inserted"] += result["inserted"]
            totals["updated"] += result["updated"]
            totals["skipped"] += result["skipped"]
            totals["removed"] += removed
            totals["calendars"] += 1
        totals["removed"] += self.db.purge_unsynced_calendars(enabled)
        return totals

    # ------------------------------------------------------------ orchestr.
    def sync_now(self) -> dict[str, Any]:
        """Sincronizzazione completa e bloccante: da chiamare fuori dal main thread.

        Con l'account Google collegato: calendari, flush della coda e pull via
        API. Con indirizzi iCal configurati: download e aggiornamento dei feed
        (sola lettura). Le due modalita' possono convivere.
        """
        connected = self.is_connected()
        if not connected and not self.ical.has_feeds():
            raise GoogleAuthRequired(
                "Nessun calendario configurato: aggiungi un indirizzo iCal nelle Impostazioni."
            )
        pushed = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
        pulled = {"inserted": 0, "updated": 0, "skipped": 0, "removed": 0, "calendars": 0}
        ical: dict[str, Any] = {"inserted": 0, "updated": 0, "removed": 0, "feeds": 0, "errors": []}
        with self._lock:
            if connected:
                self.refresh_calendars()
                pushed = self.flush_queue()
                pulled = self.pull()
            if self.ical.list_feeds() or connected:
                _past, future = self.window_days()
                ical = sync_ical_feeds(self.db, self.ical, future, fetch=self._ical_fetch)
            for key in ("inserted", "updated", "removed"):
                pulled[key] += int(ical.get(key, 0))
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.db.set_setting(SETTING_LAST_SYNC, stamp)
        self.offline = False
        self.last_error = ""
        LOGGER.info(
            "Sincronizzazione Google completata: %s inviati, %s nuovi, %s aggiornati, %s rimossi.",
            pushed["created"] + pushed["updated"] + pushed["deleted"],
            pulled["inserted"],
            pulled["updated"],
            pulled["removed"],
        )
        return {
            "pushed": pushed,
            "pulled": pulled,
            "ical": ical,
            "warnings": list(ical.get("errors", [])),
            "last_sync_at": stamp,
        }

    def sync_async(self, reason: str = "manual") -> bool:
        """Avvia la sincronizzazione in un thread daemon. ``False`` se gia' in corso."""
        with self._state_lock:
            if self._sync_running:
                return False
            self._sync_running = True
        self._notify("sync_started", reason=reason)

        def worker() -> None:
            result: dict[str, Any] | None = None
            error = ""
            try:
                result = self.sync_now()
            except GoogleOfflineError:
                self.offline = True
                error = "Offline: le modifiche restano in coda."
            except GoogleAuthRequired as exc:
                error = str(exc)
            except (GoogleAuthError, GoogleApiError) as exc:
                error = str(exc)
                LOGGER.warning("Sincronizzazione Google non riuscita: %s", mask_secrets(exc))
            except Exception as exc:  # pragma: no cover - difesa
                error = f"Errore imprevisto: {exc}"
                LOGGER.exception("Sincronizzazione Google non riuscita.")
            finally:
                self.last_error = error
                with self._state_lock:
                    self._sync_running = False
                self.db.close_current_thread()
            self._notify("sync_finished", result=result, error=error, reason=reason)

        threading.Thread(target=worker, name="StudyFlow-GoogleSync", daemon=True).start()
        return True

    def request_flush(self) -> None:
        """Dopo un'azione dell'utente: invia la coda in background (non bloccante)."""
        if not self.is_connected():
            return
        with self._state_lock:
            self._flush_requested.set()
            if self._flush_thread is not None:
                return  # il worker attivo vedra' la nuova richiesta
            self._flush_thread = threading.Thread(
                target=self._flush_worker, name="StudyFlow-GoogleFlush", daemon=True
            )
            self._flush_thread.start()

    def _flush_worker(self) -> None:
        counts: dict[str, int] = {}
        error = ""
        try:
            while True:
                with self._state_lock:
                    if not self._flush_requested.is_set():
                        self._flush_thread = None
                        break
                    self._flush_requested.clear()
                with self._lock:
                    result = self.flush_queue()
                for key, value in result.items():
                    counts[key] = counts.get(key, 0) + value
            self.offline = False
        except GoogleOfflineError:
            self.offline = True
            error = "Offline: le modifiche verranno inviate alla prossima sincronizzazione."
        except (GoogleAuthError, GoogleApiError) as exc:
            error = str(exc)
            LOGGER.warning("Invio coda Google non riuscito: %s", mask_secrets(exc))
        except Exception as exc:  # pragma: no cover - difesa
            error = f"Errore imprevisto: {exc}"
            LOGGER.exception("Invio coda Google non riuscito.")
        finally:
            with self._state_lock:
                if self._flush_thread is threading.current_thread():
                    self._flush_thread = None
                    self._flush_requested.clear()
            self.db.close_current_thread()
        self._notify("flush_finished", counts=counts, error=error)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Solo per i test: attende la fine dei thread in background."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                flush = self._flush_thread
            if not self.is_syncing and flush is None:
                return True
            time.sleep(0.02)
        return False
