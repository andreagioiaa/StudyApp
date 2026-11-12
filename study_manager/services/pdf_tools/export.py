"""Esportazione del testo sorgente in .txt, .tex, .docx e anteprima HTML.

Il sorgente puo' essere Markdown (formato predefinito dell'editor) oppure
LaTeX, se l'utente ha scelto di modificare direttamente il codice LaTeX.

- ``.txt``: testo pulito suddiviso per slide; le formule diventano leggibili
  (``\\alpha^{2}`` -> ``α²``) quando possibile.
- ``.tex``: solo corpo oppure documento completo compilabile (``article`` con
  ``amsmath``/``amssymb``). Le formule display usano ``\\[ ... \\]``, la forma
  raccomandata da amsmath (equivalente a ``$$ ... $$``).
- ``.docx``: Pandoc locale. Le formule ``$...$`` diventano equazioni native di
  Word (OMML); titoli ed elenchi usano gli stili Word (Titolo 1/2, Elenco).
- ``.html``: anteprima con il bundle KaTeX incluso nell'app, aperta nel browser
  predefinito senza connessione.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ...paths import app_data_dir, resource_path
from ...utils import atomic_write_text
from .inline import (
    STYLE_BOLD,
    STYLE_COLOR,
    STYLE_ITALIC,
    STYLE_MONO,
    STYLE_UNDERLINE,
    Math,
    Node,
    parse_inline,
    plain_text,
    preview_color,
    uses_color,
)
from .document import (
    BLOCK_BULLET,
    BLOCK_HEADING,
    BLOCK_MATH,
    BLOCK_NUMBER,
    BLOCK_TABLE,
    LIST_KINDS,
    RenderOptions,
    Slide,
    parse_markdown,
    split_inline_math,
    unescape_text,
)
from .mathconv import ALL_SYMBOLS, latex_to_unicode


class ExportError(RuntimeError):
    """Errore di esportazione mostrabile all'utente."""


FORMAT_TXT = "txt"
FORMAT_TEX = "tex"
FORMAT_DOCX = "docx"
EXPORT_FORMATS = (FORMAT_TXT, FORMAT_TEX, FORMAT_DOCX)

SOURCE_MARKDOWN = "markdown"
SOURCE_LATEX = "latex"


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------
_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEXT_SYMBOLS = {
    "•": r"\textbullet{}",
    "…": r"\ldots{}",
    "€": r"\texteuro{}",
    "™": r"\texttrademark{}",
    "©": r"\textcopyright{}",
    "®": r"\textregistered{}",
}


def latex_escape(text: str) -> str:
    """Escape del testo normale per LaTeX (pdflatex con utf8)."""
    out: list[str] = []
    for char in text:
        if char in _LATEX_SPECIALS:
            out.append(_LATEX_SPECIALS[char])
        elif char in _TEXT_SYMBOLS:
            out.append(_TEXT_SYMBOLS[char])
        elif char in ALL_SYMBOLS and char not in "–−°·":
            command = ALL_SYMBOLS[char]
            out.append(rf"\ensuremath{{{command}}}")
        elif char == "−":
            out.append("-")
        elif char == "·":
            out.append(r"\textperiodcentered{}")
        elif char == "°":
            out.append(r"\ensuremath{^{\circ}}")
        else:
            out.append(char)
    return "".join(out)


_LATEX_COMMANDS = {
    STYLE_BOLD: "textbf",
    STYLE_ITALIC: "emph",
    STYLE_UNDERLINE: "underline",
    STYLE_MONO: "texttt",
}


