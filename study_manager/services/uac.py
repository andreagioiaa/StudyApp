"""Rilevamento privilegi e avvio di processi elevati tramite UAC (solo Windows)."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..paths import is_frozen, project_root


LOGGER = logging.getLogger(__name__)

_ERROR_ACCESS_DENIED = 5
_ERROR_ELEVATION_REQUIRED = 740
_ERROR_CANCELLED = 1223


def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    # Gli handle Windows x64 sono puntatori a 64 bit: dichiarare i tipi evita
    # troncamenti silenziosi da parte dei valori di ritorno predefiniti di ctypes.
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_bool,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _python_executable() -> Path:
    executable = Path(sys.executable)
    if not is_frozen() and executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return executable


def _build_command(arguments: list[str]) -> tuple[str, str, Path]:
    """Restituisce eseguibile, argomenti e directory di lavoro sicura."""
    if is_frozen():
        executable = Path(sys.executable).resolve()
        # In una build one-file la cartella temporanea _MEIPASS viene eliminata
        # alla chiusura: come working directory usiamo quella dell'exe.
        return str(executable), subprocess.list2cmdline(arguments), executable.parent
    script = project_root() / "main.py"
    return (
        str(_python_executable()),
        subprocess.list2cmdline([str(script), *arguments]),
        project_root(),
    )


def launch_elevated(arguments: list[str], show: bool = False) -> bool:
    """Avvia un helper con il manifest 'runas' di Windows.

    L'app principale resta asInvoker: il prompt UAC compare solo quando serve
    davvero modificare il file hosts.
    """
    if os.name != "nt":
        LOGGER.warning("Elevazione UAC non disponibile su questa piattaforma.")
        return False
    executable, parameters, working_directory = _build_command(arguments)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            parameters,
            str(working_directory),
            1 if show else 0,
        )
    except (AttributeError, OSError) as exc:
        LOGGER.exception("Avvio del processo elevato fallito: %s", exc)
        return False
    code = int(result)
    if code > 32:
        LOGGER.info("Processo elevato avviato (ShellExecute=%s).", code)
        return True
    if code == _ERROR_CANCELLED:
        LOGGER.info("Richiesta UAC annullata dall'utente.")
    elif code in (_ERROR_ACCESS_DENIED, _ERROR_ELEVATION_REQUIRED):
        LOGGER.warning("UAC ha negato l'elevazione (codice %s).", code)
    else:
        LOGGER.warning("ShellExecuteW ha restituito il codice %s.", code)
    return False
