"""Percorsi applicativi e risoluzione delle risorse in modalita' sorgente o PyInstaller."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "StudyFlow"


def project_root() -> Path:
    """Directory del progetto (utile in modalita' sorgente)."""
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str | Path) -> Path:
    """Restituisce una risorsa inclusa nell'exe one-file o nel progetto sorgente."""
    base = Path(getattr(sys, "_MEIPASS", project_root()))
    return base / Path(relative)


def app_data_dir() -> Path:
    """Cartella dati persistente, sempre scrivibile e separata da Program Files."""
    override = os.environ.get("STUDYFLOW_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(root) / APP_DIR_NAME
    else:
        path = Path.home() / f".{APP_DIR_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def sounds_dir(data: Path) -> Path:
    """Cartella ``sounds`` accanto all'app, con fallback nella cartella dati.

    In modalita' sorgente usa la radice del progetto; nell'exe usa la cartella
    che contiene StudyFlow.exe. Se quella posizione non e' scrivibile (ad esempio
    Program Files), usa ``%LOCALAPPDATA%\\StudyFlow\\sounds``.
    """
    override = os.environ.get("STUDYFLOW_SOUNDS_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    elif os.environ.get("STUDYFLOW_DATA_DIR", "").strip():
        path = data / "sounds"
    elif is_frozen():
        path = executable_path().parent / "sounds"
    else:
        path = project_root() / "sounds"
    if _is_writable(path):
        return path
    fallback = data / "sounds"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def subjects_dir(data: Path) -> Path:
    """Cartella ``Materie`` nella radice di funzionamento dell'app.

    Come per ``sounds``: in modalita' sorgente e' la radice del progetto,
    nell'exe la cartella che contiene StudyFlow.exe. Se non e' scrivibile
    (es. Program Files) si usa ``%LOCALAPPDATA%\\StudyFlow\\Materie``.
    ``STUDYFLOW_SUBJECTS_DIR`` permette di scegliere un percorso diverso.
    """
    override = os.environ.get("STUDYFLOW_SUBJECTS_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    elif os.environ.get("STUDYFLOW_DATA_DIR", "").strip():
        path = data / "Materie"
    elif is_frozen():
        path = executable_path().parent / "Materie"
    else:
        path = project_root() / "Materie"
    if _is_writable(path):
        return path
    fallback = data / "Materie"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def ensure_data_layout() -> dict[str, Path]:
    """Crea e restituisce tutti i percorsi usati dall'applicazione."""
    data = app_data_dir()
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    sounds = sounds_dir(data)
    transcripts = data / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    models = data / "models"
    models.mkdir(parents=True, exist_ok=True)
    subjects = subjects_dir(data)
    paths = {
        "data": data,
        "logs": logs,
        "sounds": sounds,
        "transcripts": transcripts,
        "models": models,
        "subjects": subjects,
        "db": data / "study_manager.db",
        "log": logs / "studyflow.log",
        "guard_log": logs / "hosts_guard.log",
        "hosts_control": data / "hosts_guard_control.json",
        "hosts_status": data / "hosts_guard_status.json",
        "hosts_backup": data / "hosts_original.backup",
        "icon": resource_path("assets/app.ico"),
    }
    return paths


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_path() -> Path:
    return Path(sys.executable).resolve()
