"""Modello a blocchi e formato sorgente Markdown delle trascrizioni PDF.

Il formato sorgente e' quello mostrato nell'editor: Markdown semplice con
marcatori di slide e formule LaTeX::

    === SLIDE 1 ===

    # Titolo della slide

    Paragrafo con formula inline $E = mc^{2}$.

    - punto elenco
      - punto annidato
    1. elenco numerato

    $$
    \\int_{0}^{1} x \\, dx = \\frac{1}{2}
    $$

Tutti gli esportatori (TXT, LaTeX, Word, HTML) partono da questo testo, cosi'
le correzioni manuali fatte nell'anteprima finiscono in ogni formato.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


BLOCK_HEADING = "heading"
BLOCK_PARAGRAPH = "paragraph"
BLOCK_BULLET = "bullet"
BLOCK_NUMBER = "number"
BLOCK_MATH = "math"
BLOCK_TABLE = "table"

LIST_KINDS = (BLOCK_BULLET, BLOCK_NUMBER)
MAX_LIST_LEVEL = 3

SLIDE_MARKER_RE = re.compile(r"^\s*=+\s*SLIDE\s+(\d+)\s*=+\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
NUMBER_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class Block:
    """Unita' di contenuto di una slide.

    ``text`` contiene testo Markdown con formule inline ``$...$``; per i
    blocchi ``math`` contiene solo il codice LaTeX, senza delimitatori.
    ``level`` e' il livello del titolo (1-3) o la profondita' dell'elenco (0-3).
    """

    kind: str
    text: str
    level: int = 0
    number: int | None = None


@dataclass
class Slide:
    number: int
    blocks: list[Block] = field(default_factory=list)
    engine: str = ""

    @property
    def title(self) -> str:
        for block in self.blocks:
            if block.kind == BLOCK_HEADING:
                return block.text
        return ""


@dataclass
class PdfDocument:
    source: Path
    title: str
    slides: list[Slide] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engines: dict[str, int] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.slides)


@dataclass
class RenderOptions:
    include_markers: bool = True
    include_headings: bool = True
    include_preamble: bool = True
    docx_page_breaks: bool = False
    title: str = ""
    language: str = "italian"


# ---------------------------------------------------------------------------
# Formule inline
# ---------------------------------------------------------------------------
def split_inline_math(text: str) -> list[tuple[bool, str]]:
    """Divide il testo in segmenti ``(is_math, contenuto)``.

    Riconosce ``$...$`` e ``\\(...\\)``; ``\\$`` e' un dollaro letterale. Un
    ``$`` senza chiusura resta testo normale. I segmenti di testo mantengono
    l'escape ``\\$`` cosi' che gli esportatori possano trattarlo.
    """
    parts: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length and text[index + 1] == "$":
            buffer.append("\\$")
            index += 2
            continue
        if char == "\\" and index + 1 < length and text[index + 1] == "(":
            end = text.find("\\)", index + 2)
            if end != -1:
                if buffer:
                    parts.append((False, "".join(buffer)))
                    buffer = []
                parts.append((True, text[index + 2 : end].strip()))
                index = end + 2
                continue
        if char == "$" and not text.startswith("$$", index):
            end = _find_closing_dollar(text, index + 1)
            if end != -1 and end > index + 1:
                if buffer:
                    parts.append((False, "".join(buffer)))
                    buffer = []
                parts.append((True, text[index + 1 : end].strip()))
                index = end + 1
                continue
        if text.startswith("$$", index):
            end = text.find("$$", index + 2)
            if end != -1:
                if buffer:
                    parts.append((False, "".join(buffer)))
                    buffer = []
                parts.append((True, text[index + 2 : end].strip()))
                index = end + 2
                continue
        buffer.append(char)
        index += 1
    if buffer:
        parts.append((False, "".join(buffer)))
    return [part for part in parts if part[1] or part[0]]


def _find_closing_dollar(text: str, start: int) -> int:
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "$":
            return index
        index += 1
    return -1


def unescape_text(text: str) -> str:
    """Rimuove gli escape Markdown usati nel formato sorgente."""
    return text.replace("\\$", "$")


def strip_inline_markup(text: str) -> str:
    """Toglie grassetto/corsivo Markdown lasciando il testo."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])__(.+?)__(?![\w_])", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Rendering del sorgente
# ---------------------------------------------------------------------------
def slide_marker(number: int) -> str:
    return f"=== SLIDE {number} ==="


def render_blocks(blocks: list[Block]) -> list[str]:
    """Blocchi -> righe Markdown (senza marcatore di slide)."""
    lines: list[str] = []
    previous_kind = ""
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        is_list = block.kind in LIST_KINDS
        if lines and not (is_list and previous_kind in LIST_KINDS):
            lines.append("")
        if block.kind == BLOCK_HEADING:
            lines.append(f"{'#' * max(1, min(3, block.level or 1))} {text}")
        elif block.kind == BLOCK_BULLET:
            lines.append(f"{'  ' * _level(block)}- {text}")
        elif block.kind == BLOCK_NUMBER:
            number = block.number if block.number is not None else 1
            lines.append(f"{'  ' * _level(block)}{number}. {text}")
        elif block.kind == BLOCK_MATH:
            lines.extend(["$$", text, "$$"])
        elif block.kind == BLOCK_TABLE:
            lines.extend(text.splitlines())
        else:
            lines.append(text)
        previous_kind = block.kind
    return lines


