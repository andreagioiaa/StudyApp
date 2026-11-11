"""Vista "PDF e Slide": trascrizione, editor con anteprima live ed esportazione.

Layout: a sinistra sorgente, motori, pulizia ed esportazione; a destra un
editor (Markdown + LaTeX oppure LaTeX puro) affiancato all'anteprima, che
mostra a scelta il rendering con le formule, il codice LaTeX finale o il TXT.

Tutta l'elaborazione pesante gira in un thread separato
(``PdfConversionController``); la vista legge gli eventi con ``after``.
"""

from __future__ import annotations

import logging
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from ..config import COLORS
from ..services.pdf_tools.controller import PdfConversionController, list_pdfs
from ..services.pdf_tools.document import (
    BLOCK_BULLET,
    BLOCK_HEADING,
    BLOCK_MATH,
    BLOCK_NUMBER,
    BLOCK_TABLE,
    RenderOptions,
    parse_markdown,
    split_inline_math,
    strip_inline_markup,
    unescape_text,
)
from ..services.pdf_tools.export import (
    EXPORT_FORMATS,
    FORMAT_DOCX,
    FORMAT_TEX,
    FORMAT_TXT,
    SOURCE_LATEX,
    SOURCE_MARKDOWN,
    ExportError,
    export_file,
    find_pandoc,
    katex_available,
    latex_to_markdown,
    markdown_to_latex,
    render_source,
    write_html_preview,
)
from ..services.pdf_tools.extract import (
    DEFAULT_MARKER_COMMAND,
    DEFAULT_NOUGAT_COMMAND,
    ENGINE_LABELS,
    ExtractOptions,
    PdfCancelled,
    backend_status,
)
from ..services.pdf_tools.text_removal import (
    RemovalRule,
    apply_rules,
    remove_text,
    rules_from_json,
    rules_to_json,
)
from .math_render import MathRenderer
from .remove_text_dialog import RemoveTextDialog
from .theme import font, mono
from .widgets import Card, PageHeader, separator


LOGGER = logging.getLogger(__name__)

ENGINE_CHOICES = {"Automatico": "auto", "PyMuPDF": "pymupdf", "pdfplumber": "pdfplumber"}
OCR_CHOICES = {
    "Automatico (solo scansioni)": "auto",
    "Disattivato": "off",
    "Forza OCR su ogni pagina": "force",
}
OCR_ENGINE_CHOICES = {"Tesseract": "tesseract", "PaddleOCR": "paddle"}
MATH_CHOICES = {"Integrato (euristico)": "none", "Marker": "marker", "Nougat": "nougat"}
MATH_SCOPE_CHOICES = {"Solo pagine con formule": "math_pages", "Tutte le pagine": "all"}
BATCH_FORMAT_CHOICES = {".txt": [FORMAT_TXT], ".tex": [FORMAT_TEX], ".docx": [FORMAT_DOCX], "Tutti": list(EXPORT_FORMATS)}
PREVIEW_MODES = ("Anteprima", "LaTeX", "TXT")
SOURCE_MODES = ("Markdown", "LaTeX")
LAYOUT_MODES = ("Affiancati", "Sovrapposti")

SETTING_DEFAULTS = {
    "pdf_engine": "auto",
    "pdf_ocr": "auto",
    "pdf_ocr_engine": "tesseract",
    "pdf_ocr_lang": "ita+eng",
    "pdf_math_engine": "none",
    "pdf_math_scope": "math_pages",
    "pdf_marker_cmd": "",
    "pdf_nougat_cmd": "",
    "pdf_clean_headers": "1",
    "pdf_clean_pagenum": "1",
    "pdf_dehyphenate": "1",
    "pdf_detect_math": "1",
    "pdf_markers": "1",
    "pdf_headings": "1",
    "pdf_preamble": "1",
    "pdf_docx_pagebreak": "0",
    "pdf_layout": "Affiancati",
    "pdf_last_dir": "",
    "pdf_export_dir": "",
    "pdf_batch_format": ".docx",
    "pdf_remove_rules": "[]",
    "pdf_apply_remove_rules": "1",
}

PREVIEW_RENDER_CHUNK = 18
EDIT_DEBOUNCE_MS = 380


@dataclass
class DocState:
    path: Path
    title: str
    source: str
    fmt: str = SOURCE_MARKDOWN
    warnings: list[str] = field(default_factory=list)
    engines: dict[str, int] = field(default_factory=dict)
    error: str = ""


def _label_for(choices: dict[str, str], value: str) -> str:
    for label, key in choices.items():
        if key == value:
            return label
    return next(iter(choices))


