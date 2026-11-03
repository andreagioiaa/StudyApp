"""Riproduzione audio e generazione dei suoni di concentrazione.

Pygame viene importato solo quando serve, per non appesantire l'avvio. Il mixer
supporta MP3, OGG, WAV e FLAC; i download YouTube vengono convertiti in MP3 da
FFmpeg prima di essere salvati nella cartella ``sounds``.
"""

from __future__ import annotations

import array
import logging
import math
import os
import random
import threading
import warnings
import wave
from pathlib import Path
from typing import Any

from ..utils import clamp, safe_float


LOGGER = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".ogg", ".wav", ".flac"}
FOCUS_SOUNDS: tuple[tuple[str, str], ...] = (
    ("Rumore bianco.wav", "white"),
    ("Rumore marrone.wav", "brown"),
    ("Pioggia leggera.wav", "rain"),
    ("Onde morbide.wav", "waves"),
)
FOCUS_SOUND_NAMES = {name for name, _kind in FOCUS_SOUNDS}


def audio_backend_info() -> str:
    """Importa Pygame e restituisce la versione, per diagnostica e smoke test."""
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pygame
    return str(pygame.version.ver)


class AudioController:
    """Piccolo wrapper thread-safe intorno a ``pygame.mixer.music``."""

    def __init__(self, sounds_dir: Path):
        self.sounds_dir = Path(sounds_dir)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._mixer = None
        self._mixer_error = ""
        self._current = ""
        self._paused = False
        self._volume = 0.55
        self._loop = True
        self._source = ""

    # ------------------------------------------------------------------ lib
    def list_tracks(self) -> list[dict[str, Any]]:
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        tracks: list[dict[str, Any]] = []
        try:
            entries = list(self.sounds_dir.iterdir())
        except OSError:
            LOGGER.exception("Lettura della cartella audio non riuscita.")
            return tracks
        for path in entries:
            try:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue
                tracks.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "extension": path.suffix.lower().lstrip("."),
                        "size": path.stat().st_size,
                        "builtin": path.name in FOCUS_SOUND_NAMES,
                    }
                )
            except OSError:
                continue
        tracks.sort(key=lambda item: item["name"].lower())
        return tracks

    def track_path(self, track: str) -> Path | None:
        """Risolve un nome senza consentire percorsi esterni alla cartella."""
        name = str(track or "").strip()
        if not name or Path(name).name != name:
            return None
        path = self.sounds_dir / name
        if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            return None
        return path if path.is_file() else None

    def has_track(self, track: str) -> bool:
        return self.track_path(track) is not None

    # --------------------------------------------------------------- mixer
    def _ensure_mixer_locked(self) -> bool:
        if self._mixer is not None:
            return True
        if self._mixer_error:
            return False
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import pygame
        except Exception as exc:
            self._mixer_error = f"Pygame non disponibile: {exc}"
            LOGGER.exception("Importazione di pygame non riuscita.")
            return False
        try:
            pygame.mixer.init()
        except Exception as exc:
            self._mixer_error = f"Periferica audio non disponibile: {exc}"
            LOGGER.warning("Inizializzazione del mixer non riuscita: %s", exc)
            try:
                pygame.mixer.quit()
            except Exception:
                pass
            return False
        self._mixer = pygame.mixer
        try:
            self._mixer.music.set_volume(self._volume)
        except Exception:
            LOGGER.exception("Impostazione del volume iniziale non riuscita.")
        return True

    def retry_mixer(self) -> tuple[bool, str]:
        with self._lock:
            self._mixer_error = ""
            return self._ensure_mixer_locked(), self._mixer_error

    # ------------------------------------------------------------- controls
    def play(
        self,
        track: str,
        loop: bool | None = None,
        source: str = "manual",
    ) -> tuple[bool, str]:
        path = self.track_path(track)
        if path is None:
            return False, "Brano non trovato nella cartella sounds."
        with self._lock:
            if not self._ensure_mixer_locked():
                return False, self._mixer_error
            if loop is not None:
                self._loop = bool(loop)
            try:
                music = self._mixer.music
                music.stop()
                try:
                    music.unload()
                except Exception:
                    pass
                music.load(str(path))
                music.set_volume(self._volume)
                music.play(-1 if self._loop else 0)
            except Exception as exc:
                LOGGER.exception("Riproduzione di %s non riuscita.", path.name)
                self._current = ""
                self._paused = False
                self._source = ""
                return False, f"Impossibile riprodurre il file: {exc}"
            self._current = path.name
            self._paused = False
            self._source = source
        return True, ""

    def pause(self) -> bool:
        with self._lock:
            if self._mixer is None or not self._current or self._paused:
                return False
            try:
                self._mixer.music.pause()
            except Exception:
                LOGGER.exception("Pausa audio non riuscita.")
                return False
            self._paused = True
        return True

    def resume(self) -> bool:
        with self._lock:
            if self._mixer is None or not self._current or not self._paused:
                return False
            try:
                self._mixer.music.unpause()
            except Exception:
                LOGGER.exception("Ripresa audio non riuscita.")
                return False
            self._paused = False
        return True

    def stop(self) -> None:
        with self._lock:
            if self._mixer is not None:
                try:
                    self._mixer.music.stop()
                    try:
                        self._mixer.music.unload()
                    except Exception:
                        pass
                except Exception:
                    LOGGER.exception("Arresto audio non riuscito.")
            self._current = ""
            self._paused = False
            self._source = ""

    def set_volume(self, volume: float) -> float:
        value = clamp(safe_float(volume, 0.55), 0.0, 1.0)
        with self._lock:
            self._volume = value
            if self._mixer is not None:
                try:
                    self._mixer.music.set_volume(value)
                except Exception:
                    LOGGER.exception("Aggiornamento volume non riuscito.")
        return value

    def set_loop(self, loop: bool) -> None:
        with self._lock:
            self._loop = bool(loop)

    def status(self) -> dict[str, Any]:
        with self._lock:
            busy = False
            if self._mixer is not None and self._current:
                try:
                    busy = bool(self._mixer.music.get_busy())
                except Exception:
                    busy = False
            return {
                "available": self._mixer is not None,
                "error": self._mixer_error,
                "current": self._current,
                "playing": bool(self._current and busy and not self._paused),
                "paused": bool(self._current and self._paused),
                "volume": self._volume,
                "loop": self._loop,
                "source": self._source,
            }

    def shutdown(self) -> None:
        self.stop()
        with self._lock:
            if self._mixer is not None:
                try:
                    self._mixer.quit()
                except Exception:
                    LOGGER.exception("Chiusura del mixer non riuscita.")
                self._mixer = None


