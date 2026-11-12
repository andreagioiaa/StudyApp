"""Vista Video e Trascrizione: archivio, elaborazione e lettura dei testi."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Any, Callable

import customtkinter as ctk

from ..config import COLORS
from ..services.subject_folders import KIND_TRANSCRIPTS, move_file_into
from ..services.transcription import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    LANGUAGE_CHOICES,
    LANGUAGE_LABELS,
    MODEL_CATALOG,
    SUPPORTED_VIDEO_EXTENSIONS,
    TranscriptionError,
    default_transcript_name,
    find_ffmpeg,
    model_is_available,
    model_label,
    rename_transcript_file,
    transcript_filename,
)
from ..utils import format_clock, safe_float
from .theme import font, mono
from .widgets import (
    Card,
    PageHeader,
    center_window,
    clear_children,
    separator,
    set_window_icon,
    render_state,
)


ALL_SUBJECTS = "Tutte le materie"
STATUS_LABELS = {
    "ready": "Pronta per la trascrizione",
    "processing": "Elaborazione in corso",
    "completed": "Trascrizione completata",
    "cancelled": "Elaborazione annullata",
    "error": "Errore di elaborazione",
}


class VideoView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.paths = app.paths
        self.transcriptions = app.transcriptions
        self.transcripts_dir = Path(app.paths["transcripts"])
        self._lessons: list[dict[str, Any]] = []
        self._selected_id: int | None = None
        self._filter_subject = ALL_SUBJECTS
        self._active_lesson_id: int | None = None
        self._live_progress = 0.0
        self._live_message = ""
        self._live_phase = ""
        self._last_db_progress = 0.0
        self._detail_progress: ctk.CTkProgressBar | None = None
        self._detail_status: ctk.CTkLabel | None = None
        self._transcript_box: ctk.CTkTextbox | None = None
        self._current_transcript_text = ""

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._build_header()
        self._build_summary()
        self._build_toolbar()
        self._build_body()

    # ------------------------------------------------------------------ layout
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.grid_columnconfigure(0, weight=1)
        PageHeader(
            header,
            "Video e Trascrizione",
            "Archivia le videolezioni, trascrivile in locale e collega ogni testo a una materia.",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header,
            text="+  Aggiungi video",
            height=40,
            width=158,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=lambda: self._open_video_dialog(None),
        ).grid(row=0, column=1, sticky="e", pady=(4, 0))

    def _build_summary(self) -> None:
        summary = ctk.CTkFrame(self, fg_color="transparent")
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        summary.grid_columnconfigure(3, weight=1)
        self._summary_values: dict[str, ctk.CTkLabel] = {}
        items = (
            ("total", "VIDEOLEZIONI", COLORS.blue),
            ("done", "TRASCRITTE", COLORS.accent),
            ("subjects", "MATERIE", COLORS.purple),
        )
        for column, (key, title, color) in enumerate(items):
            item = ctk.CTkFrame(summary, fg_color="transparent")
            item.grid(row=0, column=column, sticky="w", padx=(0, 28))
            value = ctk.CTkLabel(
                item,
                text="0",
                font=font(17, "bold"),
                text_color=color,
                anchor="w",
            )
            value.pack(side="left")
            ctk.CTkLabel(
                item,
                text=title,
                font=font(9, "bold"),
                text_color=COLORS.text_muted,
                anchor="w",
            ).pack(side="left", padx=(7, 0), pady=(2, 0))
            self._summary_values[key] = value

    def _build_toolbar(self) -> None:
        toolbar = Card(self)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        toolbar.grid_columnconfigure(1, weight=1)

        subject_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        subject_frame.grid(row=0, column=0, sticky="w", padx=(18, 10), pady=(9, 8))
        ctk.CTkLabel(
            subject_frame,
            text="Archivio per materia",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self.subject_filter = ctk.CTkComboBox(
            subject_frame,
            values=[ALL_SUBJECTS],
            width=230,
            height=34,
            state="readonly",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(11),
            dropdown_font=font(11),
            corner_radius=8,
            command=self._on_subject_filter,
        )
        self.subject_filter.pack(anchor="w")
        self.subject_filter.set(ALL_SUBJECTS)

        search_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        search_frame.grid(row=0, column=1, sticky="ew", padx=10, pady=(9, 8))
        ctk.CTkLabel(
            search_frame,
            text="Cerca",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._refresh_list(True))
        ctk.CTkEntry(
            search_frame,
            textvariable=self.search_var,
            height=34,
            placeholder_text="Titolo, materia o file .txt",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(11),
            corner_radius=8,
        ).pack(fill="x")

        ctk.CTkButton(
            toolbar,
            text="Apri cartella trascrizioni",
            height=32,
            width=174,
            font=font(9, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._open_transcripts_folder,
        ).grid(row=0, column=2, sticky="se", padx=18, pady=(0, 3))

    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=2, uniform="video")
        body.grid_columnconfigure(1, weight=3, uniform="video")

        left = Card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)
        list_header = ctk.CTkFrame(left, fg_color="transparent")
        list_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        list_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            list_header,
            text="Videolezioni",
            font=font(16, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.list_count = ctk.CTkLabel(
            list_header,
            text="0",
            font=font(10, "bold"),
            text_color=COLORS.text_faint,
        )
        self.list_count.grid(row=0, column=1, sticky="e")
        self.list_scroll = ctk.CTkScrollableFrame(
            left,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.list_scroll.grid(row=1, column=0, sticky="nsew", padx=(6, 2), pady=(0, 8))
        self.list_scroll.grid_columnconfigure(0, weight=1)

        right = Card(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.detail_host = ctk.CTkFrame(right, fg_color="transparent")
        self.detail_host.grid(row=0, column=0, sticky="nsew")
        self.detail_host.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        with render_state(self).only_if_changed():
            self.refresh()

    def _job_lesson_id(self) -> int | None:
        job = self.transcriptions.current_job()
        try:
            return int(job["lesson_id"]) if job else None
        except (KeyError, TypeError, ValueError):
            return None

    def _detail_signature(self) -> tuple:
        lesson = self.db.get_video_lesson(self._selected_id) if self._selected_id is not None else None
        files = []
        for key in ("video_path", "transcript_path"):
            value = str((lesson or {}).get(key) or "")
            try:
                stat = Path(value).stat() if value else None
                files.append((stat.st_mtime_ns, stat.st_size) if stat else None)
            except OSError:
                files.append(None)
        return (self._selected_id, lesson, tuple(files), self._job_lesson_id())

    def refresh(self) -> None:
        subjects = self.db.list_subject_tags()
        values = [ALL_SUBJECTS] + subjects
        if self._filter_subject not in values:
            self._filter_subject = ALL_SUBJECTS
        self.subject_filter.configure(values=values)
        self.subject_filter.set(self._filter_subject)
        self._lessons = self.db.list_video_lessons()
        self._render_summary()
        self._refresh_list(False)
        self._render_detail()

    def _render_summary(self) -> None:
        total = len(self._lessons)
        transcribed = sum(1 for lesson in self._lessons if lesson.get("transcript_path"))
        subjects = {str(lesson.get("subject") or "").strip() for lesson in self._lessons}
        subjects.discard("")
        self._summary_values["total"].configure(text=str(total))
        self._summary_values["done"].configure(text=str(transcribed))
        self._summary_values["subjects"].configure(text=str(len(subjects)))

    def _on_subject_filter(self, value: str) -> None:
        self._filter_subject = value or ALL_SUBJECTS
        self._refresh_list(True)

    def _filtered_lessons(self) -> list[dict[str, Any]]:
        lessons = self._lessons
        if self._filter_subject and self._filter_subject != ALL_SUBJECTS:
            lessons = [
                lesson
                for lesson in lessons
                if str(lesson.get("subject") or "").strip().casefold()
                == self._filter_subject.strip().casefold()
            ]
        query = self.search_var.get().strip().casefold()
        if query:
            lessons = [
                lesson
                for lesson in lessons
                if query
                in " ".join(
                    (
                        str(lesson.get("title") or ""),
                        str(lesson.get("subject") or ""),
                        Path(str(lesson.get("video_path") or "")).name,
                        str(lesson.get("transcript_name") or ""),
                    )
                ).casefold()
            ]
        return lessons

    def _refresh_list(self, update_detail: bool) -> None:
        lessons = self._filtered_lessons()
        self.list_count.configure(text=str(len(lessons)))
        visible_ids = {int(lesson["id"]) for lesson in lessons}
        previous = self._selected_id
        if self._selected_id not in visible_ids:
            self._selected_id = int(lessons[0]["id"]) if lessons else None
        if render_state(self).should_render("list", lessons, self._selected_id, self._job_lesson_id()):
            clear_children(self.list_scroll)
            if not lessons:
                empty = ctk.CTkFrame(self.list_scroll, fg_color="transparent")
                empty.grid(row=0, column=0, sticky="ew", padx=8, pady=18)
                ctk.CTkLabel(
                    empty,
                    text="Nessuna videolezione trovata.",
                    font=font(12, "bold"),
                    text_color=COLORS.text_muted,
                    anchor="w",
                ).pack(anchor="w")
                ctk.CTkLabel(
                    empty,
                    text="Aggiungi un video oppure modifica il filtro per materia.",
                    font=font(10),
                    text_color=COLORS.text_faint,
                    anchor="w",
                    wraplength=300,
                    justify="left",
                ).pack(anchor="w", pady=(3, 0))
            else:
                for index, lesson in enumerate(lessons):
                    self._lesson_card(lesson, index)
        if update_detail or previous != self._selected_id:
            self._render_detail()

    def _lesson_card(self, lesson: dict[str, Any], row: int) -> None:
        lesson_id = int(lesson["id"])
        selected = lesson_id == self._selected_id
        card = ctk.CTkFrame(
            self.list_scroll,
            fg_color=COLORS.surface_alt if selected else COLORS.surface,
            corner_radius=11,
            border_width=1,
            border_color=COLORS.accent if selected else COLORS.border,
        )
        card.grid(row=row, column=0, sticky="ew", pady=3, padx=2)
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(11, 2))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top,
            text=str(lesson.get("title") or "Video"),
            font=font(12, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=250,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        subject = str(lesson.get("subject") or "").strip()
        ctk.CTkLabel(
            top,
            text=subject or "Senza materia",
            font=font(8, "bold"),
            text_color=COLORS.accent if subject else COLORS.text_faint,
            fg_color=COLORS.accent_dark if subject else COLORS.surface,
            corner_radius=6,
            padx=7,
            height=20,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        active_job = self.transcriptions.current_job()
        is_active = bool(
            active_job and int(active_job.get("lesson_id", 0)) == lesson_id
        )
        status = "processing" if is_active else str(lesson.get("status") or "ready")
        transcript_path = str(lesson.get("transcript_path") or "")
        status_text = STATUS_LABELS.get(status, status)
        if transcript_path and status in {"error", "cancelled"}:
            status_text += " · .txt precedente disponibile"
        if transcript_path and status == "completed":
            status_text = str(lesson.get("transcript_name") or Path(transcript_path).name)
        status_color = {
            "completed": COLORS.green,
            "processing": COLORS.amber,
            "error": COLORS.red,
            "cancelled": COLORS.text_faint,
        }.get(status, COLORS.text_muted)
        ctk.CTkLabel(
            card,
            text=status_text,
            font=font(9),
            text_color=status_color,
            anchor="w",
            wraplength=290,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=14)

        if is_active or status == "processing":
            progress = (
                self._live_progress
                if is_active
                else max(0.0, min(1.0, safe_float(lesson.get("progress", 0))))
            )
            bar = ctk.CTkProgressBar(
                card,
                height=5,
                corner_radius=3,
                fg_color=COLORS.border,
                progress_color=COLORS.amber,
            )
            bar.grid(row=2, column=0, sticky="ew", padx=14, pady=(7, 11))
            bar.set(progress)
        else:
            ctk.CTkFrame(card, height=8, fg_color="transparent").grid(row=2, column=0)

        def select(_event=None, value=lesson_id) -> None:
            if value != self._selected_id:
                self._selected_id = value
                self._refresh_list(False)
                self._render_detail()

        self._bind_click_tree(card, select)

    @staticmethod
    def _bind_click_tree(widget, callback: Callable[..., None]) -> None:
        try:
            widget.bind("<Button-1>", callback)
        except Exception:
            pass
        for child in widget.winfo_children():
            VideoView._bind_click_tree(child, callback)

    # ----------------------------------------------------------------- detail
    def _render_detail(self) -> None:
        if not render_state(self).should_render("detail", *self._detail_signature()):
            return
        clear_children(self.detail_host)
        self._detail_progress = None
        self._detail_status = None
        self._transcript_box = None
        self._current_transcript_text = ""
        if self._selected_id is None:
            self._detail_empty(
                "Nessuna videolezione selezionata",
                "Aggiungi un video o seleziona una voce dall'archivio per vedere i dettagli.",
            )
            return
        lesson = self.db.get_video_lesson(self._selected_id)
        if lesson is None:
            self._selected_id = None
            self._detail_empty(
                "Videolezione non disponibile",
                "Il record potrebbe essere stato rimosso. Aggiorna l'archivio.",
            )
            return
        self._build_detail(lesson)

    def _detail_empty(self, title: str, subtitle: str) -> None:
        self.detail_host.grid_rowconfigure(0, weight=1)
        frame = ctk.CTkFrame(self.detail_host, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.grid(row=0, column=0)
        ctk.CTkLabel(
            inner,
            text="▷",
            font=font(34),
            text_color=COLORS.border,
        ).pack()
        ctk.CTkLabel(
            inner,
            text=title,
            font=font(15, "bold"),
            text_color=COLORS.text_muted,
        ).pack(pady=(8, 3))
        ctk.CTkLabel(
            inner,
            text=subtitle,
            font=font(10),
            text_color=COLORS.text_faint,
            wraplength=360,
            justify="center",
        ).pack()

    def _build_detail(self, lesson: dict[str, Any]) -> None:
        lesson_id = int(lesson["id"])
        active_job = self.transcriptions.current_job()
        is_active = bool(
            active_job and int(active_job.get("lesson_id", 0)) == lesson_id
        )
        video_path = Path(str(lesson.get("video_path") or ""))
        transcript_path = str(lesson.get("transcript_path") or "")
        transcript_file = Path(transcript_path) if transcript_path else None
        transcript_exists = bool(transcript_file and transcript_file.is_file())

        host = self.detail_host
        host.grid_columnconfigure(0, weight=1)
        for row in range(9):
            host.grid_rowconfigure(row, weight=1 if row == 8 else 0)

        heading = ctk.CTkFrame(host, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 0))
        heading.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            heading,
            text=str(lesson.get("title") or "Video"),
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        subject = str(lesson.get("subject") or "").strip()
        ctk.CTkLabel(
            heading,
            text=subject or "Senza materia",
            font=font(9, "bold"),
            text_color=COLORS.accent if subject else COLORS.text_faint,
            fg_color=COLORS.accent_dark if subject else COLORS.surface_alt,
            corner_radius=7,
            padx=9,
            height=23,
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))

        meta_parts = []
        if video_path.name:
            meta_parts.append(video_path.name)
        duration = safe_float(lesson.get("duration_seconds", 0))
        if duration > 0:
            meta_parts.append(f"Durata audio {format_clock(duration)}")
        created = str(lesson.get("created_at") or "")
        if created:
            meta_parts.append(f"Aggiunto il {created[:10]}")
        meta_text = "  ·  ".join(meta_parts) or "Metadati non disponibili"
        if not video_path.is_file():
            meta_text += "  ·  video non trovato"
        ctk.CTkLabel(
            host,
            text=meta_text,
            font=font(10),
            text_color=COLORS.red if not video_path.is_file() else COLORS.text_faint,
            anchor="w",
            wraplength=430,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(2, 6))

        actions = ctk.CTkFrame(host, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 6))
        self._action_button(
            actions,
            "Apri video",
            96,
            lambda: self._open_file(str(video_path), "Video"),
            primary=False,
        ).pack(side="left", padx=(0, 6))
        self._action_button(
            actions,
            "Apri cartella",
            108,
            lambda: self._reveal(video_path),
            primary=False,
        ).pack(side="left", padx=6)
        self._action_button(
            actions,
            "Modifica",
            86,
            lambda: self._open_video_dialog(dict(lesson)),
            primary=False,
        ).pack(side="left", padx=6)
        self._action_button(
            actions,
            "Rimuovi",
            80,
            lambda: self._remove_lesson(dict(lesson)),
            primary=False,
            danger=True,
        ).pack(side="right")

        separator(host).grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 8))

        transcript_header = ctk.CTkFrame(host, fg_color="transparent")
        transcript_header.grid(row=4, column=0, sticky="ew", padx=20)
        transcript_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            transcript_header,
            text="Trascrizione",
            font=font(15, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        transcript_label = (
            Path(transcript_path).name
            if transcript_path
            else str(lesson.get("transcript_name") or "Nessun file .txt")
        )
        ctk.CTkLabel(
            transcript_header,
            text=transcript_label,
            font=font(9, "bold"),
            text_color=COLORS.green if transcript_exists else COLORS.text_faint,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        status = "processing" if is_active else str(lesson.get("status") or "ready")
        if transcript_exists and status in {"completed", "error", "cancelled"}:
            status_text = STATUS_LABELS.get(status, status)
            if status != "completed":
                status_text += " · è disponibile il file precedente"
        elif is_active:
            status_text = self._live_message or "Elaborazione in corso..."
        else:
            status_text = STATUS_LABELS.get(status, status)
            if status == "error" and lesson.get("error"):
                status_text = str(lesson["error"])
        status_color = {
            "completed": COLORS.green,
            "processing": COLORS.amber,
            "error": COLORS.red,
            "cancelled": COLORS.text_faint,
        }.get(status, COLORS.text_muted)
        if is_active and self._live_phase == "transcribe":
            status_color = COLORS.accent
        self._detail_status = ctk.CTkLabel(
            host,
            text=status_text,
            font=font(10),
            text_color=status_color,
            anchor="w",
            wraplength=430,
            justify="left",
        )
        self._detail_status.grid(row=5, column=0, sticky="ew", padx=20, pady=(3, 3))

        progress_value = self._progress_for(lesson, is_active)
        show_progress = (
            is_active
            or status == "processing"
            or (status in {"error", "cancelled"} and progress_value > 0)
        )
        if show_progress:
            self._detail_progress = ctk.CTkProgressBar(
                host,
                height=6,
                corner_radius=3,
                fg_color=COLORS.surface_alt,
                progress_color=COLORS.amber if is_active else COLORS.accent,
            )
            self._detail_progress.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 8))
            self._detail_progress.set(progress_value)

        transcript_actions = ctk.CTkFrame(host, fg_color="transparent")
        transcript_actions.grid(row=7, column=0, sticky="new", padx=20, pady=(0, 6))
        if is_active:
            self._action_button(
                transcript_actions,
                "Annulla trascrizione",
                150,
                lambda: self._cancel_transcription(lesson_id),
                primary=False,
                danger=True,
            ).grid(row=0, column=0, sticky="w")
        else:
            self._action_button(
                transcript_actions,
                "Ripeti trascrizione" if transcript_exists else "Trascrivi",
                132,
                lambda: self._open_transcribe_dialog(dict(lesson)),
                primary=True,
            ).grid(row=0, column=0, sticky="w")
        if transcript_exists and not is_active:
            self._action_button(
                transcript_actions,
                "Rinomina .txt",
                102,
                lambda: self._rename_transcript(dict(lesson)),
                primary=False,
            ).grid(row=0, column=1, sticky="w", padx=6)
            self._action_button(
                transcript_actions,
                "Apri .txt",
                72,
                lambda: self._open_file(transcript_path, "Trascrizione"),
                primary=False,
            ).grid(row=0, column=2, sticky="w", padx=6)
            self._action_button(
                transcript_actions,
                "Copia testo",
                86,
                self._copy_transcript,
                primary=False,
            ).grid(row=0, column=3, sticky="w", padx=6)
        if self.transcriptions.is_running() and not is_active:
            ctk.CTkLabel(
                transcript_actions,
                text="Un'altra trascrizione è in corso: attendi o annullala per avviarne una nuova.",
                font=font(9),
                text_color=COLORS.amber,
                anchor="w",
            ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        self._transcript_box = ctk.CTkTextbox(
            host,
            wrap="word",
            height=100,
            fg_color=COLORS.surface_alt,
            border_width=0,
            corner_radius=10,
            text_color=COLORS.text,
            font=mono(11),
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self._transcript_box.grid(row=8, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self._current_transcript_text = self._load_transcript_text(
            transcript_file, transcript_exists, is_active
        )
        self._transcript_box.insert("1.0", self._current_transcript_text)
        self._transcript_box.configure(state="disabled")

    def _action_button(
        self,
        parent,
        text: str,
        width: int,
        command: Callable[[], None],
        *,
        primary: bool = False,
        danger: bool = False,
    ) -> ctk.CTkButton:
        if primary:
            fg = COLORS.accent
            hover = COLORS.accent_hover
            text_color = COLORS.bg
            border = COLORS.accent
        elif danger:
            fg = "transparent"
            hover = COLORS.red_dark
            text_color = COLORS.red
            border = COLORS.border
        else:
            fg = "transparent"
            hover = COLORS.surface_alt
            text_color = COLORS.text_muted
            border = COLORS.border
        return ctk.CTkButton(
            parent,
            text=text,
            height=32,
            width=width,
            font=font(10, "bold" if primary else "normal"),
            fg_color=fg,
            hover_color=hover,
            border_width=1,
            border_color=border,
            text_color=text_color,
            corner_radius=8,
            command=command,
        )

    def _progress_for(self, lesson: dict[str, Any], is_active: bool) -> float:
        if is_active:
            return max(0.0, min(1.0, self._live_progress))
        status = str(lesson.get("status") or "ready")
        if status == "completed":
            return 1.0
        return max(0.0, min(1.0, safe_float(lesson.get("progress", 0))))

    def _load_transcript_text(
        self,
        transcript_file: Path | None,
        exists: bool,
        is_active: bool,
    ) -> str:
        if transcript_file is not None and exists:
            try:
                text = transcript_file.read_text(encoding="utf-8", errors="replace")
                if len(text) > 1_500_000:
                    text = text[:1_500_000] + "\n\n[Anteprima troncata: apri il file .txt per il testo completo.]"
                return text
            except OSError as exc:
                return f"Impossibile leggere il file .txt:\n{exc}"
        if is_active:
            return "La trascrizione è in corso. Il testo comparirà al termine dell'elaborazione."
        return (
            "Nessuna trascrizione disponibile.\n\n"
            "Premi «Trascrivi» per estrarre l'audio con FFmpeg e generare il file .txt "
            "con timestamp [mm:ss]. Il modello viene eseguito in locale."
        )

    # --------------------------------------------------------------- actions
    def _open_video_dialog(self, lesson: dict[str, Any] | None) -> None:
        VideoDialog(self, lesson)

    def _open_transcribe_dialog(self, lesson: dict[str, Any]) -> None:
        if self.transcriptions.is_running():
            job = self.transcriptions.current_job()
            if job and int(job.get("lesson_id", 0)) == int(lesson["id"]):
                self.app.toast("La trascrizione di questo video è già in corso.")
            else:
                messagebox.showwarning(
                    "Trascrizione in corso",
                    "Attendi la fine o annulla la trascrizione attiva prima di avviarne un'altra.",
                    parent=self.winfo_toplevel(),
                )
            return
        TranscribeDialog(self, lesson)

    def start_transcription(
        self,
        lesson_id: int,
        transcript_name: str,
        model: str,
        language: str,
        subject: str,
    ) -> bool:
        if self.transcriptions.is_running():
            messagebox.showwarning(
                "Trascrizione in corso",
                "E' già attiva una trascrizione. Attendi o annullala.",
                parent=self.winfo_toplevel(),
            )
            return False
        lesson = self.db.get_video_lesson(lesson_id)
        if lesson is None:
            messagebox.showerror(
                "Video non trovato", "La videolezione non è più nell'archivio.",
                parent=self.winfo_toplevel(),
            )
            return False
        video_path = Path(str(lesson.get("video_path") or ""))
        if not video_path.is_file():
            messagebox.showerror(
                "Video non trovato",
                f"Il file video non esiste più:\n{video_path}",
                parent=self.winfo_toplevel(),
            )
            return False
        target = self.transcripts_dir_for(subject) / transcript_filename(
            transcript_name, video_path.stem or "Trascrizione"
        )
        current = Path(str(lesson.get("transcript_path") or ""))
        if target.exists() and (not current.is_file() or target.resolve() != current.resolve()):
            if not messagebox.askyesno(
                "File di trascrizione esistente",
                f"Esiste già '{target.name}'.\n\nSostituirlo al termine della trascrizione?",
                parent=self.winfo_toplevel(),
            ):
                return False
        if target.exists() and current.is_file() and target.resolve() == current.resolve():
            if not messagebox.askyesno(
                "Ripetere la trascrizione?",
                "Il file .txt attuale verrà sostituito soltanto se la nuova "
                "trascrizione termina correttamente. Continuare?",
                parent=self.winfo_toplevel(),
            ):
                return False
        try:
            self.db.update_video_lesson_metadata(
                lesson_id, str(lesson.get("title") or video_path.stem), subject
            )
            self.db.set_video_lesson_job(lesson_id, "processing", 0.0, "")
        except Exception as exc:
            self.app.logger.exception("Preparazione trascrizione non riuscita.")
            messagebox.showerror("Errore", str(exc), parent=self.winfo_toplevel())
            return False
        self._active_lesson_id = lesson_id
        self._live_progress = 0.0
        self._live_message = "Avvio della trascrizione..."
        self._live_phase = "model"
        self._last_db_progress = time.monotonic()
        ok, error = self.transcriptions.start(
            lesson_id=lesson_id,
            video_path=str(video_path),
            transcript_path=str(target),
            model=model,
            language=language,
            title=str(lesson.get("title") or video_path.stem),
            subject=subject,
            on_progress=self._queue_progress,
            on_done=self._queue_done,
        )
        if not ok:
            self._active_lesson_id = None
            self._live_message = ""
            self.db.set_video_lesson_job(lesson_id, "error", 0.0, error)
            self.refresh()
            messagebox.showerror("Trascrizione non avviata", error, parent=self.winfo_toplevel())
            return False
        self.refresh()
        self.app.toast("Trascrizione avviata in background.")
        return True

    def _queue_progress(self, event: dict[str, Any]) -> None:
        self.app.post(lambda value=dict(event): self._on_job_progress(value))

    def _queue_done(self, event: dict[str, Any]) -> None:
        self.app.post(lambda value=dict(event): self._on_job_done(value))

    def _on_job_progress(self, event: dict[str, Any]) -> None:
        lesson_id = int(event.get("lesson_id", 0) or 0)
        job = self.transcriptions.current_job()
        if job is None or int(job.get("lesson_id", 0)) != lesson_id:
            return
        self._active_lesson_id = lesson_id
        self._live_progress = max(0.0, min(1.0, safe_float(event.get("percent", 0))))
        self._live_message = str(event.get("message") or "Elaborazione in corso...")
        self._live_phase = str(event.get("phase") or "")
        if self._selected_id == lesson_id and self._detail_status is not None:
            try:
                self._detail_status.configure(
                    text=self._live_message,
                    text_color=(
                        COLORS.accent if self._live_phase == "transcribe" else COLORS.amber
                    ),
                )
                if self._detail_progress is not None:
                    self._detail_progress.set(self._live_progress)
            except Exception:
                pass
        now = time.monotonic()
        if now - self._last_db_progress >= 1.0:
            self._last_db_progress = now
            try:
                self.db.set_video_lesson_job(
                    lesson_id, "processing", self._live_progress, ""
                )
            except Exception:
                self.app.logger.exception("Salvataggio avanzamento trascrizione non riuscito.")

    def _on_job_done(self, event: dict[str, Any]) -> None:
        lesson_id = int(event.get("lesson_id", 0) or self._active_lesson_id or 0)
        self._active_lesson_id = None
        self._live_message = ""
        self._live_phase = ""
        if not lesson_id:
            return
        if event.get("ok"):
            result = event.get("result") or {}
            try:
                self.db.complete_video_lesson(
                    lesson_id,
                    str(result.get("transcript_path") or ""),
                    str(result.get("transcript_name") or ""),
                    safe_float(result.get("duration_seconds", 0)),
                    str(result.get("model") or ""),
                    str(result.get("language") or ""),
                )
            except Exception:
                self.app.logger.exception("Salvataggio esito trascrizione non riuscito.")
                self.app.toast("Trascrizione completata, ma il salvataggio dei metadati è fallito.")
            else:
                self.app.toast("Trascrizione completata e salvata.")
            subjects_view = self.app.views.get("subjects")
            if subjects_view is not None and hasattr(subjects_view, "refresh_if_visible"):
                subjects_view.refresh_if_visible()
            self.refresh()
            self._selected_id = lesson_id
            self._refresh_list(False)
            self._render_detail()
            return
        cancelled = bool(event.get("cancelled"))
        error = str(event.get("error") or "Trascrizione non riuscita.")
        if event.get("detail"):
            self.app.logger.error("Dettaglio trascrizione:\n%s", event.get("detail"))
        lesson = self.db.get_video_lesson(lesson_id)
        progress = safe_float(lesson.get("progress", 0)) if lesson else 0.0
        try:
            self.db.set_video_lesson_job(
                lesson_id,
                "cancelled" if cancelled else "error",
                progress,
                "" if cancelled else error,
            )
        except Exception:
            self.app.logger.exception("Salvataggio errore trascrizione non riuscito.")
        self.refresh()
        self._selected_id = lesson_id
        self._refresh_list(False)
        self._render_detail()
        if cancelled:
            self.app.toast("Trascrizione annullata.")
        else:
            self.app.toast("Trascrizione non riuscita.")
            if self.app.is_view_visible(self):
                messagebox.showerror(
                    "Trascrizione non riuscita",
                    error,
                    parent=self.winfo_toplevel(),
                )

    def _cancel_transcription(self, lesson_id: int) -> None:
        job = self.transcriptions.current_job()
        if job is None or int(job.get("lesson_id", 0)) != lesson_id:
            return
        if not messagebox.askyesno(
            "Annullare la trascrizione?",
            "Il processo in background verrà interrotto. Il file .txt precedente, "
            "se presente, non verrà modificato.",
            parent=self.winfo_toplevel(),
        ):
            return
        self.transcriptions.cancel()
        self._live_message = "Annullamento in corso..."
        if self._detail_status is not None:
            self._detail_status.configure(text=self._live_message, text_color=COLORS.amber)
        self.app.toast("Richiesto annullamento della trascrizione.")

    def _rename_transcript(self, lesson: dict[str, Any]) -> None:
        current_path = str(lesson.get("transcript_path") or "")
        if not current_path or not Path(current_path).is_file():
            messagebox.showerror(
                "File non trovato",
                "La trascrizione non è più disponibile nel percorso indicato.",
                parent=self.winfo_toplevel(),
            )
            return
        current_name = Path(current_path).stem
        value = simpledialog.askstring(
            "Rinomina trascrizione",
            "Nuovo nome del file .txt:",
            parent=self.winfo_toplevel(),
            initialvalue=current_name,
        )
        if value is None:
            return
        try:
            new_path = rename_transcript_file(
                current_path, value, Path(current_path).parent
            )
            self.db.update_video_transcript_path(
                int(lesson["id"]), str(new_path), new_path.name
            )
        except (TranscriptionError, OSError) as exc:
            messagebox.showerror("Rinomina non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        self.refresh()
        self._selected_id = int(lesson["id"])
        self._refresh_list(False)
        self._render_detail()
        self.app.toast(f"Trascrizione rinominata in {new_path.name}.")

    def _copy_transcript(self) -> None:
        text = self._current_transcript_text.strip()
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._current_transcript_text)
            self.update_idletasks()
        except Exception as exc:
            messagebox.showerror("Copia non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        self.app.toast("Testo della trascrizione copiato negli appunti.")

    def _remove_lesson(self, lesson: dict[str, Any]) -> None:
        lesson_id = int(lesson["id"])
        job = self.transcriptions.current_job()
        if job is not None and int(job.get("lesson_id", 0)) == lesson_id:
            messagebox.showwarning(
                "Elaborazione in corso",
                "Annulla la trascrizione prima di rimuovere questa videolezione.",
                parent=self.winfo_toplevel(),
            )
            return
        if not messagebox.askyesno(
            "Rimuovere dall'archivio?",
            f"Rimuovere '{lesson.get('title', 'Video')}' dall'archivio StudyFlow?\n\n"
            "Il file video e il file .txt non verranno cancellati dal disco.",
            parent=self.winfo_toplevel(),
        ):
            return
        self.db.delete_video_lesson(lesson_id)
        if self._selected_id == lesson_id:
            self._selected_id = None
        self.refresh()
        self.app.toast("Videolezione rimossa dall'archivio.")

    def _open_file(self, path: str, label: str) -> None:
        if not path:
            messagebox.showwarning(f"{label} non impostato", f"Nessun file {label.lower()} associato.", parent=self.winfo_toplevel())
            return
        target = Path(path)
        if not target.is_file():
            messagebox.showerror(
                f"{label} non trovato",
                f"Il file non esiste più nel percorso indicato:\n{path}",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Impossibile aprire il file", str(exc), parent=self.winfo_toplevel())

    def _reveal(self, path: Path) -> None:
        try:
            if path.exists():
                if os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", str(path)])
                else:
                    os.startfile(str(path.parent))  # type: ignore[attr-defined]
            else:
                self._open_transcripts_folder() if path.suffix.lower() == ".txt" else os.startfile(str(path.parent))
        except (OSError, AttributeError):
            if path.parent.exists():
                try:
                    os.startfile(str(path.parent))  # type: ignore[attr-defined]
                except (OSError, AttributeError):
                    messagebox.showinfo("Cartella", str(path.parent), parent=self.winfo_toplevel())

    def transcripts_dir_for(self, subject: str) -> Path:
        """Cartella ``Trascrizioni`` della materia, oppure quella generale se manca la materia."""
        folder = self.app.subject_folder(subject, KIND_TRANSCRIPTS) if str(subject or "").strip() else None
        return folder or self.transcripts_dir

    def relocate_transcript(self, lesson_id: int, subject: str) -> None:
        """Sposta il .txt di una videolezione nella cartella della sua materia."""
        lesson = self.db.get_video_lesson(lesson_id)
        if lesson is None or self._job_lesson_id() == lesson_id:
            return
        current = Path(str(lesson.get("transcript_path") or ""))
        if not str(lesson.get("transcript_path") or "").strip() or not current.is_file():
            return
        target_dir = self.transcripts_dir_for(subject)
        try:
            moved = move_file_into(current, target_dir)
        except OSError:
            self.app.logger.exception("Spostamento della trascrizione non riuscito.")
            self.app.toast("Impossibile spostare la trascrizione nella cartella della materia.")
            return
        if moved != current:
            self.db.update_video_transcript_path(lesson_id, str(moved), moved.name)

    def focus_item(self, lesson_id: int | None = None, subject: str | None = None) -> None:
        """Usato dalla pagina Materie: filtra per materia e seleziona la videolezione."""
        if subject is not None:
            self._filter_subject = subject or ALL_SUBJECTS
        if lesson_id is not None:
            self._selected_id = int(lesson_id)
        render_state(self).forget()
        self.refresh()

    def _open_transcripts_folder(self) -> None:
        subject = "" if self._filter_subject == ALL_SUBJECTS else self._filter_subject
        folder = self.transcripts_dir_for(subject)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo(
                "Cartella trascrizioni", str(folder), parent=self.winfo_toplevel()
            )


class VideoDialog(ctk.CTkToplevel):
    def __init__(self, view: VideoView, lesson: dict[str, Any] | None):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.lesson = lesson
        self.title("Modifica videolezione" if lesson else "Aggiungi videolezione")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 660, 550)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self.destroy())
        if lesson:
            self._populate()

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Modifica videolezione" if self.lesson else "Aggiungi videolezione",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(24, 2))
        ctk.CTkLabel(
            self,
            text="Il video resta nella sua posizione originale: StudyFlow archivia il percorso e la trascrizione.",
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=600,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 18))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure(0, weight=1)

        self.title_var = ctk.StringVar()
        self.subject_var = ctk.StringVar()
        self.file_var = ctk.StringVar()
        self._field(form, 0, "Titolo", self.title_var, "es. Lezione 3 - Derivate")

        file_frame = ctk.CTkFrame(form, fg_color="transparent")
        file_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        file_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            file_frame,
            text="File video",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.file_entry = ctk.CTkEntry(
            file_frame,
            textvariable=self.file_var,
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        self.file_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.browse_button = ctk.CTkButton(
            file_frame,
            text="Sfoglia",
            width=92,
            height=38,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=9,
            command=self._browse,
        )
        self.browse_button.grid(row=1, column=1)

        subject_frame = ctk.CTkFrame(form, fg_color="transparent")
        subject_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        subject_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            subject_frame,
            text="Materia / Corso (Calendario Materia o tag)",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        suggestions = self.db.list_subject_tags() or [""]
        self.subject_combo = ctk.CTkComboBox(
            subject_frame,
            values=suggestions,
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(12),
            dropdown_font=font(11),
            corner_radius=9,
            variable=self.subject_var,
        )
        self.subject_combo.pack(fill="x")

        info = Card(self)
        info.grid(row=3, column=0, sticky="ew", padx=28, pady=(4, 16))
        ctk.CTkLabel(
            info,
            text=(
                "La materia può essere scelta tra quelle importate dal calendario, "
                "dai materiali o dai video già archiviati. Puoi anche digitare un nuovo tag. "
                "La trascrizione predefinita si chiama come il file video e può essere "
                "personalizzata prima e dopo l'elaborazione."
            ),
            font=font(10),
            text_color=COLORS.text_muted,
            wraplength=580,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=16, pady=12)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=28, pady=(0, 24))
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            buttons,
            text="Annulla",
            height=40,
            width=110,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self.destroy,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            buttons,
            text="Salva videolezione",
            height=40,
            width=172,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._save,
        ).grid(row=0, column=1, sticky="e")

    def _field(self, parent, row: int, label: str, variable: ctk.StringVar, placeholder: str) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(
            frame,
            text=label,
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        ctk.CTkEntry(
            frame,
            textvariable=variable,
            height=38,
            placeholder_text=placeholder,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        ).pack(fill="x")

    def _populate(self) -> None:
        assert self.lesson is not None
        self.title_var.set(str(self.lesson.get("title") or ""))
        self.file_var.set(str(self.lesson.get("video_path") or ""))
        self.subject_var.set(str(self.lesson.get("subject") or ""))
        self.subject_combo.set(str(self.lesson.get("subject") or ""))
        self.file_entry.configure(state="disabled")
        self.browse_button.configure(state="disabled")

    def _browse(self) -> None:
        patterns = " ".join(f"*{extension}" for extension in sorted(SUPPORTED_VIDEO_EXTENSIONS))
        path = filedialog.askopenfilename(
            parent=self,
            title="Seleziona una videolezione",
            filetypes=[
                ("Video", patterns),
                ("Tutti i file", "*.*"),
            ],
        )
        if path:
            self.file_var.set(path)
            if not self.title_var.get().strip():
                self.title_var.set(Path(path).stem)

    def _save(self) -> None:
        file_path = self.file_var.get().strip()
        title = self.title_var.get().strip() or (Path(file_path).stem if file_path else "")
        subject = self.subject_var.get().strip()
        if not title:
            messagebox.showerror("Titolo mancante", "Inserisci un titolo per la videolezione.", parent=self)
            return
        if not file_path:
            messagebox.showerror("Video mancante", "Seleziona un file video.", parent=self)
            return
        path = Path(file_path)
        if not path.is_file():
            if not messagebox.askyesno(
                "File non trovato",
                "Il file video indicato non esiste. Salvarlo comunque?",
                parent=self,
            ):
                return
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            if not messagebox.askyesno(
                "Formato non standard",
                "L'estensione del file non è tra quelle video supportate. "
                "FFmpeg potrebbe comunque riuscire a leggerlo. Continuare?",
                parent=self,
            ):
                return
        try:
            if self.lesson:
                self.db.update_video_lesson_metadata(int(self.lesson["id"]), title, subject)
                lesson_id = int(self.lesson["id"])
                if str(self.lesson.get("subject") or "").strip() != subject:
                    self.view.relocate_transcript(lesson_id, subject)
            else:
                duplicate = any(
                    Path(str(item.get("video_path") or "")).resolve() == path.resolve()
                    for item in self.db.list_video_lessons()
                )
                if duplicate and not messagebox.askyesno(
                    "Video già presente",
                    "Questo file è già presente nell'archivio. Aggiungerlo di nuovo?",
                    parent=self,
                ):
                    return
                lesson_id = self.db.add_video_lesson(
                    title,
                    str(path),
                    subject,
                    default_transcript_name(path),
                    DEFAULT_MODEL,
                    DEFAULT_LANGUAGE,
                )
        except Exception as exc:
            self.app.logger.exception("Salvataggio videolezione non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        if subject:
            self.app.subject_folder(subject)
        self.grab_release()
        self.destroy()
        self.view._selected_id = lesson_id
        self.view.refresh()
        self.view._refresh_list(False)
        self.view._render_detail()
        self.app.toast("Videolezione salvata.")


class TranscribeDialog(ctk.CTkToplevel):
    def __init__(self, view: VideoView, lesson: dict[str, Any]):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.lesson = lesson
        self._model_labels = {
            key: str(value.get("label") or key) for key, value in MODEL_CATALOG.items()
        }
        self._model_keys = {label: key for key, label in self._model_labels.items()}
        self._language_labels = {label: code for label, code in LANGUAGE_CHOICES.items()}
        self._language_codes = {code: label for label, code in LANGUAGE_CHOICES.items()}
        self.title("Trascrivi videolezione")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 680, 650)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self.destroy())

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Trascrivi videolezione",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(24, 2))
        ctk.CTkLabel(
            self,
            text=(
                "FFmpeg estrae l'audio in WAV mono 16 kHz, poi whisper.cpp trascrive "
                "in locale. L'elaborazione gira in background."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=620,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 18))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure((0, 1), weight=1, uniform="transcribe")

        self.name_var = ctk.StringVar()
        self.subject_var = ctk.StringVar(value=str(self.lesson.get("subject") or ""))
        self.model_var = ctk.StringVar()
        self.language_var = ctk.StringVar()

        name_frame = ctk.CTkFrame(form, fg_color="transparent")
        name_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        ctk.CTkLabel(
            name_frame,
            text="Nome del file .txt",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        ctk.CTkEntry(
            name_frame,
            textvariable=self.name_var,
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        ).pack(fill="x")
        current_transcript = str(self.lesson.get("transcript_path") or "")
        initial_name = (
            Path(current_transcript).stem
            if current_transcript
            else Path(str(self.lesson.get("video_path") or "")).stem
        )
        self.name_var.set(initial_name or "Trascrizione")

        subject_frame = ctk.CTkFrame(form, fg_color="transparent")
        subject_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        ctk.CTkLabel(
            subject_frame,
            text="Materia / Corso",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        self.subject_combo = ctk.CTkComboBox(
            subject_frame,
            values=self.db.list_subject_tags() or [""],
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(12),
            dropdown_font=font(11),
            corner_radius=9,
            variable=self.subject_var,
        )
        self.subject_combo.pack(fill="x")

        model_frame = ctk.CTkFrame(form, fg_color="transparent")
        model_frame.grid(row=2, column=0, sticky="ew", padx=(0, 7), pady=(0, 8))
        ctk.CTkLabel(
            model_frame,
            text="Modello whisper.cpp",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        self.model_combo = ctk.CTkComboBox(
            model_frame,
            values=list(self._model_labels.values()),
            height=38,
            state="readonly",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(10),
            dropdown_font=font(10),
            corner_radius=9,
            command=lambda _value: self._update_model_info(),
        )
        self.model_combo.pack(fill="x")

        language_frame = ctk.CTkFrame(form, fg_color="transparent")
        language_frame.grid(row=2, column=1, sticky="ew", padx=(7, 0), pady=(0, 8))
        ctk.CTkLabel(
            language_frame,
            text="Lingua",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        self.language_combo = ctk.CTkComboBox(
            language_frame,
            values=list(self._language_labels.keys()),
            height=38,
            state="readonly",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(11),
            dropdown_font=font(11),
            corner_radius=9,
        )
        self.language_combo.pack(fill="x")

        settings = self.db.get_settings()
        selected_model = str(settings.get("transcription_model") or DEFAULT_MODEL)
        if selected_model not in self._model_labels:
            selected_model = DEFAULT_MODEL
        self.model_combo.set(self._model_labels[selected_model])
        selected_language = str(settings.get("transcription_language") or DEFAULT_LANGUAGE)
        if selected_language not in self._language_codes:
            selected_language = DEFAULT_LANGUAGE
        self.language_combo.set(self._language_codes[selected_language])

        self.model_info = ctk.CTkLabel(
            form,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=610,
            justify="left",
        )
        self.model_info.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.ffmpeg_info = ctk.CTkLabel(
            form,
            text="",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=610,
            justify="left",
        )
        self.ffmpeg_info.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        info = Card(self)
        info.grid(row=3, column=0, sticky="ew", padx=28, pady=(2, 16))
        ctk.CTkLabel(
            info,
            text=(
                "La barra di avanzamento durante la trascrizione è calcolata sulla "
                "durata audio già elaborata rispetto alla durata totale. Il modello "
                "viene scaricato una sola volta nella cartella dati e non viene "
                "incluso nell'eseguibile. La trascrizione include timestamp [mm:ss]."
            ),
            font=font(10),
            text_color=COLORS.text_muted,
            wraplength=600,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=16, pady=12)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=28, pady=(0, 24))
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            buttons,
            text="Annulla",
            height=40,
            width=110,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self.destroy,
        ).grid(row=0, column=0, sticky="w")
        self.start_button = ctk.CTkButton(
            buttons,
            text="Avvia trascrizione",
            height=40,
            width=178,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._start,
        )
        self.start_button.grid(row=0, column=1, sticky="e")

        self._update_model_info()
        ffmpeg = find_ffmpeg(self.app.paths["sounds"])
        if ffmpeg is None:
            self.ffmpeg_info.configure(
                text=(
                    "FFmpeg non trovato. Installalo con 'winget install Gyan.FFmpeg' "
                    "oppure copia ffmpeg.exe nella cartella dati, poi riapri questa finestra."
                ),
                text_color=COLORS.red,
            )
            self.start_button.configure(state="disabled")
        else:
            self.ffmpeg_info.configure(
                text=f"FFmpeg pronto: {ffmpeg}", text_color=COLORS.green
            )

    def _selected_model(self) -> str:
        return self._model_keys.get(self.model_combo.get(), DEFAULT_MODEL)

    def _update_model_info(self) -> None:
        model = self._selected_model()
        if model_is_available(self.view.transcriptions.models_dir, model):
            text = f"{model_label(model)}: modello già disponibile sul PC."
            color = COLORS.green
        else:
            expected = int(MODEL_CATALOG[model]["bytes"]) / (1024 * 1024)
            text = (
                f"{model_label(model)}: verrà scaricato ora (circa {expected:.0f} MB, "
                "una sola volta)."
            )
            color = COLORS.text_faint
        self.model_info.configure(text=text, text_color=color)

    def _start(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Nome mancante", "Inserisci un nome per il file .txt.", parent=self)
            return
        model = self._selected_model()
        language = self._language_labels.get(self.language_combo.get(), DEFAULT_LANGUAGE)
        subject = self.subject_var.get().strip()
        try:
            self.db.set_settings(
                {
                    "transcription_model": model,
                    "transcription_language": language,
                }
            )
        except Exception:
            self.app.logger.exception("Salvataggio impostazioni trascrizione non riuscito.")
        self.grab_release()
        self.destroy()
        self.view.start_transcription(
            int(self.lesson["id"]), name, model, language, subject
        )