def _nodes_to_latex(nodes: list[Node]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(latex_escape(unescape_text(node)))
        elif isinstance(node, Math):
            out.append(f"${node.content}$")
        elif node.style == STYLE_COLOR:
            model = f"[{node.model}]" if node.model else ""
            out.append(f"\\textcolor{model}{{{node.value}}}{{{_nodes_to_latex(node.children)}}}")
        else:
            out.append(f"\\{_LATEX_COMMANDS[node.style]}{{{_nodes_to_latex(node.children)}}}")
    return "".join(out)


def inline_to_latex(text: str) -> str:
    return _nodes_to_latex(parse_inline(text))


def _table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip().strip("|")
        if re.fullmatch(r"[\s|:\-]+", stripped or "-"):
            continue
        rows.append([cell.strip() for cell in stripped.split("|")])
    return rows


def _list_env(kind: str) -> str:
    return "itemize" if kind == BLOCK_BULLET else "enumerate"


# ---------------------------------------------------------------------------
# Struttura delle sezioni
# ---------------------------------------------------------------------------
SECTION_LEVELS = ("part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph")
SECTION_LEVEL_LABELS = {
    "part": "\\part",
    "chapter": "\\chapter",
    "section": "\\section",
    "subsection": "\\subsection",
    "subsubsection": "\\subsubsection",
    "paragraph": "\\paragraph",
}
CONTENT_MARKER = "% STUDYFLOW:CONTENUTO"
TITLE_PLACEHOLDER = "{{TITOLO}}"


def _level_index(name: str) -> int:
    return SECTION_LEVELS.index(name) if name in SECTION_LEVELS else SECTION_LEVELS.index("section")


def first_heading_index(options: RenderOptions | None) -> int:
    """Indice in ``SECTION_LEVELS`` del comando usato per i titoli ``#``.

    Documento autonomo: ``#`` -> \\section. Inglobazione: la radice usa
    ``base_level`` e i titoli partono dal livello subito sotto.
    """
    if options is None or not options.embed:
        return _level_index("section")
    return min(_level_index(options.base_level) + 1, len(SECTION_LEVELS) - 1)


def heading_command(markdown_level: int, options: RenderOptions) -> str:
    index = min(first_heading_index(options) + max(1, markdown_level) - 1, len(SECTION_LEVELS) - 1)
    star = "" if options.numbered_sections else "*"
    return f"{SECTION_LEVELS[index]}{star}"


def latex_root_heading(options: RenderOptions) -> str:
    """Sezione radice dell'inglobazione (es. ``\\section{Nome documento}``)."""
    if not options.embed:
        return ""
    title = (options.root_title or options.title).strip()
    if not title:
        return ""
    star = "" if options.numbered_sections else "*"
    return f"\\{_level_name(options.base_level)}{star}{{{latex_escape(title)}}}\n\n"


def _level_name(name: str) -> str:
    return name if name in SECTION_LEVELS else "section"


# Numerazione come la produce LaTeX (classe article/report, secnumdepth 3).
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
_NUMBERED_DEPTH = SECTION_LEVELS.index("subsubsection")
_HEADING_SCAN_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)(\*?)\s*(?:\[[^\]]*\])?\s*\{"
)


def _roman(value: int) -> str:
    out = []
    for number, letters in _ROMAN:
        while value >= number:
            out.append(letters)
            value -= number
    return "".join(out)


class SectionNumbering:
    """Contatori delle sezioni, con le stesse regole di LaTeX.

    ``\\chapter`` azzera ``section``, ``section`` azzera ``subsection`` e cosi'
    via; ``\\part`` non azzera i livelli sotto. ``paragraph`` e ``subparagraph``
    non sono numerati (come in article con secnumdepth predefinito).
    """

    def __init__(self, language: str = "italian"):
        self.counts = [0] * len(SECTION_LEVELS)
        self.has_chapter = False
        self.italian = (language or "italian").startswith("ital")

    def step(self, level: str, starred: bool = False) -> str:
        """Avanza il contatore del livello e restituisce l'etichetta ('' se non numerata)."""
        index = SECTION_LEVELS.index(level) if level in SECTION_LEVELS else SECTION_LEVELS.index("section")
        if starred or index > _NUMBERED_DEPTH:
            return ""
        self.counts[index] += 1
        if index > 0:  # \part non azzera i livelli sotto
            for lower in range(index + 1, len(SECTION_LEVELS)):
                self.counts[lower] = 0
        if index == 0:
            return f"{'Parte' if self.italian else 'Part'} {_roman(self.counts[0])}"
        if index == 1:
            self.has_chapter = True
            return f"{'Capitolo' if self.italian else 'Chapter'} {self.counts[1]}"
        first = 1 if self.has_chapter else 2
        return ".".join(str(self.counts[i]) for i in range(first, index + 1))

    def scan(self, latex: str) -> None:
        """Avanza i contatori per le sezioni gia' presenti in un testo LaTeX (commenti esclusi)."""
        for line in latex.split("\n"):
            code = re.sub(r"(?<!\\)%.*$", "", line)
            for match in _HEADING_SCAN_RE.finditer(code):
                self.step(match.group(1), starred=bool(match.group(2)))


def template_prefix(template: str) -> str:
    """Parte del template che precede il punto in cui verra' inserito il contenuto."""
    text = template.replace("\r\n", "\n")
    begin = text.find("\\begin{document}")
    if begin == -1:
        return ""
    for line_start, line in _iter_lines(text):
        if line.strip() == CONTENT_MARKER:
            return text[begin:line_start]
    end = text.rfind("\\end{document}")
    return text[begin:end if end != -1 else None]


def _iter_lines(text: str):
    position = 0
    for line in text.split("\n"):
        yield position, line
        position += len(line) + 1


def numbering_for(options: RenderOptions) -> SectionNumbering:
    """Contatori pronti per l'anteprima: se il documento autonomo usa un template,
    si parte dopo le sezioni gia' presenti nel template (altrimenti da 1)."""
    numbering = SectionNumbering(options.language)
    if options.template.strip() and options.include_preamble and not options.embed:
        numbering.scan(template_prefix(options.template))
    return numbering


