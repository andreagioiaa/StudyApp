"""Configurazione logging con rotazione, senza dipendenze esterne."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import ensure_data_layout


_CONFIGURED = False


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

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=1_500_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    _CONFIGURED = True
    logging.getLogger(__name__).info("Logging inizializzato in %s", log_path)
    return log_path
