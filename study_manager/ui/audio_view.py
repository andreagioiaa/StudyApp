"""Vista Audio: musica locale, download YouTube e suoni di concentrazione."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..services.audio_service import (
    FOCUS_SOUNDS,
    generate_focus_sounds,
)
from ..services.youtube_audio import (
    FFmpegMissingError,
    YouTubeAudioError,
    copy_title,
    download_audio,
    find_existing_track,
    find_ffmpeg,
    is_youtube_url,
    probe_video,
)
from ..utils import safe_float
from .theme import font
from .widgets import (
    Card,
    PageHeader,
    center_window,
    clear_children,
    separator,
    set_window_icon,
    render_state,
)


NO_TRACK = "Nessun brano selezionato"


class DuplicateAudioDialog(ctk.CTkToplevel):
    """Scelta esplicita quando il nome del file esiste gia'."""

    def __init__(self, master, filename: str):
        super().__init__(master)
        self.result = "Salta"
        self.title("File gia' esistente")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose("Salta"))
        self.grid_columnconfigure(0, weight=1)
        set_window_icon(self, master.app.paths["icon"])

        card = Card(self)
        card.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Esiste gia' un file con questo nome",
            font=font(18, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(20, 6))
        ctk.CTkLabel(
            card,
            text=filename,
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=510,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 10))
        ctk.CTkLabel(
            card,
            text=(
                "Puoi creare una copia con un nuovo nome, aprire la cartella "
                "per gestire il file esistente oppure saltare il download."
            ),
            font=font(11),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=510,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 16))
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 20))
        for column in range(3):
            buttons.grid_columnconfigure(column, weight=1)
        ctk.CTkButton(
            buttons,
            text="Crea Copia",
            height=38,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=lambda: self._choose("Crea Copia"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            buttons,
            text="Vai a Cartella",
            height=38,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=lambda: self._choose("Vai a Cartella"),
        ).grid(row=0, column=1, sticky="ew", padx=6)
        ctk.CTkButton(
            buttons,
            text="Salta",
            height=38,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=lambda: self._choose("Salta"),
        ).grid(row=0, column=2, sticky="ew", padx=(6, 0))
        center_window(self, 620, 300)
        self.after(60, self._activate)

    def _activate(self) -> None:
        self.lift()
        self.focus_force()
        self.grab_set()

    def _choose(self, choice: str) -> None:
        self.result = choice
        self.destroy()


class AudioView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.audio = app.audio
        self.db = app.db
        self.sounds = Path(app.paths["sounds"])
        self._poll_job: str | None = None
        self._save_job: str | None = None
        self._downloading = False
        self._generating = False
        self._focus_play_after: str | None = None
        self._track_names: list[str] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        PageHeader(
            self.scroll,
            "Audio e concentrazione",
            "Musica locale, audio dei video YouTube e suoni ambientali per accompagnare lo studio.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        content = ctk.CTkFrame(self.scroll, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3, uniform="audio")
        content.grid_columnconfigure(1, weight=2, uniform="audio")

        self._build_player(content)
        self._build_automation(content)
        self._build_youtube(content)
        self._build_library(content)
        self._build_focus(content)

    # ------------------------------------------------------------------ cards
    def _build_player(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Lettore audio",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 2))
        ctk.CTkLabel(
            card,
            text="Riproduzione locale senza pubblicita' e senza video.",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 12))
        self.now_label = ctk.CTkLabel(
            card,
            text="Nessun brano",
            font=font(24, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self.now_label.grid(row=2, column=0, sticky="ew", padx=22)
        self.player_state = ctk.CTkLabel(
            card,
            text="Pronto",
            font=font(11),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.player_state.grid(row=3, column=0, sticky="w", padx=22, pady=(3, 14))

        controls = ctk.CTkFrame(card, fg_color="transparent")
        controls.grid(row=4, column=0, sticky="ew", padx=22)
        controls.grid_columnconfigure(2, weight=1)
        self.play_button = ctk.CTkButton(
            controls,
            text="Riproduci",
            height=38,
            width=118,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=self._toggle_playback,
        )
        self.play_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ctk.CTkButton(
            controls,
            text="Stop",
            height=38,
            width=76,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=self._stop_playback,
        )
        self.stop_button.grid(row=0, column=1)

        volume_frame = ctk.CTkFrame(controls, fg_color="transparent")
        volume_frame.grid(row=0, column=2, sticky="ew", padx=(20, 0))
        volume_frame.grid_columnconfigure(0, weight=1)
        volume_header = ctk.CTkFrame(volume_frame, fg_color="transparent")
        volume_header.grid(row=0, column=0, sticky="ew")
        volume_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            volume_header,
            text="Volume",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.volume_label = ctk.CTkLabel(
            volume_header,
            text="55%",
            font=font(10, "bold"),
            text_color=COLORS.accent,
        )
        self.volume_label.grid(row=0, column=1, sticky="e")
        self.volume_slider = ctk.CTkSlider(
            volume_frame,
            from_=0,
            to=100,
            number_of_steps=100,
            height=16,
            progress_color=COLORS.accent,
            button_color=COLORS.text,
            button_hover_color=COLORS.accent,
            fg_color=COLORS.border,
            command=self._on_volume,
        )
        self.volume_slider.grid(row=1, column=0, sticky="ew", pady=(7, 0))

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=22, pady=(16, 20))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            footer,
            text=self._sounds_hint(),
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=420,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            footer,
            text="Apri cartella sounds",
            height=32,
            width=150,
            font=font(10, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._open_folder,
        ).grid(row=0, column=1, sticky="e")

    def _build_automation(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Musica e Pomodoro",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Durante una fase di lavoro la musica parte automaticamente; "
                "si mette in pausa quando metti in pausa il timer."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=330,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 14))

        self.autoplay_var = ctk.BooleanVar(value=False)
        self.loop_var = ctk.BooleanVar(value=True)
        ctk.CTkLabel(
            card,
            text="Brano predefinito",
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=22)
        self.track_combo = ctk.CTkComboBox(
            card,
            values=[NO_TRACK],
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_alt,
            dropdown_text_color=COLORS.text,
            text_color=COLORS.text,
            font=font(11),
            dropdown_font=font(11),
            corner_radius=9,
            command=self._on_track_selected,
        )
        self.track_combo.grid(row=3, column=0, sticky="ew", padx=22, pady=(5, 14))

        self._switch_row(
            card,
            4,
            "Avvia con il Pomodoro",
            "Usa il brano scelto per le fasi di lavoro.",
            self.autoplay_var,
            self._on_autoplay,
        )
        self._switch_row(
            card,
            5,
            "Ripeti in loop",
            "Continua fino alla fine della sessione.",
            self.loop_var,
            self._on_loop,
        )
        separator(card).grid(row=6, column=0, sticky="ew", padx=22, pady=(14, 12))
        ctk.CTkLabel(
            card,
            text=(
                "La riproduzione manuale resta indipendente dal timer. "
                "Se scegli un altro brano durante una sessione, StudyFlow non lo interrompe."
            ),
            font=font(9),
            text_color=COLORS.text_faint,
            wraplength=330,
            justify="left",
            anchor="w",
        ).grid(row=7, column=0, sticky="ew", padx=22, pady=(0, 18))

    def _switch_row(
        self,
        parent,
        row: int,
        title: str,
        subtitle: str,
        variable: ctk.BooleanVar,
        command,
    ) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=22, pady=5)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=title,
            font=font(11, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            frame,
            text=subtitle,
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=1, column=0, sticky="w")
        ctk.CTkSwitch(
            frame,
            text="",
            width=44,
            variable=variable,
            command=command,
            progress_color=COLORS.accent,
            button_color=COLORS.text,
            button_hover_color=COLORS.text,
            fg_color=COLORS.border,
        ).grid(row=0, column=1, rowspan=2, padx=(12, 0))

    def _build_youtube(self, parent) -> None:
        card = Card(parent)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Scarica audio da YouTube",
            font=font(16, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.ffmpeg_label = ctk.CTkLabel(
            header,
            text="",
            font=font(9, "bold"),
            text_color=COLORS.text_faint,
            anchor="e",
        )
        self.ffmpeg_label.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            card,
            text=(
                "YouTube non offre una riproduzione solo-audio ufficiale. StudyFlow "
                "scarica la traccia migliore, la converte in MP3 e la salva nella "
                "cartella sounds con il titolo del video."
            ),
            font=font(10),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=950,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 12))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=22)
        row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(
            row,
            height=40,
            placeholder_text="https://www.youtube.com/watch?v=...",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            placeholder_text_color=COLORS.text_faint,
            font=font(11),
            corner_radius=9,
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.paste_button = ctk.CTkButton(
            row,
            text="Incolla",
            height=40,
            width=84,
            font=font(10, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=self._paste_url,
        )
        self.paste_button.grid(row=0, column=1, padx=(0, 8))
        self.download_button = ctk.CTkButton(
            row,
            text="Scarica audio",
            height=40,
            width=150,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=self._start_download,
        )
        self.download_button.grid(row=0, column=2)

        self.progress = ctk.CTkProgressBar(
            card,
            height=8,
            progress_color=COLORS.accent,
            fg_color=COLORS.border,
            corner_radius=4,
        )
        self.progress.grid(row=3, column=0, sticky="ew", padx=22, pady=(14, 5))
        self.progress.set(0)
        self.download_status = ctk.CTkLabel(
            card,
            text="Pronto. Il download richiede FFmpeg.",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=950,
            justify="left",
        )
        self.download_status.grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 18))

    def _build_library(self, parent) -> None:
        card = Card(parent)
        card.grid(row=2, column=0, sticky="nsew", padx=(0, 10), pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Libreria audio",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header,
            text="Aggiorna",
            height=30,
            width=84,
            font=font(9, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._refresh_library,
        ).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(
            header,
            text="Aggiungi file",
            height=30,
            width=104,
            font=font(9, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=self._import_files,
        ).grid(row=0, column=2, padx=(8, 0))
        ctk.CTkLabel(
            card,
            text="MP3, OGG, WAV e FLAC nella cartella sounds.",
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 10))
        self.library_body = ctk.CTkFrame(card, fg_color="transparent")
        self.library_body.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
        self.library_body.grid_columnconfigure(0, weight=1)

    def _build_focus(self, parent) -> None:
        card = Card(parent)
        card.grid(row=2, column=1, sticky="nsew", padx=(10, 0), pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Suoni di concentrazione",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Quattro loop generati localmente, senza copyright e senza "
                "connessione. Perfetti per coprire rumori di fondo."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=310,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 12))

        self.focus_buttons: dict[str, ctk.CTkButton] = {}
        for index, (name, _kind) in enumerate(FOCUS_SOUNDS):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.grid(row=2 + index, column=0, sticky="ew", padx=22, pady=3)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row,
                text=Path(name).stem,
                font=font(10, "bold"),
                text_color=COLORS.text_muted,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            button = ctk.CTkButton(
                row,
                text="Riproduci",
                height=30,
                width=90,
                font=font(9, "bold"),
                fg_color="transparent",
                hover_color=COLORS.surface_alt,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.text_muted,
                corner_radius=8,
                command=lambda value=name: self._play_focus(value),
            )
            button.grid(row=0, column=1, sticky="e")
            self.focus_buttons[name] = button
        separator(card).grid(row=7, column=0, sticky="ew", padx=22, pady=(12, 10))
        self.generate_button = ctk.CTkButton(
            card,
            text="Genera suoni mancanti",
            height=36,
            font=font(10, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            corner_radius=9,
            command=self._generate_focus,
        )
        self.generate_button.grid(row=8, column=0, sticky="ew", padx=22, pady=(0, 8))
        ctk.CTkLabel(
            card,
            text="I file WAV vengono creati nella cartella sounds.",
            font=font(9),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=9, column=0, sticky="w", padx=22, pady=(0, 18))

    # --------------------------------------------------------------- lifecycle
    def on_show(self) -> None:
        with render_state(self).only_if_changed():
            self._refresh_library()
        self._load_preferences()
        self._refresh_ffmpeg()
        self._refresh_player()
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except (tk.TclError, ValueError):
                pass
        self._poll_job = self.after(500, self._poll)

    def on_hide(self) -> None:
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except (tk.TclError, ValueError):
                pass
            self._poll_job = None
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except (tk.TclError, ValueError):
                pass
            self._save_job = None
            self._save_volume()

    def _poll(self) -> None:
        self._poll_job = None
        if not self.winfo_exists():
            return
        self._refresh_player()
        self._poll_job = self.after(500, self._poll)

    # -------------------------------------------------------------- preferences
    @staticmethod
    def _bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _load_preferences(self) -> None:
        settings = self.db.get_settings()
        volume = max(0.0, min(1.0, safe_float(settings.get("audio_volume", "0.55"), 0.55)))
        self.volume_slider.set(volume * 100)
        self.volume_label.configure(text=f"{round(volume * 100)}%")
        self.audio.set_volume(volume)
        loop = self._bool(settings.get("audio_loop", "1"))
        self.loop_var.set(loop)
        self.audio.set_loop(loop)
        self.autoplay_var.set(self._bool(settings.get("audio_autoplay", "0")))
        selected = str(settings.get("audio_track", "") or "")
        self.track_combo.set(selected if selected in self._track_names else NO_TRACK)

    def _persist_preferences(self) -> None:
        track = self.track_combo.get()
        if track == NO_TRACK:
            track = ""
        self.db.set_settings(
            {
                "audio_autoplay": "1" if self.autoplay_var.get() else "0",
                "audio_track": track,
                "audio_volume": f"{self.audio.status()['volume']:.2f}",
                "audio_loop": "1" if self.loop_var.get() else "0",
            }
        )
        self.app.reload_audio_settings()

    def _on_autoplay(self) -> None:
        if self.autoplay_var.get() and not self._selected_track():
            if self._track_names:
                self.track_combo.set(self._track_names[0])
            else:
                self.autoplay_var.set(False)
                messagebox.showinfo(
                    "Nessun brano",
                    "Aggiungi almeno un file audio o scarica un video YouTube, "
                    "poi riattiva l'avvio automatico.",
                    parent=self.winfo_toplevel(),
                )
                return
        self._persist_preferences()

    def _on_loop(self) -> None:
        self.audio.set_loop(self.loop_var.get())
        self._persist_preferences()

    def _on_track_selected(self, value: str) -> None:
        del value
        self._persist_preferences()

    def _on_volume(self, value: float) -> None:
        volume = self.audio.set_volume(float(value) / 100.0)
        self.volume_label.configure(text=f"{round(volume * 100)}%")
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except (tk.TclError, ValueError):
                pass
        self._save_job = self.after(500, self._save_volume)

    def _save_volume(self) -> None:
        self._save_job = None
        self._persist_preferences()

    def _selected_track(self) -> str:
        value = self.track_combo.get()
        return "" if value == NO_TRACK else value

    # ----------------------------------------------------------------- player
    def _refresh_player(self) -> None:
        status = self.audio.status()
        current = str(status["current"] or "")
        if current:
            self.now_label.configure(text=Path(current).stem)
        else:
            self.now_label.configure(text="Nessun brano")
        if status["paused"]:
            state, color = "In pausa", COLORS.amber
            self.play_button.configure(text="Riprendi")
        elif status["playing"]:
            state, color = "In riproduzione", COLORS.accent
            self.play_button.configure(text="Pausa")
        elif current:
            state, color = "Terminato", COLORS.text_muted
            self.play_button.configure(text="Riproduci")
        elif status["error"]:
            state, color = "Audio non disponibile", COLORS.red
            self.play_button.configure(text="Riprova")
        else:
            state, color = "Pronto", COLORS.text_faint
            self.play_button.configure(text="Riproduci")
        if status["source"] == "auto" and current:
            state = f"{state} · avvio automatico"
        self.player_state.configure(text=state, text_color=color)
        if current and current in self._track_names:
            if self.track_combo.get() == NO_TRACK:
                self.track_combo.set(current)

    def _toggle_playback(self) -> None:
        status = self.audio.status()
        if status["paused"]:
            self.audio.resume()
            return
        if status["playing"]:
            self.audio.pause()
            return
        track = self._selected_track() or str(status["current"] or "")
        if not track:
            if self._track_names:
                track = self._track_names[0]
                self.track_combo.set(track)
            else:
                messagebox.showinfo(
                    "Nessun brano",
                    "Aggiungi un file audio o scarica l'audio di un video YouTube.",
                    parent=self.winfo_toplevel(),
                )
                return
        self._play_track(track)

    def _play_track(self, track: str) -> None:
        if self.audio.status()["error"]:
            self.audio.retry_mixer()
        ok, error = self.audio.play(track, loop=self.loop_var.get(), source="manual")
        if not ok:
            messagebox.showerror("Riproduzione non riuscita", error, parent=self.winfo_toplevel())
            self._refresh_player()
            return
        self.track_combo.set(track)
        self._persist_preferences()
        self._refresh_player()

    def _stop_playback(self) -> None:
        self.audio.stop()
        self._refresh_player()

    def _play_focus(self, name: str) -> None:
        path = self.sounds / name
        if not path.is_file():
            self._generate_focus(then_play=name)
            return
        self._play_track(name)

    # ----------------------------------------------------------------- library
    def _refresh_library(self) -> None:
        selected = self.track_combo.get()
        tracks = self.audio.list_tracks()
        self._track_names = [str(track["name"]) for track in tracks]
        values = [NO_TRACK, *self._track_names]
        self.track_combo.configure(values=values)
        if selected not in values:
            selected = NO_TRACK
        self.track_combo.set(selected)

        player = self.audio.status()
        if not render_state(self).should_render(
            "library", tracks, str(player["current"] or ""), bool(player["playing"])
        ):
            self._refresh_focus_buttons()
            return
        clear_children(self.library_body)
        if not tracks:
            ctk.CTkLabel(
                self.library_body,
                text="Nessun file audio. Aggiungi un file o scarica da YouTube.",
                font=font(11),
                text_color=COLORS.text_faint,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=8, pady=14)
            self._refresh_focus_buttons()
            return
        status = self.audio.status()
        current = str(status["current"] or "")
        for index, track in enumerate(tracks):
            name = str(track["name"])
            row = ctk.CTkFrame(
                self.library_body,
                fg_color=COLORS.surface_alt if name == current else "transparent",
                corner_radius=8,
            )
            row.grid(row=index, column=0, sticky="ew", padx=2, pady=2)
            row.grid_columnconfigure(0, weight=1)
            title = ctk.CTkLabel(
                row,
                text=Path(name).stem,
                font=font(10, "bold"),
                text_color=COLORS.accent if name == current else COLORS.text,
                anchor="w",
                wraplength=370,
                justify="left",
            )
            title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
            detail = f"{track['extension'].upper()} · {self._format_size(int(track['size']))}"
            if track["builtin"]:
                detail += " · integrato"
            ctk.CTkLabel(
                row,
                text=detail,
                font=font(8),
                text_color=COLORS.text_faint,
                anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
            ctk.CTkButton(
                row,
                text="In corso" if name == current and status["playing"] else "Riproduci",
                height=28,
                width=86,
                font=font(9, "bold"),
                fg_color=COLORS.accent if name == current and status["playing"] else "transparent",
                hover_color=COLORS.accent_hover if name == current and status["playing"] else COLORS.surface_hover,
                border_width=0 if name == current and status["playing"] else 1,
                border_color=COLORS.border,
                text_color=COLORS.bg if name == current and status["playing"] else COLORS.text_muted,
                corner_radius=7,
                command=lambda value=name: self._play_track(value),
            ).grid(row=0, column=1, rowspan=2, sticky="e", padx=10)
        self._refresh_focus_buttons()

    def _refresh_focus_buttons(self) -> None:
        for name, button in self.focus_buttons.items():
            exists = (self.sounds / name).is_file()
            button.configure(
                text="Riproduci" if exists else "Genera",
                state="normal",
            )

    def _import_files(self) -> None:
        files = filedialog.askopenfilenames(
            parent=self.winfo_toplevel(),
            title="Aggiungi file audio",
            filetypes=[
                ("Audio", "*.mp3 *.ogg *.wav *.flac"),
                ("MP3", "*.mp3"),
                ("Ogg", "*.ogg"),
                ("WAV", "*.wav"),
                ("FLAC", "*.flac"),
            ],
        )
        if not files:
            return
        copied = 0
        try:
            for value in files:
                source = Path(value)
                if not source.is_file():
                    continue
                if source.parent.resolve() == self.sounds.resolve():
                    continue
                destination = self.sounds / source.name
                if destination.exists():
                    choice = self._ask_duplicate(source.name)
                    if choice == "Salta":
                        continue
                    if choice == "Vai a Cartella":
                        self._reveal(destination)
                        continue
                    destination = self.sounds / f"{copy_title(self.sounds, source.stem)}{source.suffix.lower()}"
                shutil.copy2(source, destination)
                copied += 1
        except OSError as exc:
            messagebox.showerror("Importazione non riuscita", str(exc), parent=self.winfo_toplevel())
        if copied:
            self._refresh_library()
            self.app.toast(f"{copied} file audio aggiunti.")

    def _ask_duplicate(self, filename: str) -> str:
        dialog = DuplicateAudioDialog(self, filename)
        self.wait_window(dialog)
        return dialog.result

    def _format_size(self, size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024.0 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024.0
        return f"{value:.1f} GB"

    # ------------------------------------------------------------------- focus
    def _generate_focus(self, then_play: str | None = None) -> None:
        if self._generating:
            return
        self._generating = True
        self._focus_play_after = then_play
        self.generate_button.configure(state="disabled", text="Generazione...")

        def worker() -> None:
            try:
                created = generate_focus_sounds(self.sounds)
            except Exception as exc:
                self.app.post(lambda error=str(exc): self._focus_done(None, error))
                return
            self.app.post(lambda result=list(created): self._focus_done(result, ""))

        threading.Thread(
            target=worker,
            name="StudyFlow-FocusSounds",
            daemon=True,
        ).start()

    def _focus_done(self, created: list[Path] | None, error: str) -> None:
        self._generating = False
        self.generate_button.configure(state="normal", text="Genera suoni mancanti")
        if error:
            self._focus_play_after = None
            messagebox.showerror("Generazione non riuscita", error, parent=self.winfo_toplevel())
            return
        self._refresh_library()
        track_to_play = self._focus_play_after
        self._focus_play_after = None
        if track_to_play and (self.sounds / track_to_play).is_file():
            self._play_track(track_to_play)
        if created:
            self.app.toast(f"{len(created)} suoni di concentrazione creati.")
        else:
            self.app.toast("I suoni di concentrazione sono gia' presenti.")

    # --------------------------------------------------------------- youtube
    def _paste_url(self) -> None:
        try:
            value = self.clipboard_get()
        except tk.TclError:
            value = ""
        if value:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, value.strip())

    def _refresh_ffmpeg(self) -> None:
        ffmpeg = find_ffmpeg(self.sounds)
        if ffmpeg is None:
            self.ffmpeg_label.configure(
                text="FFmpeg non trovato",
                text_color=COLORS.amber,
            )
            self.download_status.configure(
                text=(
                    "Per scaricare da YouTube serve FFmpeg. Installa con "
                    "'winget install Gyan.FFmpeg' oppure copia ffmpeg.exe nella "
                    "cartella dati di StudyFlow."
                ),
                text_color=COLORS.amber,
            )
        else:
            self.ffmpeg_label.configure(
                text=f"FFmpeg pronto: {ffmpeg.name}",
                text_color=COLORS.green,
            )
            if not self._downloading and "FFmpeg" in self.download_status.cget("text"):
                self.download_status.configure(
                    text="Pronto. Il download richiede FFmpeg.",
                    text_color=COLORS.text_faint,
                )

    def _start_download(self) -> None:
        if self._downloading:
            return
        url = self.url_entry.get().strip()
        if not is_youtube_url(url):
            messagebox.showwarning(
                "Link non valido",
                "Inserisci un link YouTube valido (youtube.com, youtu.be o Shorts).",
                parent=self.winfo_toplevel(),
            )
            return
        if find_ffmpeg(self.sounds) is None:
            self._refresh_ffmpeg()
            messagebox.showerror(
                "FFmpeg non trovato",
                "Per estrarre l'audio di YouTube serve FFmpeg.\n\n"
                "Installa con: winget install Gyan.FFmpeg\n"
                "oppure copia ffmpeg.exe nella cartella dati di StudyFlow.",
                parent=self.winfo_toplevel(),
            )
            return
        self._downloading = True
        self.download_button.configure(state="disabled", text="Preparazione...")
        self.url_entry.configure(state="disabled")
        self.paste_button.configure(state="disabled")
        self.progress.set(0.03)
        self.download_status.configure(
            text="Recupero le informazioni del video...",
            text_color=COLORS.text_muted,
        )

        def worker() -> None:
            try:
                info = probe_video(url)
            except YouTubeAudioError as exc:
                self.app.post(lambda error=str(exc): self._download_failed(error))
                return
            except Exception as exc:
                self.app.post(lambda error=f"Errore imprevisto: {exc}": self._download_failed(error))
                return
            self.app.post(lambda value=dict(info): self._after_probe(url, value))

        threading.Thread(
            target=worker,
            name="StudyFlow-YouTubeProbe",
            daemon=True,
        ).start()

    def _after_probe(self, url: str, info: dict[str, Any]) -> None:
        if not self._downloading:
            return
        title = str(info.get("title") or "Audio YouTube")
        duration = int(info.get("duration") or 0)
        detail = f"Trovato: {title}"
        if duration:
            detail += f" ({duration // 60}:{duration % 60:02d})"
        self.download_status.configure(text=detail, text_color=COLORS.text_muted)
        existing = find_existing_track(self.sounds, title)
        target_title = title
        if existing is not None:
            choice = self._ask_duplicate(existing.name)
            if choice == "Salta":
                self._download_finished(None, "Download annullato: il file esiste gia'.")
                return
            if choice == "Vai a Cartella":
                self._reveal(existing)
                self._download_finished(None, "File esistente mantenuto.")
                return
            target_title = copy_title(self.sounds, title)
            self.download_status.configure(
                text=f"Creo una copia: {target_title}",
                text_color=COLORS.text_muted,
            )
        self._download_worker(url, target_title)

    def _download_worker(self, url: str, title: str) -> None:
        self.download_button.configure(text="Download...")
        self.progress.set(0.05)

        def on_progress(data: dict[str, Any]) -> None:
            self.app.post(lambda value=dict(data): self._update_download(value))

        def worker() -> None:
            try:
                path = download_audio(url, self.sounds, title, progress=on_progress)
            except FFmpegMissingError as exc:
                self.app.post(lambda error=str(exc): self._download_failed(error, ffmpeg=True))
                return
            except YouTubeAudioError as exc:
                self.app.post(lambda error=str(exc): self._download_failed(error))
                return
            except Exception as exc:
                self.app.post(lambda error=f"Errore imprevisto: {exc}": self._download_failed(error))
                return
            self.app.post(lambda value=str(path): self._download_succeeded(value))

        threading.Thread(
            target=worker,
            name="StudyFlow-YouTubeDownload",
            daemon=True,
        ).start()

    def _update_download(self, data: dict[str, Any]) -> None:
        if not self._downloading:
            return
        percent = float(data.get("percent") or 0.0)
        self.progress.set(max(0.03, min(1.0, percent)))
        status = str(data.get("status") or "")
        if status == "finished":
            text = "Conversione in MP3..."
        else:
            speed = float(data.get("speed") or 0.0)
            eta = int(data.get("eta") or 0)
            text = f"Download {percent * 100:.0f}%"
            if speed > 0:
                text += f" · {speed / 1024 / 1024:.1f} MB/s"
            if eta > 0:
                text += f" · {eta // 60}:{eta % 60:02d} rimanenti"
        self.download_status.configure(text=text, text_color=COLORS.text_muted)

    def _download_succeeded(self, path: str) -> None:
        name = Path(path).name
        self._download_finished(path, f"Salvato in sounds: {name}")
        self._refresh_library()
        self.track_combo.set(name)
        self._persist_preferences()
        self.app.toast(f"Audio salvato: {name}")

    def _download_finished(self, path: str | None, message: str) -> None:
        del path
        self._downloading = False
        self.download_button.configure(state="normal", text="Scarica audio")
        self.url_entry.configure(state="normal")
        self.paste_button.configure(state="normal")
        self.download_status.configure(text=message, text_color=COLORS.text_muted)
        if "Salvato" in message:
            self.progress.set(1.0)
        else:
            self.progress.set(0)

    def _download_failed(self, error: str, ffmpeg: bool = False) -> None:
        self._download_finished(None, "Download non riuscito.")
        self.progress.set(0)
        self.download_status.configure(text=error, text_color=COLORS.red)
        if ffmpeg:
            self._refresh_ffmpeg()
        messagebox.showerror("Download non riuscito", error, parent=self.winfo_toplevel())

    # ------------------------------------------------------------------ files
    def _open_folder(self) -> None:
        try:
            os.startfile(str(self.sounds))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo("Cartella sounds", str(self.sounds), parent=self.winfo_toplevel())

    def _reveal(self, path: Path) -> None:
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path.parent))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            self._open_folder()

    def _sounds_hint(self) -> str:
        return f"File audio: {self.sounds}"
