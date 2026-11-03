"""Vista Impostazioni."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..config import APP_VERSION, COLORS
from ..services.backup import (
    BackupError,
    backup_summary,
    export_backup,
    import_backup,
    inspect_backup,
)
from ..services.uac import is_admin
from ..utils import safe_float, safe_int
from .theme import font
from .widgets import Card, PageHeader, separator


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