def heading_level_name(markdown_level: int, options: RenderOptions) -> str:
    return heading_command(markdown_level, options).rstrip("*")


def slides_to_latex_body(slides: list[Slide], options: RenderOptions) -> str:
    lines: list[str] = []
    for slide_index, slide in enumerate(slides):
        if slide_index:
            lines.append("")
        if options.include_markers and slide.number:
            lines.append(f"% === Slide {slide.number} ===")
        stack: list[tuple[str, int]] = []

        def close_lists(target_level: int = -1, kind: str | None = None) -> None:
            while stack and (
                stack[-1][1] > target_level
                or (stack[-1][1] == target_level and kind is not None and stack[-1][0] != kind)
            ):
                env_kind, _env_level = stack.pop()
                lines.append(f"{'  ' * len(stack)}\\end{{{_list_env(env_kind)}}}")

        for block in slide.blocks:
            if block.kind in LIST_KINDS:
                level = max(0, min(3, block.level))
                close_lists(level, block.kind)
                if not stack or stack[-1][1] < level:
                    depth = len(stack)
                    lines.append(f"{'  ' * depth}\\begin{{{_list_env(block.kind)}}}")
                    if block.kind == BLOCK_NUMBER and block.number and block.number > 1:
                        counter = ("enumi", "enumii", "enumiii", "enumiv")[min(3, depth)]
                        lines.append(f"{'  ' * depth}  \\setcounter{{{counter}}}{{{block.number - 1}}}")
                    stack.append((block.kind, level))
                lines.append(f"{'  ' * len(stack)}\\item {inline_to_latex(block.text)}")
                continue
            close_lists()
            if block.kind == BLOCK_HEADING:
                if not options.include_headings:
                    continue
                command = heading_command(min(block.level, 3), options)
                lines.append(f"\\{command}{{{inline_to_latex(block.text)}}}")
            elif block.kind == BLOCK_MATH:
                formula = block.text.strip()
                if "\\\\" in formula and "\\begin" not in formula:
                    lines.extend(["\\begin{gather*}", formula, "\\end{gather*}"])
                else:
                    lines.extend(["\\[", formula, "\\]"])
            elif block.kind == BLOCK_TABLE:
                rows = _table_rows(block.text)
                if not rows:
                    continue
                columns = max(len(row) for row in rows)
                lines.append("\\begin{center}")
                lines.append(f"\\begin{{tabular}}{{|{'l|' * columns}}}")
                lines.append("\\hline")
                for row in rows:
                    cells = [inline_to_latex(cell) for cell in row] + [""] * (columns - len(row))
                    lines.append(" & ".join(cells) + r" \\ \hline")
                lines.append("\\end{tabular}")
                lines.append("\\end{center}")
            else:
                lines.append(inline_to_latex(block.text))
            lines.append("")
        close_lists()
        while lines and lines[-1] == "":
            lines.pop()
    return "\n".join(lines).strip() + "\n"


