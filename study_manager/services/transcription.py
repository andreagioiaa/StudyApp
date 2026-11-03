"""Estrazione audio e trascrizione locale delle videolezioni.

Il motore usa ``whisper.cpp`` tramite il binding leggero ``pywhispercpp`` e un
modello quantizzato scaricato una sola volta nella cartella dati. FFmpeg
converte il video in WAV mono 16 kHz, formato richiesto da whisper.cpp.

La trascrizione viene eseguita in un processo separato: il binding nativo non
deve bloccare il thread grafico di Tkinter. Il processo padre riceve avanamento
e risultato tramite una coda e aggiorna la UI solo con ``app.post``.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..paths import app_data_dir
from ..utils import atomic_write_text, format_clock, now_str
from .youtube_audio import find_ffmpeg, sanitize_filename


LOGGER = logging.getLogger(__name__)

SUPPORTED_VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogv",
    ".ts",
    ".webm",
    ".wmv",
}

MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "base-q5_1": {
        "label": "Base quantizzato (circa 60 MB)",
        "filename": "ggml-base-q5_1.bin",
        "bytes": 59_707_625,
    },
    "small-q5_1": {
        "label": "Small quantizzato (circa 190 MB)",
        "filename": "ggml-small-q5_1.bin",
        "bytes": 190_085_487,
    },
}
DEFAULT_MODEL = "base-q5_1"
DEFAULT_LANGUAGE = "it"
LANGUAGE_CHOICES: dict[str, str] = {
    "Italiano": "it",
    "Inglese": "en",
    "Rilevamento automatico": "",
}
LANGUAGE_LABELS = {value: label for label, value in LANGUAGE_CHOICES.items()}
MODEL_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"


class TranscriptionError(RuntimeError):
    """Errore gestibile mostrato all'utente."""


class FFmpegMissingError(TranscriptionError):
    """FFmpeg non e' disponibile o non e' utilizzabile."""


class TranscriptionCancelled(TranscriptionError):
    """La trascrizione e' stata annullata dall'utente."""


@dataclass(frozen=True)
class TranscriptBlock:
    start: float
    end: float
    text: str


def _cancel_requested(cancel: Any) -> bool:
    try:
        return bool(cancel is not None and cancel.is_set())
    except Exception:
        return False