class PdfView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.controller = PdfConversionController()
        self.docs: list[DocState] = []
        self.current: int = -1
        self.sources: list[Path] = []
        self._poll_job: str | None = None
        self._edit_job: str | None = None
        self._render_job: str | None = None
        self._render_generation = 0
        self._suspend_modified = False
        self._task_label = ""
        self._task_done: Callable[[dict[str, Any]], None] | None = None
        self._renderer: MathRenderer | None = None
        self._preview_images: list[tk.PhotoImage] = []
        self._remove_dialog: RemoveTextDialog | None = None

        settings = self._load_settings()
        self._build_vars(settings)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        PageHeader(
            self,
            "PDF e Slide",
            "Trascrivi slide e dispense in TXT, LaTeX e Word, con formule e anteprima modificabile. Tutto in locale.",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        self._build_controls()
        self._build_workspace()
        self._refresh_backends()
        self._update_buttons()
        self._show_placeholder()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> dict[str, str]:
        try:
            stored = self.db.get_settings()
        except Exception:
            stored = {}
        return {key: str(stored.get(key, default)) for key, default in SETTING_DEFAULTS.items()}

    @staticmethod
    def _bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _build_vars(self, settings: dict[str, str]) -> None:
        self.engine_var = ctk.StringVar(value=_label_for(ENGINE_CHOICES, settings["pdf_engine"]))
        self.ocr_var = ctk.StringVar(value=_label_for(OCR_CHOICES, settings["pdf_ocr"]))
        self.ocr_engine_var = ctk.StringVar(value=_label_for(OCR_ENGINE_CHOICES, settings["pdf_ocr_engine"]))
        self.ocr_lang_var = ctk.StringVar(value=settings["pdf_ocr_lang"] or "ita+eng")
        self.math_var = ctk.StringVar(value=_label_for(MATH_CHOICES, settings["pdf_math_engine"]))
        self.math_scope_var = ctk.StringVar(value=_label_for(MATH_SCOPE_CHOICES, settings["pdf_math_scope"]))
        self.marker_cmd_var = ctk.StringVar(value=settings["pdf_marker_cmd"])
        self.nougat_cmd_var = ctk.StringVar(value=settings["pdf_nougat_cmd"])
        self.clean_headers_var = ctk.BooleanVar(value=self._bool(settings["pdf_clean_headers"]))
        self.clean_pagenum_var = ctk.BooleanVar(value=self._bool(settings["pdf_clean_pagenum"]))
        self.dehyphenate_var = ctk.BooleanVar(value=self._bool(settings["pdf_dehyphenate"]))
        self.detect_math_var = ctk.BooleanVar(value=self._bool(settings["pdf_detect_math"]))
        self.markers_var = ctk.BooleanVar(value=self._bool(settings["pdf_markers"]))
        self.headings_var = ctk.BooleanVar(value=self._bool(settings["pdf_headings"]))
        self.preamble_var = ctk.BooleanVar(value=self._bool(settings["pdf_preamble"]))
        self.pagebreak_var = ctk.BooleanVar(value=self._bool(settings["pdf_docx_pagebreak"]))
        self.layout_var = ctk.StringVar(value=settings["pdf_layout"] if settings["pdf_layout"] in LAYOUT_MODES else LAYOUT_MODES[0])
        self.batch_format_var = ctk.StringVar(
            value=settings["pdf_batch_format"] if settings["pdf_batch_format"] in BATCH_FORMAT_CHOICES else ".docx"
        )
        self.recursive_var = ctk.BooleanVar(value=False)
        self.preview_mode_var = ctk.StringVar(value=PREVIEW_MODES[0])
        self.source_mode_var = ctk.StringVar(value=SOURCE_MODES[0])
        self.doc_var = ctk.StringVar(value="")
        self._last_dir = settings["pdf_last_dir"]
        self._export_dir = settings["pdf_export_dir"]
        self.removal_rules: list[RemovalRule] = rules_from_json(settings["pdf_remove_rules"])
        self.apply_rules_var = ctk.BooleanVar(value=self._bool(settings["pdf_apply_remove_rules"]))

    def _save_settings(self, *_args) -> None:
        values = {
            "pdf_engine": ENGINE_CHOICES.get(self.engine_var.get(), "auto"),
            "pdf_ocr": OCR_CHOICES.get(self.ocr_var.get(), "auto"),
            "pdf_ocr_engine": OCR_ENGINE_CHOICES.get(self.ocr_engine_var.get(), "tesseract"),
            "pdf_ocr_lang": self.ocr_lang_var.get().strip() or "ita+eng",
            "pdf_math_engine": MATH_CHOICES.get(self.math_var.get(), "none"),
            "pdf_math_scope": MATH_SCOPE_CHOICES.get(self.math_scope_var.get(), "math_pages"),
            "pdf_marker_cmd": self.marker_cmd_var.get().strip(),
            "pdf_nougat_cmd": self.nougat_cmd_var.get().strip(),
            "pdf_clean_headers": "1" if self.clean_headers_var.get() else "0",
            "pdf_clean_pagenum": "1" if self.clean_pagenum_var.get() else "0",
            "pdf_dehyphenate": "1" if self.dehyphenate_var.get() else "0",
            "pdf_detect_math": "1" if self.detect_math_var.get() else "0",
            "pdf_markers": "1" if self.markers_var.get() else "0",
            "pdf_headings": "1" if self.headings_var.get() else "0",
            "pdf_preamble": "1" if self.preamble_var.get() else "0",
            "pdf_docx_pagebreak": "1" if self.pagebreak_var.get() else "0",
            "pdf_layout": self.layout_var.get(),
            "pdf_last_dir": self._last_dir,
            "pdf_export_dir": self._export_dir,
            "pdf_batch_format": self.batch_format_var.get(),
            "pdf_remove_rules": rules_to_json(self.removal_rules),
            "pdf_apply_remove_rules": "1" if self.apply_rules_var.get() else "0",
        }
        try:
            self.db.set_settings(values)
        except Exception:
            LOGGER.exception("Salvataggio impostazioni PDF non riuscito.")

    def extract_options(self) -> ExtractOptions:
        return ExtractOptions(
            engine=ENGINE_CHOICES.get(self.engine_var.get(), "auto"),
            ocr_mode=OCR_CHOICES.get(self.ocr_var.get(), "auto"),
            ocr_engine=OCR_ENGINE_CHOICES.get(self.ocr_engine_var.get(), "tesseract"),
            ocr_language=self.ocr_lang_var.get().strip() or "ita+eng",
            math_engine=MATH_CHOICES.get(self.math_var.get(), "none"),
            math_scope=MATH_SCOPE_CHOICES.get(self.math_scope_var.get(), "math_pages"),
            remove_headers=bool(self.clean_headers_var.get()),
            remove_page_numbers=bool(self.clean_pagenum_var.get()),
            dehyphenate=bool(self.dehyphenate_var.get()),
            detect_math=bool(self.detect_math_var.get()),
            marker_command=self.marker_cmd_var.get().strip(),
            nougat_command=self.nougat_cmd_var.get().strip(),
        )

    def render_options(self, doc: DocState | None = None) -> RenderOptions:
        return RenderOptions(
            include_markers=bool(self.markers_var.get()),
            include_headings=bool(self.headings_var.get()),
            include_preamble=bool(self.preamble_var.get()),
            docx_page_breaks=bool(self.pagebreak_var.get()),
            title=(doc.title if doc is not None else ""),
        )

    # ------------------------------------------------------------------ layout: controlli
    def _build_controls(self) -> None:
        panel = ctk.CTkScrollableFrame(
            self,
            width=318,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        panel.grid(row=1, column=0, sticky="nsw", padx=(0, 12))
        panel.grid_columnconfigure(0, weight=1)
        self.controls = panel

        # Sorgente
        source = self._card(panel, 0, "Sorgente", "Un PDF di slide/dispense oppure una cartella (batch).")
        buttons = ctk.CTkFrame(source, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 8))
        buttons.grid_columnconfigure((0, 1), weight=1)
        self._button(buttons, "Apri PDF…", self.choose_files).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._button(buttons, "Apri cartella…", self.choose_folder).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkCheckBox(
            source,
            text="Includi sottocartelle",
            variable=self.recursive_var,
            font=font(11),
            text_color=COLORS.text_muted,
            checkbox_width=18,
            checkbox_height=18,
            border_width=2,
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
        ).pack(anchor="w", padx=16, pady=(0, 6))
        self.source_label = ctk.CTkLabel(
            source,
            text="Nessun file selezionato.",
            font=font(11),
            text_color=COLORS.text_faint,
            justify="left",
            anchor="w",
            wraplength=270,
        )
        self.source_label.pack(fill="x", padx=16, pady=(0, 10))
        self.extract_button = ctk.CTkButton(
            source, text="▶  Estrai testo", height=40, font=font(13, "bold"),
            fg_color=COLORS.accent, hover_color=COLORS.accent_hover, text_color="#04130F",
            corner_radius=10, command=self.start_extraction,
        )
        self.extract_button.pack(fill="x", padx=16, pady=(0, 8))
        self.progress = ctk.CTkProgressBar(source, height=8, progress_color=COLORS.accent, fg_color=COLORS.surface_alt)
        self.progress.pack(fill="x", padx=16, pady=(0, 6))
        self.progress.set(0)
        status_row = ctk.CTkFrame(source, fg_color="transparent")
        status_row.pack(fill="x", padx=16, pady=(0, 14))
        status_row.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(
            status_row, text="Pronto.", font=font(11), text_color=COLORS.text_muted,
            justify="left", anchor="w", wraplength=190,
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.cancel_button = self._button(status_row, "Annulla", self.cancel, danger=True)
        self.cancel_button.configure(width=74, height=28)
        self.cancel_button.grid(row=0, column=1, sticky="e", padx=(6, 0))

        # Motori
        engines = self._card(panel, 3, "Motori di trascrizione", "Tutto locale: nessuna API esterna.")
        self._option(engines, "Estrazione testo", self.engine_var, list(ENGINE_CHOICES))
        self._option(engines, "OCR (PDF scansionati)", self.ocr_var, list(OCR_CHOICES))
        row = ctk.CTkFrame(engines, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=2)
        self._small_label(row, "Motore OCR").grid(row=0, column=0, sticky="w")
        self._small_label(row, "Lingue").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ctk.CTkOptionMenu(
            row, values=list(OCR_ENGINE_CHOICES), variable=self.ocr_engine_var, command=self._on_engine_change,
            **self._menu_style(),
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        lang = ctk.CTkEntry(row, textvariable=self.ocr_lang_var, height=32, **self._entry_style())
        lang.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        lang.bind("<FocusOut>", self._save_settings)
        self._option(engines, "Formule matematiche", self.math_var, list(MATH_CHOICES))
        self._option(engines, "Ambito Marker/Nougat", self.math_scope_var, list(MATH_SCOPE_CHOICES))
        for label, variable, default in (
            ("Comando Marker", self.marker_cmd_var, DEFAULT_MARKER_COMMAND),
            ("Comando Nougat", self.nougat_cmd_var, DEFAULT_NOUGAT_COMMAND),
        ):
            self._small_label(engines, f"{label} (vuoto = predefinito)").pack(anchor="w", padx=16)
            entry = ctk.CTkEntry(engines, textvariable=variable, height=30, **self._entry_style(mono_font=True))
            entry.pack(fill="x", padx=16, pady=(4, 2))
            ctk.CTkLabel(
                engines, text=default, font=mono(9), text_color=COLORS.text_faint, anchor="w",
                justify="left", wraplength=270,
            ).pack(fill="x", padx=16, pady=(0, 6))
            entry.bind("<FocusOut>", lambda _e: (self._save_settings(), self._refresh_backends()))
        status_header = ctk.CTkFrame(engines, fg_color="transparent")
        status_header.pack(fill="x", padx=16, pady=(2, 2))
        self._small_label(status_header, "Stato dei motori").pack(side="left")
        ctk.CTkButton(
            status_header, text="Aggiorna", width=70, height=22, font=font(10, "bold"),
            fg_color="transparent", hover_color=COLORS.surface_hover, text_color=COLORS.accent,
            command=self._refresh_backends,
        ).pack(side="right")
        self.backend_frame = ctk.CTkFrame(engines, fg_color="transparent")
        self.backend_frame.pack(fill="x", padx=16, pady=(2, 14))

        # Pulizia
        cleanup = self._card(panel, 2, "Pulizia e struttura", "Applicata alla prossima estrazione.")
        for text, variable in (
            ("Rimuovi intestazioni e piè di pagina ripetuti", self.clean_headers_var),
            ("Rimuovi numeri di pagina isolati", self.clean_pagenum_var),
            ("Correggi le sillabazioni a fine riga", self.dehyphenate_var),
            ("Riconosci formule (LaTeX $…$ e $$…$$)", self.detect_math_var),
        ):
            self._switch(cleanup, text, variable, self._save_settings)
        self.rules_switch = ctk.CTkSwitch(
            cleanup, text="", variable=self.apply_rules_var, command=self._save_settings, font=font(11),
            text_color=COLORS.text_muted, progress_color=COLORS.accent, button_color=COLORS.text,
            button_hover_color="#FFFFFF", fg_color=COLORS.border, switch_width=34, switch_height=18,
        )
        self.rules_switch.pack(anchor="w", padx=16, pady=3)
        self._update_rules_label()
        manage = self._button(cleanup, "Rimuovi testo…  (Ctrl+H)", self.open_remove_dialog)
        manage.pack(fill="x", padx=16, pady=(8, 14))

        # Esportazione
        export = self._card(panel, 1, "Esportazione", "Le opzioni valgono per anteprima, copia ed esportazione.")

        for text, variable in (
            ("Marcatori e numeri di slide", self.markers_var),
            ("Titoli delle slide (intestazioni)", self.headings_var),
            ("Preambolo LaTeX (documento compilabile)", self.preamble_var),
            ("Word: nuova pagina per ogni slide", self.pagebreak_var),
        ):
            self._switch(export, text, variable, self._on_render_option)

        quick = ctk.CTkFrame(export, fg_color="transparent")
        quick.pack(fill="x", padx=16, pady=(10, 6))
        quick.grid_columnconfigure((0, 1), weight=1)
        self.copy_button = self._button(quick, "Copia LaTeX", self.copy_latex)
        self.copy_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.katex_button = self._button(quick, "Anteprima KaTeX", self.open_katex_preview)
        self.katex_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self._small_label(export, "Esporta il documento aperto").pack(anchor="w", padx=16, pady=(6, 4))
        formats = ctk.CTkFrame(export, fg_color="transparent")
        formats.pack(fill="x", padx=16)
        formats.grid_columnconfigure((0, 1, 2), weight=1)
        self.export_buttons: list[ctk.CTkButton] = []
        for column, fmt in enumerate(EXPORT_FORMATS):
            button = self._button(formats, f".{fmt}", lambda value=fmt: self.export_current(value))
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 3, 0 if column == 2 else 3))
            self.export_buttons.append(button)

        self._small_label(export, "Esporta tutti i documenti estratti").pack(anchor="w", padx=16, pady=(10, 4))
        batch = ctk.CTkFrame(export, fg_color="transparent")
        batch.pack(fill="x", padx=16, pady=(0, 16))
        batch.grid_columnconfigure(1, weight=1)
        ctk.CTkOptionMenu(
            batch, values=list(BATCH_FORMAT_CHOICES), variable=self.batch_format_var, width=86,
            command=lambda _v: self._save_settings(), **self._menu_style(),
        ).grid(row=0, column=0, sticky="w")
        self.batch_button = self._button(batch, "Esporta tutti…", self.export_all)
        self.batch_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    # ------------------------------------------------------------------ layout: area di lavoro
    def _build_workspace(self) -> None:
        workspace = Card(self)
        workspace.grid(row=1, column=1, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(workspace, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        toolbar.grid_columnconfigure(1, weight=1)
        self._small_label(toolbar, "Documento").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.doc_menu = ctk.CTkOptionMenu(
            toolbar, values=["—"], variable=self.doc_var, command=self._on_doc_selected,
            dynamic_resizing=False, width=220, **self._menu_style(),
        )
        self.doc_menu.grid(row=0, column=1, sticky="w")
        self.layout_toggle = ctk.CTkSegmentedButton(
            toolbar, values=list(LAYOUT_MODES), variable=self.layout_var, command=self._on_layout_change,
            font=font(11), **self._segmented_style(),
        )
        self.layout_toggle.grid(row=0, column=2, sticky="e")

        modes = ctk.CTkFrame(workspace, fg_color="transparent")
        modes.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 6))
        modes.grid_columnconfigure(2, weight=1)
        self._small_label(modes, "Modifica in").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.source_toggle = ctk.CTkSegmentedButton(
            modes, values=list(SOURCE_MODES), variable=self.source_mode_var, command=self._on_source_mode,
            font=font(11), **self._segmented_style(),
        )
        self.source_toggle.grid(row=0, column=1, sticky="w")
        self.remove_button = self._button(modes, "✂  Rimuovi testo", self.open_remove_dialog)
        self.remove_button.configure(height=28, width=130)
        self.remove_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.preview_toggle = ctk.CTkSegmentedButton(
            modes, values=list(PREVIEW_MODES), variable=self.preview_mode_var,
            command=lambda _v: self.schedule_render(immediate=True), font=font(11), **self._segmented_style(),
        )
        self.preview_toggle.grid(row=0, column=3, sticky="e")
        self.info_label = ctk.CTkLabel(
            workspace, text="", font=font(10), text_color=COLORS.amber, anchor="w", justify="left", wraplength=760,
        )
        self.info_label.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 10))

        self.paned = tk.PanedWindow(
            workspace,
            orient=tk.HORIZONTAL if self.layout_var.get() == LAYOUT_MODES[0] else tk.VERTICAL,
            bg=COLORS.surface, sashwidth=8, sashrelief=tk.FLAT, bd=0, opaqueresize=True,
        )
        self.paned.grid(row=2, column=0, sticky="nsew", padx=12)

        editor_frame = ctk.CTkFrame(self.paned, fg_color=COLORS.surface_alt, corner_radius=10)
        editor_frame.grid_columnconfigure(0, weight=1)
        editor_frame.grid_rowconfigure(1, weight=1)
        self.editor_title = ctk.CTkLabel(
            editor_frame, text="Sorgente (Markdown + LaTeX)", font=font(10, "bold"), text_color=COLORS.text_faint,
            anchor="w",
        )
        self.editor_title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.editor = ctk.CTkTextbox(
            editor_frame, font=mono(12), wrap="word", fg_color=COLORS.surface_alt, text_color=COLORS.text,
            border_width=0, corner_radius=8, undo=True, maxundo=200, autoseparators=True,
        )
        self.editor.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 6))
        self.editor._textbox.bind("<<Modified>>", self._on_editor_modified)
        self.editor._textbox.configure(insertbackground=COLORS.accent, tabs=("1c",))
        for sequence in ("<Control-h>", "<Control-H>"):
            self.editor._textbox.bind(sequence, lambda _e: (self.open_remove_dialog(), "break")[1])

        preview_frame = ctk.CTkFrame(self.paned, fg_color=COLORS.surface_alt, corner_radius=10)
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(1, weight=1)
        self.preview_title = ctk.CTkLabel(
            preview_frame, text="Anteprima", font=font(10, "bold"), text_color=COLORS.text_faint, anchor="w",
        )
        self.preview_title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.preview = tk.Text(
            preview_frame, wrap="word", bg=COLORS.surface_alt, fg=COLORS.text, relief=tk.FLAT,
            highlightthickness=0, bd=0, padx=14, pady=8, cursor="arrow",
            font=font(12), insertbackground=COLORS.surface_alt, spacing1=2, spacing3=2,
            selectbackground=COLORS.accent_dark,
        )
        self.preview.grid(row=1, column=0, sticky="nsew", padx=(4, 0), pady=(2, 6))
        scrollbar = ctk.CTkScrollbar(
            preview_frame, command=self.preview.yview, button_color=COLORS.border,
            button_hover_color=COLORS.surface_hover,
        )
        scrollbar.grid(row=1, column=1, sticky="ns", pady=6)
        self.preview.configure(yscrollcommand=scrollbar.set)
        self._configure_preview_tags()
        self.preview.bind("<Key>", lambda event: None if event.state & 0x4 else "break")

        self.paned.add(editor_frame, minsize=220, stretch="always")
        self.paned.add(preview_frame, minsize=220, stretch="always")

    def _configure_preview_tags(self) -> None:
        text = self.preview
        text.tag_configure("marker", foreground=COLORS.accent, font=font(10, "bold"), spacing1=14, spacing3=4)
        text.tag_configure("h1", font=font(19, "bold"), spacing1=6, spacing3=6)
        text.tag_configure("h2", font=font(15, "bold"), foreground="#CBD5E1", spacing1=6, spacing3=4)
        text.tag_configure("h3", font=font(13, "bold"), spacing1=4, spacing3=3)
        text.tag_configure("para", spacing3=6)
        text.tag_configure("bold", font=font(12, "bold"))
        text.tag_configure("italic", font=font(12, "normal"), foreground="#E2E8F0")
        for level in range(4):
            indent = 14 + level * 24
            text.tag_configure(f"list{level}", lmargin1=indent, lmargin2=indent + 18, spacing3=2)
        text.tag_configure("display", justify="center", spacing1=6, spacing3=8)
        text.tag_configure("mathcode", font=mono(11), foreground=COLORS.amber)
        text.tag_configure("code", font=mono(11), foreground=COLORS.text)
        text.tag_configure("table", font=mono(11), foreground=COLORS.text_muted, lmargin1=14, lmargin2=14)
        text.tag_configure("rule", foreground=COLORS.border, spacing1=4)
        text.tag_configure("placeholder", foreground=COLORS.text_faint, font=font(12), justify="center", spacing1=60)

    # ------------------------------------------------------------------ helper widget
    def _card(self, parent, row: int, title: str, subtitle: str) -> Card:
        card = Card(parent)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(card, text=title, font=font(14, "bold"), text_color=COLORS.text, anchor="w").pack(
            fill="x", padx=16, pady=(14, 2 if subtitle else 10)
        )
        if subtitle:
            ctk.CTkLabel(
                card, text=subtitle, font=font(10), text_color=COLORS.text_faint, anchor="w",
                justify="left", wraplength=270,
            ).pack(fill="x", padx=16, pady=(0, 10))
        return card

    def _button(self, parent, text: str, command, danger: bool = False) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent, text=text, height=32, font=font(11, "bold"), corner_radius=9,
            fg_color=COLORS.red_dark if danger else COLORS.surface_alt,
            hover_color="#991B1B" if danger else COLORS.surface_hover,
            text_color=COLORS.text, border_width=1, border_color=COLORS.border, command=command,
        )

    def _small_label(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, font=font(10, "bold"), text_color=COLORS.text_muted, anchor="w")

    @staticmethod
    def _menu_style() -> dict[str, Any]:
        return {
            "height": 32,
            "font": font(11),
            "dropdown_font": font(11),
            "fg_color": COLORS.surface_alt,
            "button_color": COLORS.surface_hover,
            "button_hover_color": COLORS.border,
            "text_color": COLORS.text,
            "dropdown_fg_color": COLORS.surface_alt,
            "dropdown_hover_color": COLORS.surface_hover,
            "dropdown_text_color": COLORS.text,
            "corner_radius": 8,
        }

    @staticmethod
    def _entry_style(mono_font: bool = False) -> dict[str, Any]:
        return {
            "fg_color": COLORS.surface_alt,
            "border_color": COLORS.border,
            "text_color": COLORS.text,
            "font": mono(11) if mono_font else font(11),
            "corner_radius": 8,
        }

    @staticmethod
    def _segmented_style() -> dict[str, Any]:
        return {
            "fg_color": COLORS.surface_alt,
            "selected_color": COLORS.accent_dark,
            "selected_hover_color": COLORS.accent_dark,
            "unselected_color": COLORS.surface_alt,
            "unselected_hover_color": COLORS.surface_hover,
            "text_color": COLORS.text,
            "height": 28,
        }

    def _option(self, parent, label: str, variable: ctk.StringVar, values: list[str]) -> None:
        self._small_label(parent, label).pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            parent, values=values, variable=variable, command=self._on_engine_change, dynamic_resizing=False,
            **self._menu_style(),
        ).pack(fill="x", padx=16, pady=(4, 8))

    def _switch(self, parent, text: str, variable: ctk.BooleanVar, command) -> None:
        ctk.CTkSwitch(
            parent, text=text, variable=variable, command=command, font=font(11), text_color=COLORS.text_muted,
            progress_color=COLORS.accent, button_color=COLORS.text, button_hover_color="#FFFFFF",
            fg_color=COLORS.border, switch_width=34, switch_height=18,
        ).pack(anchor="w", padx=16, pady=3)

    # ------------------------------------------------------------------ eventi impostazioni
    def _on_engine_change(self, *_args) -> None:
        self._save_settings()
        self._refresh_backends()

    def _on_render_option(self) -> None:
        self._save_settings()
        self.schedule_render(immediate=True)

    def _on_layout_change(self, value: str) -> None:
        self.paned.configure(orient=tk.HORIZONTAL if value == LAYOUT_MODES[0] else tk.VERTICAL)
        self._save_settings()

    def _refresh_backends(self) -> None:
        for child in self.backend_frame.winfo_children():
            child.destroy()
        try:
            status = backend_status(self.extract_options())
        except Exception:
            LOGGER.exception("Verifica dei motori PDF non riuscita.")
            status = []
        pandoc = find_pandoc()
        status.append({"label": "Pandoc (Word)", "available": pandoc is not None,
                       "detail": "OMML nativo" if pandoc else "necessario per .docx"})
        status.append({"label": "KaTeX locale", "available": katex_available(),
                       "detail": "anteprima browser" if katex_available() else "assets/katex mancante"})
        for index, item in enumerate(status):
            row = ctk.CTkFrame(self.backend_frame, fg_color="transparent")
            row.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 10), pady=1)
            ctk.CTkLabel(
                row, text="●", width=12, font=font(10),
                text_color=COLORS.green if item["available"] else COLORS.text_faint,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=item["label"], font=font(10, "bold" if item["available"] else "normal"),
                text_color=COLORS.text_muted if item["available"] else COLORS.text_faint,
            ).pack(side="left", padx=(2, 0))
        self._update_buttons()

    # ------------------------------------------------------------------ sorgenti
    def _initial_dir(self) -> str:
        return self._last_dir if self._last_dir and Path(self._last_dir).is_dir() else str(Path.home())

    def choose_files(self) -> None:
        names = filedialog.askopenfilenames(
            parent=self, title="Scegli uno o piu' PDF", initialdir=self._initial_dir(),
            filetypes=[("PDF", "*.pdf"), ("Tutti i file", "*.*")],
        )
        if not names:
            return
        self.sources = [Path(name) for name in names]
        self._last_dir = str(self.sources[0].parent)
        self._save_settings()
        self._update_source_label()

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Cartella con i PDF", initialdir=self._initial_dir())
        if not folder:
            return
        pdfs = list_pdfs(folder, recursive=bool(self.recursive_var.get()))
        if not pdfs:
            messagebox.showinfo("PDF e Slide", "La cartella non contiene file PDF.", parent=self)
            return
        self.sources = pdfs
        self._last_dir = folder
        self._save_settings()
        self._update_source_label()

    def set_sources(self, paths: list[Path]) -> None:
        """Usato anche dai test e dallo smoke test."""
        self.sources = [Path(path) for path in paths]
        self._update_source_label()

    def _update_source_label(self) -> None:
        if not self.sources:
            self.source_label.configure(text="Nessun file selezionato.")
        elif len(self.sources) == 1:
            self.source_label.configure(text=f"{self.sources[0].name}\n{self.sources[0].parent}")
        else:
            names = ", ".join(path.name for path in self.sources[:3])
            more = f" e altri {len(self.sources) - 3}" if len(self.sources) > 3 else ""
            self.source_label.configure(text=f"{len(self.sources)} PDF: {names}{more}")
        self._update_buttons()

    # ------------------------------------------------------------------ estrazione
    def is_busy(self) -> bool:
        return self.controller.is_running()

    def start_extraction(self) -> None:
        if not self.sources:
            self.choose_files()
            if not self.sources:
                return
        if self.docs and any(doc.source.strip() for doc in self.docs):
            if not messagebox.askyesno(
                "PDF e Slide",
                "La nuova estrazione sostituira' i testi attuali, comprese le modifiche manuali.\nContinuare?",
                parent=self,
            ):
                return
        self._save_settings()
        ok, error = self.controller.start(self.sources, self.extract_options())
        if not ok:
            messagebox.showwarning("PDF e Slide", error, parent=self)
            return
        self.docs = []
        self.current = -1
        self._task_label = "extract"
        self.progress.set(0)
        self._set_status("Avvio estrazione…")
        self._refresh_doc_menu()
        self._update_buttons()
        self._start_polling()

    def cancel(self) -> None:
        if self.controller.is_running():
            self.controller.cancel()
            self._set_status("Annullamento in corso…")

    def _start_polling(self) -> None:
        if self._poll_job is None:
            self._poll_job = self.after(80, self._poll_events)

    def _poll_events(self) -> None:
        self._poll_job = None
        processed = 0
        while processed < 200:
            try:
                event = self.controller.events.get_nowait()
            except Exception:
                break
            processed += 1
            try:
                self._handle_event(event)
            except Exception:
                LOGGER.exception("Gestione evento PDF non riuscita.")
        if self.controller.is_running() or processed:
            self._poll_job = self.after(80, self._poll_events)
        else:
            self._update_buttons()

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "progress":
            self.progress.set(float(event.get("fraction", 0.0)))
            self._set_status(str(event.get("message", "")))
        elif kind == "document":
            document = event["document"]
            source = event["markdown"]
            warnings = list(document.warnings)
            if self.apply_rules_var.get() and self.removal_rules:
                source, removed = apply_rules(source, self.removal_rules)
                if removed:
                    warnings.append(f"Regole di rimozione: {removed} occorrenze tolte")
            state = DocState(
                path=Path(event["path"]),
                title=document.title,
                source=source,
                warnings=warnings,
                engines=dict(document.engines),
            )
            self.docs.append(state)
            self._refresh_doc_menu()
            if self.current < 0:
                self._select_doc(len(self.docs) - 1)
        elif kind == "file_error":
            path = Path(event["path"])
            self.docs.append(DocState(path=path, title=path.stem, source="", error=str(event.get("error", ""))))
            self._refresh_doc_menu()
            if self.current < 0:
                self._select_doc(len(self.docs) - 1)
        elif kind == "done":
            self.progress.set(1.0 if not event.get("cancelled") else self.progress.get())
            converted = int(event.get("converted", 0))
            failed = int(event.get("failed", 0))
            if event.get("cancelled"):
                message = f"Estrazione annullata ({converted} completati)."
            elif event.get("error"):
                message = f"Errore: {event['error']}"
            elif failed:
                message = f"Completato: {converted} PDF estratti, {failed} con errori."
            else:
                pages = sum(len(parse_markdown(doc.source)) for doc in self.docs if doc.source)
                message = f"Completato: {converted} PDF, {pages} slide/pagine."
            self._set_status(message)
            self._task_label = ""
            self._update_buttons()
            if converted and not event.get("cancelled"):
                self.app.toast(message)
        elif kind == "task_done":
            callback = self._task_done
            self._task_done = None
            self._task_label = ""
            if event.get("cancelled"):
                self._set_status("Operazione annullata.")
            elif not event.get("ok"):
                self._set_status(f"Errore: {event.get('error')}")
                messagebox.showerror("PDF e Slide", str(event.get("error")), parent=self)
            elif callback is not None:
                callback(event)
            self._update_buttons()

    def _set_status(self, message: str) -> None:
        self.status_label.configure(text=message[:220])

    def _update_buttons(self) -> None:
        busy = self.controller.is_running()
        has_doc = 0 <= self.current < len(self.docs) and bool(self._current_source().strip())
        has_any = any(doc.source.strip() for doc in self.docs)
        self.extract_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        for button in (self.copy_button, self.katex_button, self.remove_button, *self.export_buttons):
            button.configure(state="normal" if has_doc and not busy else "disabled")
        if not katex_available():
            self.katex_button.configure(state="disabled")
        self.batch_button.configure(state="normal" if has_any and not busy else "disabled")

    # ------------------------------------------------------------------ documenti
    def _doc_label(self, index: int) -> str:
        doc = self.docs[index]
        prefix = "⚠ " if doc.error else ""
        return f"{index + 1}. {prefix}{doc.path.name}"

    def _refresh_doc_menu(self) -> None:
        labels = [self._doc_label(index) for index in range(len(self.docs))] or ["—"]
        self.doc_menu.configure(values=labels)
        if 0 <= self.current < len(self.docs):
            self.doc_var.set(self._doc_label(self.current))
        else:
            self.doc_var.set(labels[0])

    def _on_doc_selected(self, label: str) -> None:
        for index in range(len(self.docs)):
            if self._doc_label(index) == label:
                self._select_doc(index)
                return

    def _select_doc(self, index: int) -> None:
        self._commit_editor()
        if not (0 <= index < len(self.docs)):
            return
        self.current = index
        doc = self.docs[index]
        self.doc_var.set(self._doc_label(index))
        self.source_mode_var.set(SOURCE_MODES[1] if doc.fmt == SOURCE_LATEX else SOURCE_MODES[0])
        self._update_editor_title()
        self._set_editor_text(doc.source)
        notes = []
        if doc.error:
            notes.append(f"Errore: {doc.error}")
        notes.extend(doc.warnings)
        if doc.engines:
            used = ", ".join(f"{ENGINE_LABELS.get(key, key)} ×{count}" for key, count in doc.engines.items())
            notes.append(f"Motori usati: {used}")
        self.info_label.configure(
            text="  ·  ".join(notes),
            text_color=COLORS.red if doc.error else (COLORS.amber if doc.warnings else COLORS.text_faint),
        )
        self.schedule_render(immediate=True)
        self._update_buttons()
        if self._remove_dialog is not None and self._remove_dialog.winfo_exists():
            self._remove_dialog.after(10, self._remove_dialog._refresh_count)

    def _current_doc(self) -> DocState | None:
        return self.docs[self.current] if 0 <= self.current < len(self.docs) else None

    def _current_source(self) -> str:
        doc = self._current_doc()
        return doc.source if doc is not None else ""

    def _update_editor_title(self) -> None:
        doc = self._current_doc()
        latex = doc is not None and doc.fmt == SOURCE_LATEX
        self.editor_title.configure(text="Sorgente LaTeX (corpo del documento)" if latex else "Sorgente (Markdown + LaTeX)")

    # ------------------------------------------------------------------ editor
    def _set_editor_text(self, text: str) -> None:
        self._suspend_modified = True
        widget = self.editor._textbox
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.edit_reset()
        widget.edit_modified(False)
        self._suspend_modified = False

    def _editor_text(self) -> str:
        return self.editor._textbox.get("1.0", "end-1c")

    def _on_editor_modified(self, _event=None) -> None:
        widget = self.editor._textbox
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if self._suspend_modified:
            return
        if self._edit_job is not None:
            self.after_cancel(self._edit_job)
        self._edit_job = self.after(EDIT_DEBOUNCE_MS, self._after_edit)

    def _after_edit(self) -> None:
        self._edit_job = None
        self._commit_editor()
        self.schedule_render()
        self._update_buttons()

    def _commit_editor(self) -> None:
        doc = self._current_doc()
        if doc is not None:
            doc.source = self._editor_text()

    def _on_source_mode(self, value: str) -> None:
        doc = self._current_doc()
        if doc is None:
            self.source_mode_var.set(SOURCE_MODES[0])
            return
        self._commit_editor()
        target = SOURCE_LATEX if value == SOURCE_MODES[1] else SOURCE_MARKDOWN
        if target == doc.fmt:
            return
        if target == SOURCE_LATEX:
            options = self.render_options(doc)
            doc.source = markdown_to_latex(doc.source, options, full=False)
        else:
            if not messagebox.askyesno(
                "PDF e Slide",
                "Il codice LaTeX verra' riconvertito in Markdown: i comandi non riconosciuti "
                "restano come testo. Continuare?",
                parent=self,
            ):
                self.source_mode_var.set(SOURCE_MODES[1])
                return
            doc.source = latex_to_markdown(doc.source)
        doc.fmt = target
        self._update_editor_title()
        self._set_editor_text(doc.source)
        self.schedule_render(immediate=True)

    # ------------------------------------------------------------------ anteprima
    def schedule_render(self, immediate: bool = False) -> None:
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except (tk.TclError, ValueError):
                pass
        self._render_job = self.after(1 if immediate else 60, self.render_preview)

    def _show_placeholder(self) -> None:
        self._clear_preview()
        self.preview.insert(
            "end",
            "Scegli un PDF e premi «Estrai testo».\n\nIl testo estratto comparira' nell'editor: ogni modifica "
            "aggiorna questa anteprima e finisce nei file esportati.",
            "placeholder",
        )

    def _clear_preview(self) -> None:
        self._render_generation += 1
        self.preview.delete("1.0", "end")
        self._preview_images = []

    def render_preview(self) -> None:
        self._render_job = None
        doc = self._current_doc()
        if doc is None:
            self._show_placeholder()
            return
        mode = self.preview_mode_var.get()
        options = self.render_options(doc)
        position = self.preview.yview()[0]
        self._clear_preview()
        if not doc.source.strip():
            self.preview.insert("end", doc.error or "Nessun testo estratto.", "placeholder")
            return
        if mode == "LaTeX":
            self.preview_title.configure(text="LaTeX generato (sola lettura)")
            self.preview.insert("end", render_source(doc.source, doc.fmt, FORMAT_TEX, options), "code")
            self.preview.yview_moveto(position)
            return
        if mode == "TXT":
            self.preview_title.configure(text="Testo .txt (sola lettura)")
            self.preview.insert("end", render_source(doc.source, doc.fmt, FORMAT_TXT, options), "code")
            self.preview.yview_moveto(position)
            return
        self.preview_title.configure(text="Anteprima con formule")
        markdown = latex_to_markdown(doc.source) if doc.fmt == SOURCE_LATEX else doc.source
        slides = parse_markdown(markdown)
        operations = list(self._preview_operations(slides, options))
        generation = self._render_generation
        self._render_chunk(operations, 0, generation, position)

    def _preview_operations(self, slides, options: RenderOptions):
        for slide_index, slide in enumerate(slides):
            if options.include_markers and slide.number:
                yield ("marker", f"SLIDE {slide.number}")
            elif slide_index:
                yield ("rule", "")
            for block in slide.blocks:
                yield ("block", block)

    def _render_chunk(self, operations: list, start: int, generation: int, position: float) -> None:
        if generation != self._render_generation:
            return
        end = min(len(operations), start + PREVIEW_RENDER_CHUNK)
        options = self.render_options(self._current_doc())
        for kind, payload in operations[start:end]:
            if kind == "marker":
                self.preview.insert("end", payload + "\n", "marker")
            elif kind == "rule":
                self.preview.insert("end", "─" * 40 + "\n", "rule")
            else:
                self._insert_block(payload, options)
        if start == 0:
            self.preview.yview_moveto(position)
        if end < len(operations):
            self.after(1, lambda: self._render_chunk(operations, end, generation, position))
        else:
            self.preview.yview_moveto(position)

    def _renderer_instance(self) -> MathRenderer:
        if self._renderer is None:
            self._renderer = MathRenderer(self.preview, COLORS.text)
        return self._renderer

    def _insert_block(self, block, options: RenderOptions) -> None:
        text = self.preview
        if block.kind == BLOCK_HEADING:
            if not options.include_headings:
                return
            tag = f"h{max(1, min(3, block.level))}"
            self._insert_inline(block.text, (tag,), size=17 if tag == "h1" else 14)
            text.insert("end", "\n", tag)
        elif block.kind in (BLOCK_BULLET, BLOCK_NUMBER):
            level = max(0, min(3, block.level))
            tag = f"list{level}"
            glyph = ("•", "◦", "▪", "‣")[level] if block.kind == BLOCK_BULLET else f"{block.number or 1}."
            text.insert("end", f"{glyph}  ", (tag,))
            self._insert_inline(block.text, (tag,))
            text.insert("end", "\n", tag)
        elif block.kind == BLOCK_MATH:
            image = self._renderer_instance().image(block.text, display=True)
            if image is not None:
                self._preview_images.append(image)
                start = text.index("end-1c")
                text.image_create("end", image=image, align="center", padx=2)
                text.insert("end", "\n")
                text.tag_add("display", start, "end-1c")
            else:
                text.insert("end", f"$$ {block.text} $$\n", ("display", "mathcode"))
        elif block.kind == BLOCK_TABLE:
            for row in block.text.splitlines():
                text.insert("end", row + "\n", "table")
        else:
            self._insert_inline(block.text, ("para",))
            text.insert("end", "\n", "para")

    def _insert_inline(self, value: str, tags: tuple[str, ...], size: int = 12) -> None:
        text = self.preview
        renderer = self._renderer_instance()
        for is_math, content in split_inline_math(value):
            if is_math:
                image = renderer.image(content, display=False, size=size)
                if image is not None:
                    self._preview_images.append(image)
                    text.image_create("end", image=image, align="center", padx=1)
                else:
                    text.insert("end", f"${content}$", tags + ("mathcode",))
                continue
            plain = unescape_text(content)
            for token in _split_emphasis(plain):
                if token.startswith("**") and token.endswith("**") and len(token) > 4:
                    text.insert("end", token[2:-2], tags + ("bold",))
                elif token.startswith("*") and token.endswith("*") and len(token) > 2:
                    text.insert("end", token[1:-1], tags + ("italic",))
                else:
                    text.insert("end", strip_inline_markup(token), tags)

    # ------------------------------------------------------------------ rimozione testo
    def open_remove_dialog(self) -> None:
        if self._current_doc() is None or self.controller.is_running():
            return
        try:
            selected = self.editor._textbox.get("sel.first", "sel.last")
        except tk.TclError:
            selected = ""
        if self._remove_dialog is not None and self._remove_dialog.winfo_exists():
            if selected:
                self._remove_dialog.set_pattern(selected)
            self._remove_dialog.lift()
            self._remove_dialog.focus_force()
            return
        self._remove_dialog = RemoveTextDialog(self, initial=selected)

    def remove_text(self, rule: RemovalRule, indices: list[int]) -> tuple[list[tuple[int, str]], int, int]:
        """Rimuove il testo dai documenti indicati.

        Restituisce (istantanea per l'annullamento, occorrenze rimosse, documenti modificati).
        Sul documento aperto la modifica passa dall'editor, quindi anche Ctrl+Z la annulla.
        """
        self._commit_editor()
        snapshot: list[tuple[int, str]] = []
        removed = 0
        touched = 0
        for index in indices:
            doc = self.docs[index]
            new_source, count = remove_text(doc.source, rule)
            if not count:
                continue
            snapshot.append((index, doc.source))
            removed += count
            touched += 1
            if index == self.current:
                self._replace_editor_text(new_source)
            doc.source = new_source
        if removed:
            self.schedule_render(immediate=True)
            self._update_buttons()
            self._set_status(f"Rimosse {removed} occorrenze di testo.")
        return snapshot, removed, touched

    def restore_sources(self, snapshot: list[tuple[int, str]]) -> None:
        for index, source in snapshot:
            if not (0 <= index < len(self.docs)):
                continue
            if index == self.current:
                self._replace_editor_text(source)
            self.docs[index].source = source
        self.schedule_render(immediate=True)
        self._update_buttons()
        self._set_status("Rimozione annullata.")

    def _replace_editor_text(self, text: str) -> None:
        """Sostituisce il contenuto dell'editor come un'unica modifica annullabile."""
        widget = self.editor._textbox
        position = widget.yview()[0]
        self._suspend_modified = True
        widget.configure(autoseparators=False)
        try:
            widget.edit_separator()
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.edit_separator()
            widget.edit_modified(False)
        finally:
            widget.configure(autoseparators=True)
            self._suspend_modified = False
        widget.yview_moveto(position)

    def add_removal_rule(self, rule: RemovalRule) -> bool:
        if any(existing.to_dict() == rule.to_dict() for existing in self.removal_rules):
            return False
        self.removal_rules.append(rule)
        self._update_rules_label()
        self._save_settings()
        return True

    def delete_removal_rule(self, index: int) -> None:
        if 0 <= index < len(self.removal_rules):
            del self.removal_rules[index]
            self._update_rules_label()
            self._save_settings()

    def _update_rules_label(self) -> None:
        count = len(self.removal_rules)
        self.rules_switch.configure(
            text=f"Applica le regole di rimozione salvate ({count})" if count
            else "Applica le regole di rimozione salvate (nessuna)"
        )

    # ------------------------------------------------------------------ azioni rapide
    def _rendered_latex(self) -> str:
        doc = self._current_doc()
        if doc is None:
            return ""
        return render_source(doc.source, doc.fmt, FORMAT_TEX, self.render_options(doc))

    def copy_latex(self) -> None:
        self._commit_editor()
        latex = self._rendered_latex()
        if not latex:
            return
        self.clipboard_clear()
        self.clipboard_append(latex)
        self.app.toast("LaTeX copiato negli appunti.")

    def open_katex_preview(self) -> None:
        self._commit_editor()
        doc = self._current_doc()
        if doc is None:
            return
        try:
            path = write_html_preview(doc.source, doc.fmt, self.render_options(doc))
        except (ExportError, OSError) as exc:
            messagebox.showerror("PDF e Slide", str(exc), parent=self)
            return
        webbrowser.open(path.as_uri())
        self.app.toast("Anteprima KaTeX aperta nel browser (offline).")

    # ------------------------------------------------------------------ esportazione
    def _export_initial_dir(self, doc: DocState) -> str:
        if self._export_dir and Path(self._export_dir).is_dir():
            return self._export_dir
        return str(doc.path.parent)

    def export_current(self, fmt: str) -> None:
        self._commit_editor()
        doc = self._current_doc()
        if doc is None or not doc.source.strip():
            return
        labels = {FORMAT_TXT: "Testo", FORMAT_TEX: "LaTeX", FORMAT_DOCX: "Microsoft Word"}
        target = filedialog.asksaveasfilename(
            parent=self, title=f"Esporta .{fmt}", defaultextension=f".{fmt}",
            initialdir=self._export_initial_dir(doc), initialfile=f"{doc.path.stem}.{fmt}",
            filetypes=[(labels[fmt], f"*.{fmt}")],
        )
        if not target:
            return
        self._export_dir = str(Path(target).parent)
        self._save_settings()
        source, source_format, options = doc.source, doc.fmt, self.render_options(doc)
        if fmt != FORMAT_DOCX:
            try:
                export_file(source, source_format, fmt, target, options)
            except (ExportError, OSError) as exc:
                messagebox.showerror("PDF e Slide", f"Esportazione non riuscita:\n{exc}", parent=self)
                return
            self._set_status(f"Esportato: {Path(target).name}")
            self.app.toast(f"Esportato {Path(target).name}")
            return

        def task(progress, cancel):
            progress(0.3, "Conversione Word con Pandoc…")
            return export_file(source, source_format, fmt, target, options)

        self._run_task(task, f"Esportato: {Path(target).name}")

    def export_all(self) -> None:
        self._commit_editor()
        ready = [doc for doc in self.docs if doc.source.strip()]
        if not ready:
            return
        folder = filedialog.askdirectory(
            parent=self, title="Cartella di destinazione", initialdir=self._export_initial_dir(ready[0]),
        )
        if not folder:
            return
        self._export_dir = folder
        self._save_settings()
        formats = BATCH_FORMAT_CHOICES.get(self.batch_format_var.get(), [FORMAT_DOCX])
        jobs = [(doc, fmt, Path(folder) / f"{doc.path.stem}.{fmt}") for doc in ready for fmt in formats]
        existing = [target for _, _, target in jobs if target.exists()]
        if existing and not messagebox.askyesno(
            "PDF e Slide",
            f"{len(existing)} file esistono gia' nella cartella e verranno sovrascritti.\nContinuare?",
            parent=self,
        ):
            return
        snapshots = [(doc.source, doc.fmt, self.render_options(doc), fmt, target) for doc, fmt, target in jobs]

        def task(progress, cancel):
            written = 0
            errors: list[str] = []
            for index, (source, source_format, options, fmt, target) in enumerate(snapshots):
                if cancel.is_set():
                    raise PdfCancelled("Esportazione annullata.")
                progress(index / len(snapshots), f"Esporto {target.name} ({index + 1}/{len(snapshots)})")
                try:
                    export_file(source, source_format, fmt, target, options)
                    written += 1
                except (ExportError, OSError) as exc:
                    errors.append(f"{target.name}: {exc}")
            if errors and not written:
                raise ExportError("\n".join(errors[:5]))
            return {"written": written, "errors": errors, "folder": folder}

        def done(event: dict[str, Any]) -> None:
            result = event.get("result") or {}
            message = f"Esportati {result.get('written', 0)} file in {Path(folder).name}."
            if result.get("errors"):
                message += f" {len(result['errors'])} errori."
                messagebox.showwarning("PDF e Slide", "\n".join(result["errors"][:8]), parent=self)
            self._set_status(message)
            self.progress.set(1.0)
            self.app.toast(message)

        self._run_task(task, "", done)

    def _run_task(self, task, success_message: str, done: Callable[[dict[str, Any]], None] | None = None) -> None:
        def default_done(_event: dict[str, Any]) -> None:
            self._set_status(success_message)
            self.progress.set(1.0)
            self.app.toast(success_message)

        ok, error = self.controller.run_task(task)
        if not ok:
            messagebox.showwarning("PDF e Slide", error, parent=self)
            return
        self._task_done = done or default_done
        self._task_label = "task"
        self.progress.set(0)
        self._set_status("Esportazione in corso…")
        self._update_buttons()
        self._start_polling()

    # ------------------------------------------------------------------ ciclo di vita
    def on_show(self) -> None:
        self._update_buttons()
        if self.controller.is_running():
            self._start_polling()

    def on_hide(self) -> None:
        if self._remove_dialog is not None and self._remove_dialog.winfo_exists():
            self._remove_dialog.close()
        self._commit_editor()
        self._save_settings()

    def shutdown(self) -> None:
        try:
            self.controller.shutdown()
        except Exception:
            LOGGER.exception("Chiusura conversione PDF non riuscita.")


def _split_emphasis(text: str) -> list[str]:
    import re

    return [token for token in re.split(r"(\*\*[^*]+?\*\*|(?<![\w*])\*(?!\s)[^*]+?(?<!\s)\*(?![\w*]))", text) if token]
