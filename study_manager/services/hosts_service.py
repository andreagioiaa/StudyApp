"""Gestione sicura e idempotente della sezione gestita del file hosts di Windows."""

from __future__ import annotations

import codecs
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import HOSTS_BEGIN_MARKER, HOSTS_END_MARKER, HOSTS_ENTRY_IP
from ..utils import normalize_domain


LOGGER = logging.getLogger(__name__)

_MANAGED_RE = re.compile(
    rf"(?ms)^{re.escape(HOSTS_BEGIN_MARKER)}.*?^{re.escape(HOSTS_END_MARKER)}[^\r\n]*(?:\r?\n)?"
)


class HostsError(RuntimeError):
    """Errore durante lettura, scrittura o validazione del file hosts."""


@dataclass(frozen=True)
class HostsApplyResult:
    changed: bool
    entry_count: int
    hosts_path: Path


def hosts_file_path() -> Path:
    override = os.environ.get("STUDYFLOW_HOSTS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def _read_raw(path: Path) -> tuple[str, str, bool]:
    try:
        raw = path.read_bytes()
    except PermissionError as exc:
        raise HostsError("Permessi insufficienti per leggere il file hosts.") from exc
    except OSError as exc:
        raise HostsError(f"Impossibile leggere il file hosts: {exc}") from exc

    has_bom = raw.startswith(codecs.BOM_UTF8)
    encoding = "utf-8-sig" if has_bom else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        has_bom = False
        text = raw.decode(encoding, errors="replace")
    return text, encoding, has_bom


def _write_raw(path: Path, text: str, encoding: str, has_bom: bool) -> None:
    payload = text.encode("utf-8-sig" if has_bom else encoding, errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".studyflow-hosts-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, temp_name)
        except OSError:
            pass
        os.replace(temp_name, path)
    except PermissionError as exc:
        raise HostsError(
            "Permessi insufficienti per modificare il file hosts. "
            "L'operazione richiede i privilegi di amministratore."
        ) from exc
    except OSError as exc:
        raise HostsError(f"Scrittura del file hosts non riuscita: {exc}") from exc
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def strip_managed_block(text: str) -> str:
    return _MANAGED_RE.sub("", text)


def has_managed_block(text: str | None = None) -> bool:
    if text is None:
        try:
            text, _, _ = _read_raw(hosts_file_path())
        except HostsError:
            return False
    return HOSTS_BEGIN_MARKER in text and HOSTS_END_MARKER in text


def expand_domains(domains: Iterable[str]) -> list[str]:
    expanded: set[str] = set()
    for value in domains:
        domain = normalize_domain(str(value))
        expanded.add(domain)
        if not domain.startswith("www."):
            expanded.add(f"www.{domain}")
        if not domain.startswith("m."):
            expanded.add(f"m.{domain}")
    return sorted(expanded)


def _build_section(domains: Iterable[str], newline: str) -> str:
    values = expand_domains(domains)
    lines = [
        HOSTS_BEGIN_MARKER,
        "# Sezione gestita da StudyFlow. Ogni modifica manuale verra' sovrascritta.",
        "# Viene rimossa automaticamente alla fine della sessione o alla chiusura dell'app.",
        "",
    ]
    lines.extend(f"{HOSTS_ENTRY_IP} {domain}" for domain in values)
    lines.extend(["", HOSTS_END_MARKER])
    return newline.join(lines) + newline


def apply_hosts_block(
    domains: Iterable[str],
    backup_path: str | Path | None = None,
    hosts_path: str | Path | None = None,
    flush_dns: bool = True,
) -> HostsApplyResult:
    """Scrive la sezione gestita senza toccare il resto del file hosts."""
    path = Path(hosts_path) if hosts_path else hosts_file_path()
    if not path.exists():
        raise HostsError(f"File hosts non trovato: {path}")
    text, encoding, has_bom = _read_raw(path)
    base = strip_managed_block(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    if not base.endswith(("\n", "\r")):
        base += newline
    section = _build_section(domains, newline)
    new_text = base + section
    changed = new_text != text

    if backup_path:
        backup = Path(backup_path)
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(base.encode(encoding, errors="replace"))
    if changed:
        _write_raw(path, new_text, encoding, has_bom)
        LOGGER.info("File hosts aggiornato con %d domini.", len(expand_domains(domains)))
        if flush_dns:
            flush_dns_cache()
    return HostsApplyResult(changed=changed, entry_count=len(expand_domains(domains)), hosts_path=path)


def restore_hosts_block(
    backup_path: str | Path | None = None,
    hosts_path: str | Path | None = None,
    flush_dns: bool = True,
) -> bool:
    """Rimuove esclusivamente la sezione StudyFlow. Ritorna True se ha modificato il file."""
    del backup_path  # Il backup originale non viene mai sovrascritto automaticamente.
    path = Path(hosts_path) if hosts_path else hosts_file_path()
    if not path.exists():
        return False
    text, encoding, has_bom = _read_raw(path)
    if HOSTS_BEGIN_MARKER not in text:
        return False
    new_text = strip_managed_block(text)
    if new_text == text:
        return False
    _write_raw(path, new_text, encoding, has_bom)
    LOGGER.info("Sezione StudyFlow rimossa dal file hosts.")
    if flush_dns:
        flush_dns_cache()
    return True


def flush_dns_cache() -> None:
    if os.name != "nt":
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["ipconfig", "/flushdns"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        LOGGER.debug("Flush DNS non riuscito.", exc_info=True)
