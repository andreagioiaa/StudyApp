"""Salvataggio temporaneo (bozza) del modulo PDF e Slide.

La bozza conserva su disco i documenti estratti e le modifiche manuali, cosi'
il lavoro non si perde cambiando sezione o chiudendo l'app. E' un unico file
JSON nella cartella dati; la scrittura e' atomica (file temporaneo + replace)
per non lasciare bozze corrotte se l'app si chiude a meta'.

Nessuna dipendenza dalla GUI: la vista passa e riceve dizionari semplici.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DRAFT_VERSION = 1
DRAFT_FILENAME = "pdf_draft.json"

_DOC_FIELDS = ("path", "title", "source", "fmt", "warnings", "engines", "error")


def default_draft_path() -> Path:
    from ...paths import app_data_dir

    return app_data_dir() / DRAFT_FILENAME


def _clean_doc(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("path"):
        return None
    return {
        "path": str(raw.get("path", "")),
        "title": str(raw.get("title", "")),
        "source": str(raw.get("source", "")),
        "fmt": str(raw.get("fmt", "") or "markdown"),
        "warnings": [str(item) for item in raw.get("warnings", []) if item] if isinstance(raw.get("warnings"), list) else [],
        "engines": {str(k): int(v) for k, v in raw.get("engines", {}).items()} if isinstance(raw.get("engines"), dict) else {},
        "error": str(raw.get("error", "")),
    }


def save_draft(path: Path, docs: list[dict[str, Any]], current: int = -1, sources: list[str] | None = None) -> float:
    """Scrive la bozza e restituisce il timestamp del salvataggio."""
    saved_at = time.time()
    payload = {
        "version": DRAFT_VERSION,
        "saved_at": saved_at,
        "current": int(current),
        "sources": [str(item) for item in (sources or [])],
        "docs": [{key: doc.get(key) for key in _DOC_FIELDS} for doc in docs],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return saved_at


def load_draft(path: Path) -> dict[str, Any] | None:
    """Legge la bozza; None se assente, vuota o illeggibile."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.warning("Bozza PDF illeggibile, ignorata: %s", path)
        return None
    if not isinstance(data, dict):
        return None
    docs = [doc for doc in (_clean_doc(raw) for raw in data.get("docs", []) or []) if doc is not None]
    if not docs:
        return None
    current = data.get("current", 0)
    current = current if isinstance(current, int) and 0 <= current < len(docs) else 0
    sources = [str(item) for item in data.get("sources", []) or [] if item]
    try:
        saved_at = float(data.get("saved_at", 0.0))
    except (TypeError, ValueError):
        saved_at = 0.0
    return {"docs": docs, "current": current, "sources": sources, "saved_at": saved_at}


def delete_draft(path: Path) -> bool:
    """Elimina la bozza. True se un file e' stato rimosso."""
    path = Path(path)
    path.with_name(path.name + ".tmp").unlink(missing_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