LATEX_PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[%(language)s]{babel}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{amsfonts}
\usepackage{textcomp}
\usepackage{xcolor}
\usepackage[margin=2.5cm]{geometry}
\usepackage[hidelinks]{hyperref}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.6em}
"""


_REQUIRED_PACKAGES = ("amsmath", "amssymb")


def _template_uses_package(template: str, package: str) -> bool:
    for match in re.finditer(r"^[^%\n]*\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", template, re.MULTILINE):
        if package in [item.strip() for item in match.group(1).split(",")]:
            return True
    return False


def apply_template(template: str, body: str, options: RenderOptions) -> str:
    """Inserisce ``body`` in un template .tex dell'utente.

    - il contenuto va al posto della riga ``% STUDYFLOW:CONTENUTO`` se presente,
      altrimenti subito prima di ``\\end{document}``;
    - ``{{TITOLO}}`` e un ``\\title{}`` vuoto vengono riempiti con il titolo;
    - se mancano, ``amsmath`` e ``amssymb`` (servono alle formule) vengono
      aggiunti prima di ``\\begin{document}``.
    """
    text = template.replace("\r\n", "\n")
    if "\\begin{document}" not in text:
        raise ExportError("Il template non contiene \\begin{document}.")
    title = latex_escape(options.title.strip()) if options.title else ""
    text = text.replace(TITLE_PLACEHOLDER, title)
    if title:
        text = re.sub(r"\\title\{\s*\}", lambda _m: f"\\title{{{title}}}", text, count=1)
    missing = [pkg for pkg in _REQUIRED_PACKAGES if not _template_uses_package(text, pkg)]
    if uses_color(body) and not any(
        _template_uses_package(text, pkg) for pkg in ("xcolor", "color", "tcolorbox", "tikz", "pgf")
    ):
        missing.append("xcolor")
    if missing:
        extra = "".join(f"\\usepackage{{{pkg}}} % aggiunto da StudyFlow\n" for pkg in missing)
        index = text.find("\\begin{document}")
        text = text[:index] + extra + text[index:]
    content = body.strip() + "\n"
    lines = text.split("\n")
    for position, line in enumerate(lines):
        if line.strip() == CONTENT_MARKER:
            lines[position] = content.rstrip("\n")
            return "\n".join(lines).rstrip() + "\n"
    end = text.rfind("\\end{document}")
    if end == -1:
        return text.rstrip() + "\n\n" + content + "\n\\end{document}\n"
    head = text[:end].rstrip()
    return head + "\n\n" + content + "\n" + text[end:].rstrip() + "\n"


def finalize_latex(body: str, options: RenderOptions, full: bool | None = None, root: bool = True) -> str:
    """Aggiunge la sezione radice (inglobazione) e, se serve, preambolo o template."""
    if root:
        body = latex_root_heading(options) + body
    if full is None:
        full = options.include_preamble and not options.embed
    return wrap_latex_document(body, options) if full else body


def wrap_latex_document(body: str, options: RenderOptions) -> str:
    if "\\documentclass" in body:
        return body
    if options.template.strip():
        return apply_template(options.template, body, options)
    preamble = LATEX_PREAMBLE % {"language": options.language or "italian"}
    parts = [preamble]
    if options.title:
        parts.append(f"\\title{{{latex_escape(options.title)}}}\n\\date{{}}\n")
    parts.append("\\begin{document}\n")
    if options.title:
        parts.append("\\maketitle\n")
    parts.append("\n" + body.strip() + "\n\n\\end{document}\n")
    return "".join(parts)


def markdown_to_latex(
    markdown: str, options: RenderOptions, full: bool | None = None, root: bool = True
) -> str:
    """Markdown -> LaTeX. ``root=False`` omette la sezione radice (editor LaTeX)."""
    body = slides_to_latex_body(parse_markdown(markdown), options)
    return finalize_latex(body, options, full, root)


# ---------------------------------------------------------------------------
# LaTeX -> Markdown (per modifiche fatte direttamente nel codice LaTeX)
# ---------------------------------------------------------------------------
_LATEX_UNESCAPE = (
    (r"\textbackslash{}", "\\"),
    (r"\textasciitilde{}", "~"),
    (r"\textasciicircum{}", "^"),
    (r"\textbullet{}", "•"),
    (r"\textperiodcentered{}", "·"),
    (r"\ldots{}", "…"),
    (r"\texteuro{}", "€"),
    (r"\&", "&"),
    (r"\%", "%"),
    (r"\#", "#"),
    (r"\_", "_"),
    (r"\{", "{"),
    (r"\}", "}"),
)
_DISPLAY_ENVS = {
    "equation": None, "equation*": None, "displaymath": None,
    "align": "aligned", "align*": "aligned", "eqnarray": "aligned", "eqnarray*": "aligned",
    "gather": "gathered", "gather*": "gathered", "multline": None, "multline*": None,
}
_SECTION_RE = re.compile(
    r"^\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\{(.*)\}\s*$"
)
_ITEM_RE = re.compile(r"^\\item(?:\[[^\]]*\])?\s*(.*)$")
_DROP_RE = re.compile(
    r"^\\(maketitle|tableofcontents|centering|noindent|newpage|clearpage|pagebreak|hline|"
    r"vspace\*?\{[^}]*\}|hspace\*?\{[^}]*\}|medskip|bigskip|smallskip|begin\{center\}|end\{center\}|"
    r"begin\{flushleft\}|end\{flushleft\})\s*$"
)


def _latex_text_to_markdown(text: str) -> str:
    text = re.sub(r"\\ensuremath\{((?:[^{}]|\{[^{}]*\})*)\}", r"$\1$", text)
    parts: list[str] = []
    for is_math, content in split_inline_math(text):
        if is_math:
            parts.append(f"${content}$")
            continue
        segment = content
        for _ in range(3):
            segment = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", segment)
            segment = re.sub(r"\\(?:emph|textit)\{([^{}]*)\}", r"*\1*", segment)
            segment = re.sub(r"\\(?:textrm|textsf|mbox|text)\{([^{}]*)\}", r"\1", segment)
        segment = segment.replace("\\$", "\x00")
        for source, target in _LATEX_UNESCAPE:
            segment = segment.replace(source, target)
        segment = re.sub(r"\\\\(\[[^\]]*\])?\s*$", "", segment)
        segment = segment.replace("~", " ").replace("``", "“").replace("''", "”")
        parts.append(segment.replace("\x00", "\\$"))
    return "".join(parts).strip()


def latex_to_markdown(tex: str, options: RenderOptions | None = None) -> str:
    """Converte LaTeX (anche generato da questo modulo) nel formato sorgente."""
    text = tex.replace("\r\n", "\n")
    begin = text.find("\\begin{document}")
    if begin != -1:
        end = text.find("\\end{document}", begin)
        text = text[begin + len("\\begin{document}") : end if end != -1 else None]
    out: list[str] = []
    stack: list[list] = []  # [kind, counter]
    math_env: str | None = None
    math_wrap: str | None = None
    math_lines: list[str] = []
    table_rows: list[str] | None = None

    for raw in text.split("\n"):
        marker = re.match(r"^\s*%\s*=+\s*Slide\s+(\d+)\s*=+\s*$", raw, re.IGNORECASE)
        if marker:
            out.extend(["", f"=== SLIDE {marker.group(1)} ===", ""])
            continue
        line = re.sub(r"(?<!\\)%.*$", "", raw).strip()
        if math_env is not None:
            closing = "\\]" if math_env == "[" else f"\\end{{{math_env}}}"
            if line.endswith(closing) or line == "$$" and math_env == "$$":
                content = line[: -len(closing)].strip() if math_env != "$$" else ""
                if content:
                    math_lines.append(content)
                formula = "\n".join(math_lines).strip()
                formula = re.sub(r"\\(label|nonumber|notag)(\{[^}]*\})?", "", formula).strip()
                if math_wrap:
                    formula = f"\\begin{{{math_wrap}}}\n{formula}\n\\end{{{math_wrap}}}"
                out.extend(["", "$$", formula, "$$", ""])
                math_env = None
                math_lines = []
            else:
                math_lines.append(line)
            continue
        if not line:
            if table_rows is None:
                out.append("")
            continue
        if line.startswith("\\begin{tabular}"):
            table_rows = []
            continue
        if table_rows is not None:
            if line.startswith("\\end{tabular}"):
                rows = [row for row in table_rows if row]
                if rows:
                    out.append("")
                    for index, row in enumerate(rows):
                        out.append(row)
                        if index == 0:
                            out.append("|" + "---|" * row.count("|", 1))
                    out.append("")
                table_rows = None
                continue
            cleaned = line.replace("\\hline", "").strip()
            cleaned = re.sub(r"\\\\\s*$", "", cleaned).strip()
            if cleaned:
                cells = [_latex_text_to_markdown(cell.strip()) for cell in re.split(r"(?<!\\)&", cleaned)]
                table_rows.append("| " + " | ".join(cells) + " |")
            continue
        if line.startswith("\\[") or line == "$$":
            rest = line[2:].strip()
            closing = "\\]" if line.startswith("\\[") else "$$"
            if rest.endswith(closing):
                out.extend(["", "$$", rest[: -len(closing)].strip(), "$$", ""])
            else:
                math_env = "[" if closing == "\\]" else "$$"
                math_wrap = None
                math_lines = [rest] if rest else []
            continue
        env = re.match(r"^\\begin\{([a-zA-Z*]+)\}(.*)$", line)
        if env and env.group(1) in _DISPLAY_ENVS:
            math_env = env.group(1)
            math_wrap = _DISPLAY_ENVS[env.group(1)]
            math_lines = [env.group(2).strip()] if env.group(2).strip() else []
            continue
        if re.match(r"^\\begin\{(itemize|enumerate)\}", line):
            stack.append(["bullet" if "itemize" in line else "number", 0])
            continue
        if re.match(r"^\\end\{(itemize|enumerate)\}", line):
            if stack:
                stack.pop()
            if not stack:
                out.append("")
            continue
        counter = re.match(r"^\\setcounter\{enum[iv]+\}\{(\d+)\}", line)
        if counter and stack:
            stack[-1][1] = int(counter.group(1))
            continue
        item = _ITEM_RE.match(line)
        if item and stack:
            depth = len(stack) - 1
            kind = stack[-1]
            if kind[0] == "bullet":
                out.append(f"{'  ' * depth}- {_latex_text_to_markdown(item.group(1))}")
            else:
                kind[1] += 1
                out.append(f"{'  ' * depth}{kind[1]}. {_latex_text_to_markdown(item.group(1))}")
            continue
        section = _SECTION_RE.match(line)
        if section:
            depth = SECTION_LEVELS.index(section.group(1)) - first_heading_index(options) + 1
            level = "#" * depth if 1 <= depth <= 3 else None
            title = _latex_text_to_markdown(section.group(2))
            out.extend(["", f"{level} {title}" if level else f"**{title}**", ""])
            continue
        if _DROP_RE.match(line) or re.match(r"^\\(title|author|date)\{.*\}$", line):
            continue
        converted = _latex_text_to_markdown(line)
        if stack and out and out[-1].strip():
            out[-1] = f"{out[-1]} {converted}"
        else:
            out.append(converted)
    markdown = "\n".join(out)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return markdown + "\n"


# ---------------------------------------------------------------------------
# Testo semplice
# ---------------------------------------------------------------------------
def _inline_to_plain(text: str) -> str:
    return re.sub(r"[ ]{2,}", " ", plain_text(parse_inline(text), latex_to_unicode)).strip()


def slides_to_txt(slides: list[Slide], options: RenderOptions) -> str:
    chunks: list[str] = []
    for slide in slides:
        lines: list[str] = []
        if options.include_markers and slide.number:
            lines.extend([f"=== SLIDE {slide.number} ===", ""])
        previous_list = False
        for block in slide.blocks:
            is_list = block.kind in LIST_KINDS
            if lines and lines[-1] != "" and not (is_list and previous_list):
                lines.append("")
            if block.kind == BLOCK_HEADING:
                if not options.include_headings:
                    continue
                lines.append(_inline_to_plain(block.text))
            elif block.kind == BLOCK_BULLET:
                lines.append(f"{'  ' * block.level}- {_inline_to_plain(block.text)}")
            elif block.kind == BLOCK_NUMBER:
                lines.append(f"{'  ' * block.level}{block.number or 1}. {_inline_to_plain(block.text)}")
            elif block.kind == BLOCK_MATH:
                for formula_line in block.text.split("\\\\"):
                    lines.append(f"    {latex_to_unicode(formula_line)}")
            elif block.kind == BLOCK_TABLE:
                for row in _table_rows(block.text):
                    lines.append(" | ".join(_inline_to_plain(cell) for cell in row))
            else:
                lines.append(_inline_to_plain(block.text))
            previous_list = is_list
        while lines and lines[-1] == "":
            lines.pop()
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n\n".join(chunks).strip() + "\n"


def markdown_to_txt(markdown: str, options: RenderOptions) -> str:
    return slides_to_txt(parse_markdown(markdown), options)


# ---------------------------------------------------------------------------
# Pandoc / Word
# ---------------------------------------------------------------------------
def _pandoc_escape(text: str) -> str:
    text = unescape_text(text).replace("\\", "\\\\")
    for char in "$<>^~`[]*_#@":
        text = text.replace(char, "\\" + char)
    return text


def _nodes_to_pandoc(nodes: list[Node]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(_pandoc_escape(node))
        elif isinstance(node, Math):
            if node.content.strip():
                out.append(f"${node.content.strip()}$")
        elif node.style == STYLE_BOLD:
            out.append(f"**{_nodes_to_pandoc(node.children)}**")
        elif node.style == STYLE_ITALIC:
            out.append(f"*{_nodes_to_pandoc(node.children)}*")
        elif node.style == STYLE_UNDERLINE:
            out.append(f"[{_nodes_to_pandoc(node.children)}]{{.underline}}")
        elif node.style == STYLE_MONO:
            code = plain_text(node.children, latex_to_unicode)
            fence = "``" if "`" in code else "`"
            pad = " " if code.startswith("`") or code.endswith("`") else ""
            out.append(f"{fence}{pad}{code}{pad}{fence}")
        else:  # colore: Word tramite Pandoc non lo supporta, resta il testo
            out.append(_nodes_to_pandoc(node.children))
    return "".join(out)


def _pandoc_inline(text: str) -> str:
    return _nodes_to_pandoc(parse_inline(text))


PAGE_BREAK_OPENXML = '```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```'


def slides_to_pandoc_markdown(slides: list[Slide], options: RenderOptions) -> str:
    out: list[str] = []
    for index, slide in enumerate(slides):
        if index and options.docx_page_breaks:
            out.extend([PAGE_BREAK_OPENXML, ""])
        if options.include_markers and slide.number:
            out.extend(['::: {custom-style="Slide Marker"}', f"Slide {slide.number}", ":::", ""])
        previous_list = False
        for block in slide.blocks:
            is_list = block.kind in LIST_KINDS
            if previous_list and not is_list:
                out.append("")
            if block.kind == BLOCK_HEADING:
                if options.include_headings:
                    out.extend([f"{'#' * max(1, min(3, block.level))} {_pandoc_inline(block.text)}", ""])
            elif block.kind == BLOCK_BULLET:
                out.append(f"{'    ' * block.level}- {_pandoc_inline(block.text)}")
            elif block.kind == BLOCK_NUMBER:
                out.append(f"{'    ' * block.level}{block.number or 1}. {_pandoc_inline(block.text)}")
            elif block.kind == BLOCK_MATH:
                formula = block.text.strip()
                if "\\\\" in formula and "\\begin" not in formula:
                    formula = f"\\begin{{gathered}}{formula}\\end{{gathered}}"
                out.extend([f"$${formula}$$", ""])
            elif block.kind == BLOCK_TABLE:
                rows = _table_rows(block.text)
                if rows:
                    columns = max(len(row) for row in rows)
                    for row_index, row in enumerate(rows):
                        cells = [_pandoc_inline(cell) for cell in row] + [""] * (columns - len(row))
                        out.append("| " + " | ".join(cells) + " |")
                        if row_index == 0:
                            out.append("|" + "---|" * columns)
                    out.append("")
            else:
                out.extend([_pandoc_inline(block.text), ""])
            previous_list = is_list
        out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def find_pandoc() -> str | None:
    """Pandoc locale: variabile d'ambiente, cartella dati, pypandoc-binary o PATH."""
    exe = "pandoc.exe" if os.name == "nt" else "pandoc"
    candidates = [os.environ.get("STUDYFLOW_PANDOC", ""), str(app_data_dir() / exe)]
    if getattr(sys, "frozen", False):
        # build.ps1 copia pandoc.exe accanto a StudyFlow.exe (non dentro l'exe
        # one-file, che altrimenti andrebbe estratto a ogni avvio).
        candidates.append(str(Path(sys.executable).resolve().parent / exe))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    try:
        import pypandoc  # type: ignore[import-not-found]

        bundled = pypandoc.get_pandoc_path()
        if bundled and Path(bundled).is_file():
            return str(bundled)
    except Exception:
        pass
    return shutil.which("pandoc")


