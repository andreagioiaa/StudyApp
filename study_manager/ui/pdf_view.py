"""Vista "PDF e Slide": trascrizione, editor con anteprima live ed esportazione.

Layout: a sinistra sorgente, motori, pulizia ed esportazione; a destra un
editor (Markdown + LaTeX oppure LaTeX puro) affiancato all'anteprima, che
mostra a scelta il rendering con le formule, il codice LaTeX finale o il TXT.

Tutta l'elaborazione pesante gira in un thread separato
(``PdfConversionController``); la vista legge gli eventi con ``after``.
"""

from __future__ import annotations

import logging
import re
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from ..config import COLORS, FONT_FAMILY, MONO_FONT_FAMILY
from ..services.subject_folders import KIND_NOTES
from ..services.pdf_tools.controller import PdfConversionController, list_pdfs
from ..services.pdf_tools.inline import (
    STYLE_BOLD,
    STYLE_COLOR,
    STYLE_ITALIC,
    STYLE_MONO,
    STYLE_UNDERLINE,
    COLOR_CHOICES,
    iter_runs,
    parse_inline,
    preview_color,
)
from ..services.pdf_tools.draft import default_draft_path, load_draft, save_draft
from ..services.pdf_tools.draft import delete_draft as delete_draft_file
from ..services.pdf_tools.document import (
    BLOCK_BULLET,
    BLOCK_HEADING,
    BLOCK_MATH,
    BLOCK_NUMBER,
    BLOCK_TABLE,
    RenderOptions,
    parse_markdown,
    unescape_text,
)
from ..services.pdf_tools.export import (
    EXPORT_FORMATS,
    FORMAT_DOCX,
    FORMAT_TEX,
    FORMAT_TXT,
    SOURCE_LATEX,
    SOURCE_MARKDOWN,
    SECTION_LEVELS,
    ExportError,
    SectionNumbering,
    apply_template,
    export_file,
    find_pandoc,
    heading_level_name,
    katex_available,
    latex_to_markdown,
    markdown_to_latex,
    numbering_for,
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
from .latex_structure_dialog import LEVEL_LABELS, LatexStructureDialog
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
    "pdf_latex_numbered": "1",
    "pdf_latex_embed": "0",
    "pdf_latex_level": "section",
    "pdf_latex_template": "",
    "pdf_panel_collapsed": "0",
    "pdf_subject": "",
}

NO_SUBJECT = "Nessuna materia"

PREVIEW_RENDER_CHUNK = 18
HEADING_FONT_SIZES = {"h1": 19, "h2": 15, "h3": 13}
EDIT_DEBOUNCE_MS = 380
DRAFT_DEBOUNCE_MS = 1500


@dataclass
class DocState:
    path: Path
    title: str
    source: str
    fmt: str = SOURCE_MARKDOWN
    warnings: list[str] = field(default_factory=list)
    engines: dict[str, int] = field(default_factory=dict)
    error: str = ""
    # Struttura LaTeX scelta all'importazione: numbered, embed, base_level, root_title
    latex: dict[str, Any] = field(default_factory=dict)


