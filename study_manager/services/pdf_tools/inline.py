"""Formattazione in linea del formato sorgente (Markdown + LaTeX).

Oltre a ``**grassetto**`` e ``*corsivo*`` il sorgente accetta direttamente i
comandi LaTeX piu' usati negli appunti:

- ``\\underline{testo}``       sottolineato
- ``\\texttt{testo}``          monospazio (codice)
- ``\\textcolor{red}{testo}``  testo colorato (pacchetto xcolor)
- ``\\textbf{}``, ``\\emph{}``/``\\textit{}`` come alternativa a ``**``/``*``

I comandi si possono annidare (``\\textcolor{blue}{\\underline{x}}``) e possono
contenere formule ``$...$``. ``parse_inline`` produce un piccolo albero che
ogni esportatore (LaTeX, TXT, Word, HTML, anteprima) visita a modo suo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Union

from .document import split_inline_math, unescape_text

STYLE_BOLD = "bold"
STYLE_ITALIC = "italic"
STYLE_UNDERLINE = "underline"
STYLE_MONO = "mono"
STYLE_COLOR = "color"

_COMMANDS = {
    "underline": STYLE_UNDERLINE,
    "texttt": STYLE_MONO,
    "textbf": STYLE_BOLD,
    "textit": STYLE_ITALIC,
    "emph": STYLE_ITALIC,
    "textcolor": STYLE_COLOR,
}
_COMMAND_RE = re.compile(r"\\(underline|texttt|textbf|textit|emph|textcolor)\s*(?=[\[{])")
_EMPHASIS_RE = re.compile(r"(\*\*[^*]+?\*\*|(?<![\w*])\*(?!\s)[^*]+?(?<!\s)\*(?![\w*]))")

# Colori proposti nel menu (nomi validi di xcolor) e resa nell'anteprima scura.
COLOR_CHOICES: dict[str, tuple[str, str]] = {
    "red": ("Rosso", "#F87171"),
    "orange": ("Arancione", "#FB923C"),
    "olive": ("Verde oliva", "#A3B34A"),
    "teal": ("Verde acqua", "#2DD4BF"),
    "blue": ("Blu", "#60A5FA"),
    "violet": ("Viola", "#A78BFA"),
    "magenta": ("Magenta", "#F472B6"),
    "brown": ("Marrone", "#C08457"),
    "gray": ("Grigio", "#9CA3AF"),
}
_EXTRA_PREVIEW_COLORS = {
    "green": "#4ADE80", "cyan": "#22D3EE", "yellow": "#FACC15", "purple": "#C084FC",
    "pink": "#F9A8D4", "lime": "#A3E635", "black": "#E5E7EB", "white": "#F9FAFB",
    "darkgray": "#6B7280", "lightgray": "#D1D5DB",
}


def preview_color(name: str) -> str | None:
    """Colore da usare nell'anteprima (``None`` se sconosciuto)."""
    name = name.strip()
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", name):
        return "#" + name.lstrip("#")
    base = re.split(r"[!]", name)[0].lower()
    if base in COLOR_CHOICES:
        return COLOR_CHOICES[base][1]
    return _EXTRA_PREVIEW_COLORS.get(base)


@dataclass
class Math:
    content: str


@dataclass
class Styled:
    style: str
    children: list["Node"] = field(default_factory=list)
    value: str = ""          # nome del colore per STYLE_COLOR
    model: str = ""          # modello opzionale, es. "HTML" in \textcolor[HTML]{FF0000}


Node = Union[str, Math, Styled]


def _read_group(text: str, start: int, open_char: str = "{", close_char: str = "}") -> tuple[str, int] | None:
    """Legge ``{...}`` bilanciato a partire da ``start``; restituisce (contenuto, fine)."""
    if start >= len(text) or text[start] != open_char:
        return None
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    return None


