"""Rendering locale delle formule LaTeX per l'anteprima dentro l'app.

Tkinter non puo' ospitare KaTeX/MathJax (servirebbe un motore web), quindi
l'anteprima interna usa ``matplotlib.mathtext``: gia' presente come
dipendenza, completamente offline, supporta la maggior parte della sintassi
usata nelle slide (frazioni, radici, apici, simboli, ``\\mathbb``, ``\\vec``).
Le formule non supportate vengono mostrate come codice evidenziato; per una
resa identica a KaTeX c'e' l'anteprima HTML nel browser (bundle locale).
"""

from __future__ import annotations

import base64
import io
import logging
import re
import tkinter as tk
from collections import OrderedDict


LOGGER = logging.getLogger(__name__)

_UNSUPPORTED_RE = re.compile(r"\\(begin|end)\{")
_STRIP_RE = re.compile(r"\\(displaystyle|textstyle|limits|nolimits|notag|nonumber)\b|\\(label|tag)\{[^}]*\}")


def prepare_for_mathtext(latex: str, display: bool = False) -> str | None:
    """Adatta il codice alle capacita' di mathtext; ``None`` se non supportato."""
    text = _STRIP_RE.sub("", latex).strip()
    if not text or _UNSUPPORTED_RE.search(text) or "\\\\" in text:
        return None
    text = text.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    if display:
        # In stile display le frazioni hanno corpo pieno, come in LaTeX/KaTeX.
        text = re.sub(r"\\frac(?![A-Za-z])", r"\\dfrac", text)
    text = text.replace("\\le ", "\\leq ").replace("\\ge ", "\\geq ")
    text = text.replace("\\lbrace", "\\{").replace("\\rbrace", "\\}")
    return text


class MathRenderer:
    """Converte formule in ``tk.PhotoImage`` trasparenti, con cache LRU."""

    def __init__(self, master: tk.Misc, color: str, cache_size: int = 600):
        self._master = master
        self._color = color
        self._cache: OrderedDict[tuple[str, bool, int], tk.PhotoImage | None] = OrderedDict()
        self._cache_size = cache_size
        self._figure_cls = None
        self._canvas_cls = None
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                from matplotlib.figure import Figure

                self._figure_cls = Figure
                self._canvas_cls = FigureCanvasAgg
                self._available = True
            except Exception:  # pragma: no cover - matplotlib e' una dipendenza
                LOGGER.exception("matplotlib non disponibile per l'anteprima delle formule.")
                self._available = False
        return bool(self._available)

    def render_png(self, latex: str, display: bool = False, size: int = 13) -> bytes | None:
        prepared = prepare_for_mathtext(latex, display)
        if prepared is None or not self.available:
            return None
        figure = self._figure_cls(figsize=(0.01, 0.01), dpi=100)  # type: ignore[misc]
        self._canvas_cls(figure)  # type: ignore[misc]
        figure.patch.set_alpha(0.0)
        figure.text(0, 0, f"${prepared}$", fontsize=size + (3 if display else 0), color=self._color)
        buffer = io.BytesIO()
        try:
            figure.savefig(buffer, format="png", dpi=100, transparent=True, bbox_inches="tight", pad_inches=0.03)
        except Exception:
            return None
        return buffer.getvalue()

    def image(self, latex: str, display: bool = False, size: int = 13) -> tk.PhotoImage | None:
        key = (latex, display, size)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        image: tk.PhotoImage | None = None
        data = self.render_png(latex, display, size)
        if data:
            try:
                image = tk.PhotoImage(master=self._master, data=base64.b64encode(data).decode("ascii"))
            except tk.TclError:
                image = None
        self._cache[key] = image
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return image

    def is_cached(self, latex: str, display: bool = False, size: int = 13) -> bool:
        return (latex, display, size) in self._cache
