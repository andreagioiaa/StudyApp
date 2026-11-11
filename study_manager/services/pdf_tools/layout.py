"""Analisi del layout: righe posizionate -> titoli, elenchi, paragrafi, formule.

Ogni motore di estrazione (PyMuPDF, pdfplumber, OCR) produce una lista di
``Line`` con coordinate in punti PDF e dimensione del font. Da qui in avanti la
pipeline e' unica, quindi elenchi puntati e titoli vengono riconosciuti nello
stesso modo indipendentemente dalla sorgente.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass, field

from .document import (
    BLOCK_BULLET,
    BLOCK_HEADING,
    BLOCK_MATH,
    BLOCK_NUMBER,
    BLOCK_PARAGRAPH,
    MAX_LIST_LEVEL,
    Block,
    join_lines,
)


BULLET_GLYPHS = set(
    "•●○◦▪▫■□►▸▹▶▷➢➣➤➔✓✔✗❖◆◇◘◙⦿⦾∙‣⁃"
    ""
)
DASH_BULLETS = {"-", "–", "—", "*", "·", "o"}
NUMBERED_RE = re.compile(r"^(\d{1,2})[.)]\s+(\S.*)$")
LETTERED_RE = re.compile(r"^([a-z])\)\s+(\S.*)$")
PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:\d{1,4}|\d{1,4}\s*/\s*\d{1,4}|\d{1,4}\s+(?:di|of)\s+\d{1,4}|"
    r"(?:pag(?:ina)?|page|p|slide)\.?\s*\d{1,4}(?:\s*(?:/|di|of)\s*\d{1,4})?|"
    r"[-–]\s*\d{1,4}\s*[-–])\s*$",
    re.IGNORECASE,
)
LIGATURES = {
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    " ": " ", " ": " ", " ": " ", "​": "", "﻿": "",
}


@dataclass(eq=False)
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 11.0
    bold: bool = False
    block_id: int = 0
    raw: str = ""
    display_math: bool = False
    math_chars: int = 0

    def __post_init__(self) -> None:
        if not self.raw:
            self.raw = self.text

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class PageLines:
    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)
    engine: str = "pymupdf"
    has_images: bool = False


# ---------------------------------------------------------------------------
# Pulizia del testo
# ---------------------------------------------------------------------------
_SPACING_ACCENTS = {"`": "\u0300", "´": "\u0301", "¨": "\u0308", "ˆ": "\u0302", "˜": "\u0303"}
_SPACING_ACCENT_RE = re.compile(r"([`´¨ˆ˜])([A-Za-z])")


def fix_spacing_accents(text: str) -> str:
    """``continuit`a`` -> ``continuità`` (PDF LaTeX con codifica OT1)."""
    if not any(char in text for char in _SPACING_ACCENTS):
        return text
    return _SPACING_ACCENT_RE.sub(
        lambda m: unicodedata.normalize("NFC", m.group(2) + _SPACING_ACCENTS[m.group(1)]), text
    )


def normalize_text(text: str) -> str:
    for source, target in LIGATURES.items():
        text = text.replace(source, target)
    text = fix_spacing_accents(text)
    text = "".join(char for char in text if char in "\t\n" or ord(char) >= 32)
    # Glifi senza codifica Unicode (controlli C1) usati come punto elenco.
    text = re.sub(r"^[\x80-\x9f]", "•", text.lstrip())
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def margin_key(text: str) -> str:
    """Chiave per riconoscere intestazioni ripetute anche con numeri diversi."""
    key = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", key).strip(" .-–|")


def strip_repeated_margins(
    pages: list[PageLines],
    remove_headers: bool = True,
    remove_page_numbers: bool = True,
    margin_ratio: float = 0.15,
    number_ratio: float = 0.2,
) -> int:
    """Rimuove intestazioni/piè di pagina ripetuti e numeri di pagina isolati.

    Una riga nella fascia superiore o inferiore della pagina viene considerata
    ripetitiva se, normalizzando i numeri, compare in almeno meta' delle pagine
    (minimo 3 pagine). Restituisce il numero di righe eliminate.
    """
    removed = 0
    counts: dict[str, int] = {}
    if remove_headers and len(pages) >= 3:
        for page in pages:
            seen: set[str] = set()
            for line in page.lines:
                if _in_margin(line, page, margin_ratio):
                    key = margin_key(line.raw)
                    if key and key not in seen:
                        seen.add(key)
                        counts[key] = counts.get(key, 0) + 1
    threshold = max(3, (len(pages) + 1) // 2)
    repeated = {key for key, count in counts.items() if count >= threshold and key != "#"}
    for page in pages:
        kept: list[Line] = []
        for line in page.lines:
            in_margin = _in_margin(line, page, margin_ratio)
            if in_margin and remove_headers and margin_key(line.raw) in repeated:
                removed += 1
                continue
            if remove_page_numbers and _in_margin(line, page, number_ratio) and PAGE_NUMBER_RE.match(line.raw):
                removed += 1
                continue
            kept.append(line)
        page.lines = kept
    return removed


def _in_margin(line: Line, page: PageLines, ratio: float) -> bool:
    if page.height <= 0:
        return False
    top = line.y1 <= page.height * ratio
    bottom = line.y0 >= page.height * (1.0 - ratio)
    return top or bottom


# ---------------------------------------------------------------------------
# Righe -> blocchi
# ---------------------------------------------------------------------------
def body_font_size(lines: list[Line]) -> float:
    weighted: list[float] = []
    for line in lines:
        weighted.extend([round(line.size, 1)] * max(1, min(200, len(line.raw))))
    if not weighted:
        return 11.0
    try:
        return float(statistics.median(weighted))
    except statistics.StatisticsError:
        return 11.0


def _bullet_split(text: str) -> tuple[str | None, str]:
    """Restituisce ``(tipo, testo)`` se la riga inizia con un punto elenco."""
    stripped = text.lstrip()
    if not stripped:
        return None, text
    first = stripped[0]
    code = ord(first)
    if first in BULLET_GLYPHS or 0x80 <= code <= 0x9F or 0xE000 <= code <= 0xF8FF:
        return "bullet", stripped[1:].lstrip()
    if first in DASH_BULLETS and len(stripped) > 2 and stripped[1] == " ":
        if first == "o" and not stripped[2:3].isupper():
            return None, text
        return "bullet", stripped[2:].lstrip()
    numbered = NUMBERED_RE.match(stripped) or LETTERED_RE.match(stripped)
    if numbered:
        return "number", numbered.group(2)
    return None, text


def _number_value(text: str) -> int | None:
    match = NUMBERED_RE.match(text.lstrip())
    if match:
        return int(match.group(1))
    lettered = LETTERED_RE.match(text.lstrip())
    if lettered:
        return ord(lettered.group(1)) - ord("a") + 1
    return None


def _indent_levels(starts: list[float], tolerance: float = 6.0) -> list[float]:
    levels: list[float] = []
    for value in sorted(starts):
        if not levels or value - levels[-1] > tolerance:
            levels.append(value)
    return levels


def _level_for(x: float, levels: list[float]) -> int:
    for index, value in enumerate(levels):
        if x <= value + 6.0:
            return min(MAX_LIST_LEVEL, index)
    return min(MAX_LIST_LEVEL, max(0, len(levels) - 1))


def analyze_page(page: PageLines, dehyphenate: bool = True) -> list[Block]:
    """Ricostruisce la struttura logica di una pagina/slide."""
    lines = [line for line in page.lines if line.text.strip()]
    if not lines:
        return []
    body = body_font_size(lines)

    # Righe composte solo dal simbolo del punto elenco: si uniscono alla
    # riga successiva sulla stessa altezza (PowerPoint le esporta separate).
    merged: list[Line] = []
    pending_glyph: Line | None = None
    for line in lines:
        stripped = line.text.strip()
        if len(stripped) == 1 and (stripped in BULLET_GLYPHS or stripped in {"-", "–", "o"}):
            pending_glyph = line
            continue
        if pending_glyph is not None:
            if abs(pending_glyph.y0 - line.y0) <= max(4.0, line.height):
                line.text = f"• {line.text.strip()}"
                line.x0 = min(line.x0, pending_glyph.x0)
            pending_glyph = None
        merged.append(line)
    lines = merged
    if not lines:
        return []

    title_lines = _title_lines(lines, page, body)
    bullet_starts = [line.x0 for line in lines if _bullet_split(line.text)[0] and line not in title_lines]
    levels = _indent_levels(bullet_starts)

    blocks: list[Block] = []
    if title_lines:
        blocks.append(Block(BLOCK_HEADING, join_lines([l.text for l in title_lines], dehyphenate), level=1))

    current: Block | None = None
    current_lines: list[str] = []
    previous: Line | None = None
    text_x0 = 0.0

    def flush() -> None:
        nonlocal current, current_lines
        if current is not None and current_lines:
            current.text = join_lines(current_lines, dehyphenate)
            if current.text:
                blocks.append(current)
        current = None
        current_lines = []

    for line in lines:
        if line in title_lines:
            continue
        text = line.text.strip()
        if line.display_math:
            flush()
            formula = text.strip("$").strip()
            if blocks and blocks[-1].kind == BLOCK_MATH and previous is not None and previous.display_math \
                    and line.y0 - previous.y1 < line.height * 0.9:
                blocks[-1].text = f"{blocks[-1].text} \\\\ {formula}"
            elif formula:
                blocks.append(Block(BLOCK_MATH, formula))
            previous = line
            continue

        kind, content = _bullet_split(text)
        if kind is not None:
            flush()
            level = _level_for(line.x0, levels)
            current = Block(
                BLOCK_BULLET if kind == "bullet" else BLOCK_NUMBER,
                "",
                level=level,
                number=_number_value(text) if kind == "number" else None,
            )
            current_lines = [content]
            text_x0 = line.x0 + 0.45 * line.size
            previous = line
            continue

        is_heading = (
            line.size >= body * 1.18
            and len(text) <= 120
            and not text.endswith((".", ";", ","))
        )
        if is_heading:
            flush()
            if blocks and blocks[-1].kind == BLOCK_HEADING and previous is not None \
                    and abs(previous.size - line.size) < 0.6 and line.y0 - previous.y1 < line.height:
                blocks[-1].text = join_lines([blocks[-1].text, text], dehyphenate)
            else:
                blocks.append(Block(BLOCK_HEADING, text, level=2))
            previous = line
            continue

        gap = (line.y0 - previous.y1) if previous is not None else 0.0
        close = previous is not None and gap < max(line.height, previous.height) * 0.9
        if current is not None and current.kind in (BLOCK_BULLET, BLOCK_NUMBER):
            if close and line.x0 >= text_x0 and abs(line.size - previous.size) < 1.0:
                current_lines.append(text)
                previous = line
                continue
            flush()
        if current is not None and current.kind == BLOCK_PARAGRAPH:
            same_block = previous is not None and line.block_id == previous.block_id
            if (same_block or close) and abs(line.size - previous.size) < 1.0 and \
                    not _starts_new_paragraph(previous, line):
                current_lines.append(text)
                previous = line
                continue
            flush()
        current = Block(BLOCK_PARAGRAPH, "")
        current_lines = [text]
        previous = line
    flush()
    return blocks


def _starts_new_paragraph(previous: Line, line: Line) -> bool:
    gap = line.y0 - previous.y1
    if gap > max(line.height, previous.height) * 0.9:
        return True
    # Riga precedente breve che termina con punto: probabile fine paragrafo
    # in slide con righe indipendenti.
    return previous.text.rstrip().endswith((".", ":", "!", "?")) and previous.x1 < line.x1 - 60


def _title_lines(lines: list[Line], page: PageLines, body: float) -> list[Line]:
    """Righe del titolo: le prime righe nella parte alta con font piu' grande."""
    top_limit = page.height * 0.35 if page.height else float("inf")
    candidates = [line for line in lines if line.y0 <= top_limit and not line.display_math]
    if not candidates:
        return []
    largest = max(line.size for line in candidates)
    if largest < body * 1.2 and not (candidates[0].bold and candidates[0].size >= body * 1.05):
        return []
    target = largest if largest >= body * 1.2 else candidates[0].size
    title: list[Line] = []
    for line in lines:
        if line in title:
            continue
        if abs(line.size - target) < 0.8 and line.y0 <= top_limit and not _bullet_split(line.text)[0]:
            if title and line.y0 - title[-1].y1 > line.height * 1.2:
                break
            title.append(line)
        elif title:
            break
    joined = " ".join(line.text for line in title)
    if len(joined) > 160:
        return []
    return title
