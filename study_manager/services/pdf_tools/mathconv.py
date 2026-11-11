"""Conversione delle formule tra Unicode (testo estratto dal PDF) e LaTeX.

I PDF esportati da PowerPoint o Word contengono le formule come caratteri
Unicode (``α``, ``≤``, ``x²``); quelli prodotti da LaTeX usano font matematici
(Computer Modern ``CMMI``/``CMSY``). Questo modulo:

- traduce i simboli Unicode nei comandi LaTeX equivalenti;
- individua nel testo le sequenze che sono formule e le racchiude in ``$...$``;
- normalizza i delimitatori prodotti da Nougat (``\\(``, ``\\[``);
- per l'esportazione .txt rende leggibile il LaTeX (``\\alpha`` -> ``α``).
"""

from __future__ import annotations

import re


GREEK: dict[str, str] = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\varepsilon",
    "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "ϑ": r"\vartheta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi",
    "π": r"\pi", "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma",
    "ς": r"\varsigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi", "ϕ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma",
    "Υ": r"\Upsilon", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega", "µ": r"\mu",
}

# Simboli che indicano con certezza una formula.
STRONG_SYMBOLS: dict[str, str] = {
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx", "≡": r"\equiv",
    "≅": r"\cong", "≃": r"\simeq", "∝": r"\propto", "≪": r"\ll", "≫": r"\gg",
    "±": r"\pm", "∓": r"\mp", "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla",
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint",
    "∮": r"\oint", "√": r"\sqrt", "∈": r"\in", "∉": r"\notin", "∋": r"\ni",
    "⊂": r"\subset", "⊆": r"\subseteq", "⊃": r"\supset", "⊇": r"\supseteq",
    "⊄": r"\not\subset", "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset",
    "∀": r"\forall", "∃": r"\exists", "∄": r"\nexists", "¬": r"\neg",
    "∧": r"\wedge", "∨": r"\vee", "⊕": r"\oplus", "⊗": r"\otimes", "⊥": r"\perp",
    "∥": r"\parallel", "∠": r"\angle", "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}",
    "ℤ": r"\mathbb{Z}", "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "ℏ": r"\hbar",
    "ℓ": r"\ell", "∖": r"\setminus", "⊢": r"\vdash", "⊨": r"\models",
    "∴": r"\therefore", "∵": r"\because", "⟨": r"\langle", "⟩": r"\rangle",
    "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil",
    "∘": r"\circ", "†": r"\dagger", "∗": r"\ast", "⋯": r"\cdots", "⋮": r"\vdots",
    "∼": r"\sim", "≺": r"\prec", "≻": r"\succ", "⊄": r"\not\subset",
}

# Simboli frequenti anche nella prosa delle slide (frecce, puntini, segno per):
# diventano LaTeX solo all'interno di una formula gia' riconosciuta.
WEAK_SYMBOLS: dict[str, str] = {
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow", "⇒": r"\Rightarrow",
    "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow", "↦": r"\mapsto", "⟶": r"\longrightarrow",
    "⟹": r"\Longrightarrow", "⟺": r"\Longleftrightarrow", "↑": r"\uparrow",
    "↓": r"\downarrow", "×": r"\times", "÷": r"\div", "·": r"\cdot", "⋅": r"\cdot",
    "−": "-", "–": "-", "…": r"\ldots", "′": "'", "″": "''", "‖": r"\|",
    "°": r"^{\circ}",
}

FRACTIONS: dict[str, str] = {
    "½": r"\frac{1}{2}", "⅓": r"\frac{1}{3}", "⅔": r"\frac{2}{3}", "¼": r"\frac{1}{4}",
    "¾": r"\frac{3}{4}", "⅕": r"\frac{1}{5}", "⅙": r"\frac{1}{6}", "⅛": r"\frac{1}{8}",
}

SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7",
    "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(", "⁾": ")", "ⁿ": "n",
    "ⁱ": "i", "ᵃ": "a", "ᵇ": "b", "ᶜ": "c", "ᵈ": "d", "ᵉ": "e", "ᵏ": "k", "ᵐ": "m",
    "ᵗ": "t", "ˣ": "x", "ʸ": "y", "ᵀ": "T",
}
SUBSCRIPTS = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7",
    "₈": "8", "₉": "9", "₊": "+", "₋": "-", "₌": "=", "₍": "(", "₎": ")", "ₐ": "a",
    "ₑ": "e", "ₒ": "o", "ₓ": "x", "ₕ": "h", "ₖ": "k", "ₗ": "l", "ₘ": "m", "ₙ": "n",
    "ₚ": "p", "ₛ": "s", "ₜ": "t", "ᵢ": "i", "ⱼ": "j", "ᵣ": "r", "ᵤ": "u", "ᵥ": "v",
}

