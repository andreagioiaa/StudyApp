"""Test della sincronizzazione Google Calendar (senza rete: API simulata)."""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from study_manager.database import (
    EVENT_PENDING_CREATE,
    EVENT_PENDING_DELETE,
    EVENT_PENDING_UPDATE,
    EVENT_SOURCE_GOOGLE,
    EVENT_SOURCE_MANUAL,
    SCHEMA_VERSION,
    Database,
)
from study_manager.logging_setup import SecretMaskingFilter, mask_secrets
from study_manager.services.backup import export_backup, import_backup
from study_manager.services.calendar_sync import (
    CalendarSyncService,
    GoogleApiError,
    format_elapsed,
    local_event_to_google,
    text_color_for,
)
from study_manager.services.google_auth import (
    GoogleAuthError,
    GoogleOfflineError,
    make_pkce_pair,
    parse_client_credentials,
    protect_secret,
    unprotect_secret,
)


CAL_SO = "so-cal@group.calendar.google.com"
CAL_IS = "is-cal@group.calendar.google.com"
CAL_FARRA = "farra@group.calendar.google.com"


def _iso(moment: datetime) -> str:
    return moment.astimezone().isoformat()


class FakeAuth:
    def __init__(self) -> None:
        self.email = ""

    def is_connected(self) -> bool:
        return True

    def set_account_email(self, email: str) -> None:
        self.email = email