def pandoc_version(command: str | None = None) -> str:
    command = command or find_pandoc()
    if not command:
        return ""
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (result.stdout or "").splitlines()[:1]
    return first[0].strip() if first else ""


def reference_docx() -> Path | None:
    """Modello Word facoltativo con stili personalizzati (cartella dati)."""
    candidate = app_data_dir() / "pdf_reference.docx"
    return candidate if candidate.is_file() else None


def export_docx(source: str, source_format: str, target: str | Path, options: RenderOptions) -> Path:
    command = find_pandoc()
    if not command:
        raise ExportError(
            "Pandoc non trovato. Installa le dipendenze (pypandoc-binary) oppure copia "
            "pandoc.exe nella cartella dati di StudyFlow."
        )
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studyflow-docx-") as temp:
        if source_format == SOURCE_LATEX:
            input_path = Path(temp) / "input.tex"
            input_path.write_text(source, encoding="utf-8")
            reader = "latex"
        else:
            input_path = Path(temp) / "input.md"
            input_path.write_text(slides_to_pandoc_markdown(parse_markdown(source), options), encoding="utf-8")
            reader = "markdown+tex_math_dollars+raw_attribute+fenced_divs+pipe_tables"
        temp_output = Path(temp) / "output.docx"
        args = [command, "-f", reader, "-t", "docx", "-o", str(temp_output), str(input_path)]
        if options.title:
            args[1:1] = ["-M", f"title={options.title}"]
        args[1:1] = ["-M", "lang=it-IT"]
        reference = reference_docx()
        if reference is not None:
            args[1:1] = ["--reference-doc", str(reference)]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExportError("Pandoc non ha terminato la conversione entro 5 minuti.") from exc
        except OSError as exc:
            raise ExportError(f"Impossibile avviare Pandoc: {exc}") from exc
        if result.returncode != 0 or not temp_output.is_file():
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise ExportError(f"Pandoc ha restituito un errore: {detail[-1] if detail else result.returncode}")
        shutil.copyfile(temp_output, target_path)
    return target_path


