"""Importazione file .ics (Google Calendar) con espansione delle ricorrenze."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..database import Database
from ..utils import LOCAL_DATETIME_FMT


LOGGER = logging.getLogger(__name__)
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_OCCURRENCES_PER_EVENT = 500


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_local_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raise TypeError(f"Tipo data non supportato: {type(value)!r}")


def _to_rrule_datetime(value: Any, tzinfo: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None and tzinfo is not None:
            return value.replace(tzinfo=tzinfo)
        return value
    return datetime.combine(value, time.min, tzinfo=tzinfo)


def _parse_categories(value: Any) -> str:
    for category in _as_list(value):
        cats = getattr(category, "cats", None)
        if cats:
            return str(cats[0])[:150]
        text = str(category).strip()
        if text:
            return text.split(",", 1)[0][:150]
    return ""


def parse_ics(path: str | Path, horizon_days: int = 365) -> tuple[list[dict[str, Any]], list[str]]:
    """Legge un file ICS e restituisce eventi normalizzati e warning."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File ICS non trovato: {file_path}")
    if file_path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Il file ICS supera il limite di 20 MB.")
    return parse_ics_bytes(file_path.read_bytes(), file_path.name, horizon_days)


def calendar_name(data: bytes) -> str:
    """Nome del calendario (``X-WR-CALNAME``) se presente nel contenuto ICS."""
    from icalendar import Calendar

    try:
        calendar = Calendar.from_ical(data)
    except Exception:
        return ""
    return str(calendar.get("x-wr-calname") or "").strip()[:150]


def parse_ics_bytes(
    data: bytes,
    label: str = "calendario",
    horizon_days: int = 365,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Come :func:`parse_ics`, ma da contenuto gia' in memoria (es. URL iCal)."""
    from icalendar import Calendar

    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Il calendario ICS supera il limite di 20 MB.")
    calendar = Calendar.from_ical(data)
    now = datetime.now()
    import_start = now - timedelta(days=180)
    recurrence_start = now - timedelta(days=31)
    import_end = now + timedelta(days=max(7, min(730, int(horizon_days))))
    recurrence_end = now + timedelta(days=max(7, min(730, int(horizon_days))))
    events: list[dict[str, Any]] = []
    warnings: list[str] = []

    for component in calendar.walk("VEVENT"):
        try:
            dtstart_raw = component.decoded("dtstart")
        except (KeyError, ValueError):
            warnings.append("Evento senza DTSTART ignorato.")
            continue
        try:
            dtend_raw = component.decoded("dtend")
        except (KeyError, ValueError):
            dtend_raw = None
        try:
            duration_raw = component.decoded("duration")
        except (KeyError, ValueError):
            duration_raw = None

        all_day = not isinstance(dtstart_raw, datetime)
        start_local = _to_local_datetime(dtstart_raw)
        if dtend_raw is not None:
            end_local = _to_local_datetime(dtend_raw)
        elif isinstance(duration_raw, timedelta):
            end_local = start_local + duration_raw
        else:
            end_local = start_local + timedelta(days=1 if all_day else 0, hours=0 if all_day else 1)
        if end_local <= start_local:
            end_local = start_local + timedelta(days=1 if all_day else 0, hours=0 if all_day else 1)
        base_duration = end_local - start_local

        title = str(component.get("summary") or "Evento senza titolo").strip()[:500]
        location = str(component.get("location") or "").strip()[:500]
        description = str(component.get("description") or "").strip()
        subject = _parse_categories(component.get("categories")) or title[:150]
        uid = str(component.get("uid") or "").strip()
        if not uid:
            digest = hashlib.sha1(
                f"{title}|{start_local.isoformat()}|{location}".encode("utf-8")
            ).hexdigest()[:24]
            uid = f"studyflow-{digest}"

        rrule_prop = component.get("rrule")
        occurrences: list[datetime] = []
        if rrule_prop is not None:
            try:
                from dateutil.rrule import rruleset, rrulestr

                if isinstance(rrule_prop, list):
                    rrule_prop = rrule_prop[0]
                rule_text = rrule_prop.to_ical().decode("utf-8")
                source_tz = getattr(dtstart_raw, "tzinfo", None)
                recurrence_start_dt = _to_rrule_datetime(dtstart_raw, source_tz)
                rule = rrulestr(rule_text, dtstart=recurrence_start_dt)
                rset = rruleset()
                rset.rrule(rule)
                for exdate in _as_list(component.get("exdate")):
                    for item in getattr(exdate, "dts", []):
                        rset.exdate(_to_rrule_datetime(item.dt, source_tz))
                for rdate in _as_list(component.get("rdate")):
                    for item in getattr(rdate, "dts", []):
                        rset.rdate(_to_rrule_datetime(item.dt, source_tz))
                if source_tz is not None:
                    window_start = datetime.now(source_tz) - timedelta(days=31)
                    window_end = datetime.now(source_tz) + timedelta(
                        days=max(7, min(730, int(horizon_days)))
                    )
                else:
                    window_start = recurrence_start
                    window_end = recurrence_end
                occurrences = list(rset.between(window_start, window_end, inc=True))[
                    :_MAX_OCCURRENCES_PER_EVENT
                ]
            except Exception as exc:
                LOGGER.warning("Ricorrenza non espandibile per '%s': %s", title, exc)
                warnings.append(f"Ricorrenza non espandibile per '{title}': {exc}")
                occurrences = []
        else:
            if end_local >= import_start and start_local <= import_end:
                occurrences = [start_local]

        for occurrence in occurrences:
            occurrence_local = _to_local_datetime(occurrence)
            occurrence_end = occurrence_local + base_duration
            if occurrence_end < import_start or occurrence_local > import_end:
                continue
            events.append(
                {
                    "uid": uid,
                    "title": title,
                    "subject": subject,
                    "start": occurrence_local.strftime(LOCAL_DATETIME_FMT),
                    "end": occurrence_end.strftime(LOCAL_DATETIME_FMT),
                    "location": location,
                    "description": description,
                    "all_day": all_day,
                }
            )

    # Deduplica in memoria: RFC 5545 consente override multipli dello stesso UID.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        deduped[(event["uid"], event["start"])] = event
    LOGGER.info("ICS '%s': %d eventi importabili.", label, len(deduped))
    return list(deduped.values()), warnings


def import_ics_file(db: Database, path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    events, warnings = parse_ics(file_path)
    imported = db.import_calendar_events(events, source_file=file_path.name)
    return {
        "file": file_path.name,
        "parsed": len(events),
        "imported": imported,
        "warnings": warnings,
    }
