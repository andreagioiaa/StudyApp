"""Sezioni della pagina materia per videolezioni/trascrizioni e flashcard.

Affiancano ``MaterialsSection`` (PDF, documenti ed export di PDF e Slide) cosi'
che dalla sezione Materie si raggiungano tutti i materiali di una materia.
"""

from __future__ import annotations

import os
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..services.subject_folders import (
    FLASHCARD_EXPORT_NAME,
    KIND_FLASHCARDS,
    KIND_TRANSCRIPTS,
    SUBFOLDERS,
)
from .theme import font
from .widgets import Card


VIDEO_STATUS_LABELS = {
    "ready": "Da trascrivere",
    "processing": "Trascrizione in corso",
    "completed": "Trascritta",
    "cancelled": "Trascrizione annullata",
    "error": "Errore di trascrizione",
}
MAX_FLASHCARD_PREVIEW = 5


def _open_path(widget, path: str, label: str) -> None:
    if not path:
        messagebox.showwarning(f"{label} non disponibile", f"Nessun file {label.lower()} associato.",
                               parent=widget.winfo_toplevel())
        return
    target = Path(path)
    if not target.exists():
        messagebox.showerror("File non trovato", f"Il file non esiste piu':\n{path}", parent=widget.winfo_toplevel())
        return
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
    except (OSError, AttributeError) as exc:
        messagebox.showerror("Impossibile aprire il file", str(exc), parent=widget.winfo_toplevel())


class _SectionBase(Card):
    def __init__(self, master, app, subject: str, title: str):
        super().__init__(master)
        self.app = app
        self.db = app.db
        self.subject = str(subject or "")
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=title, font=font(9, "bold"), text_color=COLORS.text_faint,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(header, text="", font=font(10), text_color=COLORS.text_muted, anchor="w")
        self.count_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.header_actions = ctk.CTkFrame(header, fg_color="transparent")
        self.header_actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))
        self.body.grid_columnconfigure(0, weight=1)

    @staticmethod
    def button(parent, text: str, width: int, command, primary: bool = False) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent, text=text, height=30, width=width,
            font=font(10, "bold" if primary else "normal"),
            fg_color=COLORS.accent_dark if primary else "transparent",
            hover_color=COLORS.accent_hover if primary else COLORS.surface_hover,
            border_width=0 if primary else 1, border_color=COLORS.border,
            text_color=COLORS.text if primary else COLORS.text_muted,
            corner_radius=7, command=command,
        )

    def empty(self, text: str) -> None:
        ctk.CTkLabel(self.body, text=text, font=font(11), text_color=COLORS.text_faint, anchor="w",
                     wraplength=440, justify="left").grid(row=0, column=0, sticky="w", pady=(0, 2))

    def row_card(self, row: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, fg_color=COLORS.surface_alt, corner_radius=10, border_width=1,
                            border_color=COLORS.border)
        card.grid(row=row, column=0, sticky="ew", pady=3)
        card.grid_columnconfigure(0, weight=1)
        return card