# ---------------------------------------------------------------------------
# Esportazione generica
# ---------------------------------------------------------------------------
def render_source(source: str, source_format: str, fmt: str, options: RenderOptions) -> str:
    """Testo finale di un formato testuale (txt/tex) a partire dal sorgente."""
    if source_format == SOURCE_LATEX:
        if fmt == FORMAT_TEX:
            return finalize_latex(source.strip() + "\n", options)
        return markdown_to_txt(latex_to_markdown(source, options), options)
    if fmt == FORMAT_TEX:
        return markdown_to_latex(source, options)
    return markdown_to_txt(source, options)


def export_file(source: str, source_format: str, fmt: str, target: str | Path, options: RenderOptions) -> Path:
    target_path = Path(target)
    if fmt == FORMAT_DOCX:
        return export_docx(source, source_format, target_path, options)
    if fmt not in (FORMAT_TXT, FORMAT_TEX):
        raise ExportError(f"Formato non supportato: {fmt}")
    atomic_write_text(target_path, render_source(source, source_format, fmt, options))
    return target_path


# ---------------------------------------------------------------------------
# Anteprima HTML con KaTeX locale
# ---------------------------------------------------------------------------
def katex_dir() -> Path:
    return resource_path("assets/katex")


def katex_available() -> bool:
    folder = katex_dir()
    return (folder / "katex.min.js").is_file() and (folder / "contrib" / "auto-render.min.js").is_file()