ALL_SYMBOLS: dict[str, str] = {**GREEK, **STRONG_SYMBOLS, **WEAK_SYMBOLS, **FRACTIONS}
STRONG_CHARS = set(GREEK) | set(STRONG_SYMBOLS) | set(SUPERSCRIPTS) | set(SUBSCRIPTS)
WEAK_CHARS = set(WEAK_SYMBOLS)
ASCII_OPERATORS = {"=", "+", "-", "<", ">", "/", "*", "^", "|", ":="}

# Font matematici: Computer Modern/AMS (LaTeX), Cambria Math (Word),
# Symbol/MT Extra (PowerPoint), STIX e Latin Modern Math.
MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|CMBSY|CMMIB|MSAM|MSBM|EUFM|EUSM|RSFS|LMMATH|Math|Symbol|STIX|"
    r"MTExtra|MT Extra|MTPro|Euclid|Mathematica|TeXGyre.*Math|XITS)",
    re.IGNORECASE,
)

_VARIABLE_RE = re.compile(r"^[A-Za-z0-9().,\[\]{}'|_^]+$")
_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+$")


def is_math_font(font_name: str) -> bool:
    return bool(font_name) and bool(MATH_FONT_RE.search(font_name))


def has_strong_math(text: str) -> bool:
    return any(char in STRONG_CHARS for char in text)


def math_char_count(text: str) -> int:
    return sum(1 for char in text if char in STRONG_CHARS)


