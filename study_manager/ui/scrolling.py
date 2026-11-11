"""Scorrimento con la rotella piu' fluido per le aree di CustomTkinter.

CustomTkinter 5.2.2 gestisce la rotella dividendo il delta per 6: su Windows
uno scatto classico (delta 120) muove solo 20 pixel, mentre mouse e touchpad ad
alta risoluzione producono spostamenti di 1-2 pixel. Inoltre ogni
``CTkScrollableFrame`` registra un handler globale e le aree annidate scorrono
insieme.

Questo modulo sostituisce l'handler con una versione che:

- muove circa tre righe di testo per scatto (60 pixel, con un minimo di 1);
- sceglie l'area scrollabile piu' interna che puo' davvero scorrere;
- lascia gestire la rotella ai widget di testo con scorrimento proprio;
- mantiene il supporto a Shift+rotella per lo scorrimento orizzontale.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Iterator

import customtkinter as ctk


WHEEL_NOTCH = 120
WHEEL_STEP_PIXELS = 60
_EPSILON = 1e-6


def wheel_scroll_units(delta: int, step: int = WHEEL_STEP_PIXELS) -> int:
    """Converte il delta della rotella in unita' di scorrimento del canvas.

    Un delta positivo (rotella verso l'alto) produce un valore negativo, che
    sposta il contenuto verso l'alto. I delta piccoli dei touchpad vengono
    arrotondati ma non azzerati, cosi' il contenuto si muove sempre.
    """
    try:
        value = int(delta)
    except (TypeError, ValueError):
        return 0
    if value == 0:
        return 0
    pixels = int(round(value / WHEEL_NOTCH * step))
    if pixels == 0:
        pixels = 1 if value > 0 else -1
    return -pixels


def _iter_ancestors(widget) -> Iterator[object]:
    node = widget
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        yield node
        node = getattr(node, "master", None)


def _view_can_scroll(widget, units: int) -> bool:
    try:
        first, last = widget.yview()
    except (tk.TclError, AttributeError):
        return False
    if last - first >= 1.0 - _EPSILON:
        return False
    if units > 0:
        return last < 1.0 - _EPSILON
    return first > _EPSILON


def _owns_wheel(widget, units: int) -> bool:
    """True se il widget gestisce la rotella al posto dell'area scrollabile."""
    if isinstance(widget, ctk.CTkTextbox):
        inner = getattr(widget, "_textbox", None)
        return inner is None or _view_can_scroll(inner, units)
    if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview)):
        return _view_can_scroll(widget, units)
    return False


def _canvas_can_scroll(canvas: tk.Canvas, horizontal: bool, units: int) -> bool:
    first, last = canvas.xview() if horizontal else canvas.yview()
    if last - first >= 1.0 - _EPSILON:
        return False
    if units > 0:
        return last < 1.0 - _EPSILON
    return first > _EPSILON


def _resolve_target(widget, units: int, shift: bool):
    """Restituisce il CTkScrollableFrame piu' interno in grado di scorrere."""
    for node in _iter_ancestors(widget):
        if _owns_wheel(node, units):
            return None
        frame = getattr(node, "_studyflow_frame", None)
        if frame is not None:
            node = frame
        if not isinstance(node, ctk.CTkScrollableFrame):
            continue
        canvas = node._parent_canvas
        horizontal = shift or getattr(node, "_orientation", "vertical") == "horizontal"
        if horizontal and _canvas_can_scroll(canvas, True, units):
            return node
        if _canvas_can_scroll(canvas, False, units):
            return node
    return None


def _mouse_wheel_all(self, event) -> None:
    units = wheel_scroll_units(getattr(event, "delta", 0))
    if not units:
        return
    shift = bool(getattr(self, "_shift_pressed", False))
    if _resolve_target(event.widget, units, shift) is not self:
        return
    canvas = self._parent_canvas
    horizontal = shift or getattr(self, "_orientation", "vertical") == "horizontal"
    if horizontal and _canvas_can_scroll(canvas, True, units):
        canvas.xview_scroll(units, "units")
        return
    if _canvas_can_scroll(canvas, False, units):
        canvas.yview_scroll(units, "units")


def install_smooth_scrolling() -> None:
    """Sostituisce l'handler della rotella di CustomTkinter (una sola volta)."""
    frame_class = ctk.CTkScrollableFrame
    if getattr(frame_class, "_studyflow_smooth_scrolling", False):
        return
    original_init = frame_class.__init__

    def _init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Permette di risalire dall'area di disegno al frame scrollabile anche
        # quando il puntatore si trova su uno spazio vuoto della vista.
        for widget in (self._parent_canvas, self._parent_frame, self._scrollbar):
            try:
                widget._studyflow_frame = self
            except Exception:
                pass

    frame_class.__init__ = _init
    frame_class._mouse_wheel_all = _mouse_wheel_all
    frame_class._studyflow_smooth_scrolling = True
