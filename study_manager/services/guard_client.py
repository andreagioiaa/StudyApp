"""Client non elevato che coordina il processo guardiano del file hosts."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..utils import atomic_write_json, normalize_domain, read_json
from .uac import launch_elevated, pid_alive


LOGGER = logging.getLogger(__name__)


class GuardClient:
    def __init__(
        self,
        control_path: Path,
        status_path: Path,
        guard_log_path: Path | None = None,
    ):
        self.control_path = Path(control_path)
        self.status_path = Path(status_path)
        self.guard_log_path = Path(guard_log_path) if guard_log_path else None
        self.parent_pid = os.getpid()
        self._lock = threading.RLock()
        self._enabled = False
        self._domains: list[str] = []
        self._stop = False
        self._launch_error = ""
        self._launching = False
        self._last_launch = 0.0

    # ------------------------------------------------------------------ state
    def _read_status(self) -> dict[str, Any]:
        return read_json(self.status_path)

    def guard_status(self) -> dict[str, Any]:
        data = self._read_status()
        pid = int(data.get("pid", 0) or 0)
        alive = pid_alive(pid) if pid else False
        state = str(data.get("state", ""))
        if state and not alive and state not in {"stopped", "error"}:
            state = "stale"
        return {
            "pid": pid,
            "alive": alive,
            "state": state or ("unknown" if self._enabled else "inactive"),
            "message": str(data.get("message", "")),
            "updated": float(data.get("updated", 0) or 0),
            "enabled": bool(data.get("enabled", False)),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = self.guard_status()
            state = status["state"]
            alive = status["alive"]
            if not self._enabled:
                if state in {"active", "starting"} and not alive:
                    state = "inactive"
                elif state not in {"active", "starting", "error"}:
                    state = "inactive"
            elif not alive:
                if self._launching:
                    state = "starting"
                elif self._launch_error:
                    state = "error"
                else:
                    state = "starting"
            return {
                **status,
                "state": state,
                "enabled": self._enabled,
                "domains": list(self._domains),
                "launch_error": self._launch_error,
                "launching": self._launching,
            }

    # ----------------------------------------------------------------- control
    def _write_control(self, enabled: bool, domains: list[str], stop: bool) -> None:
        try:
            atomic_write_json(
                self.control_path,
                {
                    "enabled": bool(enabled),
                    "domains": domains,
                    "stop": bool(stop),
                    "heartbeat": time.time(),
                    "revision": time.time_ns(),
                    "parent_pid": self.parent_pid,
                },
            )
        except OSError:
            LOGGER.exception("Scrittura del file di controllo hosts non riuscita.")

    def _normalized_domains(self, domains: Iterable[str]) -> list[str]:
        result: set[str] = set()
        for domain in domains:
            try:
                result.add(normalize_domain(str(domain)))
            except ValueError:
                LOGGER.warning("Dominio ignorato perche' non valido: %r", domain)
        return sorted(result)

    def _launch_guard(self) -> bool:
        arguments = [
            "--hosts-guard",
            "--control",
            str(self.control_path),
            "--status",
            str(self.status_path),
            "--log",
            str(self.guard_log_path or (self.control_path.parent / "hosts_guard.log")),
            "--parent-pid",
            str(self.parent_pid),
        ]
        self._launching = True
        try:
            launched = launch_elevated(arguments)
        finally:
            self._launching = False
            self._last_launch = time.time()
        if launched:
            self._launch_error = ""
            return True
        self._launch_error = (
            "Autorizzazione amministratore non concessa o helper non avviato. "
            "Il blocco siti non e' attivo."
        )
        return False

    def ensure_enabled(self, domains: Iterable[str]) -> bool:
        with self._lock:
            self._domains = self._normalized_domains(domains)
            self._enabled = True
            self._stop = False
            self._write_control(True, self._domains, False)
            status = self.guard_status()
            if status["alive"] and status["state"] in {"active", "starting"}:
                return True
            if self._launching:
                return True
            # Evita una raffica di prompt UAC se l'utente ha appena annullato.
            if self._launch_error and time.time() - self._last_launch < 4.0:
                return False
            return self._launch_guard()

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
            self._write_control(False, self._domains, False)

    def retry(self) -> bool:
        with self._lock:
            self._launch_error = ""
            self._enabled = True
            self._write_control(True, self._domains, False)
            return self._launch_guard()

    def reload_domains(self, domains: Iterable[str]) -> None:
        with self._lock:
            self._domains = self._normalized_domains(domains)
            if self._enabled:
                self._write_control(True, self._domains, False)

    def heartbeat(self) -> None:
        with self._lock:
            if not self._enabled and not self.guard_status()["alive"]:
                return
            self._write_control(self._enabled, self._domains, False)

    def cleanup_hosts(self, timeout: float = 10.0) -> bool:
        """Avvia o riusa il guardiano per rimuovere una sezione hosts residua.

        Se non esiste alcuna sezione gestita e nessun guardiano e' attivo, non
        viene avviato nulla: in questo modo il ripristino manuale non mostra un
        prompt UAC quando non c'e' davvero nulla da modificare.
        """
        with self._lock:
            status = self.guard_status()
            blocked = self._hosts_blocked()
            self._enabled = False
            if not blocked and not status["alive"]:
                self._stop = False
                self._write_control(False, self._domains, False)
                return True
            self._stop = True
            self._write_control(False, self._domains, True)
            if not status["alive"]:
                self._launch_guard()
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = self.guard_status()
                if not status["alive"]:
                    return not self._hosts_blocked()
                time.sleep(0.2)
            return not self._hosts_blocked()

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._lock:
            status = self.guard_status()
            self._enabled = False
            self._stop = True
            self._write_control(False, self._domains, True)
            if not status["alive"]:
                return
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not pid_alive(status["pid"]):
                    return
                time.sleep(0.15)

    def _hosts_blocked(self) -> bool:
        try:
            from .hosts_service import has_managed_block

            return has_managed_block()
        except Exception:
            return False
