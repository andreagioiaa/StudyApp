"""Tema e helper tipografici per CustomTkinter."""

from __future__ import annotations

import customtkinter as ctk

from ..config import COLORS, FONT_FAMILY, MONO_FONT_FAMILY
from .scrolling import install_smooth_scrolling


def configure_theme() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    try:
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
    except Exception:
        pass
    install_smooth_scrolling()


def font(size: int = 13, weight: str = "normal", family: str = FONT_FAMILY) -> ctk.CTkFont:
    return ctk.CTkFont(family=family, size=size, weight=weight)


def mono(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=MONO_FONT_FAMILY, size=size, weight=weight)


def phase_color(phase: str) -> str:
    if phase == "short_break":
        return COLORS.blue
    if phase == "long_break":
        return COLORS.purple
    return COLORS.accent


def status_color(status: str) -> str:
    return {
        "running": COLORS.accent,
        "paused": COLORS.amber,
        "idle": COLORS.text_muted,
    }.get(status, COLORS.text_muted)
