"""Rimozione di testo ridondante dal sorgente ("sostituisci tutto con niente").

Funziona sul testo sorgente dell'editor (Markdown + LaTeX oppure LaTeX), quindi
vale per anteprima, copia ed esportazione. Nessuna dipendenza dalla GUI.

Caratteristiche:

- ricerca letterale o con espressione regolare;
- maiuscole/minuscole e parola intera opzionali;
- "spazi flessibili": uno spazio nel testo cercato corrisponde a qualunque
  sequenza di spazi e a capo (utile quando il PDF spezza le righe in punti
  diversi da una slide all'altra);
- i marcatori ``=== SLIDE N ===`` non vengono mai toccati;
- pulizia facoltativa delle righe rimaste vuote, dei doppi spazi e dei punti
  elenco / titoli rimasti senza testo.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

MARKER_RE = re.compile(r"^=== SLIDE \d+ ===[ \t]*$", re.MULTILINE)
_SENTINEL = ""
_EMPTY_STUB_RE = re.compile(r"^[ \t]*(?:#{1,6}|[-*+•]|\d+[.)])?[ \t]*$")
_PUNCT_AFTER = r"(?=[,.;:!?)\]}])"
_SEPARATORS = r"[-–—|·/]"


class RemovalPatternError(ValueError):
    """Espressione regolare non valida o testo da cercare vuoto."""


@dataclass
class RemovalRule:
    pattern: str
    match_case: bool = False
    whole_word: bool = False
    regex: bool = False
    flexible_spaces: bool = True
    tidy: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RemovalRule":
        fields = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        return cls(**fields)

    def label(self) -> str:
        text = " ".join(self.pattern.split())
        if len(text) > 48:
            text = text[:45] + "…"
        flags = []
        if self.regex:
            flags.append("regex")
        if self.match_case:
            flags.append("Aa")
        if self.whole_word:
            flags.append("parola")
        return f"{text}  [{', '.join(flags)}]" if flags else text


def compile_rule(rule: RemovalRule) -> re.Pattern[str]:
    if not rule.pattern or (not rule.regex and not rule.pattern.strip()):
        raise RemovalPatternError("Scrivi il testo da rimuovere.")
    if rule.regex:
        body = rule.pattern
    else:
        text = rule.pattern.strip() if rule.flexible_spaces else rule.pattern
        if rule.flexible_spaces:
            body = r"\s+".join(re.escape(part) for part in text.split())
        else:
            body = re.escape(text)
    if rule.whole_word:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    flags = re.MULTILINE
    if not rule.match_case:
        flags |= re.IGNORECASE
    try:
        compiled = re.compile(body, flags)
    except re.error as exc:
        raise RemovalPatternError(f"Espressione regolare non valida: {exc}") from exc
    # Le corrispondenze vuote (es. regex "a*") vengono ignorate in find_matches.
    return compiled


def _protected_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in MARKER_RE.finditer(text)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def find_matches(text: str, rule: RemovalRule) -> list[tuple[int, int]]:
    """Intervalli (inizio, fine) che verrebbero rimossi."""
    compiled = compile_rule(rule)
    protected = _protected_spans(text)
    spans = []
    for match in compiled.finditer(text):
        start, end = match.span()
        if start == end or _overlaps(start, end, protected):
            continue
        spans.append((start, end))
    return spans


def count_matches(text: str, rule: RemovalRule) -> int:
    return len(find_matches(text, rule))


def _tidy_line(line: str) -> str | None:
    """Ripulisce una riga toccata dalla rimozione; None se va eliminata."""
    lead = re.match(r"[ \t]*", line).group()
    rest = line[len(lead):]
    # Marcatore di elenco/titolo seguito dal testo rimosso: "- X - resto" -> "- resto".
    stub = re.match(rf"((?:#{{1,6}}|[-*+•]|\d+[.)])[ \t]+){_SENTINEL}", rest)
    prefix = ""
    if stub:
        prefix, rest = stub.group(1), rest[len(stub.group(1)):]
    # Testo rimosso a inizio riga seguito da un separatore: "X - resto" -> "resto".
    rest = re.sub(rf"^{_SENTINEL}+[ \t]*(?:[,;:]|{_SEPARATORS}(?=[ \t]))?[ \t]*", "", rest)
    # Separatore + testo rimosso a fine riga: "resto - X" / "resto, X." -> "resto" / "resto.".
    rest = re.sub(rf"[ \t]*(?:[ \t]{_SEPARATORS}|[,;:])[ \t]*{_SENTINEL}+[ \t]*(?=[.!?]?[ \t]*$)", "", rest)
    rest = re.sub(rf"[ \t]*{_SENTINEL}+[ \t]*{_PUNCT_AFTER}", "", rest)
    rest = re.sub(rf"[ \t]*{_SENTINEL}+[ \t]*", " ", rest)
    rest = rest.strip(" \t")
    cleaned = lead + prefix + rest if rest else ""
    if _EMPTY_STUB_RE.match(cleaned):
        return None
    return cleaned


def remove_text(text: str, rule: RemovalRule) -> tuple[str, int]:
    """Rimuove tutte le occorrenze. Restituisce (nuovo testo, numero di rimozioni)."""
    spans = find_matches(text, rule)
    if not spans:
        return text, 0
    tidy = rule.tidy and _SENTINEL not in text
    filler = _SENTINEL if tidy else ""
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(filler)
        cursor = end
    parts.append(text[cursor:])
    result = "".join(parts)
    if not tidy:
        return result, len(spans)

    output: list[str] = []
    for line in result.split("\n"):
        if _SENTINEL not in line:
            output.append(line)
            continue
        cleaned = _tidy_line(line)
        if cleaned is not None:
            output.append(cleaned)
    result = "\n".join(output)
    # Evita le pile di righe vuote lasciate dai blocchi eliminati.
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result, len(spans)


def apply_rules(text: str, rules: list[RemovalRule]) -> tuple[str, int]:
    """Applica in sequenza piu' regole; le regole non valide vengono saltate."""
    total = 0
    for rule in rules:
        try:
            text, count = remove_text(text, rule)
        except RemovalPatternError:
            continue
        total += count
    return text, total


def rules_to_json(rules: list[RemovalRule]) -> str:
    return json.dumps([rule.to_dict() for rule in rules], ensure_ascii=False)


def rules_from_json(value: str) -> list[RemovalRule]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    rules = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("pattern"):
            try:
                rules.append(RemovalRule.from_dict(item))
            except TypeError:
                continue
    return rules
