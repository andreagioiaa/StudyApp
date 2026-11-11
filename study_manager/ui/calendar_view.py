"""Vista Calendario con import Google Calendar (.ics)."""

from __future__ import annotations

import threading
import tkinter as tk
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..config import COLORS
from ..services.ics_import import import_ics_file
from ..utils import day_bounds, parse_datetime, week_bounds
from .formatting import (
    format_day,
    format_day_short,
    format_event_datetime,
    format_event_time,
    format_week_range,
)
from .theme import font
from .widgets import Card, PageHeader, clear_children, render_state


class CalendarView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self._anchor = date.today()
        self._mode = "day"
        self._importing = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        PageHeader(
            self,
            "Calendario",
            "Importa il tuo calendario Google e trasforma gli eventi in sessioni di studio.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))
        self._build_toolbar()

        self.events_scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.events_scroll.grid(row=2, column=0, sticky="nsew")
        self.events_scroll.grid_columnconfigure(0, weight=1)

    def _build_toolbar(self) -> None:
        toolbar = Card(self)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        toolbar.grid_columnconfigure(1, weight=1)

        nav = ctk.CTkFrame(toolbar, fg_color="transparent")
        nav.grid(row=0, column=0, sticky="w", padx=16, pady=12)
        ctk.CTkButton(
            nav,
            text="‹",
            width=34,
            height=34,
            font=font(17),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda: self._move(-1),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            nav,
            text="Oggi",
            width=64,
            height=34,
            font=font(11, "bold"),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=self._today,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            nav,
            text="›",
            width=34,
            height=34,
            font=font(17),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda: self._move(1),
        ).pack(side="left", padx=(6, 0))

        self.period_label = ctk.CTkLabel(
            toolbar,
            text="",
            font=font(15, "bold"),
            text_color=COLORS.text,
            anchor="w",
        )
        self.period_label.grid(row=0, column=1, sticky="w", padx=20)

        controls = ctk.CTkFrame(toolbar, fg_color="transparent")
        controls.grid(row=0, column=2, sticky="e", padx=16, pady=10)
        self.mode_switch = ctk.CTkSegmentedButton(
            controls,
            values=["Giorno", "Settimana"],
            width=170,
            height=32,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            selected_color=COLORS.accent_dark,
            selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt,
            unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            command=self._change_mode,
        )
        self.mode_switch.set("Giorno")
        self.mode_switch.pack(side="left", padx=(0, 10))
        self.import_btn = ctk.CTkButton(
            controls,
            text="Importa .ics",
            width=108,
            height=32,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=8,
            command=self._import_dialog,
        )
        self.import_btn.pack(side="left", padx=5)
        ctk.CTkButton(
            controls,
            text="Svuota",
            width=66,
            height=32,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_faint,
            corner_radius=8,
            command=self._clear_events,
        ).pack(side="left", padx=(5, 0))

        self.status_label = ctk.CTkLabel(
            toolbar,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.status_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=20, pady=(0, 9))

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        with render_state(self).only_if_changed():
            self._refresh()

    def _today(self) -> None:
        self._anchor = date.today()
        self._refresh()

    def _move(self, direction: int) -> None:
        step = timedelta(days=7 if self._mode == "week" else 1)
        self._anchor += step * direction
        self._refresh()

    def _change_mode(self, value: str) -> None:
        self._mode = "week" if value == "Settimana" else "day"
        self._refresh()

    def _bounds(self) -> tuple[datetime, datetime]:
        if self._mode == "week":
            monday, next_monday = week_bounds(self._anchor)
            return datetime.combine(monday, datetime.min.time()), datetime.combine(
                next_monday, datetime.min.time()
            )
        start_text, end_text = day_bounds(self._anchor)
        return datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S"), datetime.strptime(
            end_text, "%Y-%m-%d %H:%M:%S"
        )

    def _refresh(self) -> None:
        start, end = self._bounds()
        if self._mode == "week":
            self.period_label.configure(text=format_week_range(start.date(), end.date()))
        else:
            self.period_label.configure(text=format_day(self._anchor))
        events = self.db.get_events(
            start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
        )
        if not render_state(self).should_render("events", self._mode, self._anchor, events, date.today()):
            return
        clear_children(self.events_scroll)
        if not events:
            empty = Card(self.events_scroll)
            empty.grid(row=0, column=0, sticky="ew", pady=(4, 0))
            ctk.CTkLabel(
                empty,
                text="Nessun evento in questo periodo.",
                font=font(14, "bold"),
                text_color=COLORS.text_muted,
            ).pack(anchor="w", padx=22, pady=(24, 4))
            ctk.CTkLabel(
                empty,
                text="Importa un file .ics esportato da Google Calendar per iniziare.",
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=22, pady=(0, 24))
            return

        if self._mode == "week":
            grouped: OrderedDict[date, list[dict]] = OrderedDict()
            for event in events:
                parsed = parse_datetime(str(event["start"]))
                event_day = parsed.date() if parsed else start.date()
                grouped.setdefault(event_day, []).append(event)
            row = 0
            for event_day, day_events in grouped.items():
                heading = ctk.CTkFrame(self.events_scroll, fg_color="transparent")
                heading.grid(row=row, column=0, sticky="ew", pady=(8, 4))
                ctk.CTkLabel(
                    heading,
                    text=format_day(event_day).capitalize(),
                    font=font(12, "bold"),
                    text_color=COLORS.text_muted,
                    anchor="w",
                ).pack(side="left")
                ctk.CTkLabel(
                    heading,
                    text=f"{len(day_events)} eventi",
                    font=font(10),
                    text_color=COLORS.text_faint,
                ).pack(side="left", padx=10)
                row += 1
                for event in day_events:
                    self._event_card(event, row)
                    row += 1
        else:
            for index, event in enumerate(events):
                self._event_card(event, index)

    def _event_card(self, event: dict, row: int) -> None:
        card = ctk.CTkFrame(
            self.events_scroll,
            fg_color=COLORS.surface,
            corner_radius=12,
            border_width=1,
            border_color=COLORS.border,
        )
        card.grid(row=row, column=0, sticky="ew", pady=4)
        card.grid_columnconfigure(1, weight=1)
        accent = ctk.CTkFrame(card, width=4, fg_color=COLORS.blue, corner_radius=2)
        accent.grid(row=0, column=0, rowspan=4, sticky="ns", padx=(12, 10), pady=12)

        time_label = format_event_time(
            str(event["start"]), str(event["end"]), bool(event["all_day"])
        )
        ctk.CTkLabel(
            card,
            text=time_label,
            font=font(10, "bold"),
            text_color=COLORS.blue if not event["all_day"] else COLORS.purple,
            anchor="w",
        ).grid(row=0, column=1, sticky="w", pady=(12, 1))
        ctk.CTkLabel(
            card,
            text=str(event["title"]),
            font=font(15, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=650,
            justify="left",
        ).grid(row=1, column=1, sticky="w")
        meta_parts = []
        if event.get("subject") and event.get("subject") != event.get("title"):
            meta_parts.append(str(event["subject"]))
        if event.get("location"):
            meta_parts.append(str(event["location"]))
        if meta_parts:
            ctk.CTkLabel(
                card,
                text=" · ".join(meta_parts),
                font=font(10),
                text_color=COLORS.text_muted,
                anchor="w",
                wraplength=650,
                justify="left",
            ).grid(row=2, column=1, sticky="w", pady=(1, 0))
        description = " ".join(str(event.get("description") or "").split())
        if description:
            ctk.CTkLabel(
                card,
                text=description[:280] + ("..." if len(description) > 280 else ""),
                font=font(10),
                text_color=COLORS.text_faint,
                anchor="w",
                wraplength=650,
                justify="left",
            ).grid(row=3, column=1, sticky="w", pady=(2, 12))
        else:
            ctk.CTkFrame(card, height=10, fg_color="transparent").grid(row=3, column=1)
        ctk.CTkButton(
            card,
            text="Studia ora",
            width=108,
            height=34,
            font=font(11, "bold"),
            fg_color=COLORS.accent_dark,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda value=dict(event): self.app.start_from_event(value),
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=14, pady=(12, 2))

    # ------------------------------------------------------------------ import
    def _import_dialog(self) -> None:
        if self._importing:
            return
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="Importa calendario Google",
            filetypes=[("Calendario iCalendar", "*.ics"), ("Tutti i file", "*.*")],
        )
        if not path:
            return
        self._importing = True
        self.import_btn.configure(state="disabled", text="Importazione...")
        self.status_label.configure(text="Lettura e importazione in corso...", text_color=COLORS.amber)
        file_path = Path(path)

        def worker() -> None:
            try:
                result = import_ics_file(self.db, file_path)
            except Exception as exc:
                self.app.logger.exception("Importazione ICS fallita.")
                self.app.post(lambda error=str(exc): self._import_done(None, error))
            else:
                self.app.post(lambda value=result: self._import_done(value, ""))
            finally:
                # Thread dedicato: la connessione SQLite thread-local non deve
                # restare registrata dopo la fine dell'importazione.
                self.db.close_current_thread()

        threading.Thread(target=worker, name="StudyFlow-ICSImport", daemon=True).start()

    def _import_done(self, result: dict | None, error: str) -> None:
        if not self.winfo_exists():
            return
        self._importing = False
        self.import_btn.configure(state="normal", text="Importa .ics")
        if error:
            self.status_label.configure(text=f"Importazione non riuscita: {error}", text_color=COLORS.red)
            messagebox.showerror("Importazione non riuscita", error, parent=self.winfo_toplevel())
            return
        if result is None:
            return
        warning_note = f" ({len(result['warnings'])} avvisi)" if result.get("warnings") else ""
        self.status_label.configure(
            text=f"Importati {result['imported']} eventi da {result['file']}{warning_note}.",
            text_color=COLORS.green,
        )
        self._refresh()
        if result.get("warnings"):
            messagebox.showwarning(
                "Importazione completata con avvisi",
                "\n".join(result["warnings"][:8]),
                parent=self.winfo_toplevel(),
            )

    def _clear_events(self) -> None:
        if not messagebox.askyesno(
            "Svuotare il calendario?",
            "Tutti gli eventi importati verranno eliminati. I materiali resteranno, "
            "ma perderanno il collegamento all'evento. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        removed = self.db.clear_calendar_events()
        self.status_label.configure(
            text=f"Rimossi {removed} eventi.", text_color=COLORS.text_muted
        )
        self._refresh()
