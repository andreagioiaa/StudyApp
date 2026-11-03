"""Motore Pomodoro thread-safe e indipendente dall'interfaccia grafica."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..config import DEFAULT_SETTINGS
from ..utils import clamp, safe_float, safe_int


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimerConfig:
    work_minutes: float = 25.0
    short_break_minutes: float = 5.0
    long_break_minutes: float = 15.0
    cycles: int = 4
    auto_start_next: bool = True
    notification_sound: bool = True

    @classmethod
    def from_settings(cls, settings: dict[str, str]) -> "TimerConfig":
        return cls(
            work_minutes=clamp(
                safe_float(settings.get("pomodoro_work", "25"), 25.0), 1.0, 180.0
            ),
            short_break_minutes=clamp(
                safe_float(settings.get("pomodoro_short", "5"), 5.0), 1.0, 60.0
            ),
            long_break_minutes=clamp(
                safe_float(settings.get("pomodoro_long", "15"), 15.0), 1.0, 120.0
            ),
            cycles=max(1, min(12, safe_int(settings.get("pomodoro_cycles", "4"), 4))),
            auto_start_next=str(settings.get("auto_start_next", "1")).lower()
            in {"1", "true", "yes", "on"},
            notification_sound=str(settings.get("notification_sound", "1")).lower()
            in {"1", "true", "yes", "on"},
        )

    def duration_seconds(self, phase: str) -> int:
        if phase == "short_break":
            return max(60, int(round(self.short_break_minutes * 60)))
        if phase == "long_break":
            return max(60, int(round(self.long_break_minutes * 60)))
        return max(60, int(round(self.work_minutes * 60)))


class PomodoroEngine:
    """State machine con clock monotonic e thread dedicato.

    Il thread aggiorna il countdown e completa la fase anche se la UI e'
    momentaneamente occupata. La UI legge soltanto snapshot immutabili.
    """

    def __init__(
        self,
        config: TimerConfig,
        on_phase_start: Callable[[str, int, str], None] | None = None,
        on_phase_end: Callable[[str, int, bool], None] | None = None,
    ):
        self._config = config
        self._on_phase_start = on_phase_start
        self._on_phase_end = on_phase_end
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        self._phase = "work"
        self._status = "idle"
        self._subject = "Studio"
        self._total_seconds = float(config.duration_seconds("work"))
        self._accumulated = 0.0
        self._run_started_at: float | None = None
        self._pending_override: int | None = None
        self._completed_work = 0
        self._revision = 0
        self._last_finished_phase: str | None = None

        self._thread = threading.Thread(
            target=self._timer_loop,
            name="StudyFlow-Pomodoro",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------ config
    def update_config(self, config: TimerConfig) -> None:
        with self._lock:
            self._config = config
            if self._status == "idle":
                self._total_seconds = float(config.duration_seconds(self._phase))
                self._accumulated = 0.0
                self._run_started_at = None
            # La fase in corso mantiene la durata con cui e' iniziata.
            self._revision += 1

    def set_subject(self, subject: str) -> None:
        with self._lock:
            self._subject = subject.strip()[:160] or "Studio"
            self._revision += 1

    # ------------------------------------------------------------------ controls
    def start(self, work_override_seconds: int | None = None) -> bool:
        with self._lock:
            if self._status == "running":
                return False
            if self._status == "paused":
                self._run_started_at = time.monotonic()
                self._status = "running"
                self._revision += 1
                self._wake_event.set()
                return True
            if self._status != "idle":
                return False
            if self._phase == "work" and work_override_seconds is not None:
                self._pending_override = max(60, int(work_override_seconds))
            self._start_phase_locked()
            self._revision += 1
            self._wake_event.set()
            return True

    def toggle_pause(self) -> str:
        now = time.monotonic()
        with self._lock:
            if self._status == "running":
                if self._run_started_at is not None:
                    self._accumulated += now - self._run_started_at
                    self._run_started_at = None
                self._status = "paused"
            elif self._status == "paused":
                self._run_started_at = now
                self._status = "running"
                self._wake_event.set()
            self._revision += 1
            return self._status

    def skip(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._status in {"running", "paused"}:
                elapsed = self._elapsed_seconds_locked(now)
                phase = self._phase
                self._accumulated = 0.0
                self._run_started_at = None
                self._status = "idle"
                self._notify_end(phase, elapsed, False)
                if phase == "work":
                    next_phase = "short_break"
                else:
                    next_phase = "work"
            elif self._status == "idle":
                if self._phase == "work":
                    return
                next_phase = "work"
            else:
                return
            self._set_idle_phase_locked(next_phase)
            self._revision += 1
            self._wake_event.set()

    def reset(self, reset_cycles: bool = True) -> None:
        now = time.monotonic()
        with self._lock:
            if self._status in {"running", "paused"}:
                elapsed = self._elapsed_seconds_locked(now)
                phase = self._phase
                self._notify_end(phase, elapsed, False)
            if reset_cycles:
                self._completed_work = 0
            self._pending_override = None
            self._phase = "work"
            self._status = "idle"
            self._total_seconds = float(self._config.duration_seconds("work"))
            self._accumulated = 0.0
            self._run_started_at = None
            self._last_finished_phase = None
            self._revision += 1
            self._wake_event.set()

    def prepare_work_idle(self, reset_cycles: bool = False) -> None:
        with self._lock:
            if self._status in {"running", "paused"}:
                return
            if reset_cycles:
                self._completed_work = 0
            self._phase = "work"
            self._status = "idle"
            self._total_seconds = float(self._config.duration_seconds("work"))
            self._accumulated = 0.0
            self._run_started_at = None
            self._revision += 1

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        now = time.monotonic()
        with self._lock:
            if self._status in {"running", "paused"}:
                elapsed = self._elapsed_seconds_locked(now)
                phase = self._phase
                self._status = "idle"
                self._accumulated = 0.0
                self._run_started_at = None
                self._notify_end(phase, elapsed, False)
                self._phase = "work"
            self._revision += 1

    # ------------------------------------------------------------------ timer
    def _timer_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(0.2)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self._tick()
            except Exception:
                LOGGER.exception("Errore nel thread Pomodoro.")
                time.sleep(0.5)

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._status != "running":
                return
            remaining = self._remaining_seconds_locked(now)
            if remaining > 0.01:
                return
            phase = self._phase
            self._notify_end(phase, float(self._total_seconds), True)
            self._last_finished_phase = phase
            self._status = "idle"
            self._accumulated = 0.0
            self._run_started_at = None
            if phase == "work":
                self._completed_work += 1
                cycle_position = self._completed_work % max(1, self._config.cycles)
                next_phase = "long_break" if cycle_position == 0 else "short_break"
            else:
                next_phase = "work"
            self._phase = next_phase
            self._total_seconds = float(self._config.duration_seconds(next_phase))
            if self._config.auto_start_next:
                self._start_phase_locked()
            else:
                self._accumulated = 0.0
                self._run_started_at = None
            self._revision += 1
            self._wake_event.set()

    # ------------------------------------------------------------------ helpers
    def _start_phase_locked(self) -> None:
        if self._phase == "work" and self._pending_override:
            self._total_seconds = float(max(60, self._pending_override))
            self._pending_override = None
        else:
            self._total_seconds = float(self._config.duration_seconds(self._phase))
        self._accumulated = 0.0
        self._run_started_at = time.monotonic()
        self._status = "running"
        self._last_finished_phase = None
        self._notify_start(self._phase, int(round(self._total_seconds)))

    def _set_idle_phase_locked(self, phase: str) -> None:
        self._phase = phase
        self._status = "idle"
        self._total_seconds = float(self._config.duration_seconds(phase))
        self._accumulated = 0.0
        self._run_started_at = None

    def _remaining_seconds_locked(self, now: float) -> float:
        elapsed = self._elapsed_seconds_locked(now)
        return max(0.0, self._total_seconds - elapsed)

    def _elapsed_seconds_locked(self, now: float) -> float:
        elapsed = self._accumulated
        if self._status == "running" and self._run_started_at is not None:
            elapsed += max(0.0, now - self._run_started_at)
        return min(float(self._total_seconds), elapsed)

    def _notify_start(self, phase: str, seconds: int) -> None:
        callback = self._on_phase_start
        if callback is None:
            return
        try:
            callback(phase, seconds, self._subject)
        except Exception:
            LOGGER.exception("Callback di avvio fase Pomodoro non riuscita.")

    def _notify_end(self, phase: str, elapsed_seconds: float, completed: bool) -> None:
        callback = self._on_phase_end
        if callback is not None:
            try:
                callback(phase, int(round(elapsed_seconds)), completed)
            except Exception:
                LOGGER.exception("Callback di fine fase Pomodoro non riuscita.")
        if completed and self._config.notification_sound:
            self._beep()
        elif phase == "work" and elapsed_seconds >= 1:
            LOGGER.info("Sessione di lavoro interrotta dopo %ss.", int(elapsed_seconds))

    @staticmethod
    def _beep() -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except (ImportError, RuntimeError, OSError):
            LOGGER.debug("Notifica sonora non disponibile.", exc_info=True)

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            remaining = self._remaining_seconds_locked(now) if self._status != "idle" else self._total_seconds
            total = max(1.0, self._total_seconds)
            elapsed = max(0.0, total - remaining)
            progress = min(1.0, max(0.0, elapsed / total))
            return {
                "phase": self._phase,
                "status": self._status,
                "remaining_seconds": int(round(remaining)),
                "total_seconds": int(round(total)),
                "progress": progress,
                "subject": self._subject,
                "completed_work": self._completed_work,
                "cycle_position": self._completed_work % max(1, self._config.cycles),
                "cycle_target": self._config.cycles,
                "auto_start_next": self._config.auto_start_next,
                "revision": self._revision,
                "last_finished_phase": self._last_finished_phase,
                "config": self._config,
            }
