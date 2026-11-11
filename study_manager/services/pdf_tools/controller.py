"""Esecuzione in background delle conversioni PDF (singole o batch).

La GUI non viene mai toccata da questo modulo: gli eventi finiscono in una
coda che la vista svuota con ``after`` sul thread principale di Tkinter.

Eventi prodotti (dizionari):

- ``{"type": "progress", "fraction", "message", "file_index", "file_count"}``
- ``{"type": "document", "index", "path", "document", "markdown"}``
- ``{"type": "file_error", "index", "path", "error"}``
- ``{"type": "done", "ok", "cancelled", "error", "converted", "failed"}``
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from .document import render_markdown
from .extract import ExtractOptions, PdfCancelled, PdfToolsError, convert_pdf


LOGGER = logging.getLogger(__name__)


def list_pdfs(folder: str | Path, recursive: bool = False) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        return []
    pattern = "**/*" if recursive else "*"
    return sorted(
        (path for path in root.glob(pattern) if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).lower(),
    )


class PdfConversionController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.events: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, sources: list[Path], options: ExtractOptions) -> tuple[bool, str]:
        if not sources:
            return False, "Nessun PDF selezionato."
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "Un'estrazione e' gia' in corso."
            self._cancel = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(list(sources), options, self._cancel),
                name="studyflow-pdf-convert",
                daemon=True,
            )
            self._thread.start()
        return True, ""

    def run_task(self, task: Callable[[Callable[[float, str], None], threading.Event], Any]) -> tuple[bool, str]:
        """Esegue un'operazione generica (es. esportazione batch) nello stesso canale."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "Un'altra operazione e' in corso."
            self._cancel = threading.Event()
            cancel = self._cancel

            def runner() -> None:
                try:
                    result = task(lambda fraction, message: self._progress(fraction, message, 0, 1), cancel)
                    self.events.put({"type": "task_done", "ok": True, "result": result, "cancelled": cancel.is_set()})
                except PdfCancelled:
                    self.events.put({"type": "task_done", "ok": False, "cancelled": True, "error": ""})
                except Exception as exc:  # pragma: no cover - mostrato all'utente
                    LOGGER.exception("Operazione PDF non riuscita.")
                    self.events.put({"type": "task_done", "ok": False, "cancelled": False, "error": str(exc)})

            self._thread = threading.Thread(target=runner, name="studyflow-pdf-task", daemon=True)
            self._thread.start()
        return True, ""

    def cancel(self) -> None:
        self._cancel.set()

    def shutdown(self, timeout: float = 3.0) -> None:
        self._cancel.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _progress(self, fraction: float, message: str, index: int, count: int) -> None:
        overall = (index + max(0.0, min(1.0, fraction))) / max(1, count)
        self.events.put(
            {
                "type": "progress",
                "fraction": overall,
                "message": message,
                "file_index": index,
                "file_count": count,
            }
        )

    def _run(self, sources: list[Path], options: ExtractOptions, cancel: threading.Event) -> None:
        converted = 0
        failed = 0
        count = len(sources)
        try:
            for index, path in enumerate(sources):
                if cancel.is_set():
                    raise PdfCancelled("Elaborazione annullata.")
                prefix = f"[{index + 1}/{count}] {path.name}" if count > 1 else path.name
                try:
                    document = convert_pdf(
                        path,
                        options,
                        progress=lambda fraction, message, i=index: self._progress(
                            fraction, f"{prefix} · {message}", i, count
                        ),
                        cancel=cancel,
                    )
                except PdfCancelled:
                    raise
                except PdfToolsError as exc:
                    failed += 1
                    self.events.put({"type": "file_error", "index": index, "path": path, "error": str(exc)})
                    continue
                except Exception as exc:
                    failed += 1
                    LOGGER.exception("Estrazione di %s non riuscita.", path)
                    self.events.put(
                        {"type": "file_error", "index": index, "path": path, "error": f"Errore imprevisto: {exc}"}
                    )
                    continue
                converted += 1
                self.events.put(
                    {
                        "type": "document",
                        "index": index,
                        "path": path,
                        "document": document,
                        "markdown": render_markdown(document),
                    }
                )
            self.events.put(
                {"type": "done", "ok": failed == 0, "cancelled": False, "error": "",
                 "converted": converted, "failed": failed}
            )
        except PdfCancelled:
            self.events.put(
                {"type": "done", "ok": False, "cancelled": True, "error": "",
                 "converted": converted, "failed": failed}
            )
        except Exception as exc:  # pragma: no cover - rete di sicurezza
            LOGGER.exception("Conversione PDF interrotta.")
            self.events.put(
                {"type": "done", "ok": False, "cancelled": False, "error": str(exc),
                 "converted": converted, "failed": failed}
            )