# ---------------------------------------------------------------------------
# Unicode -> LaTeX
# ---------------------------------------------------------------------------
def unicode_math_to_latex(text: str) -> str:
    """Traduce una formula Unicode in LaTeX (``x² ≤ α`` -> ``x^{2} \\leq \\alpha``)."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in SUPERSCRIPTS or char in SUBSCRIPTS:
            table = SUPERSCRIPTS if char in SUPERSCRIPTS else SUBSCRIPTS
            marker = "^" if table is SUPERSCRIPTS else "_"
            run = []
            while index < length and text[index] in table:
                run.append(table[text[index]])
                index += 1
            out.append(f"{marker}{{{''.join(run)}}}")
            continue
        if char == "√":
            index += 1
            argument, index = _sqrt_argument(text, index)
            out.append(rf"\sqrt{{{unicode_math_to_latex(argument)}}}" if argument else r"\sqrt{}")
            continue
        replacement = ALL_SYMBOLS.get(char)
        if replacement is not None:
            out.append(replacement)
            following = text[index + 1] if index + 1 < length else ""
            if _LATEX_COMMAND_RE.search(replacement) and following.isalpha():
                out.append(" ")
            index += 1
            continue
        out.append(char)
        index += 1
    result = "".join(out)
    return re.sub(r"[ ]{2,}", " ", result).strip()


def _sqrt_argument(text: str, index: int) -> tuple[str, int]:
    while index < len(text) and text[index] == " ":
        index += 1
    if index >= len(text):
        return "", index
    if text[index] in "({[":
        closing = {"(": ")", "{": "}", "[": "]"}[text[index]]
        depth = 0
        start = index
        while index < len(text):
            if text[index] == text[start]:
                depth += 1
            elif text[index] == closing:
                depth -= 1
                if depth == 0:
                    inner = text[start + 1 : index]
                    return inner, index + 1
            index += 1
        return text[start + 1 :], len(text)
    start = index
    while index < len(text) and (text[index].isalnum() or text[index] in SUPERSCRIPTS):
        index += 1
    return text[start:index], index


# ---------------------------------------------------------------------------
# Riconoscimento delle formule in una riga di testo
# ---------------------------------------------------------------------------
STOPWORDS = {
    "a", "e", "i", "o", "il", "lo", "la", "le", "gli", "di", "da", "in", "con", "su", "per", "tra",
    "fra", "un", "se", "si", "ne", "ci", "ed", "ad", "od", "al", "del", "dal", "nel", "sul", "is",
    "of", "to", "an", "as", "at", "be", "by", "if", "or", "on", "it", "we", "no", "non", "che",
}


def _token_kind(token: str) -> str:
    core = token.strip(",;:.!?")
    if not core:
        return "W"
    if core in STOPWORDS or (len(core) > 1 and core.lower() in STOPWORDS):
        return "W"
    if any(char in STRONG_CHARS for char in core):
        return "S"
    if all(char in FRACTIONS or char.isdigit() or char in ".," for char in core):
        return "V"
    if core in ASCII_OPERATORS or (len(core) == 1 and core in WEAK_CHARS):
        return "O"
    if all(char in WEAK_CHARS or char in "=+-<>/*" for char in core):
        return "O"
    if _VARIABLE_RE.match(core):
        letters = sum(1 for char in core if char.isalpha())
        if letters <= 2 or any(char in "()[]{}^_|" for char in core) or core.isdigit():
            return "V"
    return "W"


def wrap_unicode_math(text: str) -> str:
    """Racchiude in ``$...$`` le formule Unicode presenti in un testo.

    Una formula parte da un token con simboli "forti" (greco, ≤, ², ∑, ...)
    e si estende ai token vicini collegati da operatori (``f(x) = x² + 1``).
    Le frecce e il segno ``·`` da soli non bastano: nelle slide compaiono
    spesso come punteggiatura (``Studio ⇒ risultati``).
    """
    if not text or not (has_strong_math(text)):
        return escape_dollars(text)
    tokens = re.split(r"(\s+)", text)
    words = [(i, tok) for i, tok in enumerate(tokens) if tok and not tok.isspace()]
    kinds = [_token_kind(tok) for _, tok in words]
    in_math = [False] * len(words)
    for pos, kind in enumerate(kinds):
        if kind != "S":
            continue
        in_math[pos] = True
        _extend(kinds, in_math, pos, step=1)
        _extend(kinds, in_math, pos, step=-1)

    pieces: list[str] = []
    pos = 0
    while pos < len(words):
        if not in_math[pos]:
            pieces.append(escape_dollars(words[pos][1]))
            pos += 1
            continue
        end = pos
        while end + 1 < len(words) and in_math[end + 1]:
            end += 1
        run = [tok for _, tok in words[pos : end + 1]]
        trailing = ""
        last = run[-1]
        stripped_last = last.rstrip(",;:.!?")
        if stripped_last != last and stripped_last:
            trailing = last[len(stripped_last) :]
            run[-1] = stripped_last
        leading = ""
        first = run[0]
        if first.startswith("(") and first.count("(") > first.count(")"):
            leading = "("
            run[0] = first[1:]
        latex = unicode_math_to_latex(" ".join(run))
        pieces.append(f"{leading}${latex}${trailing}" if latex else leading + trailing)
        pos = end + 1
    return " ".join(piece for piece in pieces if piece)


def _extend(kinds: list[str], in_math: list[bool], pos: int, step: int) -> None:
    cursor = pos + step
    chain = 0
    while 0 <= cursor < len(kinds):
        kind = kinds[cursor]
        if kind == "S":
            in_math[cursor] = True
            chain = 0
            cursor += step
            continue
        if kind == "O":
            nxt = cursor + step
            if 0 <= nxt < len(kinds) and kinds[nxt] in {"V", "S"}:
                in_math[cursor] = True
                in_math[nxt] = True
                chain = 0
                cursor = nxt + step
                continue
            return
        if kind == "V" and kinds[cursor - step] in {"S", "V"} and chain < 3:
            # Prodotti senza operatore: "½ m v²", "∑ i".
            in_math[cursor] = True
            chain += 1
            cursor += step
            continue
        return


def escape_dollars(text: str) -> str:
    return re.sub(r"(?<!\\)\$", r"\\$", text)


# ---------------------------------------------------------------------------
# Normalizzazione dell'output di Marker / Nougat
# ---------------------------------------------------------------------------
def normalize_math_delimiters(markdown: str) -> str:
    """``\\[...\\]`` -> ``$$...$$`` e ``\\(...\\)`` -> ``$...$`` (formato Nougat)."""
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: "\n$$\n" + m.group(1).strip() + "\n$$\n", markdown, flags=re.DOTALL)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1).strip() + "$", text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# LaTeX -> testo leggibile (esportazione .txt)
# ---------------------------------------------------------------------------
_LATEX_TO_UNICODE = {value: key for key, value in {**GREEK, **STRONG_SYMBOLS, **WEAK_SYMBOLS}.items()
                     if value.startswith("\\") and " " not in value}
_LATEX_TO_UNICODE.update({
    r"\le": "≤", r"\ge": "≥", r"\ne": "≠", r"\epsilon": "ε", r"\varepsilon": "ε",
    r"\phi": "φ", r"\varphi": "φ", r"\cdot": "·", r"\times": "×", r"\to": "→",
    r"\rightarrow": "→", r"\ldots": "…", r"\dots": "…", r"\cdots": "⋯", r"\mu": "μ",
    r"\quad": " ", r"\qquad": "  ", r"\,": " ", r"\;": " ", r"\!": "", r"\ ": " ",
    r"\mathbb{R}": "ℝ", r"\mathbb{N}": "ℕ", r"\mathbb{Z}": "ℤ", r"\mathbb{Q}": "ℚ",
    r"\mathbb{C}": "ℂ", r"\|": "‖", r"\lbrace": "{", r"\rbrace": "}",
})
_LATEX_TO_UNICODE.pop(r"\sqrt", None)
_SUP_REVERSE = {v: k for k, v in SUPERSCRIPTS.items() if v.isdigit() or v in "+-=()ni"}
_SUB_REVERSE = {v: k for k, v in SUBSCRIPTS.items()}


_ACCENT_UNICODE = {"vec": "\u20d7", "hat": "\u0302", "bar": "\u0304", "tilde": "\u0303",
                   "dot": "\u0307", "ddot": "\u0308", "overline": "\u0305"}
_TEXT_FUNCTIONS = ("arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "lim", "sup", "inf", "max", "min",
                   "sin", "cos", "tan", "cot", "log", "ln", "exp", "det", "arg", "dim", "ker", "gcd", "mod")


def latex_to_unicode(latex: str) -> str:
    """Rende una formula LaTeX leggibile come testo semplice (best effort)."""
    text = latex
    text = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)l?r?([()\[\]|.]|\\[{}|])", r"\1", text)
    text = text.replace("\\{", "\x01").replace("\\}", "\x02")
    text = re.sub(r"\\(begin|end)\{[a-z*]+\}", "", text)
    text = text.replace("&", " ").replace("\\\\", "; ")
    text = re.sub(r"\\(" + "|".join(_TEXT_FUNCTIONS) + r")(?![A-Za-z])", r"\1 ", text)
    for command in sorted(_LATEX_TO_UNICODE, key=len, reverse=True):
        if command.startswith("\\") and command[1:].isalpha():
            text = re.sub(re.escape(command) + r"(?![A-Za-z])", _LATEX_TO_UNICODE[command], text)
        elif command.startswith("\\mathbb"):
            continue
        else:
            text = text.replace(command, _LATEX_TO_UNICODE[command])
    for _ in range(8):
        previous = text
        for command in ("mathrm", "mathbf", "mathit", "text", "textrm", "operatorname", "mathsf",
                        "boldsymbol", "mathcal", "mathscr", "mathfrak"):
            text = re.sub(r"\\" + command + r"\s*\{([^{}]*)\}", r"\1", text)
        text = re.sub(r"\\mathbb\s*\{([^{}]*)\}",
                      lambda m: "".join({"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}.get(c, c) for c in m.group(1)),
                      text)
        text = re.sub(r"\\(vec|hat|bar|tilde|dot|ddot|overline)\s*\{([^{}]*)\}",
                      lambda m: m.group(2) + _ACCENT_UNICODE[m.group(1)] if len(m.group(2)) == 1 else m.group(2),
                      text)
        text = re.sub(r"\^\{([^{}]*)\}", lambda m: _script(m.group(1), _SUP_REVERSE, "^"), text)
        text = re.sub(r"_\{([^{}]*)\}", lambda m: _script(m.group(1), _SUB_REVERSE, "_"), text)
        text = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
                      lambda m: f"{_group(m.group(1))}/{_group(m.group(2))}", text)
        text = re.sub(r"\\sqrt\s*\{([^{}]*)\}", lambda m: f"√{_group(m.group(1))}", text)
        if text == previous:
            break
    text = re.sub(r"\^([0-9A-Za-z+\-])", lambda m: _script(m.group(1), _SUP_REVERSE, "^"), text)
    text = re.sub(r"_([0-9A-Za-z])", lambda m: _script(m.group(1), _SUB_REVERSE, "_"), text)
    text = text.replace("{", "").replace("}", "").replace("\x01", "{").replace("\x02", "}")
    return re.sub(r"[ ]{2,}", " ", text).strip()


def _group(value: str) -> str:
    value = value.strip()
    if len(value) <= 1 or re.fullmatch(r"[A-Za-z0-9.]+", value):
        return value
    return f"({value})"


def _script(value: str, table: dict[str, str], marker: str) -> str:
    compact = value.replace(" ", "")
    if compact and all(char in table for char in compact):
        return "".join(table[char] for char in compact)
    return f"{marker}({value.strip()})" if len(compact) > 1 else f"{marker}{compact}"


def _replace_command_arg(text: str, command: str, transform) -> str:
    pattern = re.compile(r"\\" + command + r"\s*\{([^{}]*)\}")
    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(lambda m: transform(m.group(1)), text)
    return text
