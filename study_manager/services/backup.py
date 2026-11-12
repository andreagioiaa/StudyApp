"""Esportazione e importazione completa dei dati StudyFlow.

Il backup e' un archivio ZIP portabile che contiene:

- ``manifest.json`` con formato, versione e riepilogo;
- ``data.json`` con tutte le tabelle SQLite;
- ``transcripts/*.txt`` con le trascrizioni delle videolezioni.

I modelli whisper.cpp e i file audio non vengono inclusi: sono grandi e possono
essere scaricati o reimportati separatamente.
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import APP_VERSION, GOOGLE_ACCOUNT_SETTINGS
from ..database import DATA_TABLES, SCHEMA_VERSION, Database
from ..utils import now_str


BACKUP_FORMAT = "studyflow-backup"
BACKUP_VERSION = 1
BACKUP_EXTENSION = ".zip"
MANIFEST_NAME = "manifest.json"
DATA_NAME = "data.json"
TRANSCRIPTS_PREFIX = "transcripts/"


class BackupError(RuntimeError):
    """Errore di backup o ripristino mostrabile all'utente."""


def _target_path(target: str | Path) -> Path:
    path = Path(target).expanduser()
    if path.suffix.lower() != BACKUP_EXTENSION:
        path = path.with_suffix(BACKUP_EXTENSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _strip_account_settings(tables: dict[str, Any]) -> dict[str, Any]:
    """Toglie token OAuth, ``client_secret`` e stato account Google dal backup."""
    rows = tables.get("settings")
    if isinstance(rows, list):
        tables["settings"] = [
            row
            for row in rows
            if not (isinstance(row, dict) and str(row.get("key", "")) in GOOGLE_ACCOUNT_SETTINGS)
        ]
    return tables


def _table_counts(tables: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, rows in tables.items():
        if isinstance(rows, list):
            counts[str(name)] = len(rows)
    return counts


def export_backup(
    db: Database,
    paths: dict[str, Any],
    target: str | Path,
) -> dict[str, Any]:
    """Scrive un backup ZIP atomico e restituisce il riepilogo."""
    destination = _target_path(target)
    tables = _strip_account_settings(db.snapshot_tables())
    counts = _table_counts(tables)
    exported_at = now_str()
    payload = {
        "format": BACKUP_FORMAT,
        "backup_version": BACKUP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "exported_at": exported_at,
        "tables": tables,
    }
    manifest = {
        "format": BACKUP_FORMAT,
        "backup_version": BACKUP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "exported_at": exported_at,
        "counts": counts,
    }
    transcripts_dir = Path(paths["transcripts"])
    transcript_files = sorted(transcripts_dir.glob("*.txt"))
    temporary = destination.with_name(destination.name + ".part")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                DATA_NAME,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
            for file in transcript_files:
                archive.write(file, f"{TRANSCRIPTS_PREFIX}{file.name}")
        os.replace(temporary, destination)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError(f"Esportazione non riuscita: {exc}") from exc
    return {
        "path": str(destination),
        "exported_at": exported_at,
        "counts": counts,
        "transcripts": len(transcript_files),
    }


def inspect_backup(source: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Valida un archivio e restituisce manifest e contenuto delle tabelle."""
    path = Path(source).expanduser()
    if not path.is_file():
        raise BackupError("Il file di backup selezionato non esiste.")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if MANIFEST_NAME not in names or DATA_NAME not in names:
                raise BackupError(
                    "Il file non e' un backup StudyFlow valido: mancano i metadati."
                )
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            payload = json.loads(archive.read(DATA_NAME).decode("utf-8"))
    except BackupError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise BackupError(f"Backup non leggibile: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupError("Il file non e' un backup StudyFlow.")
    version = int(manifest.get("backup_version", 0) or 0)
    if version > BACKUP_VERSION:
        raise BackupError(
            "Il backup e' stato creato da una versione piu' recente di StudyFlow."
        )
    schema_version = int(manifest.get("schema_version", 0) or 0)
    if schema_version > SCHEMA_VERSION:
        raise BackupError(
            "Il backup usa uno schema dati piu' recente e non puo' essere importato "
            "da questa versione di StudyFlow."
        )
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, dict):
        raise BackupError("Il backup non contiene dati validi.")
    for name, rows in tables.items():
        if not isinstance(rows, list):
            raise BackupError(f"La tabella '{name}' non e' valida.")
    return manifest, payload


def _extract_transcripts(source: Path, transcripts_dir: Path) -> int:
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    try:
        with zipfile.ZipFile(source, "r") as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir() or not name.startswith(TRANSCRIPTS_PREFIX):
                    continue
                filename = Path(name).name
                if not filename.lower().endswith(".txt") or not filename:
                    continue
                target = transcripts_dir / filename
                temporary = target.with_name(target.name + ".part")
                try:
                    temporary.write_bytes(archive.read(info))
                    os.replace(temporary, target)
                    extracted += 1
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise BackupError(f"Estrazione delle trascrizioni non riuscita: {exc}") from exc
    return extracted


def import_backup(
    db: Database,
    paths: dict[str, Any],
    source: str | Path,
) -> dict[str, Any]:
    """Sostituisce i dati con un backup, creando prima un backup di sicurezza."""
    path = Path(source).expanduser()
    manifest, payload = inspect_backup(path)
    tables = _strip_account_settings(payload["tables"])
    # Il collegamento Google di questo PC sopravvive al ripristino.
    current_settings = db.get_settings()
    preserved = {
        key: current_settings[key]
        for key in GOOGLE_ACCOUNT_SETTINGS
        if current_settings.get(key)
    }
    data_dir = Path(paths["data"])
    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety_path = backups_dir / f"backup-pre-import-{stamp}.zip"
    safety = export_backup(db, paths, safety_path)
    try:
        db.restore_tables(tables)
        if preserved:
            db.set_settings(preserved)
        extracted = _extract_transcripts(path, Path(paths["transcripts"]))
        # Se un percorso assoluto del backup non esiste su questo PC, prova a
        # ritrovare il .txt nella cartella trascrizioni corrente.
        repaired = 0
        for lesson in db.list_video_lessons():
            transcript_path = str(lesson.get("transcript_path") or "")
            if not transcript_path or Path(transcript_path).is_file():
                continue
            candidate = Path(paths["transcripts"]) / Path(transcript_path).name
            if candidate.is_file():
                db.update_video_transcript_path(
                    int(lesson["id"]),
                    str(candidate),
                    str(lesson.get("transcript_name") or candidate.name),
                )
                repaired += 1
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(
            "Importazione non riuscita. Il backup di sicurezza e' stato creato in "
            f"{safety['path']}.\n\nDettaglio: {exc}"
        ) from exc
    return {
        "path": str(path),
        "manifest": manifest,
        "counts": _table_counts(tables),
        "transcripts": extracted,
        "repaired_transcripts": repaired,
        "safety_backup": safety["path"],
    }


def backup_summary(counts: dict[str, Any]) -> str:
    """Riepilogo leggibile per la finestra di conferma."""
    labels = {
        "sessions": "sessioni",
        "materials": "materiali",
        "video_lessons": "videolezioni",
        "calendar_events": "eventi",
        "subjects": "materie",
        "flashcards": "flashcard",
        "flashcard_reviews": "ripassi",
        "blocked_sites": "siti bloccati",
        "blocked_apps": "app bloccate",
    }
    parts = [
        f"{int(counts.get(key, 0) or 0)} {label}"
        for key, label in labels.items()
        if int(counts.get(key, 0) or 0) > 0
    ]
    return ", ".join(parts) if parts else "nessun dato"
