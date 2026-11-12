"""Vista Impostazioni."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk

from ..config import APP_VERSION, COLORS
from ..services.backup import (
    BackupError,
    backup_summary,
    export_backup,
    import_backup,
    inspect_backup,
)
from ..services.calendar_sync import (
    SETTING_AUTO_CREATE,
    SETTING_FUTURE_DAYS,
    SETTING_PAST_DAYS,
    format_elapsed,
)
from ..services.google_auth import GoogleAuthError
from ..services.ical_feeds import FEED_COLORS, ICalFeedError
from ..database import normalize_hex
from ..services.uac import is_admin
from ..utils import safe_float, safe_int
from .calendar_help import TAB_API, TAB_ICAL, open_calendar_help
from .theme import font
from .widgets import Card, PageHeader, clear_children, separator


NO_SUBJECT_CHOICE = "— Nessuna (materia dal titolo) —"


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.paths = app.paths
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
            "Impostazioni",
            "Durata delle fasi, comportamento del timer e informazioni sui dati locali.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        content = ctk.CTkFrame(self.scroll, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3, uniform="settings")
        content.grid_columnconfigure(1, weight=2, uniform="settings")

        self._build_timer_card(content)
        self._build_behavior_card(content)
        self._build_storage_card(content)
        self._build_backup_card(content)
        self._build_ical_card(content)
        self._build_google_card(content)
        self._connecting = False
        self._cancel_connect: threading.Event | None = None
        self._calendar_rows: dict[str, dict] = {}

    # ------------------------------------------------------------------ layout
    def _build_timer_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        card.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(
            card,
            text="Ciclo Pomodoro",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(20, 3))
        ctk.CTkLabel(
            card,
            text="I valori vengono applicati alle fasi successive e salvati nel database.",
            font=font(11),
            text_color=COLORS.text_faint,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 16))

        self.work_var = ctk.StringVar(value="25")
        self.short_var = ctk.StringVar(value="5")
        self.long_var = ctk.StringVar(value="15")
        self.cycles_var = ctk.StringVar(value="4")
        fields = (
            ("Lavoro (min)", self.work_var, 22),
            ("Pausa breve (min)", self.short_var, 22),
            ("Pausa lunga (min)", self.long_var, 22),
            ("Cicli prima pausa lunga", self.cycles_var, 22),
        )
        for index, (label, variable, _pad) in enumerate(fields):
            row = 2 + index // 2
            column = index % 2
            frame = ctk.CTkFrame(card, fg_color="transparent")
            frame.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(22 if column == 0 else 8, 22 if column == 1 else 8),
                pady=(0, 14),
            )
            frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                frame,
                text=label,
                font=font(11, "bold"),
                text_color=COLORS.text_muted,
                anchor="w",
            ).pack(anchor="w", pady=(0, 5))
            ctk.CTkEntry(
                frame,
                textvariable=variable,
                height=38,
                fg_color=COLORS.surface_alt,
                border_color=COLORS.border,
                text_color=COLORS.text,
                font=font(13),
                corner_radius=9,
            ).pack(fill="x")

        separator(card).grid(row=4, column=0, columnspan=2, sticky="ew", padx=22, pady=(4, 16))
        self.auto_start_var = ctk.BooleanVar(value=True)
        self.sound_var = ctk.BooleanVar(value=True)
        self._switch(
            card,
            5,
            "Avvia automaticamente la fase successiva",
            "Dopo lavoro o pausa il timer riparte da solo.",
            self.auto_start_var,
        )
        self._switch(
            card,
            6,
            "Segnale sonoro a fine fase",
            "Un breve beep di sistema alla conclusione.",
            self.sound_var,
        )

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", padx=22, pady=(16, 20))
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            buttons,
            text="Salva impostazioni",
            height=40,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._save,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="Valori consigliati",
            height=40,
            width=150,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self._restore_defaults,
        ).grid(row=0, column=1)

    def _switch(
        self,
        parent,
        row: int,
        title: str,
        subtitle: str,
        variable: ctk.BooleanVar,
    ) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=22, pady=5)
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
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=1, column=0, sticky="w")
        ctk.CTkSwitch(
            frame,
            text="",
            width=44,
            variable=variable,
            progress_color=COLORS.accent,
            button_color=COLORS.text,
            button_hover_color=COLORS.text,
            fg_color=COLORS.border,
        ).grid(row=0, column=1, rowspan=2, padx=(12, 0))

    def _build_behavior_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Sicurezza e autorizzazioni",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(20, 6))
        ctk.CTkLabel(
            card,
            text=(
                "StudyFlow usa il manifest asInvoker. L'app non richiede privilegi "
                "amministratore all'avvio: il prompt UAC compare soltanto quando deve "
                "essere modificato il file hosts per bloccare i siti."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            wraplength=330,
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=20)
        admin_text = "Amministratore" if is_admin() else "Utente standard"
        admin_color = COLORS.green if is_admin() else COLORS.text_muted
        ctk.CTkLabel(
            card,
            text=f"●  Sessione corrente: {admin_text}",
            font=font(11, "bold"),
            text_color=admin_color,
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(14, 6))
        ctk.CTkLabel(
            card,
            text=(
                "Il processo guardiano elevato ripristina il file hosts alla chiusura, "
                "in caso di crash, arresto del processo principale o heartbeat assente."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            wraplength=330,
            justify="left",
            anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 16))
        separator(card).grid(row=4, column=0, sticky="ew", padx=20)
        ctk.CTkLabel(
            card,
            text="Nota sui blocchi",
            font=font(12, "bold"),
            text_color=COLORS.text,
        ).grid(row=5, column=0, sticky="w", padx=20, pady=(16, 5))
        ctk.CTkLabel(
            card,
            text=(
                "Il file hosts non e' un firewall: browser con DNS-over-HTTPS o "
                "estensioni VPN possono aggirarlo. StudyFlow registra comunque l'attivita' "
                "del timer e le applicazioni rilevate."
            ),
            font=font(10),
            text_color=COLORS.text_muted,
            wraplength=330,
            justify="left",
            anchor="w",
        ).grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 20))

    def _build_storage_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            card,
            text="Dati locali",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(18, 4))
        ctk.CTkLabel(
            card,
            text="Database SQLite, backup hosts e log restano nella cartella utente.",
            font=font(11),
            text_color=COLORS.text_faint,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 12))
        self.path_label = ctk.CTkLabel(
            card,
            text="",
            font=font(10),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=700,
            justify="left",
        )
        self.path_label.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 6))
        self.size_label = ctk.CTkLabel(
            card,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.size_label.grid(row=3, column=0, sticky="w", padx=22, pady=(0, 18))
        ctk.CTkButton(
            card,
            text="Apri cartella dati",
            height=34,
            width=150,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._open_data_folder,
        ).grid(row=2, column=1, rowspan=2, sticky="e", padx=22)
        ctk.CTkLabel(
            card,
            text=f"StudyFlow v{APP_VERSION} · Python + CustomTkinter + SQLite",
            font=font(10),
            text_color=COLORS.text_faint,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 18))

    def _build_backup_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Backup e trasferimento dati",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Esporta un archivio ZIP con database, impostazioni, materie, "
                "flashcard e trascrizioni. L'importazione sostituisce tutti i dati "
                "attuali e crea automaticamente un backup di sicurezza."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=22)
        self.last_backup_label = ctk.CTkLabel(
            card,
            text="Nessun backup esportato in questa sessione.",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            justify="left",
            wraplength=700,
        )
        self.last_backup_label.grid(
            row=2, column=0, columnspan=2, sticky="w", padx=22, pady=(10, 14)
        )
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", padx=22, pady=(0, 18))
        ctk.CTkButton(
            buttons,
            text="Esporta dati",
            height=38,
            width=150,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=self._export_data,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="Importa dati",
            height=38,
            width=150,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=9,
            command=self._import_data,
        ).pack(side="left")

    def _build_google_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Google Calendar tramite API (avanzato, bidirezionale)",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        self._help_button(card, TAB_API).grid(row=0, column=0, sticky="e", padx=22, pady=(16, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Sincronizzazione bidirezionale con i tuoi calendari Google. Serve un "
                "credentials.json di tipo \"App desktop\" creato in Google Cloud Console "
                "con la Google Calendar API attiva. I token vengono cifrati con Windows "
                "(DPAPI) e non vengono mai inclusi nei backup."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, sticky="w", padx=22)
        self.google_status = ctk.CTkLabel(
            card, text="", font=font(11, "bold"), text_color=COLORS.text_muted, anchor="w"
        )
        self.google_status.grid(row=2, column=0, sticky="w", padx=22, pady=(12, 8))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 12))
        button_style = dict(
            height=36,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=9,
        )
        ctk.CTkButton(
            buttons, text="Carica credentials.json", width=180,
            command=self._load_credentials, **button_style,
        ).pack(side="left", padx=(0, 8))
        self.connect_btn = ctk.CTkButton(
            buttons, text="Connetti account", width=150, command=self._toggle_connection,
            **{**button_style, "fg_color": COLORS.accent, "hover_color": COLORS.accent_hover,
               "text_color": COLORS.bg, "border_width": 0},
        )
        self.connect_btn.pack(side="left", padx=(0, 8))
        self.refresh_calendars_btn = ctk.CTkButton(
            buttons, text="Aggiorna elenco calendari", width=190,
            command=self._refresh_calendar_list, **button_style,
        )
        self.refresh_calendars_btn.pack(side="left")

        separator(card).grid(row=4, column=0, sticky="ew", padx=22, pady=(4, 12))
        ctk.CTkLabel(
            card,
            text="Calendari da sincronizzare e materia abbinata",
            font=font(12, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=5, column=0, sticky="w", padx=22)
        ctk.CTkLabel(
            card,
            text=(
                "I calendari con lo stesso nome di una materia vengono abbinati in "
                "automatico. Un calendario senza materia usa il titolo dell'evento."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=6, column=0, sticky="w", padx=22, pady=(0, 8))
        self.calendars_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.calendars_frame.grid(row=7, column=0, sticky="ew", padx=22)
        self.calendars_frame.grid_columnconfigure(2, weight=1)

        options = ctk.CTkFrame(card, fg_color="transparent")
        options.grid(row=8, column=0, sticky="ew", padx=22, pady=(14, 4))
        self.past_days_var = ctk.StringVar(value="180")
        self.future_days_var = ctk.StringVar(value="365")
        for label, variable in (
            ("Giorni passati", self.past_days_var),
            ("Giorni futuri", self.future_days_var),
        ):
            ctk.CTkLabel(
                options, text=label, font=font(11, "bold"), text_color=COLORS.text_muted
            ).pack(side="left", padx=(0, 6))
            ctk.CTkEntry(
                options,
                textvariable=variable,
                width=70,
                height=32,
                fg_color=COLORS.surface_alt,
                border_color=COLORS.border,
                text_color=COLORS.text,
                font=font(12),
                corner_radius=8,
            ).pack(side="left", padx=(0, 18))
        self.auto_create_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            options,
            text="Crea un calendario Google per ogni nuova materia",
            variable=self.auto_create_var,
            font=font(11),
            text_color=COLORS.text_muted,
            progress_color=COLORS.accent,
            button_color=COLORS.text,
            button_hover_color=COLORS.text,
            fg_color=COLORS.border,
        ).pack(side="left")

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.grid(row=9, column=0, sticky="ew", padx=22, pady=(10, 18))
        ctk.CTkButton(
            footer,
            text="Salva e sincronizza",
            height=38,
            width=170,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=self._save_google,
        ).pack(side="left")
        self.google_sync_label = ctk.CTkLabel(
            footer, text="", font=font(10), text_color=COLORS.text_faint
        )
        self.google_sync_label.pack(side="left", padx=14)

    def _build_ical_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Calendari tramite indirizzo iCal (senza API)",
            font=font(16, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 4))
        self._help_button(card, TAB_ICAL).grid(row=0, column=0, sticky="e", padx=22, pady=(16, 4))
        ctk.CTkLabel(
            card,
            text=(
                "Per ogni calendario Google incolla il suo \"Indirizzo segreto in formato "
                "iCal\" (Google Calendar dal computer > Impostazioni e condivisione > "
                "Integra calendario). Nessun login: gli eventi vengono letti all'avvio e con "
                "\"Sincronizza Ora\". E' in sola lettura: gli eventi creati in StudyFlow "
                "restano locali. Gli indirizzi sono cifrati e non finiscono nel backup."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, sticky="w", padx=22)
        self.feeds_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.feeds_frame.grid(row=2, column=0, sticky="ew", padx=22, pady=(12, 6))
        self.feeds_frame.grid_columnconfigure(1, weight=1)

        add_row = ctk.CTkFrame(card, fg_color="transparent")
        add_row.grid(row=3, column=0, sticky="ew", padx=22, pady=(6, 18))
        add_row.grid_columnconfigure(0, weight=1)
        self.feed_url_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            add_row,
            textvariable=self.feed_url_var,
            placeholder_text="https://calendar.google.com/calendar/ical/.../private-.../basic.ics",
            height=36,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(11),
            corner_radius=9,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.feed_subject_var = ctk.StringVar(value=NO_SUBJECT_CHOICE)
        self.feed_subject_combo = ctk.CTkComboBox(
            add_row,
            values=[NO_SUBJECT_CHOICE],
            variable=self.feed_subject_var,
            width=230,
            height=36,
            font=font(11),
            dropdown_font=font(11),
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            text_color=COLORS.text,
            corner_radius=9,
        )
        self.feed_subject_combo.grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            add_row,
            text="Aggiungi",
            width=100,
            height=36,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=9,
            command=self._add_feed,
        ).grid(row=0, column=2)

    def _help_button(self, parent, tab: str) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text="?  Come Connettere Calendario",
            width=200,
            height=30,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.blue,
            corner_radius=8,
            command=lambda: open_calendar_help(self, self.app, initial=tab),
        )

    # --------------------------------------------------------------- ical
    def _subject_choices(self) -> list[str]:
        return [NO_SUBJECT_CHOICE, *self.db.list_subject_tags()]

    def _render_ical(self) -> None:
        clear_children(self.feeds_frame)
        store = self._sync.ical
        feeds = store.list_feeds()
        choices = self._subject_choices()
        self.feed_subject_combo.configure(values=choices)
        if not feeds:
            ctk.CTkLabel(
                self.feeds_frame,
                text="Nessun indirizzo iCal aggiunto. Vedi il README per come trovarlo.",
                font=font(11),
                text_color=COLORS.text_faint,
            ).grid(row=0, column=0, columnspan=4, sticky="w", pady=2)
            return
        for row, feed in enumerate(feeds):
            ctk.CTkButton(
                self.feeds_frame,
                text="",
                width=24,
                height=24,
                corner_radius=12,
                fg_color=feed["color"],
                hover_color=feed["color"],
                border_width=2,
                border_color=COLORS.bg,
                command=lambda value=feed["id"]: self._pick_feed_color(value),
            ).grid(row=row, column=0, padx=(0, 10), pady=4, sticky="n")
            info = ctk.CTkFrame(self.feeds_frame, fg_color="transparent")
            info.grid(row=row, column=1, sticky="ew", pady=4)
            ctk.CTkLabel(
                info,
                text=feed["name"] or "Calendario (nome dopo la prima sincronizzazione)",
                font=font(11, "bold"),
                text_color=COLORS.text,
                anchor="w",
            ).pack(anchor="w")
            if not feed["has_url"]:
                detail, detail_color = "Indirizzo mancante su questo PC: rimuovi e aggiungi di nuovo.", COLORS.amber
            elif feed["error"]:
                detail, detail_color = feed["error"], COLORS.red
            else:
                synced = format_elapsed(feed["last_ok"]) if feed["last_ok"] else "non ancora sincronizzato"
                detail, detail_color = f"{feed['url_hint']} · {synced}", COLORS.text_faint
            ctk.CTkLabel(
                info,
                text=detail,
                font=font(9),
                text_color=detail_color,
                anchor="w",
                wraplength=430,
                justify="left",
            ).pack(anchor="w")
            current = feed["subject"] or NO_SUBJECT_CHOICE
            subject_var = ctk.StringVar(value=current)
            ctk.CTkComboBox(
                self.feeds_frame,
                values=choices if current in choices else [*choices, current],
                variable=subject_var,
                state="readonly",
                width=230,
                height=30,
                font=font(11),
                dropdown_font=font(11),
                fg_color=COLORS.surface_alt,
                border_color=COLORS.border,
                button_color=COLORS.surface_hover,
                button_hover_color=COLORS.border,
                dropdown_fg_color=COLORS.surface,
                text_color=COLORS.text,
                corner_radius=8,
                command=lambda value, feed_id=feed["id"]: self._set_feed_subject(feed_id, value),
            ).grid(row=row, column=2, padx=(8, 8), pady=4, sticky="n")
            ctk.CTkButton(
                self.feeds_frame,
                text="Rimuovi",
                width=80,
                height=30,
                font=font(10),
                fg_color="transparent",
                hover_color=COLORS.red_dark,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.red,
                corner_radius=8,
                command=lambda value=feed["id"]: self._remove_feed(value),
            ).grid(row=row, column=3, pady=4, sticky="n")

    def _resync_feeds(self) -> None:
        if not self._sync.sync_async("settings"):
            self.app.toast("Sincronizzazione gia' in corso: riprova tra poco.")
        self._render_google()

    def _add_feed(self) -> None:
        subject = self.feed_subject_var.get().strip()
        if subject == NO_SUBJECT_CHOICE:
            subject = ""
        if subject and self._sync.ical.for_subject(subject) is not None:
            messagebox.showerror(
                "Materia gia' collegata",
                f"La materia \"{subject}\" ha gia' un indirizzo iCal.",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            self._sync.ical.add_feed(self.feed_url_var.get(), subject)
        except ICalFeedError as exc:
            messagebox.showerror("Indirizzo non valido", str(exc), parent=self.winfo_toplevel())
            return
        except Exception as exc:
            self.app.logger.exception("Aggiunta indirizzo iCal non riuscita.")
            messagebox.showerror("Indirizzo non valido", str(exc), parent=self.winfo_toplevel())
            return
        self.feed_url_var.set("")
        self.feed_subject_var.set(NO_SUBJECT_CHOICE)
        self.app.toast("Indirizzo iCal aggiunto. Sincronizzazione in corso...")
        self._resync_feeds()

    def _set_feed_subject(self, feed_id: str, value: str) -> None:
        subject = "" if value == NO_SUBJECT_CHOICE else value
        other = self._sync.ical.for_subject(subject) if subject else None
        if other is not None and other["id"] != feed_id:
            messagebox.showerror(
                "Materia gia' collegata",
                f"La materia \"{subject}\" ha gia' un indirizzo iCal.",
                parent=self.winfo_toplevel(),
            )
            self._render_ical()
            return
        self._sync.ical.update_feed(feed_id, subject=subject)
        self._resync_feeds()

    def _pick_feed_color(self, feed_id: str) -> None:
        feed = self._sync.ical.get(feed_id)
        if feed is None:
            return
        _ColorPicker(self, feed["color"], lambda color: self._set_feed_color(feed_id, color))

    def _set_feed_color(self, feed_id: str, color: str) -> None:
        self._sync.ical.update_feed(feed_id, color=color)
        self._resync_feeds()

    def _remove_feed(self, feed_id: str) -> None:
        feed = self._sync.ical.get(feed_id)
        if feed is None:
            return
        if not messagebox.askyesno(
            "Rimuovere il calendario?",
            f"\"{feed['name'] or feed['url_hint']}\" verra' rimosso e i suoi eventi spariranno "
            "dall'agenda alla prossima sincronizzazione. Sessioni e materiali restano. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        self._sync.ical.remove_feed(feed_id)
        self._resync_feeds()

    # ------------------------------------------------------------- google
    @property
    def _sync(self):
        return self.app.google_sync

    def _render_google(self) -> None:
        sync = self._sync
        auth = sync.auth
        if self._connecting:
            status, color = "In attesa del consenso nel browser...", COLORS.amber
            button_text = "Annulla"
        elif auth.is_connected():
            email = auth.account_email()
            status = f"●  Connesso{(' come ' + email) if email else ''}"
            color = COLORS.green
            button_text = "Disconnetti"
        elif auth.has_client():
            status, color = "●  credentials.json caricato · account non collegato", COLORS.amber
            button_text = "Connetti account"
        else:
            status, color = "●  Non configurato: carica il file credentials.json", COLORS.text_faint
            button_text = "Connetti account"
        self.google_status.configure(text=status, text_color=color)
        self.connect_btn.configure(
            text=button_text,
            state="normal" if (auth.has_client() or self._connecting) else "disabled",
        )
        self.refresh_calendars_btn.configure(
            state="normal" if auth.is_connected() and not sync.is_syncing else "disabled"
        )
        settings = self.db.get_settings()
        self.past_days_var.set(settings.get(SETTING_PAST_DAYS, "180"))
        self.future_days_var.set(settings.get(SETTING_FUTURE_DAYS, "365"))
        self.auto_create_var.set(self._bool(settings.get(SETTING_AUTO_CREATE, "0")))
        last = format_elapsed(sync.last_sync_at())
        self.google_sync_label.configure(
            text="Sincronizzazione in corso..." if sync.is_syncing else f"Ultima sincronizzazione: {last}"
        )
        self._render_calendar_rows()
        self._render_ical()

    def _render_calendar_rows(self) -> None:
        clear_children(self.calendars_frame)
        self._calendar_rows = {}
        sync = self._sync
        calendars = sync.cached_calendars() if sync.auth.is_connected() else []
        if not calendars:
            ctk.CTkLabel(
                self.calendars_frame,
                text=(
                    "Collega l'account per vedere i tuoi calendari."
                    if not sync.auth.is_connected()
                    else "Nessun calendario in elenco: premi \"Aggiorna elenco calendari\"."
                ),
                font=font(11),
                text_color=COLORS.text_faint,
            ).grid(row=0, column=0, columnspan=4, sticky="w", pady=4)
            return
        enabled = set(sync.enabled_calendars())
        mapping = self.db.subject_calendar_map()
        subjects = self.db.list_subject_tags()
        choices = [NO_SUBJECT_CHOICE, *subjects]
        for row, calendar in enumerate(calendars):
            calendar_id = str(calendar["id"])
            color = str(calendar.get("backgroundColor") or COLORS.blue)
            enabled_var = ctk.BooleanVar(value=calendar_id in enabled)
            ctk.CTkCheckBox(
                self.calendars_frame,
                text="",
                width=24,
                variable=enabled_var,
                fg_color=COLORS.accent,
                hover_color=COLORS.accent_hover,
                border_color=COLORS.border,
                checkbox_width=18,
                checkbox_height=18,
                corner_radius=5,
            ).grid(row=row, column=0, padx=(0, 6), pady=3)
            ctk.CTkFrame(
                self.calendars_frame,
                width=18,
                height=18,
                fg_color=color,
                corner_radius=9,
            ).grid(row=row, column=1, padx=(0, 10), pady=3)
            readonly = str(calendar.get("accessRole") or "") not in {"owner", "writer"}
            name = str(calendar.get("summary") or calendar_id)
            if calendar.get("primary"):
                name += "  (principale)"
            if readonly:
                name += "  · sola lettura"
            ctk.CTkLabel(
                self.calendars_frame,
                text=name,
                font=font(11),
                text_color=COLORS.text,
                anchor="w",
            ).grid(row=row, column=2, sticky="w", pady=3)
            current = (mapping.get(calendar_id) or {}).get("name") or NO_SUBJECT_CHOICE
            subject_var = ctk.StringVar(value=current)
            ctk.CTkComboBox(
                self.calendars_frame,
                values=choices if current in choices else [*choices, current],
                variable=subject_var,
                state="readonly",
                width=250,
                height=30,
                font=font(11),
                dropdown_font=font(11),
                fg_color=COLORS.surface_alt,
                border_color=COLORS.border,
                button_color=COLORS.surface_hover,
                button_hover_color=COLORS.border,
                dropdown_fg_color=COLORS.surface,
                text_color=COLORS.text,
                corner_radius=8,
            ).grid(row=row, column=3, sticky="e", pady=3)
            self._calendar_rows[calendar_id] = {
                "enabled": enabled_var,
                "subject": subject_var,
                "color": color,
                "mapped": mapping.get(calendar_id, {}).get("name"),
            }

    def on_sync_event(self, kind: str, data: dict) -> None:
        if not self.winfo_exists():
            return
        if kind in {"sync_started", "sync_finished"}:
            self._render_google()
            if kind == "sync_finished" and data.get("error") and data.get("reason") in {"connect", "settings"}:
                self.google_sync_label.configure(text=f"Errore: {data['error']}")

    def _load_credentials(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="Seleziona credentials.json (client OAuth \"App desktop\")",
            filetypes=[("Credenziali OAuth", "*.json"), ("Tutti i file", "*.*")],
        )
        if not path:
            return
        try:
            self._sync.auth.load_credentials_file(path)
        except GoogleAuthError as exc:
            messagebox.showerror("Credenziali non valide", str(exc), parent=self.winfo_toplevel())
            return
        except Exception as exc:
            self.app.logger.exception("Caricamento credentials.json non riuscito.")
            messagebox.showerror("Credenziali non valide", str(exc), parent=self.winfo_toplevel())
            return
        self._render_google()
        self.app.toast("credentials.json caricato. Ora collega l'account.")

    def _toggle_connection(self) -> None:
        auth = self._sync.auth
        if self._connecting:
            if self._cancel_connect is not None:
                self._cancel_connect.set()
            return
        if auth.is_connected():
            if not messagebox.askyesno(
                "Disconnettere Google Calendar?",
                "I token verranno revocati ed eliminati da questo PC. Gli eventi gia' "
                "sincronizzati restano in StudyFlow; le modifiche in coda verranno inviate "
                "quando ricolleghi l'account. Continuare?",
                parent=self.winfo_toplevel(),
            ):
                return

            def disconnect_worker() -> None:
                try:
                    auth.disconnect(revoke=True)
                except Exception:
                    self.app.logger.exception("Disconnessione Google non riuscita.")
                finally:
                    self.db.close_current_thread()
                self.app.post(self._render_google)

            threading.Thread(target=disconnect_worker, name="StudyFlow-GoogleDisconnect", daemon=True).start()
            return
        self._connecting = True
        self._cancel_connect = threading.Event()
        cancel = self._cancel_connect
        self._render_google()

        def connect_worker() -> None:
            error = ""
            try:
                auth.authorize(cancel_event=cancel)
            except Exception as exc:
                error = str(exc)
                if not isinstance(exc, GoogleAuthError):
                    self.app.logger.exception("Collegamento Google non riuscito.")
            finally:
                self.db.close_current_thread()
            self.app.post(lambda: self._connect_done(error))

        threading.Thread(target=connect_worker, name="StudyFlow-GoogleConnect", daemon=True).start()

    def _connect_done(self, error: str) -> None:
        self._connecting = False
        self._cancel_connect = None
        if not self.winfo_exists():
            return
        self._render_google()
        if error:
            if error != "Collegamento annullato.":
                messagebox.showerror("Collegamento non riuscito", error, parent=self.winfo_toplevel())
            return
        self.app.toast("Account Google collegato. Sincronizzazione in corso...")
        self._sync.sync_async("connect")

    def _refresh_calendar_list(self) -> None:
        if not self._sync.sync_async("connect"):
            self.app.toast("Sincronizzazione gia' in corso.")
        self._render_google()

    def _save_google(self) -> None:
        past = safe_int(self.past_days_var.get(), -1)
        future = safe_int(self.future_days_var.get(), -1)
        if not 1 <= past <= 3650 or not 1 <= future <= 3650:
            messagebox.showerror(
                "Valore non valido",
                "La finestra di sincronizzazione deve essere tra 1 e 3650 giorni.",
                parent=self.winfo_toplevel(),
            )
            return
        chosen: dict[str, str] = {}
        for calendar_id, row in self._calendar_rows.items():
            subject = row["subject"].get().strip()
            if not subject or subject == NO_SUBJECT_CHOICE:
                continue
            key = subject.casefold()
            if key in chosen:
                messagebox.showerror(
                    "Abbinamento non valido",
                    f"La materia \"{subject}\" e' abbinata a piu' calendari. "
                    "Ogni materia puo' avere un solo calendario Google.",
                    parent=self.winfo_toplevel(),
                )
                return
            chosen[key] = calendar_id
        try:
            for calendar_id, row in self._calendar_rows.items():
                subject = row["subject"].get().strip()
                if subject and subject != NO_SUBJECT_CHOICE:
                    self.db.link_subject_calendar(subject, calendar_id, row["color"])
                elif row["mapped"]:
                    self.db.unlink_calendar(calendar_id)
            self._sync.set_enabled_calendars(
                calendar_id for calendar_id, row in self._calendar_rows.items() if row["enabled"].get()
            )
            self.db.set_settings(
                {
                    SETTING_PAST_DAYS: str(past),
                    SETTING_FUTURE_DAYS: str(future),
                    SETTING_AUTO_CREATE: "1" if self.auto_create_var.get() else "0",
                }
            )
        except Exception as exc:
            self.app.logger.exception("Salvataggio impostazioni Google non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self.winfo_toplevel())
            return
        if self._sync.is_connected():
            self._sync.sync_async("settings")
        self._render_google()
        self.app.toast("Impostazioni Google Calendar salvate.")

    # ------------------------------------------------------------------ actions
    def on_show(self) -> None:
        settings = self.db.get_settings()
        self.work_var.set(settings.get("pomodoro_work", "25"))
        self.short_var.set(settings.get("pomodoro_short", "5"))
        self.long_var.set(settings.get("pomodoro_long", "15"))
        self.cycles_var.set(settings.get("pomodoro_cycles", "4"))
        self.auto_start_var.set(self._bool(settings.get("auto_start_next", "1")))
        self.sound_var.set(self._bool(settings.get("notification_sound", "1")))
        self.path_label.configure(text=str(self.paths["db"]))
        size = self.paths["db"].stat().st_size if self.paths["db"].exists() else 0
        self.size_label.configure(text=f"Dimensione database: {size / 1024:.1f} KB")
        self._render_google()

    @staticmethod
    def _bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _save(self) -> None:
        work = safe_float(self.work_var.get(), -1)
        short = safe_float(self.short_var.get(), -1)
        long = safe_float(self.long_var.get(), -1)
        cycles = safe_int(self.cycles_var.get(), -1)
        if not 1 <= work <= 180:
            messagebox.showerror("Valore non valido", "Il tempo di lavoro deve essere tra 1 e 180 minuti.", parent=self.winfo_toplevel())
            return
        if not 1 <= short <= 60:
            messagebox.showerror("Valore non valido", "La pausa breve deve essere tra 1 e 60 minuti.", parent=self.winfo_toplevel())
            return
        if not 1 <= long <= 120:
            messagebox.showerror("Valore non valido", "La pausa lunga deve essere tra 1 e 120 minuti.", parent=self.winfo_toplevel())
            return
        if not 1 <= cycles <= 12:
            messagebox.showerror("Valore non valido", "I cicli devono essere tra 1 e 12.", parent=self.winfo_toplevel())
            return
        self.db.set_settings(
            {
                "pomodoro_work": f"{work:g}",
                "pomodoro_short": f"{short:g}",
                "pomodoro_long": f"{long:g}",
                "pomodoro_cycles": str(cycles),
                "auto_start_next": "1" if self.auto_start_var.get() else "0",
                "notification_sound": "1" if self.sound_var.get() else "0",
            }
        )
        self.app.pomodoro.reload_config()
        self.app.blocker.reload_config()
        self.app.toast("Impostazioni salvate.")

    def _restore_defaults(self) -> None:
        self.work_var.set("25")
        self.short_var.set("5")
        self.long_var.set("15")
        self.cycles_var.set("4")
        self.auto_start_var.set(True)
        self.sound_var.set(True)

    def _export_data(self) -> None:
        default_name = f"StudyFlow-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
        path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="Esporta dati StudyFlow",
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[("Backup StudyFlow", "*.zip"), ("Tutti i file", "*.*")],
        )
        if not path:
            return
        try:
            result = export_backup(self.db, self.paths, path)
        except BackupError as exc:
            messagebox.showerror("Esportazione non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        except Exception as exc:
            self.app.logger.exception("Esportazione dati non riuscita.")
            messagebox.showerror("Esportazione non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        self.last_backup_label.configure(text=f"Ultimo backup: {result['path']}")
        messagebox.showinfo(
            "Esportazione completata",
            "Backup creato correttamente.\n\n"
            f"{result['path']}\n\n"
            f"Contenuto: {backup_summary(result['counts'])}; "
            f"{result['transcripts']} trascrizioni.",
            parent=self.winfo_toplevel(),
        )

    def _import_data(self) -> None:
        snapshot = self.app.pomodoro.snapshot()
        if snapshot.get("status") in {"running", "paused"}:
            messagebox.showwarning(
                "Sessione in corso",
                "Termina o azzera il Pomodoro prima di importare un backup.",
                parent=self.winfo_toplevel(),
            )
            return
        if self.app.transcriptions.current_job() is not None:
            messagebox.showwarning(
                "Trascrizione in corso",
                "Attendi la fine o annulla la trascrizione prima di importare un backup.",
                parent=self.winfo_toplevel(),
            )
            return
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="Importa dati StudyFlow",
            filetypes=[("Backup StudyFlow", "*.zip"), ("Tutti i file", "*.*")],
        )
        if not path:
            return
        try:
            manifest, _payload = inspect_backup(path)
        except BackupError as exc:
            messagebox.showerror("Backup non valido", str(exc), parent=self.winfo_toplevel())
            return
        except Exception as exc:
            self.app.logger.exception("Lettura backup non riuscita.")
            messagebox.showerror("Backup non valido", str(exc), parent=self.winfo_toplevel())
            return
        counts = manifest.get("counts") or {}
        exported_at = str(manifest.get("exported_at") or "data sconosciuta")
        if not messagebox.askyesno(
            "Sostituire tutti i dati?",
            "L'importazione sostituira' sessioni, materiali, calendario, video, "
            "materie, flashcard, blacklist e impostazioni con il contenuto del "
            "backup.\n\n"
            f"Backup del: {exported_at}\n"
            f"Contenuto: {backup_summary(counts)}.\n\n"
            "Prima dell'importazione verra' creato un backup automatico dei dati "
            "attuali. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            result = import_backup(self.db, self.paths, path)
        except BackupError as exc:
            messagebox.showerror("Importazione non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        except Exception as exc:
            self.app.logger.exception("Importazione dati non riuscita.")
            messagebox.showerror("Importazione non riuscita", str(exc), parent=self.winfo_toplevel())
            return
        self.app.refresh_after_import()
        self.on_show()
        self.last_backup_label.configure(
            text=f"Backup di sicurezza: {result['safety_backup']}"
        )
        messagebox.showinfo(
            "Importazione completata",
            "Dati ripristinati correttamente.\n\n"
            f"Trascrizioni ripristinate: {result['transcripts']}\n"
            f"Backup dei dati precedenti: {result['safety_backup']}",
            parent=self.winfo_toplevel(),
        )

    def _open_data_folder(self) -> None:
        folder = Path(self.paths["data"])
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            messagebox.showinfo("Cartella dati", str(folder), parent=self.winfo_toplevel())


class _ColorPicker(ctk.CTkToplevel):
    """Piccola tavolozza con i colori dei calendari Google, piu' un colore libero."""

    def __init__(self, view: SettingsView, current: str, on_pick) -> None:
        super().__init__(view.winfo_toplevel())
        self._on_pick = on_pick
        self.title("Colore del calendario")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(padx=18, pady=(18, 8))
        for index, color in enumerate(FEED_COLORS):
            ctk.CTkButton(
                grid,
                text="",
                width=30,
                height=30,
                corner_radius=15,
                fg_color=color,
                hover_color=color,
                border_width=2,
                border_color=COLORS.text if color == current else COLORS.bg,
                command=lambda value=color: self._choose(value),
            ).grid(row=index // 6, column=index % 6, padx=4, pady=4)
        ctk.CTkButton(
            self,
            text="Altro colore...",
            height=32,
            font=font(11),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=self._custom,
        ).pack(fill="x", padx=18, pady=(0, 18))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.after(50, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _custom(self) -> None:
        _rgb, value = colorchooser.askcolor(parent=self, title="Scegli un colore")
        if value:
            self._choose(value)

    def _choose(self, color: str) -> None:
        clean = normalize_hex(color)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if clean:
            self._on_pick(clean)
