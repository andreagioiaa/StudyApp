"""Vista Blocco Distrazioni e blacklist."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from ..config import COLORS
from ..utils import normalize_domain, normalize_process_name
from .theme import font
from .widgets import Card, PageHeader, StatCard, clear_children, separator


class BlockerView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.blocker = app.blocker
        self._poll_job: str | None = None
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
            "Blocco Distrazioni",
            "Siti web tramite file hosts, applicazioni tramite monitoraggio dei processi.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        self._build_status_cards()
        self._build_options()
        self._build_blacklists()
        self._build_notes()

    # ------------------------------------------------------------------ layout
    def _build_status_cards(self) -> None:
        row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew")
        for column in range(3):
            row.grid_columnconfigure(column, weight=1, uniform="status")
        self.sites_card = StatCard(
            row,
            "BLOCCO SITI",
            "In attesa",
            "Si attiva con il Pomodoro",
            COLORS.blue,
        )
        self.sites_card.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.apps_card = StatCard(
            row,
            "BLOCCO APP",
            "In attesa",
            "Monitoraggio processi",
            COLORS.purple,
        )
        self.apps_card.grid(row=0, column=1, sticky="ew", padx=8)
        self.attempts_card = StatCard(
            row,
            "TENTATIVI BLOCCATI",
            "0",
            "Sessione corrente",
            COLORS.amber,
        )
        self.attempts_card.grid(row=0, column=2, sticky="ew", padx=(8, 0))

    def _build_options(self) -> None:
        card = Card(self.scroll)
        card.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Comportamento del blocco",
            font=font(15, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w")
        self.retry_button = ctk.CTkButton(
            header,
            text="Riprova autorizzazione siti",
            width=190,
            height=32,
            font=font(11, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.amber,
            text_color=COLORS.amber,
            corner_radius=8,
            command=self._retry_sites,
        )
        self.retry_button.grid(row=0, column=1, sticky="e")

        self.sites_enabled_var = ctk.BooleanVar(value=True)
        self.apps_enabled_var = ctk.BooleanVar(value=True)
        self.terminate_var = ctk.BooleanVar(value=True)
        self.trap_var = ctk.BooleanVar(value=True)
        self._switch(
            card,
            1,
            "Blocca siti web con il file hosts",
            "Reindirizza i domini a 127.0.0.1 durante il lavoro.",
            self.sites_enabled_var,
            lambda: self._save_option("block_sites_enabled", self.sites_enabled_var.get()),
        )
        self._switch(
            card,
            2,
            "Monitora applicazioni in blacklist",
            "Scansione leggera dei processi attivi ogni pochi secondi.",
            self.apps_enabled_var,
            lambda: self._save_option("block_apps_enabled", self.apps_enabled_var.get()),
        )
        self._switch(
            card,
            3,
            "Termina le applicazioni rilevate",
            "Chiude i processi bloccati. Salva il lavoro nelle app prima di iniziare.",
            self.terminate_var,
            lambda: self._save_option("terminate_blocked_apps", self.terminate_var.get()),
        )
        self._switch(
            card,
            4,
            "Intercetta i tentativi web",
            "Listener locale su 127.0.0.1:80/443 per registrare i domini contattati.",
            self.trap_var,
            lambda: self._save_option("web_trap_enabled", self.trap_var.get()),
        )

        self.options_note = ctk.CTkLabel(
            card,
            text="Le modifiche si applicano subito alla sessione in corso.",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.options_note.grid(row=5, column=0, sticky="w", padx=20, pady=(2, 18))

    def _switch(self, parent, row: int, title: str, subtitle: str, variable, command) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=20, pady=5)
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
            command=command,
            progress_color=COLORS.accent,
            button_color=COLORS.text,
            button_hover_color=COLORS.text,
            fg_color=COLORS.border,
        ).grid(row=0, column=1, rowspan=2, padx=(12, 0))

    def _build_blacklists(self) -> None:
        row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        row.grid_columnconfigure(0, weight=1, uniform="lists")
        row.grid_columnconfigure(1, weight=1, uniform="lists")

        web_card = Card(row)
        web_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        web_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            web_card,
            text="Siti web bloccati",
            font=font(15, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            web_card,
            text="Inserisci il dominio base, ad esempio youtube.com.",
            font=font(10),
            text_color=COLORS.text_faint,
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 10))
        self.sites_list = ctk.CTkScrollableFrame(
            web_card,
            height=285,
            fg_color=COLORS.surface_alt,
            corner_radius=10,
            scrollbar_button_color=COLORS.border,
        )
        self.sites_list.grid(row=2, column=0, sticky="ew", padx=18)
        add_row = ctk.CTkFrame(web_card, fg_color="transparent")
        add_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 16))
        add_row.grid_columnconfigure(0, weight=1)
        self.site_entry = ctk.CTkEntry(
            add_row,
            height=36,
            placeholder_text="es. instagram.com",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=8,
        )
        self.site_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.site_entry.bind("<Return>", lambda _event: self._add_site())
        ctk.CTkButton(
            add_row,
            text="Aggiungi",
            width=88,
            height=36,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=8,
            command=self._add_site,
        ).grid(row=0, column=1)

        app_card = Card(row)
        app_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        app_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            app_card,
            text="Applicazioni bloccate",
            font=font(15, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            app_card,
            text="Il nome processo e' senza .exe (es. discord).",
            font=font(10),
            text_color=COLORS.text_faint,
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 10))
        self.apps_list = ctk.CTkScrollableFrame(
            app_card,
            height=285,
            fg_color=COLORS.surface_alt,
            corner_radius=10,
            scrollbar_button_color=COLORS.border,
        )
        self.apps_list.grid(row=2, column=0, sticky="ew", padx=18)
        add_app = ctk.CTkFrame(app_card, fg_color="transparent")
        add_app.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 16))
        add_app.grid_columnconfigure((0, 1), weight=1)
        self.app_name_entry = ctk.CTkEntry(
            add_app,
            height=36,
            placeholder_text="Nome visualizzato",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=8,
        )
        self.app_name_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.app_process_entry = ctk.CTkEntry(
            add_app,
            height=36,
            placeholder_text="processo",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=8,
        )
        self.app_process_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.app_process_entry.bind("<Return>", lambda _event: self._add_app())
        ctk.CTkButton(
            add_app,
            text="+",
            width=40,
            height=36,
            font=font(16, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=8,
            command=self._add_app,
        ).grid(row=0, column=2, padx=(6, 0))

    def _build_notes(self) -> None:
        card = Card(self.scroll)
        card.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Integrita' del sistema",
            font=font(14, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))
        self.residual_label = ctk.CTkLabel(
            card,
            text="",
            font=font(10),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=760,
            justify="left",
        )
        self.residual_label.grid(row=1, column=0, sticky="ew", padx=20)
        ctk.CTkButton(
            card,
            text="Ripristina file hosts",
            height=34,
            width=170,
            font=font(11, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.red,
            text_color=COLORS.red,
            corner_radius=8,
            command=self._restore_hosts,
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=20, pady=16)
        ctk.CTkLabel(
            card,
            text=(
                "StudyFlow salva un backup del file hosts originale in %LOCALAPPDATA%\\StudyFlow. "
                "La sezione gestita viene rimossa automaticamente; il resto del file non viene "
                "mai sovrascritto. Se un crash lascia una sezione residua, usa il pulsante qui a destra."
            ),
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=760,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(8, 18))

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        settings = self.db.get_settings()
        self.sites_enabled_var.set(self._bool(settings.get("block_sites_enabled", "1")))
        self.apps_enabled_var.set(self._bool(settings.get("block_apps_enabled", "1")))
        self.terminate_var.set(self._bool(settings.get("terminate_blocked_apps", "1")))
        self.trap_var.set(self._bool(settings.get("web_trap_enabled", "1")))
        self._refresh_lists()
        self._refresh_status()
        self._start_poll()

    def on_hide(self) -> None:
        self._stop_poll()

    @staticmethod
    def _bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _refresh_lists(self) -> None:
        clear_children(self.sites_list)
        sites = self.db.list_blocked_sites()
        if not sites:
            ctk.CTkLabel(
                self.sites_list,
                text="Nessun sito configurato.",
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=12, pady=12)
        for site in sites:
            row = ctk.CTkFrame(self.sites_list, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row,
                text=str(site["domain"]),
                font=font(11),
                text_color=COLORS.text,
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=6, pady=5)
            variable = ctk.BooleanVar(value=bool(site["enabled"]))
            ctk.CTkSwitch(
                row,
                text="",
                width=38,
                variable=variable,
                progress_color=COLORS.accent,
                button_color=COLORS.text,
                fg_color=COLORS.border,
                command=lambda value=variable, site_id=int(site["id"]): self._toggle_site(
                    site_id, value.get()
                ),
            ).grid(row=0, column=1, padx=4)
            ctk.CTkButton(
                row,
                text="×",
                width=28,
                height=28,
                font=font(14, "bold"),
                fg_color="transparent",
                hover_color=COLORS.red_dark,
                text_color=COLORS.text_faint,
                corner_radius=6,
                command=lambda site_id=int(site["id"]), name=str(site["domain"]): self._delete_site(
                    site_id, name
                ),
            ).grid(row=0, column=2, padx=(0, 2))

        clear_children(self.apps_list)
        apps = self.db.list_blocked_apps()
        if not apps:
            ctk.CTkLabel(
                self.apps_list,
                text="Nessuna applicazione configurata.",
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=12, pady=12)
        for app_row in apps:
            row = ctk.CTkFrame(self.apps_list, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=2)
            row.grid_columnconfigure(0, weight=1)
            labels = ctk.CTkFrame(row, fg_color="transparent")
            labels.grid(row=0, column=0, sticky="w", padx=6, pady=3)
            ctk.CTkLabel(
                labels,
                text=str(app_row["display_name"]),
                font=font(11, "bold"),
                text_color=COLORS.text,
                anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                labels,
                text=f"{app_row['process_name']}.exe",
                font=font(9),
                text_color=COLORS.text_faint,
                anchor="w",
            ).pack(anchor="w")
            variable = ctk.BooleanVar(value=bool(app_row["enabled"]))
            ctk.CTkSwitch(
                row,
                text="",
                width=38,
                variable=variable,
                progress_color=COLORS.accent,
                button_color=COLORS.text,
                fg_color=COLORS.border,
                command=lambda value=variable, app_id=int(app_row["id"]): self._toggle_app(
                    app_id, value.get()
                ),
            ).grid(row=0, column=1, padx=4)
            ctk.CTkButton(
                row,
                text="×",
                width=28,
                height=28,
                font=font(14, "bold"),
                fg_color="transparent",
                hover_color=COLORS.red_dark,
                text_color=COLORS.text_faint,
                corner_radius=6,
                command=lambda app_id=int(app_row["id"]), name=str(app_row["display_name"]): self._delete_app(
                    app_id, name
                ),
            ).grid(row=0, column=2, padx=(0, 2))

    # ------------------------------------------------------------------ actions
    def _save_option(self, key: str, value: bool) -> None:
        self.db.set_setting(key, "1" if value else "0")
        self.blocker.reload_config()
        self.app.toast("Impostazione di blocco aggiornata.")

    def _add_site(self) -> None:
        try:
            domain = normalize_domain(self.site_entry.get())
        except ValueError as exc:
            messagebox.showerror("Dominio non valido", str(exc), parent=self.winfo_toplevel())
            return
        self.db.add_blocked_site(domain)
        self.site_entry.delete(0, "end")
        self.blocker.reload_config()
        self._refresh_lists()

    def _toggle_site(self, site_id: int, enabled: bool) -> None:
        self.db.set_blocked_site_enabled(site_id, enabled)
        self.blocker.reload_config()

    def _delete_site(self, site_id: int, name: str) -> None:
        if messagebox.askyesno(
            "Rimuovere il sito?",
            f"Rimuovere {name} dalla blacklist?",
            parent=self.winfo_toplevel(),
        ):
            self.db.delete_blocked_site(site_id)
            self.blocker.reload_config()
            self._refresh_lists()

    def _add_app(self) -> None:
        display = self.app_name_entry.get().strip()
        try:
            process = normalize_process_name(self.app_process_entry.get())
        except ValueError as exc:
            messagebox.showerror("Processo non valido", str(exc), parent=self.winfo_toplevel())
            return
        self.db.add_blocked_app(display or process.title(), process)
        self.app_name_entry.delete(0, "end")
        self.app_process_entry.delete(0, "end")
        self.blocker.reload_config()
        self._refresh_lists()

    def _toggle_app(self, app_id: int, enabled: bool) -> None:
        self.db.set_blocked_app_enabled(app_id, enabled)
        self.blocker.reload_config()

    def _delete_app(self, app_id: int, name: str) -> None:
        if messagebox.askyesno(
            "Rimuovere l'applicazione?",
            f"Rimuovere {name} dalla blacklist?",
            parent=self.winfo_toplevel(),
        ):
            self.db.delete_blocked_app(app_id)
            self.blocker.reload_config()
            self._refresh_lists()

    def _retry_sites(self) -> None:
        if self.blocker.retry_sites():
            self.app.toast("Nuova richiesta di autorizzazione inviata.")
        else:
            self.app.toast("Avvia un Pomodoro con siti abilitati per riprovare.")

    def _restore_hosts(self) -> None:
        status = self.blocker.status()
        if status["active"]:
            messagebox.showwarning(
                "Sessione attiva",
                "Termina o metti in pausa il timer prima di ripristinare manualmente il file hosts.",
                parent=self.winfo_toplevel(),
            )
            return
        if not messagebox.askyesno(
            "Ripristinare il file hosts?",
            "Verra' richiesta l'autorizzazione amministratore solo se necessario. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        self.residual_label.configure(text="Ripristino in corso...", text_color=COLORS.amber)

        def worker() -> None:
            try:
                ok = self.blocker.force_restore_hosts()
            except Exception as exc:
                self.app.logger.exception("Ripristino hosts fallito.")

                def show_error(error: str = str(exc)) -> None:
                    messagebox.showerror(
                        "Ripristino non riuscito",
                        error,
                        parent=self.winfo_toplevel(),
                    )

                self.app.post(show_error)
                return
            message = (
                "File hosts ripristinato correttamente."
                if ok
                else "Il file hosts non e' stato modificato oppure il ripristino non e' riuscito."
            )

            def show_result() -> None:
                self.app.toast(message)
                self._refresh_status()

            self.app.post(show_result)

        threading.Thread(target=worker, name="StudyFlow-HostsRestore", daemon=True).start()

    # ------------------------------------------------------------------ status
    def _start_poll(self) -> None:
        self._stop_poll()
        self._poll()

    def _stop_poll(self) -> None:
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except (tk.TclError, ValueError):
                pass
            self._poll_job = None

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self._refresh_status()
        except Exception:
            self.app.logger.exception("Errore aggiornando la vista Blocco.")
        self._poll_job = self.after(1000, self._poll)

    def _refresh_status(self) -> None:
        status = self.blocker.status()
        sites = status["site_state"]
        if not status["active"]:
            sites_value, sites_color = "In attesa", COLORS.text_faint
        elif sites == "active":
            sites_value, sites_color = "Attivi", COLORS.green
        elif sites == "starting":
            sites_value, sites_color = "Avvio / UAC", COLORS.amber
        elif sites in {"error", "stale"}:
            sites_value, sites_color = "Errore", COLORS.red
        elif sites == "disabled":
            sites_value, sites_color = "Disattivato", COLORS.text_faint
        else:
            sites_value, sites_color = "In attesa", COLORS.text_faint
        if not status["active"]:
            apps_value, apps_color = "In attesa", COLORS.text_faint
        elif status["apps_enabled"]:
            apps_value, apps_color = "Attivo", COLORS.green
        else:
            apps_value, apps_color = "Disattivato", COLORS.text_faint
        self.sites_card.set(
            sites_value,
            str(status.get("site_message") or "Si attiva con il Pomodoro"),
            sites_color,
        )
        apps_subtitle = (
            "Terminazione processi attiva"
            if status["terminate_apps"]
            else "Solo rilevamento e logging"
        )
        self.apps_card.set(apps_value, apps_subtitle, apps_color)
        self.attempts_card.set(
            str(status["attempts"]),
            f"{len(self.db.list_blocked_sites(enabled_only=True))} siti · "
            f"{len(self.db.list_blocked_apps(enabled_only=True))} app attive",
            COLORS.amber if status["attempts"] else COLORS.text_faint,
        )
        trap = status.get("trap", {})
        ports = trap.get("ports", {})
        if status["active"] and trap.get("running"):
            port80 = "on" if ports.get(80) else "off"
            port443 = "on" if ports.get(443) else "off"
            trap_text = f"Intercettazione: porta 80 {port80}, porta 443 {port443}."
        elif status["trap_enabled"]:
            trap_text = "Intercettazione in attesa dell'avvio della sessione."
        else:
            trap_text = "Intercettazione tentativi disattivata."
        self.options_note.configure(text=trap_text)
        self.retry_button.configure(
            state="normal"
            if status["active"]
            and sites in {"error", "stale", "inactive", "unknown"}
            else "disabled"
        )
        if status["hosts_residual"]:
            self.residual_label.configure(
                text=(
                    "ATTENZIONE: e' presente una sezione StudyFlow residua nel file hosts. "
                    "Usa 'Ripristina file hosts' per pulirla."
                ),
                text_color=COLORS.amber,
            )
        else:
            self.residual_label.configure(
                text=(
                    "Nessuna sezione residua rilevata. Il file hosts viene gestito in modo "
                    "idempotente e ripristinato automaticamente."
                ),
                text_color=COLORS.text_muted,
            )