def _no_window_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def format_timestamp(seconds: float) -> str:
    """Timestamp ``[mm:ss]``; oltre un'ora i minuti continuano (es. 75:12)."""
    total = max(0, int(float(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def transcript_filename(value: str, fallback: str = "Trascrizione") -> str:
    """Normalizza il nome scelto dall'utente e aggiunge l'estensione .txt."""
    raw = str(value or "").strip()
    if raw.lower().endswith(".txt"):
        raw = raw[:-4].strip()
    safe = sanitize_filename(raw or fallback, max_length=120)
    return f"{safe}.txt"


def default_transcript_name(video_path: str | Path) -> str:
    return transcript_filename(Path(video_path).stem, "Trascrizione")


def rename_transcript_file(
    current_path: str | Path,
    new_name: str,
    transcripts_dir: str | Path,
) -> Path:
    """Rinomina il .txt nella cartella trascrizioni senza sovrascrivere file."""
    source = Path(current_path)
    if not source.is_file():
        raise TranscriptionError("Il file di trascrizione non esiste piu'.")
    target = Path(transcripts_dir) / transcript_filename(
        new_name, source.stem or "Trascrizione"
    )
    if target.resolve() == source.resolve():
        return target
    if target.exists():
        raise TranscriptionError(
            f"Esiste gia' un file chiamato '{target.name}'. Scegli un altro nome."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(target))
    except OSError as exc:
        raise TranscriptionError(f"Impossibile rinominare il file: {exc}") from exc
    return target


def model_label(model: str) -> str:
    return str(MODEL_CATALOG.get(model, {}).get("label") or model)


def model_filename(model: str) -> str:
    if model not in MODEL_CATALOG:
        raise TranscriptionError(f"Modello di trascrizione sconosciuto: {model}")
    return str(MODEL_CATALOG[model]["filename"])


def model_path(models_dir: str | Path, model: str) -> Path:
    return Path(models_dir) / model_filename(model)


def model_is_available(models_dir: str | Path, model: str) -> bool:
    try:
        target = model_path(models_dir, model)
        expected = int(MODEL_CATALOG[model]["bytes"])
        return target.is_file() and target.stat().st_size >= expected * 0.9
    except (KeyError, OSError):
        return False


def transcription_backend_info() -> str:
    """Carica il binding nativo: usato dallo smoke test per verificare la build."""
    try:
        from importlib.metadata import version

        package_version = version("pywhispercpp")
    except Exception:
        package_version = "incluso"
    try:
        import _pywhispercpp  # noqa: F401
        import pywhispercpp  # noqa: F401
    except Exception as exc:
        raise TranscriptionError(
            f"Il motore whisper.cpp non e' disponibile nella build: {exc}"
        ) from exc
    return f"pywhispercpp {package_version} / whisper.cpp (CPU)"


def _ffprobe_path(ffmpeg: str | Path) -> Path | None:
    candidate = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        pass
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def probe_media_duration(ffmpeg: str | Path, media: str | Path) -> float:
    """Legge la durata con ffprobe e, se assente, dal messaggio di FFmpeg."""
    probe = _ffprobe_path(ffmpeg)
    if probe is not None:
        try:
            completed = subprocess.run(
                [
                    str(probe),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                creationflags=_no_window_flags(),
            )
            value = float(completed.stdout.strip().splitlines()[-1])
            if value > 0:
                return value
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(media)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=_no_window_flags(),
        )
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:[.,]\d+)?)",
            completed.stderr or "",
        )
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds.replace(",", "."))
    except (OSError, subprocess.SubprocessError):
        pass
    return 0.0


def wav_duration(path: str | Path) -> float:
    try:
        with wave.open(str(path), "rb") as source:
            rate = source.getframerate()
            frames = source.getnframes()
            return float(frames / rate) if rate else 0.0
    except (OSError, wave.Error):
        return 0.0


def build_extract_command(
    ffmpeg: str | Path,
    video_path: str | Path,
    output_wav: str | Path,
) -> list[str]:
    """Comando FFmpeg: WAV PCM 16 bit, mono, 16 kHz, senza traccia video."""
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]


def _parse_progress_seconds(line: str) -> float | None:
    key, separator, value = line.strip().partition("=")
    if not separator:
        return None
    if key in {"out_time_us", "out_time_ms"}:
        try:
            return max(0.0, int(value) / 1_000_000.0)
        except ValueError:
            return None
    if key == "out_time":
        match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def extract_audio(
    ffmpeg: str | Path,
    video_path: str | Path,
    output_wav: str | Path,
    duration_hint: float = 0.0,
    progress: Callable[[float], None] | None = None,
    cancel: Any = None,
) -> Path:
    """Estrae l'audio in WAV mono 16 kHz e riporta l'avanzamento da FFmpeg."""
    output = Path(output_wav)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_extract_command(ffmpeg, video_path, output)
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace"
    ) as error_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=error_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_no_window_flags(),
            )
        except OSError as exc:
            raise FFmpegMissingError(f"Impossibile avviare FFmpeg: {exc}") from exc

        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader_thread = threading.Thread(
            target=reader, name="StudyFlow-FFmpegOutput", daemon=True
        )
        reader_thread.start()
        last_percent = 0.0
        try:
            while True:
                if _cancel_requested(cancel):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise TranscriptionCancelled("Estrazione audio annullata.")
                try:
                    line = lines.get(timeout=0.12)
                except queue.Empty:
                    if process.poll() is not None:
                        try:
                            line = lines.get(timeout=0.5)
                        except queue.Empty:
                            break
                    else:
                        continue
                if line is None:
                    break
                seconds = _parse_progress_seconds(line)
                if seconds is None or duration_hint <= 0 or progress is None:
                    continue
                percent = max(0.0, min(0.995, seconds / duration_hint))
                if percent - last_percent >= 0.005:
                    last_percent = percent
                    try:
                        progress(percent)
                    except Exception:
                        LOGGER.exception("Aggiornamento avanzamento estrazione non riuscito.")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TranscriptionError("FFmpeg non ha completato l'estrazione audio.")
        finally:
            if process.poll() is None:
                process.terminate()
            error_file.seek(0)
            details = error_file.read().strip()
        if process.returncode != 0:
            message = details[-700:] if details else "errore FFmpeg sconosciuto"
            raise TranscriptionError(
                "FFmpeg non e' riuscito a estrarre l'audio dal video: " + message
            )
        if not output.is_file() or output.stat().st_size <= 44:
            raise TranscriptionError(
                "L'estrazione audio non ha prodotto un file WAV valido. "
                "Verifica che il video contenga una traccia audio."
            )
        if progress is not None:
            try:
                progress(1.0)
            except Exception:
                LOGGER.exception("Aggiornamento finale estrazione non riuscito.")
    return output


