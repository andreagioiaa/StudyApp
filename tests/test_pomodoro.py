"""Test del motore Pomodoro e dell'integrazione con il database."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from study_manager.database import Database
from study_manager.services.pomodoro import PomodoroEngine, TimerConfig
from study_manager.services.pomodoro_service import PomodoroService


class FakeBlocker:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.ended = 0

    def start_session(self, session_id: int) -> None:
        self.started.append(session_id)

    def end_session(self) -> None:
        self.ended += 1


class PomodoroEngineTests(unittest.TestCase):
    def test_phase_transitions(self) -> None:
        events: list[tuple] = []
        engine = PomodoroEngine(
            TimerConfig(
                work_minutes=1,
                short_break_minutes=1,
                long_break_minutes=1,
                cycles=2,
                auto_start_next=False,
                notification_sound=False,
            ),
            on_phase_start=lambda phase, seconds, subject: events.append(("start", phase)),
            on_phase_end=lambda phase, seconds, completed: events.append(
                ("end", phase, completed)
            ),
        )
        engine.start()
        time.sleep(0.05)
        engine._total_seconds = 1.0
        time.sleep(1.3)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["phase"], "short_break")
        self.assertEqual(snapshot["status"], "idle")
        self.assertIn(("end", "work", True), events)
        engine.start()
        engine._total_seconds = 1.0
        time.sleep(1.3)
        self.assertEqual(engine.snapshot()["phase"], "work")
        engine.shutdown()

    def test_service_saves_session_and_material(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            material_id = db.add_material("Test", "C:/tmp/x.pdf", "Fisica", 2)
            blocker = FakeBlocker()
            service = PomodoroService(db, blocker)
            ok, error = service.start_current(
                subject="Fisica", material_id=material_id, context_label="Materiale: Test"
            )
            self.assertTrue(ok, error)
            time.sleep(0.05)
            service._engine._total_seconds = 1.0
            time.sleep(1.3)
            sessions = db.list_recent_sessions(5)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["completed"], 1)
            self.assertGreaterEqual(sessions[0]["duration_minutes"], 0)
            self.assertEqual(len(blocker.started), 1)
            self.assertGreaterEqual(blocker.ended, 1)
            service.shutdown()
            db.close_all()
            db.close_current_thread()
            import gc
            import time as _time

            gc.collect()
            _time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
