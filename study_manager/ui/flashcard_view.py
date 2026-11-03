"""Vista Flashcard: archivio per materia e ripasso a ripetizione spaziata."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..services.spaced_repetition import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    RATING_HINTS,
    RATING_LABELS,
    RATINGS,
    format_due_label,
    format_interval_days,
    is_due,
    preview_schedule,
    state_label,
)
from .theme import font
from .widgets import (
    Card,
    PageHeader,
    StatCard,
    center_window,
    clear_children,
    separator,
    set_window_icon,
)


ALL_SUBJECTS = "Tutte le materie"
ALL_STATES = "Tutte le schede"
STATE_FILTERS = (
    ALL_STATES,
    "Da ripassare",
    "Nuove",
    "Apprendimento",
    "Ripasso",
)

STATE_COLORS = {
    "new": COLORS.blue,
    "learning": COLORS.amber,
    "review": COLORS.accent,
    "relearning": COLORS.purple,
}


class FlashcardView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self._filter_subject = ALL_SUBJECTS
        self._search_job: str | None = None
        self._cards: list[dict[str, Any]] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_header()
        self._build_summary()
        self._build_toolbar()
        self._build_body()

    # ------------------------------------------------------------------ layout
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.grid_columnconfigure(0, weight=1)
        PageHeader(
            header,
            "Flashcard",
            "Crea schede per materia e ripassale con la ripetizione spaziata.",
        ).grid(row=0, column=0, sticky="w")
        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e", pady=(4, 0))
        ctk.CTkButton(
            actions,
            text="Studia ora",
            height=40,
            width=124,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._study_due,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="+  Nuova flashcard",
            height=40,
            width=168,
            font=font(12, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=10,
            command=lambda: self._open_dialog(None),
        ).pack(side="left")

    def _build_summary(self) -> None:
        summary = ctk.CTkFrame(self, fg_color="transparent")
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for column in range(4):
            summary.grid_columnconfigure(column, weight=1, uniform="flashcards")
        self.due_card = StatCard(
            summary, "DA RIPASSARE", "0", "Scadute o nuove", COLORS.amber
        )
        self.due_card.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.new_card = StatCard(summary, "NUOVE", "0", "Mai studiate", COLORS.blue)
        self.new_card.grid(row=0, column=1, sticky="ew", padx=6)
        self.learning_card = StatCard(
            summary, "IN APPRENDIMENTO", "0", "Passi brevi", COLORS.purple
        )
        self.learning_card.grid(row=0, column=2, sticky="ew", padx=6)
        self.total_card = StatCard(
            summary, "TOTALI", "0", "0 ripassi oggi", COLORS.accent
        )
        self.total_card.grid(row=0, column=3, sticky="ew", padx=(6, 0))

    def _build_toolbar(self) -> None:
        toolbar = Card(self)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        toolbar.grid_columnconfigure(2, weight=1)

        subject_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        subject_frame.grid(row=0, column=0, sticky="w", padx=(18, 10), pady=(9, 8))
        ctk.CTkLabel(
            subject_frame,
            text="Materia",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self.subject_filter = ctk.CTkComboBox(
            subject_frame,
            values=[ALL_SUBJECTS],
            width=210,
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
            command=self._on_filter_change,
        )
        self.subject_filter.pack(anchor="w")
        self.subject_filter.set(ALL_SUBJECTS)

        state_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        state_frame.grid(row=0, column=1, sticky="w", padx=10, pady=(9, 8))
        ctk.CTkLabel(
            state_frame,
            text="Stato",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self.state_filter = ctk.CTkComboBox(
            state_frame,
            values=list(STATE_FILTERS),
            width=170,
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
            command=self._on_filter_change,
        )
        self.state_filter.pack(anchor="w")
        self.state_filter.set(ALL_STATES)

        search_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        search_frame.grid(row=0, column=2, sticky="ew", padx=(10, 18), pady=(9, 8))
        ctk.CTkLabel(
            search_frame,
            text="Cerca",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self.search_entry = ctk.CTkEntry(
            search_frame,
            height=34,
            placeholder_text="Fronte, retro, note o materia...",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(11),
            corner_radius=8,
        )
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", self._on_search)

    def _build_body(self) -> None:
        self.cards_scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.cards_scroll.grid(row=3, column=0, sticky="nsew")
        self.cards_scroll.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        self.refresh()

    def refresh_if_visible(self) -> None:
        if self.winfo_ismapped():
            self.refresh()

    def refresh(self) -> None:
        subjects = self.db.list_subject_tags()
        values = [ALL_SUBJECTS, *subjects]
        self.subject_filter.configure(values=values)
        if self._filter_subject not in values:
            self._filter_subject = ALL_SUBJECTS
            self.subject_filter.set(ALL_SUBJECTS)
        subject = None if self._filter_subject == ALL_SUBJECTS else self._filter_subject
        state_filter = self.state_filter.get()
        due_only = state_filter == "Da ripassare"
        cards = self.db.list_flashcards(
            subject=subject,
            search=self.search_entry.get(),
            due_only=due_only,
        )
        if state_filter == "Nuove":
            cards = [card for card in cards if card.get("state") == "new"]
        elif state_filter == "Apprendimento":
            cards = [
                card
                for card in cards
                if card.get("state") in {"learning", "relearning"}
            ]
        elif state_filter == "Ripasso":
            cards = [card for card in cards if card.get("state") == "review"]
        self._cards = cards

        stats = self.db.flashcard_stats(subject)
        self.due_card.set(str(stats["due"]), "Scadute o nuove", COLORS.amber)
        self.new_card.set(str(stats["new"]), "Mai studiate", COLORS.blue)
        self.learning_card.set(
            str(stats["learning"]), "Passi brevi", COLORS.purple
        )
        self.total_card.set(
            str(stats["total"]),
            f"{stats['reviewed_today']} ripassi oggi",
            COLORS.accent,
        )

        clear_children(self.cards_scroll)
        if not cards:
            empty = Card(self.cards_scroll)
            empty.grid(row=0, column=0, sticky="ew", pady=(4, 0))
            ctk.CTkLabel(
                empty,
                text="Nessuna flashcard in questa vista.",
                font=font(14, "bold"),
                text_color=COLORS.text_muted,
            ).pack(anchor="w", padx=22, pady=(24, 3))
            ctk.CTkLabel(
                empty,
                text=(
                    "Crea una scheda oppure cambia filtro. Le schede nuove sono "
                    "subito disponibili per il ripasso."
                ),
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=22, pady=(0, 24))
            return
        for index, card in enumerate(cards):
            self._card_row(card, index)

    def _card_row(self, card: dict[str, Any], row: int) -> None:
        frame = ctk.CTkFrame(
            self.cards_scroll,
            fg_color=COLORS.surface,
            corner_radius=12,
            border_width=1,
            border_color=COLORS.border,
        )
        frame.grid(row=row, column=0, sticky="ew", pady=4)
        frame.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(13, 2))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top,
            text=str(card.get("front") or "Senza domanda"),
            font=font(14, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=650,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        subject = str(card.get("subject") or "Senza materia")
        ctk.CTkLabel(
            top,
            text=subject,
            font=font(9, "bold"),
            text_color=COLORS.accent,
            fg_color=COLORS.accent_dark,
            corner_radius=6,
            padx=8,
            height=22,
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))

        state = str(card.get("state") or "new")
        ctk.CTkLabel(
            top,
            text=state_label(state),
            font=font(9, "bold"),
            text_color=STATE_COLORS.get(state, COLORS.text_muted),
            fg_color=COLORS.surface_alt,
            corner_radius=6,
            padx=8,
            height=22,
        ).grid(row=0, column=2, sticky="e", padx=(6, 0))

        ctk.CTkLabel(
            frame,
            text=str(card.get("back") or ""),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=800,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 2))

        due = is_due(card)
        due_text = (
            "Da ripassare adesso"
            if due
            else f"Prossimo ripasso {format_due_label(str(card.get('due_at') or ''))}"
        )
        meta = (
            f"{due_text}   |   Ripetizioni: {int(card.get('repetitions', 0) or 0)}"
            f"   |   Facilita': {float(card.get('ease_factor', 2.5) or 2.5):.2f}"
            f"   |   Lapse: {int(card.get('lapses', 0) or 0)}"
        )
        ctk.CTkLabel(
            frame,
            text=meta,
            font=font(9),
            text_color=COLORS.amber if due else COLORS.text_faint,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=18, pady=(4, 0))

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=18, pady=(9, 13))
        ctk.CTkButton(
            actions,
            text="Studia",
            height=30,
            width=76,
            font=font(10, "bold"),
            fg_color=COLORS.accent_dark,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda value=int(card["id"]): self._start_study(value),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="Modifica",
            height=30,
            width=84,
            font=font(10),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda value=dict(card): self._open_dialog(value),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="Ripristina",
            height=30,
            width=88,
            font=font(10),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda value=dict(card): self._reset(value),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="Elimina",
            height=30,
            width=76,
            font=font(10),
            fg_color="transparent",
            hover_color=COLORS.red_dark,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.red,
            corner_radius=8,
            command=lambda value=dict(card): self._delete(value),
        ).pack(side="right")

    # ------------------------------------------------------------------ actions
    def _on_search(self, _event: tk.Event | None = None) -> None:
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except (tk.TclError, ValueError):
                pass
        self._search_job = self.after(220, self.refresh)

    def _on_filter_change(self, _value: str | None = None) -> None:
        self._filter_subject = self.subject_filter.get()
        self.refresh()

    def _open_dialog(self, card: dict[str, Any] | None) -> None:
        FlashcardDialog(self, card)

    def _study_due(self) -> None:
        subject = None if self._filter_subject == ALL_SUBJECTS else self._filter_subject
        cards = self.db.due_flashcards(subject=subject, limit=300)
        if not cards:
            messagebox.showinfo(
                "Nessuna scheda da ripassare",
                "Non ci sono flashcard scadute per il filtro corrente.\n"
                "Puoi crearne una nuova oppure cambiare materia.",
                parent=self.winfo_toplevel(),
            )
            return
        StudySessionDialog(self, cards)

    def _start_study(self, card_id: int) -> None:
        card = self.db.get_flashcard(card_id)
        if card is None:
            self.refresh()
            return
        StudySessionDialog(self, [card])

    def _delete(self, card: dict[str, Any]) -> None:
        if not messagebox.askyesno(
            "Eliminare la flashcard?",
            f"Eliminare la scheda '{str(card.get('front') or '')[:120]}'?\n"
            "Anche lo storico dei ripassi verra' rimosso.",
            parent=self.winfo_toplevel(),
        ):
            return
        self.db.delete_flashcard(int(card["id"]))
        self.refresh()
        self.app.toast("Flashcard eliminata.")

    def _reset(self, card: dict[str, Any]) -> None:
        if not messagebox.askyesno(
            "Ripristinare la flashcard?",
            "La scheda tornara' nuova e perdera' intervallo, facilita' e storico "
            "di pianificazione. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        self.db.reset_flashcard(int(card["id"]))
        self.refresh()
        self.app.toast("Flashcard ripristinata.")


class FlashcardDialog(ctk.CTkToplevel):
    def __init__(self, view: FlashcardView, card: dict[str, Any] | None):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.card = card
        self.title("Modifica flashcard" if card else "Nuova flashcard")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 720, 650)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self._close())
        self.after(80, self._focus_front)

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Modifica flashcard" if self.card else "Nuova flashcard",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(22, 2))
        ctk.CTkLabel(
            self,
            text="La scheda viene ripassata con intervalli calcolati in base ai tuoi giudizi.",
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 16))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure(0, weight=1)

        subject_frame = ctk.CTkFrame(form, fg_color="transparent")
        subject_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(
            subject_frame,
            text="Materia",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        subjects = self.db.list_subject_tags()
        self.subject_combo = ctk.CTkComboBox(
            subject_frame,
            values=subjects or [""],
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(12),
            dropdown_font=font(12),
            corner_radius=9,
        )
        self.subject_combo.pack(fill="x")

        self.front_box = self._text_area(
            form, 1, "Fronte / domanda", "Scrivi la domanda o il concetto da ricordare..."
        )
        self.back_box = self._text_area(
            form, 2, "Retro / risposta", "Scrivi la risposta, la definizione o la spiegazione..."
        )
        self.notes_box = self._text_area(
            form, 3, "Note (facoltative)", "Esempi, collegamenti o suggerimenti...", height=70
        )

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
            text="Salva flashcard",
            height=40,
            width=170,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._save,
        ).grid(row=0, column=1, sticky="e")

        if self.card:
            self.subject_combo.set(str(self.card.get("subject") or ""))
            self._set_text(self.front_box, str(self.card.get("front") or ""))
            self._set_text(self.back_box, str(self.card.get("back") or ""))
            self._set_text(self.notes_box, str(self.card.get("notes") or ""))

    def _text_area(
        self,
        parent,
        row: int,
        label: str,
        placeholder: str,
        height: int = 95,
    ) -> ctk.CTkTextbox:
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
        box = ctk.CTkTextbox(
            frame,
            height=height,
            wrap="word",
            fg_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        box.grid(row=1, column=0, sticky="ew")
        box.insert("1.0", placeholder)
        box.bind("<FocusIn>", lambda _event, widget=box: self._clear_placeholder(widget, placeholder))
        return box

    @staticmethod
    def _clear_placeholder(box: ctk.CTkTextbox, placeholder: str) -> None:
        if box.get("1.0", "end").strip() == placeholder:
            box.delete("1.0", "end")

    @staticmethod
    def _set_text(box: ctk.CTkTextbox, value: str) -> None:
        box.delete("1.0", "end")
        box.insert("1.0", value)

    def _focus_front(self) -> None:
        try:
            self.front_box.focus_set()
        except tk.TclError:
            pass

    def _save(self) -> None:
        subject = self.subject_combo.get().strip()
        front = self.front_box.get("1.0", "end").strip()
        back = self.back_box.get("1.0", "end").strip()
        notes = self.notes_box.get("1.0", "end").strip()
        if front in {"Scrivi la domanda o il concetto da ricordare...", ""}:
            messagebox.showerror(
                "Domanda mancante",
                "Inserisci il fronte della flashcard.",
                parent=self,
            )
            return
        if back in {"Scrivi la risposta, la definizione o la spiegazione...", ""}:
            messagebox.showerror(
                "Risposta mancante",
                "Inserisci il retro della flashcard.",
                parent=self,
            )
            return
        if notes == "Esempi, collegamenti o suggerimenti...":
            notes = ""
        try:
            if self.card:
                self.db.update_flashcard(
                    int(self.card["id"]), subject, front, back, notes
                )
            else:
                self.db.add_flashcard(subject, front, back, notes)
        except Exception as exc:
            self.app.logger.exception("Salvataggio flashcard non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        self._close()
        self.view.refresh()
        self.app.toast("Flashcard salvata.")

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class StudySessionDialog(ctk.CTkToplevel):
    def __init__(self, view: FlashcardView, cards: list[dict[str, Any]]):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.queue = [dict(card) for card in cards]
        self.index = 0
        self.reviewed = 0
        self.revealed = False
        self.again_counts: dict[int, int] = {}
        self.title("Ripasso flashcard")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 780, 640)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<space>", self._on_space)
        for rating in RATINGS:
            self.bind(str(rating), lambda _event, value=rating: self._rate(value))
        self._load_card()
        self.after(80, self._focus_dialog)

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(20, 12))
        header.grid_columnconfigure(1, weight=1)
        self.subject_label = ctk.CTkLabel(
            header,
            text="",
            font=font(12, "bold"),
            text_color=COLORS.accent,
            anchor="w",
        )
        self.subject_label.grid(row=0, column=0, sticky="w")
        self.progress_label = ctk.CTkLabel(
            header,
            text="",
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="e",
        )
        self.progress_label.grid(row=0, column=1, sticky="e")
        self.close_button = ctk.CTkButton(
            header,
            text="Chiudi",
            width=76,
            height=30,
            font=font(10),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._close,
        )
        self.close_button.grid(row=0, column=2, sticky="e", padx=(12, 0))

        self.card_panel = Card(self)
        self.card_panel.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 12))
        self.card_panel.grid_columnconfigure(0, weight=1)
        self.card_panel.grid_rowconfigure(1, weight=1)
        self.state_label = ctk.CTkLabel(
            self.card_panel,
            text="",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        )
        self.state_label.grid(row=0, column=0, sticky="w", padx=22, pady=(16, 6))

        self.front_box = ctk.CTkTextbox(
            self.card_panel,
            wrap="word",
            fg_color="transparent",
            border_width=0,
            text_color=COLORS.text,
            font=font(20, "bold"),
            corner_radius=0,
        )
        self.front_box.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 8))
        self.front_box.configure(state="disabled")

        self.answer_frame = ctk.CTkFrame(
            self.card_panel, fg_color=COLORS.surface_alt, corner_radius=12
        )
        self.answer_frame.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 8))
        self.answer_frame.grid_columnconfigure(0, weight=1)
        self.answer_box = ctk.CTkTextbox(
            self.answer_frame,
            height=120,
            wrap="word",
            fg_color="transparent",
            border_width=0,
            text_color=COLORS.text,
            font=font(15),
            corner_radius=0,
        )
        self.answer_box.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        self.answer_box.configure(state="disabled")
        self.notes_label = ctk.CTkLabel(
            self.answer_frame,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.notes_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.answer_frame.grid_remove()

        self.bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 22))
        self.bottom.grid_columnconfigure(0, weight=1)
        self.reveal_button = ctk.CTkButton(
            self.bottom,
            text="Mostra risposta",
            height=48,
            font=font(13, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._reveal,
        )
        self.reveal_button.grid(row=0, column=0, sticky="ew")

        self.rating_frame = ctk.CTkFrame(self.bottom, fg_color="transparent")
        self.rating_frame.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            self.rating_frame.grid_columnconfigure(column, weight=1, uniform="ratings")
        self.rating_buttons: dict[int, ctk.CTkButton] = {}
        styles = {
            RATING_AGAIN: (COLORS.red_dark, COLORS.text),
            RATING_HARD: (COLORS.surface_alt, COLORS.amber),
            RATING_GOOD: (COLORS.accent_dark, COLORS.text),
            RATING_EASY: (COLORS.accent, COLORS.bg),
        }
        for column, rating in enumerate(RATINGS):
            color, text_color = styles[rating]
            button = ctk.CTkButton(
                self.rating_frame,
                text=RATING_LABELS[rating],
                height=54,
                font=font(11, "bold"),
                fg_color=color,
                hover_color=COLORS.accent_hover if rating in {RATING_GOOD, RATING_EASY} else COLORS.surface_hover,
                text_color=text_color,
                corner_radius=10,
                command=lambda value=rating: self._rate(value),
            )
            button.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 5, 0 if column == 3 else 5),
            )
            self.rating_buttons[rating] = button
        self.rating_frame.grid_remove()

    def _focus_dialog(self) -> None:
        try:
            self.focus_force()
        except tk.TclError:
            pass

    @staticmethod
    def _set_box(box: ctk.CTkTextbox, value: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", value)
        box.configure(state="disabled")

    def _load_card(self) -> None:
        if self.index >= len(self.queue):
            self._finish()
            return
        card = self.queue[self.index]
        self.revealed = False
        self.subject_label.configure(
            text=str(card.get("subject") or "Senza materia")
        )
        self.progress_label.configure(
            text=f"Scheda {self.index + 1} di {len(self.queue)}  |  "
            f"{self.reviewed} ripassi"
        )
        self.state_label.configure(
            text=state_label(str(card.get("state") or "new")).upper(),
            text_color=STATE_COLORS.get(str(card.get("state") or "new"), COLORS.text_muted),
        )
        self._set_box(self.front_box, str(card.get("front") or ""))
        self._set_box(self.answer_box, str(card.get("back") or ""))
        notes = str(card.get("notes") or "").strip()
        if notes:
            self.notes_label.configure(text=f"Note: {notes}")
            self.notes_label.grid()
        else:
            self.notes_label.grid_remove()
        self.answer_frame.grid_remove()
        self.rating_frame.grid_remove()
        self.reveal_button.configure(text="Mostra risposta", command=self._reveal)
        self.reveal_button.grid()
        self._update_rating_previews(card)

    def _update_rating_previews(self, card: dict[str, Any]) -> None:
        for rating, button in self.rating_buttons.items():
            schedule = preview_schedule(card, rating)
            interval = format_interval_days(float(schedule["scheduled_days"]))
            button.configure(
                text=(
                    f"{RATING_LABELS[rating]}\n"
                    f"{RATING_HINTS[rating]} · {interval}"
                )
            )

    def _on_space(self, _event: tk.Event | None = None) -> None:
        if not self.revealed:
            self._reveal()

    def _reveal(self) -> None:
        if self.revealed or self.index >= len(self.queue):
            return
        self.revealed = True
        self.answer_frame.grid()
        self.reveal_button.grid_remove()
        self.rating_frame.grid()

    def _rate(self, rating: int) -> None:
        if not self.revealed or self.index >= len(self.queue):
            return
        card = self.queue[self.index]
        try:
            updated, _schedule = self.db.review_flashcard(int(card["id"]), rating)
        except Exception as exc:
            self.app.logger.exception("Salvataggio ripasso non riuscito.")
            messagebox.showerror("Ripasso non salvato", str(exc), parent=self)
            return
        self.reviewed += 1
        card_id = int(card["id"])
        if rating == RATING_AGAIN and self.again_counts.get(card_id, 0) < 2:
            self.again_counts[card_id] = self.again_counts.get(card_id, 0) + 1
            self.queue.append(updated)
        self.index += 1
        self._load_card()

    def _finish(self) -> None:
        self.subject_label.configure(text="Sessione completata")
        self.progress_label.configure(text=f"{self.reviewed} ripassi")
        self.state_label.configure(text="")
        self._set_box(
            self.front_box,
            "Hai completato la sessione.\n\n"
            f"Flashcard ripassate: {self.reviewed}\n"
            "Le prossime scadenze sono state aggiornate.",
        )
        self.answer_frame.grid_remove()
        self.rating_frame.grid_remove()
        self.reveal_button.configure(text="Chiudi", command=self._close)
        self.reveal_button.grid()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        try:
            self.view.refresh()
        except tk.TclError:
            pass
