"""Persistenza SQLite thread-safe per StudyFlow."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULT_BLOCKED_APPS, DEFAULT_BLOCKED_SITES, DEFAULT_SETTINGS
from .services.spaced_repetition import DEFAULT_EASE_FACTOR, schedule_card
from .services.subjects import MAX_CFU, cfu_to_hours, parse_exam_date
from .utils import (
    LOCAL_DATE_FMT,
    LOCAL_DATETIME_FMT,
    now_str,
    parse_datetime,
    safe_float,
    safe_int,
)


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 7

# Origine di un evento del calendario. Gli stati ``google_pending_*`` formano
# la coda offline verso Google Calendar; ``google_pending_delete`` e' un
# tombstone: resta nel database finche' Google non conferma l'eliminazione e
# non compare mai nell'agenda.
EVENT_SOURCE_MANUAL = "manual"
EVENT_SOURCE_ICS = "ics_file"
EVENT_SOURCE_GOOGLE = "google_api"
# Eventi letti da un indirizzo iCal (es. "Indirizzo segreto in formato iCal" di
# Google Calendar): sola lettura, aggiornati a ogni sincronizzazione.
EVENT_SOURCE_ICAL = "ical_url"
EVENT_PENDING_CREATE = "google_pending_create"
EVENT_PENDING_UPDATE = "google_pending_update"
EVENT_PENDING_DELETE = "google_pending_delete"
EVENT_SOURCES: tuple[str, ...] = (
    EVENT_SOURCE_MANUAL,
    EVENT_SOURCE_ICS,
    EVENT_SOURCE_GOOGLE,
    EVENT_SOURCE_ICAL,
    EVENT_PENDING_CREATE,
    EVENT_PENDING_UPDATE,
    EVENT_PENDING_DELETE,
)
EVENT_PENDING_SOURCES: tuple[str, ...] = (
    EVENT_PENDING_CREATE,
    EVENT_PENDING_UPDATE,
    EVENT_PENDING_DELETE,
)
GOOGLE_LINKED_SOURCES: tuple[str, ...] = (
    EVENT_SOURCE_GOOGLE,
    EVENT_PENDING_UPDATE,
    EVENT_PENDING_DELETE,
)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def normalize_hex(value: Any) -> str | None:
    """``#RRGGBB`` maiuscolo oppure ``None`` se il valore non e' un colore."""
    match = _HEX_RE.match(str(value or "").strip())
    return f"#{match.group(1).upper()}" if match else None


def google_uid(google_calendar_id: str, google_event_id: str) -> str:
    """UID locale di un evento Google.

    Non usa ``iCalUID``: lo stesso evento potrebbe essere stato importato anche
    da un file .ics e il vincolo ``UNIQUE(uid, start)`` andrebbe in conflitto.
    """
    return f"gcal:{google_event_id}@{google_calendar_id}"[:400]


def _parse_local(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None)
    return parse_datetime(str(value or ""))

# Tabelle incluse nel backup JSON. L'ordine e' quello di inserimento: rispetta
# le foreign key fra calendario, materiali, note, sessioni, flashcard e dubbi.
DATA_TABLES: tuple[str, ...] = (
    "settings",
    "subjects",
    "calendar_events",
    "sessions",
    "materials",
    "material_notes",
    "distraction_attempts",
    "video_lessons",
    "flashcards",
    "flashcard_reviews",
    "doubts",
    "blocked_sites",
    "blocked_apps",
)

_RESTORE_DELETE_ORDER: tuple[str, ...] = (
    "doubts",
    "material_notes",
    "flashcard_reviews",
    "flashcards",
    "materials",
    "distraction_attempts",
    "sessions",
    "calendar_events",
    "video_lessons",
    "subjects",
    "blocked_sites",
    "blocked_apps",
    "settings",
)

