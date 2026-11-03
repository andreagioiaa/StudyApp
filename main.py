"""Entry point di StudyFlow.

Il parsing degli argomenti avviene prima di importare CustomTkinter: in questo
modo l'helper elevato per il file hosts parte in pochi millisecondi e non
inizializza alcuna GUI.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path


APP_MUTEX_NAME = "Local\\StudyFlow.Desktop.SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _message_box(title: str, text: str, error: bool = False) -> None:
    if os.name == "nt":
        try:
            flags = 0x00000010 if error else 0x00000040
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except (AttributeError, OSError):
            pass
    print(f"{title}: {text}", file=sys.stderr)


def _configure_mutex_functions(kernel32) -> None:
    """Dichiara i tipi Win32: su Windows x64 gli handle sono a 64 bit.

    Senza argtypes/restype ctypes assume ``int`` a 32 bit e puo' troncare
    l'handle, rendendo impossibile chiuderlo correttamente.
    """
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool


def _acquire_single_instance() -> int | None:
    if os.name != "nt":
        return 0
    kernel32 = ctypes.windll.kernel32
    _configure_mutex_functions(kernel32)
    handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    if not handle:
        _message_box(
            "StudyFlow",
            "Impossibile inizializzare il controllo di istanza singola.",
            error=True,
        )
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        _message_box(
            "StudyFlow",
            "StudyFlow e' gia' in esecuzione.\n"
            "Chiudi la finestra esistente prima di avviarne un'altra.",
        )
        return None
    return int(handle)


def _release_single_instance(handle: int | None) -> None:
    if not handle or os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        _configure_mutex_functions(kernel32)
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    except (AttributeError, OSError):
        pass


def _guard_mode(arguments: list[str]) -> int:
    from study_manager.services.hosts_guard import main as guard_main

    return int(guard_main(arguments) or 0)


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)

    if "--hosts-guard" in arguments:
        return _guard_mode(arguments)

    parser = argparse.ArgumentParser(prog="StudyFlow", description="Gestionale studio e analytics desktop.")
    parser.add_argument("--version", action="store_true", help="Mostra la versione ed esce.")
    parser.add_argument("--debug", action="store_true", help="Abilita log di debug.")
    parser.add_argument("--data-dir", help="Cartella dati alternativa (utile per test o modalita' portabile).")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Verifica automatica di avvio, viste, timer e chiusura (uso diagnostico).",
    )
    options = parser.parse_args(arguments)

    if options.version:
        from study_manager import __version__

        version_text = f"StudyFlow {__version__}"
        if getattr(sys, "frozen", False) or sys.stdout is None:
            # La build one-file usa console=False: senza console l'output di
            # --version non sarebbe visibile.
            _message_box("StudyFlow", version_text)
        else:
            print(version_text)
        return 0

    if options.data_dir:
        os.environ["STUDYFLOW_DATA_DIR"] = str(Path(options.data_dir).expanduser().resolve())
    elif options.smoke_test:
        os.environ["STUDYFLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="studyflow-smoke-")

    logging.basicConfig(level=logging.INFO)
    mutex = _acquire_single_instance()
    if mutex is None:
        return 2

    try:
        from study_manager.ui.app import run_app

        return run_app(debug=options.debug, smoke_test=options.smoke_test)
    except ImportError as exc:
        logging.exception("Dipendenza mancante.")
        _message_box(
            "StudyFlow - dipendenza mancante",
            f"Impossibile avviare l'applicazione:\n{exc}\n\n"
            "Installa le dipendenze con:\n"
            "python -m pip install -r requirements.txt",
            error=True,
        )
        return 1
    except Exception as exc:
        logging.exception("Errore fatale all'avvio.")
        _message_box(
            "StudyFlow - errore fatale",
            f"Errore durante l'avvio:\n{exc}\n\n"
            "Controlla il file studyflow.log nella cartella dati.",
            error=True,
        )
        return 1
    finally:
        _release_single_instance(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
