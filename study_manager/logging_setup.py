"""Configurazione logging con rotazione, senza dipendenze esterne."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import ensure_data_layout


_CONFIGURED = False

# Token e segreti OAuth Google che non devono mai finire in studyflow.log.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"), "ya29.***"),  # access token
    (re.compile(r"1//[0-9A-Za-z_\-]{10,}"), "1//***"),  # refresh token
    (re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"), "GOCSPX-***"),  # client secret
    (re.compile(r"private-[0-9A-Za-z]+"), "private-***"),  # indirizzo segreto iCal
    (re.compile(r"(?i)(Bearer\s+)[^\s\"',}]+"), r"\1***"),
    (
        re.compile(
            r"(?i)(\"?(?:access_token|refresh_token|client_secret|id_token|code_verifier)"
            r"\"?\s*[:=]\s*\"?)[^\s\"&,}]+"
        ),
        r"\1***",
    ),
    (re.compile(r"([?&]code=)[^\s&\"]+"), r"\1***"),  # codice di autorizzazione
)


def mask_secrets(text: object) -> str:
    """Sostituisce token OAuth e segreti con ``***``."""
    value = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class SecretMaskingFilter(logging.Filter):
    """Maschera i segreti nel messaggio finale e nei traceback di ogni record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_secrets(record.exc_text)
        return True


def setup_logging(debug: bool = False) -> Path:
    global _CONFIGURED
    paths = ensure_data_layout()
    log_path = paths["log"]
    if _CONFIGURED:
        return log_path

    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    masking = SecretMaskingFilter()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=1_500_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(masking)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(masking)
    root.addHandler(console)
    # urllib3 in DEBUG scrive gli URL completi delle richieste HTTP.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True
    logging.getLogger(__name__).info("Logging inizializzato in %s", log_path)
    return log_path
