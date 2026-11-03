"""Blocco distrazioni: hosts tramite guardiano UAC, processi via psutil e web trap."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..config import PROCESS_LOG_COOLDOWN_SECONDS, WEB_LOG_COOLDOWN_SECONDS
from ..database import Database
from ..utils import normalize_domain, normalize_process_name, safe_float, safe_int
from .guard_client import GuardClient
from .hosts_service import has_managed_block
from .uac import is_admin, pid_alive
from .web_trap import WebTrap


LOGGER = logging.getLogger(__name__)


class BlockerController:
    def __init__(self, db: Database, paths: dict[str, Path]):
        self.db = db
        self.paths = paths
        self.guard = GuardClient(
            paths["hosts_control"],
            paths["hosts_status"],
            paths.get("guard_log"),
        )
        self.trap = WebTrap(self._on_web_attempt)

        self._lock = threading.RLock()
        self._active = False
        self._session_id: int | None = None
        self._monitor_thread: threading.Thread | None = None
        self._sites_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._attempt_count = 0
        self._last_log: dict[tuple[str, str], float] = {}
        self._site_message = "Blocco siti inattivo."
        self._site_error = ""
        self._apps_enabled = True
        self._sites_enabled = True
        self._terminate_apps = True
        self._trap_enabled = True
        self._app_scan_interval = 2.0
        self._app_map: dict[str, str] = {}
        self._protected_names = {
            "system",
            "system idle process",
            "registry",
            "smss",
            "csrss",
            "wininit",
            "services",
            "lsass",
            "winlogon",
            "explorer",
            "dwm",
            "fontdrvhost",
            "studyflow",
        }
        try:
            own_name = normalize_process_name(Path(sys.executable).name)
            if own_name:
                self._protected_names.add(own_name)
        except ValueError:
            pass
        self._hosts_residual = self._detect_residual_hosts()
        self._last_residual_check = time.monotonic()
        self._reload_runtime_config()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="StudyFlow-Heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------ config
    @staticmethod
    def _as_bool(value: str | bool) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _reload_runtime_config(self) -> None:
        settings = self.db.get_settings()
        apps = self.db.list_blocked_apps(enabled_only=True)
        with self._lock:
            self._apps_enabled = self._as_bool(settings.get("block_apps_enabled", "1"))
            self._sites_enabled = self._as_bool(settings.get("block_sites_enabled", "1"))
            self._terminate_apps = self._as_bool(
                settings.get("terminate_blocked_apps", "1")
            )
            self._trap_enabled = self._as_bool(settings.get("web_trap_enabled", "1"))
            self._app_scan_interval = max(
                1.0, min(10.0, safe_float(settings.get("app_scan_interval", "2.0"), 2.0))
            )
            self._app_map = {
                str(row["process_name"]).strip().lower(): str(row["display_name"])
                for row in apps
            }

    def _domains(self) -> list[str]:
        return [str(row["domain"]) for row in self.db.list_blocked_sites(enabled_only=True)]

    def reload_config(self) -> None:
        self._reload_runtime_config()
        with self._lock:
            active = self._active
            sites_enabled = self._sites_enabled
            trap_enabled = self._trap_enabled
            domains = self._domains()
            sites_thread_alive = bool(self._sites_thread and self._sites_thread.is_alive())
        if not active:
            return
        if not sites_enabled or not domains:
            self.trap.stop()
            self.guard.disable()
            with self._lock:
                self._site_message = (
                    "Blocco siti disattivato nelle impostazioni."
                    if not sites_enabled
                    else "Nessun sito in blacklist."
                )
            return

        guard_state = self.guard.status()
        guard_ready = guard_state["alive"] and guard_state["state"] in {"active", "starting"}
        if not guard_ready and not sites_thread_alive:
            self.trap.stop()
            with self._lock:
                self._site_message = "Riavvio protezione siti..."
                self._site_error = ""
                self._sites_thread = threading.Thread(
                    target=self._sites_worker,
                    args=(domains,),
                    name="StudyFlow-SiteBlockerReload",
                    daemon=True,
                )
                self._sites_thread.start()
            return
        self.trap.update_domains(domains)
        self.guard.reload_domains(domains)
        if trap_enabled:
            if not self.trap.status()["running"]:
                self.trap.start(domains)
        else:
            self.trap.stop()

    # ------------------------------------------------------------------ session
    def start_session(self, session_id: int) -> None:
        self._reload_runtime_config()
        with self._lock:
            if self._active:
                self._end_session_locked()
            self._active = True
            self._session_id = int(session_id)
            self._attempt_count = 0
            self._last_log.clear()
            self._site_error = ""
            self._site_message = "Preparazione del blocco..."
            apps_enabled = self._apps_enabled
            sites_enabled = self._sites_enabled
            domains = self._domains()
            self._monitor_stop = threading.Event()

        LOGGER.info("Blocco distrazioni avviato per la sessione %s.", session_id)
        if apps_enabled:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                args=(self._monitor_stop,),
                name="StudyFlow-ProcessMonitor",
                daemon=True,
            )
            self._monitor_thread.start()
        if sites_enabled and domains:
            self._sites_thread = threading.Thread(
                target=self._sites_worker,
                args=(domains,),
                name="StudyFlow-SiteBlocker",
                daemon=True,
            )
            self._sites_thread.start()
        elif sites_enabled:
            with self._lock:
                self._site_message = "Nessun sito in blacklist."
        else:
            with self._lock:
                self._site_message = "Blocco siti disattivato nelle impostazioni."

    def end_session(self) -> None:
        with self._lock:
            self._end_session_locked()

    def _end_session_locked(self) -> None:
        was_active = self._active
        self._active = False
        self._session_id = None
        self._monitor_stop.set()
        self.trap.stop()
        self.guard.disable()
        monitor = self._monitor_thread
        self._monitor_thread = None
        sites = self._sites_thread
        self._sites_thread = None
        if was_active:
            LOGGER.info("Blocco distrazioni terminato.")
        if monitor and monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(timeout=2.5)
        if sites and sites.is_alive() and sites is not threading.current_thread():
            sites.join(timeout=0.5)

    # ------------------------------------------------------------------ sites
    def _sites_worker(self, domains: list[str]) -> None:
        with self._lock:
            self._site_message = "Richiesta autorizzazione amministratore..."
        success = self.guard.ensure_enabled(domains)
        with self._lock:
            active = self._active
            trap_enabled = self._trap_enabled
            current_domains = self._domains()
        if not active:
            self.guard.disable()
            return
        if not success:
            with self._lock:
                self._site_message = "Siti non bloccati: autorizzazione UAC negata."
                self._site_error = self.guard.status().get("launch_error", "")
            LOGGER.warning("Blocco siti non attivo: UAC non concesso.")
            return
        with self._lock:
            self._site_message = "In attesa del guardiano hosts..."
        if trap_enabled:
            self.trap.start(current_domains)
            trap_status = self.trap.status()
            if not trap_status["running"]:
                with self._lock:
                    self._site_message = (
                        "Hosts attivo; intercettazione tentativi non disponibile "
                        "(porte 80/443 occupate)."
                    )
            else:
                with self._lock:
                    self._site_message = "Protezione siti attiva."
        else:
            with self._lock:
                self._site_message = "Hosts attivo; intercettazione tentativi disattivata."
        self._hosts_residual = False

    def retry_sites(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            domains = self._domains()
            if not domains or not self._sites_enabled:
                return False
            if self._sites_thread and self._sites_thread.is_alive():
                return False
            self._site_error = ""
            self._site_message = "Nuovo tentativo di autorizzazione..."
            self._sites_thread = threading.Thread(
                target=self._sites_worker,
                args=(domains,),
                name="StudyFlow-SiteBlockerRetry",
                daemon=True,
            )
            self._sites_thread.start()
            return True

    def force_restore_hosts(self) -> bool:
        self.guard.disable()
        result = self.guard.cleanup_hosts()
        self._hosts_residual = self._detect_residual_hosts()
        return result

    def _detect_residual_hosts(self) -> bool:
        return has_managed_block()

    # ------------------------------------------------------------------ apps
    def _monitor_loop(self, stop_event: threading.Event) -> None:
        try:
            import psutil
        except ImportError:
            LOGGER.exception("psutil non disponibile: monitoraggio applicazioni disattivato.")
            with self._lock:
                self._site_error = "psutil non installato."
            return
        own_pid = os.getpid()
        while not stop_event.is_set():
            with self._lock:
                active = self._active
                enabled = self._apps_enabled
                terminate = self._terminate_apps
                app_map = dict(self._app_map)
                interval = self._app_scan_interval
            if not active:
                break
            if enabled and app_map:
                self._scan_processes(psutil, app_map, terminate, own_pid)
            stop_event.wait(interval)

    def _scan_processes(
        self,
        psutil_module: Any,
        app_map: dict[str, str],
        terminate: bool,
        own_pid: int,
    ) -> None:
        matches: list[tuple[Any, str]] = []
        try:
            iterator = psutil_module.process_iter(["pid", "name"])
        except Exception:
            LOGGER.exception("Scansione processi non riuscita.")
            return
        for proc in iterator:
            try:
                info = proc.info
                pid = safe_int(info.get("pid", 0))
                if pid <= 0 or pid == own_pid:
                    continue
                raw_name = str(info.get("name") or "")
                try:
                    normalized = normalize_process_name(raw_name)
                except ValueError:
                    continue
                if normalized in self._protected_names:
                    continue
                display = app_map.get(normalized)
                if display:
                    matches.append((proc, display))
            except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
                continue
            except Exception:
                continue

        for proc, display in matches:
            with self._lock:
                if not self._active:
                    return
            self._register_attempt("app", display)
            if terminate:
                try:
                    LOGGER.warning("Terminazione applicazione bloccata: %s (PID %s)", display, proc.pid)
                    proc.terminate()
                except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
                    LOGGER.debug("Impossibile terminare %s.", display, exc_info=True)
                except Exception:
                    LOGGER.exception("Errore terminando %s.", display)

    # ------------------------------------------------------------------ events
    def _on_web_attempt(self, host: str) -> None:
        try:
            domain = normalize_domain(host)
        except ValueError:
            domain = host.strip().lower()
        try:
            self._register_attempt("website", domain, cooldown=WEB_LOG_COOLDOWN_SECONDS)
        finally:
            # Il callback gira su un thread per connessione: chiudere la
            # connessione SQLite thread-local evita di accumularne una per ogni
            # tentativo web.
            self.db.close_current_thread()

    def _register_attempt(
        self,
        source_type: str,
        target: str,
        cooldown: float = PROCESS_LOG_COOLDOWN_SECONDS,
    ) -> None:
        now = time.monotonic()
        key = (source_type, target.lower())
        with self._lock:
            if not self._active or self._session_id is None:
                return
            last = self._last_log.get(key, 0.0)
            if now - last < cooldown:
                return
            self._last_log[key] = now
            session_id = self._session_id
            self._attempt_count += 1
        try:
            self.db.log_distraction(session_id, source_type, target)
        except Exception:
            LOGGER.exception("Impossibile registrare il tentativo di distrazione.")
            with self._lock:
                self._attempt_count = max(0, self._attempt_count - 1)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        guard_status = self.guard.status()
        trap_status = self.trap.status()
        with self._lock:
            active = self._active
            attempts = self._attempt_count
            site_message = self._site_message
            site_error = self._site_error
            session_id = self._session_id
            apps_enabled = self._apps_enabled
            sites_enabled = self._sites_enabled
            terminate = self._terminate_apps
            trap_enabled = self._trap_enabled
            monitor_alive = bool(self._monitor_thread and self._monitor_thread.is_alive())
            should_check_residual = (
                not active and time.monotonic() - self._last_residual_check >= 10.0
            )
            if should_check_residual:
                self._last_residual_check = time.monotonic()
        if should_check_residual:
            detected = self._detect_residual_hosts()
            with self._lock:
                self._hosts_residual = detected
        with self._lock:
            hosts_residual = self._hosts_residual
        guard_state = str(guard_status.get("state", "inactive"))
        if not sites_enabled:
            guard_state = "disabled"
        elif not active:
            guard_state = "idle" if guard_state not in {"error", "stale"} else guard_state
        return {
            "active": active,
            "session_id": session_id,
            "apps_enabled": apps_enabled,
            "sites_enabled": sites_enabled,
            "terminate_apps": terminate,
            "trap_enabled": trap_enabled,
            "monitor_alive": monitor_alive,
            "attempts": attempts,
            "site_state": guard_state,
            "site_message": site_message,
            "site_error": site_error,
            "guard": guard_status,
            "trap": trap_status,
            "hosts_residual": hosts_residual,
            "is_admin": is_admin(),
        }

    # ------------------------------------------------------------------ lifecycle
    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(15.0):
            try:
                self.guard.heartbeat()
            except Exception:
                LOGGER.exception("Heartbeat guardiano hosts fallito.")

    def shutdown(self) -> None:
        self._heartbeat_stop.set()
        if (
            self._heartbeat_thread.is_alive()
            and self._heartbeat_thread is not threading.current_thread()
        ):
            self._heartbeat_thread.join(timeout=1.0)
        self.end_session()
        try:
            self.guard.shutdown()
        except Exception:
            LOGGER.exception("Chiusura guardiano hosts non riuscita.")
        self._hosts_residual = self._detect_residual_hosts()