def _pretty_name(path: Path) -> str:
    """Nome leggibile proposto per la sezione radice (es. ``03_reti-ip`` -> ``03 reti ip``)."""
    name = path.stem.replace("_", " ").replace("-", " ")
    return " ".join(name.split()) or path.stem


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
        self._preview_numbering: SectionNumbering | None = None
        self._font_tags: set[str] = set()
        self._draft_job: str | None = None
        self._draft_saved_at = 0.0
        try:
            self._draft_path = default_draft_path()
        except Exception:
            LOGGER.exception("Cartella della bozza PDF non disponibile.")
            self._draft_path = None

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
        self._apply_panel_state()
        self._refresh_backends()
        self._update_buttons()
        self._show_placeholder()
        self._restore_draft()

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
        self._latex_defaults = {
            "numbered": self._bool(settings["pdf_latex_numbered"]),
            "embed": self._bool(settings["pdf_latex_embed"]),
            "base_level": settings["pdf_latex_level"] if settings["pdf_latex_level"] in LEVEL_LABELS else "section",
        }
        self._pending_latex: dict[str, Any] | None = None
        self._template_path = settings["pdf_latex_template"]
        self._template_text = ""
        self._template_error = ""
        self._load_template_text()
        self._panel_collapsed = self._bool(settings["pdf_panel_collapsed"])
        self.subject_var = ctk.StringVar(value=settings["pdf_subject"] or NO_SUBJECT)

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
            "pdf_latex_numbered": "1" if self._latex_defaults["numbered"] else "0",
            "pdf_latex_embed": "1" if self._latex_defaults["embed"] else "0",
            "pdf_latex_level": self._latex_defaults["base_level"],
            "pdf_latex_template": self._template_path,
            "pdf_panel_collapsed": "1" if self._panel_collapsed else "0",
            "pdf_subject": self.selected_subject(),
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
        latex = dict(self._latex_defaults)
        if doc is not None and doc.latex:
            latex.update(doc.latex)
        root_title = str(latex.get("root_title") or "")
        if not root_title and doc is not None:
            root_title = _pretty_name(doc.path)
        return RenderOptions(
            include_markers=bool(self.markers_var.get()),
            include_headings=bool(self.headings_var.get()),
            include_preamble=bool(self.preamble_var.get()),
            docx_page_breaks=bool(self.pagebreak_var.get()),
            title=(doc.title if doc is not None else ""),
            numbered_sections=bool(latex.get("numbered", True)),
            embed=bool(latex.get("embed", False)),
            base_level=str(latex.get("base_level") or "section"),
            root_title=root_title,
            template=self._template_text,
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

        self._small_label(export, "Materia").pack(anchor="w", padx=16)
        self.subject_combo = ctk.CTkComboBox(
            export, values=[NO_SUBJECT], variable=self.subject_var, command=self._on_subject_change,
            border_color=COLORS.border, **self._menu_style(),
        )
        self.subject_combo.pack(fill="x", padx=16, pady=(4, 2))
        self.subject_combo.bind("<FocusOut>", lambda _event: self._on_subject_change())
        self.subject_hint = ctk.CTkLabel(
            export, text="", font=font(10), text_color=COLORS.text_faint, anchor="w", justify="left", wraplength=270,
        )
        self.subject_hint.pack(fill="x", padx=16, pady=(0, 8))
        self._refresh_subjects()

        for text, variable in (
            ("Marcatori e numeri di slide", self.markers_var),
            ("Titoli delle slide (intestazioni)", self.headings_var),
            ("Preambolo LaTeX (documento compilabile)", self.preamble_var),
            ("Word: nuova pagina per ogni slide", self.pagebreak_var),
        ):
            self._switch(export, text, variable, self._on_render_option)

        self._small_label(export, "Struttura LaTeX").pack(anchor="w", padx=16, pady=(10, 2))
        self.structure_label = ctk.CTkLabel(
            export, text="", font=font(10), text_color=COLORS.text_faint, anchor="w", justify="left", wraplength=270,
        )
        self.structure_label.pack(fill="x", padx=16, pady=(0, 4))
        self.structure_button = self._button(export, "Struttura LaTeX…", self.edit_structure)
        self.structure_button.pack(fill="x", padx=16, pady=(0, 8))
        self._small_label(export, "Template del documento").pack(anchor="w", padx=16, pady=(2, 2))
        self.template_status = ctk.CTkLabel(
            export, text="", font=font(10), text_color=COLORS.text_faint, anchor="w", justify="left", wraplength=270,
        )
        self.template_status.pack(fill="x", padx=16, pady=(0, 4))
        template_row = ctk.CTkFrame(export, fg_color="transparent")
        template_row.pack(fill="x", padx=16)
        template_row.grid_columnconfigure((0, 1), weight=1)
        self._button(template_row, "Carica template…", self.choose_template).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        self.template_remove_button = self._button(template_row, "Rimuovi", self.clear_template)
        self.template_remove_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._update_template_label()

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
        batch.pack(fill="x", padx=16, pady=(0, 6))
        batch.grid_columnconfigure(1, weight=1)
        ctk.CTkOptionMenu(
            batch, values=list(BATCH_FORMAT_CHOICES), variable=self.batch_format_var, width=86,
            command=lambda _v: self._save_settings(), **self._menu_style(),
        ).grid(row=0, column=0, sticky="w")
        self.batch_button = self._button(batch, "Esporta tutti…", self.export_all)
        self.batch_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        separator(export).pack(fill="x", padx=16, pady=(10, 8))
        self._small_label(export, "Salvataggio temporaneo").pack(anchor="w", padx=16, pady=(0, 2))
        self.draft_label = ctk.CTkLabel(
            export, text="Nessuna bozza salvata.", font=font(10), text_color=COLORS.text_faint,
            anchor="w", justify="left", wraplength=270,
        )
        self.draft_label.pack(fill="x", padx=16, pady=(0, 6))
        draft = ctk.CTkFrame(export, fg_color="transparent")
        draft.pack(fill="x", padx=16, pady=(0, 16))
        draft.grid_columnconfigure((0, 1), weight=1)
        self.draft_save_button = self._button(draft, "Salva ora", lambda: self._save_draft(notify=True))
        self.draft_save_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.draft_delete_button = self._button(draft, "Elimina", self.delete_draft, danger=True)
        self.draft_delete_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    # ------------------------------------------------------------------ layout: area di lavoro
    def _build_workspace(self) -> None:
        workspace = Card(self)
        workspace.grid(row=1, column=1, sticky="nsew")
        self.workspace = workspace
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(workspace, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        toolbar.grid_columnconfigure(3, weight=1)
        self.panel_button = self._button(toolbar, "◀  Opzioni", self.toggle_panel)
        self.panel_button.configure(height=28, width=104)
        self.panel_button.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self._small_label(toolbar, "Documento").grid(row=0, column=1, sticky="w", padx=(0, 6))
        self.doc_menu = ctk.CTkOptionMenu(
            toolbar, values=["—"], variable=self.doc_var, command=self._on_doc_selected,
            dynamic_resizing=False, width=220, **self._menu_style(),
        )
        self.doc_menu.grid(row=0, column=2, sticky="w")
        # Stato compatto, visibile quando il pannello delle opzioni e' chiuso.
        self.mini_status = ctk.CTkLabel(
            toolbar, text="", font=font(10), text_color=COLORS.text_faint, anchor="w",
        )
        self.mini_status.grid(row=0, column=3, sticky="ew", padx=(12, 8))
        self.layout_toggle = ctk.CTkSegmentedButton(
            toolbar, values=list(LAYOUT_MODES), variable=self.layout_var, command=self._on_layout_change,
            font=font(11), **self._segmented_style(),
        )
        self.layout_toggle.grid(row=0, column=4, sticky="e")

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
        for key, style in (("b", STYLE_BOLD), ("i", STYLE_ITALIC), ("u", STYLE_UNDERLINE), ("e", STYLE_MONO)):
            for sequence in (f"<Control-{key}>", f"<Control-{key.upper()}>"):
                self.editor._textbox.bind(sequence, lambda _e, value=style: (self.format_selection(value), "break")[1])
        for sequence in ("<Control-space>",):
            self.editor._textbox.bind(sequence, lambda _e: (self.clear_formatting(), "break")[1])
        self.editor._textbox.bind("<Button-3>", self._show_editor_menu)

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
        self.preview.bind("<Button-3>", self._show_preview_menu)
        # I menu del tasto destro servono sia all'editor sia all'anteprima: vanno
        # costruiti dopo che entrambi i widget esistono.
        self._build_editor_menu()

        self.paned.add(editor_frame, minsize=220, stretch="always")
        self.paned.add(preview_frame, minsize=220, stretch="always")

    def _configure_preview_tags(self) -> None:
        text = self.preview
        text.tag_configure("marker", foreground=COLORS.accent, font=font(10, "bold"), spacing1=14, spacing3=4)
        text.tag_configure("h1", font=font(19, "bold"), spacing1=6, spacing3=6)
        text.tag_configure("h2", font=font(15, "bold"), foreground="#CBD5E1", spacing1=6, spacing3=4)
        text.tag_configure("h3", font=font(13, "bold"), spacing1=4, spacing3=3)
        text.tag_configure("para", spacing3=6)
        text.tag_configure("secnum", foreground=COLORS.accent)
        text.tag_configure("underline", underline=True)
        text.tag_configure("inline_code", background=COLORS.surface, foreground="#F9A8D4")
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
        structure = self._ask_structure_for_import()
        if structure is None:
            return
        self._pending_latex = structure
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
                latex=dict(self._pending_latex or {}),
            )
            self.docs.append(state)
            self._refresh_doc_menu()
            if self.current < 0:
                self._select_doc(len(self.docs) - 1)
            self._schedule_draft_save()
        elif kind == "file_error":
            path = Path(event["path"])
            self.docs.append(DocState(
                path=path, title=path.stem, source="", error=str(event.get("error", "")),
                latex=dict(self._pending_latex or {}),
            ))
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
            self._save_draft()
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
        if hasattr(self, "mini_status"):
            self.mini_status.configure(text=message[:110] if self._panel_collapsed else "")

    # ------------------------------------------------------------------ pannello opzioni
    def toggle_panel(self) -> None:
        self._panel_collapsed = not self._panel_collapsed
        self._apply_panel_state()
        self._save_settings()

    def _apply_panel_state(self) -> None:
        """Mostra o nasconde il pannello delle opzioni; chiuso, editor e anteprima usano tutta la larghezza."""
        if self._panel_collapsed:
            self.controls.grid_remove()
            self.workspace.grid_configure(column=0, columnspan=2)
            self.panel_button.configure(text="▶  Opzioni")
            self.mini_status.configure(text=self.status_label.cget("text")[:110])
        else:
            self.workspace.grid_configure(column=1, columnspan=1)
            self.controls.grid()
            self.panel_button.configure(text="◀  Opzioni")
            self.mini_status.configure(text="")

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
        if hasattr(self, "structure_button"):
            self.structure_button.configure(state="normal" if self._current_doc() is not None and not busy else "disabled")
            self.template_remove_button.configure(state="normal" if self._template_path else "disabled")
            self._update_structure_label()
        if hasattr(self, "draft_delete_button"):
            has_draft = bool(self.docs) or self._draft_exists()
            self.draft_save_button.configure(state="normal" if has_any and not busy else "disabled")
            self.draft_delete_button.configure(state="normal" if has_draft and not busy else "disabled")

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
        self._schedule_draft_save()
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
        self._schedule_draft_save()

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
            doc.source = markdown_to_latex(doc.source, options, full=False, root=False)
        else:
            if not messagebox.askyesno(
                "PDF e Slide",
                "Il codice LaTeX verra' riconvertito in Markdown: i comandi non riconosciuti "
                "restano come testo. Continuare?",
                parent=self,
            ):
                self.source_mode_var.set(SOURCE_MODES[1])
                return
            doc.source = latex_to_markdown(doc.source, self.render_options(doc))
        doc.fmt = target
        self._update_editor_title()
        self._set_editor_text(doc.source)
        self.schedule_render(immediate=True)
        self._schedule_draft_save()

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
        numbering = numbering_for(options)
        start = self._numbering_start_note(options, numbering)
        self.preview_title.configure(
            text="Anteprima con formule e numerazione LaTeX" + (f" ({start})" if start else "")
            if options.numbered_sections else "Anteprima con formule (sezioni non numerate)"
        )
        self._preview_numbering = numbering
        markdown = latex_to_markdown(doc.source, options) if doc.fmt == SOURCE_LATEX else doc.source
        slides = parse_markdown(markdown)
        operations = list(self._preview_operations(slides, options))
        generation = self._render_generation
        self._render_chunk(operations, 0, generation, position)

    def _numbering_start_note(self, options: RenderOptions, numbering: SectionNumbering) -> str:
        """Nota sul punto di partenza quando il template contiene gia' delle sezioni."""
        if not any(numbering.counts):
            return ""
        probe = SectionNumbering(options.language)
        probe.counts = list(numbering.counts)
        probe.has_chapter = numbering.has_chapter
        first = probe.step(heading_level_name(1, options))
        return f"si parte da {first}: il template contiene gia' delle sezioni" if first else ""

    def _preview_operations(self, slides, options: RenderOptions):
        if options.embed and options.include_headings and (options.root_title or options.title).strip():
            yield ("root", (options.root_title or options.title).strip())
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
            elif kind == "root":
                self._insert_heading(payload, options.base_level, options, plain=True)
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
            self._insert_heading(block.text, heading_level_name(min(block.level, 3), options), options)
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

    def _insert_heading(self, value: str, level: str, options: RenderOptions, plain: bool = False) -> None:
        """Titolo con il numero che gli darebbe LaTeX (1, 1.1, Capitolo 2, Parte I…)."""
        top = SECTION_LEVELS.index(options.base_level if options.embed else "section")
        depth = max(0, SECTION_LEVELS.index(level) - top) if level in SECTION_LEVELS else 0
        tag = f"h{min(3, depth + 1)}"
        numbering = self._preview_numbering or SectionNumbering(options.language)
        label = numbering.step(level, starred=not options.numbered_sections)
        if label:
            separator = "\n" if level in ("part", "chapter") else "   "
            self.preview.insert("end", label + separator, (tag, "secnum"))
        if plain:
            self.preview.insert("end", value, (tag,))
        else:
            self._insert_inline(value, (tag,), size=17 if tag == "h1" else 14)
        self.preview.insert("end", "\n", tag)

    def _insert_inline(self, value: str, tags: tuple[str, ...], size: int = 12) -> None:
        """Testo in linea con formule, grassetto, corsivo, sottolineato, monospazio e colori."""
        text = self.preview
        renderer = self._renderer_instance()
        heading = any(tag in ("h1", "h2", "h3") for tag in tags)
        for kind, content, styles in iter_runs(parse_inline(value)):
            names = {style for style, _value in styles}
            colors = [value for style, value in styles if style == STYLE_COLOR]
            if kind == "math":
                image = renderer.image(content, display=False, size=size)
                if image is not None:
                    self._preview_images.append(image)
                    text.image_create("end", image=image, align="center", padx=1)
                else:
                    text.insert("end", f"${content}$", tags + ("mathcode",))
                continue
            run_tags = list(tags)
            font_size = next((HEADING_FONT_SIZES[tag] for tag in tags if tag in HEADING_FONT_SIZES), 12)
            font_tag = self._style_font_tag(
                font_size, bold=STYLE_BOLD in names or heading, italic=STYLE_ITALIC in names,
                mono_font=STYLE_MONO in names, force=bool(names & {STYLE_BOLD, STYLE_ITALIC, STYLE_MONO}),
            )
            if font_tag:
                run_tags.append(font_tag)
            if STYLE_MONO in names:
                run_tags.append("inline_code")
            if STYLE_UNDERLINE in names:
                run_tags.append("underline")
            if colors:
                color_tag = self._color_tag(colors[-1])  # il colore piu' interno vince, come in LaTeX
                if color_tag:
                    run_tags.append(color_tag)
            text.insert("end", unescape_text(content), tuple(run_tags))

    def _style_font_tag(self, size: int, bold: bool, italic: bool, mono_font: bool, force: bool) -> str:
        if not force:
            return ""
        name = f"font_{size}_{int(bold)}{int(italic)}{int(mono_font)}"
        if name not in self._font_tags:
            family = MONO_FONT_FAMILY if mono_font else FONT_FAMILY
            self.preview.tag_configure(
                name,
                font=ctk.CTkFont(
                    family=family, size=size - 1 if mono_font else size,
                    weight="bold" if bold else "normal", slant="italic" if italic else "roman",
                ),
            )
            self._font_tags.add(name)
            self._raise_overlay_tags()
        return name

    def _color_tag(self, value: str) -> str:
        color = preview_color(value)
        if color is None:
            return ""
        name = f"color_{color.lstrip('#')}"
        if name not in self._font_tags:
            self.preview.tag_configure(name, foreground=color)
            self._font_tags.add(name)
            self._raise_overlay_tags()
        return name

    def _raise_overlay_tags(self) -> None:
        """Colori e sottolineatura sopra ai tag dei titoli e del testo."""
        for name in sorted(self._font_tags):
            if name.startswith("color_"):
                self.preview.tag_raise(name)
        self.preview.tag_raise("underline")

    # ------------------------------------------------------------------ formattazione rapida (tasto destro)
    def _build_editor_menu(self) -> None:
        style = dict(
            tearoff=0, bg=COLORS.surface_alt, fg=COLORS.text, activebackground=COLORS.accent_dark,
            activeforeground=COLORS.text, disabledforeground=COLORS.text_faint, bd=0, relief=tk.FLAT,
        )
        menu = tk.Menu(self.editor._textbox, **style)
        menu.add_command(label="Grassetto", accelerator="Ctrl+B", command=lambda: self.format_selection(STYLE_BOLD))
        menu.add_command(label="Corsivo", accelerator="Ctrl+I", command=lambda: self.format_selection(STYLE_ITALIC))
        menu.add_command(label="Sottolineato", accelerator="Ctrl+U",
                         command=lambda: self.format_selection(STYLE_UNDERLINE))
        menu.add_command(label="Monospazio (\\texttt)", accelerator="Ctrl+E",
                         command=lambda: self.format_selection(STYLE_MONO))
        colors = tk.Menu(menu, **style)
        for name, (label, hex_color) in COLOR_CHOICES.items():
            colors.add_command(
                label=f"●  {label}", foreground=hex_color, activeforeground=hex_color,
                command=lambda value=name: self.format_selection(STYLE_COLOR, value),
            )
        menu.add_cascade(label="Colore", menu=colors)
        menu.add_command(label="Rimuovi formattazione", accelerator="Ctrl+Spazio", command=self.clear_formatting)
        menu.add_separator()
        menu.add_command(label="Taglia", accelerator="Ctrl+X",
                         command=lambda: self.editor._textbox.event_generate("<<Cut>>"))
        menu.add_command(label="Copia", accelerator="Ctrl+C",
                         command=lambda: self.editor._textbox.event_generate("<<Copy>>"))
        menu.add_command(label="Incolla", accelerator="Ctrl+V",
                         command=lambda: self.editor._textbox.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Rimuovi questo testo ovunque…", accelerator="Ctrl+H", command=self.open_remove_dialog)
        self.editor_menu = menu
        self._format_entries = (0, 1, 2, 3, 4, 5)

        # Stesso menu sull'anteprima: la selezione viene ritrovata nel sorgente.
        preview_menu = tk.Menu(self.preview, **style)
        on_preview = self._apply_on_preview_selection
        preview_menu.add_command(label="Grassetto", command=lambda: on_preview(STYLE_BOLD))
        preview_menu.add_command(label="Corsivo", command=lambda: on_preview(STYLE_ITALIC))
        preview_menu.add_command(label="Sottolineato", command=lambda: on_preview(STYLE_UNDERLINE))
        preview_menu.add_command(label="Monospazio (\\texttt)", command=lambda: on_preview(STYLE_MONO))
        preview_colors = tk.Menu(preview_menu, **style)
        for name, (label, hex_color) in COLOR_CHOICES.items():
            preview_colors.add_command(
                label=f"●  {label}", foreground=hex_color, activeforeground=hex_color,
                command=lambda value=name: on_preview(STYLE_COLOR, value),
            )
        preview_menu.add_cascade(label="Colore", menu=preview_colors)
        preview_menu.add_command(label="Rimuovi formattazione", command=lambda: on_preview(""))
        preview_menu.add_separator()
        preview_menu.add_command(label="Copia", accelerator="Ctrl+C",
                                 command=lambda: self.preview.event_generate("<<Copy>>"))
        preview_menu.add_command(label="Mostra nell'editor", command=self._reveal_preview_selection)
        self.preview_menu = preview_menu

    def _show_editor_menu(self, event) -> str:
        LOGGER.info("Menu tasto destro aperto (editor).")
        widget = self.editor._textbox
        has_doc = self._current_doc() is not None and not self.controller.is_running()
        has_selection = bool(widget.tag_ranges("sel"))
        if not has_selection:
            widget.mark_set("insert", f"@{event.x},{event.y}")
        state = "normal" if has_doc else "disabled"
        for index in self._format_entries:
            self.editor_menu.entryconfigure(index, state=state)
        self.editor_menu.entryconfigure(5, state="normal" if has_doc and has_selection else "disabled")
        for index in (7, 8):  # Taglia, Copia
            self.editor_menu.entryconfigure(index, state="normal" if has_selection else "disabled")
        self.editor_menu.entryconfigure(11, state="normal" if has_doc and has_selection else "disabled")
        widget.focus_set()
        try:
            self.editor_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.editor_menu.grab_release()
        return "break"

    def _show_preview_menu(self, event) -> str:
        LOGGER.info("Menu tasto destro aperto (anteprima).")
        has_doc = self._current_doc() is not None and not self.controller.is_running()
        has_selection = bool(self.preview.tag_ranges("sel"))
        rendered = self.preview_mode_var.get() == PREVIEW_MODES[0]
        state = "normal" if has_doc and has_selection and rendered else "disabled"
        for index in range(6):
            self.preview_menu.entryconfigure(index, state=state)
        self.preview_menu.entryconfigure(7, state="normal" if has_selection else "disabled")
        self.preview_menu.entryconfigure(8, state=state)
        try:
            self.preview_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.preview_menu.grab_release()
        return "break"

    _PREVIEW_PREFIX_RE = re.compile(
        r"^(?:[•◦▪‣]|\d+\.|\d+(?:\.\d+)*|Parte [IVXLCDM]+|Capitolo \d+)\s{2,}"
    )

    def _preview_selection_targets(self) -> list[tuple[str, str]]:
        """Intervalli dell'editor che corrispondono al testo selezionato nell'anteprima.

        Ogni riga selezionata (senza punti elenco, numeri di sezione o di lista)
        viene cercata nel sorgente, in ordine; per la prima si sceglie
        l'occorrenza con lo stesso indice che ha nell'anteprima, cosi' un testo
        ripetuto in piu' slide viene trovato nel punto giusto.
        """
        try:
            selected = self.preview.get("sel.first", "sel.last")
            before = self.preview.get("1.0", "sel.first")
        except tk.TclError:
            return []
        self._commit_editor()
        source = self._editor_text()
        lines = []
        for raw in selected.split("\n"):
            line = self._PREVIEW_PREFIX_RE.sub("", raw.strip()) if raw.strip() else ""
            if not line or line.startswith("SLIDE ") or set(line) <= {"─"}:
                continue
            lines.append(line)
        targets: list[tuple[str, str]] = []
        position = 0
        for index, line in enumerate(lines):
            candidates = [line] + ([line.replace("$", "\\$")] if "$" in line else [])
            found = -1
            length = 0
            for candidate in candidates:
                if index == 0:
                    occurrence = before.count(candidate)
                    found = self._nth_find(source, candidate, occurrence)
                    if found == -1:
                        found = source.find(candidate)
                else:
                    found = source.find(candidate, position)
                if found != -1:
                    length = len(candidate)
                    break
            if found == -1:
                continue
            targets.append((f"1.0+{found}c", f"1.0+{found + length}c"))
            position = found + length
        return targets

    @staticmethod
    def _nth_find(text: str, needle: str, occurrence: int) -> int:
        position = -1
        for _ in range(occurrence + 1):
            position = text.find(needle, position + 1)
            if position == -1:
                return -1
        return position

    def _apply_on_preview_selection(self, style: str, color: str = "") -> None:
        """Applica la formattazione (``style`` vuoto = rimuovi) al testo selezionato nell'anteprima."""
        if self._current_doc() is None or self.controller.is_running():
            return
        targets = self._preview_selection_targets()
        if not targets:
            messagebox.showinfo(
                "PDF e Slide",
                "Non riesco a ritrovare il testo selezionato nel sorgente (succede se la selezione "
                "comprende formule o testo gia' formattato).\nSelezionalo nell'editor e usa il tasto destro li'.",
                parent=self,
            )
            return
        widget = self.editor._textbox
        # dall'ultimo al primo, cosi' gli indici precedenti restano validi
        for start, end in reversed(targets):
            start, end = widget.index(start), widget.index(end)
            widget.tag_remove("sel", "1.0", "end")
            widget.tag_add("sel", start, end)
            if style:
                self.format_selection(style, color)
            else:
                self.clear_formatting()
        widget.tag_remove("sel", "1.0", "end")
        widget.see(widget.index(targets[0][0]))

    def _reveal_preview_selection(self) -> None:
        targets = self._preview_selection_targets()
        if not targets:
            return
        widget = self.editor._textbox
        widget.tag_remove("sel", "1.0", "end")
        widget.tag_add("sel", targets[0][0], targets[-1][1])
        widget.mark_set("insert", targets[-1][1])
        widget.see(targets[0][0])
        widget.focus_set()

    def _wrapper(self, style: str, color: str = "") -> tuple[str, str]:
        doc = self._current_doc()
        latex = doc is not None and doc.fmt == SOURCE_LATEX
        if style == STYLE_BOLD:
            return ("\\textbf{", "}") if latex else ("**", "**")
        if style == STYLE_ITALIC:
            return ("\\emph{", "}") if latex else ("*", "*")
        if style == STYLE_UNDERLINE:
            return "\\underline{", "}"
        if style == STYLE_MONO:
            return "\\texttt{", "}"
        return f"\\textcolor{{{color or 'red'}}}{{", "}"

    _LINE_PREFIX_RE = re.compile(r"^(\s*(?:[-*+•]\s+|\d+[.)]\s+|#{1,6}\s+)?)(.*?)(\s*)$")
    _SKIP_LINE_RE = re.compile(r"^\s*(=+\s*SLIDE\s+\d+\s*=+|\$\$|\\\[|\\\]|\\begin\{|\\end\{|%|\|)", re.IGNORECASE)
    _COLOR_OPEN_RE = re.compile(r"\\textcolor\{[^{}]*\}\{$")

    def _wrap_text(self, text: str, prefix: str, suffix: str) -> str:
        """Avvolge ogni riga non vuota mantenendo punti elenco, numeri e # dei titoli."""
        lines = text.split("\n")
        out = []
        for line in lines:
            if not line.strip() or (len(lines) > 1 and self._SKIP_LINE_RE.match(line)):
                out.append(line)
                continue
            match = self._LINE_PREFIX_RE.match(line) if len(lines) > 1 else None
            if match and match.group(2):
                out.append(f"{match.group(1)}{prefix}{match.group(2)}{suffix}{match.group(3)}")
            else:
                out.append(f"{prefix}{line}{suffix}")
        return "\n".join(out)

    def _replace_range(self, start: str, end: str, new_text: str, select: bool = True) -> None:
        widget = self.editor._textbox
        widget.edit_separator()
        widget.delete(start, end)
        widget.insert(start, new_text)
        widget.edit_separator()
        widget.tag_remove("sel", "1.0", "end")
        if select:
            widget.tag_add("sel", start, f"{start}+{len(new_text)}c")
        widget.mark_set("insert", f"{start}+{len(new_text)}c")

    def format_selection(self, style: str, color: str = "") -> None:
        """Applica (o toglie, se gia' presente) la formattazione alla selezione."""
        if self._current_doc() is None or self.controller.is_running():
            return
        widget = self.editor._textbox
        prefix, suffix = self._wrapper(style, color)
        ranges = widget.tag_ranges("sel")
        if not ranges:
            # nessuna selezione: inserisce il comando vuoto con il cursore dentro
            widget.edit_separator()
            widget.insert("insert", prefix + suffix)
            widget.mark_set("insert", f"insert-{len(suffix)}c")
            widget.edit_separator()
            return
        start, end = str(ranges[0]), str(ranges[1])
        selected = widget.get(start, end)
        before = widget.get(f"{start} linestart", start)
        after = widget.get(end, f"{end} lineend")
        # 1) la selezione e' gia' dentro lo stesso comando: lo si toglie (o si cambia colore)
        if style == STYLE_COLOR:
            opened = self._COLOR_OPEN_RE.search(before)
            if opened and after.startswith("}"):
                outer_start = f"{start}-{len(opened.group(0))}c"
                current = re.match(r"\\textcolor\{([^{}]*)\}", opened.group(0)).group(1)
                replacement = selected if current == color else prefix + selected + suffix
                self._replace_range(outer_start, f"{end}+1c", replacement)
                return
        elif before.endswith(prefix) and after.startswith(suffix) and not (
            style == STYLE_ITALIC and prefix == "*" and before.endswith("**")
        ):
            self._replace_range(f"{start}-{len(prefix)}c", f"{end}+{len(suffix)}c", selected)
            return
        # 2) la selezione comprende gia' il comando: lo si toglie
        if "\n" not in selected:
            if style == STYLE_COLOR:
                inner = re.fullmatch(r"\\textcolor\{([^{}]*)\}\{(.*)\}", selected, re.DOTALL)
                if inner and self._balanced(inner.group(2)):
                    replacement = inner.group(2) if inner.group(1) == color else prefix + inner.group(2) + suffix
                    self._replace_range(start, end, replacement)
                    return
            elif (
                selected.startswith(prefix) and selected.endswith(suffix) and len(selected) > len(prefix + suffix)
                and self._balanced(selected[len(prefix):len(selected) - len(suffix)])
                and not (prefix == "*" and selected.startswith("**"))
            ):
                self._replace_range(start, end, selected[len(prefix):len(selected) - len(suffix)])
                return
        # 3) altrimenti si applica, riga per riga
        self._replace_range(start, end, self._wrap_text(selected, prefix, suffix))

    @staticmethod
    def _balanced(text: str) -> bool:
        depth = 0
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    return False
            index += 1
        return depth == 0

    @staticmethod
    def _strip_styles(text: str) -> str:
        """Toglie tutta la formattazione in linea lasciando testo e formule."""
        lines = []
        for line in text.split("\n"):
            out = []
            for node_kind, content, _styles in iter_runs(parse_inline(line)):
                out.append(f"${content}$" if node_kind == "math" else content)
            lines.append("".join(out))
        return "\n".join(lines)

    def clear_formatting(self) -> None:
        if self._current_doc() is None or self.controller.is_running():
            return
        widget = self.editor._textbox
        ranges = widget.tag_ranges("sel")
        if not ranges:
            return
        start, end = str(ranges[0]), str(ranges[1])
        # se la selezione e' esattamente il contenuto di un comando, si toglie anche quello
        before = widget.get(f"{start} linestart", start)
        after = widget.get(end, f"{end} lineend")
        outer = re.search(r"(\\(?:underline|texttt|textbf|textit|emph)\{|\\textcolor\{[^{}]*\}\{|\*\*|\*)$", before)
        if outer:
            closing = "}" if outer.group(1).endswith("{") else outer.group(1)
            if after.startswith(closing):
                start = f"{start}-{len(outer.group(1))}c"
                end = f"{end}+{len(closing)}c"
        selected = widget.get(start, end)
        cleaned = self._strip_styles(selected)
        if cleaned != selected:
            self._replace_range(start, end, cleaned)

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
            self._schedule_draft_save()
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
        self._schedule_draft_save()

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
    # ------------------------------------------------------------------ materia
    def selected_subject(self) -> str:
        value = self.subject_var.get().strip() if hasattr(self, "subject_var") else ""
        return "" if value in {"", NO_SUBJECT} else value

    def _refresh_subjects(self) -> None:
        try:
            tags = self.db.list_subject_tags()
        except Exception:
            tags = []
        current = self.selected_subject()
        values = [NO_SUBJECT, *tags]
        if current and current.casefold() not in {tag.casefold() for tag in tags}:
            values.append(current)
        self.subject_combo.configure(values=values)
        self.subject_combo.set(current or NO_SUBJECT)
        self._update_subject_hint()

    def _update_subject_hint(self) -> None:
        subject = self.selected_subject()
        if subject:
            text = f"Gli export vanno in Materie/{subject}/Appunti e compaiono nella pagina della materia."
        else:
            text = "Scegli una materia per salvare gli export nella sua cartella e collegarli alla materia."
        self.subject_hint.configure(text=text)

    def _on_subject_change(self, *_args) -> None:
        subject = self.selected_subject()
        if subject:
            self.app.subject_folder(subject)
        self._update_subject_hint()
        self._save_settings()

    def _link_exports(self, paths: list[Path]) -> None:
        """Collega i file esportati alla materia scelta (sezione Materie)."""
        subject = self.selected_subject()
        if not subject:
            return
        for path in paths:
            if not path.is_file():
                continue
            try:
                self.app.register_generated_material(path, subject, path.name)
            except Exception:
                LOGGER.exception("Collegamento dell'export alla materia non riuscito.")

    def _export_initial_dir(self, doc: DocState) -> str:
        subject = self.selected_subject()
        if subject:
            folder = self.app.subject_folder(subject, KIND_NOTES)
            if folder is not None:
                return str(folder)
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
        if not self.selected_subject():
            self._export_dir = str(Path(target).parent)
        self._save_settings()
        source, source_format, options = doc.source, doc.fmt, self.render_options(doc)
        if fmt != FORMAT_DOCX:
            try:
                export_file(source, source_format, fmt, target, options)
            except (ExportError, OSError) as exc:
                messagebox.showerror("PDF e Slide", f"Esportazione non riuscita:\n{exc}", parent=self)
                return
            self._link_exports([Path(target)])
            self._set_status(f"Esportato: {Path(target).name}")
            self.app.toast(f"Esportato {Path(target).name}")
            return

        def task(progress, cancel):
            progress(0.3, "Conversione Word con Pandoc…")
            return export_file(source, source_format, fmt, target, options)

        def done(_event: dict[str, Any]) -> None:
            self._link_exports([Path(target)])
            message = f"Esportato: {Path(target).name}"
            self._set_status(message)
            self.progress.set(1.0)
            self.app.toast(message)

        self._run_task(task, f"Esportato: {Path(target).name}", done)

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
        if not self.selected_subject():
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
            written_paths: list[str] = []
            errors: list[str] = []
            for index, (source, source_format, options, fmt, target) in enumerate(snapshots):
                if cancel.is_set():
                    raise PdfCancelled("Esportazione annullata.")
                progress(index / len(snapshots), f"Esporto {target.name} ({index + 1}/{len(snapshots)})")
                try:
                    export_file(source, source_format, fmt, target, options)
                    written += 1
                    written_paths.append(str(target))
                except (ExportError, OSError) as exc:
                    errors.append(f"{target.name}: {exc}")
            if errors and not written:
                raise ExportError("\n".join(errors[:5]))
            return {"written": written, "errors": errors, "folder": folder, "paths": written_paths}

        def done(event: dict[str, Any]) -> None:
            result = event.get("result") or {}
            self._link_exports([Path(value) for value in result.get("paths") or []])
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

    # ------------------------------------------------------------------ struttura LaTeX e template
    def _ask_structure_for_import(self) -> dict[str, Any] | None:
        multiple = len(self.sources) > 1
        proposed = "" if multiple else _pretty_name(self.sources[0])
        dialog = LatexStructureDialog(
            self, self._latex_defaults, proposed, multiple=multiple,
            heading="Importazione: struttura LaTeX", confirm_text="Estrai",
        )
        result = dialog.show()
        if result is None:
            return None
        result.pop("apply_all", None)
        self._latex_defaults = {key: result[key] for key in ("numbered", "embed", "base_level")}
        return result

    def edit_structure(self) -> None:
        doc = self._current_doc()
        if doc is None:
            return
        current = dict(self._latex_defaults)
        current.update(doc.latex)
        proposed = str(current.get("root_title") or "") or _pretty_name(doc.path)
        dialog = LatexStructureDialog(
            self, current, proposed, heading="Struttura LaTeX", confirm_text="Applica",
            allow_apply_all=len(self.docs) > 1,
        )
        result = dialog.show()
        if result is None:
            return
        apply_all = result.pop("apply_all", False)
        self._latex_defaults = {key: result[key] for key in ("numbered", "embed", "base_level")}
        for target in (self.docs if apply_all else [doc]):
            values = dict(result)
            if target is not doc:
                values["root_title"] = target.latex.get("root_title", "")
            target.latex = values
        self._save_settings()
        self.schedule_render(immediate=True)
        self._update_buttons()
        self._schedule_draft_save()

    def _update_structure_label(self) -> None:
        doc = self._current_doc()
        if doc is None:
            self.structure_label.configure(text="Viene chiesta a ogni importazione.")
            return
        options = self.render_options(doc)
        numbered = "numerate" if options.numbered_sections else "non numerate"
        if options.embed:
            star = "" if options.numbered_sections else "*"
            text = f"Inglobato, sezioni {numbered}: \\{options.base_level}{star}{{{options.root_title}}}"
        else:
            text = f"Documento autonomo, sezioni {numbered}."
        self.structure_label.configure(text=text)

    def _load_template_text(self) -> None:
        self._template_text = ""
        self._template_error = ""
        if not self._template_path:
            return
        path = Path(self._template_path)
        try:
            raw = path.read_bytes()
        except OSError:
            self._template_error = "file non trovato"
            return
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        if "\\begin{document}" not in text:
            self._template_error = "manca \\begin{document}"
            return
        self._template_text = text

    def template_name(self) -> str:
        return Path(self._template_path).name if self._template_path and self._template_text else ""

    def _update_template_label(self) -> None:
        if not hasattr(self, "template_status"):
            return
        if not self._template_path:
            text, color = "Nessuno: si usa il preambolo standard.", COLORS.text_faint
        elif self._template_error:
            text, color = f"{Path(self._template_path).name}: {self._template_error}.", COLORS.amber
        else:
            text, color = (
                f"{Path(self._template_path).name} (usato con «Preambolo LaTeX» attivo, non per l'inglobazione).",
                COLORS.text_muted,
            )
        self.template_status.configure(text=text, text_color=color)

    def choose_template(self, parent=None) -> None:
        initial = Path(self._template_path).parent if self._template_path else Path(self._initial_dir())
        name = filedialog.askopenfilename(
            parent=parent or self, title="Scegli il template LaTeX",
            initialdir=str(initial) if initial.is_dir() else self._initial_dir(),
            filetypes=[("LaTeX", "*.tex"), ("Tutti i file", "*.*")],
        )
        if not name:
            return
        previous = (self._template_path, self._template_text, self._template_error)
        self._template_path = name
        self._load_template_text()
        if self._template_error:
            messagebox.showwarning(
                "PDF e Slide", f"Template non valido ({self._template_error}).", parent=parent or self,
            )
            self._template_path, self._template_text, self._template_error = previous
            return
        try:
            apply_template(self._template_text, "\\section{Prova}\n", RenderOptions())
        except ExportError as exc:
            messagebox.showwarning("PDF e Slide", f"Template non valido: {exc}", parent=parent or self)
            self._template_path, self._template_text, self._template_error = previous
            return
        self._save_settings()
        self._update_template_label()
        self._update_buttons()
        self.schedule_render(immediate=True)
        self.app.toast(f"Template: {Path(name).name}")

    def clear_template(self) -> None:
        self._template_path = ""
        self._load_template_text()
        self._save_settings()
        self._update_template_label()
        self._update_buttons()
        self.schedule_render(immediate=True)

    # ------------------------------------------------------------------ salvataggio temporaneo
    def _draft_exists(self) -> bool:
        return self._draft_path is not None and self._draft_path.is_file()

    def _is_extracting(self) -> bool:
        return self.controller.is_running() and self._task_label == "extract"

    def _schedule_draft_save(self) -> None:
        if self._draft_path is None:
            return
        if self._draft_job is not None:
            try:
                self.after_cancel(self._draft_job)
            except (tk.TclError, ValueError):
                pass
        self._draft_job = self.after(DRAFT_DEBOUNCE_MS, self._save_draft)

    def _cancel_draft_job(self) -> None:
        if self._draft_job is not None:
            try:
                self.after_cancel(self._draft_job)
            except (tk.TclError, ValueError):
                pass
            self._draft_job = None

    def _save_draft(self, force: bool = False, notify: bool = False) -> None:
        """Scrive la bozza su disco (o la rimuove se non c'e' piu' nulla da salvare).

        Durante un'estrazione non si salva: la bozza precedente resta intatta
        finche' la nuova estrazione non e' terminata (o l'app viene chiusa).
        """
        self._cancel_draft_job()
        if self._draft_path is None:
            return
        if self._is_extracting() and not force:
            return
        self._commit_editor()
        if not self.docs:
            # Nessun documento: niente da salvare. Una bozza gia' presente resta
            # su disco finche' non si preme «Elimina».
            return
        payload = [
            {
                "path": str(doc.path),
                "title": doc.title,
                "source": doc.source,
                "fmt": doc.fmt,
                "warnings": list(doc.warnings),
                "engines": dict(doc.engines),
                "error": doc.error,
                "latex": dict(doc.latex),
            }
            for doc in self.docs
        ]
        try:
            self._draft_saved_at = save_draft(
                self._draft_path, payload, self.current, [str(path) for path in self.sources]
            )
        except Exception as exc:
            LOGGER.exception("Salvataggio bozza PDF non riuscito.")
            if notify:
                messagebox.showerror("PDF e Slide", f"Salvataggio della bozza non riuscito:\n{exc}", parent=self)
            return
        self._update_draft_label()
        self._update_buttons()
        if notify:
            self.app.toast("Bozza salvata")

    def _update_draft_label(self) -> None:
        if not hasattr(self, "draft_label"):
            return
        if not self._draft_saved_at or not self._draft_exists():
            self.draft_label.configure(
                text="Nessuna bozza salvata. Le modifiche vengono salvate in automatico.",
                text_color=COLORS.text_faint,
            )
            return
        stamp = time.localtime(self._draft_saved_at)
        if time.strftime("%Y-%m-%d", stamp) == time.strftime("%Y-%m-%d"):
            when = time.strftime("alle %H:%M", stamp)
        else:
            when = time.strftime("il %d/%m alle %H:%M", stamp)
        count = len(self.docs)
        docs = "1 documento" if count == 1 else f"{count} documenti"
        self.draft_label.configure(
            text=f"Bozza salvata {when} ({docs}). Resta disponibile anche dopo la chiusura dell'app.",
            text_color=COLORS.text_muted,
        )

    def _restore_draft(self) -> None:
        if self._draft_path is None:
            self._update_draft_label()
            return
        try:
            data = load_draft(self._draft_path)
        except Exception:
            LOGGER.exception("Lettura bozza PDF non riuscita.")
            data = None
        if not data:
            self._update_draft_label()
            return
        self.docs = [
            DocState(
                path=Path(raw["path"]),
                title=raw["title"],
                source=raw["source"],
                fmt=SOURCE_LATEX if raw["fmt"] == SOURCE_LATEX else SOURCE_MARKDOWN,
                warnings=list(raw["warnings"]),
                engines=dict(raw["engines"]),
                error=raw["error"],
                latex=dict(raw.get("latex") or {}),
            )
            for raw in data["docs"]
        ]
        self.sources = [Path(item) for item in data["sources"] if Path(item).is_file()]
        self._draft_saved_at = float(data.get("saved_at") or 0.0)
        self.current = -1
        self._update_source_label()
        self._refresh_doc_menu()
        self._select_doc(int(data["current"]))
        self._cancel_draft_job()
        self._update_draft_label()
        self._set_status("Bozza ripristinata: puoi continuare da dove eri rimasto.")

    def delete_draft(self) -> None:
        if self.controller.is_running():
            return
        if not messagebox.askyesno(
            "PDF e Slide",
            "Eliminare la bozza salvata?\n\nIl testo estratto e tutte le modifiche manuali verranno "
            "rimossi dall'editor. I file gia' esportati non vengono toccati.",
            icon="warning",
            parent=self,
        ):
            return
        self._cancel_draft_job()
        if self._edit_job is not None:
            try:
                self.after_cancel(self._edit_job)
            except (tk.TclError, ValueError):
                pass
            self._edit_job = None
        try:
            if self._draft_path is not None:
                delete_draft_file(self._draft_path)
        except Exception as exc:
            LOGGER.exception("Eliminazione bozza PDF non riuscita.")
            messagebox.showerror("PDF e Slide", f"Eliminazione della bozza non riuscita:\n{exc}", parent=self)
            return
        self._draft_saved_at = 0.0
        self.docs = []
        self.current = -1
        self._set_editor_text("")
        self.source_mode_var.set(SOURCE_MODES[0])
        self._update_editor_title()
        self.info_label.configure(text="")
        self._refresh_doc_menu()
        self._show_placeholder()
        self.progress.set(0)
        self._set_status("Bozza eliminata.")
        self._update_draft_label()
        self._update_buttons()
        self.app.toast("Bozza eliminata")

    # ------------------------------------------------------------------ ciclo di vita
    def on_show(self) -> None:
        self._refresh_subjects()
        self._update_buttons()
        if self.controller.is_running():
            self._start_polling()

    def on_hide(self) -> None:
        if self._remove_dialog is not None and self._remove_dialog.winfo_exists():
            self._remove_dialog.close()
        self._commit_editor()
        self._save_settings()
        self._save_draft()

    def shutdown(self) -> None:
        try:
            self._commit_editor()
            # Se l'app si chiude durante un'estrazione si salva quanto gia' arrivato.
            if self.docs:
                self._save_draft(force=True)
        except Exception:
            LOGGER.exception("Salvataggio bozza PDF in chiusura non riuscito.")
        try:
            self.controller.shutdown()
        except Exception:
            LOGGER.exception("Chiusura conversione PDF non riuscita.")