def ensure_model(
    models_dir: str | Path,
    model: str = DEFAULT_MODEL,
    progress: Callable[[float, int, int], None] | None = None,
    cancel: Any = None,
) -> Path:
    """Scarica il modello GGML una sola volta nella cartella dati."""
    target = model_path(models_dir, model)
    expected = int(MODEL_CATALOG[model]["bytes"])
    if target.is_file() and target.stat().st_size >= expected * 0.9:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        import requests
    except Exception as exc:
        raise TranscriptionError(
            "Il componente per il download del modello non e' disponibile."
        ) from exc
    url = f"{MODEL_BASE_URL}/{target.name}"
    received = 0
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(20, 120),
            headers={"User-Agent": "StudyFlow/1.2 (desktop transcription)"},
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or expected)
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if _cancel_requested(cancel):
                        raise TranscriptionCancelled("Download del modello annullato.")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        try:
                            progress(
                                max(0.0, min(1.0, received / total if total else 0.0)),
                                received,
                                total,
                            )
                        except Exception:
                            LOGGER.exception("Avanzamento download modello non riuscito.")
                handle.flush()
                os.fsync(handle.fileno())
        if received <= 0:
            raise TranscriptionError("Il download del modello ha restituito un file vuoto.")
        if expected and received < expected * 0.9:
            raise TranscriptionError(
                "Il modello scaricato e' incompleto. Controlla la connessione e riprova."
            )
        os.replace(temporary, target)
    except TranscriptionCancelled:
        raise
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, TranscriptionError):
            raise
        raise TranscriptionError(
            "Download del modello di trascrizione non riuscito. "
            "Verifica la connessione Internet e riprova. "
            f"Dettaglio: {exc}"
        ) from exc
    if progress is not None:
        try:
            progress(1.0, received, received)
        except Exception:
            LOGGER.exception("Avanzamento finale modello non riuscito.")
    LOGGER.info("Modello whisper scaricato: %s", target)
    return target


def build_transcript_text(
    title: str,
    video_path: str | Path,
    subject: str,
    duration_seconds: float,
    model: str,
    language: str,
    blocks: list[TranscriptBlock],
    created_at: str | None = None,
) -> str:
    video_name = Path(video_path).name
    language_text = LANGUAGE_LABELS.get(language, language or "Rilevamento automatico")
    header = [
        "# StudyFlow - Trascrizione videolezione",
        f"# Titolo: {title.strip() or Path(video_path).stem}",
        f"# Video: {video_name}",
        f"# Materia: {subject.strip() or 'Senza materia'}",
        f"# Durata: {format_clock(duration_seconds)}",
        f"# Modello: {model} · {model_label(model)}",
        f"# Lingua: {language_text}",
        f"# Creata: {created_at or now_str()}",
        "",
    ]
    body: list[str] = []
    for block in blocks:
        text = re.sub(r"\s+", " ", str(block.text or "")).strip()
        if not text:
            continue
        body.append(f"[{format_timestamp(block.start)}] {text}")
    if not body:
        body.append("[00:00] (Nessun parlato riconosciuto.)")
    return "\n".join(header + body) + "\n"