_HTML_TAGS = {STYLE_BOLD: "strong", STYLE_ITALIC: "em", STYLE_UNDERLINE: "u", STYLE_MONO: "code"}


def _nodes_to_html(nodes: list[Node]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            escaped = html.escape(unescape_text(node), quote=False)
            out.append(escaped.replace("$", '<span class="dollar">$</span>'))
        elif isinstance(node, Math):
            out.append(f"${html.escape(node.content, quote=False)}$")
        elif node.style == STYLE_COLOR:
            color = (f"#{node.value}" if node.model.upper() == "HTML" else preview_color(node.value)) or "inherit"
            out.append(f'<span style="color:{html.escape(color)}">{_nodes_to_html(node.children)}</span>')
        else:
            tag = _HTML_TAGS[node.style]
            out.append(f"<{tag}>{_nodes_to_html(node.children)}</{tag}>")
    return "".join(out)


def _html_inline(text: str) -> str:
    return _nodes_to_html(parse_inline(text))


def slides_to_html(slides: list[Slide], options: RenderOptions) -> str:
    folder = katex_dir()
    css = (folder / "katex.min.css").as_uri()
    js = (folder / "katex.min.js").as_uri()
    auto = (folder / "contrib" / "auto-render.min.js").as_uri()
    body: list[str] = []
    for slide in slides:
        body.append('<section class="slide">')
        if options.include_markers and slide.number:
            body.append(f'<div class="marker">Slide {slide.number}</div>')
        stack: list[tuple[str, int]] = []

        def close(level: int = -1, kind: str | None = None) -> None:
            while stack and (stack[-1][1] > level or (stack[-1][1] == level and kind and stack[-1][0] != kind)):
                env, _ = stack.pop()
                body.append("</ul>" if env == BLOCK_BULLET else "</ol>")

        for block in slide.blocks:
            if block.kind in LIST_KINDS:
                close(block.level, block.kind)
                if not stack or stack[-1][1] < block.level:
                    if block.kind == BLOCK_BULLET:
                        body.append("<ul>")
                    else:
                        body.append(f'<ol start="{block.number or 1}">')
                    stack.append((block.kind, block.level))
                body.append(f"<li>{_html_inline(block.text)}</li>")
                continue
            close()
            if block.kind == BLOCK_HEADING:
                if options.include_headings:
                    level = max(1, min(3, block.level))
                    body.append(f"<h{level}>{_html_inline(block.text)}</h{level}>")
            elif block.kind == BLOCK_MATH:
                body.append(f'<div class="display">$${html.escape(block.text, quote=False)}$$</div>')
            elif block.kind == BLOCK_TABLE:
                rows = _table_rows(block.text)
                body.append("<table>")
                for index, row in enumerate(rows):
                    tag = "th" if index == 0 else "td"
                    body.append("<tr>" + "".join(f"<{tag}>{_html_inline(cell)}</{tag}>" for cell in row) + "</tr>")
                body.append("</table>")
            else:
                body.append(f"<p>{_html_inline(block.text)}</p>")
        close()
        body.append("</section>")
    title = html.escape(options.title or "Anteprima StudyFlow")
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="{css}">
<script defer src="{js}"></script>
<script defer src="{auto}" onload="renderMathInElement(document.body, {{
  delimiters: [
    {{left: '$$', right: '$$', display: true}},
    {{left: '\\\\[', right: '\\\\]', display: true}},
    {{left: '$', right: '$', display: false}},
    {{left: '\\\\(', right: '\\\\)', display: false}}
  ],
  ignoredClasses: ['dollar'],
  throwOnError: false
}});"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: "Segoe UI", system-ui, sans-serif; max-width: 860px; margin: 32px auto;
         padding: 0 24px; line-height: 1.55; background: #0B1120; color: #F1F5F9; }}
  .slide {{ background: #111C31; border: 1px solid #24344F; border-radius: 14px;
           padding: 18px 26px; margin-bottom: 18px; }}
  .marker {{ font-size: 11px; font-weight: 700; letter-spacing: .08em; color: #22D3A7;
            text-transform: uppercase; }}
  h1, h2, h3 {{ margin: 8px 0 10px; }}
  h1 {{ font-size: 24px; }} h2 {{ font-size: 19px; color: #CBD5E1; }} h3 {{ font-size: 16px; }}
  .display {{ overflow-x: auto; margin: 10px 0; }}
  table {{ border-collapse: collapse; margin: 10px 0; }}
  td, th {{ border: 1px solid #24344F; padding: 4px 10px; }}
  @media print {{ body {{ background: #fff; color: #000; }} .slide {{ background: #fff; border-color: #ccc; }} }}
</style>
</head>
<body>
<h1>{title}</h1>
{chr(10).join(body)}
</body>
</html>
"""


def source_to_html(source: str, source_format: str, options: RenderOptions) -> str:
    markdown = latex_to_markdown(source, options) if source_format == SOURCE_LATEX else source
    return slides_to_html(parse_markdown(markdown), options)


def write_html_preview(source: str, source_format: str, options: RenderOptions) -> Path:
    if not katex_available():
        raise ExportError("Il bundle KaTeX locale (assets/katex) non e' presente.")
    target = app_data_dir() / "pdf_preview" / "anteprima.html"
    atomic_write_text(target, source_to_html(source, source_format, options))
    return target