def _match_command(text: str, index: int) -> tuple[Styled, str, int] | None:
    match = _COMMAND_RE.match(text, index)
    if not match:
        return None
    name = match.group(1)
    position = match.end()
    model = ""
    color = ""
    if text.startswith("[", position):
        optional = _read_group(text, position, "[", "]")
        if optional is None or name != "textcolor":
            return None
        model, position = optional[0].strip(), optional[1]
    if name == "textcolor":
        group = _read_group(text, position)
        if group is None:
            return None
        color, position = group[0].strip(), group[1]
    group = _read_group(text, position)
    if group is None:
        return None
    content, end = group
    return Styled(_COMMANDS[name], value=color, model=model), content, end


def _plain_nodes(text: str) -> list[Node]:
    """Testo senza comandi: formule e grassetto/corsivo Markdown."""
    nodes: list[Node] = []
    for is_math, content in split_inline_math(text):
        if is_math:
            nodes.append(Math(content))
            continue
        for token in _EMPHASIS_RE.split(content):
            if not token:
                continue
            if token.startswith("**") and token.endswith("**") and len(token) > 4:
                nodes.append(Styled(STYLE_BOLD, parse_inline(token[2:-2])))
            elif token.startswith("*") and token.endswith("*") and len(token) > 2:
                nodes.append(Styled(STYLE_ITALIC, parse_inline(token[1:-1])))
            else:
                nodes.append(token)
    return nodes


def _math_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    position = 0
    for is_math, content in split_inline_math(text):
        # ricostruisce la lunghezza originale del segmento
        if is_math:
            for opener, closer in (("$", "$"), ("\\(", "\\)")):
                candidate = f"{opener}{content}{closer}"
                if text.startswith(candidate, position):
                    spans.append((position, position + len(candidate)))
                    position += len(candidate)
                    break
            else:
                found = text.find(content, position)
                if found == -1:
                    break
                position = found + len(content) + 1
        else:
            position += len(content)
    return spans


def parse_inline(text: str) -> list[Node]:
    nodes: list[Node] = []
    buffer_start = 0
    index = 0
    math = _math_spans(text)
    math_index = 0
    while index < len(text):
        while math_index < len(math) and math[math_index][1] <= index:
            math_index += 1
        if math_index < len(math) and math[math_index][0] <= index < math[math_index][1]:
            index = math[math_index][1]
            continue
        if text[index] == "\\":
            matched = _match_command(text, index)
            if matched is not None:
                node, content, end = matched
                if buffer_start < index:
                    nodes.extend(_plain_nodes(text[buffer_start:index]))
                node.children = parse_inline(content)
                nodes.append(node)
                index = buffer_start = end
                continue
            index += 2
            continue
        index += 1
    if buffer_start < len(text):
        nodes.extend(_plain_nodes(text[buffer_start:]))
    return nodes


def iter_runs(nodes: list[Node], styles: tuple = ()) -> Iterator[tuple[str, str, tuple]]:
    """Appiattisce l'albero in ``(tipo, contenuto, stili)``; tipo ``text`` o ``math``.

    ``stili`` e' una tupla di (stile, valore), es. (("bold", ""), ("color", "red")).
    """
    for node in nodes:
        if isinstance(node, str):
            yield "text", node, styles
        elif isinstance(node, Math):
            yield "math", node.content, styles
        else:
            yield from iter_runs(node.children, styles + ((node.style, node.value),))


def plain_text(nodes: list[Node], math_to_text=None) -> str:
    out = []
    for kind, content, _styles in iter_runs(nodes):
        if kind == "math":
            out.append(math_to_text(content) if math_to_text else f"${content}$")
        else:
            out.append(unescape_text(content))
    return "".join(out)


def strip_inline_styles(text: str) -> str:
    """Testo senza alcuna formattazione (le formule restano ``$...$``)."""
    return plain_text(parse_inline(text))


def uses_color(text: str) -> bool:
    return "\\textcolor" in text