def _friendly_engine_error(exc: Exception) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if isinstance(exc, MemoryError) or "memory" in lowered or "bad_alloc" in lowered:
        return (
            "Memoria insufficiente per il modello scelto. "
            "Chiudi altre applicazioni oppure usa il modello Base."
        )
    if "model" in lowered and "load" in lowered:
        return f"Impossibile caricare il modello whisper.cpp: {message}"
    return message or exc.__class__.__name__


def _run_whisper(
    wav_path: Path,
    model_file: Path,
    model: str,
    language: str,
    duration_seconds: float,
    emit: Callable[..., None],
    cancel: Any,
) -> list[TranscriptBlock]:
    # Nella build windowed sys.stdout/sys.stderr possono essere None. Il binding
    # li usa per reindirizzare i log nativi, quindi forniamo un sink valido.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    try:
        from pywhispercpp.model import Model
    except Exception as exc:
        raise TranscriptionError(
            "Motore di trascrizione non disponibile. "
            "Reinstalla StudyFlow o le dipendenze del progetto."
        ) from exc

    threads = max(1, min(4, os.cpu_count() or 4))
    last_report = {"time": 0.0, "percent": 0.0}

    def segment_callback(segment: Any) -> None:
        if _cancel_requested(cancel):
            return
        try:
            end = max(0.0, float(getattr(segment, "t1", 0)) / 100.0)
            percent = min(0.995, end / duration_seconds) if duration_seconds > 0 else 0.0
            current = time.monotonic()
            if (
                percent - last_report["percent"] < 0.003
                and current - last_report["time"] < 0.4
            ):
                return
            last_report["percent"] = percent
            last_report["time"] = current
            emit(
                "transcribe",
                percent,
                f"Trascrizione in corso · {format_clock(end)} / "
                f"{format_clock(duration_seconds)}",
                end,
                duration_seconds,
            )
        except Exception:
            LOGGER.exception("Callback di avanzamento whisper non riuscita.")

    try:
        engine = Model(
            str(model_file),
            models_dir=str(model_file.parent),
            n_threads=threads,
            print_progress=False,
            print_realtime=False,
            redirect_whispercpp_logs_to=None,
            language=language,
            no_context=True,
        )
        segments = engine.transcribe(
            str(wav_path),
            new_segment_callback=segment_callback,
            abort_callback=lambda: _cancel_requested(cancel),
            print_progress=False,
        )
    except TranscriptionCancelled:
        raise
    except Exception as exc:
        raise TranscriptionError(_friendly_engine_error(exc)) from exc
    if _cancel_requested(cancel):
        raise TranscriptionCancelled("Trascrizione annullata.")
    blocks: list[TranscriptBlock] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", str(getattr(segment, "text", "") or "")).strip()
        if not text:
            continue
        blocks.append(
            TranscriptBlock(
                start=max(0.0, float(getattr(segment, "t0", 0)) / 100.0),
                end=max(0.0, float(getattr(segment, "t1", 0)) / 100.0),
                text=text,
            )
        )
    return blocks


