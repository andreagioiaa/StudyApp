"""Finestra "Rimuovi testo" della vista PDF e Slide.

E' l'equivalente di "Sostituisci tutto" con il campo di sostituzione vuoto:
conta ed evidenzia le occorrenze nell'editor, le rimuove dal documento aperto
o da tutti i documenti estratti e permette di annullare l'ultima rimozione.
Le regole possono essere salvate e applicate in automatico alle estrazioni
successive (es. il nome del corso ripetuto su ogni slide).
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

from ..config import COLORS
from ..services.pdf_tools.text_removal import (
    RemovalPatternError,
    RemovalRule,
    count_matches,
    find_matches,
)
from .theme import font, mono

if TYPE_CHECKING:  # pragma: no cover
    from .pdf_view import PdfView

SCOPE_CURRENT = "Documento aperto"
SCOPE_ALL = "Tutti i documenti"
HIGHLIGHT_TAG = "remove_hit"
COUNT_DEBOUNCE_MS = 180


class RemoveTextDialog(ctk.CTkToplevel):
    def __init__(self, view: "PdfView", initial: str = ""):
        super().__init__(view)
        self.view = view
        self.title("Rimuovi testo")
        self.configure(fg_color=COLORS.bg)
        self.resizable(True, False)
        self.minsize(460, 0)
        self.transient(view.winfo_toplevel())
        self._count_job: str | None = None
        self._undo: list[tuple[int, str]] | None = None

        self.case_var = ctk.BooleanVar(value=False)
        self.word_var = ctk.BooleanVar(value=False)
        self.regex_var = ctk.BooleanVar(value=False)
        self.flex_var = ctk.BooleanVar(value=True)
        self.tidy_var = ctk.BooleanVar(value=True)
        self.save_var = ctk.BooleanVar(value=False)
        self.scope_var = ctk.StringVar(value=SCOPE_CURRENT)

        self._build()
        if initial:
            self.pattern_box.insert("1.0", initial)
        self.after(60, self._focus_pattern)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self._refresh_count()
        self._fit(place=True)

    def _fit(self, place: bool = False) -> None:
        """Adatta l'altezza al contenuto; alla prima apertura si posiziona sopra l'anteprima."""
        try:
            self.update_idletasks()
            width = max(500, self.winfo_width() if not place else 0)
            height = self.body.winfo_reqheight() + 24
            if place:
                x = self.view.winfo_rootx() + max(0, self.view.winfo_width() - width - 40)
                y = self.view.winfo_rooty() + 110
                self.geometry(f"{width}x{height}+{x}+{y}")
            else:
                self.geometry(f"{width}x{height}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        body = ctk.CTkFrame(self, fg_color=COLORS.surface, corner_radius=14, border_width=1,
                            border_color=COLORS.border)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self.body = body

        ctk.CTkLabel(body, text="Testo da rimuovere", font=font(12, "bold"), text_color=COLORS.text,
                     anchor="w").pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            body, text="Ogni occorrenza viene sostituita con niente. Puoi incollare anche piu' righe.",
            font=font(10), text_color=COLORS.text_faint, anchor="w", justify="left", wraplength=420,
        ).pack(fill="x", padx=16, pady=(0, 6))
        self.pattern_box = ctk.CTkTextbox(
            body, height=74, font=mono(12), wrap="word", fg_color=COLORS.surface_alt, text_color=COLORS.text,
            border_width=1, border_color=COLORS.border, corner_radius=8,
        )
        self.pattern_box.pack(fill="x", padx=16)
        self.pattern_box._textbox.configure(insertbackground=COLORS.accent)
        self.pattern_box._textbox.bind("<<Modified>>", self._on_pattern_modified)
        self.pattern_box._textbox.bind("<Control-Return>", lambda _e: (self.remove(), "break")[1])

        options = ctk.CTkFrame(body, fg_color="transparent")
        options.pack(fill="x", padx=16, pady=(10, 0))
        options.grid_columnconfigure((0, 1), weight=1)
        for index, (text, variable) in enumerate((
            ("Maiuscole/minuscole", self.case_var),
            ("Parola intera", self.word_var),
            ("Espressione regolare", self.regex_var),
            ("Spazi e a capo flessibili", self.flex_var),
        )):
            self._checkbox(options, text, variable).grid(row=index // 2, column=index % 2, sticky="w", pady=3)
        self._checkbox(body, "Elimina righe, punti elenco e titoli rimasti vuoti", self.tidy_var).pack(
            anchor="w", padx=16, pady=(3, 0)
        )
        self._checkbox(body, "Ricorda e applica alle prossime estrazioni", self.save_var).pack(
            anchor="w", padx=16, pady=(6, 0)
        )

        scope_row = ctk.CTkFrame(body, fg_color="transparent")
        scope_row.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(scope_row, text="Applica a", font=font(10, "bold"), text_color=COLORS.text_muted).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkSegmentedButton(
            scope_row, values=[SCOPE_CURRENT, SCOPE_ALL], variable=self.scope_var, font=font(11),
            command=lambda _v: self._refresh_count(), fg_color=COLORS.surface_alt,
            selected_color=COLORS.accent_dark, selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt, unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text, height=28,
        ).pack(side="left")

        self.count_label = ctk.CTkLabel(body, text="", font=font(11), text_color=COLORS.text_muted, anchor="w",
                                        justify="left", wraplength=420)
        self.count_label.pack(fill="x", padx=16, pady=(10, 0))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(10, 14))
        buttons.grid_columnconfigure(0, weight=1)
        self.undo_button = self._button(buttons, "Annulla rimozione", self.undo)
        self.undo_button.grid(row=0, column=0, sticky="w")
        self.undo_button.configure(state="disabled")
        self._button(buttons, "Chiudi", self.close).grid(row=0, column=1, sticky="e", padx=(6, 6))
        self.remove_button = ctk.CTkButton(
            buttons, text="Rimuovi tutto", height=34, font=font(12, "bold"), corner_radius=9,
            fg_color=COLORS.accent, hover_color=COLORS.accent_hover, text_color="#04130F", command=self.remove,
        )
        self.remove_button.grid(row=0, column=2, sticky="e")

        self.rules_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.rules_frame.pack(fill="x", padx=16, pady=(0, 12))
        self.refresh_rules()

    def _checkbox(self, parent, text: str, variable: ctk.BooleanVar) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            parent, text=text, variable=variable, command=self._refresh_count, font=font(11),
            text_color=COLORS.text_muted, checkbox_width=18, checkbox_height=18, border_width=2,
            fg_color=COLORS.accent, hover_color=COLORS.accent_hover,
        )

    def _button(self, parent, text: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent, text=text, height=34, font=font(11, "bold"), corner_radius=9, fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover, text_color=COLORS.text, border_width=1,
            border_color=COLORS.border, command=command,
        )

    def _focus_pattern(self) -> None:
        try:
            self.lift()
            self.pattern_box.focus_set()
            self.pattern_box._textbox.tag_add("sel", "1.0", "end-1c")
        except tk.TclError:
            pass

    def set_pattern(self, text: str) -> None:
        self.pattern_box.delete("1.0", "end")
        self.pattern_box.insert("1.0", text)
        self._focus_pattern()
        self._refresh_count()

    # ------------------------------------------------------------------ regole salvate
    def refresh_rules(self) -> None:
        for child in self.rules_frame.winfo_children():
            child.destroy()
        rules = self.view.removal_rules
        if not rules:
            return
        ctk.CTkFrame(self.rules_frame, height=1, fg_color=COLORS.border).pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            self.rules_frame, text="Regole applicate alle prossime estrazioni", font=font(10, "bold"),
            text_color=COLORS.text_muted, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        for index, rule in enumerate(rules):
            row = ctk.CTkFrame(self.rules_frame, fg_color=COLORS.surface_alt, corner_radius=8)
            row.pack(fill="x", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=rule.label(), font=mono(10), text_color=COLORS.text, anchor="w").grid(
                row=0, column=0, sticky="ew", padx=10, pady=4
            )
            ctk.CTkButton(
                row, text="Usa", width=44, height=24, font=font(10, "bold"), fg_color="transparent",
                hover_color=COLORS.surface_hover, text_color=COLORS.accent,
                command=lambda value=rule: self._load_rule(value),
            ).grid(row=0, column=1, padx=(0, 2))
            ctk.CTkButton(
                row, text="✕", width=28, height=24, font=font(11, "bold"), fg_color="transparent",
                hover_color=COLORS.red_dark, text_color=COLORS.text_muted,
                command=lambda position=index: self._delete_rule(position),
            ).grid(row=0, column=2, padx=(0, 6))

    def _load_rule(self, rule: RemovalRule) -> None:
        self.case_var.set(rule.match_case)
        self.word_var.set(rule.whole_word)
        self.regex_var.set(rule.regex)
        self.flex_var.set(rule.flexible_spaces)
        self.tidy_var.set(rule.tidy)
        self.set_pattern(rule.pattern)

    def _delete_rule(self, index: int) -> None:
        self.view.delete_removal_rule(index)
        self.refresh_rules()
        self._fit()

    # ------------------------------------------------------------------ conteggio ed evidenziazione
    def current_rule(self) -> RemovalRule:
        pattern = self.pattern_box.get("1.0", "end-1c")
        return RemovalRule(
            pattern=pattern,
            match_case=bool(self.case_var.get()),
            whole_word=bool(self.word_var.get()),
            regex=bool(self.regex_var.get()),
            flexible_spaces=bool(self.flex_var.get()),
            tidy=bool(self.tidy_var.get()),
        )

    def _on_pattern_modified(self, _event=None) -> None:
        widget = self.pattern_box._textbox
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if self._count_job is not None:
            self.after_cancel(self._count_job)
        self._count_job = self.after(COUNT_DEBOUNCE_MS, self._refresh_count)

    def _targets(self) -> list[int]:
        if self.scope_var.get() == SCOPE_ALL:
            return [index for index, doc in enumerate(self.view.docs) if doc.source.strip()]
        current = self.view.current
        return [current] if 0 <= current < len(self.view.docs) else []

    def _refresh_count(self) -> None:
        self._count_job = None
        self.view._commit_editor()
        self.clear_highlight()
        rule = self.current_rule()
        if not rule.pattern.strip() and not rule.regex:
            self.count_label.configure(text="Scrivi o incolla il testo da rimuovere.", text_color=COLORS.text_faint)
            self.remove_button.configure(state="disabled")
            return
        try:
            targets = self._targets()
            counts = [count_matches(self.view.docs[index].source, rule) for index in targets]
        except RemovalPatternError as exc:
            self.count_label.configure(text=str(exc), text_color=COLORS.red)
            self.remove_button.configure(state="disabled")
            return
        total = sum(counts)
        self._highlight(rule)
        if not targets:
            text, color = "Nessun documento aperto.", COLORS.text_faint
        elif total == 0:
            text, color = "Nessuna occorrenza trovata.", COLORS.amber
        elif self.scope_var.get() == SCOPE_ALL:
            touched = sum(1 for count in counts if count)
            text = f"{total} occorrenze in {touched} document{'o' if touched == 1 else 'i'} su {len(targets)}."
            color = COLORS.text_muted
        else:
            text = f"{total} occorrenz{'a' if total == 1 else 'e'} nel documento aperto (evidenziate nell'editor)."
            color = COLORS.text_muted
        self.count_label.configure(text=text, text_color=color)
        can_save = self.save_var.get() and bool(rule.pattern.strip() or rule.regex)
        self.remove_button.configure(state="normal" if total or can_save else "disabled")

    def _highlight(self, rule: RemovalRule) -> None:
        widget = self.view.editor._textbox
        widget.tag_configure(HIGHLIGHT_TAG, background=COLORS.amber, foreground="#111827")
        try:
            spans = find_matches(widget.get("1.0", "end-1c"), rule)
        except RemovalPatternError:
            return
        for start, end in spans[:2000]:
            widget.tag_add(HIGHLIGHT_TAG, f"1.0+{start}c", f"1.0+{end}c")
        if spans:
            widget.see(f"1.0+{spans[0][0]}c")

    def clear_highlight(self) -> None:
        try:
            self.view.editor._textbox.tag_remove(HIGHLIGHT_TAG, "1.0", "end")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ azioni
    def remove(self) -> None:
        rule = self.current_rule()
        try:
            snapshot, removed, touched = self.view.remove_text(rule, self._targets())
        except RemovalPatternError as exc:
            self.count_label.configure(text=str(exc), text_color=COLORS.red)
            return
        saved = False
        if self.save_var.get():
            saved = self.view.add_removal_rule(rule)
            self.refresh_rules()
            self._fit()
        if removed:
            self._undo = snapshot
            self.undo_button.configure(state="normal")
        parts = []
        if removed:
            parts.append(f"Rimoss{'a' if removed == 1 else 'e'} {removed} occorrenz{'a' if removed == 1 else 'e'}"
                         + (f" in {touched} documenti" if touched > 1 else "") + ".")
        if saved:
            parts.append("Regola salvata per le prossime estrazioni.")
        self._refresh_count()
        if parts:
            self.count_label.configure(text=" ".join(parts), text_color=COLORS.green)

    def undo(self) -> None:
        if not self._undo:
            return
        self.view.restore_sources(self._undo)
        self._undo = None
        self.undo_button.configure(state="disabled")
        self._refresh_count()
        self.count_label.configure(text="Rimozione annullata.", text_color=COLORS.text_muted)

    def close(self) -> None:
        self.clear_highlight()
        if self._count_job is not None:
            try:
                self.after_cancel(self._count_job)
            except (tk.TclError, ValueError):
                pass
        self.view._remove_dialog = None
        self.destroy()
