"""Vista Materie: pagina dedicata per ogni materia, CFU ed esami."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..services.subjects import (
    MAX_CFU,
    cfu_to_hours,
    exam_countdown,
    format_cfu,
    format_exam_date,
    parse_exam_date,
)
from ..utils import format_hours
from .materials import ORPHAN_SUBJECT, ORPHAN_SUBJECT_KEY, MaterialsSection
from .theme import font
from .widgets import (
    Card,
    PageHeader,
    StatCard,
    center_window,
    clear_children,
    set_window_icon,
    render_state,
)


LAST_SUBJECT_SETTING = "subjects_last_selected"

COUNTDOWN_COLORS = {
    "undefined": COLORS.text_muted,
    "upcoming": COLORS.accent,
    "today": COLORS.amber,
    "past": COLORS.red,
}


class SubjectsView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self._selected: str | None = self._load_last_selection()
        self._search_job: str | None = None
        self._countdown_job: str | None = None
        self._countdown_label: ctk.CTkLabel | None = None
        self._countdown_date = ""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_body()

    # ------------------------------------------------------------------ layout
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.grid_columnconfigure(0, weight=1)
        PageHeader(
            header,
            "Materie",
            "Una pagina per ogni materia: docente, CFU, monte ore ed esame.",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header,
            text="+  Nuova materia",
            height=40,
            width=160,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=lambda: self._open_dialog(None),
        ).grid(row=0, column=1, sticky="e", pady=(4, 0))

    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        # Barra di selezione compatta: il menu a tendina sostituisce la
        # vecchia colonna con l'elenco completo, lasciando tutta la
        # larghezza alla pagina della materia.
        bar = Card(body)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        bar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            bar,
            text="MATERIA",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(18, 10), pady=12)
        self.subject_combo = ctk.CTkComboBox(
            bar,
            values=[""],
            width=320,
            height=36,
            state="readonly",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(12, "bold"),
            dropdown_font=font(11),
            corner_radius=9,
            command=self._on_combo,
        )
        self.subject_combo.grid(row=0, column=1, sticky="w", pady=12)

        info = ctk.CTkFrame(bar, fg_color="transparent")
        info.grid(row=0, column=2, sticky="w", padx=(14, 10))
        self.subject_meta = ctk.CTkLabel(
            info,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.subject_meta.pack(side="left")
        self.subject_badge = ctk.CTkLabel(
            info,
            text="",
            font=font(9, "bold"),
            text_color=COLORS.text_muted,
            fg_color=COLORS.surface_alt,
            corner_radius=6,
            padx=7,
            height=20,
        )
        self.subject_badge.pack(side="left", padx=(10, 0))

        self.search_entry = ctk.CTkEntry(
            bar,
            width=240,
            height=36,
            placeholder_text="Cerca materia, docente o email...",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(11),
            corner_radius=9,
        )
        self.search_entry.grid(row=0, column=3, sticky="e", padx=(0, 14), pady=12)
        self.search_entry.bind("<KeyRelease>", self._on_search)
        self._combo_map: dict[str, str] = {}

        self.detail_scroll = ctk.CTkScrollableFrame(
            body,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.detail_scroll.grid(row=1, column=0, sticky="nsew")
        self.detail_scroll.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        # Ricostruisce lista e dettaglio solo se i dati sono cambiati da
        # quando la sezione e' stata disegnata l'ultima volta.
        with render_state(self).only_if_changed():
            self.refresh()
        self._start_countdown()

    def on_hide(self) -> None:
        self._stop_countdown()

    def refresh_if_visible(self) -> None:
        if self.app.is_view_visible(self):
            self.refresh()

    def refresh(self) -> None:
        search = self.search_entry.get()
        subjects = self.db.list_subjects(search)
        orphans = self.db.count_materials_without_subject()
        if orphans and not search.strip():
            subjects.append(self._orphan_entry(orphans))
        names = [str(item["name"]) for item in subjects]
        if self._selected is not None:
            match = next(
                (
                    name
                    for name in names
                    if name.casefold() == self._selected.casefold()
                ),
                None,
            )
            self._selected = match
        if self._selected is None and names:
            self._selected = names[0]
        self._render_list(subjects)
        self._render_detail()

    @staticmethod
    def _orphan_entry(count: int) -> dict[str, Any]:
        return {
            "name": ORPHAN_SUBJECT_KEY,
            "display_name": ORPHAN_SUBJECT,
            "subject_id": 0,
            "teacher_name": "",
            "teacher_email": "",
            "cfu": 0,
            "hours": 0,
            "exam_date": "",
            "notes": "",
            "materials": count,
            "videos": 0,
            "flashcards": 0,
            "due_flashcards": 0,
            "events": 0,
            "sessions": 0,
            "study_minutes": 0,
            "orphan": True,
        }

    def _on_search(self, _event: tk.Event | None = None) -> None:
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except (tk.TclError, ValueError):
                pass
        self._search_job = self.after(200, self.refresh)

    def _select(self, name: str) -> None:
        self._selected = name
        self._remember_selection(name)
        self.refresh()

    def _on_combo(self, label: str) -> None:
        name = self._combo_map.get(label)
        if name is not None:
            self._select(name)

    def _load_last_selection(self) -> str | None:
        try:
            value = self.db.get_setting(LAST_SUBJECT_SETTING, "")
        except Exception:
            return None
        return value or None

    def _remember_selection(self, name: str) -> None:
        try:
            self.db.set_setting(LAST_SUBJECT_SETTING, name)
        except Exception:
            self.app.logger.debug("Impossibile salvare l'ultima materia aperta.", exc_info=True)

    # ------------------------------------------------------------- dropdown
    @staticmethod
    def _combo_label(subject: dict[str, Any]) -> str:
        name = str(subject.get("name") or "")
        display_name = str(subject.get("display_name") or name) or "Senza nome"
        if subject.get("orphan"):
            count = int(subject.get("materials", 0) or 0)
            return f"⚠  {display_name} ({count})"
        return display_name

    def _render_list(self, subjects: list[dict[str, Any]]) -> None:
        if not render_state(self).should_render("list", subjects, self._selected):
            return
        self._combo_map = {}
        if not subjects:
            placeholder = "Nessuna materia trovata"
            self.subject_combo.configure(values=[placeholder])
            self.subject_combo.set(placeholder)
            self.subject_combo.configure(state="disabled")
            self.subject_meta.configure(text="Crea una materia oppure cambia la ricerca.")
            self.subject_badge.pack_forget()
            return

        labels: list[str] = []
        selected_label = ""
        selected_subject: dict[str, Any] | None = None
        for subject in subjects:
            name = str(subject.get("name") or "")
            label = self._combo_label(subject)
            # Etichette univoche anche nel caso (raro) di nomi visualizzati uguali.
            base, suffix = label, 2
            while label in self._combo_map:
                label = f"{base} ({suffix})"
                suffix += 1
            self._combo_map[label] = name
            labels.append(label)
            if self._selected and name.casefold() == self._selected.casefold():
                selected_label = label
                selected_subject = subject

        self.subject_combo.configure(state="readonly", values=labels)
        self.subject_combo.set(selected_label or labels[0])
        self._render_selection_info(selected_subject or subjects[0])

    def _render_selection_info(self, subject: dict[str, Any]) -> None:
        is_orphan = bool(subject.get("orphan"))
        cfu = int(subject.get("cfu", 0) or 0)
        if is_orphan:
            count = int(subject.get("materials", 0) or 0)
            meta_text = f"{count} materiali da assegnare"
        elif cfu:
            meta_text = f"{format_cfu(cfu)}  ·  {cfu_to_hours(cfu)} h"
        else:
            meta_text = "CFU e dettagli da impostare"
        countdown = exam_countdown(str(subject.get("exam_date") or ""))
        self.subject_meta.configure(text=meta_text)
        self.subject_badge.configure(
            text=(
                "Da assegnare a una materia"
                if is_orphan
                else f"Esame {countdown['date_label']}"
                if countdown["defined"]
                else "Data esame da definire"
            ),
            text_color=(
                COLORS.amber
                if is_orphan
                else COUNTDOWN_COLORS.get(str(countdown["status"]), COLORS.text_muted)
            ),
        )
        # Ri-mostra il badge se era stato nascosto dallo stato "nessuna materia".
        self.subject_badge.pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------ detail
    def _render_detail(self) -> None:
        from datetime import date

        selected = self._selected
        if selected == ORPHAN_SUBJECT_KEY:
            detail_data: Any = self.db.list_materials_without_subject()
        elif selected:
            detail_data = (self.db.get_subject(selected), self.db.list_materials_for_subject(selected))
        else:
            detail_data = None
        if not render_state(self).should_render("detail", selected, detail_data, date.today().isoformat()):
            return
        clear_children(self.detail_scroll)
        self._countdown_label = None
        self._countdown_date = ""
        if not self._selected:
            self._empty_detail()
            return
        if self._selected == ORPHAN_SUBJECT_KEY:
            self._orphan_detail()
            return
        subject = self.db.get_subject(self._selected)
        if subject is None:
            self._empty_detail()
            return
        self._detail_header(subject)
        self._detail_countdown(subject)
        self._detail_materials(subject)
        self._detail_stats(subject)
        self._detail_teacher(subject)
        self._detail_links(subject)
        self._detail_notes(subject)

    def _orphan_detail(self) -> None:
        header = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(2, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="MATERIALI DA ASSEGNARE",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text=ORPHAN_SUBJECT,
            font=font(26, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=680,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 6))
        ctk.CTkLabel(
            header,
            text=(
                "Questi materiali non hanno ancora una materia. Apri "
                "Modifica su un materiale per assegnarlo: una volta collegato, "
                "sparirà da qui e comparirà nella pagina della materia scelta."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=680,
            justify="left",
        ).grid(row=2, column=0, sticky="w")

        section = MaterialsSection(
            self.detail_scroll,
            self.app,
            "",
            on_change=self.refresh,
            allow_add=False,
            allow_subject_change=True,
        )
        section.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        note = Card(self.detail_scroll)
        note.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ctk.CTkLabel(
            note,
            text=(
                "Suggerimento: puoi assegnare il materiale a una materia "
                "esistente oppure scrivere il nome di una nuova materia. "
                "La pagina materia verrà creata automaticamente."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=660,
            justify="left",
        ).pack(anchor="w", padx=20, pady=14)

    def _empty_detail(self) -> None:
        card = Card(self.detail_scroll)
        card.grid(row=0, column=0, sticky="nsew", pady=(4, 0))
        ctk.CTkLabel(
            card,
            text="Nessuna materia selezionata",
            font=font(18, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).pack(anchor="w", padx=26, pady=(28, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Crea una materia per raccogliere docente, CFU, monte ore e "
                "data d'esame. Le materie gia' usate in materiali, video o "
                "flashcard compaiono qui automaticamente."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=660,
        ).pack(anchor="w", padx=26, pady=(0, 16))
        ctk.CTkButton(
            card,
            text="+  Crea la prima materia",
            height=38,
            width=190,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=lambda: self._open_dialog(None),
        ).pack(anchor="w", padx=26, pady=(0, 26))

    def _detail_header(self, subject: dict[str, Any]) -> None:
        header = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(2, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="PAGINA MATERIA",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text=str(subject.get("name") or ""),
            font=font(26, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=680,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 6))

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="w")
        ctk.CTkButton(
            actions,
            text=(
                "Completa dettagli"
                if not int(subject.get("subject_id", 0) or 0)
                else "Modifica"
            ),
            height=32,
            width=122,
            font=font(10, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda value=dict(subject): self._open_dialog(value),
        ).pack(side="left", padx=(0, 6))
        linked = sum(
            int(subject.get(key, 0) or 0)
            for key in ("materials", "videos", "flashcards", "events", "sessions")
        )
        if int(subject.get("subject_id", 0) or 0) and linked == 0:
            ctk.CTkButton(
                actions,
                text="Elimina",
                height=32,
                width=76,
                font=font(10),
                fg_color="transparent",
                hover_color=COLORS.red_dark,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.red,
                corner_radius=8,
                command=lambda value=dict(subject): self._delete(value),
            ).pack(side="left")

    def _detail_countdown(self, subject: dict[str, Any]) -> None:
        countdown = exam_countdown(str(subject.get("exam_date") or ""))
        status = str(countdown["status"])
        color = COUNTDOWN_COLORS.get(status, COLORS.text_muted)
        card = Card(self.detail_scroll, border_color=color if status != "undefined" else COLORS.border)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="ESAME",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 0))
        self._countdown_label = ctk.CTkLabel(
            card,
            text=str(countdown["label"]),
            font=font(24, "bold"),
            text_color=color,
            anchor="w",
            wraplength=640,
            justify="left",
        )
        self._countdown_label.grid(row=1, column=0, sticky="w", padx=20, pady=(2, 0))
        self._countdown_date = str(subject.get("exam_date") or "")
        detail = (
            f"Data esame: {countdown['date_label']}"
            if countdown["defined"]
            else "Puoi indicare la data in qualsiasi momento, anche dopo aver creato la materia."
        )
        ctk.CTkLabel(
            card,
            text=detail,
            font=font(10),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=640,
            justify="left",
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(2, 4))
        if not countdown["defined"]:
            ctk.CTkButton(
                card,
                text="Imposta data esame",
                height=30,
                width=150,
                font=font(10, "bold"),
                fg_color="transparent",
                hover_color=COLORS.surface_alt,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.accent,
                corner_radius=8,
                command=lambda value=dict(subject): self._open_dialog(value),
            ).grid(row=3, column=0, sticky="w", padx=20, pady=(2, 14))
        else:
            ctk.CTkLabel(card, text="", height=10).grid(row=3, column=0)

    def _detail_stats(self, subject: dict[str, Any]) -> None:
        cfu = int(subject.get("cfu", 0) or 0)
        hours = int(subject.get("hours", cfu_to_hours(cfu)) or 0)
        study_minutes = int(subject.get("study_minutes", 0) or 0)
        study_hours = study_minutes / 60.0
        percent = min(100, int(round(study_hours / hours * 100))) if hours else 0
        stats = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
        stats.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        for column in range(2):
            stats.grid_columnconfigure(column, weight=1, uniform="subject_stats")

        cfu_card = StatCard(
            stats,
            "CFU",
            str(cfu) if cfu else "-",
            format_cfu(cfu) if cfu else "Non indicati",
            COLORS.blue,
        )
        cfu_card.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))
        hours_card = StatCard(
            stats,
            "MONTE ORE",
            f"{hours} h" if hours else "-",
            "1 CFU = 25 ore" if hours else "Aggiungi i CFU",
            COLORS.purple,
        )
        hours_card.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 6))
        study_card = StatCard(
            stats,
            "STUDIO REGISTRATO",
            format_hours(study_hours),
            f"{percent}% del monte ore" if hours else "Monte ore non impostato",
            COLORS.accent,
        )
        study_card.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(6, 0))
        cards = int(subject.get("flashcards", 0) or 0)
        due = int(subject.get("due_flashcards", 0) or 0)
        cards_card = StatCard(
            stats,
            "FLASHCARD",
            str(cards),
            f"{due} da ripassare" if cards else "Nessuna scheda",
            COLORS.amber,
        )
        cards_card.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))

    def _detail_materials(self, subject: dict[str, Any]) -> None:
        section = MaterialsSection(
            self.detail_scroll,
            self.app,
            str(subject.get("name") or ""),
            on_change=self.refresh,
        )
        section.grid(row=2, column=0, sticky="ew", pady=(0, 12))

    def _detail_teacher(self, subject: dict[str, Any]) -> None:
        card = Card(self.detail_scroll)
        card.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="DOCENTE",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 2))
        teacher = str(subject.get("teacher_name") or "").strip()
        email = str(subject.get("teacher_email") or "").strip()
        ctk.CTkLabel(
            card,
            text=teacher or "Nome non indicato",
            font=font(14, "bold"),
            text_color=COLORS.text if teacher else COLORS.text_muted,
            anchor="w",
            wraplength=300,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(2, 0))
        ctk.CTkLabel(
            card,
            text=email or "Email non indicata",
            font=font(11),
            text_color=COLORS.accent if email else COLORS.text_muted,
            anchor="w",
            wraplength=300,
            justify="left",
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(2, 14))
        if email:
            ctk.CTkButton(
                card,
                text="Scrivi email",
                height=30,
                width=110,
                font=font(10, "bold"),
                fg_color="transparent",
                hover_color=COLORS.surface_alt,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.text,
                corner_radius=8,
                command=lambda value=email: self._write_email(value),
            ).grid(row=1, column=1, rowspan=2, sticky="e", padx=(10, 20))

    def _detail_links(self, subject: dict[str, Any]) -> None:
        card = Card(self.detail_scroll)
        card.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="COLLEGAMENTI",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 4))
        parts = [
            ("materiali", int(subject.get("materials", 0) or 0)),
            ("videolezioni", int(subject.get("videos", 0) or 0)),
            ("flashcard", int(subject.get("flashcards", 0) or 0)),
            ("eventi", int(subject.get("events", 0) or 0)),
            ("sessioni", int(subject.get("sessions", 0) or 0)),
        ]
        summary = "  ·  ".join(f"{count} {label}" for label, count in parts if count)
        ctk.CTkLabel(
            card,
            text=summary or "Nessun collegamento: la materia esiste solo come anagrafica.",
            font=font(11),
            text_color=COLORS.text_muted if summary else COLORS.text_faint,
            anchor="w",
            wraplength=660,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 6))
        ctk.CTkLabel(
            card,
            text="Rinominando la materia, i collegamenti esistenti vengono aggiornati automaticamente.",
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=660,
            justify="left",
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(0, 14))

    def _detail_notes(self, subject: dict[str, Any]) -> None:
        notes = str(subject.get("notes") or "").strip()
        if not notes:
            return
        card = Card(self.detail_scroll)
        card.grid(row=6, column=0, sticky="ew", pady=(0, 4))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="NOTE",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            card,
            text=notes,
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=660,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 14))

    # ------------------------------------------------------------------ actions
    def _open_dialog(self, subject: dict[str, Any] | None) -> None:
        SubjectDialog(self, subject)

    def _delete(self, subject: dict[str, Any]) -> None:
        name = str(subject.get("name") or "")
        if not messagebox.askyesno(
            "Eliminare la materia?",
            f"Eliminare i dettagli di '{name}'?\n\n"
            "L'operazione e' consentita solo se non ci sono materiali, video, "
            "flashcard, eventi o sessioni collegati.",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            self.db.delete_subject(name)
        except ValueError as exc:
            messagebox.showwarning(
                "Materia non eliminabile",
                str(exc),
                parent=self.winfo_toplevel(),
            )
            return
        self._selected = None
        self.refresh()
        self.app.toast("Materia eliminata.")

    def _write_email(self, email: str) -> None:
        try:
            webbrowser.open(f"mailto:{email}")
        except Exception:
            messagebox.showinfo(
                "Email docente",
                email,
                parent=self.winfo_toplevel(),
            )

    # --------------------------------------------------------------- countdown
    def _start_countdown(self) -> None:
        self._stop_countdown()
        self._countdown_job = self.after(60000, self._tick_countdown)

    def _stop_countdown(self) -> None:
        if self._countdown_job is not None:
            try:
                self.after_cancel(self._countdown_job)
            except (tk.TclError, ValueError):
                pass
            self._countdown_job = None

    def _tick_countdown(self) -> None:
        self._countdown_job = None
        if not self.app.is_view_visible(self):
            return
        if self._countdown_label is not None and self._selected:
            subject = self.db.get_subject(self._selected)
            if subject is not None:
                countdown = exam_countdown(str(subject.get("exam_date") or ""))
                try:
                    self._countdown_label.configure(
                        text=str(countdown["label"]),
                        text_color=COUNTDOWN_COLORS.get(
                            str(countdown["status"]), COLORS.text_muted
                        ),
                    )
                except tk.TclError:
                    return
        self._countdown_job = self.after(60000, self._tick_countdown)


class SubjectDialog(ctk.CTkToplevel):
    def __init__(self, view: SubjectsView, subject: dict[str, Any] | None):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.subject = subject
        self.original_name = str(subject.get("name") or "") if subject else None
        self.title("Modifica materia" if subject else "Nuova materia")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 720, 660)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self._close())
        self.after(80, self._focus_name)

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Modifica materia" if self.subject else "Nuova materia",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(22, 2))
        ctk.CTkLabel(
            self,
            text=(
                "Tutti i campi sono facoltativi tranne il nome. "
                "Modificando il nome, i collegamenti esistenti vengono aggiornati."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=650,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 14))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure(0, weight=1)

        self.name_var = ctk.StringVar(value=self.original_name or "")
        self.name_entry = self._field(
            form, 0, "Nome materia *", self.name_var, "es. Analisi Matematica"
        )[1]

        teacher_row = ctk.CTkFrame(form, fg_color="transparent")
        teacher_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        teacher_row.grid_columnconfigure(0, weight=1)
        teacher_row.grid_columnconfigure(1, weight=1)
        self.teacher_var = ctk.StringVar(
            value=str(self.subject.get("teacher_name") or "") if self.subject else ""
        )
        self.email_var = ctk.StringVar(
            value=str(self.subject.get("teacher_email") or "") if self.subject else ""
        )
        teacher_frame, _ = self._field(
            teacher_row, 0, "Nome docente", self.teacher_var, "es. Mario Rossi"
        )
        teacher_frame.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        email_frame, _ = self._field(
            teacher_row, 0, "Email docente", self.email_var, "es. mario.rossi@uni.it"
        )
        email_frame.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        numbers_row = ctk.CTkFrame(form, fg_color="transparent")
        numbers_row.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        numbers_row.grid_columnconfigure(0, weight=0, minsize=150)
        numbers_row.grid_columnconfigure(1, weight=1)
        self.cfu_var = ctk.StringVar(
            value=(
                str(int(self.subject.get("cfu", 0) or 0))
                if self.subject and int(self.subject.get("cfu", 0) or 0)
                else ""
            )
        )
        cfu_frame, _ = self._field(
            numbers_row, 0, "CFU", self.cfu_var, "es. 6"
        )
        cfu_frame.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        date_frame = ctk.CTkFrame(numbers_row, fg_color="transparent")
        date_frame.grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(
            date_frame,
            text="Data esame",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        self.date_var = ctk.StringVar(
            value=format_exam_date(str(self.subject.get("exam_date") or ""))
            if self.subject
            else ""
        )
        self.date_entry = ctk.CTkEntry(
            date_frame,
            height=36,
            textvariable=self.date_var,
            placeholder_text="GG/MM/AAAA",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        self.date_entry.pack(fill="x")

        self.undefined_var = ctk.BooleanVar(
            value=not bool(self.subject and str(self.subject.get("exam_date") or "").strip())
        )
        self.undefined_check = ctk.CTkCheckBox(
            form,
            text="Data esame non ancora definita",
            variable=self.undefined_var,
            font=font(11),
            text_color=COLORS.text_muted,
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            border_color=COLORS.border,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=5,
            command=self._toggle_date,
        )
        self.undefined_check.grid(row=3, column=0, sticky="w", pady=(2, 12))

        notes_frame = ctk.CTkFrame(form, fg_color="transparent")
        notes_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        notes_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            notes_frame,
            text="Note (facoltative)",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.notes_box = ctk.CTkTextbox(
            notes_frame,
            height=90,
            wrap="word",
            fg_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        self.notes_box.grid(row=1, column=0, sticky="ew")
        if self.subject:
            self.notes_box.insert("1.0", str(self.subject.get("notes") or ""))

        ctk.CTkLabel(
            form,
            text=(
                "Monte ore: 1 CFU = 25 ore di impegno complessivo "
                "(D.M. 270/2004). Il conteggio include studio individuale e lezioni."
            ),
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=650,
            justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(0, 2))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=28, pady=(12, 22))
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
            command=self._close,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            buttons,
            text="Salva materia",
            height=40,
            width=160,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._save,
        ).grid(row=0, column=1, sticky="e")

        self._toggle_date()

    def _field(
        self,
        parent,
        row: int,
        label: str,
        variable: ctk.StringVar,
        placeholder: str,
    ) -> tuple[ctk.CTkFrame, ctk.CTkEntry]:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=label,
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        entry = ctk.CTkEntry(
            frame,
            height=36,
            textvariable=variable,
            placeholder_text=placeholder,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        entry.grid(row=1, column=0, sticky="ew")
        return frame, entry

    def _toggle_date(self) -> None:
        undefined = bool(self.undefined_var.get())
        self.date_entry.configure(state="disabled" if undefined else "normal")
        if not undefined:
            self.after(50, self._focus_date)

    def _focus_date(self) -> None:
        try:
            self.date_entry.focus_set()
        except tk.TclError:
            pass

    def _focus_name(self) -> None:
        try:
            self.name_entry.focus_set()
        except tk.TclError:
            pass

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(
                "Nome mancante",
                "Inserisci il nome della materia.",
                parent=self,
            )
            return
        cfu_text = self.cfu_var.get().strip()
        if cfu_text:
            try:
                cfu = int(cfu_text)
            except ValueError:
                messagebox.showerror(
                    "CFU non validi",
                    "I CFU devono essere un numero intero.",
                    parent=self,
                )
                return
            if cfu < 0 or cfu > MAX_CFU:
                messagebox.showerror(
                    "CFU non validi",
                    f"Inserisci un numero di CFU tra 0 e {MAX_CFU}.",
                    parent=self,
                )
                return
        else:
            cfu = 0
        email = self.email_var.get().strip()
        if email and "@" not in email:
            messagebox.showerror(
                "Email non valida",
                "Inserisci un indirizzo email valido oppure lascia il campo vuoto.",
                parent=self,
            )
            return
        if bool(self.undefined_var.get()):
            exam_date = ""
        else:
            try:
                exam_date = parse_exam_date(self.date_var.get())
            except ValueError as exc:
                messagebox.showerror("Data non valida", str(exc), parent=self)
                return
            if not exam_date:
                messagebox.showerror(
                    "Data mancante",
                    "Inserisci la data dell'esame oppure seleziona "
                    "'Data esame non ancora definita'.",
                    parent=self,
                )
                return
        try:
            saved = self.db.save_subject(
                name,
                self.teacher_var.get().strip(),
                email,
                cfu,
                exam_date,
                self.notes_box.get("1.0", "end").strip(),
                original_name=self.original_name,
            )
        except ValueError as exc:
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        except Exception as exc:
            self.app.logger.exception("Salvataggio materia non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        self.view._selected = str(saved.get("name") or name)
        self.view._remember_selection(self.view._selected)
        self._close()
        self.view.refresh()
        self.app.toast("Materia salvata.")

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