def transcribe_media(
    video_path: str | Path,
    transcript_path: str | Path,
    models_dir: str | Path,
    model: str = DEFAULT_MODEL,
    language: str = DEFAULT_LANGUAGE,
    title: str = "",
    subject: str = "",
    sounds_dir: str | Path | None = None,
    ffmpeg_path: str | Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel: Any = None,
) -> dict[str, Any]:
    """Estrae l'audio, trascrive e scrive il .txt con timestamp ``[mm:ss]``."""
    video = Path(video_path)
    if not video.is_file():
        raise TranscriptionError(f"Video non trovato: {video}")
    target = Path(transcript_path)
    if target.suffix.lower() != ".txt":
        target = target.with_name(target.name + ".txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    if model not in MODEL_CATALOG:
        raise TranscriptionError(f"Modello non valido: {model}")
    if ffmpeg_path is not None and Path(ffmpeg_path).is_file():
        ffmpeg = Path(ffmpeg_path)
    else:
        search_sounds = Path(sounds_dir) if sounds_dir else app_data_dir() / "sounds"
        ffmpeg = find_ffmpeg(search_sounds)
    if ffmpeg is None:
        raise FFmpegMissingError(
            "FFmpeg non trovato. La trascrizione richiede FFmpeg: installalo con "
            "'winget install Gyan.FFmpeg' oppure copia ffmpeg.exe nella cartella "
            "dati di StudyFlow."
        )

    def emit(
        phase: str,
        percent: float,
        message: str,
        processed_seconds: float = 0.0,
        total_seconds: float = 0.0,
    ) -> None:
        if progress is None:
            return
        try:
            progress(
                {
                    "phase": phase,
                    "percent": max(0.0, min(1.0, float(percent))),
                    "message": message,
                    "processed_seconds": max(0.0, float(processed_seconds)),
                    "duration_seconds": max(0.0, float(total_seconds)),
                }
            )
        except Exception:
            LOGGER.exception("Aggiornamento avanzamento trascrizione non riuscito.")

    if _cancel_requested(cancel):
        raise TranscriptionCancelled("Trascrizione annullata.")

    emit("model", 0.0, f"Preparazione modello {model_label(model)}...")
    model_file = ensure_model(
        models_dir,
        model,
        progress=lambda percent, received, total: emit(
            "model",
            percent,
            "Download modello "
            f"{model_label(model)} · {received / (1024 * 1024):.1f} MB"
            + (f" / {total / (1024 * 1024):.1f} MB" if total else ""),
        ),
        cancel=cancel,
    )
    emit("model", 1.0, "Modello pronto.")

    source_duration = probe_media_duration(ffmpeg, video)
    with tempfile.TemporaryDirectory(prefix="studyflow-transcribe-") as folder:
        wav_path = Path(folder) / "audio.wav"
        emit("extract", 0.0, "Estrazione audio con FFmpeg...")
        extract_audio(
            ffmpeg,
            video,
            wav_path,
            duration_hint=source_duration,
            progress=lambda percent: emit(
                "extract",
                percent,
                f"Estrazione audio · {percent * 100:.0f}%",
                percent * source_duration,
                source_duration,
            ),
            cancel=cancel,
        )
        duration = wav_duration(wav_path) or source_duration
        if duration <= 0:
            raise TranscriptionError(
                "Impossibile determinare la durata dell'audio estratto."
            )
        emit(
            "transcribe",
            0.0,
            f"Trascrizione in corso · 00:00 / {format_clock(duration)}",
            0.0,
            duration,
        )
        blocks = _run_whisper(
            wav_path,
            model_file,
            model,
            language,
            duration,
            emit,
            cancel,
        )
        emit(
            "transcribe",
            1.0,
            f"Trascrizione completata · {format_clock(duration)}",
            duration,
            duration,
        )
        if _cancel_requested(cancel):
            raise TranscriptionCancelled("Trascrizione annullata.")
        text = build_transcript_text(
            title or video.stem,
            video,
            subject,
            duration,
            model,
            language,
            blocks,
        )
        atomic_write_text(target, text)
    LOGGER.info("Trascrizione salvata: %s", target)
    return {
        "transcript_path": str(target),
        "transcript_name": target.name,
        "duration_seconds": duration,
        "blocks": len(blocks),
        "model": model,
        "language": language,
        "title": title or video.stem,
        "subject": subject,
    }


# --------------------------------------------------------------------- process
def _queue_progress(
    event_queue: Any,
    job_id: int,
    lesson_id: int,
    event: dict[str, Any],
) -> None:
    try:
        event_queue.put(
            {
                "type": "progress",
                "job_id": job_id,
                "lesson_id": lesson_id,
                **event,
            }
        )
    except Exception:
        LOGGER.exception("Invio avanzamento trascrizione non riuscito.")


def _transcription_process(
    payload: dict[str, Any],
    event_queue: Any,
    cancel: Any,
) -> None:
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    job_id = int(payload["job_id"])
    lesson_id = int(payload["lesson_id"])
    try:
        result = transcribe_media(
            payload["video_path"],
            payload["transcript_path"],
            payload["models_dir"],
            model=payload["model"],
            language=payload["language"],
            title=payload["title"],
            subject=payload["subject"],
            sounds_dir=payload["sounds_dir"],
            progress=lambda event: _queue_progress(
                event_queue, job_id, lesson_id, event
            ),
            cancel=cancel,
        )
        event_queue.put(
            {
                "type": "done",
                "job_id": job_id,
                "lesson_id": lesson_id,
                "ok": True,
                "result": result,
            }
        )
    except TranscriptionCancelled as exc:
        event_queue.put(
            {
                "type": "done",
                "job_id": job_id,
                "lesson_id": lesson_id,
                "ok": False,
                "cancelled": True,
                "error": str(exc) or "Trascrizione annullata.",
            }
        )
    except Exception as exc:
        LOGGER.exception("Trascrizione non riuscita nel processo di lavoro.")
        event_queue.put(
            {
                "type": "done",
                "job_id": job_id,
                "lesson_id": lesson_id,
                "ok": False,
                "cancelled": False,
                "error": str(exc) or exc.__class__.__name__,
                "detail": traceback.format_exc(),
            }
        )
    finally:
        try:
            event_queue.close()
            event_queue.join_thread()
        except Exception:
            pass


class TranscriptionController:
    """Avvia un solo processo di trascrizione e ne distribuisce gli eventi."""

    def __init__(self, paths: dict[str, Any]):
        self.paths = paths
        self.transcripts_dir = Path(paths["transcripts"])
        self.models_dir = Path(paths["models"])
        self.sounds_dir = Path(paths["sounds"])
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.RLock()
        self._job_counter = 0
        self._active_job: dict[str, Any] | None = None
        self._process = None
        self._event_queue = None
        self._cancel = None
        self._monitor: threading.Thread | None = None
        self._on_progress: Callable[[dict[str, Any]], None] | None = None
        self._on_done: Callable[[dict[str, Any]], None] | None = None
        self._shutting_down = False

    # ---------------------------------------------------------------- public
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._process is not None and self._process.is_alive())

    def current_job(self) -> dict[str, Any] | None:
        with self._lock:
            if self._active_job is None:
                return None
            if self._process is not None and not self._process.is_alive():
                return None
            return dict(self._active_job)

    def start(
        self,
        *,
        lesson_id: int,
        video_path: str,
        transcript_path: str,
        model: str,
        language: str,
        title: str,
        subject: str,
        on_progress: Callable[[dict[str, Any]], None],
        on_done: Callable[[dict[str, Any]], None],
    ) -> tuple[bool, str]:
        with self._lock:
            if self._process is not None and self._process.is_alive():
                return False, "E' gia' in corso una trascrizione. Attendi o annullala."
            self._job_counter += 1
            job_id = self._job_counter
            cancel = self._ctx.Event()
            event_queue = self._ctx.Queue()
            payload = {
                "job_id": job_id,
                "lesson_id": int(lesson_id),
                "video_path": str(video_path),
                "transcript_path": str(transcript_path),
                "models_dir": str(self.models_dir),
                "sounds_dir": str(self.sounds_dir),
                "model": model,
                "language": language,
                "title": title,
                "subject": subject,
            }
            process = self._ctx.Process(
                target=_transcription_process,
                args=(payload, event_queue, cancel),
                name=f"StudyFlow-Transcribe-{job_id}",
                daemon=True,
            )
            self._active_job = {
                "job_id": job_id,
                "lesson_id": int(lesson_id),
                "video_path": str(video_path),
                "transcript_path": str(transcript_path),
                "model": model,
                "language": language,
                "title": title,
                "subject": subject,
            }
            self._event_queue = event_queue
            self._cancel = cancel
            self._on_progress = on_progress
            self._on_done = on_done
            self._shutting_down = False
            try:
                process.start()
            except Exception as exc:
                self._active_job = None
                self._process = None
                self._event_queue = None
                self._cancel = None
                self._on_progress = None
                self._on_done = None
                try:
                    event_queue.close()
                except Exception:
                    pass
                LOGGER.exception("Avvio del processo di trascrizione non riuscito.")
                return False, f"Impossibile avviare la trascrizione: {exc}"
            self._process = process
            self._monitor = threading.Thread(
                target=self._monitor_process,
                args=(job_id, process, event_queue, cancel),
                name=f"StudyFlow-TranscribeMonitor-{job_id}",
                daemon=True,
            )
            self._monitor.start()
        return True, ""

    def cancel(self) -> None:
        with self._lock:
            cancel = self._cancel
            job = dict(self._active_job) if self._active_job else None
        if cancel is not None:
            try:
                cancel.set()
            except Exception:
                LOGGER.exception("Annullamento trascrizione non riuscito.")
        if job is not None:
            LOGGER.info("Richiesto annullamento trascrizione video %s.", job["lesson_id"])

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            cancel = self._cancel
            process = self._process
            monitor = self._monitor
        if cancel is not None:
            try:
                cancel.set()
            except Exception:
                pass
        if process is not None:
            process.join(timeout=2.5)
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                process.join(timeout=1.5)
        if monitor is not None and monitor.is_alive():
            monitor.join(timeout=1.0)
        with self._lock:
            self._active_job = None
            self._process = None
            self._event_queue = None
            self._cancel = None
            self._on_progress = None
            self._on_done = None

    # --------------------------------------------------------------- monitor
    def _monitor_process(self, job_id: int, process: Any, event_queue: Any, cancel: Any) -> None:
        done: dict[str, Any] | None = None
        started = time.monotonic()
        while True:
            try:
                message = event_queue.get(timeout=0.2)
            except queue.Empty:
                if not process.is_alive():
                    deadline = time.monotonic() + 0.6
                    while time.monotonic() < deadline:
                        try:
                            message = event_queue.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if message.get("type") == "done":
                            done = message
                            break
                        self._dispatch_progress(message)
                    break
                if _cancel_requested(cancel) and time.monotonic() - started > 4.0:
                    process.terminate()
                continue
            except (EOFError, OSError):
                break
            message_type = message.get("type")
            if message_type == "progress":
                self._dispatch_progress(message)
            elif message_type == "done":
                done = message
                break
        try:
            process.join(timeout=3.0)
        except Exception:
            pass
        if process.is_alive():
            try:
                process.terminate()
                process.join(timeout=1.5)
            except Exception:
                pass
        if done is None:
            exit_code = getattr(process, "exitcode", None)
            done = {
                "type": "done",
                "job_id": job_id,
                "ok": False,
                "cancelled": _cancel_requested(cancel),
                "error": (
                    "Il processo di trascrizione si e' chiuso in modo imprevisto "
                    f"(codice {exit_code})."
                ),
            }
        self._finish_job(job_id, done, event_queue)

    def _dispatch_progress(self, message: dict[str, Any]) -> None:
        with self._lock:
            callback = self._on_progress
            shutting_down = self._shutting_down
        if callback is None or shutting_down:
            return
        try:
            callback(dict(message))
        except Exception:
            LOGGER.exception("Callback di avanzamento trascrizione non riuscita.")

    def _finish_job(self, job_id: int, done: dict[str, Any], event_queue: Any) -> None:
        with self._lock:
            if self._active_job is None or int(self._active_job.get("job_id", 0)) != job_id:
                return
            lesson_id = int(self._active_job["lesson_id"])
            callback = self._on_done
            shutting_down = self._shutting_down
            self._active_job = None
            self._process = None
            self._event_queue = None
            self._cancel = None
            self._on_progress = None
            self._on_done = None
            self._monitor = None
        done.setdefault("lesson_id", lesson_id)
        try:
            event_queue.close()
        except Exception:
            pass
        if callback is None or shutting_down:
            return
        try:
            callback(dict(done))
        except Exception:
            LOGGER.exception("Callback finale trascrizione non riuscita.")
