"""Costanti, default e palette dell'applicazione."""

from __future__ import annotations

from dataclasses import dataclass


APP_NAME = "StudyFlow"
APP_TITLE = "StudyFlow - Gestionale Studio & Analytics"
APP_VERSION = "1.4.0"

DEFAULT_SETTINGS: dict[str, str] = {
    "pomodoro_work": "25",
    "pomodoro_short": "5",
    "pomodoro_long": "15",
    "pomodoro_cycles": "4",
    "auto_start_next": "1",
    "notification_sound": "1",
    "block_sites_enabled": "1",
    "block_apps_enabled": "1",
    "terminate_blocked_apps": "1",
    "web_trap_enabled": "1",
    "app_scan_interval": "2.0",
    "audio_autoplay": "0",
    "audio_track": "",
    "audio_volume": "0.55",
    "audio_loop": "1",
    "transcription_model": "base-q5_1",
    "transcription_language": "it",
}

DEFAULT_BLOCKED_SITES: tuple[str, ...] = (
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "reddit.com",
    "netflix.com",
    "twitch.tv",
)

DEFAULT_BLOCKED_APPS: tuple[tuple[str, str], ...] = (
    ("Discord", "discord"),
    ("Steam", "steam"),
    ("Telegram", "telegram"),
    ("WhatsApp", "whatsapp"),
    ("Epic Games Launcher", "epicgameslauncher"),
    ("TikTok", "tiktok"),
)

HOSTS_BEGIN_MARKER = "# >>> STUDYFLOW MANAGED BLOCK - DO NOT EDIT >>>"
HOSTS_END_MARKER = "# <<< STUDYFLOW MANAGED BLOCK <<<"
HOSTS_ENTRY_IP = "127.0.0.1"

TRAP_PORTS: tuple[int, ...] = (80, 443)
PROCESS_LOG_COOLDOWN_SECONDS = 45.0
WEB_LOG_COOLDOWN_SECONDS = 10.0


@dataclass(frozen=True)
class Palette:
    bg: str = "#0B1120"
    sidebar: str = "#0F172A"
    surface: str = "#111C31"
    surface_alt: str = "#16233A"
    surface_hover: str = "#1D2D48"
    border: str = "#24344F"
    text: str = "#F1F5F9"
    text_muted: str = "#94A3B8"
    text_faint: str = "#64748B"
    accent: str = "#22D3A7"
    accent_hover: str = "#16B98F"
    accent_dark: str = "#0F766E"
    blue: str = "#38BDF8"
    amber: str = "#FBBF24"
    red: str = "#F87171"
    red_dark: str = "#7F1D1D"
    green: str = "#4ADE80"
    purple: str = "#A78BFA"
    chart_grid: str = "#26364F"


COLORS = Palette()

PHASE_LABELS = {
    "work": "Lavoro",
    "short_break": "Pausa breve",
    "long_break": "Pausa lunga",
}

STATUS_LABELS = {
    "idle": "Pronto",
    "running": "In corso",
    "paused": "In pausa",
}

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"


def quality_score(stats: dict) -> float:
    """Quality Score 0-100.

    Componenti:
      - 35% completamento dei pomodori avviati;
      - 40% pomodori completati senza alcuna distrazione;
      - 25% attenzione, penalizzata dalle interruzioni per ora di studio.

    La formula premia studio effettivo e sessioni pulite; non dipende da dati
    inventati e restituisce 0 quando non esiste storico nel periodo.
    """
    started = int(stats.get("started", 0) or 0)
    completed = int(stats.get("completed", 0) or 0)
    clean = int(stats.get("clean_completed", 0) or 0)
    minutes = int(stats.get("minutes", 0) or 0)
    attempts = int(stats.get("attempts", 0) or 0)
    if started <= 0:
        return 0.0

    completion_rate = min(1.0, completed / started)
    clean_rate = (clean / completed) if completed else 0.0
    hours = max(minutes / 60.0, 0.25)
    interruptions_per_hour = attempts / hours
    attention = max(0.0, 100.0 - interruptions_per_hour * 9.0)
    score = (
        completion_rate * 35.0
        + clean_rate * 40.0
        + (attention / 100.0) * 25.0
    )
    return round(max(0.0, min(100.0, score)), 1)