# ---------------------------------------------------------------- focus sounds
def _fade(sample: float, index: int, total: int, seconds: float, rate: int) -> float:
    fade_samples = max(1, int(seconds * rate))
    if index < fade_samples:
        return sample * (index / fade_samples)
    remaining = total - index - 1
    if remaining < fade_samples:
        return sample * (remaining / fade_samples)
    return sample


def generate_focus_sound(
    path: Path,
    kind: str,
    duration: float = 30.0,
    sample_rate: int = 22050,
) -> Path:
    """Genera un loop WAV mono deterministico per un suono ambientale."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, int(max(0.1, duration) * sample_rate))
    samples = array.array("h")
    brown = 0.0
    low = 0.0
    droplet = 0.0
    randomizer = random.Random(f"studyflow-{kind}")
    for index in range(total):
        white = randomizer.uniform(-1.0, 1.0)
        brown = max(-1.0, min(1.0, brown * 0.997 + white * 0.02))
        low = low * 0.95 + white * 0.05
        droplet *= 0.992
        if randomizer.random() < 0.0012:
            droplet = randomizer.uniform(0.25, 0.8)
        if kind == "white":
            value = white * 0.22
        elif kind == "brown":
            value = brown * 0.85
        elif kind == "rain":
            value = low * 0.28 + droplet * 0.35
        else:  # waves
            time = index / sample_rate
            modulation = 0.55 + 0.34 * math.sin(2.0 * math.pi * 0.075 * time)
            value = brown * 0.72 * modulation + low * 0.05
        value = _fade(value, index, total, 0.45, sample_rate)
        value = max(-1.0, min(1.0, value))
        samples.append(int(value * 32767))

    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(samples.tobytes())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def generate_focus_sounds(
    sounds_dir: Path,
    duration: float = 30.0,
    sample_rate: int = 22050,
) -> list[Path]:
    sounds_dir = Path(sounds_dir)
    sounds_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, kind in FOCUS_SOUNDS:
        path = sounds_dir / name
        if path.is_file():
            continue
        generate_focus_sound(path, kind, duration=duration, sample_rate=sample_rate)
        created.append(path)
    return created
