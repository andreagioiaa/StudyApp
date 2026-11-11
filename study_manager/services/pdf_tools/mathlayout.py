"""Ricostruzione geometrica delle formule dai glifi posizionati del PDF.

Nei PDF prodotti da LaTeX una formula non e' una stringa: e' un insieme di
glifi in font diversi, con apici e pedici rimpiccioliti e spostati, frazioni
con numeratore sopra e denominatore sotto la riga di base, simboli grandi
(``∫``, ``∑``) con i limiti sopra/sotto. PyMuPDF restituisce questi pezzi come
span separati, spesso su "righe" diverse.

Qui ogni span diventa un ``Atom`` con posizione e dimensione; l'algoritmo:

1. stabilisce la riga di base della formula (posizione piu' frequente dei
   glifi di dimensione normale);
2. assegna ai simboli grandi i glifi sovrapposti orizzontalmente come limiti;
3. raggruppa i glifi fuori dalla base: sopra + sotto sovrapposti = frazione,
   solo sopra = apice, solo sotto = pedice (con un livello di annidamento);
4. converte i simboli Unicode e i nomi di funzione (``lim``, ``sin``) in LaTeX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .mathconv import STRONG_CHARS, is_math_font, unicode_math_to_latex


BIG_OPERATORS = {"∫": r"\int", "∑": r"\sum", "∏": r"\prod", "∮": r"\oint", "⋃": r"\bigcup",
                 "⋂": r"\bigcap", "∬": r"\iint", "∭": r"\iiint", "⨁": r"\bigoplus"}
# Codifica del font Computer Modern Extension (senza ToUnicode i glifi
# arrivano come lettere ASCII).
CMEX_MAP = {
    "P": r"\sum", "X": r"\sum", "Q": r"\prod", "Y": r"\prod", "R": r"\int", "Z": r"\int",
    "H": r"\oint", "I": r"\oint", "S": r"\bigcup", "[": r"\bigcup", "T": r"\bigcap",
    "\\": r"\bigcap", "L": r"\bigoplus", "M": r"\bigoplus", "p": r"\sqrt", "q": r"\sqrt",
    "r": r"\sqrt", "s": r"\sqrt",
}
CMEX_DELIMITERS = {"\x00": "(", "\x01": ")", "\x02": "[", "\x03": "]", "\x08": r"\{", "\x09": r"\}",
                   "\x10": "(", "\x11": ")", "\x12": "(", "\x13": ")", "\x14": "[", "\x15": "]",
                   "0": "(", "1": ")", "@": "(", "A": ")", "(": r"\{", ")": r"\}",
                   "j": "|", "k": r"\|", "\x0c": "|"}
FUNCTION_NAMES = (
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "lim", "liminf", "limsup", "sup", "inf",
    "max", "min", "sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "det", "arg",
    "deg", "dim", "ker", "gcd", "lcm", "Pr", "mod",
)
_FUNCTION_RE = re.compile(r"(?<![\\A-Za-z])(" + "|".join(sorted(FUNCTION_NAMES, key=len, reverse=True)) + r")(?![A-Za-z])")
RELATION_SYMBOLS = {"=", "<", ">", "≤", "≥", "≠", "≈", "≡", "∼", "≃", "≅", "→", "⇒", "⇔", "∈", ":=", "∝"}
GLUE_RE = re.compile(r"^(?![\s.,;:!]*$)[\s=+\-−–()\[\],.0-9<>/|*:;!']*$")
_ITALIC_VARIABLE_RE = re.compile(r"^[A-Za-z]{1,2}'*$")
_COMMAND_END_RE = re.compile(r"\\[A-Za-z]+$")


@dataclass(eq=False)
class Atom:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    origin_y: float
    size: float
    font: str = ""
    flags: int = 0
    math: bool = False
    bigop: str | None = None
    role: str = "base"  # base | up | down

    @property
    def stripped(self) -> str:
        return self.text.strip()


def atom_from_span(span: dict[str, Any]) -> Atom:
    x0, y0, x1, y1 = (float(value) for value in span.get("bbox", (0, 0, 0, 0)))
    origin = span.get("origin") or (x0, y1)
    font = str(span.get("font", ""))
    text = str(span.get("text", ""))
    atom = Atom(
        text=text,
        x0=x0,
        x1=x1,
        y0=y0,
        y1=y1,
        origin_y=float(origin[1]),
        size=float(span.get("size", 11.0)) or 11.0,
        font=font,
        flags=int(span.get("flags", 0)),
    )
    stripped = atom.stripped
    if "CMEX" in font.upper() and stripped:
        mapped = "".join(CMEX_MAP.get(char, CMEX_DELIMITERS.get(char, "")) for char in stripped)
        atom.text = mapped
        atom.math = True
        if stripped in CMEX_MAP and CMEX_MAP[stripped] != r"\sqrt":
            atom.bigop = CMEX_MAP[stripped]
    elif stripped in BIG_OPERATORS:
        atom.bigop = BIG_OPERATORS[stripped]
        atom.math = True
    return atom


def is_math_atom(atom: Atom, bullet_glyphs: set[str]) -> bool:
    stripped = atom.stripped
    if not stripped or stripped in bullet_glyphs:
        return False
    if atom.math or atom.bigop:
        return True
    if is_math_font(atom.font):
        return True
    italic = bool(atom.flags & 2) or "italic" in atom.font.lower() or atom.font.upper().startswith(("CMSSI", "CMTI"))
    if italic and _ITALIC_VARIABLE_RE.match(stripped):
        return True
    # Simboli Unicode in un font di testo: lo span e' una formula solo se non
    # contiene parole (altrimenti decide il riconoscimento a livello di token).
    return (
        any(char in STRONG_CHARS for char in stripped)
        and len(stripped) <= 12
        and not re.search(r"[A-Za-zÀ-ÿ]{3,}", stripped)
    )


def dominant_baseline(atoms: list[Atom]) -> tuple[float, float]:
    """Riga di base e dimensione "normale" di un gruppo di glifi."""
    candidates = [atom for atom in atoms if atom.stripped and not atom.bigop]
    if not candidates:
        candidates = [atom for atom in atoms if atom.stripped] or atoms
    if not candidates:
        return 0.0, 11.0
    size = max(atom.size for atom in candidates)
    normal = [atom for atom in candidates if atom.size >= size * 0.92]
    relations = [
        atom for atom in normal
        if len(atom.stripped) <= 4 and any(symbol in atom.stripped for symbol in RELATION_SYMBOLS)
    ]
    if relations:
        values = sorted(atom.origin_y for atom in relations)
        return values[len(values) // 2], size
    bigops = [atom for atom in atoms if atom.bigop]
    if bigops:
        # L'asse matematico (centro dei simboli grandi) sta circa un quarto di
        # corpo sopra la riga di base.
        target = (bigops[0].y0 + bigops[0].y1) / 2 + 0.25 * size
        return min((atom.origin_y for atom in normal), key=lambda value: abs(value - target)), size
    weights: dict[float, int] = {}
    for atom in normal:
        key = round(atom.origin_y, 0)
        weights[key] = weights.get(key, 0) + max(1, len(atom.stripped))
    baseline = max(weights.items(), key=lambda item: (item[1], -abs(item[0])))[0]
    exact = [atom.origin_y for atom in normal if round(atom.origin_y, 0) == baseline]
    return (sum(exact) / len(exact) if exact else baseline), size


def classify(atoms: list[Atom], base_y: float, base_size: float) -> None:
    for atom in atoms:
        if atom.bigop or not atom.stripped or "CMEX" in atom.font.upper() or atom.text == r"\sqrt":
            atom.role = "base"
            continue
        delta = atom.origin_y - base_y
        small = atom.size < base_size * 0.85
        if small:
            if delta < -0.12 * base_size:
                atom.role = "up"
            elif delta > 0.08 * base_size:
                atom.role = "down"
            else:
                atom.role = "base"
        elif delta < -0.3 * base_size:
            atom.role = "up"
        elif delta > 0.3 * base_size:
            atom.role = "down"
        else:
            atom.role = "base"
        # Il flag "superscript" di PyMuPDF e' euristico e inaffidabile: conta
        # solo la geometria.
        if atom.role != "base":
            atom.math = True


def _join(parts: list[str]) -> str:
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out and (_COMMAND_END_RE.search(out) and part[:1].isalpha()):
            out += " "
        elif out and out[-1:].isalnum() and part[:1].isalpha() and len(part) > 1 and not part.startswith("\\"):
            out += " "
        out += part
    return out


def _atom_latex(atom: Atom) -> str:
    if atom.bigop:
        return atom.bigop
    font = atom.font.upper()
    if "CMEX" in font:
        return atom.text
    stripped = atom.stripped
    alphabet = None
    if "MSBM" in font or "BBOLD" in font or "DSROM" in font:
        alphabet = "mathbb"
    elif "EUFM" in font or "EUFB" in font:
        alphabet = "mathfrak"
    elif "RSFS" in font:
        alphabet = "mathscr"
    elif ("CMSY" in font or "CMBSY" in font) and stripped.isupper():
        alphabet = "mathcal"
    if alphabet and stripped.isalpha() and stripped.isascii():
        return "".join(rf"\{alphabet}{{{char}}}" for char in stripped)
    return unicode_math_to_latex(atom.text)


def _script_group(atoms: list[Atom], base_size: float) -> str:
    """Contenuto di apice/pedice, con un livello di annidamento."""
    atoms = sorted(atoms, key=lambda atom: atom.x0)
    main_size = max(atom.size for atom in atoms)
    main = [atom for atom in atoms if atom.size >= main_size * 0.92]
    main_y = sum(atom.origin_y for atom in main) / len(main)
    parts: list[str] = []
    for atom in atoms:
        latex = _atom_latex(atom)
        if atom.size < main_size * 0.85:
            delta = atom.origin_y - main_y
            if delta > 0.08 * base_size:
                parts.append(f"_{{{latex}}}")
                continue
            if delta < -0.1 * base_size:
                parts.append(f"^{{{latex}}}")
                continue
        parts.append(latex)
    return _join(parts)


def atoms_to_latex(atoms: list[Atom], base_y: float | None = None, base_size: float | None = None) -> str:
    """Glifi di una formula -> codice LaTeX."""
    atoms = [atom for atom in atoms if atom.text]
    if not atoms:
        return ""
    if base_y is None or base_size is None:
        base_y, base_size = dominant_baseline(atoms)
    classify(atoms, base_y, base_size)
    atoms = _apply_accents(atoms, base_size)

    # Limiti dei simboli grandi: glifi fuori base sovrapposti al simbolo o
    # subito alla sua destra (come per gli integrali in stile display).
    limits: dict[int, dict[str, list[Atom]]] = {}
    assigned: set[int] = set()
    for index, op in enumerate(atoms):
        if not op.bigop:
            continue
        reach = op.x1 + 0.8 * base_size if op.bigop in (r"\int", r"\oint", r"\iint", r"\iiint") else op.x1 + 0.2 * base_size
        for other_index, atom in enumerate(atoms):
            if other_index in assigned or atom.role == "base" or not atom.stripped:
                continue
            if atom.x1 >= op.x0 - 0.3 * base_size and atom.x0 <= reach:
                limits.setdefault(index, {"up": [], "down": []})[atom.role].append(atom)
                assigned.add(other_index)

    remaining = [(index, atom) for index, atom in enumerate(atoms) if index not in assigned]
    remaining.sort(key=lambda item: (item[1].x0, item[0]))
    parts: list[str] = []
    position = 0
    previous_base: Atom | None = None
    while position < len(remaining):
        index, atom = remaining[position]
        if atom.role == "base" or not atom.stripped:
            latex = _atom_latex(atom)
            if index in limits:
                group = limits[index]
                if group["down"]:
                    latex += f"_{{{_script_group(group['down'], base_size)}}}"
                if group["up"]:
                    latex += f"^{{{_script_group(group['up'], base_size)}}}"
            if atom.bigop == r"\sqrt" or latex == r"\sqrt":
                latex = r"\sqrt"
            parts.append(latex if atom.stripped else " ")
            if atom.stripped:
                previous_base = atom
            position += 1
            continue
        cluster = [atom]
        right = atom.x1
        cursor = position + 1
        while cursor < len(remaining):
            _, candidate = remaining[cursor]
            if candidate.role == "base" and candidate.stripped:
                break
            if candidate.stripped and candidate.x0 > right + 0.35 * base_size:
                break
            if candidate.stripped:
                cluster.append(candidate)
                right = max(right, candidate.x1)
            cursor += 1
        ups = [item for item in cluster if item.role == "up"]
        downs = [item for item in cluster if item.role == "down"]
        overlap = bool(ups and downs) and _horizontal_overlap(ups, downs)
        cluster_x0 = min(item.x0 for item in cluster)
        attached = (
            previous_base is not None
            and cluster_x0 - previous_base.x1 < 0.2 * base_size
            and re.search(r"[A-Za-z0-9)\]}|']$", parts[-1].strip() if parts and parts[-1].strip() else "") is not None
        )
        if overlap and (not attached or all(item.size >= base_size * 0.85 for item in ups + downs)):
            parts.append(rf"\frac{{{_script_group(ups, base_size)}}}{{{_script_group(downs, base_size)}}}")
        elif overlap:
            parts.append(f"_{{{_script_group(downs, base_size)}}}^{{{_script_group(ups, base_size)}}}")
        else:
            if downs:
                parts.append(f"_{{{_script_group(downs, base_size)}}}")
            if ups:
                parts.append(f"^{{{_script_group(ups, base_size)}}}")
        position = cursor

    latex = _join(_merge_sqrt(parts))
    latex = _FUNCTION_RE.sub(r"\\\1", latex)
    latex = re.sub(r"\s*([=<>+])\s*", r" \1 ", latex)
    latex = re.sub(r"(\^|_)\{\s*\}", "", latex)
    return re.sub(r"\s{2,}", " ", latex).strip()


_RELATION_PART_RE = re.compile(r"^\s*(=|<|>|\\(?:leq|geq|neq|approx|to|equiv|sim|le|ge|in|Rightarrow|iff)\b)")


def _merge_sqrt(parts: list[str]) -> list[str]:
    """Il radicando arriva fino al primo simbolo di relazione (la barra della
    radice e' un tratto grafico, non testo, quindi la sua estensione e' ignota)."""
    merged: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == r"\sqrt" and index + 1 < len(parts):
            nxt = index + 1
            radicand: list[str] = []
            while nxt < len(parts):
                candidate = parts[nxt]
                if radicand and _RELATION_PART_RE.match(candidate):
                    break
                if candidate.strip() and candidate.strip()[:1] in {",", ";"}:
                    break
                radicand.append(candidate)
                nxt += 1
            if radicand:
                merged.append(rf"\sqrt{{{_join(radicand)}}}")
                index = nxt
                continue
        merged.append(part)
        index += 1
    return merged


ACCENT_COMMANDS = {
    "\u20d7": "vec", "⃗": "vec", "\u0302": "hat", "ˆ": "hat", "^": "hat", "\u0304": "bar", "¯": "bar",
    "\u0303": "tilde", "˜": "tilde", "~": "tilde", "\u0307": "dot", "˙": "dot", "\u0308": "ddot", "¨": "ddot",
}


def _apply_accents(atoms: list[Atom], base_size: float) -> list[Atom]:
    """Accenti matematici (``\\vec``, ``\\hat``...) posizionati sopra una lettera."""
    result: list[Atom] = []
    accents = [atom for atom in atoms if atom.stripped in ACCENT_COMMANDS and atom.stripped not in {"^", "~"}
               or (atom.stripped in {"^", "~"} and atom.size <= base_size and atom.role == "up")]
    if not accents:
        return atoms
    consumed: set[int] = set()
    for accent in accents:
        best = None
        best_distance = None
        center = (accent.x0 + accent.x1) / 2
        for candidate in atoms:
            if candidate is accent or candidate in accents or not candidate.stripped:
                continue
            overlap = min(candidate.x1, accent.x1) - max(candidate.x0, accent.x0)
            if overlap <= 0 or len(candidate.stripped) > 2:
                continue
            distance = abs((candidate.x0 + candidate.x1) / 2 - center)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best = candidate
        if best is not None:
            command = ACCENT_COMMANDS[accent.stripped]
            best.text = rf"\{command}{{{_atom_latex(best)}}}"
            best.font = ""
            best.math = True
            consumed.add(id(accent))
    for atom in atoms:
        if id(atom) in consumed:
            continue
        if "\u20d7" in atom.text:
            if atom.font.upper().startswith("CM") or "LMMATH" in atom.font.upper():
                # TeX emette l'accento prima della lettera a cui si riferisce.
                atom.text = re.sub(r"\u20d7(\w)", r"\\vec{\1}", atom.text)
            atom.text = re.sub(r"(\w)\u20d7", r"\\vec{\1}", atom.text).replace("\u20d7", "")
        result.append(atom)
    return result


def _horizontal_overlap(first: list[Atom], second: list[Atom]) -> bool:
    a0 = min(atom.x0 for atom in first)
    a1 = max(atom.x1 for atom in first)
    b0 = min(atom.x0 for atom in second)
    b1 = max(atom.x1 for atom in second)
    overlap = min(a1, b1) - max(a0, b0)
    return overlap > 0.3 * min(a1 - a0, b1 - b0)
