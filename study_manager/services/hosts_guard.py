"""Processo guardiano elevato che gestisce il file hosts.

Il guardiano viene avviato con UAC soltanto al primo Pomodoro che richiede il
blocco siti. Resta in attesa e:
  - applica/aggiorna i domini dal file di controllo;
  - rimuove la sezione gestita quando il blocco viene disattivato;
  - ripristina il file hosts se l'app principale termina o va in crash;
  - si arrende se l'heartbeat dell'app diventa obsoleto.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ..utils import atomic_write_json, normalize_domain, read_json
from .hosts_service import (
    HostsError,
    apply_hosts_block,
    has_managed_block,
    restore_hosts_block,
)
from .uac import pid_alive


LOGGER = logging.getLogger("studyflow.hosts_guard")
_STOP = False
_HEARTBEAT_TIMEOUT = 150.0
_POLL_SECONDS = 0.5


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=750_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(handler.formatter)
        root.addHandler(stream)


def _handle_signal(signum: int, frame: Any) -> None:
    del frame
    global _STOP
    _STOP = True
    LOGGER.info("Segnale %s ricevuto: avvio ripristino hosts.", signum)


def _write_status(
    path: Path,
    state: str,
    message: str,
    enabled: bool,
    domains: list[str],
) -> None:
    try:
        atomic_write_json(
            path,
            {
                "pid": os.getpid(),
                "state": state,
                "message": message,
                "enabled": bool(enabled),
                "domains": domains,
                "updated": time.time(),
            },
        )
    except OSError:
        LOGGER.exception("Impossibile scrivere lo stato del guardiano.")


def _load_control(path: Path) -> dict[str, Any]:
    data = read_json(path)
    return data if data else {}


def _sanitize_domains(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: set[str] = set()
    for value in values:
        try:
            result.add(normalize_domain(str(value)))
        except (ValueError, TypeError):
            LOGGER.warning("Dominio non valido ignorato dal guardiano: %r", value)
    return sorted(result)


def run_guard(
    control_path: Path,
    status_path: Path,
    parent_pid: int,
    backup_path: Path,
    log_path: Path,
) -> int:
    _configure_logging(log_path)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)
    LOGGER.info("Guardiano hosts avviato. Parent PID=%s", parent_pid)

    applied_domains: tuple[str, ...] | None = None
    last_heartbeat = time.time()
    state = "starting"
    message = "Inizializzazione"
    last_status_signature: tuple | None = None
    last_status_write = 0.0

    try:
        while not _STOP:
            control = _load_control(control_path)
            enabled = bool(control.get("enabled", False))
            stop = bool(control.get("stop", False))
            domains = _sanitize_domains(control.get("domains", []))
            requested = tuple(domains) if enabled else ()

            if stop:
                LOGGER.info("Richiesta di stop ricevuta.")
                break
            if parent_pid and not pid_alive(parent_pid):
                LOGGER.warning("Processo principale terminato: ripristino hosts.")
                break

            heartbeat = float(control.get("heartbeat", 0) or 0)
            if heartbeat > 0:
                last_heartbeat = max(last_heartbeat, heartbeat)
            if time.time() - last_heartbeat > _HEARTBEAT_TIMEOUT:
                LOGGER.warning("Heartbeat obsoleto: ripristino hosts per sicurezza.")
                break

            try:
                if enabled and domains and requested != applied_domains:
                    result = apply_hosts_block(domains, backup_path=backup_path)
                    applied_domains = requested
                    state = "active"
                    message = (
                        f"Protezione attiva: {result.entry_count} voci hosts."
                        if result.entry_count
                        else "Protezione attiva."
                    )
                    LOGGER.info(message)
                elif not enabled and applied_domains:
                    restore_hosts_block(backup_path=backup_path)
                    applied_domains = ()
                    state = "idle"
                    message = "Blocco siti sospeso."
                    LOGGER.info("Blocco hosts sospeso.")
                elif not enabled and applied_domains is None:
                    if has_managed_block():
                        restore_hosts_block(backup_path=backup_path)
                        state = "idle"
                        message = "Sezione hosts residua rimossa."
                        LOGGER.info("Sezione hosts residua rimossa.")
                    else:
                        state = "idle"
                        message = "Blocco siti inattivo."
                    applied_domains = ()
                elif enabled and not domains:
                    if has_managed_block():
                        restore_hosts_block(backup_path=backup_path)
                    applied_domains = ()
                    state = "idle"
                    message = "Nessun dominio configurato."
            except (HostsError, OSError, ValueError) as exc:
                state = "error"
                message = str(exc)
                LOGGER.exception("Errore nella gestione hosts.")
                time.sleep(3.0)

            signature = (state, message, enabled, tuple(domains))
            now = time.time()
            if signature != last_status_signature or now - last_status_write >= 10.0:
                _write_status(status_path, state, message, enabled, domains)
                last_status_signature = signature
                last_status_write = now
            time.sleep(_POLL_SECONDS)
    finally:
        try:
            if has_managed_block():
                restore_hosts_block(backup_path=backup_path)
        except Exception:
            LOGGER.exception("Ripristino finale del file hosts non riuscito.")
        _write_status(status_path, "stopped", "File hosts ripristinato.", False, [])
        LOGGER.info("Guardiano hosts terminato.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="StudyFlow --hosts-guard")
    parser.add_argument("--hosts-guard", action="store_true")
    parser.add_argument("--control", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)
    control_path = Path(args.control)
    status_path = Path(args.status)
    log_path = Path(args.log)
    backup_path = control_path.parent / "hosts_original.backup"
    try:
        return run_guard(control_path, status_path, args.parent_pid, backup_path, log_path) or 0
    except Exception:
        LOGGER.exception("Errore fatale del guardiano hosts.")
        try:
            restore_hosts_block(backup_path=backup_path)
        except Exception:
            LOGGER.exception("Ripristino di emergenza non riuscito.")
        _write_status(status_path, "error", "Errore fatale; hosts ripristinato.", False, [])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