_RESTORE_INSERT_ORDER: tuple[str, ...] = (
    "subjects",
    "calendar_events",
    "sessions",
    "materials",
    "material_notes",
    "distraction_attempts",
    "video_lessons",
    "flashcards",
    "flashcard_reviews",
    "doubts",
    "blocked_sites",
    "blocked_apps",
    "settings",
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL DEFAULT '',
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL,
    calendar_event_id INTEGER REFERENCES calendar_events(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS distraction_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    source_type TEXT NOT NULL CHECK (source_type IN ('website', 'app')),
    target_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    target_hours REAL NOT NULL DEFAULT 0,
    logged_hours REAL NOT NULL DEFAULT 0,
    calendar_event_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (calendar_event_id) REFERENCES calendar_events(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS material_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL DEFAULT 1,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (material_id) REFERENCES materials(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS video_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    video_path TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    transcript_name TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',
    progress REAL NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flashcards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL DEFAULT '',
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'new',
    due_at TEXT NOT NULL,
    interval_days REAL NOT NULL DEFAULT 0,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    repetitions INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    learning_step INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flashcard_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
    reviewed_at TEXT NOT NULL,
    previous_state TEXT NOT NULL DEFAULT '',
    new_state TEXT NOT NULL DEFAULT '',
    previous_interval REAL NOT NULL DEFAULT 0,
    new_interval REAL NOT NULL DEFAULT 0,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    elapsed_seconds INTEGER NOT NULL DEFAULT 0,
    scheduled_days REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (card_id) REFERENCES flashcards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS doubts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL DEFAULT '',
    material_id INTEGER,
    page_number INTEGER,
    question TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    flashcard_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (material_id) REFERENCES materials(id) ON DELETE SET NULL,
    FOREIGN KEY (flashcard_id) REFERENCES flashcards(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    teacher_name TEXT NOT NULL DEFAULT '',
    teacher_email TEXT NOT NULL DEFAULT '',
    cfu INTEGER NOT NULL DEFAULT 0,
    exam_date TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    google_calendar_id TEXT,
    color_hex TEXT
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    all_day INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ics_file',
    google_event_id TEXT,
    google_calendar_id TEXT,
    previous_google_calendar_id TEXT,
    color_hex TEXT,
    color_id TEXT,
    sync_error TEXT NOT NULL DEFAULT '',
    local_revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE(uid, start)
);

CREATE TABLE IF NOT EXISTS blocked_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS blocked_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    process_name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sessions(timestamp);
CREATE INDEX IF NOT EXISTS idx_attempts_timestamp ON distraction_attempts(timestamp);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON distraction_attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_events_start ON calendar_events(start);
CREATE INDEX IF NOT EXISTS idx_events_end ON calendar_events(end);
CREATE INDEX IF NOT EXISTS idx_materials_subject ON materials(subject);
CREATE INDEX IF NOT EXISTS idx_material_notes_material ON material_notes(material_id, page_number);
CREATE INDEX IF NOT EXISTS idx_doubts_subject ON doubts(subject);
CREATE INDEX IF NOT EXISTS idx_doubts_material ON doubts(material_id);
CREATE INDEX IF NOT EXISTS idx_doubts_status ON doubts(status);
CREATE INDEX IF NOT EXISTS idx_video_lessons_subject ON video_lessons(subject);
CREATE INDEX IF NOT EXISTS idx_video_lessons_updated ON video_lessons(updated_at);
CREATE INDEX IF NOT EXISTS idx_flashcards_subject ON flashcards(subject);
CREATE INDEX IF NOT EXISTS idx_flashcards_due ON flashcards(due_at);
CREATE INDEX IF NOT EXISTS idx_flashcard_reviews_card ON flashcard_reviews(card_id);
CREATE INDEX IF NOT EXISTS idx_flashcard_reviews_date ON flashcard_reviews(reviewed_at);
CREATE INDEX IF NOT EXISTS idx_subjects_exam ON subjects(exam_date);
"""


class Database:
    """Wrapper SQLite con una connessione per thread e WAL.

    L'interfaccia e' volutamente sincrona: tutte le operazioni sono locali e
    brevi. Le chiamate da thread di background sono supportate tramite
    connessioni thread-locali separate.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections: set[sqlite3.Connection] = set()
        self._initialize()

    # ------------------------------------------------------------------ core
    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.path), timeout=12, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 12000")
            connection.execute("PRAGMA synchronous = NORMAL")
            self._local.connection = connection
            with self._connections_lock:
                self._connections.add(connection)
        return connection

    def _initialize(self) -> None:
        with self._init_lock:
            connection = sqlite3.connect(str(self.path), timeout=12, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 12000")
                connection.executescript(SCHEMA_SQL)
                self._migrate(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            finally:
                connection.close()
            self._seed_defaults()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(materials)").fetchall()
        }
        if columns and "calendar_event_id" not in columns:
            connection.execute(
                "ALTER TABLE materials ADD COLUMN calendar_event_id INTEGER "
                "REFERENCES calendar_events(id) ON DELETE SET NULL"
            )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(calendar_events)").fetchall()
        }
        if columns:
            for column, definition in (
                ("subject", "TEXT NOT NULL DEFAULT ''"),
                ("location", "TEXT NOT NULL DEFAULT ''"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("all_day", "INTEGER NOT NULL DEFAULT 0"),
                ("source_file", "TEXT NOT NULL DEFAULT ''"),
                ("imported_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE calendar_events ADD COLUMN {column} {definition}"
                    )
        self._migrate_v7_google(connection)

    @staticmethod
    def _migrate_v7_google(connection: sqlite3.Connection) -> None:
        """Schema v7: sincronizzazione Google Calendar multi-calendario.

        Idempotente: aggiunge solo le colonne mancanti. Gli eventi gia' presenti
        provengono dall'importazione .ics, quindi ricevono ``source='ics_file'``.
        """

        def columns_of(table: str) -> set[str]:
            return {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }

        subject_columns = columns_of("subjects")
        for column in ("google_calendar_id", "color_hex"):
            if subject_columns and column not in subject_columns:
                connection.execute(f"ALTER TABLE subjects ADD COLUMN {column} TEXT")

        event_columns = columns_of("calendar_events")
        if event_columns:
            for column, definition in (
                ("source", "TEXT NOT NULL DEFAULT 'ics_file'"),
                ("google_event_id", "TEXT"),
                ("google_calendar_id", "TEXT"),
                ("previous_google_calendar_id", "TEXT"),
                ("color_hex", "TEXT"),
                ("color_id", "TEXT"),
                ("sync_error", "TEXT NOT NULL DEFAULT ''"),
                ("local_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in event_columns:
                    connection.execute(
                        f"ALTER TABLE calendar_events ADD COLUMN {column} {definition}"
                    )

        session_columns = columns_of("sessions")
        if session_columns and "calendar_event_id" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN calendar_event_id INTEGER "
                "REFERENCES calendar_events(id) ON DELETE SET NULL"
            )

        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_google_calendar
                ON subjects(google_calendar_id)
                WHERE google_calendar_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_google
                ON calendar_events(google_calendar_id, google_event_id)
                WHERE google_calendar_id IS NOT NULL AND google_event_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_source ON calendar_events(source);
            CREATE INDEX IF NOT EXISTS idx_sessions_event ON sessions(calendar_event_id);
            CREATE INDEX IF NOT EXISTS idx_materials_event ON materials(calendar_event_id);
            """
        )

    def _seed_defaults(self) -> None:
        with self._connection() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                list(DEFAULT_SETTINGS.items()),
            )
            seeded = connection.execute(
                "SELECT value FROM settings WHERE key = 'defaults_seeded'"
            ).fetchone()
            if seeded is None:
                connection.executemany(
                    "INSERT OR IGNORE INTO blocked_sites(domain, enabled) VALUES (?, 1)",
                    [(domain,) for domain in DEFAULT_BLOCKED_SITES],
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO blocked_apps(display_name, process_name, enabled) "
                    "VALUES (?, ?, 1)",
                    [(display, process) for display, process in DEFAULT_BLOCKED_APPS],
                )
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES ('defaults_seeded', '1')"
                )
        LOGGER.info("Database pronto: %s", self.path)

    def close_current_thread(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
            with self._connections_lock:
                self._connections.discard(connection)
            self._local.connection = None

    def close_all(self) -> None:
        """Chiude tutte le connessioni registrate (usato in chiusura e nei test)."""
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local.connection = None

    # -------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, str]:
        rows = self._connection().execute("SELECT key, value FROM settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        result.update({row["key"]: row["value"] for row in rows})
        return result

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._connection().execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str | int | float | bool) -> None:
        self._connection().execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def set_settings(self, values: dict[str, Any]) -> None:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(key, str(value)) for key, value in values.items()],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    # -------------------------------------------------------------- sessions
    def create_session(
        self,
        subject: str,
        timestamp: str | None = None,
        calendar_event_id: int | None = None,
    ) -> int:
        # La sottoquery evita violazioni di foreign key se l'evento e' stato
        # rimosso (per esempio da una sincronizzazione) mentre il timer partiva.
        cursor = self._connection().execute(
            "INSERT INTO sessions(subject, duration_minutes, completed, timestamp, "
            "calendar_event_id) VALUES (?, 0, 0, ?, "
            "(SELECT id FROM calendar_events WHERE id = ? AND source <> ?))",
            (
                subject.strip() or "Studio",
                timestamp or now_str(),
                int(calendar_event_id) if calendar_event_id else None,
                EVENT_PENDING_DELETE,
            ),
        )
        return int(cursor.lastrowid)

    def finalize_session(self, session_id: int, duration_minutes: int, completed: bool) -> None:
        self._connection().execute(
            "UPDATE sessions SET duration_minutes = ?, completed = ? WHERE id = ?",
            (max(0, int(duration_minutes)), 1 if completed else 0, int(session_id)),
        )

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_recent_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM sessions ORDER BY timestamp DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- distractions
    def log_distraction(
        self,
        session_id: int | None,
        source_type: str,
        target_name: str,
        timestamp: str | None = None,
    ) -> int | None:
        if source_type not in {"website", "app"}:
            raise ValueError("source_type deve essere 'website' oppure 'app'.")
        target = target_name.strip()[:300]
        if not target:
            return None
        cursor = self._connection().execute(
            "INSERT INTO distraction_attempts(session_id, source_type, target_name, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (session_id, source_type, target, timestamp or now_str()),
        )
        return int(cursor.lastrowid)

    # --------------------------------------------------------------- materials
    def add_material(
        self,
        title: str,
        file_path: str,
        subject: str,
        target_hours: float,
        calendar_event_id: int | None = None,
    ) -> int:
        cursor = self._connection().execute(
            "INSERT INTO materials(title, file_path, subject, target_hours, logged_hours, "
            "calendar_event_id, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (
                title.strip()[:250] or "Materiale",
                file_path.strip(),
                subject.strip()[:120],
                max(0.0, safe_float(target_hours)),
                calendar_event_id,
                now_str(),
            ),
        )
        return int(cursor.lastrowid)

    def update_material(
        self,
        material_id: int,
        title: str,
        file_path: str,
        subject: str,
        target_hours: float,
        calendar_event_id: int | None,
    ) -> None:
        self._connection().execute(
            "UPDATE materials SET title = ?, file_path = ?, subject = ?, target_hours = ?, "
            "calendar_event_id = ? WHERE id = ?",
            (
                title.strip()[:250] or "Materiale",
                file_path.strip(),
                subject.strip()[:120],
                max(0.0, safe_float(target_hours)),
                calendar_event_id,
                int(material_id),
            ),
        )

    def find_material_by_path(self, file_path: str) -> dict[str, Any] | None:
        """Materiale gia' registrato per lo stesso file (confronto case-insensitive)."""
        wanted = os.path.normcase(os.path.abspath(str(file_path or "")))
        if not str(file_path or "").strip():
            return None
        rows = self._connection().execute(
            "SELECT * FROM materials WHERE TRIM(file_path) <> ''"
        ).fetchall()
        for row in rows:
            current = os.path.normcase(os.path.abspath(str(row["file_path"])))
            if current == wanted:
                return dict(row)
        return None

    def relocate_paths(self, old_dir: str | Path, new_dir: str | Path) -> int:
        """Aggiorna i percorsi salvati dopo lo spostamento di una cartella.

        Riguarda ``materials.file_path`` e ``video_lessons.transcript_path``:
        ogni percorso dentro ``old_dir`` viene riscritto sotto ``new_dir``.
        """
        old_root = os.path.normcase(os.path.abspath(str(old_dir))).rstrip("\\/")
        new_root = Path(new_dir)
        connection = self._connection()
        changed = 0
        for table, column in (("materials", "file_path"), ("video_lessons", "transcript_path")):
            rows = connection.execute(
                f"SELECT id, {column} AS value FROM {table} WHERE TRIM({column}) <> ''"
            ).fetchall()
            for row in rows:
                raw = os.path.abspath(str(row["value"]))
                normalized = os.path.normcase(raw)
                if normalized != old_root and not normalized.startswith(old_root + os.sep):
                    continue
                relative = raw[len(old_root):].lstrip("\\/")
                target = str(new_root / relative) if relative else str(new_root)
                connection.execute(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    (target, int(row["id"])),
                )
                changed += 1
        return changed

    def delete_material(self, material_id: int) -> None:
        self._connection().execute("DELETE FROM materials WHERE id = ?", (int(material_id),))

    def get_material(self, material_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT m.*, e.title AS event_title, e.start AS event_start "
            "FROM materials m LEFT JOIN calendar_events e ON e.id = m.calendar_event_id "
            "WHERE m.id = ?",
            (int(material_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_materials(self) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT m.*, e.title AS event_title, e.start AS event_start "
            "FROM materials m LEFT JOIN calendar_events e ON e.id = m.calendar_event_id "
            "ORDER BY LOWER(m.subject), LOWER(m.title)"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_materials_for_subject(self, subject: str) -> list[dict[str, Any]]:
        """Materiali collegati a una materia, ignorando maiuscole e spazi."""
        rows = self._connection().execute(
            "SELECT m.*, e.title AS event_title, e.start AS event_start "
            "FROM materials m LEFT JOIN calendar_events e ON e.id = m.calendar_event_id "
            "WHERE LOWER(TRIM(m.subject)) = LOWER(TRIM(?)) "
            "ORDER BY LOWER(m.title)",
            (str(subject or ""),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_materials_without_subject(self) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT m.*, e.title AS event_title, e.start AS event_start "
            "FROM materials m LEFT JOIN calendar_events e ON e.id = m.calendar_event_id "
            "WHERE TRIM(m.subject) = '' "
            "ORDER BY LOWER(m.title)"
        ).fetchall()
        return [dict(row) for row in rows]

    def count_materials_without_subject(self) -> int:
        row = self._connection().execute(
            "SELECT COUNT(*) AS total FROM materials WHERE TRIM(subject) = ''"
        ).fetchone()
        return int(row["total"] if row is not None else 0)

    def add_material_hours(self, material_id: int, hours: float) -> None:
        self._connection().execute(
            "UPDATE materials SET logged_hours = MAX(0, logged_hours + ?) WHERE id = ?",
            (max(0.0, safe_float(hours)), int(material_id)),
        )

    def set_material_hours(self, material_id: int, hours: float) -> None:
        self._connection().execute(
            "UPDATE materials SET logged_hours = ? WHERE id = ?",
            (max(0.0, safe_float(hours)), int(material_id)),
        )

    # -------------------------------------------------------- material notes
    def add_material_note(self, material_id: int, page_number: int, text: str) -> int:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("Il testo della nota e' obbligatorio.")
        page = max(1, safe_int(page_number, 1))
        timestamp = now_str()
        cursor = self._connection().execute(
            "INSERT INTO material_notes(material_id, page_number, text, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            (int(material_id), page, clean[:8000], timestamp, timestamp),
        )
        return int(cursor.lastrowid)

    def update_material_note(
        self,
        note_id: int,
        text: str,
        page_number: int | None = None,
    ) -> None:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("Il testo della nota e' obbligatorio.")
        if page_number is None:
            self._connection().execute(
                "UPDATE material_notes SET text = ?, updated_at = ? WHERE id = ?",
                (clean[:8000], now_str(), int(note_id)),
            )
            return
        self._connection().execute(
            "UPDATE material_notes SET text = ?, page_number = ?, updated_at = ? "
            "WHERE id = ?",
            (clean[:8000], max(1, safe_int(page_number, 1)), now_str(), int(note_id)),
        )

    def delete_material_note(self, note_id: int) -> None:
        self._connection().execute(
            "DELETE FROM material_notes WHERE id = ?", (int(note_id),)
        )

    def get_material_note(self, note_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM material_notes WHERE id = ?", (int(note_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_material_notes(self, material_id: int | None = None) -> list[dict[str, Any]]:
        if material_id is None:
            rows = self._connection().execute(
                "SELECT n.*, m.title AS material_title FROM material_notes n "
                "LEFT JOIN materials m ON m.id = n.material_id "
                "ORDER BY n.material_id, n.page_number, n.created_at"
            ).fetchall()
        else:
            rows = self._connection().execute(
                "SELECT n.*, m.title AS material_title FROM material_notes n "
                "LEFT JOIN materials m ON m.id = n.material_id "
                "WHERE n.material_id = ? "
                "ORDER BY n.page_number, n.created_at",
                (int(material_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_material_notes(self, material_id: int) -> int:
        row = self._connection().execute(
            "SELECT COUNT(*) AS total FROM material_notes WHERE material_id = ?",
            (int(material_id),),
        ).fetchone()
        return safe_int(row["total"] if row is not None else 0)

    # ----------------------------------------------------------------- doubts
    def add_doubt(
        self,
        subject: str,
        question: str,
        source: str = "",
        material_id: int | None = None,
        page_number: int | None = None,
    ) -> int:
        clean = str(question or "").strip()
        if not clean:
            raise ValueError("La domanda o il dubbio e' obbligatorio.")
        page = None if page_number in (None, "") else max(1, safe_int(page_number, 1))
        timestamp = now_str()
        cursor = self._connection().execute(
            "INSERT INTO doubts(subject, material_id, page_number, question, source, "
            "status, flashcard_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', NULL, ?, ?)",
            (
                str(subject or "").strip()[:150],
                int(material_id) if material_id not in (None, "") else None,
                page,
                clean[:8000],
                str(source or "").strip()[:500],
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def update_doubt(
        self,
        doubt_id: int,
        question: str,
        source: str = "",
        subject: str = "",
        material_id: int | None = None,
        page_number: int | None = None,
    ) -> None:
        clean = str(question or "").strip()
        if not clean:
            raise ValueError("La domanda o il dubbio e' obbligatorio.")
        page = None if page_number in (None, "") else max(1, safe_int(page_number, 1))
        self._connection().execute(
            "UPDATE doubts SET subject = ?, material_id = ?, page_number = ?, "
            "question = ?, source = ?, updated_at = ? WHERE id = ?",
            (
                str(subject or "").strip()[:150],
                int(material_id) if material_id not in (None, "") else None,
                page,
                clean[:8000],
                str(source or "").strip()[:500],
                now_str(),
                int(doubt_id),
            ),
        )

    def set_doubt_status(self, doubt_id: int, status: str) -> None:
        clean = str(status or "").strip().lower()
        if clean not in {"open", "resolved"}:
            raise ValueError("Stato del dubbio non valido.")
        self._connection().execute(
            "UPDATE doubts SET status = ?, updated_at = ? WHERE id = ?",
            (clean, now_str(), int(doubt_id)),
        )

    def resolve_doubt_with_flashcard(self, doubt_id: int, flashcard_id: int) -> None:
        self._connection().execute(
            "UPDATE doubts SET status = 'resolved', flashcard_id = ?, updated_at = ? "
            "WHERE id = ?",
            (int(flashcard_id), now_str(), int(doubt_id)),
        )

    def delete_doubt(self, doubt_id: int) -> None:
        self._connection().execute(
            "DELETE FROM doubts WHERE id = ?", (int(doubt_id),)
        )

    def get_doubt(self, doubt_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT d.*, m.title AS material_title, m.file_path AS material_path, "
            "f.front AS flashcard_front FROM doubts d "
            "LEFT JOIN materials m ON m.id = d.material_id "
            "LEFT JOIN flashcards f ON f.id = d.flashcard_id "
            "WHERE d.id = ?",
            (int(doubt_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_doubts(
        self,
        subject: str | None = None,
        status: str | None = None,
        material_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None and subject.strip():
            clauses.append("LOWER(d.subject) = LOWER(?)")
            params.append(subject.strip())
        if status in {"open", "resolved"}:
            clauses.append("d.status = ?")
            params.append(status)
        if material_id is not None:
            clauses.append("d.material_id = ?")
            params.append(int(material_id))
        sql = (
            "SELECT d.*, m.title AS material_title, m.file_path AS material_path, "
            "f.front AS flashcard_front FROM doubts d "
            "LEFT JOIN materials m ON m.id = d.material_id "
            "LEFT JOIN flashcards f ON f.id = d.flashcard_id"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY CASE d.status WHEN 'open' THEN 0 ELSE 1 END, "
            "d.created_at DESC, d.id DESC"
        )
        rows = self._connection().execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def doubt_stats(self, subject: str | None = None) -> dict[str, int]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None and subject.strip():
            clauses.append("LOWER(subject) = LOWER(?)")
            params.append(subject.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self._connection().execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open, "
            "COALESCE(SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END), 0) AS resolved "
            f"FROM doubts{where}",
            tuple(params),
        ).fetchone()
        return {
            "total": safe_int(row["total"] if row else 0),
            "open": safe_int(row["open"] if row else 0),
            "resolved": safe_int(row["resolved"] if row else 0),
        }

    # ------------------------------------------------------------ video lessons
    def add_video_lesson(
        self,
        title: str,
        video_path: str,
        subject: str = "",
        transcript_name: str = "",
        model: str = "",
        language: str = "",
    ) -> int:
        timestamp = now_str()
        cursor = self._connection().execute(
            "INSERT INTO video_lessons(title, video_path, subject, transcript_path, "
            "transcript_name, duration_seconds, status, progress, model, language, error, "
            "created_at, updated_at) VALUES (?, ?, ?, '', ?, 0, 'ready', 0, ?, ?, '', ?, ?)",
            (
                title.strip()[:250] or "Video",
                video_path.strip(),
                subject.strip()[:150],
                transcript_name.strip()[:250],
                model.strip()[:80],
                language.strip()[:20],
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def update_video_lesson_metadata(self, video_id: int, title: str, subject: str) -> None:
        self._connection().execute(
            "UPDATE video_lessons SET title = ?, subject = ?, updated_at = ? WHERE id = ?",
            (
                title.strip()[:250] or "Video",
                subject.strip()[:150],
                now_str(),
                int(video_id),
            ),
        )

    def set_video_lesson_job(
        self,
        video_id: int,
        status: str,
        progress: float = 0.0,
        error: str = "",
    ) -> None:
        self._connection().execute(
            "UPDATE video_lessons SET status = ?, progress = ?, error = ?, updated_at = ? "
            "WHERE id = ?",
            (
                str(status)[:30],
                max(0.0, min(1.0, safe_float(progress))),
                str(error)[:1000],
                now_str(),
                int(video_id),
            ),
        )

    def complete_video_lesson(
        self,
        video_id: int,
        transcript_path: str,
        transcript_name: str,
        duration_seconds: float,
        model: str = "",
        language: str = "",
    ) -> None:
        self._connection().execute(
            "UPDATE video_lessons SET transcript_path = ?, transcript_name = ?, "
            "duration_seconds = ?, status = 'completed', progress = 1, model = ?, "
            "language = ?, error = '', updated_at = ? WHERE id = ?",
            (
                transcript_path.strip(),
                transcript_name.strip()[:250],
                max(0.0, safe_float(duration_seconds)),
                model.strip()[:80],
                language.strip()[:20],
                now_str(),
                int(video_id),
            ),
        )

    def update_video_transcript_path(
        self,
        video_id: int,
        transcript_path: str,
        transcript_name: str,
    ) -> None:
        self._connection().execute(
            "UPDATE video_lessons SET transcript_path = ?, transcript_name = ?, "
            "status = 'completed', error = '', updated_at = ? WHERE id = ?",
            (
                transcript_path.strip(),
                transcript_name.strip()[:250],
                now_str(),
                int(video_id),
            ),
        )

    def get_video_lesson(self, video_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM video_lessons WHERE id = ?", (int(video_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_video_lessons(self, subject: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM video_lessons"
        params: tuple[Any, ...] = ()
        if subject is not None and subject.strip():
            sql += " WHERE LOWER(subject) = LOWER(?)"
            params = (subject.strip(),)
        sql += " ORDER BY LOWER(subject), updated_at DESC, id DESC"
        rows = self._connection().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_video_lesson(self, video_id: int) -> None:
        self._connection().execute(
            "DELETE FROM video_lessons WHERE id = ?", (int(video_id),)
        )

    def recover_interrupted_video_jobs(self) -> int:
        cursor = self._connection().execute(
            "UPDATE video_lessons SET status = 'error', error = ?, updated_at = ? "
            "WHERE status IN ('queued', 'processing')",
            (
                "Elaborazione interrotta: l'applicazione e' stata chiusa durante la trascrizione.",
                now_str(),
            ),
        )
        return int(cursor.rowcount)

    # -------------------------------------------------------------- flashcards
    def add_flashcard(
        self,
        subject: str,
        front: str,
        back: str,
        notes: str = "",
    ) -> int:
        question = front.strip()
        answer = back.strip()
        if not question or not answer:
            raise ValueError("Fronte e retro della flashcard sono obbligatori.")
        timestamp = now_str()
        cursor = self._connection().execute(
            "INSERT INTO flashcards(subject, front, back, notes, state, due_at, "
            "interval_days, ease_factor, repetitions, lapses, learning_step, "
            "last_reviewed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'new', ?, 0, ?, 0, 0, 0, '', ?, ?)",
            (
                subject.strip()[:150],
                question[:8000],
                answer[:8000],
                notes.strip()[:8000],
                timestamp,
                DEFAULT_EASE_FACTOR,
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def update_flashcard(
        self,
        card_id: int,
        subject: str,
        front: str,
        back: str,
        notes: str = "",
    ) -> None:
        question = front.strip()
        answer = back.strip()
        if not question or not answer:
            raise ValueError("Fronte e retro della flashcard sono obbligatori.")
        self._connection().execute(
            "UPDATE flashcards SET subject = ?, front = ?, back = ?, notes = ?, "
            "updated_at = ? WHERE id = ?",
            (
                subject.strip()[:150],
                question[:8000],
                answer[:8000],
                notes.strip()[:8000],
                now_str(),
                int(card_id),
            ),
        )

    def delete_flashcard(self, card_id: int) -> None:
        self._connection().execute(
            "DELETE FROM flashcards WHERE id = ?", (int(card_id),)
        )

    def get_flashcard(self, card_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM flashcards WHERE id = ?", (int(card_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_flashcards(
        self,
        subject: str | None = None,
        search: str = "",
        state: str | None = None,
        due_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None and subject.strip():
            clauses.append("LOWER(subject) = LOWER(?)")
            params.append(subject.strip())
        if search.strip():
            token = f"%{search.strip().lower()}%"
            clauses.append(
                "(LOWER(front) LIKE ? OR LOWER(back) LIKE ? OR LOWER(notes) LIKE ? "
                "OR LOWER(subject) LIKE ?)"
            )
            params.extend([token, token, token, token])
        if state:
            clauses.append("state = ?")
            params.append(str(state))
        if due_only:
            clauses.append("due_at <= ?")
            params.append(now_str())
        sql = "SELECT * FROM flashcards"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY due_at ASC, LOWER(subject), id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self._connection().execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def due_flashcards(
        self,
        subject: str | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        return self.list_flashcards(subject=subject, due_only=True, limit=limit)

    def review_flashcard(
        self,
        card_id: int,
        rating: int,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        card = self.get_flashcard(card_id)
        if card is None:
            raise ValueError("Flashcard non trovata.")
        schedule = schedule_card(card, rating, now)
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE flashcards SET state = ?, due_at = ?, interval_days = ?, "
                "ease_factor = ?, repetitions = ?, lapses = ?, learning_step = ?, "
                "last_reviewed_at = ?, updated_at = ? WHERE id = ?",
                (
                    schedule["state"],
                    schedule["due_at"],
                    schedule["interval_days"],
                    schedule["ease_factor"],
                    schedule["repetitions"],
                    schedule["lapses"],
                    schedule["learning_step"],
                    schedule["last_reviewed_at"],
                    schedule["last_reviewed_at"],
                    int(card_id),
                ),
            )
            connection.execute(
                "INSERT INTO flashcard_reviews(card_id, rating, reviewed_at, "
                "previous_state, new_state, previous_interval, new_interval, "
                "ease_factor, elapsed_seconds, scheduled_days) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(card_id),
                    int(rating),
                    schedule["last_reviewed_at"],
                    str(card.get("state") or "new"),
                    schedule["state"],
                    max(0.0, safe_float(card.get("interval_days", 0))),
                    max(0.0, safe_float(schedule["interval_days"])),
                    schedule["ease_factor"],
                    int(schedule["elapsed_seconds"]),
                    schedule["scheduled_days"],
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        updated = self.get_flashcard(card_id)
        if updated is None:
            raise RuntimeError("Flashcard aggiornata ma non leggibile.")
        return updated, schedule

    def reset_flashcard(self, card_id: int) -> None:
        timestamp = now_str()
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE flashcards SET state = 'new', due_at = ?, interval_days = 0, "
                "ease_factor = ?, repetitions = 0, lapses = 0, learning_step = 0, "
                "last_reviewed_at = '', updated_at = ? WHERE id = ?",
                (timestamp, DEFAULT_EASE_FACTOR, timestamp, int(card_id)),
            )
            connection.execute(
                "DELETE FROM flashcard_reviews WHERE card_id = ?", (int(card_id),)
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def list_flashcard_reviews(
        self, card_id: int, limit: int = 30
    ) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM flashcard_reviews WHERE card_id = ? "
            "ORDER BY reviewed_at DESC, id DESC LIMIT ?",
            (int(card_id), max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def flashcard_stats(self, subject: str | None = None) -> dict[str, int]:
        now = now_str()
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None and subject.strip():
            clauses.append("LOWER(subject) = LOWER(?)")
            params.append(subject.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self._connection().execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN due_at <= ? THEN 1 ELSE 0 END), 0) AS due, "
            "COALESCE(SUM(CASE WHEN state = 'new' THEN 1 ELSE 0 END), 0) AS new, "
            "COALESCE(SUM(CASE WHEN state IN ('learning', 'relearning') "
            "THEN 1 ELSE 0 END), 0) AS learning, "
            "COALESCE(SUM(CASE WHEN state = 'review' THEN 1 ELSE 0 END), 0) AS review, "
            "COUNT(DISTINCT CASE WHEN TRIM(subject) <> '' THEN subject END) AS subjects "
            f"FROM flashcards{where}",
            tuple([now, *params]),
        ).fetchone()
        today_start = datetime.now().strftime(LOCAL_DATE_FMT) + " 00:00:00"
        review_clauses = ["r.reviewed_at >= ?"]
        review_params: list[Any] = [today_start]
        if subject is not None and subject.strip():
            review_clauses.append("LOWER(f.subject) = LOWER(?)")
            review_params.append(subject.strip())
        reviewed = self._connection().execute(
            "SELECT COUNT(*) AS count FROM flashcard_reviews r "
            "JOIN flashcards f ON f.id = r.card_id WHERE "
            + " AND ".join(review_clauses),
            tuple(review_params),
        ).fetchone()
        return {
            "total": safe_int(row["total"] if row else 0),
            "due": safe_int(row["due"] if row else 0),
            "new": safe_int(row["new"] if row else 0),
            "learning": safe_int(row["learning"] if row else 0),
            "review": safe_int(row["review"] if row else 0),
            "subjects": safe_int(row["subjects"] if row else 0),
            "reviewed_today": safe_int(reviewed["count"] if reviewed else 0),
        }

    # ----------------------------------------------------------- backup tables
    def snapshot_tables(self) -> dict[str, list[dict[str, Any]]]:
        """Restituisce una copia serializzabile di tutte le tabelle applicative."""
        connection = self._connection()
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in DATA_TABLES
        }

    def restore_tables(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        """Sostituisce tutte le tabelle con i dati di un backup gia' validato."""
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in _RESTORE_DELETE_ORDER:
                connection.execute(f"DELETE FROM {table}")
            try:
                connection.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('sessions', 'distraction_attempts', 'materials', 'material_notes', "
                    "'video_lessons', 'calendar_events', 'blocked_sites', 'blocked_apps', "
                    "'flashcards', 'flashcard_reviews', 'doubts')"
                )
            except sqlite3.Error:
                pass
            for table in _RESTORE_INSERT_ORDER:
                rows = tables.get(table) or []
                if not isinstance(rows, list):
                    raise ValueError(f"Formato non valido per la tabella {table}.")
                columns = [
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                if not columns:
                    continue
                for item in rows:
                    if not isinstance(item, dict):
                        raise ValueError(f"Riga non valida nella tabella {table}.")
                    usable = [column for column in columns if column in item]
                    if not usable:
                        continue
                    placeholders = ", ".join("?" for _ in usable)
                    connection.execute(
                        f"INSERT INTO {table}({', '.join(usable)}) "
                        f"VALUES ({placeholders})",
                        tuple(item[column] for column in usable),
                    )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError("Il backup contiene riferimenti non validi.")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- subjects
    def list_subjects(self, search: str = "") -> list[dict[str, Any]]:
        """Elenca le materie esplicite e quelle ricavate dai dati collegati.

        Una materia puo' esistere solo come tag (per esempio in una flashcard)
        oppure avere una riga in ``subjects`` con docente, CFU, data d'esame e
        note. Le due fonti vengono unite senza duplicati, ignorando maiuscole e
        minuscole.
        """
        now = now_str()
        sql = (
            "WITH names(name) AS ("
            "SELECT name FROM subjects WHERE TRIM(name) <> '' "
            "UNION SELECT subject FROM calendar_events WHERE TRIM(subject) <> '' "
            f"AND source <> '{EVENT_PENDING_DELETE}' "
            "UNION SELECT subject FROM materials WHERE TRIM(subject) <> '' "
            "UNION SELECT subject FROM sessions WHERE TRIM(subject) <> '' "
            "UNION SELECT subject FROM video_lessons WHERE TRIM(subject) <> '' "
            "UNION SELECT subject FROM flashcards WHERE TRIM(subject) <> '' "
            "UNION SELECT subject FROM doubts WHERE TRIM(subject) <> ''"
            ") "
            "SELECT n.name, COALESCE(s.id, 0) AS subject_id, "
            "COALESCE(s.teacher_name, '') AS teacher_name, "
            "COALESCE(s.teacher_email, '') AS teacher_email, "
            "COALESCE(s.cfu, 0) AS cfu, "
            "COALESCE(s.exam_date, '') AS exam_date, "
            "COALESCE(s.notes, '') AS notes, "
            "COALESCE(s.created_at, '') AS created_at, "
            "COALESCE(s.updated_at, '') AS updated_at, "
            "COALESCE(s.google_calendar_id, '') AS google_calendar_id, "
            "COALESCE(s.color_hex, '') AS color_hex, "
            "(SELECT COUNT(*) FROM materials m "
            "WHERE LOWER(m.subject) = LOWER(n.name)) AS materials, "
            "(SELECT COUNT(*) FROM video_lessons v "
            "WHERE LOWER(v.subject) = LOWER(n.name)) AS videos, "
            "(SELECT COUNT(*) FROM flashcards f "
            "WHERE LOWER(f.subject) = LOWER(n.name)) AS flashcards, "
            "(SELECT COUNT(*) FROM flashcards f "
            "WHERE LOWER(f.subject) = LOWER(n.name) AND f.due_at <= ?) AS due_flashcards, "
            "(SELECT COUNT(*) FROM doubts d "
            "WHERE LOWER(d.subject) = LOWER(n.name) AND d.status = 'open') AS open_doubts, "
            "(SELECT COUNT(*) FROM doubts d "
            "WHERE LOWER(d.subject) = LOWER(n.name)) AS doubts, "
            "(SELECT COUNT(*) FROM calendar_events e "
            "WHERE LOWER(e.subject) = LOWER(n.name) "
            f"AND e.source <> '{EVENT_PENDING_DELETE}') AS events, "
            "(SELECT COUNT(*) FROM sessions se "
            "WHERE LOWER(se.subject) = LOWER(n.name)) AS sessions, "
            "(SELECT COALESCE(SUM(se.duration_minutes), 0) FROM sessions se "
            "WHERE LOWER(se.subject) = LOWER(n.name)) AS study_minutes "
            "FROM names n LEFT JOIN subjects s ON LOWER(s.name) = LOWER(n.name) "
            "ORDER BY n.name COLLATE NOCASE"
        )
        rows = self._connection().execute(sql, (now,)).fetchall()
        count_keys = (
            "materials",
            "videos",
            "flashcards",
            "due_flashcards",
            "events",
            "sessions",
            "study_minutes",
        )
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["cfu"] = safe_int(item.get("cfu", 0))
            item["hours"] = cfu_to_hours(item["cfu"])
            item["study_minutes"] = safe_int(item.get("study_minutes", 0))
            key = str(item.get("name", "")).casefold()
            current = merged.get(key)
            if current is None:
                merged[key] = item
                continue
            for count_key in count_keys:
                current[count_key] = safe_int(current.get(count_key, 0)) + safe_int(
                    item.get(count_key, 0)
                )
            # Se una delle due varianti ha una riga esplicita, i suoi metadati
            # (nome, docente, CFU, esame, note) hanno la precedenza.
            if safe_int(item.get("subject_id", 0)) and not safe_int(
                current.get("subject_id", 0)
            ):
                counts = {name: current[name] for name in count_keys}
                current.update(item)
                current.update(counts)
        token = search.strip().casefold()
        subjects: list[dict[str, Any]] = []
        for item in sorted(
            merged.values(), key=lambda value: str(value.get("name", "")).casefold()
        ):
            if token and not any(
                token in str(item.get(key, "")).casefold()
                for key in ("name", "teacher_name", "teacher_email", "notes")
            ):
                continue
            subjects.append(item)
        return subjects

    def get_subject(self, name: str) -> dict[str, Any] | None:
        target = str(name or "").strip().casefold()
        if not target:
            return None
        for subject in self.list_subjects():
            if str(subject.get("name", "")).casefold() == target:
                return subject
        return None

    def save_subject(
        self,
        name: str,
        teacher_name: str = "",
        teacher_email: str = "",
        cfu: int | float = 0,
        exam_date: str = "",
        notes: str = "",
        original_name: str | None = None,
    ) -> dict[str, Any]:
        """Crea o aggiorna una materia, gestendo anche la rinomina.

        Quando il nome cambia, il nuovo nome viene propagato a sessioni,
        materiali, videolezioni, flashcard ed eventi del calendario, cosi' i
        collegamenti esistenti non vengono persi.
        """
        clean_name = str(name or "").strip()[:150]
        if not clean_name:
            raise ValueError("Il nome della materia e' obbligatorio.")
        clean_teacher = str(teacher_name or "").strip()[:150]
        clean_email = str(teacher_email or "").strip()[:200]
        clean_cfu = max(0, min(MAX_CFU, safe_int(cfu)))
        clean_exam = parse_exam_date(exam_date)
        clean_notes = str(notes or "").strip()[:8000]
        timestamp = now_str()
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous = str(original_name or "").strip()
            renaming = bool(previous) and previous != clean_name
            if renaming:
                if previous.casefold() != clean_name.casefold():
                    duplicate = connection.execute(
                        "SELECT id FROM subjects WHERE LOWER(name) = LOWER(?)",
                        (clean_name,),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError("Esiste gia' una materia con questo nome.")
                existing = connection.execute(
                    "SELECT id FROM subjects WHERE LOWER(name) = LOWER(?)",
                    (previous,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO subjects(name, teacher_name, teacher_email, cfu, "
                        "exam_date, notes, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            clean_name,
                            clean_teacher,
                            clean_email,
                            clean_cfu,
                            clean_exam,
                            clean_notes,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE subjects SET name = ?, teacher_name = ?, "
                        "teacher_email = ?, cfu = ?, exam_date = ?, notes = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            clean_name,
                            clean_teacher,
                            clean_email,
                            clean_cfu,
                            clean_exam,
                            clean_notes,
                            timestamp,
                            int(existing["id"]),
                        ),
                    )
                for table in (
                    "sessions",
                    "materials",
                    "video_lessons",
                    "flashcards",
                    "calendar_events",
                    "doubts",
                ):
                    connection.execute(
                        f"UPDATE {table} SET subject = ? "
                        "WHERE LOWER(subject) = LOWER(?)",
                        (clean_name, previous),
                    )
            else:
                existing = connection.execute(
                    "SELECT id FROM subjects WHERE LOWER(name) = LOWER(?)",
                    (clean_name,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO subjects(name, teacher_name, teacher_email, cfu, "
                        "exam_date, notes, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            clean_name,
                            clean_teacher,
                            clean_email,
                            clean_cfu,
                            clean_exam,
                            clean_notes,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE subjects SET name = ?, teacher_name = ?, "
                        "teacher_email = ?, cfu = ?, exam_date = ?, notes = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            clean_name,
                            clean_teacher,
                            clean_email,
                            clean_cfu,
                            clean_exam,
                            clean_notes,
                            timestamp,
                            int(existing["id"]),
                        ),
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        subject = self.get_subject(clean_name)
        if subject is None:
            raise RuntimeError("Materia salvata ma non leggibile.")
        return subject

    def delete_subject(self, name: str) -> None:
        """Elimina i dettagli di una materia solo se non ha dati collegati."""
        subject = self.get_subject(name)
        if subject is None or not safe_int(subject.get("subject_id", 0)):
            raise ValueError("Materia non trovata.")
        linked = sum(
            safe_int(subject.get(key, 0))
            for key in ("materials", "videos", "flashcards", "events", "sessions")
        )
        if linked:
            raise ValueError(
                "La materia ha dati collegati e non puo' essere eliminata. "
                "Rinominala oppure rimuovi prima i collegamenti."
            )
        self._connection().execute(
            "DELETE FROM subjects WHERE id = ?",
            (safe_int(subject["subject_id"]),),
        )

    def list_subject_tags(self) -> list[str]:
        """Nomi delle materie note, senza duplicati di maiuscole/minuscole."""
        return [str(subject["name"]) for subject in self.list_subjects()]

    # --------------------------------------------------------------- calendar
    def import_calendar_events(self, events: Iterable[dict[str, Any]], source_file: str = "") -> int:
        rows: list[tuple[Any, ...]] = []
        imported_at = now_str()
        for event in events:
            uid = str(event.get("uid", "")).strip()
            title = str(event.get("title", "")).strip()
            start = str(event.get("start", "")).strip()
            end = str(event.get("end", "")).strip()
            if not uid or not title or not start or not end:
                continue
            rows.append(
                (
                    uid[:400],
                    title[:500],
                    str(event.get("subject", "")).strip()[:150],
                    start,
                    end,
                    str(event.get("location", "")).strip()[:500],
                    str(event.get("description", "")).strip(),
                    1 if event.get("all_day") else 0,
                    source_file[:500],
                    imported_at,
                )
            )
        if not rows:
            return 0
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "INSERT INTO calendar_events(uid, title, subject, start, end, location, "
                "description, all_day, source_file, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uid, start) DO UPDATE SET "
                "title = excluded.title, subject = excluded.subject, end = excluded.end, "
                "location = excluded.location, description = excluded.description, "
                "all_day = excluded.all_day, source_file = excluded.source_file, "
                "imported_at = excluded.imported_at",
                rows,
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return len(rows)

    def get_events(self, start: str, end: str) -> list[dict[str, Any]]:
        # I tombstone della coda offline (eliminazioni non ancora confermate da
        # Google) non devono mai comparire nell'agenda.
        rows = self._connection().execute(
            "SELECT * FROM calendar_events WHERE end > ? AND start < ? "
            "AND source <> ? ORDER BY start, title",
            (start, end, EVENT_PENDING_DELETE),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM calendar_events WHERE id = ?", (int(event_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_events_for_materials(self) -> list[dict[str, Any]]:
        start = (datetime.now() - timedelta(days=1)).strftime(LOCAL_DATETIME_FMT)
        end = (datetime.now() + timedelta(days=400)).strftime(LOCAL_DATETIME_FMT)
        return self.get_events(start, end)

    def clear_calendar_events(self) -> int:
        """Svuota l'agenda locale.

        I tombstone delle eliminazioni in coda restano, altrimenti un evento
        cancellato offline non verrebbe mai rimosso da Google. Gli eventi
        sincronizzati vengono riscaricati alla sincronizzazione successiva.
        """
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM calendar_events WHERE source <> ?",
                    (EVENT_PENDING_DELETE,),
                ).fetchall()
            ]
            self._unlink_event_refs(connection, ids)
            cursor = connection.execute(
                "DELETE FROM calendar_events WHERE source <> ?", (EVENT_PENDING_DELETE,)
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return int(cursor.rowcount)

    # ------------------------------------------------------- google calendar
    @staticmethod
    def _unlink_event_refs(connection: sqlite3.Connection, event_ids: Iterable[int]) -> None:
        """Scollega sessioni e materiali dagli eventi senza cancellare lo storico."""
        ids = [int(value) for value in event_ids]
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            marks = ", ".join("?" for _ in chunk)
            connection.execute(
                f"UPDATE sessions SET calendar_event_id = NULL "
                f"WHERE calendar_event_id IN ({marks})",
                chunk,
            )
            connection.execute(
                f"UPDATE materials SET calendar_event_id = NULL "
                f"WHERE calendar_event_id IN ({marks})",
                chunk,
            )

    def _delete_events(self, connection: sqlite3.Connection, event_ids: list[int]) -> int:
        if not event_ids:
            return 0
        self._unlink_event_refs(connection, event_ids)
        removed = 0
        for offset in range(0, len(event_ids), 500):
            chunk = event_ids[offset:offset + 500]
            marks = ", ".join("?" for _ in chunk)
            cursor = connection.execute(
                f"DELETE FROM calendar_events WHERE id IN ({marks})", chunk
            )
            removed += int(cursor.rowcount)
        return removed

    def subject_calendar_map(self) -> dict[str, dict[str, str]]:
        """``google_calendar_id -> {name, color_hex}`` per le materie collegate."""
        rows = self._connection().execute(
            "SELECT name, google_calendar_id, COALESCE(color_hex, '') AS color_hex "
            "FROM subjects WHERE google_calendar_id IS NOT NULL"
        ).fetchall()
        return {
            str(row["google_calendar_id"]): {
                "name": str(row["name"]),
                "color_hex": str(row["color_hex"] or ""),
            }
            for row in rows
        }

    def get_subject_calendar(self, subject: str) -> tuple[str | None, str | None]:
        """Calendario Google e colore della materia (``(None, None)`` se assenti)."""
        clean = str(subject or "").strip()
        if not clean:
            return None, None
        row = self._connection().execute(
            "SELECT google_calendar_id, color_hex FROM subjects "
            "WHERE LOWER(TRIM(name)) = LOWER(?)",
            (clean,),
        ).fetchone()
        if row is None:
            return None, None
        return (row["google_calendar_id"] or None), (row["color_hex"] or None)

    def link_subject_calendar(
        self,
        subject: str,
        google_calendar_id: str | None,
        color_hex: str | None = None,
    ) -> None:
        """Collega (o scollega con ``None``) una materia a un calendario Google.

        Un calendario appartiene al massimo a una materia: un collegamento
        precedente dello stesso calendario viene rimosso. Se la materia esiste
        solo come etichetta viene creata la sua riga in ``subjects``.
        """
        clean = str(subject or "").strip()[:150]
        if not clean:
            raise ValueError("Il nome della materia e' obbligatorio.")
        calendar_id = str(google_calendar_id or "").strip() or None
        color = normalize_hex(color_hex)
        timestamp = now_str()
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if calendar_id is not None:
                connection.execute(
                    "UPDATE subjects SET google_calendar_id = NULL, updated_at = ? "
                    "WHERE google_calendar_id = ? AND LOWER(name) <> LOWER(?)",
                    (timestamp, calendar_id, clean),
                )
            existing = connection.execute(
                "SELECT id FROM subjects WHERE LOWER(name) = LOWER(?)", (clean,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO subjects(name, created_at, updated_at, "
                    "google_calendar_id, color_hex) VALUES (?, ?, ?, ?, ?)",
                    (clean, timestamp, timestamp, calendar_id, color),
                )
            elif calendar_id is None:
                connection.execute(
                    "UPDATE subjects SET google_calendar_id = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (timestamp, int(existing["id"])),
                )
            else:
                connection.execute(
                    "UPDATE subjects SET google_calendar_id = ?, "
                    "color_hex = COALESCE(?, color_hex), updated_at = ? WHERE id = ?",
                    (calendar_id, color, timestamp, int(existing["id"])),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def unlink_calendar(self, google_calendar_id: str) -> None:
        self._connection().execute(
            "UPDATE subjects SET google_calendar_id = NULL, updated_at = ? "
            "WHERE google_calendar_id = ?",
            (now_str(), str(google_calendar_id)),
        )

    def set_subject_color(self, subject: str, color_hex: str | None) -> None:
        """Colore della materia (crea la riga in ``subjects`` se e' solo un'etichetta)."""
        clean = str(subject or "").strip()[:150]
        color = normalize_hex(color_hex)
        if not clean or color is None:
            return
        timestamp = now_str()
        connection = self._connection()
        existing = connection.execute(
            "SELECT id FROM subjects WHERE LOWER(name) = LOWER(?)", (clean,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO subjects(name, created_at, updated_at, color_hex) VALUES (?, ?, ?, ?)",
                (clean, timestamp, timestamp, color),
            )
        else:
            connection.execute(
                "UPDATE subjects SET color_hex = ?, updated_at = ? WHERE id = ?",
                (color, timestamp, int(existing["id"])),
            )

    def set_calendar_color(self, google_calendar_id: str, color_hex: str | None) -> None:
        """Aggiorna il colore della materia collegata al calendario indicato."""
        color = normalize_hex(color_hex)
        if color is None:
            return
        self._connection().execute(
            "UPDATE subjects SET color_hex = ? WHERE google_calendar_id = ? "
            "AND COALESCE(color_hex, '') <> ?",
            (color, str(google_calendar_id), color),
        )

    def save_local_event(
        self,
        event_id: int | None,
        title: str,
        subject: str,
        start: str,
        end: str,
        all_day: bool = False,
        description: str = "",
        color_hex: str | None = None,
        color_id: str | None = None,
        location: str | None = None,
        google_connected: bool = True,
    ) -> int:
        """Crea o modifica un evento da StudyFlow e lo accoda verso Google.

        Transizioni di ``source``:

        - nuovo evento con materia collegata a un calendario (account connesso)
          -> ``google_pending_create``; altrimenti ``manual`` (solo locale);
        - ``google_api`` modificato -> ``google_pending_update``; se la materia
          porta l'evento su un altro calendario viene ricordato quello di
          origine in ``previous_google_calendar_id`` per la ``move``;
        - ``google_pending_create`` resta tale (non esiste ancora su Google);
        - ``manual`` assegnato a una materia collegata -> ``google_pending_create``;
        - ``ics_file`` resta locale: l'importazione .ics non cambia.
        """
        clean_title = str(title or "").strip()[:500]
        if not clean_title:
            raise ValueError("Il titolo dell'evento e' obbligatorio.")
        start_dt = _parse_local(start)
        end_dt = _parse_local(end)
        if start_dt is None or end_dt is None:
            raise ValueError("Data o ora dell'evento non valide.")
        if end_dt <= start_dt:
            raise ValueError("La fine dell'evento deve essere successiva all'inizio.")
        clean_subject = str(subject or "").strip()[:150]
        clean_description = str(description or "").strip()
        target_calendar, subject_color = self.get_subject_calendar(clean_subject)
        clean_color_id = str(color_id or "").strip() or None
        final_color = normalize_hex(color_hex) or subject_color
        values = {
            "title": clean_title,
            "subject": clean_subject,
            "start": start_dt.strftime(LOCAL_DATETIME_FMT),
            "end": end_dt.strftime(LOCAL_DATETIME_FMT),
            "all_day": 1 if all_day else 0,
            "description": clean_description,
            "color_hex": final_color,
            "color_id": clean_color_id,
        }
        timestamp = now_str()
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if event_id is None:
                push = bool(target_calendar) and google_connected
                cursor = connection.execute(
                    "INSERT INTO calendar_events(uid, title, subject, start, end, "
                    "location, description, all_day, source_file, imported_at, source, "
                    "google_calendar_id, color_hex, color_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
                    (
                        f"studyflow-local-{uuid.uuid4().hex}",
                        values["title"],
                        values["subject"],
                        values["start"],
                        values["end"],
                        str(location or "").strip()[:500],
                        values["description"],
                        values["all_day"],
                        timestamp,
                        EVENT_PENDING_CREATE if push else EVENT_SOURCE_MANUAL,
                        target_calendar if push else None,
                        values["color_hex"],
                        values["color_id"],
                    ),
                )
                new_id = int(cursor.lastrowid)
                connection.execute("COMMIT")
                return new_id

            row = connection.execute(
                "SELECT * FROM calendar_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            if row is None or row["source"] == EVENT_PENDING_DELETE:
                raise ValueError("Evento non trovato.")
            source = str(row["source"])
            current_calendar = row["google_calendar_id"]
            previous_calendar = row["previous_google_calendar_id"]
            new_source = source
            new_calendar = current_calendar
            if source in (EVENT_SOURCE_GOOGLE, EVENT_PENDING_UPDATE):
                new_source = EVENT_PENDING_UPDATE
                if target_calendar and target_calendar != current_calendar:
                    if previous_calendar is None:
                        previous_calendar = current_calendar
                    new_calendar = target_calendar
                if previous_calendar == new_calendar:
                    previous_calendar = None
            elif source == EVENT_PENDING_CREATE:
                if target_calendar:
                    new_calendar = target_calendar
            elif source == EVENT_SOURCE_MANUAL:
                if target_calendar and google_connected:
                    new_source = EVENT_PENDING_CREATE
                    new_calendar = target_calendar
            # ics_file: modifica solo locale.
            connection.execute(
                "UPDATE calendar_events SET title = ?, subject = ?, start = ?, end = ?, "
                "all_day = ?, description = ?, color_hex = ?, color_id = ?, "
                "location = COALESCE(?, location), source = ?, google_calendar_id = ?, "
                "previous_google_calendar_id = ?, sync_error = '', imported_at = ?, "
                "local_revision = local_revision + 1 WHERE id = ?",
                (
                    values["title"],
                    values["subject"],
                    values["start"],
                    values["end"],
                    values["all_day"],
                    values["description"],
                    values["color_hex"],
                    values["color_id"],
                    None if location is None else str(location).strip()[:500],
                    new_source,
                    new_calendar,
                    previous_calendar,
                    timestamp,
                    int(event_id),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return int(event_id)

    def delete_local_event(self, event_id: int) -> str:
        """Elimina un evento da StudyFlow.

        Gli eventi presenti su Google diventano tombstone ``google_pending_delete``
        (spariscono subito dall'agenda); gli altri vengono rimossi. In entrambi
        i casi sessioni e materiali vengono solo scollegati.
        Restituisce ``"queued"`` oppure ``"deleted"``.
        """
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM calendar_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            if row is None or row["source"] == EVENT_PENDING_DELETE:
                connection.execute("COMMIT")
                return "deleted"
            source = str(row["source"])
            if source in (EVENT_SOURCE_GOOGLE, EVENT_PENDING_UPDATE) and row["google_event_id"]:
                # Se uno spostamento non e' ancora stato inviato, l'evento si
                # trova ancora nel calendario di origine.
                real_calendar = row["previous_google_calendar_id"] or row["google_calendar_id"]
                conflict = connection.execute(
                    "SELECT id FROM calendar_events WHERE google_calendar_id = ? "
                    "AND google_event_id = ? AND id <> ?",
                    (real_calendar, row["google_event_id"], int(event_id)),
                ).fetchone()
                if conflict is not None:
                    self._delete_events(connection, [int(conflict["id"])])
                self._unlink_event_refs(connection, [int(event_id)])
                connection.execute(
                    "UPDATE calendar_events SET source = ?, google_calendar_id = ?, "
                    "previous_google_calendar_id = NULL, sync_error = '', imported_at = ?, "
                    "local_revision = local_revision + 1 WHERE id = ?",
                    (EVENT_PENDING_DELETE, real_calendar, now_str(), int(event_id)),
                )
                result = "queued"
            else:
                self._delete_events(connection, [int(event_id)])
                result = "deleted"
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return result

    def list_pending_events(self) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM calendar_events WHERE source IN (?, ?, ?) ORDER BY id",
            EVENT_PENDING_SOURCES,
        ).fetchall()
        return [dict(row) for row in rows]

    def count_pending_events(self) -> int:
        row = self._connection().execute(
            "SELECT COUNT(*) AS total FROM calendar_events WHERE source IN (?, ?, ?)",
            EVENT_PENDING_SOURCES,
        ).fetchone()
        return safe_int(row["total"] if row else 0)

    def mark_event_synced(
        self,
        event_id: int,
        google_event_id: str,
        google_calendar_id: str,
        revision: int,
    ) -> str:
        """Conferma che Google ha applicato la modifica letta alla ``revision`` data.

        Restituisce:

        - ``"synced"``: nessuna modifica locale nel frattempo, ``source='google_api'``;
        - ``"changed"``: l'utente ha modificato l'evento mentre la richiesta era in
          volo; resta in coda come ``google_pending_update`` (con l'id Google);
        - ``"missing"``: l'evento e' stato eliminato localmente nel frattempo
          (il chiamante deve rimuoverlo anche da Google).
        """
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT source, local_revision, google_calendar_id FROM calendar_events "
                "WHERE id = ?",
                (int(event_id),),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return "missing"
            if row["source"] == EVENT_PENDING_DELETE:
                # Eliminato mentre veniva creato/aggiornato: il tombstone deve
                # puntare all'evento reale.
                connection.execute(
                    "UPDATE calendar_events SET google_event_id = ?, google_calendar_id = ? "
                    "WHERE id = ?",
                    (str(google_event_id), str(google_calendar_id), int(event_id)),
                )
                connection.execute("COMMIT")
                return "changed"
            if safe_int(row["local_revision"]) == int(revision):
                connection.execute(
                    "UPDATE calendar_events SET source = ?, google_event_id = ?, "
                    "google_calendar_id = ?, previous_google_calendar_id = NULL, "
                    "sync_error = '', uid = CASE WHEN uid LIKE 'studyflow-local-%' "
                    "THEN ? ELSE uid END WHERE id = ?",
                    (
                        EVENT_SOURCE_GOOGLE,
                        str(google_event_id),
                        str(google_calendar_id),
                        google_uid(google_calendar_id, google_event_id),
                        int(event_id),
                    ),
                )
                result = "synced"
            else:
                current_calendar = row["google_calendar_id"]
                connection.execute(
                    "UPDATE calendar_events SET source = ?, google_event_id = ?, "
                    "previous_google_calendar_id = ?, google_calendar_id = ? WHERE id = ?",
                    (
                        EVENT_PENDING_UPDATE,
                        str(google_event_id),
                        None
                        if not current_calendar or current_calendar == google_calendar_id
                        else str(google_calendar_id),
                        current_calendar or str(google_calendar_id),
                        int(event_id),
                    ),
                )
                result = "changed"
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return result

    def mark_event_moved(self, event_id: int, moved_to: str) -> None:
        """La ``move`` e' riuscita: ora l'evento si trova in ``moved_to``."""
        target = str(moved_to)
        self._connection().execute(
            "UPDATE calendar_events SET "
            "google_calendar_id = CASE WHEN source = ? THEN ? ELSE google_calendar_id END, "
            "previous_google_calendar_id = CASE "
            "WHEN source = ? THEN NULL "
            "WHEN google_calendar_id = ? THEN NULL ELSE ? END "
            "WHERE id = ?",
            (EVENT_PENDING_DELETE, target, EVENT_PENDING_DELETE, target, target, int(event_id)),
        )

    def requeue_as_create(self, event_id: int) -> None:
        """L'evento modificato non esiste piu' su Google: verra' ricreato."""
        self._connection().execute(
            "UPDATE calendar_events SET source = ?, google_event_id = NULL, "
            "previous_google_calendar_id = NULL WHERE id = ? AND source = ?",
            (EVENT_PENDING_CREATE, int(event_id), EVENT_PENDING_UPDATE),
        )

    def set_event_sync_error(self, event_id: int, message: str) -> None:
        self._connection().execute(
            "UPDATE calendar_events SET sync_error = ? WHERE id = ?",
            (str(message or "")[:500], int(event_id)),
        )

    def purge_tombstone(self, event_id: int) -> None:
        self._connection().execute(
            "DELETE FROM calendar_events WHERE id = ? AND source = ?",
            (int(event_id), EVENT_PENDING_DELETE),
        )

    def upsert_google_events(
        self,
        google_calendar_id: str,
        events: Iterable[dict[str, Any]],
        source: str = EVENT_SOURCE_GOOGLE,
        source_file: str = "Google Calendar",
    ) -> dict[str, int]:
        """Inserisce o aggiorna gli eventi scaricati da un calendario Google.

        Non tocca mai i record locali in ``google_pending_update`` o
        ``google_pending_delete`` (anche se lo spostamento verso un altro
        calendario non e' ancora stato inviato): le modifiche locali in coda
        hanno la precedenza sul pull.
        """
        calendar_id = str(google_calendar_id)
        imported_at = now_str()
        inserted = updated = skipped = 0
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                google_event_id = str(event.get("google_event_id") or "").strip()
                start = str(event.get("start") or "").strip()
                end = str(event.get("end") or "").strip()
                if not google_event_id or not start or not end:
                    continue
                pending = connection.execute(
                    "SELECT id FROM calendar_events WHERE google_event_id = ? "
                    "AND source IN (?, ?) AND (google_calendar_id = ? "
                    "OR previous_google_calendar_id = ?)",
                    (
                        google_event_id,
                        EVENT_PENDING_UPDATE,
                        EVENT_PENDING_DELETE,
                        calendar_id,
                        calendar_id,
                    ),
                ).fetchone()
                if pending is not None:
                    skipped += 1
                    continue
                values = (
                    str(event.get("title") or "Evento senza titolo").strip()[:500],
                    str(event.get("subject") or "").strip()[:150],
                    start,
                    end,
                    str(event.get("location") or "").strip()[:500],
                    str(event.get("description") or "").strip(),
                    1 if event.get("all_day") else 0,
                    normalize_hex(event.get("color_hex")),
                    str(event.get("color_id") or "").strip() or None,
                    imported_at,
                )
                existing = connection.execute(
                    "SELECT id FROM calendar_events WHERE google_calendar_id = ? "
                    "AND google_event_id = ?",
                    (calendar_id, google_event_id),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        "UPDATE calendar_events SET title = ?, subject = ?, start = ?, "
                        "end = ?, location = ?, description = ?, all_day = ?, "
                        "color_hex = ?, color_id = ?, imported_at = ?, source = ?, "
                        "sync_error = '' WHERE id = ?",
                        (*values, source, int(existing["id"])),
                    )
                    updated += 1
                else:
                    connection.execute(
                        "INSERT INTO calendar_events(title, subject, start, end, location, "
                        "description, all_day, color_hex, color_id, imported_at, uid, "
                        "source_file, source, google_event_id, google_calendar_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            *values,
                            google_uid(calendar_id, google_event_id),
                            str(source_file or "")[:500],
                            source,
                            google_event_id,
                            calendar_id,
                        ),
                    )
                    inserted += 1
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def mirror_google_deletions(
        self,
        google_calendar_id: str,
        window_start: str,
        window_end: str,
        remote_event_ids: Iterable[str],
        source: str = EVENT_SOURCE_GOOGLE,
    ) -> int:
        """Rimuove gli eventi ``google_api`` spariti da Google nella finestra attiva.

        Prima di eliminarli azzera ``calendar_event_id`` su sessioni e
        materiali, cosi' lo storico di studio resta intatto.
        """
        keep = {str(value) for value in remote_event_ids}
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT id, google_event_id FROM calendar_events "
                "WHERE source = ? AND google_calendar_id = ? AND end > ? AND start < ?",
                (source, str(google_calendar_id), window_start, window_end),
            ).fetchall()
            stale = [
                int(row["id"])
                for row in rows
                if str(row["google_event_id"] or "") not in keep
            ]
            removed = self._delete_events(connection, stale)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return removed

    def purge_unsynced_calendars(
        self,
        enabled_calendar_ids: Iterable[str],
        source: str = EVENT_SOURCE_GOOGLE,
    ) -> int:
        """Rimuove gli eventi (di ``source``) dei calendari non piu' sincronizzati."""
        enabled = [str(value) for value in enabled_calendar_ids]
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            sql = "SELECT id FROM calendar_events WHERE source = ?"
            params: list[Any] = [source]
            if enabled:
                sql += f" AND google_calendar_id NOT IN ({', '.join('?' for _ in enabled)})"
                params.extend(enabled)
            ids = [int(row["id"]) for row in connection.execute(sql, params).fetchall()]
            removed = self._delete_events(connection, ids)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return removed

    # ---------------------------------------------------------------- blockers
    def list_blocked_sites(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM blocked_sites"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY domain"
        rows = self._connection().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def add_blocked_site(self, domain: str) -> int:
        cursor = self._connection().execute(
            "INSERT INTO blocked_sites(domain, enabled) VALUES (?, 1) "
            "ON CONFLICT(domain) DO UPDATE SET enabled = 1",
            (domain,),
        )
        row = self._connection().execute(
            "SELECT id FROM blocked_sites WHERE domain = ?", (domain,)
        ).fetchone()
        return int(row["id"]) if row is not None else int(cursor.lastrowid)

    def delete_blocked_site(self, site_id: int) -> None:
        self._connection().execute("DELETE FROM blocked_sites WHERE id = ?", (int(site_id),))

    def set_blocked_site_enabled(self, site_id: int, enabled: bool) -> None:
        self._connection().execute(
            "UPDATE blocked_sites SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, int(site_id)),
        )

    def list_blocked_apps(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM blocked_apps"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY LOWER(display_name)"
        rows = self._connection().execute(sql).fetchall()
        return [dict(row) for row in rows]

    def add_blocked_app(self, display_name: str, process_name: str) -> int:
        cursor = self._connection().execute(
            "INSERT INTO blocked_apps(display_name, process_name, enabled) VALUES (?, ?, 1) "
            "ON CONFLICT(process_name) DO UPDATE SET display_name = excluded.display_name, "
            "enabled = 1",
            (display_name.strip()[:150], process_name),
        )
        row = self._connection().execute(
            "SELECT id FROM blocked_apps WHERE process_name = ?", (process_name,)
        ).fetchone()
        return int(row["id"]) if row is not None else int(cursor.lastrowid)

    def delete_blocked_app(self, app_id: int) -> None:
        self._connection().execute("DELETE FROM blocked_apps WHERE id = ?", (int(app_id),))

    def set_blocked_app_enabled(self, app_id: int, enabled: bool) -> None:
        self._connection().execute(
            "UPDATE blocked_apps SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, int(app_id)),
        )

    # --------------------------------------------------------------- analytics
    def period_stats(self, start: str, end: str) -> dict[str, int]:
        connection = self._connection()
        row = connection.execute(
            "SELECT COUNT(*) AS started, "
            "COALESCE(SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END), 0) AS completed, "
            "COALESCE(SUM(duration_minutes), 0) AS minutes "
            "FROM sessions WHERE timestamp >= ? AND timestamp < ?",
            (start, end),
        ).fetchone()
        attempts = connection.execute(
            "SELECT COUNT(*) AS count FROM distraction_attempts "
            "WHERE timestamp >= ? AND timestamp < ?",
            (start, end),
        ).fetchone()
        clean = connection.execute(
            "SELECT COUNT(*) AS count FROM sessions s WHERE s.completed = 1 "
            "AND s.timestamp >= ? AND s.timestamp < ? AND NOT EXISTS ("
            "SELECT 1 FROM distraction_attempts d WHERE d.session_id = s.id)",
            (start, end),
        ).fetchone()
        return {
            "started": safe_int(row["started"] if row else 0),
            "completed": safe_int(row["completed"] if row else 0),
            "minutes": safe_int(row["minutes"] if row else 0),
            "attempts": safe_int(attempts["count"] if attempts else 0),
            "clean_completed": safe_int(clean["count"] if clean else 0),
        }

    def hours_by_day(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT substr(timestamp, 1, 10) AS day, "
            "ROUND(SUM(duration_minutes) / 60.0, 2) AS hours "
            "FROM sessions WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY day ORDER BY day",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    def hours_by_subject(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT COALESCE(NULLIF(TRIM(subject), ''), 'Senza materia') AS subject, "
            "ROUND(SUM(duration_minutes) / 60.0, 2) AS hours "
            "FROM sessions WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY subject ORDER BY hours DESC",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    def attempts_by_hour(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT CAST(substr(timestamp, 12, 2) AS INTEGER) AS hour, COUNT(*) AS attempts "
            "FROM distraction_attempts WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY hour ORDER BY hour",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    def attempts_by_target(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT source_type, target_name, COUNT(*) AS attempts "
            "FROM distraction_attempts WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY source_type, target_name ORDER BY attempts DESC, target_name LIMIT 12",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    def attempts_by_type(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT source_type, COUNT(*) AS attempts FROM distraction_attempts "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY source_type",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]
