"""Orchestrazione Pomodoro + database + blocco distrazioni."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Protocol

from ..database import Database
from ..utils import clamp, format_seconds, parse_datetime
from .pomodoro import PomodoroEngine, TimerConfig


LOGGER = logging.getLogger(__name__)


class BlockerProtocol(Protocol):
    def start_session(self, session_id: int) -> None: ...

    def end_session(self) -> None: ...


class PomodoroService:
    """Collega il motore timer alla persistenza e al blocker.

    Il contesto (materia, materiale, evento) resta associato alla serie di
    Pomodori finche' l'utente non esegue Reset o avvia un nuovo studio.
    """

    def __init__(self, db: Database, blocker: BlockerProtocol):
        self.db = db
        self.blocker = blocker
        self._lock = threading.RLock()
        self._revision = 0
        self._session_id: int | None = None
        self._material_id: int | None = None
        self._event_id: int | None = None
        self._context_label = ""
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""

        settings = self.db.get_settings()
        self._config = TimerConfig.from_settings(settings)
        self._engine = PomodoroEngine(
            self._config,
            on_phase_start=self._on_phase_start,
            on_phase_end=self._on_phase_end,
        )

    # ------------------------------------------------------------------ callbacks
    def _on_phase_start(self, phase: str, seconds: int, subject: str) -> None:
        del seconds
        if phase != "work":
            return
        with self._lock:
            event_id = self._event_id
        try:
            session_id = self.db.create_session(subject, calendar_event_id=event_id)
        except Exception as exc:
            LOGGER.exception("Creazione sessione non riuscita.")
            with self._lock:
                self._last_error = f"Impossibile salvare la sessione: {exc}"
                self._session_id = None
                self._revision += 1
            return
        with self._lock:
            self._session_id = session_id
            self._last_error = ""
            self._revision += 1
        try:
            self.blocker.start_session(session_id)
        except Exception:
            LOGGER.exception("Avvio blocco distrazioni non riuscito.")
            with self._lock:
                self._last_error = "Timer avviato, ma il blocco distrazioni ha segnalato un errore."
        LOGGER.info("Pomodoro avviato: sessione %s, materia '%s'.", session_id, subject)

    def _on_phase_end(self, phase: str, elapsed_seconds: int, completed: bool) -> None:
        if phase != "work":
            return
        with self._lock:
            session_id = self._session_id
            material_id = self._material_id
            context_label = self._context_label
            self._session_id = None
        if session_id is None:
            return
        minutes = max(0, int(round(elapsed_seconds / 60.0)))
        try:
            self.db.finalize_session(session_id, minutes, completed)
        except Exception:
            LOGGER.exception("Salvataggio sessione %s non riuscito.", session_id)
            with self._lock:
                self._last_error = "Impossibile aggiornare la sessione nel database."
        if material_id is not None and elapsed_seconds > 0:
            try:
                self.db.add_material_hours(material_id, elapsed_seconds / 3600.0)
            except Exception:
                LOGGER.exception("Aggiornamento ore materiale non riuscito.")
        try:
            self.blocker.end_session()
        except Exception:
            LOGGER.exception("Chiusura blocco distrazioni non riuscita.")
        with self._lock:
            self._last_result = {
                "session_id": session_id,
                "subject": self._engine.snapshot().get("subject", "Studio"),
                "minutes": minutes,
                "completed": bool(completed),
                "context": context_label,
            }
            self._revision += 1
        LOGGER.info(
            "Pomodoro %s: %s minuti%s.",
            "completato" if completed else "interrotto",
            minutes,
            f" - {context_label}" if context_label else "",
        )

    # ------------------------------------------------------------------ controls
    def start_current(
        self,
        subject: str | None = None,
        work_minutes: float | None = None,
        material_id: int | None = None,
        event_id: int | None = None,
        context_label: str = "",
        force_work_phase: bool = False,
    ) -> tuple[bool, str]:
        snapshot = self._engine.snapshot()
        if snapshot["status"] in {"running", "paused"}:
            return False, "Una sessione e' gia' in corso. Mettila in pausa o termina il timer."
        new_subject: str | None = None
        with self._lock:
            if subject is not None:
                self._material_id = material_id
                self._event_id = event_id
                self._context_label = context_label.strip()
                new_subject = subject
            elif material_id is not None or event_id is not None:
                self._material_id = material_id
                self._event_id = event_id
                self._context_label = context_label.strip()
        if new_subject is not None:
            self._engine.set_subject(new_subject)
        if force_work_phase and snapshot["phase"] != "work":
            self._engine.prepare_work_idle(reset_cycles=False)
        override = None
        if work_minutes is not None and (force_work_phase or snapshot["phase"] == "work"):
            override = int(round(clamp(float(work_minutes), 1.0, 180.0) * 60))
        started = self._engine.start(override)
        if not started:
            return False, "Impossibile avviare il timer nello stato corrente."
        with self._lock:
            self._revision += 1
        return True, ""

    def start_from_event(self, event: dict[str, Any]) -> tuple[bool, str]:
        start = parse_datetime(str(event.get("start", "")))
        end = parse_datetime(str(event.get("end", "")))
        duration_minutes = 25.0
        if start and end and end > start:
            duration_minutes = clamp((end - start).total_seconds() / 60.0, 5.0, 120.0)
        subject = str(event.get("subject") or event.get("title") or "Studio").strip()
        label = f"Evento: {event.get('title', subject)}"
        if start:
            label += f" ({start.strftime('%d/%m %H:%M')})"
        return self.start_current(
            subject=subject,
            work_minutes=duration_minutes,
            event_id=int(event["id"]) if event.get("id") else None,
            context_label=label,
            force_work_phase=True,
        )

    def start_from_material(self, material: dict[str, Any]) -> tuple[bool, str]:
        subject = str(material.get("subject") or material.get("title") or "Studio").strip()
        return self.start_current(
            subject=subject,
            material_id=int(material["id"]),
            event_id=int(material["calendar_event_id"])
            if material.get("calendar_event_id")
            else None,
            context_label=f"Materiale: {material.get('title', '')}",
            force_work_phase=True,
        )

    def toggle_pause(self) -> str:
        status = self._engine.toggle_pause()
        with self._lock:
            self._revision += 1
        return status

    def skip(self) -> None:
        self._engine.skip()
        with self._lock:
            self._revision += 1

    def reset(self) -> None:
        self._engine.reset()
        with self._lock:
            self._material_id = None
            self._event_id = None
            self._context_label = ""
            self._revision += 1

    def reload_config(self) -> None:
        settings = self.db.get_settings()
        self._config = TimerConfig.from_settings(settings)
        self._engine.update_config(self._config)
        with self._lock:
            self._revision += 1

    def shutdown(self) -> None:
        self._engine.shutdown()
        with self._lock:
            self._session_id = None
            self._revision += 1

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        data = self._engine.snapshot()
        with self._lock:
            data.update(
                {
                    "session_id": self._session_id,
                    "material_id": self._material_id,
                    "event_id": self._event_id,
                    "context_label": self._context_label,
                    "last_result": dict(self._last_result) if self._last_result else None,
                    "last_error": self._last_error,
                    "revision": self._revision + int(data.get("revision", 0)),
                    "remaining_label": format_seconds(data["remaining_seconds"]),
                }
            )
        return data

    @property
    def is_work_active(self) -> bool:
        data = self._engine.snapshot()
        return data["phase"] == "work" and data["status"] in {"running", "paused"}
