"""Motori di estrazione locali per PDF vettoriali, scansionati e scientifici.

Ordine di lavoro per ogni pagina:

1. testo vettoriale con PyMuPDF (``fitz``), oppure pdfplumber come alternativa;
2. se la pagina non contiene testo ma immagini (scansione) -> OCR con
   Tesseract (eseguibile locale) o PaddleOCR (pacchetto Python opzionale);
3. pulizia di intestazioni/piè di pagina ripetuti e numeri di pagina;
4. per le pagine con molte formule, se configurato, Marker o Nougat
   (strumenti locali installati a parte) sostituiscono il risultato
   euristico con Markdown + LaTeX di qualita' superiore.

Nessuna funzione effettua chiamate di rete. Marker e Nougat possono scaricare i
propri modelli al primo utilizzo: e' un comportamento loro, avviato solo se
l'utente li sceglie esplicitamente.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...paths import app_data_dir
from .document import Block, PdfDocument, Slide, parse_markdown
from .layout import BULLET_GLYPHS, Line, PageLines, analyze_page, body_font_size, normalize_text, strip_repeated_margins
from .mathlayout import GLUE_RE, Atom, atom_from_span, atoms_to_latex, classify, dominant_baseline, is_math_atom
from .mathconv import (
    escape_dollars,
    is_math_font,
    math_char_count,
    normalize_math_delimiters,
    unicode_math_to_latex,
    wrap_unicode_math,
)


LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]

DEFAULT_MARKER_COMMAND = "marker {input_dir} --output_dir {output_dir}"
DEFAULT_NOUGAT_COMMAND = "nougat {inputs} -o {output_dir} --no-skipping"
MIN_TEXT_CHARS = 25
OCR_LANGUAGE_DEFAULT = "ita+eng"

ENGINE_LABELS = {
    "pymupdf": "PyMuPDF",
    "pdfplumber": "pdfplumber",
    "tesseract": "OCR Tesseract",
    "paddle": "OCR PaddleOCR",
    "marker": "Marker",
    "nougat": "Nougat",
}


class PdfToolsError(RuntimeError):
    """Errore gestibile, con messaggio mostrabile all'utente."""


class PdfCancelled(PdfToolsError):
    """Elaborazione annullata dall'utente."""


@dataclass
class ExtractOptions:
    engine: str = "auto"  # auto | pymupdf | pdfplumber
    ocr_mode: str = "auto"  # auto | off | force
    ocr_engine: str = "tesseract"  # tesseract | paddle
    ocr_language: str = OCR_LANGUAGE_DEFAULT
    ocr_dpi: int = 300
    math_engine: str = "none"  # none | marker | nougat
    math_scope: str = "math_pages"  # math_pages | all
    remove_headers: bool = True
    remove_page_numbers: bool = True
    dehyphenate: bool = True
    detect_math: bool = True
    marker_command: str = ""
    nougat_command: str = ""


