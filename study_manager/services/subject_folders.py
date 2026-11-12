"""Cartelle su disco per ogni materia.

Ogni materia ha una cartella nella radice di funzionamento dell'app
(``<root>/Materie/<Nome materia>``) con sottocartelle dedicate ai materiali
generati da StudyFlow:

* ``Trascrizioni`` - i .txt prodotti dalle videolezioni;
* ``Appunti``      - TXT/LaTeX/Word esportati dal modulo PDF e Slide;
* ``Flashcard``    - export CSV delle flashcard della materia.

I file originali scelti dall'utente (PDF, video, dispense) non vengono mai
copiati ne' spostati: restano dove sono e sono solo collegati alla materia nel
database.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterable


LOGGER = logging.getLogger(__name__)

KIND_TRANSCRIPTS = "transcripts"
KIND_NOTES = "notes"
KIND_FLASHCARDS = "flashcards"

SUBFOLDERS: dict[str, str] = {
    KIND_TRANSCRIPTS: "Trascrizioni",
    KIND_NOTES: "Appunti",
    KIND_FLASHCARDS: "Flashcard",
}

FLASHCARD_EXPORT_NAME = "Flashcard.csv"

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def folder_name(subject: str, max_length: int = 80) -> str:
    """Nome di cartella valido su Windows a partire dal nome della materia."""
    value = unicodedata.normalize("NFKC", str(subject or ""))
    value = _INVALID.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return ""
    if value.split(".", 1)[0].strip().upper() in _RESERVED:
        value = f"_{value}"
    return value[:max_length].rstrip(" .")


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_inside(path: str | Path, folder: str | Path) -> bool:
    """``True`` se ``path`` si trova dentro ``folder`` (confronto case-insensitive su Windows)."""
    child = os.path.normcase(os.path.abspath(str(path)))
    parent = os.path.normcase(os.path.abspath(str(folder)))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def unique_target(folder: Path, name: str) -> Path:
    """Percorso libero in ``folder``: aggiunge `` (2)``, `` (3)``... se serve."""
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 2
    while True:
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def move_file_into(source: str | Path, folder: str | Path) -> Path:
    """Sposta un file in ``folder`` senza sovrascrivere nulla e restituisce il nuovo percorso."""
    src = Path(source)
    dest_dir = Path(folder)
    if _same_path(src.parent, dest_dir):
        return src
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = unique_target(dest_dir, src.name)
    shutil.move(str(src), str(target))
    return target


class SubjectFolders:
    """Gestisce la cartella ``Materie`` e le sottocartelle di ogni materia."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ----------------------------------------------------------------- percorsi
    def path_for(self, subject: str) -> Path | None:
        """Cartella della materia (senza crearla); ``None`` se il nome e' vuoto."""
        name = folder_name(subject)
        return self.root / name if name else None

    def ensure(self, subject: str) -> Path | None:
        """Crea (se mancano) la cartella della materia e le sue sottocartelle."""
        folder = self.path_for(subject)
        if folder is None:
            return None
        try:
            for sub in SUBFOLDERS.values():
                (folder / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            LOGGER.exception("Impossibile creare la cartella della materia '%s'.", subject)
            return None
        return folder

    def subfolder(self, subject: str, kind: str) -> Path | None:
        """Sottocartella di un tipo di materiale (creata se manca)."""
        folder = self.ensure(subject)
        if folder is None:
            return None
        return folder / SUBFOLDERS[kind]

    def ensure_all(self, subjects: Iterable[str]) -> int:
        created = 0
        for subject in subjects:
            folder = self.path_for(subject)
            if folder is None:
                continue
            existed = folder.is_dir()
            if self.ensure(subject) is not None and not existed:
                created += 1
        return created

    def subject_of(self, path: str | Path) -> str | None:
        """Nome della cartella-materia che contiene ``path``, se ce n'e' una."""
        if not path or not is_inside(path, self.root):
            return None
        try:
            relative = Path(os.path.abspath(str(path))).relative_to(Path(os.path.abspath(self.root)))
        except ValueError:
            return None
        return relative.parts[0] if relative.parts else None

    # ------------------------------------------------------------------ rinomina
    def rename(self, old_subject: str, new_subject: str) -> tuple[Path | None, Path | None]:
        """Rinomina la cartella di una materia.

        Restituisce ``(vecchia, nuova)``. Se la nuova cartella esiste gia', i
        contenuti vengono uniti senza sovrascrivere file.
        """
        old = self.path_for(old_subject)
        new = self.path_for(new_subject)
        if new is None:
            return old, None
        if old is None or not old.is_dir():
            return old, self.ensure(new_subject)
        if old.name == new.name:
            return old, self.ensure(new_subject)
        if _same_path(old, new):
            # Cambia solo maiuscole/minuscole: su Windows serve un passaggio intermedio.
            temp = old.with_name(f".{old.name}.{uuid.uuid4().hex[:8]}")
            old.rename(temp)
            temp.rename(new)
        elif not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
        else:
            self._merge(old, new)
        self.ensure(new_subject)
        return old, new

    def _merge(self, source: Path, target: Path) -> None:
        for entry in list(source.iterdir()):
            dest = target / entry.name
            if entry.is_dir() and dest.is_dir():
                self._merge(entry, dest)
                continue
            if dest.exists():
                dest = unique_target(target, entry.name)
            shutil.move(str(entry), str(dest))
        try:
            source.rmdir()
        except OSError:
            LOGGER.debug("Cartella %s non vuota dopo l'unione.", source)

    def remove_if_empty(self, subject: str) -> bool:
        """Rimuove la cartella della materia solo se non contiene file."""
        folder = self.path_for(subject)
        if folder is None or not folder.is_dir():
            return False
        if any(path.is_file() for path in folder.rglob("*")):
            return False
        for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                return False
        try:
            folder.rmdir()
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------- export
    def export_flashcards(self, subject: str, cards: list[dict[str, Any]]) -> Path | None:
        """Scrive ``Flashcard/Flashcard.csv`` con le schede della materia.

        Formato CSV con ``;`` e BOM UTF-8, apribile direttamente in Excel e
        importabile in Anki. Senza schede il file viene rimosso.
        """
        folder = self.subfolder(subject, KIND_FLASHCARDS)
        if folder is None:
            return None
        target = folder / FLASHCARD_EXPORT_NAME
        if not cards:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("Impossibile rimuovere %s.", target, exc_info=True)
            return None
        temp = target.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["Fronte", "Retro", "Note"])
            for card in sorted(cards, key=lambda value: int(value.get("id", 0) or 0)):
                writer.writerow(
                    [
                        str(card.get("front") or ""),
                        str(card.get("back") or ""),
                        str(card.get("notes") or ""),
                    ]
                )
        os.replace(temp, target)
        return target
