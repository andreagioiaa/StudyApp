"""Intercettatore locale best-effort dei tentativi verso domini bloccati.

Il file hosts reindirizza i domini a 127.0.0.1. Due listener leggeri su 80/443
leggono l'header HTTP Host oppure il Server Name Indication (SNI) del
ClientHello TLS. Non viene decrittografato alcun traffico e nessun dato viene
salvato oltre al nome del dominio e all'orario.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any

from ..config import TRAP_PORTS
from ..utils import normalize_domain


LOGGER = logging.getLogger(__name__)
_MAX_CONNECTIONS = 24
_READ_LIMIT = 16_384


def _normalize_host(host: str) -> str:
    value = host.strip().strip(".").lower()
    if value.startswith("["):
        value = value.split("]", 1)[0].strip("[")
    elif ":" in value:
        value = value.rsplit(":", 1)[0]
    try:
        return normalize_domain(value)
    except ValueError:
        return value


def _match_domain(host: str, domains: set[str]) -> bool:
    normalized = _normalize_host(host)
    if not normalized:
        return False
    for domain in domains:
        if normalized == domain or normalized.endswith("." + domain):
            return True
    return False


def _parse_client_hello(body: bytes) -> str:
    try:
        pos = 0
        if len(body) < 34:
            return ""
        pos += 2 + 32  # versione + random
        session_len = body[pos]
        pos += 1 + session_len
        cipher_len = int.from_bytes(body[pos : pos + 2], "big")
        pos += 2 + cipher_len
        compression_len = body[pos]
        pos += 1 + compression_len
        if pos + 2 > len(body):
            return ""
        extensions_len = int.from_bytes(body[pos : pos + 2], "big")
        pos += 2
        end = min(len(body), pos + extensions_len)
        while pos + 4 <= end:
            ext_type = int.from_bytes(body[pos : pos + 2], "big")
            ext_len = int.from_bytes(body[pos + 2 : pos + 4], "big")
            ext_data = body[pos + 4 : pos + 4 + ext_len]
            pos += 4 + ext_len
            if ext_type == 0 and len(ext_data) >= 5:
                list_len = int.from_bytes(ext_data[0:2], "big")
                inner = 2
                inner_end = min(len(ext_data), 2 + list_len)
                while inner + 3 <= inner_end:
                    name_type = ext_data[inner]
                    name_len = int.from_bytes(ext_data[inner + 1 : inner + 3], "big")
                    name_data = ext_data[inner + 3 : inner + 3 + name_len]
                    inner += 3 + name_len
                    if name_type == 0:
                        return name_data.decode("idna", errors="ignore").strip()
        return ""
    except (IndexError, ValueError):
        return ""


def extract_sni(record: bytes) -> str:
    pos = 0
    while pos + 5 <= len(record):
        content_type = record[pos]
        length = int.from_bytes(record[pos + 3 : pos + 5], "big")
        payload = record[pos + 5 : pos + 5 + length]
        if content_type == 22:
            handshake_pos = 0
            while handshake_pos + 4 <= len(payload):
                handshake_type = payload[handshake_pos]
                handshake_len = int.from_bytes(payload[handshake_pos + 1 : handshake_pos + 4], "big")
                body = payload[
                    handshake_pos + 4 : handshake_pos + 4 + handshake_len
                ]
                if handshake_type == 1:
                    return _parse_client_hello(body)
                handshake_pos += 4 + handshake_len
        pos += 5 + length
    return ""


class WebTrap:
    """Listener loopback per registrare i tentativi di accesso ai domini bloccati."""

    def __init__(self, on_attempt: Callable[[str], None]):
        self._on_attempt = on_attempt
        self._lock = threading.RLock()
        self._domains: set[str] = set()
        self._running = False
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._semaphore = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._errors: list[str] = []
        self._ports_active: dict[int, bool] = {port: False for port in TRAP_PORTS}

    def start(self, domains: Iterable[str]) -> None:
        self.stop()
        with self._lock:
            self._domains = {
                _normalize_host(domain)
                for domain in domains
                if str(domain).strip()
            }
            self._domains.discard("")
            self._errors = []
            self._ports_active = {port: False for port in TRAP_PORTS}
            if not self._domains:
                return
            self._running = True
            for port in TRAP_PORTS:
                thread = threading.Thread(
                    target=self._listen,
                    args=(port,),
                    name=f"StudyFlow-WebTrap-{port}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def update_domains(self, domains: Iterable[str]) -> None:
        with self._lock:
            self._domains = {
                _normalize_host(domain)
                for domain in domains
                if str(domain).strip()
            }
            self._domains.discard("")

    def stop(self) -> None:
        with self._lock:
            self._running = False
            sockets = list(self._sockets)
            self._sockets.clear()
        for listener in sockets:
            try:
                listener.close()
            except OSError:
                pass
        for thread in list(self._threads):
            if thread.is_alive():
                thread.join(timeout=1.0)
        with self._lock:
            self._threads.clear()
            self._ports_active = {port: False for port in TRAP_PORTS}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "ports": dict(self._ports_active),
                "errors": list(self._errors),
                "domains": len(self._domains),
            }

    # ------------------------------------------------------------------ loop
    def _listen(self, port: int) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(0.5)
            listener.bind(("127.0.0.1", port))
            listener.listen(32)
            with self._lock:
                self._sockets.append(listener)
                self._ports_active[port] = True
            while True:
                with self._lock:
                    if not self._running:
                        break
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not self._semaphore.acquire(blocking=False):
                    connection.close()
                    continue
                threading.Thread(
                    target=self._handle_connection,
                    args=(connection, port),
                    name=f"StudyFlow-WebTrap-{port}-client",
                    daemon=True,
                ).start()
        except OSError as exc:
            message = f"Porta {port} non disponibile: {exc}"
            LOGGER.warning(message)
            with self._lock:
                self._errors.append(message)
                self._ports_active[port] = False
        finally:
            try:
                listener.close()
            except OSError:
                pass

    def _handle_connection(self, connection: socket.socket, port: int) -> None:
        try:
            connection.settimeout(1.2)
            payload = b""
            while len(payload) < _READ_LIMIT:
                try:
                    chunk = connection.recv(4096)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                payload += chunk
                if port == 443 and len(payload) >= 5:
                    needed = 5 + int.from_bytes(payload[3:5], "big")
                    if len(payload) >= needed:
                        break
                elif b"\r\n\r\n" in payload:
                    break
            host = ""
            if payload:
                if port == 443:
                    host = extract_sni(payload)
                else:
                    host = self._extract_http_host(payload)
            with self._lock:
                domains = set(self._domains)
                active = self._running
            if active and host and _match_domain(host, domains):
                LOGGER.info("Tentativo web intercettato: %s", host)
                try:
                    self._on_attempt(host)
                except Exception:
                    LOGGER.exception("Errore nel callback del tentativo web.")
                if port == 80:
                    try:
                        connection.sendall(
                            b"HTTP/1.1 204 No Content\r\n"
                            b"Connection: close\r\n"
                            b"Content-Length: 0\r\n\r\n"
                        )
                    except OSError:
                        pass
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            self._semaphore.release()

    @staticmethod
    def _extract_http_host(payload: bytes) -> str:
        try:
            header_text = payload.split(b"\r\n\r\n", 1)[0].decode("latin-1", errors="ignore")
            for line in header_text.split("\r\n"):
                if line.lower().startswith("host:"):
                    return line.split(":", 1)[1].strip()
            first_line = header_text.split("\r\n", 1)[0]
            parts = first_line.split()
            if len(parts) >= 2 and "://" in parts[1]:
                from urllib.parse import urlsplit

                return urlsplit(parts[1]).hostname or ""
        except (IndexError, ValueError):
            return ""
        return ""
