"""Download e conversione dell'audio da un video YouTube.

YouTube non espone un flusso audio-only ufficiale: yt-dlp scarica la traccia
migliore disponibile e FFmpeg la converte in MP3, cosi' il file puo' essere
riprodotto dal mixer locale e riascoltato anche senza connessione.

Il download e' consentito solo per contenuti di cui l'utente possiede i diritti
o che la licenza permette di scaricare.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..paths import app_data_dir, executable_path, is_frozen, project_root
from .audio_service import SUPPORTED_AUDIO_EXTENSIONS


LOGGER = logging.getLogger(__name__)
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class YouTubeAudioError(RuntimeError):
    """Errore generico di download o conversione."""


class FFmpegMissingError(YouTubeAudioError):
    """FFmpeg non e' disponibile."""


class YouTubeDuplicateError(YouTubeAudioError):
    """Un file con lo stesso nome esiste gia'."""

    def __init__(self, title: str, existing: Path):
        super().__init__(f"Esiste gia' un file per '{title}'.")
        self.title = title
        self.existing = existing


def is_youtube_url(url: str) -> bool:
    value = str(url or "").strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return False
    if host == "youtu.be":
        return len(parsed.path.strip("/")) > 0
    if parsed.path.rstrip("/") == "/watch":
        return bool(parsed.query)
    return any(
        parsed.path.startswith(prefix)
        for prefix in ("/watch/", "/shorts/", "/embed/", "/live/")
    )


def sanitize_filename(title: str, max_length: int = 120) -> str:
    value = unicodedata.normalize("NFKC", str(title or ""))
    value = _INVALID_FILENAME.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "Audio YouTube"
    if value.split(".", 1)[0].strip().upper() in _RESERVED_NAMES:
        value = f"_{value}"
    value = value[:max_length].rstrip(" .")
    return value or "Audio YouTube"


def find_existing_track(sounds_dir: Path, title: str) -> Path | None:
    """Cerca un file audio con lo stesso nome, indipendentemente dall'estensione."""
    folder = Path(sounds_dir)
    if not folder.is_dir():
        return None
    wanted = sanitize_filename(title).casefold()
    try:
        entries = list(folder.iterdir())
    except OSError:
        return None
    for path in entries:
        try:
            if (
                path.is_file()
                and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
                and path.stem.casefold() == wanted
            ):
                return path
        except OSError:
            continue
    return None


def copy_title(sounds_dir: Path, title: str, start: int = 2) -> str:
    """Restituisce un titolo libero del tipo 'Titolo (copia 2)'."""
    base = sanitize_filename(title)
    folder = Path(sounds_dir)
    existing_stems = set()
    try:
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                existing_stems.add(path.stem.casefold())
    except OSError:
        pass
    index = max(2, start)
    while f"{base} (copia {index})".casefold() in existing_stems:
        index += 1
    return f"{base} (copia {index})"


def _unique_destination(sounds_dir: Path, title: str) -> Path:
    base = sanitize_filename(title)
    candidate = Path(sounds_dir) / f"{base}.mp3"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = Path(sounds_dir) / f"{base} (copia {index}).mp3"
        if not candidate.exists():
            return candidate
        index += 1


def find_ffmpeg(sounds_dir: Path) -> Path | None:
    """Cerca FFmpeg nel PATH, nella cartella dati o accanto all'eseguibile."""
    candidates: list[Path] = []
    configured = os.environ.get("STUDYFLOW_FFMPEG", "").strip()
    if configured:
        path = Path(configured).expanduser()
        candidates.append(path / "ffmpeg.exe" if path.is_dir() else path)

    app_folder = Path(sounds_dir).parent
    data_folder = app_data_dir()
    candidates.extend(
        (
            app_folder / "ffmpeg.exe",
            app_folder / "ffmpeg" / "ffmpeg.exe",
            data_folder / "ffmpeg.exe",
            data_folder / "ffmpeg" / "ffmpeg.exe",
        )
    )
    if is_frozen():
        candidates.append(executable_path().parent / "ffmpeg.exe")
    else:
        candidates.append(project_root() / "ffmpeg.exe")

    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


