"""Test di integrazione leggeri eseguibili senza GUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from unittest.mock import patch

from study_manager.config import quality_score
from study_manager.database import Database
from study_manager.services.guard_client import GuardClient
from study_manager.services.hosts_service import (
    HOSTS_BEGIN_MARKER,
    apply_hosts_block,
    has_managed_block,
    restore_hosts_block,
    strip_managed_block,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.db.close_current_thread()
        self.temp.cleanup()

    def test_seed_and_settings(self) -> None:
        settings = self.db.get_settings()
        self.assertEqual(settings["pomodoro_work"], "25")
        self.assertGreaterEqual(len(self.db.list_blocked_sites()), 5)
        self.assertGreaterEqual(len(self.db.list_blocked_apps()), 3)
        self.db.set_setting("pomodoro_work", "40")
        self.assertEqual(self.db.get_settings()["pomodoro_work"], "40")

    def test_sessions_attempts_analytics_and_material(self) -> None:
        session_id = self.db.create_session("Matematica")
        self.db.finalize_session(session_id, 25, True)
        self.db.log_distraction(session_id, "website", "youtube.com")
        material_id = self.db.add_material("Dispense", "C:/tmp/test.pdf", "Matematica", 10)
        self.db.add_material_hours(material_id, 0.5)
        now = datetime.now()
        start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        stats = self.db.period_stats(start, end)
        self.assertEqual(stats["started"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["minutes"], 25)
        self.assertEqual(stats["attempts"], 1)
        self.assertEqual(stats["clean_completed"], 0)
        material = self.db.get_material(material_id)
        self.assertAlmostEqual(float(material["logged_hours"]), 0.5)

    def test_calendar_events(self) -> None:
        now = datetime.now().replace(microsecond=0)
        start = now.strftime("%Y-%m-%d %H:%M:%S")
        end = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        inserted = self.db.import_calendar_events(
            [
                {
                    "uid": "event-1",
                    "title": "Studio",
                    "subject": "Fisica",
                    "start": start,
                    "end": end,
                    "all_day": False,
                }
            ],
            "test.ics",
        )
        self.assertEqual(inserted, 1)
        events = self.db.get_events(
            (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Studio")


class HostsTests(unittest.TestCase):
    def test_apply_and_restore_preserve_user_content(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            hosts = Path(folder) / "hosts"
            backup = Path(folder) / "hosts.backup"
            original = "127.0.0.1 localhost\r\n# user line\r\n\r\n"
            hosts.write_text(original, encoding="utf-8", newline="")
            result = apply_hosts_block(
                ["youtube.com"],
                backup_path=backup,
                hosts_path=hosts,
                flush_dns=False,
            )
            self.assertTrue(result.changed)
            content = hosts.read_text(encoding="utf-8")
            self.assertIn(HOSTS_BEGIN_MARKER, content)
            self.assertIn("127.0.0.1 youtube.com", content)
            self.assertIn("127.0.0.1 www.youtube.com", content)
            self.assertIn("# user line", content)
            self.assertTrue(has_managed_block(content))
            self.assertTrue(restore_hosts_block(hosts_path=hosts, flush_dns=False))
            restored = hosts.read_text(encoding="utf-8")
            self.assertNotIn(HOSTS_BEGIN_MARKER, restored)
            self.assertIn("# user line", restored)
            self.assertEqual(strip_managed_block(strip_managed_block(original)), original)


class GuardClientTests(unittest.TestCase):
    def test_guard_process_applies_and_restores_temp_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hosts = root / "hosts"
            hosts.write_text(
                "127.0.0.1 localhost\r\n# keep me\r\n",
                encoding="utf-8",
                newline="",
            )
            control = root / "control.json"
            status = root / "status.json"
            log = root / "guard.log"
            control.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "domains": ["youtube.com"],
                        "stop": False,
                        "heartbeat": time.time(),
                        "parent_pid": os.getpid(),
                    }
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["STUDYFLOW_HOSTS_PATH"] = str(hosts)
            script = Path(__file__).resolve().parents[1] / "main.py"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--hosts-guard",
                    "--control",
                    str(control),
                    "--status",
                    str(status),
                    "--log",
                    str(log),
                    "--parent-pid",
                    str(os.getpid()),
                ],
                cwd=str(script.parent),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                applied = False
                for _ in range(50):
                    if has_managed_block(hosts.read_text(encoding="utf-8")):
                        applied = True
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    applied,
                    log.read_text(encoding="utf-8") if log.exists() else "log assente",
                )
                self.assertIn("youtube.com", hosts.read_text(encoding="utf-8"))
                control.write_text(
                    json.dumps(
                        {
                            "enabled": False,
                            "domains": ["youtube.com"],
                            "stop": True,
                            "heartbeat": time.time(),
                            "parent_pid": os.getpid(),
                        }
                    ),
                    encoding="utf-8",
                )
                process.wait(timeout=8)
                self.assertEqual(process.returncode, 0)
                restored = hosts.read_text(encoding="utf-8")
                self.assertNotIn(HOSTS_BEGIN_MARKER, restored)
                self.assertIn("# keep me", restored)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()

    def test_cleanup_without_residual_does_not_request_uac(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hosts = root / "hosts"
            hosts.write_text("127.0.0.1 localhost\r\n", encoding="utf-8", newline="")
            client = GuardClient(root / "control.json", root / "status.json", root / "guard.log")
            with patch.dict(os.environ, {"STUDYFLOW_HOSTS_PATH": str(hosts)}):
                with patch(
                    "study_manager.services.guard_client.launch_elevated",
                    return_value=True,
                ) as launch:
                    self.assertTrue(client.cleanup_hosts(timeout=0.2))
                    launch.assert_not_called()


class QualityTests(unittest.TestCase):
    def test_quality_bounds(self) -> None:
        self.assertEqual(quality_score({}), 0.0)
        perfect = quality_score(
            {"started": 4, "completed": 4, "clean_completed": 4, "minutes": 100, "attempts": 0}
        )
        self.assertEqual(perfect, 100.0)
        noisy = quality_score(
            {"started": 4, "completed": 2, "clean_completed": 0, "minutes": 50, "attempts": 30}
        )
        self.assertLess(noisy, perfect)


if __name__ == "__main__":
    unittest.main()
