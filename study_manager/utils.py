"""Utility comuni: date, formattazione, scrittura atomica e dominio."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LOCAL_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
LOCAL_DATE_FMT = "%Y-%m-%d"


def now_local() -> datetime:
    return datetime.now().replace(microsecond=0)


def now_str() -> str:
    return now_local().strftime(LOCAL_DATETIME_FMT)


def date_str(value: datetime | date) -> str:
    return value.strftime(LOCAL_DATE_FMT)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for parser in (
        datetime.fromisoformat,
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%dT%H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except (ValueError, TypeError):
            continue
    return None


def day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    return start.strftime(LOCAL_DATETIME_FMT), end.strftime(LOCAL_DATETIME_FMT)


def week_bounds(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=7)


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    if first.month == 12:
        following = first.replace(year=first.year + 1, month=1)
    else:
        following = first.replace(month=first.month + 1)
    return first, following


def format_minutes(minutes: float | int) -> str:
    total = max(0, int(round(float(minutes))))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins:02d}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def format_hours(hours: float) -> str:
    return f"{max(0.0, float(hours)):.1f} h"


def format_seconds(total_seconds: float) -> str:
    seconds = max(0, int(round(total_seconds)))
    mins, secs = divmod(seconds, 60)
    return f"{mins:02d}:{secs:02d}"


def format_clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def normalize_domain(value: str) -> str:
    """Normalizza un dominio incollato dall'utente. Solleva ValueError se non valido."""
    raw = value.strip().lower()
    if not raw:
        raise ValueError("Dominio vuoto.")
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    else:
        raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if ":" in raw and not raw.startswith("["):
            raw = raw.rsplit(":", 1)[0]
    raw = raw.strip().strip(".")
    if raw.startswith("*."):
        raw = raw[2:]
    if not raw or raw in {"localhost", "localhost.localdomain"}:
        raise ValueError("Dominio non consentito.")
    try:
        ascii_domain = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Dominio non valido.") from exc
    if len(ascii_domain) > 253:
        raise ValueError("Dominio troppo lungo.")
    labels = ascii_domain.split(".")
    if len(labels) < 2:
        raise ValueError("Inserisci un dominio completo, ad esempio youtube.com.")
    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    if any(not label_re.match(label) for label in labels):
        raise ValueError("Formato dominio non valido.")
    if ascii_domain.startswith("www."):
        ascii_domain = ascii_domain[4:]
    return ascii_domain


def normalize_process_name(value: str) -> str:
    name = value.strip().strip('"').strip("'")
    if not name:
        raise ValueError("Nome processo vuoto.")
    name = Path(name).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if len(name) > 120 or "/" in name or "\\" in name:
        raise ValueError("Nome processo non valido.")
    return name


def safe_float(value: str | float | int, default: float = 0.0) -> float:
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return default
        return result
    except (TypeError, ValueError):
        return default


def safe_int(value: str | int, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