class _YtDlpLogger:
    """Adapter minimale: evita scritture su una console inesistente nell'exe."""

    def debug(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        LOGGER.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        LOGGER.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        LOGGER.error("yt-dlp: %s", message)


def _import_ytdlp():
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise YouTubeAudioError(
            "yt-dlp non e' disponibile. Reinstalla le dipendenze del progetto."
        ) from exc
    return YoutubeDL


def youtube_backend_info() -> str:
    """Verifica che yt-dlp e l'estrattore YouTube siano inclusi nella build."""
    _import_ytdlp()
    try:
        import yt_dlp.extractor.youtube as youtube_extractor
        import yt_dlp_ejs  # noqa: F401
        from yt_dlp.version import __version__ as yt_version
    except Exception as exc:
        raise YouTubeAudioError(
            "L'estrattore YouTube o il solver EJS non sono inclusi nella build."
        ) from exc
    if not hasattr(youtube_extractor, "YoutubeIE"):
        raise YouTubeAudioError("Estrattore YouTube non caricabile.")
    return str(yt_version)


def _js_runtimes() -> dict[str, dict[str, str]]:
    """Abilita il primo runtime JavaScript disponibile per YouTube."""
    runtimes: dict[str, dict[str, str]] = {}
    for name, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("bun", "bun"),
        ("quickjs", "qjs"),
    ):
        found = shutil.which(executable)
        if found:
            runtimes[name] = {"path": str(Path(found))}
    return runtimes


def _base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(),
        "socket_timeout": 30,
        "js_runtimes": _js_runtimes(),
    }


def probe_video(url: str) -> dict[str, Any]:
    if not is_youtube_url(url):
        raise YouTubeAudioError("Inserisci un link YouTube valido.")
    YoutubeDL = _import_ytdlp()
    options = _base_options()
    options.update({"skip_download": True})
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as exc:
        message = str(exc).strip() or "Impossibile leggere il video."
        if "Private video" in message or "unavailable" in message.lower():
            raise YouTubeAudioError(
                "Il video non e' disponibile pubblicamente o richiede l'accesso."
            ) from exc
        raise YouTubeAudioError(message) from exc
    if not isinstance(info, dict):
        raise YouTubeAudioError("YouTube non ha restituito i dati del video.")
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries:
            info = entries[0]
    title = str(info.get("title") or "").strip()
    if not title:
        raise YouTubeAudioError("Titolo del video non disponibile.")
    return {
        "id": str(info.get("id") or ""),
        "title": title,
        "duration": int(info.get("duration") or 0),
        "uploader": str(info.get("uploader") or ""),
        "webpage_url": str(info.get("webpage_url") or url),
    }


def download_audio(
    url: str,
    sounds_dir: Path,
    title: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Scarica la traccia migliore e la salva come MP3 con il titolo indicato."""
    if not is_youtube_url(url):
        raise YouTubeAudioError("Inserisci un link YouTube valido.")
    ffmpeg = find_ffmpeg(sounds_dir)
    if ffmpeg is None:
        raise FFmpegMissingError(
            "FFmpeg non trovato. Installalo con 'winget install Gyan.FFmpeg' "
            "oppure copia ffmpeg.exe nella cartella dati di StudyFlow."
        )
    YoutubeDL = _import_ytdlp()
    sounds_dir = Path(sounds_dir)
    sounds_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title)

    last_report = {"time": 0.0}

    def report(data: dict[str, Any]) -> None:
        if progress is None:
            return
        now = time.monotonic()
        status = data.get("status")
        if status == "downloading" and now - last_report["time"] < 0.2:
            return
        last_report["time"] = now
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        percent = float(downloaded / total) if total else 0.0
        if status == "finished":
            percent = 0.98
        progress(
            {
                "status": status or "",
                "percent": max(0.0, min(1.0, percent)),
                "speed": float(data.get("speed") or 0.0),
                "eta": int(data.get("eta") or 0),
            }
        )

    with tempfile.TemporaryDirectory(prefix=".download-", dir=str(sounds_dir)) as folder:
        temp = Path(folder)
        options = _base_options()
        options.update(
            {
                "format": "bestaudio/best",
                "outtmpl": str(temp / "%(id)s.%(ext)s"),
                "ffmpeg_location": str(ffmpeg),
                "progress_hooks": [report],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                        "nopostoverwrites": False,
                    }
                ],
            }
        )
        try:
            with YoutubeDL(options) as downloader:
                downloader.extract_info(url, download=True)
        except Exception as exc:
            message = str(exc).strip() or "Download non riuscito."
            if "ffmpeg" in message.lower():
                raise FFmpegMissingError(
                    "FFmpeg non e' disponibile o non funziona correttamente."
                ) from exc
            raise YouTubeAudioError(message) from exc

        produced = sorted(temp.glob("*.mp3"))
        if not produced:
            raise YouTubeAudioError(
                "Conversione MP3 non riuscita: FFmpeg potrebbe non essere completo."
            )
        destination = _unique_destination(sounds_dir, safe_title)
        try:
            shutil.move(str(produced[0]), str(destination))
        except OSError as exc:
            raise YouTubeAudioError(
                f"Impossibile salvare il file nella cartella sounds: {exc}"
            ) from exc
    if progress is not None:
        progress({"status": "finished", "percent": 1.0, "speed": 0.0, "eta": 0})
    LOGGER.info("Audio YouTube salvato: %s", destination)
    return destination
