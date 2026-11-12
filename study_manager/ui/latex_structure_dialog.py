"""Finestra "Struttura LaTeX" della vista PDF e Slide.

Si apre all'importazione dei PDF (e dal pulsante «Struttura LaTeX…») e chiede:

- se le sezioni devono essere numerate (``\\section``) o no (``\\section*``);
- se il testo verra' inglobato in un altro documento: in quel caso si sceglie
  il livello di partenza e il titolo della sezione radice (proposto dal nome
  del documento) e il .tex esportato e' un frammento senza preambolo;
- altrimenti, quale template usare per il documento autonomo.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from ..config import COLORS
from ..services.pdf_tools.export import SECTION_LEVELS
from .theme import font, mono

if TYPE_CHECKING:  # pragma: no cover
    from .pdf_view import PdfView

BASE_LEVELS = ("part", "chapter", "section", "subsection", "subsubsection")
LEVEL_LABELS = {name: f"\\{name}" for name in BASE_LEVELS}
NUMBER_MODES = ("Numerate", "Non numerate")
EMBED_MODES = ("Documento autonomo", "Inglobato in un altro documento")


def structure_example(numbered: bool, embed: bool, base_level: str, root_title: str) -> str:
    star = "" if numbered else "*"
    if embed:
        start = SECTION_LEVELS.index(base_level) if base_level in SECTION_LEVELS else 2
        names = [SECTION_LEVELS[min(start + offset, len(SECTION_LEVELS) - 1)] for offset in range(3)]
        title = root_title.strip() or "Nome documento"
        return (
            f"\\{names[0]}{star}{{{title}}}\n"
            f"  \\{names[1]}{star}{{titolo slide}}\n"
            f"    \\{names[2]}{star}{{sottotitolo}}"
        )
    return f"\\section{star}{{titolo slide}}\n  \\subsection{star}{{sottotitolo}}"


class LatexStructureDialog(ctk.CTkToplevel):
    """Dialogo modale: ``result`` e' None se annullato, altrimenti un dict."""

    def __init__(
        self,
        view: "PdfView",
        defaults: dict[str, Any],
        proposed_title: str,
        multiple: bool = False,
        heading: str = "Struttura LaTeX",
        confirm_text: str = "Conferma",
        allow_apply_all: bool = False,
    ):
        super().__init__(view)
        self.view = view
        self.result: dict[str, Any] | None = None
        self.multiple = multiple
        self.title(heading)
        self.configure(fg_color=COLORS.bg)
        self.resizable(True, False)
        self.minsize(500, 0)
        self.transient(view.winfo_toplevel())

        self.number_var = ctk.StringVar(value=NUMBER_MODES[0] if defaults.get("numbered", True) else NUMBER_MODES[1])
        self.embed_var = ctk.StringVar(value=EMBED_MODES[1] if defaults.get("embed") else EMBED_MODES[0])
        level = defaults.get("base_level", "section")
        self.level_var = ctk.StringVar(value=LEVEL_LABELS.get(level, "\\section"))
        self.root_var = ctk.StringVar(value=proposed_title)
        self.apply_all_var = ctk.BooleanVar(value=False)
        self._allow_apply_all = allow_apply_all

        self._build(heading, confirm_text)
        self._on_change()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())
        self.bind("<Return>", lambda _e: self.confirm())
        self._place()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ layout
    def _build(self, heading: str, confirm_text: str) -> None:
        body = ctk.CTkFrame(self, fg_color=COLORS.surface, corner_radius=14, border_width=1,
                            border_color=COLORS.border)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self.body = body

        ctk.CTkLabel(body, text=heading, font=font(13, "bold"), text_color=COLORS.text, anchor="w").pack(
            fill="x", padx=16, pady=(14, 2)
        )
        ctk.CTkLabel(
            body, text="Come vuoi che venga costruito il file .tex? Puoi cambiarlo dopo con «Struttura LaTeX…».",
            font=font(10), text_color=COLORS.text_faint, anchor="w", justify="left", wraplength=440,
        ).pack(fill="x", padx=16, pady=(0, 10))

        self._label(body, "Sezioni")
        self._segmented(body, NUMBER_MODES, self.number_var).pack(anchor="w", padx=16, pady=(0, 10))

        self._label(body, "Il testo verra' inglobato in un altro documento?")
        self._segmented(body, EMBED_MODES, self.embed_var).pack(anchor="w", padx=16, pady=(0, 10))

        # --- inglobazione
        self.embed_frame = ctk.CTkFrame(body, fg_color=COLORS.surface_alt, corner_radius=10)
        self.embed_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.embed_frame, text="Parti da", font=font(10, "bold"), text_color=COLORS.text_muted).grid(
            row=0, column=0, sticky="w", padx=(12, 8), pady=(10, 4)
        )
        ctk.CTkOptionMenu(
            self.embed_frame, values=[LEVEL_LABELS[name] for name in BASE_LEVELS], variable=self.level_var,
            command=lambda _v: self._on_change(), width=160, font=font(11), fg_color=COLORS.surface,
            button_color=COLORS.border, button_hover_color=COLORS.surface_hover, text_color=COLORS.text,
            dropdown_fg_color=COLORS.surface_alt, dropdown_text_color=COLORS.text,
            dropdown_hover_color=COLORS.surface_hover,
        ).grid(row=0, column=1, sticky="w", pady=(10, 4))
        ctk.CTkLabel(self.embed_frame, text="Titolo", font=font(10, "bold"), text_color=COLORS.text_muted).grid(
            row=1, column=0, sticky="w", padx=(12, 8), pady=(4, 4)
        )
        self.root_entry = ctk.CTkEntry(
            self.embed_frame, textvariable=self.root_var, font=font(11), fg_color=COLORS.surface,
            border_color=COLORS.border, text_color=COLORS.text,
        )
        self.root_entry.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(4, 4))
        self.root_var.trace_add("write", lambda *_a: self._on_change())
        hint = (
            "Con piu' PDF il titolo e' il nome di ciascun documento."
            if self.multiple else "Proposto dal nome del documento: modificalo se serve."
        )
        ctk.CTkLabel(
            self.embed_frame, text=hint + " Il .tex esportato sara' un frammento senza preambolo, "
            "pronto per \\input{…}.", font=font(10), text_color=COLORS.text_faint, anchor="w",
            justify="left", wraplength=420,
        ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 10))
        if self.multiple:
            self.root_entry.configure(state="disabled")

        # --- documento autonomo
        self.template_frame = ctk.CTkFrame(body, fg_color=COLORS.surface_alt, corner_radius=10)
        self.template_frame.grid_columnconfigure(0, weight=1)
        self.template_label = ctk.CTkLabel(
            self.template_frame, text="", font=font(10), text_color=COLORS.text_muted, anchor="w",
            justify="left", wraplength=300,
        )
        self.template_label.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        ctk.CTkButton(
            self.template_frame, text="Carica template…", height=28, width=130, font=font(11, "bold"),
            fg_color=COLORS.surface, hover_color=COLORS.surface_hover, text_color=COLORS.text,
            border_width=1, border_color=COLORS.border, command=self._load_template,
        ).grid(row=0, column=1, sticky="e", padx=12, pady=10)

        self.example_title = self._label(body, "Struttura risultante")
        self.example = ctk.CTkLabel(
            body, text="", font=mono(11), text_color=COLORS.text, anchor="w", justify="left",
            fg_color=COLORS.surface_alt, corner_radius=8,
        )
        self.example.pack(fill="x", padx=16, pady=(0, 8), ipadx=10, ipady=6)

        if self._allow_apply_all:
            ctk.CTkCheckBox(
                body, text="Applica a tutti i documenti estratti", variable=self.apply_all_var, font=font(11),
                text_color=COLORS.text_muted, checkbox_width=18, checkbox_height=18, border_width=2,
                fg_color=COLORS.accent,
            ).pack(anchor="w", padx=16, pady=(2, 4))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(8, 14))
        buttons.grid_columnconfigure(0, weight=1)
        self._button(buttons, "Annulla", self.cancel).grid(row=0, column=1, padx=(0, 6))
        self._button(buttons, confirm_text, self.confirm, primary=True).grid(row=0, column=2)

    def _label(self, parent, text: str) -> ctk.CTkLabel:
        label = ctk.CTkLabel(parent, text=text, font=font(10, "bold"), text_color=COLORS.text_muted, anchor="w")
        label.pack(fill="x", padx=16, pady=(0, 4))
        return label

    def _segmented(self, parent, values, variable) -> ctk.CTkSegmentedButton:
        return ctk.CTkSegmentedButton(
            parent, values=list(values), variable=variable, font=font(11), command=lambda _v: self._on_change(),
            fg_color=COLORS.surface_alt, selected_color=COLORS.accent_dark, selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt, unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text, height=28,
        )

    def _button(self, parent, text: str, command, primary: bool = False) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent, text=text, height=32, width=110, font=font(11, "bold"), corner_radius=9,
            fg_color=COLORS.accent if primary else COLORS.surface_alt,
            hover_color=COLORS.accent_dark if primary else COLORS.surface_hover,
            text_color=COLORS.text, border_width=0 if primary else 1, border_color=COLORS.border, command=command,
        )

    def _place(self) -> None:
        try:
            self.update_idletasks()
            width = max(520, self.body.winfo_reqwidth() + 24)
            height = self.body.winfo_reqheight() + 24
            top = self.view.winfo_toplevel()
            x = top.winfo_rootx() + max(0, (top.winfo_width() - width) // 2)
            y = top.winfo_rooty() + max(0, (top.winfo_height() - height) // 3)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ logica
    def _embed(self) -> bool:
        return self.embed_var.get() == EMBED_MODES[1]

    def _level(self) -> str:
        for name, label in LEVEL_LABELS.items():
            if label == self.level_var.get():
                return name
        return "section"

    def _on_change(self) -> None:
        embed = self._embed()
        if embed:
            self.template_frame.pack_forget()
            self.embed_frame.pack(fill="x", padx=16, pady=(0, 10), before=self.example_title)
        else:
            self.embed_frame.pack_forget()
            self.template_frame.pack(fill="x", padx=16, pady=(0, 10), before=self.example_title)
            name = self.view.template_name()
            self.template_label.configure(
                text=f"Template: {name}" if name else "Template: nessuno (preambolo standard di StudyFlow)."
            )
        title = "Nome documento" if self.multiple else self.root_var.get()
        self.example.configure(
            text=structure_example(self.number_var.get() == NUMBER_MODES[0], embed, self._level(), title)
        )
        self._place()

    def _load_template(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.view.choose_template(parent=self)
        try:
            self.grab_set()
            self.lift()
        except tk.TclError:
            pass
        self._on_change()

    def confirm(self) -> None:
        self.result = {
            "numbered": self.number_var.get() == NUMBER_MODES[0],
            "embed": self._embed(),
            "base_level": self._level(),
            "root_title": "" if self.multiple else self.root_var.get().strip(),
            "apply_all": bool(self.apply_all_var.get()),
        }
        self.destroy()

    def cancel(self) -> None:
        self.result = None
        self.destroy()

    def show(self) -> dict[str, Any] | None:
        self.wait_window()
        return self.result
