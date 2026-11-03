"""Formattazione date e testi in italiano, indipendente dal locale di Windows."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..utils import parse_datetime


MONTHS = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
)
MONTHS_SHORT = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
WEEKDAYS = (
    "lunedi'",
    "martedi'",
    "mercoledi'",
    "giovedi'",
    "venerdi'",
    "sabato",
    "domenica",
)
WEEKDAYS_SHORT = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")


def format_day(value: date) -> str:
    return f"{WEEKDAYS[value.weekday()]} {value.day} {MONTHS[value.month - 1]} {value.year}"


def format_day_short(value: date) -> str:
    return f"{WEEKDAYS_SHORT[value.weekday()]} {value.day} {MONTHS_SHORT[value.month - 1]}"


def format_event_time(start: str, end: str, all_day: bool = False) -> str:
    start_dt = parse_datetime(start)
    end_dt = parse_datetime(end)
    if all_day:
        if start_dt and end_dt:
            days = max(1, (end_dt.date() - start_dt.date()).days)
            return "Tutto il giorno" if days <= 1 else f"Tutto il giorno · {days} giorni"
        return "Tutto il giorno"
    if not start_dt or not end_dt:
        return ""
    if start_dt.date() == end_dt.date():
        return f"{start_dt:%H:%M} - {end_dt:%H:%M}"
    return f"{start_dt:%d/%m %H:%M} - {end_dt:%d/%m %H:%M}"


def format_event_datetime(start: str) -> str:
    value = parse_datetime(start)
    if not value:
        return start
    return f"{format_day_short(value.date())} · {value:%H:%M}"


def format_week_range(start: date, end_exclusive: date) -> str:
    last = end_exclusive - timedelta(days=1)
    if start.month == last.month:
        return f"{start.day} - {last.day} {MONTHS[start.month - 1]} {start.year}"
    return (
        f"{start.day} {MONTHS_SHORT[start.month - 1]} - "
        f"{last.day} {MONTHS_SHORT[last.month - 1]} {last.year}"
    )