# ---------------------------------------------------------------------------
# Rilevamento dei backend
# ---------------------------------------------------------------------------
def import_fitz():
    """Importa PyMuPDF con il nome moderno (``pymupdf``) o storico (``fitz``)."""
    for name in ("pymupdf", "fitz"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "open"):
            return module
    return None


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def find_tesseract() -> str | None:
    candidates = [
        os.environ.get("STUDYFLOW_TESSERACT", ""),
        str(app_data_dir() / "tesseract" / ("tesseract.exe" if os.name == "nt" else "tesseract")),
    ]
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if root:
                candidates.append(str(Path(root) / "Tesseract-OCR" / "tesseract.exe"))
                candidates.append(str(Path(root) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("tesseract")


_TESS_LANG_CACHE: dict[str, set[str]] = {}


def tesseract_languages(command: str) -> set[str]:
    if command in _TESS_LANG_CACHE:
        return _TESS_LANG_CACHE[command]
    languages: set[str] = set()
    try:
        result = subprocess.run(
            [command, "--list-langs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=_no_window_flags(),
        )
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            token = line.strip()
            if token and " " not in token and not token.endswith(":"):
                languages.add(token)
    except (OSError, subprocess.SubprocessError):
        pass
    _TESS_LANG_CACHE[command] = languages
    return languages


def resolve_language(command: str, requested: str) -> str:
    """Tiene solo le lingue Tesseract installate (fallback ``eng``)."""
    available = tesseract_languages(command)
    wanted = [part for part in re.split(r"[+,\s]+", requested or "") if part]
    if not available:
        return "+".join(wanted) or "eng"
    chosen = [part for part in wanted if part in available]
    if not chosen:
        chosen = ["eng"] if "eng" in available else sorted(available)[:1]
    return "+".join(chosen)


def _template_executable(template: str) -> str | None:
    try:
        parts = shlex.split(template, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    exe = parts[0].strip('"')
    if Path(exe).is_file():
        return exe
    return shutil.which(exe)


def math_engine_command(engine: str, options: ExtractOptions) -> str:
    if engine == "marker":
        return (options.marker_command or DEFAULT_MARKER_COMMAND).strip()
    if engine == "nougat":
        return (options.nougat_command or DEFAULT_NOUGAT_COMMAND).strip()
    return ""


def math_engine_available(engine: str, options: ExtractOptions) -> bool:
    template = math_engine_command(engine, options)
    return bool(template) and _template_executable(template) is not None


def backend_status(options: ExtractOptions | None = None) -> list[dict[str, Any]]:
    """Stato dei motori locali, per la scheda "Motori" della vista."""
    options = options or ExtractOptions()
    fitz = import_fitz()
    tesseract = find_tesseract()
    status = [
        {
            "key": "pymupdf",
            "label": "PyMuPDF",
            "available": fitz is not None,
            "detail": f"v{getattr(fitz, 'VersionBind', '?')}" if fitz else "pip install pymupdf",
        },
        {
            "key": "pdfplumber",
            "label": "pdfplumber",
            "available": has_module("pdfplumber"),
            "detail": "alternativa a PyMuPDF" if has_module("pdfplumber") else "opzionale",
        },
        {
            "key": "tesseract",
            "label": "Tesseract OCR",
            "available": tesseract is not None,
            "detail": "eseguibile locale" if tesseract else "installa Tesseract-OCR",
        },
        {
            "key": "paddle",
            "label": "PaddleOCR",
            "available": has_module("paddleocr"),
            "detail": "pacchetto Python" if has_module("paddleocr") else "opzionale",
        },
        {
            "key": "marker",
            "label": "Marker",
            "available": math_engine_available("marker", options),
            "detail": "formule (CLI locale)" if math_engine_available("marker", options) else "opzionale",
        },
        {
            "key": "nougat",
            "label": "Nougat",
            "available": math_engine_available("nougat", options),
            "detail": "formule (CLI locale)" if math_engine_available("nougat", options) else "opzionale",
        },
    ]
    return status


# ---------------------------------------------------------------------------
# PyMuPDF
# ---------------------------------------------------------------------------
_EQUATION_NUMBER_RE = re.compile(r"^\s*\((\d{1,3}(?:\.\d{1,3})?[a-z]?)\)\s*$")


def _split_trailing_token(atom: Atom) -> Atom | None:
    """Stacca l'ultima parola di uno span di testo come base di un pedice."""
    text = atom.text
    if not text or text[-1:].isspace():
        return None
    match = re.search(r"([A-Za-z0-9)\]]+)$", text)
    if not match:
        return None
    token = match.group(1)
    if len(token) > 3 and not _FUNCTION_ONLY_RE.match(token):
        return None
    ratio = len(token) / max(1, len(text))
    split_x = atom.x1 - (atom.x1 - atom.x0) * ratio
    base = Atom(
        text=token,
        x0=split_x,
        x1=atom.x1,
        y0=atom.y0,
        y1=atom.y1,
        origin_y=atom.origin_y,
        size=atom.size,
        font=atom.font,
        flags=atom.flags,
        math=True,
    )
    atom.text = text[: match.start()]
    atom.x1 = split_x
    return base


def line_atoms_to_pieces(atoms: list[Atom], detect_math: bool) -> list[list[Any]]:
    """Glifi di una riga -> segmenti ``[is_math, contenuto]``.

    Le sequenze di glifi matematici (font matematici, variabili in corsivo,
    apici/pedici, simboli Unicode) diventano formule LaTeX ricostruite con la
    loro geometria; il resto resta testo.
    """
    atoms = [atom for atom in atoms if atom.text]
    for atom in atoms:
        atom.text = normalize_text_keep_spaces(atom.text) if "CMEX" not in atom.font.upper() else atom.text
    atoms = [atom for atom in atoms if atom.text]
    if not atoms:
        return []
    if not detect_math:
        return [[False, "".join(atom.text for atom in atoms)]]
    base_y, base_size = dominant_baseline(atoms)
    classify(atoms, base_y, base_size)
    for atom in atoms:
        atom.math = atom.math or is_math_atom(atom, BULLET_GLYPHS)

    # Base di un apice/pedice che si trova nello span di testo precedente.
    expanded: list[Atom] = []
    for atom in atoms:
        if atom.math and atom.role != "base" and expanded and not expanded[-1].math:
            base = _split_trailing_token(expanded[-1])
            if base is not None:
                if not expanded[-1].text:
                    expanded.pop()
                expanded.append(base)
        expanded.append(atom)
    atoms = expanded

    # Testo di "colla" fra due formule (operatori, numeri, parentesi).
    math_positions = [index for index, atom in enumerate(atoms) if atom.math]
    for index, atom in enumerate(atoms):
        if atom.math or not math_positions:
            continue
        before = any(position < index for position in math_positions)
        after = any(position > index for position in math_positions)
        if before and after and GLUE_RE.match(atom.text):
            if atom.stripped:
                atom.math = True
        elif after and atom.stripped and _FUNCTION_ONLY_RE.match(atom.stripped):
            atom.math = True

    pieces: list[list[Any]] = []
    run: list[Atom] = []

    def flush_run() -> None:
        nonlocal run
        if run:
            latex = atoms_to_latex(run, base_y, base_size)
            if latex:
                pieces.append([True, latex])
            run = []

    for atom in atoms:
        if atom.math or (run and not atom.stripped):
            if not atom.math and not atom.stripped:
                # spazio dentro una formula: si conserva solo come separatore
                continue
            if not run and atom.text[:1].isspace() and pieces and not pieces[-1][0]:
                pieces[-1][1] += " "
            run.append(atom)
            continue
        flush_run()
        if pieces and not pieces[-1][0]:
            pieces[-1][1] += atom.text
        else:
            if pieces and pieces[-1][0] and atom.text[:1].isspace() is False and atom.text[:1].isalnum():
                pieces.append([False, " " + atom.text])
            else:
                pieces.append([False, atom.text])
    flush_run()
    return _absorb_operands(_balance_parentheses(pieces))


_OPERATOR_END_RE = re.compile(r"(=|<|>|\+|-|\\(?:leq|geq|neq|approx|to|cdot|times|in|le|ge|equiv|sim))\s*$")
_OPERATOR_START_RE = re.compile(r"^\s*(=|<|>|\+|\\(?:leq|geq|neq|approx|to|cdot|times|in|le|ge|equiv|sim)(?![A-Za-z]))")
_GLUE_HEAD_RE = re.compile(
    r"^(\s*[=+\-<>]\s*(?:\d+(?:[.,]\d+)?|[A-Za-z])(?:\s*[=+\-<>]\s*(?:\d+(?:[.,]\d+)?|[A-Za-z]))*)(?=$|[\s,.;:!?])"
)
_OPERAND_HEAD_RE = re.compile(r"^(\s*)(\d+(?:[.,]\d+)?|[A-Za-z])(?=$|[\s,.;:!?)])")
_OPERAND_TAIL_RE = re.compile(r"(?:^|(?<=\s))(\d+(?:[.,]\d+)?|[A-Za-z])(\s*)$")


def _absorb_operands(pieces: list[list[Any]]) -> list[list[Any]]:
    """``$|q| <$ 1`` -> ``$|q| < 1$``: l'operando rimasto nel testo entra nella formula."""
    for index, piece in enumerate(pieces):
        if not piece[0]:
            continue
        if index + 1 < len(pieces) and not pieces[index + 1][0]:
            following = pieces[index + 1][1]
            match = _OPERAND_HEAD_RE.match(following) if _OPERATOR_END_RE.search(piece[1]) else None
            if match:
                piece[1] = f"{piece[1].rstrip()} {match.group(2)}"
                pieces[index + 1][1] = following[match.end():]
            else:
                glue = _GLUE_HEAD_RE.match(following)
                if glue:
                    piece[1] = f"{piece[1].rstrip()} {unicode_math_to_latex(glue.group(1).strip())}"
                    pieces[index + 1][1] = following[glue.end():]
        if _OPERATOR_START_RE.match(piece[1]) and index > 0 and not pieces[index - 1][0]:
            match = _OPERAND_TAIL_RE.search(pieces[index - 1][1])
            if match:
                piece[1] = f"{match.group(1)} {piece[1].lstrip()}"
                pieces[index - 1][1] = pieces[index - 1][1][: match.start()] + " "
    return [piece for piece in pieces if piece[1] != ""]


_FUNCTION_ONLY_RE = re.compile(r"^(lim|sup|inf|max|min|sin|cos|tan|log|ln|exp|det)$")


def _balance_parentheses(pieces: list[list[Any]]) -> list[list[Any]]:
    for index, piece in enumerate(pieces):
        if not piece[0]:
            continue
        opened = piece[1].count("(") - piece[1].count(")")
        if opened > 0 and index + 1 < len(pieces) and not pieces[index + 1][0]:
            following = pieces[index + 1][1]
            moved = 0
            while moved < opened and following[:1] == ")":
                following = following[1:]
                moved += 1
            if moved:
                piece[1] += ")" * moved
                pieces[index + 1][1] = following
        if opened < 0 and index > 0 and not pieces[index - 1][0]:
            preceding = pieces[index - 1][1]
            moved = 0
            while moved < -opened and preceding[-1:] == "(":
                preceding = preceding[:-1]
                moved += 1
            if moved:
                piece[1] = "(" * moved + piece[1]
                pieces[index - 1][1] = preceding
    return [piece for piece in pieces if piece[1] != ""]


def normalize_text_keep_spaces(text: str) -> str:
    leading = " " if text[:1].isspace() else ""
    trailing = " " if text[-1:].isspace() and text.strip() else ""
    core = normalize_text(text)
    return f"{leading}{core}{trailing}" if core else (" " if text else "")


def pieces_to_markdown(pieces: list[list[Any]], detect_math: bool) -> tuple[str, bool, int]:
    """Segmenti -> testo Markdown; indica se la riga e' una formula display."""
    parts: list[str] = []
    math_chars = 0
    for is_math, content in pieces:
        if is_math:
            formula = re.sub(r"\s{2,}", " ", content).strip()
            if formula:
                parts.append(f"${formula}$")
                math_chars += len(formula)
            continue
        leading = " " if content[:1].isspace() else ""
        trailing = " " if content[-1:].isspace() else ""
        core = content.strip()
        if not core:
            parts.append(" ")
            continue
        math_chars += math_char_count(core)
        converted = wrap_unicode_math(core) if detect_math else escape_dollars(core)
        parts.append(f"{leading}{converted}{trailing}")
    text = re.sub(r"[ ]{2,}", " ", "".join(parts)).strip()
    display = False
    if detect_math:
        math_only = [p for p in pieces if p[0] and p[1].strip()]
        rest = "".join(p[1] for p in pieces if not p[0]).strip()
        equation_number = _EQUATION_NUMBER_RE.match(rest) if rest else None
        if len(math_only) == 1 and (not rest or equation_number):
            formula = math_only[0][1].strip()
            if len(formula) >= 3 and (re.search(r"[=<>]|\\[a-zA-Z]{2,}", formula) or equation_number):
                if equation_number:
                    formula = f"{formula} \\qquad ({equation_number.group(1)})"
                text = formula
                display = True
    return text, display, math_chars


def _pymupdf_page(page: Any, number: int, detect_math: bool) -> PageLines:
    rect = page.rect
    result = PageLines(number=number, width=float(rect.width), height=float(rect.height), engine="pymupdf")
    try:
        data = page.get_text("dict", sort=True)
    except TypeError:
        data = page.get_text("dict")
    raw_lines: list[tuple[int, tuple[float, float, float, float], list[Atom]]] = []
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type", 0) != 0:
            result.has_images = True
            continue
        for line in block.get("lines", []):
            direction = line.get("dir", (1.0, 0.0))
            if abs(float(direction[1])) > 0.2:
                continue  # testo verticale/ruotato (watermark, etichette assi)
            atoms = [atom_from_span(span) for span in line.get("spans", []) if span.get("text")]
            if not any(atom.stripped for atom in atoms):
                continue
            bbox = tuple(float(value) for value in line.get("bbox", (0, 0, 0, 0)))
            raw_lines.append((block_index, bbox, atoms))  # type: ignore[arg-type]

    _build_lines(result, raw_lines, detect_math)
    if not result.has_images:
        try:
            result.has_images = bool(page.get_images(full=False))
        except Exception:
            pass
    return result


def _build_lines(result: PageLines, raw_lines: list, detect_math: bool) -> None:
    """Righe grezze (glifi posizionati) -> ``Line`` con formule ricostruite."""
    if detect_math:
        raw_lines = _merge_math_fragments(raw_lines)
    regions: list[list[int]] = _display_regions(raw_lines) if detect_math else []
    in_region = {index for region in regions for index in region}
    region_lines: list[Line] = []
    for region in regions:
        atoms = [atom for index in region for atom in raw_lines[index][2]]
        for atom in atoms:
            if "CMEX" not in atom.font.upper():
                atom.text = normalize_text_keep_spaces(atom.text)
        latex = atoms_to_latex(atoms)
        if not latex:
            continue
        boxes = [raw_lines[index][1] for index in region]
        base_size = dominant_baseline(atoms)[1]
        region_lines.append(
            Line(
                text=latex,
                x0=min(box[0] for box in boxes),
                y0=min(box[1] for box in boxes),
                x1=max(box[2] for box in boxes),
                y1=max(box[3] for box in boxes),
                size=base_size,
                block_id=raw_lines[region[0]][0],
                raw=normalize_text(" ".join(atom.text for atom in atoms)),
                display_math=True,
                math_chars=len(latex),
            )
        )

    for index, (block_index, bbox, atoms) in enumerate(raw_lines):
        if index in in_region:
            continue
        raw = normalize_text("".join(atom.text for atom in atoms))
        if not raw:
            continue
        pieces = line_atoms_to_pieces(atoms, detect_math)
        text, display, math_chars = pieces_to_markdown(pieces, detect_math)
        if not text:
            continue
        dominant = max(atoms, key=lambda atom: len(atom.stripped))
        bold = bool(dominant.flags & 16) or "bold" in dominant.font.lower() or "CMSSBX" in dominant.font.upper()
        result.lines.append(
            Line(
                text=text,
                x0=bbox[0],
                y0=bbox[1],
                x1=bbox[2],
                y1=bbox[3],
                size=dominant.size,
                bold=bold,
                block_id=block_index,
                raw=raw,
                display_math=display,
                math_chars=math_chars,
            )
        )
    if region_lines:
        result.lines.extend(region_lines)
        result.lines.sort(key=lambda line: (round(line.y0 / 3.0), line.x0))
    _attach_equation_numbers(result.lines)


RawLine = tuple[int, tuple[float, float, float, float], list[Atom]]


def _visible_chars(atoms: list[Atom]) -> int:
    return sum(len(atom.stripped) for atom in atoms)


def _merge_math_fragments(raw_lines: list[RawLine]) -> list[RawLine]:
    """Unisce alla riga "ospite" i frammenti di formula su righe proprie.

    PyMuPDF separa in righe diverse numeratori, denominatori, limiti e il
    segno di radice: qui un frammento breve (solo glifi matematici) che si
    sovrappone verticalmente a un'altra riga viene fuso con essa, cosi' la
    ricostruzione geometrica vede la formula intera.
    """
    if len(raw_lines) < 2:
        return raw_lines
    sizes = sorted(atom.size for _, _, atoms in raw_lines for atom in atoms if atom.stripped)
    body = sizes[len(sizes) // 2] if sizes else 11.0
    fragments = [
        index for index, (_, _, atoms) in enumerate(raw_lines)
        if _visible_chars(atoms) <= 6 and _line_is_mathy(atoms, body)
        and not _EQUATION_NUMBER_RE.match("".join(atom.text for atom in atoms))
    ]
    if not fragments:
        return raw_lines
    merged_into: dict[int, int] = {}
    lines = [(block, list(bbox), list(atoms)) for block, bbox, atoms in raw_lines]
    # I frammenti piu' piccoli si uniscono per primi; un ospite e' preferibilmente
    # una riga non-frammento, altrimenti il frammento piu' lungo.
    for index in sorted(fragments, key=lambda item: _visible_chars(raw_lines[item][2])):
        if index in merged_into:
            continue
        _, box, atoms = lines[index]
        size = max((atom.size for atom in atoms if atom.stripped), default=body)
        best = None
        best_score = None
        for host_index, (_, host_box, host_atoms) in enumerate(lines):
            if host_index == index or host_index in merged_into or not host_atoms:
                continue
            host_size = max((atom.size for atom in host_atoms if atom.stripped), default=body)
            pad = 0.25 * host_size
            vertical = box[1] <= host_box[3] + pad and box[3] >= host_box[1] - pad
            horizontal = box[0] <= host_box[2] + 1.0 * host_size and box[2] >= host_box[0] - 1.0 * host_size
            if not (vertical and horizontal):
                continue
            is_fragment = host_index in fragments
            if is_fragment and _visible_chars(host_atoms) < _visible_chars(atoms):
                continue
            center = (box[1] + box[3]) / 2
            host_center = (host_box[1] + host_box[3]) / 2
            score = (1 if is_fragment else 0, abs(center - host_center) / max(1.0, size))
            if best_score is None or score < best_score:
                best = host_index
                best_score = score
        if best is None:
            continue
        host_block, host_box, host_atoms = lines[best]
        host_atoms.extend(atoms)
        host_atoms.sort(key=lambda atom: atom.x0)
        host_box[0], host_box[1] = min(host_box[0], box[0]), min(host_box[1], box[1])
        host_box[2], host_box[3] = max(host_box[2], box[2]), max(host_box[3], box[3])
        merged_into[index] = best
        lines[index] = (lines[index][0], box, [])
    return [
        (block, tuple(box), atoms)  # type: ignore[misc]
        for index, (block, box, atoms) in enumerate(lines)
        if index not in merged_into and atoms
    ]


def _attach_equation_numbers(lines: list[Line]) -> None:
    """``(1)`` allineato a una formula display diventa ``\\qquad (1)``."""
    for line in list(lines):
        match = _EQUATION_NUMBER_RE.match(line.raw)
        if not match:
            continue
        for other in lines:
            if other is line or not other.display_math:
                continue
            if line.y0 <= other.y1 and line.y1 >= other.y0:
                other.text = f"{other.text} \\qquad ({match.group(1)})"
                lines.remove(line)
                break


def _line_is_mathy(atoms: list[Atom], body: float) -> bool:
    visible = [atom for atom in atoms if atom.stripped]
    if not visible:
        return False
    has_math = False
    for atom in visible:
        if atom.bigop or is_math_atom(atom, BULLET_GLYPHS):
            has_math = True
            continue
        if GLUE_RE.match(atom.text) or atom.size < body * 0.85 or re.fullmatch(r"[.,;:!?]+", atom.stripped):
            continue
        if _FUNCTION_ONLY_RE.match(atom.stripped):
            continue
        return False
    text = "".join(atom.stripped for atom in visible)
    return has_math or len(text) <= 6


def _display_regions(raw_lines: list[tuple[int, tuple[float, float, float, float], list[Atom]]]) -> list[list[int]]:
    """Gruppi di righe vicine che compongono una formula display su piu' livelli."""
    all_atoms = [atom for _, _, atoms in raw_lines for atom in atoms if atom.stripped]
    if not all_atoms:
        return []
    weighted: list[float] = []
    for atom in all_atoms:
        weighted.extend([atom.size] * min(50, len(atom.stripped)))
    weighted.sort()
    body = weighted[len(weighted) // 2] if weighted else 11.0
    mathy = [index for index, (_, _, atoms) in enumerate(raw_lines) if _line_is_mathy(atoms, body)]
    regions: list[list[int]] = []
    boxes: list[list[float]] = []
    for index in sorted(mathy, key=lambda item: raw_lines[item][1][1]):
        x0, y0, x1, y1 = raw_lines[index][1]
        placed = False
        for region, box in zip(regions, boxes):
            vertical = y0 <= box[3] + 0.45 * body and y1 >= box[1] - 0.45 * body
            horizontal = x0 <= box[2] + 1.5 * body and x1 >= box[0] - 1.5 * body
            if vertical and horizontal:
                region.append(index)
                box[0], box[1] = min(box[0], x0), min(box[1], y0)
                box[2], box[3] = max(box[2], x1), max(box[3], y1)
                placed = True
                break
        if not placed:
            regions.append([index])
            boxes.append([x0, y0, x1, y1])
    # Le regioni cresciute dopo l'assegnazione possono ora toccarsi: unione
    # ripetuta finche' non cambia piu' nulla (es. numeratore isolato a destra).
    changed = True
    while changed:
        changed = False
        for first in range(len(regions)):
            for second in range(first + 1, len(regions)):
                a, b = boxes[first], boxes[second]
                vertical = b[1] <= a[3] + 0.45 * body and b[3] >= a[1] - 0.45 * body
                horizontal = b[0] <= a[2] + 1.5 * body and b[2] >= a[0] - 1.5 * body
                if vertical and horizontal:
                    regions[first].extend(regions.pop(second))
                    box = boxes.pop(second)
                    a[0], a[1], a[2], a[3] = min(a[0], box[0]), min(a[1], box[1]), max(a[2], box[2]), max(a[3], box[3])
                    changed = True
                    break
            if changed:
                break
    valid: list[list[int]] = []
    for region in regions:
        if len(region) < 2:
            continue
        atoms = [atom for index in region for atom in raw_lines[index][2]]
        if any(atom.bigop for atom in atoms) or _has_stacked_atoms(atoms):
            valid.append(sorted(region))
    return valid


def _has_stacked_atoms(atoms: list[Atom]) -> bool:
    base_y, base_size = dominant_baseline(atoms)
    ups = [atom for atom in atoms if atom.stripped and atom.origin_y < base_y - 0.3 * base_size]
    downs = [atom for atom in atoms if atom.stripped and atom.origin_y > base_y + 0.3 * base_size]
    return bool(ups and downs)


# ---------------------------------------------------------------------------
# pdfplumber
# ---------------------------------------------------------------------------
_CID_RE = re.compile(r"\(cid:(\d+)\)")


def _pdfplumber_char_text(char: dict[str, Any]) -> str:
    text = str(char.get("text", ""))
    if "(cid:" in text:
        text = _CID_RE.sub(lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "", text)
    return text


def _pdfplumber_page(page: Any, number: int, detect_math: bool) -> PageLines:
    """pdfplumber: i caratteri vengono raggruppati in span e righe e passano
    dalla stessa ricostruzione di layout e formule usata con PyMuPDF."""
    result = PageLines(number=number, width=float(page.width), height=float(page.height), engine="pdfplumber")
    result.has_images = bool(getattr(page, "images", []))
    height = float(page.height)
    chars = [char for char in getattr(page, "chars", []) if char.get("upright", True)]
    entries: list[dict[str, Any]] = []
    for char in chars:
        text = _pdfplumber_char_text(char)
        if not text:
            continue
        size = float(char.get("size", 10.0)) or 10.0
        matrix = char.get("matrix")
        baseline = height - float(matrix[5]) if matrix and len(matrix) == 6 else float(char["bottom"]) - 0.2 * size
        font_name = str(char.get("fontname", "")).split("+")[-1]
        flags = 0
        lowered = font_name.lower()
        if "italic" in lowered or "oblique" in lowered or font_name.upper().startswith(("CMMI", "CMTI", "CMSSI")):
            flags |= 2
        if "bold" in lowered or "CMBX" in font_name.upper():
            flags |= 16
        entries.append({
            "text": text, "x0": float(char["x0"]), "x1": float(char["x1"]), "top": float(char["top"]),
            "bottom": float(char["bottom"]), "baseline": baseline, "size": size, "font": font_name, "flags": flags,
        })
    entries.sort(key=lambda item: (item["baseline"], item["x0"]))
    rows: list[list[dict[str, Any]]] = []
    for entry in entries:
        for row in reversed(rows[-4:]):
            reference = row[0]
            if abs(entry["baseline"] - reference["baseline"]) <= 0.3 * max(entry["size"], reference["size"]):
                row.append(entry)
                break
        else:
            rows.append([entry])
    rows.sort(key=lambda row: min(item["top"] for item in row))
    raw_lines: list = []
    block_id = 0
    previous_bottom: float | None = None
    for row in rows:
        row.sort(key=lambda item: item["x0"])
        spans: list[dict[str, Any]] = []
        for entry in row:
            if spans:
                last = spans[-1]
                gap = entry["x0"] - last["bbox"][2]
                same = last["font"] == entry["font"] and abs(last["size"] - entry["size"]) < 0.2 \
                    and abs(last["origin"][1] - entry["baseline"]) < 0.5
                if gap > 0.22 * entry["size"] and not last["text"].endswith(" ") and entry["text"] != " ":
                    if same:
                        last["text"] += " "
                    else:
                        entry = dict(entry, text=" " + entry["text"])
                        same = False
                if same:
                    last["text"] += entry["text"]
                    last["bbox"][2] = max(last["bbox"][2], entry["x1"])
                    last["bbox"][1] = min(last["bbox"][1], entry["top"])
                    last["bbox"][3] = max(last["bbox"][3], entry["bottom"])
                    continue
            spans.append({
                "text": entry["text"], "bbox": [entry["x0"], entry["top"], entry["x1"], entry["bottom"]],
                "origin": (entry["x0"], entry["baseline"]), "size": entry["size"], "font": entry["font"],
                "flags": entry["flags"],
            })
        atoms = [atom_from_span(span) for span in spans if span["text"]]
        if not any(atom.stripped for atom in atoms):
            continue
        bbox = (
            min(item["x0"] for item in row), min(item["top"] for item in row),
            max(item["x1"] for item in row), max(item["bottom"] for item in row),
        )
        size = max(item["size"] for item in row)
        if previous_bottom is not None and bbox[1] - previous_bottom > size * 0.9:
            block_id += 1
        previous_bottom = bbox[3]
        raw_lines.append((block_id, bbox, atoms))
    _build_lines(result, raw_lines, detect_math)
    return result


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def _tesseract_lines(image_path: Path, command: str, language: str, scale: float) -> list[Line]:
    try:
        completed = subprocess.run(
            [command, str(image_path), "stdout", "-l", language, "--psm", "3", "tsv"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=_no_window_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfToolsError("Tesseract non ha risposto entro 10 minuti.") from exc
    except OSError as exc:
        raise PdfToolsError(f"Impossibile avviare Tesseract: {exc}") from exc
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()[-1:] or ["errore sconosciuto"]
        raise PdfToolsError(f"Tesseract ha restituito un errore: {tail[0]}")
    return parse_tesseract_tsv(completed.stdout, scale)


def parse_tesseract_tsv(tsv: str, scale: float = 1.0) -> list[Line]:
    """TSV di Tesseract -> righe con coordinate in punti PDF."""
    groups: dict[tuple[int, int, int], list[tuple[int, int, int, int, str]]] = {}
    order: list[tuple[int, int, int]] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            conf = float(cols[10])
            key = (int(cols[2]), int(cols[3]), int(cols[4]))
            left, top, width, height = (int(cols[index]) for index in (6, 7, 8, 9))
        except ValueError:
            continue
        if conf < 0:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((left, top, width, height, text))
    lines: list[Line] = []
    for key in order:
        words = groups[key]
        heights = sorted(word[3] for word in words)
        median_height = heights[len(heights) // 2]
        text = normalize_text(" ".join(word[4] for word in words))
        lines.append(
            Line(
                text=wrap_unicode_math(text),
                x0=min(word[0] for word in words) * scale,
                y0=min(word[1] for word in words) * scale,
                x1=max(word[0] + word[2] for word in words) * scale,
                y1=max(word[1] + word[3] for word in words) * scale,
                size=median_height * scale * 1.15,
                block_id=key[0] * 1000 + key[1],
                raw=text,
                math_chars=math_char_count(text),
            )
        )
    return lines


_PADDLE_CACHE: dict[str, Any] = {}
_PADDLE_LANGS = {"ita": "it", "eng": "en", "fra": "fr", "deu": "german", "spa": "es", "por": "pt"}


def _paddle_lines(image_path: Path, language: str, scale: float) -> list[Line]:
    try:
        from paddleocr import PaddleOCR  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PdfToolsError("PaddleOCR non e' installato (pip install paddleocr).") from exc
    first = (re.split(r"[+,\s]+", language or "eng") or ["eng"])[0]
    code = _PADDLE_LANGS.get(first, "en")
    engine = _PADDLE_CACHE.get(code)
    if engine is None:
        engine = PaddleOCR(lang=code)
        _PADDLE_CACHE[code] = engine
    result = None
    if hasattr(engine, "predict"):
        try:
            result = engine.predict(str(image_path))
        except Exception:
            result = None
    if result is None:
        result = engine.ocr(str(image_path))
    items: list[tuple[str, float, float, float, float]] = []
    for page_result in result or []:
        texts = None
        try:
            texts = page_result["rec_texts"]
        except (TypeError, KeyError, IndexError):
            texts = None
        if texts is not None:
            boxes = None
            for key in ("rec_boxes", "rec_polys", "dt_polys"):
                try:
                    boxes = page_result[key]
                    break
                except (TypeError, KeyError):
                    continue
            for index, text in enumerate(texts):
                box = boxes[index] if boxes is not None and index < len(boxes) else None
                items.append((str(text), *_box_bounds(box)))
            continue
        for entry in page_result or []:
            try:
                polygon, (text, _score) = entry[0], entry[1]
            except (TypeError, ValueError, IndexError):
                continue
            items.append((str(text), *_box_bounds(polygon)))
    items.sort(key=lambda item: (round(item[2] / 8), item[1]))
    lines: list[Line] = []
    block_id = 0
    previous: Line | None = None
    for text, x0, y0, x1, y1 in items:
        clean = normalize_text(text)
        if not clean:
            continue
        line = Line(
            text=wrap_unicode_math(clean),
            x0=x0 * scale,
            y0=y0 * scale,
            x1=x1 * scale,
            y1=y1 * scale,
            size=max(1.0, (y1 - y0) * scale * 0.95),
            raw=clean,
        )
        if previous is not None and line.y0 - previous.y1 > line.height * 0.9:
            block_id += 1
        line.block_id = block_id
        lines.append(line)
        previous = line
    return lines


def _box_bounds(box: Any) -> tuple[float, float, float, float]:
    if box is None:
        return 0.0, 0.0, 0.0, 0.0
    try:
        values = [float(value) for value in list(box)]
        if len(values) == 4:
            return values[0], values[1], values[2], values[3]
    except (TypeError, ValueError):
        pass
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        return min(xs), min(ys), max(xs), max(ys)
    except (TypeError, ValueError, IndexError):
        return 0.0, 0.0, 0.0, 0.0


def ocr_page_image(image_path: Path, page: PageLines, options: ExtractOptions, scale: float) -> PageLines:
    if options.ocr_engine == "paddle":
        lines = _paddle_lines(image_path, options.ocr_language, scale)
        engine = "paddle"
    else:
        command = find_tesseract()
        if not command:
            raise PdfToolsError(
                "Tesseract non trovato. Installa Tesseract-OCR (con la lingua italiana) "
                "oppure scegli PaddleOCR."
            )
        language = resolve_language(command, options.ocr_language)
        lines = _tesseract_lines(image_path, command, language, scale)
        engine = "tesseract"
    return PageLines(
        number=page.number,
        width=page.width,
        height=page.height,
        lines=lines,
        engine=engine,
        has_images=True,
    )


def _needs_ocr(page: PageLines, options: ExtractOptions) -> bool:
    if options.ocr_mode == "off":
        return False
    if options.ocr_mode == "force":
        return True
    chars = sum(len(line.raw) for line in page.lines)
    return chars < MIN_TEXT_CHARS and (page.has_images or chars == 0)


def ocr_available(options: ExtractOptions) -> bool:
    if options.ocr_engine == "paddle":
        return has_module("paddleocr")
    return find_tesseract() is not None


# ---------------------------------------------------------------------------
# Marker / Nougat
# ---------------------------------------------------------------------------
def build_engine_args(template: str, input_dir: Path, output_dir: Path, inputs: list[Path]) -> list[str]:
    """Espande il modello di comando sostituendo i segnaposto per argomento."""
    try:
        parts = shlex.split(template, posix=os.name != "nt")
    except ValueError as exc:
        raise PdfToolsError(f"Comando non valido: {exc}") from exc
    if not parts:
        raise PdfToolsError("Comando del motore formule vuoto.")
    args: list[str] = []
    for index, part in enumerate(parts):
        part = part.strip('"') if os.name == "nt" else part
        if part == "{inputs}":
            args.extend(str(path) for path in inputs)
            continue
        value = part.replace("{input_dir}", str(input_dir)).replace("{output_dir}", str(output_dir))
        if index == 0:
            value = _template_executable(value) or value
        args.append(value)
    return args


def run_math_engine(
    engine: str,
    fitz: Any,
    source: Any,
    page_indexes: list[int],
    options: ExtractOptions,
    cancel: threading.Event | None,
    progress: Callable[[str], None] | None = None,
) -> dict[int, str]:
    """Esegue Marker o Nougat sulle pagine indicate (un PDF per pagina).

    Le pagine vengono salvate come PDF singoli in una cartella temporanea e il
    motore viene avviato una sola volta su tutti i file: il modello viene
    caricato una volta sola e ogni output e' gia' associato alla sua pagina.
    """
    if not page_indexes:
        return {}
    template = math_engine_command(engine, options)
    if _template_executable(template) is None:
        raise PdfToolsError(f"{ENGINE_LABELS.get(engine, engine)} non trovato: controlla il comando configurato.")
    results: dict[int, str] = {}
    with tempfile.TemporaryDirectory(prefix="studyflow-pdf-") as temp:
        input_dir = Path(temp) / "input"
        output_dir = Path(temp) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        inputs: list[Path] = []
        for index in page_indexes:
            single = fitz.open()
            try:
                single.insert_pdf(source, from_page=index, to_page=index)
                target = input_dir / f"p{index + 1:04d}.pdf"
                single.save(str(target))
                inputs.append(target)
            finally:
                single.close()
        args = build_engine_args(template, input_dir, output_dir, inputs)
        LOGGER.info("Avvio %s: %s", engine, " ".join(args))
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_no_window_flags(),
            )
        except OSError as exc:
            raise PdfToolsError(f"Impossibile avviare {engine}: {exc}") from exc
        tail: list[str] = []

        def reader() -> None:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-20]
                if progress is not None:
                    progress(line[-120:])

        thread = threading.Thread(target=reader, name=f"studyflow-{engine}-output", daemon=True)
        thread.start()
        while process.poll() is None:
            if cancel is not None and cancel.wait(0.25):
                process.kill()
                process.wait(timeout=10)
                raise PdfCancelled("Elaborazione annullata.")
        thread.join(timeout=2)
        if process.returncode != 0:
            message = tail[-1] if tail else f"codice {process.returncode}"
            raise PdfToolsError(f"{ENGINE_LABELS.get(engine, engine)} terminato con errore: {message}")
        for index in page_indexes:
            stem = f"p{index + 1:04d}"
            found = sorted(
                [*output_dir.rglob(f"{stem}*.md"), *output_dir.rglob(f"{stem}*.mmd")],
                key=lambda path: len(str(path)),
            )
            if found:
                text = found[0].read_text(encoding="utf-8", errors="replace")
                results[index] = normalize_math_delimiters(text)
    return results


def _page_has_math(page: PageLines) -> bool:
    return any(line.display_math for line in page.lines) or sum(line.math_chars for line in page.lines) >= 12


# ---------------------------------------------------------------------------
# Pipeline completa
# ---------------------------------------------------------------------------
def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise PdfCancelled("Elaborazione annullata.")


def convert_pdf(
    path: str | Path,
    options: ExtractOptions | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> PdfDocument:
    """Estrae un PDF completo e restituisce il documento a blocchi."""
    options = options or ExtractOptions()
    source_path = Path(path)
    if not source_path.is_file():
        raise PdfToolsError(f"File non trovato: {source_path}")

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), message)

    fitz = import_fitz() if options.engine in {"auto", "pymupdf"} else None
    use_plumber = fitz is None
    if use_plumber and not has_module("pdfplumber"):
        if options.engine == "pdfplumber":
            raise PdfToolsError("pdfplumber non e' installato (pip install pdfplumber).")
        raise PdfToolsError("Nessun motore PDF disponibile: installa PyMuPDF (pip install pymupdf).")

    warnings: list[str] = []
    pages: list[PageLines] = []
    engines: dict[str, int] = {}
    math_pages: dict[int, str] = {}
    document_title = source_path.stem
    wants_math_engine = options.math_engine in {"marker", "nougat"}
    extraction_share = 0.75 if wants_math_engine else 1.0
    ocr_ok = ocr_available(options)
    ocr_warned = False

    if use_plumber:
        import pdfplumber  # type: ignore[import-not-found]

        try:
            handle = pdfplumber.open(str(source_path))
        except Exception as exc:
            raise PdfToolsError(f"Impossibile aprire il PDF: {exc}") from exc
        with handle as pdf:
            meta_title = str((pdf.metadata or {}).get("Title") or "").strip()
            if meta_title:
                document_title = meta_title
            total = len(pdf.pages)
            for index, page in enumerate(pdf.pages):
                _check_cancel(cancel)
                report(index / max(1, total) * extraction_share, f"Pagina {index + 1} di {total}")
                lines = _pdfplumber_page(page, index + 1, options.detect_math)
                if _needs_ocr(lines, options):
                    if ocr_ok:
                        with tempfile.TemporaryDirectory(prefix="studyflow-ocr-") as temp:
                            image = Path(temp) / "page.png"
                            page.to_image(resolution=options.ocr_dpi).save(str(image))
                            lines = ocr_page_image(image, lines, options, 72.0 / options.ocr_dpi)
                    elif not ocr_warned:
                        warnings.append("Alcune pagine sembrano scansionate ma nessun motore OCR e' disponibile.")
                        ocr_warned = True
                pages.append(lines)
        if wants_math_engine:
            warnings.append("Marker/Nougat richiedono PyMuPDF per dividere le pagine: motore formule saltato.")
            wants_math_engine = False
    else:
        try:
            source = fitz.open(str(source_path))
        except Exception as exc:
            raise PdfToolsError(f"Impossibile aprire il PDF: {exc}") from exc
        try:
            if getattr(source, "needs_pass", False):
                raise PdfToolsError("Il PDF e' protetto da password.")
            meta_title = str((source.metadata or {}).get("title") or "").strip()
            if meta_title and len(meta_title) > 3 and not meta_title.lower().startswith("microsoft"):
                document_title = meta_title
            total = source.page_count
            for index in range(total):
                _check_cancel(cancel)
                report(index / max(1, total) * extraction_share, f"Pagina {index + 1} di {total}")
                page = source.load_page(index)
                lines = _pymupdf_page(page, index + 1, options.detect_math)
                if _needs_ocr(lines, options):
                    if ocr_ok:
                        with tempfile.TemporaryDirectory(prefix="studyflow-ocr-") as temp:
                            image = Path(temp) / "page.png"
                            pixmap = page.get_pixmap(dpi=options.ocr_dpi)
                            pixmap.save(str(image))
                            lines = ocr_page_image(image, lines, options, 72.0 / options.ocr_dpi)
                    elif not ocr_warned:
                        warnings.append("Alcune pagine sembrano scansionate ma nessun motore OCR e' disponibile.")
                        ocr_warned = True
                pages.append(lines)

            if wants_math_engine:
                if not math_engine_available(options.math_engine, options):
                    warnings.append(
                        f"{ENGINE_LABELS[options.math_engine]} non trovato: uso il riconoscimento formule integrato."
                    )
                else:
                    selected = [
                        index for index, page in enumerate(pages)
                        if options.math_scope == "all" or _page_has_math(page)
                    ]
                    if selected:
                        label = ENGINE_LABELS[options.math_engine]
                        report(extraction_share, f"{label}: {len(selected)} pagine con formule...")
                        try:
                            math_pages = run_math_engine(
                                options.math_engine,
                                fitz,
                                source,
                                selected,
                                options,
                                cancel,
                                lambda line: report(extraction_share, f"{label}: {line}"),
                            )
                        except PdfCancelled:
                            raise
                        except PdfToolsError as exc:
                            warnings.append(f"{exc} Uso il riconoscimento integrato.")
                        missing = [index + 1 for index in selected if index not in math_pages]
                        if math_pages and missing:
                            warnings.append(
                                f"{label} non ha prodotto output per le pagine {', '.join(map(str, missing[:12]))}."
                            )
        finally:
            source.close()

    _check_cancel(cancel)
    removed = strip_repeated_margins(
        pages,
        remove_headers=options.remove_headers,
        remove_page_numbers=options.remove_page_numbers,
    )
    if removed:
        LOGGER.info("Rimosse %s righe di intestazione/piè di pagina da %s.", removed, source_path.name)

    slides: list[Slide] = []
    for index, page in enumerate(pages):
        markdown = math_pages.get(index)
        if markdown is not None:
            parsed = parse_markdown(markdown)
            blocks: list[Block] = [block for slide in parsed for block in slide.blocks]
            engine = options.math_engine
        else:
            blocks = analyze_page(page, dehyphenate=options.dehyphenate)
            engine = page.engine
        engines[engine] = engines.get(engine, 0) + 1
        slides.append(Slide(number=index + 1, blocks=blocks, engine=engine))
    report(1.0, "Estrazione completata")
    return PdfDocument(
        source=source_path,
        title=document_title,
        slides=slides,
        warnings=warnings,
        engines=engines,
    )