class FakeGoogle:
    """Calendar API v3 in memoria, con modalita' offline e registro chiamate."""

    def __init__(self) -> None:
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.offline = False
        self.calls: list[tuple[str, ...]] = []
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self._counter = 0
        self._guard = threading.Lock()
        self.on_insert = None

    def add_calendar(self, calendar_id: str, summary: str, color: str, role: str = "owner") -> None:
        self.calendars[calendar_id] = {
            "id": calendar_id,
            "summary": summary,
            "backgroundColor": color,
            "accessRole": role,
        }
        self.events.setdefault(calendar_id, {})

    def add_event(self, calendar_id: str, title: str, start: datetime, hours: float = 2, **extra: Any) -> str:
        with self._guard:
            self._counter += 1
            event_id = f"evt{self._counter}"
        self.events[calendar_id][event_id] = {
            "id": event_id,
            "summary": title,
            "status": "confirmed",
            "start": {"dateTime": _iso(start)},
            "end": {"dateTime": _iso(start + timedelta(hours=hours))},
            **extra,
        }
        return event_id

    def _enter(self, *call: str) -> None:
        if self.offline:
            raise GoogleOfflineError("offline")
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(call)
        if self.delay:
            time.sleep(self.delay)
        with self._guard:
            self.active -= 1

    # --- API
    def list_calendars(self) -> list[dict[str, Any]]:
        self._enter("calendarList")
        return [dict(item) for item in self.calendars.values()]

    def get_colors(self) -> dict[str, Any]:
        self._enter("colors")
        return {"calendar": {}, "event": {"11": {"background": "#dc2127"}, "5": {"background": "#fbd75b"}}}

    def list_events(self, calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
        self._enter("list", calendar_id)
        return [dict(item) for item in self.events.get(calendar_id, {}).values()]

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._enter("insert", calendar_id)
        with self._guard:
            self._counter += 1
            event_id = f"new{self._counter}"
        resource = {"id": event_id, "status": "confirmed", **body}
        self.events[calendar_id][event_id] = resource
        if self.on_insert is not None:
            callback, self.on_insert = self.on_insert, None
            callback()
        return dict(resource)

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._enter("patch", calendar_id, event_id)
        if event_id not in self.events.get(calendar_id, {}):
            raise GoogleApiError(404, "Not Found")
        resource = self.events[calendar_id][event_id]
        for key, value in body.items():
            if isinstance(value, dict):
                merged = dict(resource.get(key) or {})
                merged.update(value)
                resource[key] = {k: v for k, v in merged.items() if v is not None}
            elif value is None:
                resource.pop(key, None)
            else:
                resource[key] = value
        return dict(resource)

    def move_event(self, calendar_id: str, event_id: str, destination: str) -> dict[str, Any]:
        self._enter("move", calendar_id, event_id, destination)
        if event_id not in self.events.get(calendar_id, {}):
            raise GoogleApiError(404, "Not Found")
        resource = self.events[calendar_id].pop(event_id)
        self.events[destination][event_id] = resource
        return dict(resource)

    def delete_event(self, calendar_id: str, event_id: str) -> int:
        self._enter("delete", calendar_id, event_id)
        if self.events.get(calendar_id, {}).pop(event_id, None) is None:
            return 410
        return 204

    def create_calendar(self, summary: str) -> dict[str, Any]:
        self._enter("createCalendar", summary)
        calendar_id = f"{summary.lower().replace(' ', '-')}@group.calendar.google.com"
        self.add_calendar(calendar_id, summary, "#9E69AF")
        return dict(self.calendars[calendar_id])


class CalendarSyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "sync.db")
        self.google = FakeGoogle()
        self.google.add_calendar(CAL_SO, "Sistemi Operativi", "#4285f4")
        self.google.add_calendar(CAL_IS, "Ingegneria del Software", "#0B8043")
        self.google.add_calendar(CAL_FARRA, "Farra Calcio", "#F4511E", role="reader")
        self.db.save_subject("Sistemi Operativi", cfu=9)
        self.db.save_subject("ingegneria del software", cfu=6)
        self.service = CalendarSyncService(self.db, auth=FakeAuth(), client=self.google)
        self.tomorrow = (datetime.now() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    def tearDown(self) -> None:
        self.service.wait_idle(5)
        self.db.close_all()
        self.temp.cleanup()

    def _event_by_title(self, title: str) -> dict[str, Any]:
        row = self.db._connection().execute(
            "SELECT * FROM calendar_events WHERE title = ?", (title,)
        ).fetchone()
        self.assertIsNotNone(row, f"Evento '{title}' non trovato")
        return dict(row)


class MigrationTests(unittest.TestCase):
    def test_migration_from_v6_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL DEFAULT '', duration_minutes INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, timestamp TEXT NOT NULL);
                CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE, teacher_name TEXT NOT NULL DEFAULT '',
                    teacher_email TEXT NOT NULL DEFAULT '', cfu INTEGER NOT NULL DEFAULT 0,
                    exam_date TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE calendar_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL, title TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
                    start TEXT NOT NULL, end TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', all_day INTEGER NOT NULL DEFAULT 0,
                    source_file TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL,
                    UNIQUE(uid, start));
                INSERT INTO subjects(name, created_at, updated_at) VALUES ('Analisi', 'x', 'x');
                INSERT INTO calendar_events(uid, title, subject, start, end, imported_at)
                    VALUES ('u1', 'Lezione', 'Analisi', '2026-11-12 09:00:00',
                            '2026-11-12 11:00:00', 'x');
                INSERT INTO sessions(subject, duration_minutes, completed, timestamp)
                    VALUES ('Analisi', 25, 1, '2026-11-11 10:00:00');
                PRAGMA user_version = 6;
                """
            )
            connection.close()

            db = Database(path)
            try:
                conn = db._connection()
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 7)
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(calendar_events)")}
                for column in ("source", "google_event_id", "google_calendar_id", "color_hex"):
                    self.assertIn(column, columns)
                subject_columns = {row["name"] for row in conn.execute("PRAGMA table_info(subjects)")}
                self.assertTrue({"google_calendar_id", "color_hex"} <= subject_columns)
                session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
                self.assertIn("calendar_event_id", session_columns)
                event = conn.execute("SELECT * FROM calendar_events").fetchone()
                self.assertEqual(event["source"], "ics_file")
                indexes = {row["name"] for row in conn.execute("PRAGMA index_list(calendar_events)")}
                self.assertIn("idx_events_google", indexes)
                self.assertEqual(len(db.list_recent_sessions()), 1)
                # Indice univoco parziale: due righe senza id Google sono ammesse...
                db.save_local_event(None, "A", "", "2026-11-12 09:00", "2026-11-12 10:00")
                db.save_local_event(None, "B", "", "2026-11-12 09:00", "2026-11-12 10:00")
                # ...ma non due con la stessa coppia (calendario, evento).
                conn.execute(
                    "UPDATE calendar_events SET google_calendar_id = 'c', google_event_id = 'e' "
                    "WHERE title = 'A'"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE calendar_events SET google_calendar_id = 'c', "
                        "google_event_id = 'e' WHERE title = 'B'"
                    )
                # Riaprire il database e' idempotente.
                db.close_all()
                Database(path).close_all()
            finally:
                db.close_all()


class PullTests(CalendarSyncTestCase):
    def test_multi_calendar_sync_with_colors(self) -> None:
        self.google.add_event(CAL_SO, "Lezione SO", self.tomorrow)
        self.google.add_event(CAL_IS, "Lezione IS", self.tomorrow + timedelta(hours=3), colorId="11")
        self.google.add_event(CAL_FARRA, "Allenamento", self.tomorrow + timedelta(hours=10))
        self.google.add_event(CAL_FARRA, "Vecchio", datetime.now() - timedelta(days=2), status="cancelled")

        self.service.refresh_calendars()
        # Abbinamento automatico per nome (case-insensitive) + abilitazione.
        mapping = self.db.subject_calendar_map()
        self.assertEqual(mapping[CAL_SO]["name"], "Sistemi Operativi")
        self.assertEqual(mapping[CAL_IS]["name"], "ingegneria del software")
        self.assertEqual(mapping[CAL_SO]["color_hex"], "#4285F4")
        self.assertNotIn(CAL_FARRA, mapping)
        self.assertEqual(set(self.service.enabled_calendars()), {CAL_SO, CAL_IS})
        self.service.set_enabled_calendars([CAL_SO, CAL_IS, CAL_FARRA])

        result = self.service.sync_now()
        self.assertEqual(result["pulled"]["inserted"], 3)
        so = self._event_by_title("Lezione SO")
        self.assertEqual((so["subject"], so["color_hex"], so["source"]), ("Sistemi Operativi", "#4285F4", EVENT_SOURCE_GOOGLE))
        is_event = self._event_by_title("Lezione IS")
        self.assertEqual(is_event["color_hex"], "#DC2127")  # colorId dell'evento
        self.assertEqual(is_event["color_id"], "11")
        farra = self._event_by_title("Allenamento")
        self.assertEqual(farra["subject"], "Allenamento")  # fallback sul titolo
        self.assertEqual(farra["color_hex"], "#F4511E")
        self.assertTrue(self.service.last_sync_at().endswith("Z"))
        subject = self.db.get_subject("Sistemi Operativi")
        self.assertEqual(subject["google_calendar_id"], CAL_SO)

        # Seconda sync: aggiornamento, nessun duplicato.
        event_id = next(iter(self.google.events[CAL_SO]))
        self.google.events[CAL_SO][event_id]["summary"] = "Lezione SO (aula 3)"
        result = self.service.sync_now()
        self.assertEqual(result["pulled"]["inserted"], 0)
        self.assertEqual(self._event_by_title("Lezione SO (aula 3)")["id"], so["id"])

        # Calendario disattivato: i suoi eventi spariscono dall'agenda.
        self.service.set_enabled_calendars([CAL_SO, CAL_IS])
        self.service.sync_now()
        start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        titles = {event["title"] for event in self.db.get_events(start, end)}
        self.assertNotIn("Allenamento", titles)

    def test_mirror_deletion_keeps_sessions_and_materials(self) -> None:
        keep_id = self.google.add_event(CAL_SO, "Resta", self.tomorrow)
        gone_id = self.google.add_event(CAL_SO, "Sparisce", self.tomorrow + timedelta(hours=4))
        self.service.refresh_calendars()
        self.service.sync_now()
        local = self._event_by_title("Sparisce")
        material_id = self.db.add_material("Slide", "C:/slide.pdf", "Sistemi Operativi", 4, local["id"])
        session_id = self.db.create_session("Sistemi Operativi", calendar_event_id=local["id"])
        self.db.finalize_session(session_id, 25, True)
        self.assertEqual(self.db.get_session(session_id)["calendar_event_id"], local["id"])

        del self.google.events[CAL_SO][gone_id]
        result = self.service.sync_now()
        self.assertEqual(result["pulled"]["removed"], 1)
        self.assertIsNone(self.db.get_event(local["id"]))
        self.assertIsNotNone(self._event_by_title("Resta"))
        self.assertIn(keep_id, self.google.events[CAL_SO])
        material = self.db.get_material(material_id)
        self.assertIsNotNone(material)
        self.assertIsNone(material["calendar_event_id"])
        session = self.db.get_session(session_id)
        self.assertIsNotNone(session)
        self.assertIsNone(session["calendar_event_id"])
        self.assertEqual(session["duration_minutes"], 25)

    def test_pull_never_overwrites_pending_changes(self) -> None:
        event_id = self.google.add_event(CAL_SO, "Originale", self.tomorrow)
        other_id = self.google.add_event(CAL_SO, "Da eliminare", self.tomorrow + timedelta(hours=5))
        self.service.refresh_calendars()
        self.service.sync_now()
        local = self._event_by_title("Originale")
        self.db.save_local_event(
            local["id"], "Modificato qui", "Sistemi Operativi", local["start"], local["end"]
        )
        to_delete = self._event_by_title("Da eliminare")
        self.assertEqual(self.db.delete_local_event(to_delete["id"]), "queued")
        self.google.events[CAL_SO][event_id]["summary"] = "Modificato su Google"

        totals = self.service.pull()  # pull senza flush
        self.assertGreaterEqual(totals["skipped"], 2)
        self.assertEqual(self.db.get_event(local["id"])["title"], "Modificato qui")
        self.assertEqual(self.db.get_event(local["id"])["source"], EVENT_PENDING_UPDATE)
        tombstone = self.db.get_event(to_delete["id"])
        self.assertEqual(tombstone["source"], EVENT_PENDING_DELETE)
        self.assertIn(other_id, self.google.events[CAL_SO])


class QueueTests(CalendarSyncTestCase):
    def test_offline_create_update_delete_flushed_on_next_sync(self) -> None:
        edit_id = self.google.add_event(CAL_SO, "Da modificare", self.tomorrow)
        drop_id = self.google.add_event(CAL_IS, "Da cancellare", self.tomorrow + timedelta(hours=2))
        self.service.refresh_calendars()
        self.service.sync_now()

        self.google.offline = True
        start = self.tomorrow + timedelta(days=1)
        new_id = self.db.save_local_event(
            None,
            "Ripasso offline",
            "Ingegneria del Software",
            start,
            start + timedelta(hours=1),
            description="Capitolo 4",
            color_id="5",
            color_hex="#FBD75B",
        )
        created = self.db.get_event(new_id)
        self.assertEqual(created["source"], EVENT_PENDING_CREATE)
        self.assertEqual(created["google_calendar_id"], CAL_IS)
        edit_local = self._event_by_title("Da modificare")
        self.db.save_local_event(
            edit_local["id"], "Modificato offline", "Sistemi Operativi",
            edit_local["start"], edit_local["end"], all_day=False,
        )
        drop_local = self._event_by_title("Da cancellare")
        self.assertEqual(self.db.delete_local_event(drop_local["id"]), "queued")

        # Il tombstone non compare nell'agenda.
        window = (
            (self.tomorrow - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            (self.tomorrow + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        titles = {event["title"] for event in self.db.get_events(*window)}
        self.assertNotIn("Da cancellare", titles)
        self.assertIn("Ripasso offline", titles)
        self.assertEqual(self.db.count_pending_events(), 3)

        # Flush in background dopo l'azione UI: offline, la coda resta intatta.
        self.service.request_flush()
        self.assertTrue(self.service.wait_idle(5))
        self.assertTrue(self.service.offline)
        self.assertEqual(self.db.count_pending_events(), 3)
        with self.assertRaises(GoogleOfflineError):
            self.service.sync_now()

        self.google.offline = False
        self.service.sync_now()
        self.assertEqual(self.db.count_pending_events(), 0)
        created = self.db.get_event(new_id)
        self.assertEqual(created["source"], EVENT_SOURCE_GOOGLE)
        remote = self.google.events[CAL_IS][created["google_event_id"]]
        self.assertEqual(remote["summary"], "Ripasso offline")
        self.assertEqual(remote["colorId"], "5")
        self.assertEqual(remote["description"], "Capitolo 4")
        self.assertEqual(self.google.events[CAL_SO][edit_id]["summary"], "Modificato offline")
        self.assertNotIn(drop_id, self.google.events[CAL_IS])
        self.assertIsNone(self.db.get_event(drop_local["id"]))  # tombstone rimosso
        self.assertIn(("delete", CAL_IS, drop_id), self.google.calls)
        # Il pull successivo non duplica l'evento appena creato.
        self.service.sync_now()
        count = self.db._connection().execute(
            "SELECT COUNT(*) FROM calendar_events WHERE title = 'Ripasso offline'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_background_flush_after_ui_action(self) -> None:
        self.service.refresh_calendars()
        new_id = self.db.save_local_event(
            None, "Subito", "Sistemi Operativi", self.tomorrow, self.tomorrow + timedelta(hours=1)
        )
        finished = threading.Event()
        self.service.add_listener(lambda kind, data: finished.set() if kind == "flush_finished" else None)
        self.service.request_flush()
        self.assertTrue(finished.wait(5))
        event = self.db.get_event(new_id)
        self.assertEqual(event["source"], EVENT_SOURCE_GOOGLE)
        self.assertIn(event["google_event_id"], self.google.events[CAL_SO])

    def test_move_event_between_subject_calendars(self) -> None:
        event_id = self.google.add_event(CAL_SO, "Esercitazione", self.tomorrow)
        self.service.refresh_calendars()
        self.service.sync_now()
        local = self._event_by_title("Esercitazione")
        self.assertEqual(local["color_hex"], "#4285F4")

        self.google.offline = True
        self.db.save_local_event(
            local["id"], "Esercitazione", "Ingegneria del Software", local["start"], local["end"]
        )
        moved = self.db.get_event(local["id"])
        self.assertEqual(moved["source"], EVENT_PENDING_UPDATE)
        self.assertEqual(moved["color_hex"], "#0B8043")
        self.assertEqual(moved["google_calendar_id"], CAL_IS)
        self.assertEqual(moved["previous_google_calendar_id"], CAL_SO)

        self.google.offline = False
        self.google.calls.clear()
        self.service.sync_now()
        operations = [call for call in self.google.calls if call[0] in {"move", "patch"}]
        self.assertEqual(
            operations,
            [("move", CAL_SO, event_id, CAL_IS), ("patch", CAL_IS, event_id)],
        )
        self.assertNotIn(event_id, self.google.events[CAL_SO])
        self.assertIn(event_id, self.google.events[CAL_IS])
        final = self.db.get_event(local["id"])
        self.assertEqual(final["source"], EVENT_SOURCE_GOOGLE)
        self.assertEqual(final["google_calendar_id"], CAL_IS)
        self.assertIsNone(final["previous_google_calendar_id"])
        self.assertEqual(final["subject"], "ingegneria del software")
        self.assertEqual(final["color_hex"], "#0B8043")
        self.assertEqual(self.service.pull()["removed"], 0)

    def test_delete_after_unsent_move_targets_original_calendar(self) -> None:
        event_id = self.google.add_event(CAL_SO, "Spostato e cancellato", self.tomorrow)
        self.service.refresh_calendars()
        self.service.sync_now()
        local = self._event_by_title("Spostato e cancellato")
        self.db.save_local_event(local["id"], local["title"], "Ingegneria del Software", local["start"], local["end"])
        self.db.delete_local_event(local["id"])
        self.service.sync_now()
        self.assertNotIn(event_id, self.google.events[CAL_SO])
        self.assertIsNone(self.db.get_event(local["id"]))

    def test_edit_while_create_in_flight_is_not_lost(self) -> None:
        self.service.refresh_calendars()
        new_id = self.db.save_local_event(
            None, "Prima", "Sistemi Operativi", self.tomorrow, self.tomorrow + timedelta(hours=1)
        )
        current = self.db.get_event(new_id)
        self.google.on_insert = lambda: self.db.save_local_event(
            new_id, "Dopo", "Sistemi Operativi", current["start"], current["end"]
        )
        with self.service._lock:
            self.service.flush_queue()
        event = self.db.get_event(new_id)
        self.assertEqual(event["source"], EVENT_PENDING_UPDATE)
        self.service.sync_now()
        self.assertEqual(self.google.events[CAL_SO][event["google_event_id"]]["summary"], "Dopo")

    def test_manual_event_without_calendar_stays_local(self) -> None:
        self.service.refresh_calendars()
        local_id = self.db.save_local_event(
            None, "Studio libero", "Fisica", self.tomorrow, self.tomorrow + timedelta(hours=1)
        )
        self.assertEqual(self.db.get_event(local_id)["source"], EVENT_SOURCE_MANUAL)
        self.service.sync_now()
        self.assertEqual(self.db.get_event(local_id)["source"], EVENT_SOURCE_MANUAL)
        self.assertEqual(self.db.delete_local_event(local_id), "deleted")
        self.assertIsNone(self.db.get_event(local_id))

    def test_all_day_body_and_patch_switch(self) -> None:
        body = local_event_to_google(
            {"title": "Esame", "start": "2027-01-20 00:00:00", "end": "2027-01-21 00:00:00", "all_day": 1}
        )
        self.assertEqual(body["start"], {"date": "2027-01-20"})
        self.assertEqual(body["end"], {"date": "2027-01-21"})
        patch = local_event_to_google(
            {"title": "Esame", "start": "2027-01-20 09:00:00", "end": "2027-01-20 11:00:00", "all_day": 0},
            for_patch=True,
        )
        self.assertIsNone(patch["start"]["date"])
        self.assertIn("dateTime", patch["start"])
        self.assertIsNone(patch["colorId"])

    def test_auto_create_calendar_for_new_subject(self) -> None:
        self.db.set_setting("google_auto_create_calendar", "1")
        self.service.refresh_calendars()
        self.db.save_subject("Reti di Calcolatori")
        calendar_id = self.service.ensure_calendar_for_subject("Reti di Calcolatori")
        self.assertIsNotNone(calendar_id)
        self.assertIn(calendar_id, self.service.enabled_calendars())
        self.assertEqual(self.db.get_subject_calendar("Reti di Calcolatori")[0], calendar_id)


class ConcurrencyTests(CalendarSyncTestCase):
    def test_sync_operations_are_serialized(self) -> None:
        self.google.add_event(CAL_SO, "Lezione", self.tomorrow)
        self.service.refresh_calendars()
        self.google.delay = 0.05
        self.google.max_active = 0
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.service.sync_now()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                self.db.close_current_thread()

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        self.db.save_local_event(None, "Concorrente", "Sistemi Operativi", self.tomorrow, self.tomorrow + timedelta(hours=1))
        self.service.request_flush()
        for thread in threads:
            thread.join(15)
        self.assertTrue(self.service.wait_idle(10))
        self.assertEqual(errors, [])
        self.assertEqual(self.google.max_active, 1)
        count = self.db._connection().execute(
            "SELECT COUNT(*) FROM calendar_events WHERE title = 'Lezione'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.db.count_pending_events(), 0)

    def test_sync_async_rejects_parallel_runs(self) -> None:
        self.service.refresh_calendars()
        self.google.delay = 0.1
        done = threading.Event()
        self.service.add_listener(lambda kind, data: done.set() if kind == "sync_finished" else None)
        self.assertTrue(self.service.sync_async())
        self.assertFalse(self.service.sync_async())
        self.assertTrue(done.wait(10))
        self.assertTrue(self.service.wait_idle(5))
        self.assertTrue(self.service.sync_async())
        self.assertTrue(self.service.wait_idle(10))


class SecurityAndFormattingTests(unittest.TestCase):
    def test_secrets_masked_in_logs(self) -> None:
        secret_line = (
            "token ya29.a0AfB_byC-SECRET refresh 1//0gSECRETREFRESHTOKEN client GOCSPX-abc123 "
            '{"access_token": "abc.def", "refresh_token":"xyz"} Bearer qwerty ?code=4/0Aabc&state=1'
        )
        masked = mask_secrets(secret_line)
        for leaked in ("SECRET", "abc123", "abc.def", "xyz", "qwerty", "4/0Aabc"):
            self.assertNotIn(leaked, masked)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "Token: %s", ("ya29.TOPSECRET",), None)
        SecretMaskingFilter().filter(record)
        self.assertNotIn("TOPSECRET", record.getMessage())

    def test_backup_excludes_oauth_secrets_and_keeps_them_on_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "transcripts").mkdir()
            paths = {"data": root, "transcripts": root / "transcripts"}
            db = Database(root / "b.db")
            try:
                db.set_settings(
                    {
                        "google_refresh_token_enc": protect_secret("1//refresh-secret"),
                        "google_access_token_enc": protect_secret("ya29.access-secret"),
                        "google_client_secret_enc": protect_secret("GOCSPX-secret"),
                        "google_client_id": "123.apps.googleusercontent.com",
                        "pomodoro_work": "30",
                    }
                )
                result = export_backup(db, paths, root / "out.zip")
                with zipfile.ZipFile(result["path"]) as archive:
                    raw = archive.read("data.json").decode("utf-8")
                for leaked in ("google_refresh_token_enc", "google_client_secret_enc", "refresh-secret", "GOCSPX"):
                    self.assertNotIn(leaked, raw)
                self.assertIn("pomodoro_work", raw)
                import_backup(db, paths, result["path"])
                self.assertEqual(
                    unprotect_secret(db.get_setting("google_refresh_token_enc")), "1//refresh-secret"
                )
                self.assertEqual(db.get_setting("pomodoro_work"), "30")
            finally:
                db.close_all()

    def test_credentials_and_pkce(self) -> None:
        client = parse_client_credentials(
            json.dumps({"installed": {"client_id": "1.apps.googleusercontent.com", "client_secret": "s"}})
        )
        self.assertEqual(client["client_secret"], "s")
        with self.assertRaises(GoogleAuthError):
            parse_client_credentials(json.dumps({"web": {"client_id": "x"}}))
        verifier, challenge = make_pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertNotIn("=", challenge)
        self.assertEqual(unprotect_secret(protect_secret("segreto")), "segreto")

    def test_elapsed_label_and_contrast(self) -> None:
        now = datetime(2026, 11, 12, 15, 0, 0)
        self.assertEqual(format_elapsed("", now), "Mai sincronizzato")
        self.assertEqual(format_elapsed("2026-11-12T14:59:40Z", now), "Aggiornato ora")
        self.assertEqual(format_elapsed("2026-11-12T14:55:00Z", now), "5 min fa")
        self.assertEqual(format_elapsed("2026-11-12T13:00:00Z", now), "2 ore fa")
        self.assertEqual(format_elapsed("2026-11-10T13:00:00Z", now), "2 giorni fa")
        self.assertEqual(text_color_for("#0B8043"), "#FFFFFF")
        self.assertEqual(text_color_for("#FBD75B"), "#0B1120")


if __name__ == "__main__":
    unittest.main()