class VideosSection(_SectionBase):
    """Videolezioni della materia con le relative trascrizioni."""

    def __init__(self, master, app, subject: str, lessons: list[dict[str, Any]]):
        super().__init__(master, app, subject, "VIDEOLEZIONI E TRASCRIZIONI")
        self.button(self.header_actions, "Cartella", 80,
                    lambda: self.app.open_subject_folder(self.subject, KIND_TRANSCRIPTS)).pack(side="left", padx=(0, 6))
        self.button(self.header_actions, "+  Videolezione", 126,
                    lambda: self.app.open_in_view("videos", subject=self.subject), primary=True).pack(side="left")
        transcribed = sum(1 for lesson in lessons if str(lesson.get("transcript_path") or "").strip())
        self.count_label.configure(
            text=f"{len(lessons)} video · {transcribed} trascritti" if lessons else "Nessuna videolezione collegata"
        )
        if not lessons:
            self.empty("Nessuna videolezione per questa materia. Aggiungila da Video e Trascrizione "
                       "scegliendo questa materia: la trascrizione verrà salvata nella sua cartella.")
            return
        for index, lesson in enumerate(lessons):
            self._lesson_row(lesson, index)

    def _lesson_row(self, lesson: dict[str, Any], row: int) -> None:
        card = self.row_card(row)
        ctk.CTkLabel(card, text=str(lesson.get("title") or "Videolezione"), font=font(13, "bold"),
                     text_color=COLORS.text, anchor="w", wraplength=380, justify="left").grid(
            row=0, column=0, sticky="w", padx=14, pady=(10, 1))
        status = str(lesson.get("status") or "ready")
        transcript = str(lesson.get("transcript_path") or "").strip()
        has_transcript = bool(transcript) and Path(transcript).is_file()
        meta = [VIDEO_STATUS_LABELS.get(status, status)]
        if transcript:
            meta.append(Path(transcript).name + ("" if has_transcript else " (non trovato)"))
        ctk.CTkLabel(card, text="  ·  ".join(meta), font=font(9),
                     text_color=COLORS.green if has_transcript else COLORS.text_faint, anchor="w",
                     wraplength=420, justify="left").grid(row=1, column=0, sticky="w", padx=14)
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 10))
        if has_transcript:
            self.button(actions, "Apri trascrizione", 120,
                        lambda path=transcript: _open_path(self, path, "Trascrizione"), primary=True).pack(
                side="left", padx=(0, 4))
        self.button(actions, "Apri video", 80,
                    lambda path=str(lesson.get("video_path") or ""): _open_path(self, path, "Video")).pack(
            side="left", padx=4)
        self.button(actions, "Mostra in Video", 110,
                    lambda lesson_id=int(lesson["id"]): self.app.open_in_view(
                        "videos", lesson_id=lesson_id, subject=self.subject)).pack(side="left", padx=4)


class FlashcardsSection(_SectionBase):
    """Riepilogo delle flashcard della materia con accesso al ripasso."""

    def __init__(self, master, app, subject: str, cards: list[dict[str, Any]], stats: dict[str, int]):
        super().__init__(master, app, subject, "FLASHCARD")
        self.button(self.header_actions, "Cartella", 80,
                    lambda: self.app.open_subject_folder(self.subject, KIND_FLASHCARDS)).pack(side="left", padx=(0, 6))
        self.button(self.header_actions, "Gestisci", 80,
                    lambda: self.app.open_in_view("flashcards", subject=self.subject)).pack(side="left", padx=(0, 6))
        due = int(stats.get("due", 0) or 0)
        self.button(self.header_actions, f"Ripassa ({due})" if due else "Ripassa", 96,
                    lambda: self.app.open_in_view("flashcards", subject=self.subject, study=True),
                    primary=bool(due)).pack(side="left")
        total = len(cards)
        self.count_label.configure(
            text=f"{total} schede · {due} da ripassare" if total else "Nessuna flashcard collegata"
        )
        if not cards:
            self.empty("Nessuna flashcard per questa materia. Creale dalla sezione Flashcard "
                       "scegliendo questa materia.")
            return
        for index, card in enumerate(cards[:MAX_FLASHCARD_PREVIEW]):
            row = self.row_card(index)
            ctk.CTkLabel(row, text=str(card.get("front") or "").strip()[:160], font=font(11, "bold"),
                         text_color=COLORS.text, anchor="w", wraplength=440, justify="left").grid(
                row=0, column=0, sticky="w", padx=14, pady=(8, 8))
        footer = ctk.CTkFrame(self.body, fg_color="transparent")
        footer.grid(row=MAX_FLASHCARD_PREVIEW + 1, column=0, sticky="ew", pady=(6, 0))
        if total > MAX_FLASHCARD_PREVIEW:
            ctk.CTkLabel(footer, text=f"… e altre {total - MAX_FLASHCARD_PREVIEW} schede", font=font(10),
                         text_color=COLORS.text_faint, anchor="w").pack(side="left")
        folder = self.app.subject_folders.path_for(self.subject)
        export = folder / SUBFOLDERS[KIND_FLASHCARDS] / FLASHCARD_EXPORT_NAME if folder else None
        if export is not None:
            self.button(footer, "Apri CSV", 76,
                        lambda path=str(export): _open_path(self, path, "CSV")).pack(side="right")
