"""Pianificazione della ripetizione spaziata per le flashcard.

Il motore implementa i passaggi di apprendimento in stile Anki sopra la curva
SM-2 di SuperMemo: ogni risposta modifica intervallo, fattore di facilita' e
stato della scheda. I quattro giudizi sono ``Ancora``, ``Difficile``, ``Bene``
e ``Facile``.

Regole principali:

- una scheda nuova passa da passi di 1 e 10 minuti;
- ``Bene`` al termine dei passi la fa entrare in ripasso dopo 1 giorno;
- ``Facile`` la fa entrare subito in ripasso dopo 4 giorni;
- in ripasso l'intervallo viene moltiplicato per il fattore di facilita';
- ``Ancora`` registra un lapse, riduce la facilita' e riporta la scheda in
  riapprendimento;
- il fattore di facilita' resta sempre tra 1.3 e 5.0 e l'intervallo massimo e'
  di 100 anni.

Le date sono memorizzate come testo locale ``YYYY-MM-DD HH:MM:SS`` per restare
coerenti con il resto del database SQLite di StudyFlow.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..utils import LOCAL_DATETIME_FMT, now_local, parse_datetime, safe_float, safe_int


DEFAULT_EASE_FACTOR = 2.5
MIN_EASE_FACTOR = 1.3
MAX_EASE_FACTOR = 5.0
LEARNING_STEPS_MINUTES: tuple[int, ...] = (1, 10)
RELEARNING_STEPS_MINUTES: tuple[int, ...] = (10,)
GRADUATING_INTERVAL_DAYS = 1.0
EASY_INTERVAL_DAYS = 4.0
MAX_INTERVAL_DAYS = 36500.0
LAPSE_INTERVAL_FACTOR = 0.20

RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4
RATINGS = (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY)
RATING_LABELS = {
    RATING_AGAIN: "Ancora",
    RATING_HARD: "Difficile",
    RATING_GOOD: "Bene",
    RATING_EASY: "Facile",
}
RATING_HINTS = {
    RATING_AGAIN: "Non la ricordavo",
    RATING_HARD: "Con difficolta'",
    RATING_GOOD: "Ricordata",
    RATING_EASY: "Molto facile",
}
STATE_LABELS = {
    "new": "Nuova",
    "learning": "Apprendimento",
    "review": "Ripasso",
    "relearning": "Riapprendimento",
}


def _format_datetime(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(LOCAL_DATETIME_FMT)


def _round_days(value: float) -> float:
    """Arrotonda a giorni interi con arrotondamento commerciale."""
    return float(max(1, int(max(0.0, value) + 0.5)))


def _clamp_ease(value: float) -> float:
    return round(max(MIN_EASE_FACTOR, min(MAX_EASE_FACTOR, value)), 2)


def _clamp_interval(value: float) -> float:
    return float(max(1.0, min(MAX_INTERVAL_DAYS, value)))


def _schedule_minutes(now: datetime, minutes: int) -> tuple[str, float]:
    return _format_datetime(now + timedelta(minutes=minutes)), minutes / 1440.0


def _schedule_days(now: datetime, days: float) -> tuple[str, float]:
    return _format_datetime(now + timedelta(days=days)), days


def schedule_card(
    card: dict[str, Any],
    rating: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Calcola il nuovo stato della scheda senza modificare il database."""
    if int(rating) not in RATINGS:
        raise ValueError("Giudizio non valido: usare 1, 2, 3 oppure 4.")
    moment = (now or now_local()).replace(microsecond=0)
    state = str(card.get("state") or "new")
    interval = max(0.0, safe_float(card.get("interval_days", 0)))
    ease = _clamp_ease(safe_float(card.get("ease_factor", DEFAULT_EASE_FACTOR)))
    repetitions = max(0, safe_int(card.get("repetitions", 0)))
    lapses = max(0, safe_int(card.get("lapses", 0)))
    step = max(0, safe_int(card.get("learning_step", 0)))
    previous_review = parse_datetime(str(card.get("last_reviewed_at") or ""))
    elapsed_seconds = (
        max(0, int((moment - previous_review).total_seconds()))
        if previous_review is not None
        else 0
    )

    if state in {"new", "learning"}:
        steps = LEARNING_STEPS_MINUTES
        if int(rating) == RATING_AGAIN:
            state = "learning"
            step = 0
            due_at, scheduled_days = _schedule_minutes(moment, steps[0])
            interval = scheduled_days
        elif int(rating) == RATING_HARD:
            state = "learning"
            step = min(step, len(steps) - 1)
            due_at, scheduled_days = _schedule_minutes(moment, steps[step])
            interval = scheduled_days
        elif int(rating) == RATING_GOOD:
            next_step = step + 1
            if next_step < len(steps):
                state = "learning"
                step = next_step
                due_at, scheduled_days = _schedule_minutes(moment, steps[next_step])
                interval = scheduled_days
            else:
                state = "review"
                step = 0
                repetitions += 1
                interval = GRADUATING_INTERVAL_DAYS
                due_at, scheduled_days = _schedule_days(moment, interval)
        else:
            state = "review"
            step = 0
            repetitions += 1
            interval = EASY_INTERVAL_DAYS
            due_at, scheduled_days = _schedule_days(moment, interval)

    elif state == "review":
        if int(rating) == RATING_AGAIN:
            state = "relearning"
            step = 0
            lapses += 1
            ease = _clamp_ease(ease - 0.20)
            interval = _round_days(max(1.0, interval * LAPSE_INTERVAL_FACTOR))
            due_at, scheduled_days = _schedule_minutes(
                moment, RELEARNING_STEPS_MINUTES[0]
            )
        elif int(rating) == RATING_HARD:
            step = 0
            repetitions += 1
            ease = _clamp_ease(ease - 0.15)
            interval = _clamp_interval(_round_days(interval * 1.2))
            due_at, scheduled_days = _schedule_days(moment, interval)
        elif int(rating) == RATING_GOOD:
            step = 0
            repetitions += 1
            interval = _clamp_interval(_round_days(interval * ease))
            due_at, scheduled_days = _schedule_days(moment, interval)
        else:
            step = 0
            repetitions += 1
            ease = _clamp_ease(ease + 0.15)
            interval = _clamp_interval(_round_days(interval * ease * 1.3))
            due_at, scheduled_days = _schedule_days(moment, interval)

    else:
        # Stato sconosciuto o riapprendimento: si riparte in modo sicuro.
        steps = RELEARNING_STEPS_MINUTES
        if int(rating) in {RATING_AGAIN, RATING_HARD}:
            state = "relearning"
            step = 0
            due_at, scheduled_days = _schedule_minutes(moment, steps[0])
            interval = max(1.0, interval)
        elif int(rating) == RATING_GOOD:
            state = "review"
            step = 0
            repetitions += 1
            interval = _clamp_interval(max(1.0, interval))
            due_at, scheduled_days = _schedule_days(moment, interval)
        else:
            state = "review"
            step = 0
            repetitions += 1
            interval = _clamp_interval(max(2.0, interval * 1.3))
            due_at, scheduled_days = _schedule_days(moment, interval)

    return {
        "state": state,
        "due_at": due_at,
        "interval_days": round(interval, 4),
        "ease_factor": ease,
        "repetitions": repetitions,
        "lapses": lapses,
        "learning_step": step,
        "last_reviewed_at": _format_datetime(moment),
        "scheduled_days": round(scheduled_days, 6),
        "elapsed_seconds": elapsed_seconds,
    }


