"""Persistenza SQLite thread-safe per StudyFlow."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULT_BLOCKED_APPS, DEFAULT_BLOCKED_SITES, DEFAULT_SETTINGS
from .services.spaced_repetition import DEFAULT_EASE_FACTOR, schedule_card
from .services.subjects import MAX_CFU, cfu_to_hours, parse_exam_date
from .utils import LOCAL_DATE_FMT, LOCAL_DATETIME_FMT, now_str, safe_float, safe_int


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 6

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
    timestamp TEXT NOT NULL
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
    updated_at TEXT NOT NULL
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
    def create_session(self, subject: str, timestamp: str | None = None) -> int:
        cursor = self._connection().execute(
            "INSERT INTO sessions(subject, duration_minutes, completed, timestamp) "
            "VALUES (?, 0, 0, ?)",
            (subject.strip() or "Studio", timestamp or now_str()),
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
            "WHERE LOWER(e.subject) = LOWER(n.name)) AS events, "
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
        rows = self._connection().execute(
            "SELECT * FROM calendar_events WHERE end > ? AND start < ? "
            "ORDER BY start, title",
            (start, end),
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
        cursor = self._connection().execute("DELETE FROM calendar_events")
        return int(cursor.rowcount)

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
