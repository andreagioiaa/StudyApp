"""Componenti riutilizzabili dell'interfaccia."""

from __future__ import annotations

import tkinter as tk
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

import customtkinter as ctk

from ..config import COLORS, FONT_FAMILY, MONO_FONT_FAMILY
from .theme import font, mono


class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", COLORS.surface)
        kwargs.setdefault("corner_radius", 16)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", COLORS.border)
        super().__init__(master, **kwargs)


class PageHeader(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str, **kwargs):
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self,
            text=title,
            font=font(28, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            self,
            text=subtitle,
            font=font(13),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))


class StatCard(Card):
    def __init__(
        self,
        master,
        title: str,
        value: str = "-",
        subtitle: str = "",
        accent: str = COLORS.accent,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._accent = accent
        self.grid_columnconfigure(0, weight=1)
        self._bar = ctk.CTkFrame(self, width=42, height=4, fg_color=accent, corner_radius=2)
        self._bar.grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        self._title = ctk.CTkLabel(
            self,
            text=title,
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        )
        self._title.grid(row=1, column=0, sticky="ew", padx=16, pady=(9, 0))
        self._value = ctk.CTkLabel(
            self,
            text=value,
            font=font(26, "bold"),
            text_color=COLORS.text,
            anchor="w",
        )
        self._value.grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 0))
        self._subtitle = ctk.CTkLabel(
            self,
            text=subtitle,
            font=font(11),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self._subtitle.grid(row=3, column=0, sticky="ew", padx=16, pady=(1, 14))

    def set(self, value: str, subtitle: str | None = None, accent: str | None = None) -> None:
        # Riconfigurare un widget CTk lo ridisegna anche a parita' di testo:
        # se nulla cambia non si tocca niente.
        signature = (value, subtitle, accent)
        if signature == getattr(self, "_last_set", None):
            return
        self._last_set = signature
        self._value.configure(text=value)
        if subtitle is not None:
            self._subtitle.configure(text=subtitle)
        if accent is not None:
            self._bar.configure(fg_color=accent)


class TimerRing(tk.Canvas):
    def __init__(self, master, size: int = 264):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=COLORS.surface,
            highlightthickness=0,
            bd=0,
        )
        self._size = size
        self._progress = 0.0
        self._main_text = "25:00"
        self._sub_text = "Pronto"
        self._color = COLORS.accent
        self._last_signature: tuple | None = None
        self._draw()

    def set(
        self,
        progress: float,
        main_text: str,
        sub_text: str,
        color: str = COLORS.accent,
        force: bool = False,
    ) -> None:
        signature = (round(progress, 3), main_text, sub_text, color)
        if not force and signature == self._last_signature:
            return
        self._last_signature = signature
        self._progress = max(0.0, min(1.0, progress))
        self._main_text = main_text
        self._sub_text = sub_text
        self._color = color
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        pad = 16
        size = self._size
        self.create_oval(
            pad,
            pad,
            size - pad,
            size - pad,
            outline=COLORS.border,
            width=11,
        )
        if self._progress > 0.001:
            self.create_arc(
                pad,
                pad,
                size - pad,
                size - pad,
                start=90,
                extent=-359.9 * self._progress,
                style="arc",
                outline=self._color,
                width=11,
            )
        self.create_oval(
            size / 2 - 5,
            pad - 3,
            size / 2 + 5,
            pad + 7,
            fill=self._color,
            outline="",
        )
        self.create_text(
            size / 2,
            size / 2 - 12,
            text=self._main_text,
            fill=COLORS.text,
            font=(MONO_FONT_FAMILY, 44, "bold"),
        )
        self.create_text(
            size / 2,
            size / 2 + 34,
            text=self._sub_text.upper(),
            fill=COLORS.text_muted,
            font=(FONT_FAMILY, 10, "bold"),
        )


class RenderState:
    """Ricorda quali dati sono attualmente disegnati in una sezione di una vista.

    Le viste restano in memoria dopo la prima apertura, ma ``on_show``
    ricostruiva comunque liste e dettagli con ``clear_children``: l'area restava
    vuota per un istante e poi veniva ridisegnata (sfarfallio al cambio di
    sezione). Ogni funzione di disegno registra la firma dei dati che mostra;
    durante ``on_show`` (contesto ``only_if_changed``) la ricostruzione viene
    saltata se la firma e' identica a quella gia' a schermo.

    Fuori da ``on_show`` le azioni dell'utente ricostruiscono sempre, cosi'
    nessun comportamento esistente cambia (per esempio "Annulla" che ripristina
    un campo modificato).
    """

    def __init__(self) -> None:
        self._signatures: dict[str, str] = {}
        self._lazy_depth = 0

    @staticmethod
    def signature(*parts: Any) -> str:
        return repr(parts)

    @contextmanager
    def only_if_changed(self) -> Iterator[None]:
        self._lazy_depth += 1
        try:
            yield
        finally:
            self._lazy_depth -= 1

    def should_render(self, key: str, *parts: Any) -> bool:
        """True se la sezione va (ri)disegnata; registra sempre la nuova firma."""
        value = self.signature(*parts)
        if self._lazy_depth and self._signatures.get(key) == value:
            return False
        self._signatures[key] = value
        return True

    def forget(self, key: str | None = None) -> None:
        if key is None:
            self._signatures.clear()
        else:
            self._signatures.pop(key, None)


def render_state(widget) -> RenderState:
    """``RenderState`` associato a una vista (creato al primo uso)."""
    state = getattr(widget, "_studyflow_render_state", None)
    if state is None:
        state = RenderState()
        widget._studyflow_render_state = state
    return state


def clear_children(widget) -> None:
    for child in list(widget.winfo_children()):
        try:
            child.destroy()
        except tk.TclError:
            pass


def small_label(master, text: str, muted: bool = False) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=text,
        font=font(11),
        text_color=COLORS.text_faint if muted else COLORS.text_muted,
        anchor="w",
    )


def separator(master) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, height=1, fg_color=COLORS.border, corner_radius=0)


def labeled_entry(
    master,
    label: str,
    variable: tk.Variable | None = None,
    width: int = 120,
    placeholder: str = "",
) -> tuple[ctk.CTkFrame, ctk.CTkEntry]:
    frame = ctk.CTkFrame(master, fg_color="transparent")
    ctk.CTkLabel(
        frame,
        text=label,
        font=font(11, "bold"),
        text_color=COLORS.text_muted,
        anchor="w",
    ).pack(anchor="w", pady=(0, 5))
    entry = ctk.CTkEntry(
        frame,
        width=width,
        height=36,
        textvariable=variable,
        placeholder_text=placeholder,
        fg_color=COLORS.surface_alt,
        border_color=COLORS.border,
        text_color=COLORS.text,
        font=font(13),
        corner_radius=9,
    )
    entry.pack(fill="x")
    return frame, entry


def center_window(window: ctk.CTkToplevel | ctk.CTk, width: int, height: int) -> None:
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def set_window_icon(window: tk.Misc, icon_path) -> None:
    try:
        window.iconbitmap(str(icon_path))
    except (tk.TclError, OSError):
        pass
