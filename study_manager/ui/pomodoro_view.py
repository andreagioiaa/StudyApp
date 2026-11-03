"""Vista Timer Pomodoro."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS, PHASE_LABELS, STATUS_LABELS
from .theme import font, phase_color, status_color
from .widgets import Card, PageHeader, TimerRing


class PomodoroView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.pomodoro = app.pomodoro
        self.blocker = app.blocker
        self._last_result_id: int | None = None
        self._last_subject: str = ""
        self._last_block_update = 0.0
        self._poll_job: str | None = None

        self.grid_columnconfigure(0, weight=3, uniform="pomo")
        self.grid_columnconfigure(1, weight=2, uniform="pomo")
        self.grid_rowconfigure(1, weight=1)

        PageHeader(
            self,
            "Timer Pomodoro",
            "Concentrazione profonda, pause rigeneranti e blocco automatico delle distrazioni.",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))

        self._build_timer_card()
        self._build_side_panel()

    # ------------------------------------------------------------------ layout
    def _build_timer_card(self) -> None:
        card = Card(self)
        card.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        top.grid_columnconfigure(0, weight=1)
        self.phase_label = ctk.CTkLabel(
            top,
            text="LAVORO",
            font=font(11, "bold"),
            text_color=COLORS.accent,
            fg_color=COLORS.accent_dark,
            corner_radius=8,
            padx=12,
            height=26,
        )
        self.phase_label.grid(row=0, column=0, sticky="w")
        self.status_label = ctk.CTkLabel(
            top,
            text="Pronto",
            font=font(11),
            text_color=COLORS.text_muted,
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        self.ring = TimerRing(card, size=270)
        self.ring.grid(row=1, column=0, pady=(16, 4))

        self.subject_label = ctk.CTkLabel(
            card,
            text="Studio",
            font=font(18, "bold"),
            text_color=COLORS.text,
        )
        self.subject_label.grid(row=2, column=0, pady=(0, 2))
        self.context_label = ctk.CTkLabel(
            card,
            text="Sessione libera",
            font=font(11),
            text_color=COLORS.text_faint,
        )
        self.context_label.grid(row=3, column=0, pady=(0, 16))

        subject_row = ctk.CTkFrame(card, fg_color="transparent")
        subject_row.grid(row=4, column=0, sticky="ew", padx=34)
        subject_row.grid_columnconfigure(0, weight=1)
        self.subject_entry = ctk.CTkEntry(
            subject_row,
            height=40,
            placeholder_text="Materia o argomento (es. Analisi Matematica)",
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(13),
            corner_radius=10,
        )
        self.subject_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.subject_entry.insert(0, "Studio")
        self.subject_entry.bind("<Return>", lambda _event: self._start())

        self.start_btn = ctk.CTkButton(
            subject_row,
            text="Avvia",
            width=104,
            height=40,
            font=font(13, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._start,
        )
        self.start_btn.grid(row=0, column=1)

        controls = ctk.CTkFrame(card, fg_color="transparent")
        controls.grid(row=5, column=0, sticky="ew", padx=34, pady=(12, 8))
        for index in range(3):
            controls.grid_columnconfigure(index, weight=1)
        self.pause_btn = ctk.CTkButton(
            controls,
            text="Pausa",
            height=38,
            font=font(12, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=10,
            command=self._toggle_pause,
        )
        self.pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.skip_btn = ctk.CTkButton(
            controls,
            text="Salta",
            height=38,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self._skip,
        )
        self.skip_btn.grid(row=0, column=1, sticky="ew", padx=6)
        self.reset_btn = ctk.CTkButton(
            controls,
            text="Reset",
            height=38,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self._reset,
        )
        self.reset_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        self.cycle_label = ctk.CTkLabel(
            card,
            text="Ciclo 1 di 4 · 0 Pomodori completati",
            font=font(11),
            text_color=COLORS.text_faint,
        )
        self.cycle_label.grid(row=6, column=0, pady=(6, 20))
        self._build_cycle_dots(card, 7)

    def _build_cycle_dots(self, parent, row: int) -> None:
        self.dots_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.dots_frame.grid(row=row, column=0, pady=(0, 22))
        self._dot_labels: list[ctk.CTkLabel] = []
        self._render_cycle_dots(0, 4)

    def _render_cycle_dots(self, position: int, target: int) -> None:
        if len(self._dot_labels) != target:
            for label in self._dot_labels:
                label.destroy()
            self._dot_labels = []
            for _ in range(target):
                label = ctk.CTkLabel(
                    self.dots_frame,
                    text="●",
                    font=font(15),
                    text_color=COLORS.border,
                    width=18,
                )
                label.pack(side="left", padx=2)
                self._dot_labels.append(label)
        for index, label in enumerate(self._dot_labels):
            label.configure(
                text_color=COLORS.accent if index < position else COLORS.border
            )

    def _build_side_panel(self) -> None:
        panel = ctk.CTkFrame(self, fg_color="transparent")
        panel.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        session_card = Card(panel)
        session_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        session_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            session_card,
            text="Sessione corrente",
            font=font(13, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 10))
        self._kv(session_card, 1, "Materia", "Studio")
        self._context_value = self._kv(session_card, 2, "Contesto", "Nessun evento o materiale")
        self._target_value = self._kv(session_card, 3, "Durata fase", "25 min")
        self._last_session_value = self._kv(session_card, 4, "Ultima sessione", "-")
        ctk.CTkFrame(
            session_card, height=1, fg_color=COLORS.border, corner_radius=0
        ).grid(row=5, column=0, columnspan=2, sticky="ew", padx=18, pady=(8, 10))
        self.config_value = ctk.CTkLabel(
            session_card,
            text="25 / 5 / 15 min · 4 cicli",
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
        )
        self.config_value.grid(row=6, column=0, sticky="w", padx=18, pady=(0, 4))
        ctk.CTkButton(
            session_card,
            text="Configura timer",
            height=32,
            font=font(11, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda: self.app.show_view("settings"),
        ).grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=(4, 16))

        block_card = Card(panel)
        block_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        block_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            block_card,
            text="Protezione attenzione",
            font=font(13, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        self.site_status = self._status_row(block_card, 1, "Siti web")
        self.app_status = self._status_row(block_card, 2, "Applicazioni")
        self.attempts_status = self._status_row(block_card, 3, "Tentativi bloccati")
        self.site_detail = ctk.CTkLabel(
            block_card,
            text="Il blocco si attiva automaticamente con il Pomodoro.",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
            wraplength=330,
            justify="left",
        )
        self.site_detail.grid(row=4, column=0, sticky="ew", padx=18, pady=(5, 16))

        tip_card = Card(panel)
        tip_card.grid(row=2, column=0, sticky="nsew")
        tip_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tip_card,
            text="Una sessione alla volta",
            font=font(13, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 6))
        ctk.CTkLabel(
            tip_card,
            text=(
                "Il timer continua a funzionare anche mentre navighi nelle altre sezioni. "
                "Alla fine di ogni fase di lavoro i minuti effettivi vengono salvati "
                "automaticamente e il blocco viene rilasciato."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            wraplength=330,
            justify="left",
            anchor="nw",
        ).grid(row=1, column=0, sticky="new", padx=18, pady=(0, 16))

    def _kv(self, parent, row: int, key: str, value: str) -> ctk.CTkLabel:
        ctk.CTkLabel(
            parent,
            text=key,
            font=font(11),
            text_color=COLORS.text_faint,
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(18, 10), pady=3)
        value_label = ctk.CTkLabel(
            parent,
            text=value,
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="e",
            wraplength=230,
            justify="right",
        )
        value_label.grid(row=row, column=1, sticky="e", padx=(0, 18), pady=3)
        return value_label

    def _status_row(self, parent, row: int, title: str) -> ctk.CTkLabel:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=18, pady=3)
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            frame,
            text="●",
            font=font(12),
            text_color=COLORS.border,
            width=16,
        ).grid(row=0, column=0, padx=(0, 4))
        ctk.CTkLabel(
            frame,
            text=title,
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        value = ctk.CTkLabel(
            frame,
            text="Inattivo",
            font=font(11, "bold"),
            text_color=COLORS.text_faint,
            anchor="e",
        )
        value.grid(row=0, column=2, sticky="e")
        value._dot = frame.winfo_children()[0]  # type: ignore[attr-defined]
        return value

    # ------------------------------------------------------------------ actions
    def _start(self) -> None:
        subject = self.subject_entry.get().strip() or "Studio"
        ok, error = self.pomodoro.start_current(subject=subject)
        if not ok:
            messagebox.showwarning("StudyFlow", error, parent=self.winfo_toplevel())
        self._refresh_now()

    def _toggle_pause(self) -> None:
        if self.pomodoro.snapshot()["status"] == "idle":
            self._start()
            return
        self.pomodoro.toggle_pause()
        self._refresh_now()

    def _skip(self) -> None:
        snapshot = self.pomodoro.snapshot()
        if snapshot["status"] == "idle" and snapshot["phase"] == "work":
            return
        if snapshot["phase"] == "work" and snapshot["status"] in {"running", "paused"}:
            if not messagebox.askyesno(
                "Saltare il Pomodoro?",
                "La sessione corrente verra' salvata come incompleta. Continuare?",
                parent=self.winfo_toplevel(),
            ):
                return
        self.pomodoro.skip()
        self._refresh_now()

    def _reset(self) -> None:
        snapshot = self.pomodoro.snapshot()
        if snapshot["status"] in {"running", "paused"}:
            if not messagebox.askyesno(
                "Azzerare il timer?",
                "La sessione corrente verra' salvata come incompleta e il ciclo verra' azzerato.",
                parent=self.winfo_toplevel(),
            ):
                return
        self.pomodoro.reset()
        self.subject_entry.configure(state="normal")
        self.subject_entry.delete(0, "end")
        self.subject_entry.insert(0, "Studio")
        self._refresh_now()

    # ------------------------------------------------------------------ refresh
    def on_show(self) -> None:
        snapshot = self.pomodoro.snapshot()
        config = snapshot["config"]
        self.config_value.configure(
            text=(
                f"{config.work_minutes:g} / {config.short_break_minutes:g} / "
                f"{config.long_break_minutes:g} min · {config.cycles} cicli"
            )
        )
        if not self.subject_entry.get().strip():
            self.subject_entry.insert(0, snapshot["subject"])
        self._refresh_now()
        self._start_poll()

    def on_hide(self) -> None:
        self._stop_poll()

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
            self._refresh_now()
        except Exception:
            self.app.logger.exception("Errore aggiornando la vista Pomodoro.")
        self._poll_job = self.after(250, self._poll)

    def _refresh_now(self) -> None:
        snapshot = self.pomodoro.snapshot()
        phase = snapshot["phase"]
        status = snapshot["status"]
        color = phase_color(phase)
        if status == "paused":
            color = COLORS.amber
        self.phase_label.configure(
            text=PHASE_LABELS.get(phase, phase).upper(),
            text_color=color,
            fg_color=self._badge_bg(phase, status),
        )
        self.status_label.configure(
            text=STATUS_LABELS.get(status, status),
            text_color=status_color(status),
        )
        self.ring.set(
            snapshot["progress"],
            snapshot["remaining_label"],
            PHASE_LABELS.get(phase, phase),
            color,
        )
        self.subject_label.configure(text=snapshot["subject"])
        context = snapshot["context_label"] or "Sessione libera"
        self.context_label.configure(text=context)
        self._context_value.configure(text=context)
        self._target_value.configure(text=f"{snapshot['total_seconds'] // 60} min")
        self.subject_entry.configure(state="disabled" if status != "idle" else "normal")
        if status != "idle" and self.subject_entry.get() != snapshot["subject"]:
            self.subject_entry.configure(state="normal")
            self.subject_entry.delete(0, "end")
            self.subject_entry.insert(0, snapshot["subject"])
            self.subject_entry.configure(state="disabled")

        self.start_btn.configure(
            state="disabled" if status in {"running", "paused"} else "normal",
            text="Avvia" if phase == "work" or status != "idle" else "Avvia pausa",
        )
        self.pause_btn.configure(
            state="disabled" if status == "idle" else "normal",
            text="Riprendi" if status == "paused" else "Pausa",
        )
        self.skip_btn.configure(
            state="disabled" if status == "idle" and phase == "work" else "normal"
        )

        position = int(snapshot["cycle_position"])
        target = int(snapshot["cycle_target"])
        completed = int(snapshot["completed_work"])
        self.cycle_label.configure(
            text=(
                f"Ciclo {min(position + 1, target)} di {target} · "
                f"{completed} Pomodori completati"
            )
        )
        self._render_cycle_dots(position, target)

        result = snapshot.get("last_result")
        if isinstance(result, dict):
            result_id = int(result.get("session_id", 0) or 0)
            if result_id and result_id != self._last_result_id:
                self._last_result_id = result_id
                state_text = "completata" if result.get("completed") else "interrotta"
                self._last_session_value.configure(
                    text=f"{result.get('minutes', 0)} min · {state_text}"
                )
                self.app.on_session_finished(result)
        if snapshot.get("last_error"):
            self.site_detail.configure(
                text=str(snapshot["last_error"]), text_color=COLORS.red
            )

        now = time.monotonic()
        if now - self._last_block_update >= 1.0:
            self._last_block_update = now
            self._update_block_status()
        self.app.update_window_title(snapshot)

    def _update_block_status(self) -> None:
        status = self.blocker.status()
        sites = status["site_state"]
        if not status["active"]:
            site_text, site_color = "In attesa", COLORS.text_faint
        elif sites == "active":
            site_text, site_color = "Attivo", COLORS.green
        elif sites == "starting":
            site_text, site_color = "Avvio / UAC", COLORS.amber
        elif sites in {"error", "stale"}:
            site_text, site_color = "Errore", COLORS.red
        elif sites == "disabled":
            site_text, site_color = "Disattivato", COLORS.text_faint
        else:
            site_text, site_color = "Non attivo", COLORS.amber

        if status["active"] and status["apps_enabled"]:
            app_text, app_color = "Attivo", COLORS.green
        else:
            app_text, app_color = "In attesa", COLORS.text_faint
        self.site_status.configure(text=site_text, text_color=site_color)
        self.app_status.configure(text=app_text, text_color=app_color)
        self.attempts_status.configure(
            text=str(status["attempts"]),
            text_color=COLORS.amber if status["attempts"] else COLORS.text_faint,
        )
        self.site_status._dot.configure(text_color=site_color)  # type: ignore[attr-defined]
        self.app_status._dot.configure(text_color=app_color)  # type: ignore[attr-defined]
        self.attempts_status._dot.configure(  # type: ignore[attr-defined]
            text_color=COLORS.amber if status["attempts"] else COLORS.border
        )
        detail = str(status.get("site_message") or "Blocco non attivo.")
        self.site_detail.configure(
            text=detail,
            text_color=COLORS.red if sites in {"error", "stale"} else COLORS.text_faint,
        )

    @staticmethod
    def _badge_bg(phase: str, status: str) -> str:
        if status == "paused":
            return "#44320B"
        if phase == "short_break":
            return "#0C3348"
        if phase == "long_break":
            return "#2F2455"
        return COLORS.accent_dark