def preview_schedule(
    card: dict[str, Any],
    rating: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Alias esplicito usato dalla UI per mostrare l'anteprima dei giudizi."""
    return schedule_card(card, rating, now)


def format_interval_days(days: float) -> str:
    value = max(0.0, safe_float(days))
    if value < 1.0:
        minutes = max(1, int(round(value * 1440.0)))
        if minutes < 60:
            return f"{minutes} min"
        hours = max(1, int(round(minutes / 60.0)))
        return f"{hours} h"
    if value < 30.0:
        return f"{int(round(value))} g"
    if value < 365.0:
        months = value / 30.4375
        return f"{months:.1f} mesi".replace(".", ",")
    years = value / 365.25
    return f"{years:.1f} anni".replace(".", ",")


def format_due_label(due_at: str, now: datetime | None = None) -> str:
    due = parse_datetime(due_at)
    if due is None:
        return "adesso"
    moment = (now or now_local()).replace(microsecond=0)
    delta = due - moment
    seconds = delta.total_seconds()
    if seconds <= 60:
        return "adesso"
    if seconds < 3600:
        return f"tra {max(1, int(round(seconds / 60.0)))} min"
    if seconds < 86400:
        return f"tra {max(1, int(round(seconds / 3600.0)))} h"
    if seconds < 172800:
        return "domani"
    return due.strftime("%d/%m/%Y")


def state_label(state: str) -> str:
    return STATE_LABELS.get(str(state), "Sconosciuto")


def is_due(card: dict[str, Any], now: datetime | None = None) -> bool:
    due = parse_datetime(str(card.get("due_at") or ""))
    if due is None:
        return True
    return due <= (now or now_local()).replace(microsecond=0)