def _level(block: Block) -> int:
    return max(0, min(MAX_LIST_LEVEL, int(block.level or 0)))


def render_markdown(document: PdfDocument) -> str:
    """Documento estratto -> testo sorgente dell'editor (sempre con marcatori)."""
    chunks: list[str] = []
    for slide in document.slides:
        body = render_blocks(slide.blocks)
        chunk = [slide_marker(slide.number), ""]
        chunk.extend(body if body else ["_(slide senza testo)_"])
        chunks.append("\n".join(chunk))
    return "\n\n".join(chunks).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Parsing del sorgente
# ---------------------------------------------------------------------------
def parse_markdown(text: str) -> list[Slide]:
    """Testo dell'editor -> slide e blocchi.

    Il contenuto prima del primo marcatore diventa la slide 0 (se presente);
    senza alcun marcatore tutto il testo e' una sola slide numero 1.
    """
    text = COMMENT_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    slides: list[Slide] = []
    current = Slide(number=0)
    has_marker = False
    paragraph: list[str] = []
    table: list[str] = []
    math_lines: list[str] | None = None
    math_close = "$$"
    list_item: Block | None = None
    list_indent = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            current.blocks.append(Block(BLOCK_PARAGRAPH, join_lines(paragraph)))
            paragraph = []

    def flush_table() -> None:
        nonlocal table
        if table:
            current.blocks.append(Block(BLOCK_TABLE, "\n".join(table)))
            table = []

    def flush_all() -> None:
        nonlocal list_item
        flush_paragraph()
        flush_table()
        list_item = None

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if math_lines is not None:
            stripped = line.strip()
            if stripped.endswith(math_close):
                content = stripped[: -len(math_close)].strip()
                if content:
                    math_lines.append(content)
                current.blocks.append(Block(BLOCK_MATH, "\n".join(math_lines).strip()))
                math_lines = None
            else:
                math_lines.append(line)
            continue

        marker = SLIDE_MARKER_RE.match(line)
        if marker:
            flush_all()
            if current.blocks or has_marker:
                slides.append(current)
            current = Slide(number=int(marker.group(1)))
            has_marker = True
            continue

        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_table()
            list_item = None
            continue
        if stripped in {"_(slide senza testo)_"}:
            continue
        line_no_images = IMAGE_RE.sub("", line)
        if not line_no_images.strip():
            continue
        line = line_no_images
        stripped = line.strip()

        if stripped.startswith("$$") or stripped.startswith("\\["):
            flush_all()
            opener = "$$" if stripped.startswith("$$") else "\\["
            math_close = "$$" if opener == "$$" else "\\]"
            rest = stripped[len(opener) :]
            if rest.rstrip().endswith(math_close) and rest.strip() != "":
                content = rest.rstrip()[: -len(math_close)].strip()
                current.blocks.append(Block(BLOCK_MATH, content))
            else:
                math_lines = [rest] if rest.strip() else []
            continue

        heading = HEADING_RE.match(stripped)
        if heading and not line.startswith("    "):
            flush_all()
            current.blocks.append(
                Block(BLOCK_HEADING, heading.group(2).strip(), level=min(3, len(heading.group(1))))
            )
            continue

        if TABLE_RE.match(line):
            flush_paragraph()
            list_item = None
            table.append(stripped)
            continue

        bullet = BULLET_RE.match(line)
        number = NUMBER_RE.match(line)
        if bullet and not stripped.startswith("**"):
            flush_paragraph()
            flush_table()
            indent = len(bullet.group(1).expandtabs(4))
            list_item = Block(BLOCK_BULLET, bullet.group(2).strip(), level=_indent_level(indent))
            list_indent = indent
            current.blocks.append(list_item)
            continue
        if number:
            flush_paragraph()
            flush_table()
            indent = len(number.group(1).expandtabs(4))
            list_item = Block(
                BLOCK_NUMBER,
                number.group(3).strip(),
                level=_indent_level(indent),
                number=int(number.group(2)),
            )
            list_indent = indent
            current.blocks.append(list_item)
            continue

        if list_item is not None and len(line) - len(line.lstrip()) > list_indent:
            list_item.text = join_lines([list_item.text, stripped])
            continue
        list_item = None
        flush_table()
        paragraph.append(stripped)

    if math_lines is not None:
        current.blocks.append(Block(BLOCK_MATH, "\n".join(math_lines).strip()))
    flush_all()
    if has_marker:
        slides.append(current)
    elif current.blocks:
        current.number = 1
        slides.append(current)
    return [slide for slide in slides if slide.blocks or slide.number]


def _indent_level(indent: int) -> int:
    return max(0, min(MAX_LIST_LEVEL, indent // 2))


def join_lines(lines: list[str], dehyphenate: bool = True) -> str:
    """Unisce righe di uno stesso paragrafo correggendo la sillabazione.

    ``infor-`` + ``mazione`` diventa ``informazione`` solo se la parola
    successiva inizia in minuscolo: i trattini veri (``Rossi-Bianchi``)
    restano invariati.
    """
    result = ""
    for raw in lines:
        piece = raw.strip().replace("­", "")
        if not piece:
            continue
        if not result:
            result = piece
            continue
        if (
            dehyphenate
            and len(result) >= 2
            and result[-1] in "-‐"
            and result[-2].isalpha()
            and piece[:1].islower()
        ):
            result = result[:-1] + piece
        else:
            result = f"{result} {piece}"
    return re.sub(r"[ \t]{2,}", " ", result)
