"""Vista Calendario: agenda colorata, import .ics e sincronizzazione Google Calendar."""

from __future__ import annotations

import threading
import tkinter as tk
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..database import (
    EVENT_PENDING_CREATE,
    EVENT_PENDING_UPDATE,
    EVENT_SOURCE_ICAL,
    EVENT_SOURCE_ICS,
    normalize_hex,
)
from ..services.calendar_sync import format_elapsed, text_color_for
from ..services.ics_import import import_ics_file
from ..utils import day_bounds, parse_datetime, week_bounds
from .calendar_help import open_calendar_help
from .formatting import (
    format_day,
    format_day_short,
    format_event_datetime,
    format_event_time,
    format_week_range,
)
from .theme import font
from .widgets import Card, PageHeader, center_window, clear_children, render_state, set_window_icon


ELAPSED_REFRESH_MS = 30_000
NO_SUBJECT_LABEL = "— Nessuna materia —"


def _event_color(event: dict[str, Any]) -> str:
    return normalize_hex(event.get("color_hex")) or COLORS.blue


class CalendarView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.sync = app.google_sync
        self._anchor = date.today()
        self._mode = "day"
        self._importing = False
        self._elapsed_job: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        PageHeader(
            self,
            "Calendario",
            "Agenda sincronizzata con Google Calendar: ogni materia ha il suo calendario e il suo colore.",
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
        self._update_sync_widgets()
        self._schedule_elapsed()

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
        ctk.CTkButton(
            controls,
            text="+ Nuovo Evento",
            width=122,
            height=32,
            font=font(11, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=8,
            command=lambda: self.open_event_dialog(None),
        ).pack(side="left", padx=5)
        self.import_btn = ctk.CTkButton(
            controls,
            text="Importa .ics",
            width=108,
            height=32,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
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
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 9))

        sync_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        sync_frame.grid(row=1, column=2, sticky="e", padx=16, pady=(0, 9))
        ctk.CTkButton(
            sync_frame,
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
            command=self.open_help,
        ).pack(side="left", padx=(0, 10))
        self.elapsed_label = ctk.CTkLabel(
            sync_frame,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="e",
        )
        self.elapsed_label.pack(side="left", padx=(0, 10))
        self.sync_btn = ctk.CTkButton(
            sync_frame,
            text="⟳  Sincronizza Ora",
            width=146,
            height=30,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=8,
            command=self._sync_now,
        )
        self.sync_btn.pack(side="left")

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        self._update_sync_widgets()
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
                text=(
                    "Crea un evento con \"+ Nuovo Evento\", sincronizza Google Calendar "
                    "oppure importa un file .ics."
                ),
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=22, pady=(0, 12 if not self.sync.has_sources() else 24))
            if not self.sync.has_sources():
                ctk.CTkButton(
                    empty,
                    text="Come Connettere Calendario",
                    width=210,
                    height=34,
                    font=font(11, "bold"),
                    fg_color=COLORS.accent,
                    hover_color=COLORS.accent_hover,
                    text_color=COLORS.bg,
                    corner_radius=8,
                    command=self.open_help,
                ).pack(anchor="w", padx=22, pady=(0, 22))
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
        color = _event_color(event)
        card = ctk.CTkFrame(
            self.events_scroll,
            fg_color=COLORS.surface,
            corner_radius=12,
            border_width=1,
            border_color=COLORS.border,
        )
        card.grid(row=row, column=0, sticky="ew", pady=4)
        card.grid_columnconfigure(1, weight=1)
        accent = ctk.CTkFrame(card, width=6, fg_color=color, corner_radius=3)
        accent.grid(row=0, column=0, rowspan=4, sticky="ns", padx=(12, 10), pady=12)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=1, sticky="w", pady=(12, 1))
        time_label = format_event_time(
            str(event["start"]), str(event["end"]), bool(event["all_day"])
        )
        ctk.CTkLabel(
            top,
            text=time_label,
            font=font(10, "bold"),
            text_color=COLORS.text_muted if not event["all_day"] else COLORS.purple,
            anchor="w",
        ).pack(side="left")
        subject = str(event.get("subject") or "").strip()
        if subject:
            # Chip colorata con contrasto del testo calcolato sul colore Google.
            ctk.CTkLabel(
                top,
                text=f" {subject} ",
                font=font(9, "bold"),
                fg_color=color,
                text_color=text_color_for(color),
                corner_radius=6,
                height=18,
            ).pack(side="left", padx=(8, 0))
        badge = self._sync_badge(event)
        if badge:
            ctk.CTkLabel(
                top,
                text=badge[0],
                font=font(9),
                text_color=badge[1],
            ).pack(side="left", padx=(8, 0))

        title = ctk.CTkLabel(
            card,
            text=str(event["title"]),
            font=font(15, "bold"),
            text_color=COLORS.text,
            anchor="w",
            wraplength=620,
            justify="left",
            cursor="hand2",
        )
        title.grid(row=1, column=1, sticky="w")
        title.bind("<Button-1>", lambda _event, value=dict(event): self.open_event_dialog(value))
        meta_parts = []
        if event.get("location"):
            meta_parts.append(str(event["location"]))
        origin = self.sync.source_label(event.get("google_calendar_id"))
        if origin:
            meta_parts.append(origin)
        elif event.get("source") == EVENT_SOURCE_ICS:
            meta_parts.append("Importato da .ics")
        if meta_parts:
            ctk.CTkLabel(
                card,
                text=" · ".join(meta_parts),
                font=font(10),
                text_color=COLORS.text_muted,
                anchor="w",
                wraplength=620,
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
                wraplength=620,
                justify="left",
            ).grid(row=3, column=1, sticky="w", pady=(2, 12))
        else:
            ctk.CTkFrame(card, height=10, fg_color="transparent").grid(row=3, column=1)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=0, column=2, rowspan=2, sticky="e", padx=14, pady=(12, 2))
        ctk.CTkButton(
            actions,
            text="Modifica",
            width=84,
            height=34,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda value=dict(event): self.open_event_dialog(value),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="Studia ora",
            width=108,
            height=34,
            font=font(11, "bold"),
            fg_color=COLORS.accent_dark,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda value=dict(event): self.app.start_from_event(value),
        ).pack(side="left")

    @staticmethod
    def _sync_badge(event: dict) -> tuple[str, str] | None:
        if event.get("sync_error"):
            return "⚠ Non sincronizzato", COLORS.red
        if event.get("source") in (EVENT_PENDING_CREATE, EVENT_PENDING_UPDATE):
            return "● In attesa di sincronizzazione", COLORS.amber
        return None

    # ----------------------------------------------------------- sync google
    def _schedule_elapsed(self) -> None:
        self._update_elapsed()
        try:
            self._elapsed_job = self.after(ELAPSED_REFRESH_MS, self._schedule_elapsed)
        except tk.TclError:
            self._elapsed_job = None

    def _update_elapsed(self) -> None:
        if not self.winfo_exists():
            return
        if not self.sync.has_sources():
            self.elapsed_label.configure(text="Nessun calendario collegato", text_color=COLORS.text_faint)
            return
        if self.sync.is_syncing:
            self.elapsed_label.configure(text="Sincronizzazione in corso...", text_color=COLORS.amber)
            return
        text = format_elapsed(self.sync.last_sync_at())
        pending = self.db.count_pending_events()
        if pending:
            text += f" · {pending} in coda"
        if self.sync.offline:
            text += " · offline"
        self.elapsed_label.configure(
            text=text,
            text_color=COLORS.amber if (pending or self.sync.offline) else COLORS.text_faint,
        )

    def _update_sync_widgets(self) -> None:
        busy = self.sync.is_syncing
        self.sync_btn.configure(
            state="disabled" if busy else "normal",
            text="Sincronizzazione..." if busy else "⟳  Sincronizza Ora",
        )
        self._update_elapsed()

    def _sync_now(self) -> None:
        if not self.sync.has_sources():
            # Nessun calendario collegato: mostra direttamente il tutorial.
            self.open_help()
            return
        if self.sync.sync_async("manual"):
            self.status_label.configure(text="Sincronizzazione con Google in corso...", text_color=COLORS.amber)
        self._update_sync_widgets()

    def on_sync_event(self, kind: str, data: dict[str, Any]) -> None:
        """Chiamato dal main thread (tramite ``app.post``) a ogni evento di sync."""
        if not self.winfo_exists():
            return
        self._update_sync_widgets()
        if kind == "sync_finished":
            error = str(data.get("error") or "")
            result = data.get("result") or {}
            warnings = list((result or {}).get("warnings") or [])
            if error:
                self.status_label.configure(text=f"Sincronizzazione: {error}", text_color=COLORS.amber)
            elif warnings:
                self.status_label.configure(
                    text=f"Sincronizzato con avvisi: {warnings[0]}"
                    + (f" (+{len(warnings) - 1})" if len(warnings) > 1 else ""),
                    text_color=COLORS.amber,
                )
            elif result:
                pulled = result.get("pulled", {})
                pushed = result.get("pushed", {})
                sent = sum(int(pushed.get(key, 0)) for key in ("created", "updated", "deleted"))
                self.status_label.configure(
                    text=(
                        f"Calendari sincronizzati: {pulled.get('inserted', 0)} nuovi, "
                        f"{pulled.get('updated', 0)} aggiornati, {pulled.get('removed', 0)} rimossi, "
                        f"{sent} modifiche inviate."
                    ),
                    text_color=COLORS.green,
                )
        elif kind == "flush_finished" and data.get("error"):
            self.status_label.configure(text=str(data["error"]), text_color=COLORS.amber)
        if kind in {"sync_finished", "flush_finished"}:
            render_state(self).forget("events")
            self._refresh()

    def destroy(self) -> None:
        if self._elapsed_job is not None:
            try:
                self.after_cancel(self._elapsed_job)
            except (tk.TclError, ValueError):
                pass
            self._elapsed_job = None
        super().destroy()

    def open_help(self) -> None:
        open_calendar_help(self, self.app, open_settings=lambda: self.app.show_view("settings"))

    # --------------------------------------------------------------- dialogo
    def open_event_dialog(self, event: dict[str, Any] | None) -> None:
        if event is not None and event.get("id"):
            fresh = self.db.get_event(int(event["id"]))
            if fresh is None:
                self._refresh()
                return
            event = fresh
        EventDialog(self, event, default_day=self._anchor)

    def after_event_change(self, message: str) -> None:
        self.sync.request_flush()
        render_state(self).forget("events")
        self._refresh()
        self._update_elapsed()
        self.app.toast(message)
        subjects = self.app.views.get("subjects")
        if subjects is not None:
            subjects.refresh_if_visible()

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
            "Tutti gli eventi verranno rimossi da StudyFlow (non da Google: gli eventi "
            "sincronizzati torneranno alla prossima sincronizzazione). I materiali e le "
            "sessioni resteranno, ma perderanno il collegamento all'evento. Continuare?",
            parent=self.winfo_toplevel(),
        ):
            return
        removed = self.db.clear_calendar_events()
        self.status_label.configure(
            text=f"Rimossi {removed} eventi.", text_color=COLORS.text_muted
        )
        self._refresh()


def _parse_day(text: str) -> date | None:
    value = str(text or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(text: str) -> tuple[int, int] | None:
    value = str(text or "").strip().replace(".", ":")
    if value.isdigit() and len(value) in (1, 2):
        value += ":00"
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


class EventDialog(ctk.CTkToplevel):
    """Crea / Modifica / Elimina Evento con materia, calendario e colore Google."""

    def __init__(self, view: CalendarView, event: dict[str, Any] | None, default_day: date):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.sync = view.sync
        self.event = event
        self.palette = self.sync.event_palette()
        self._color_id: str | None = (str(event.get("color_id") or "") or None) if event else None
        self._swatches: dict[str, ctk.CTkButton] = {}
        start = parse_datetime(str(event.get("start"))) if event else None
        end = parse_datetime(str(event.get("end"))) if event else None
        if start is None:
            now = datetime.now()
            base = datetime.combine(default_day, datetime.min.time())
            hour = now.hour + 1 if default_day == now.date() else 9
            start = base.replace(hour=min(22, hour))
            end = start + timedelta(hours=1)
        if end is None or end <= start:
            end = start + timedelta(hours=1)
        self._start = start
        self._end = end
        self._day_offset = max(0, (end.date() - start.date()).days)
        self.readonly_reason = self._readonly_reason()

        self.title("Modifica evento" if event else "Nuovo evento")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 640, 700)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _event: self._close())
        self.after(80, self._focus_title)

    def _readonly_reason(self) -> str:
        if not self.event:
            return ""
        if self.event.get("source") == EVENT_SOURCE_ICAL:
            return (
                "Evento letto da un indirizzo iCal (sola lettura): modificalo su Google "
                "Calendar, StudyFlow lo aggiornera' alla prossima sincronizzazione."
            )
        calendar_id = self.event.get("google_calendar_id")
        if calendar_id and not self.sync.is_calendar_writable(calendar_id):
            info = self.sync.calendar_info(calendar_id) or {}
            return (
                f"Il calendario Google \"{info.get('summary', calendar_id)}\" e' in sola "
                "lettura: l'evento non puo' essere modificato da StudyFlow."
            )
        return ""

    # ------------------------------------------------------------- layout
    def _label(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent, text=text, font=font(11, "bold"), text_color=COLORS.text_muted, anchor="w"
        )

    def _entry(self, parent, variable: ctk.StringVar, placeholder: str, width: int = 0) -> ctk.CTkEntry:
        options: dict[str, Any] = {}
        if width:
            options["width"] = width
        return ctk.CTkEntry(
            parent,
            height=36,
            textvariable=variable,
            placeholder_text=placeholder,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
            **options,
        )

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Modifica evento" if self.event else "Nuovo evento",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(22, 2))
        info = (
            self.readonly_reason
            or (
                "Evento importato da file .ics: le modifiche restano solo in StudyFlow e "
                "una nuova importazione dello stesso file puo' sovrascriverle."
                if self.event and self.event.get("source") == EVENT_SOURCE_ICS
                else "La materia sceglie il calendario Google di destinazione e il colore."
            )
        )
        ctk.CTkLabel(
            self,
            text=info,
            font=font(11),
            text_color=COLORS.amber if self.readonly_reason else COLORS.text_muted,
            anchor="w",
            wraplength=580,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 14))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure(0, weight=1)

        self._label(form, "Titolo *").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.title_var = ctk.StringVar(value=str(self.event.get("title") or "") if self.event else "")
        self.title_entry = self._entry(form, self.title_var, "es. Lezione di Sistemi Operativi")
        self.title_entry.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self._label(form, "Materia").grid(row=2, column=0, sticky="w", pady=(0, 5))
        subjects = self.db.list_subject_tags()
        current_subject = str(self.event.get("subject") or "") if self.event else ""
        if current_subject and current_subject.casefold() not in {s.casefold() for s in subjects}:
            subjects.append(current_subject)
        self.subject_var = ctk.StringVar(value=current_subject or NO_SUBJECT_LABEL)
        subject_row = ctk.CTkFrame(form, fg_color="transparent")
        subject_row.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        subject_row.grid_columnconfigure(0, weight=1)
        ctk.CTkComboBox(
            subject_row,
            values=[NO_SUBJECT_LABEL, *subjects],
            variable=self.subject_var,
            height=36,
            font=font(12),
            dropdown_font=font(12),
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            text_color=COLORS.text,
            corner_radius=9,
            command=lambda _value: self._on_subject_change(),
        ).grid(row=0, column=0, sticky="ew")
        self.subject_var.trace_add("write", lambda *_args: self._on_subject_change())
        self.destination_label = ctk.CTkLabel(
            form, text="", font=font(10), text_color=COLORS.text_faint, anchor="w"
        )
        self.destination_label.grid(row=4, column=0, sticky="w", pady=(0, 12))

        self._label(form, "Colore").grid(row=5, column=0, sticky="w", pady=(0, 5))
        colors_row = ctk.CTkFrame(form, fg_color="transparent")
        colors_row.grid(row=6, column=0, sticky="w", pady=(0, 12))
        self._default_swatch = ctk.CTkButton(
            colors_row,
            text="Materia",
            width=70,
            height=26,
            font=font(10, "bold"),
            corner_radius=13,
            border_width=2,
            command=lambda: self._pick_color(None),
        )
        self._default_swatch.pack(side="left", padx=(0, 8))
        for color_id, entry in self.palette.items():
            swatch = ctk.CTkButton(
                colors_row,
                text="",
                width=26,
                height=26,
                corner_radius=13,
                fg_color=entry["background"],
                hover_color=entry["background"],
                border_width=2,
                border_color=COLORS.bg,
                command=lambda value=color_id: self._pick_color(value),
            )
            swatch.pack(side="left", padx=2)
            self._swatches[color_id] = swatch

        when = ctk.CTkFrame(form, fg_color="transparent")
        when.grid(row=7, column=0, sticky="ew", pady=(0, 6))
        for column in range(3):
            when.grid_columnconfigure(column, weight=1)
        self.date_var = ctk.StringVar(value=self._start.strftime("%d/%m/%Y"))
        self.start_var = ctk.StringVar(value=self._start.strftime("%H:%M"))
        self.end_var = ctk.StringVar(value=self._end.strftime("%H:%M"))
        for column, (label, variable, placeholder) in enumerate(
            (
                ("Data *", self.date_var, "GG/MM/AAAA"),
                ("Ora inizio", self.start_var, "HH:MM"),
                ("Ora fine", self.end_var, "HH:MM"),
            )
        ):
            frame = ctk.CTkFrame(when, fg_color="transparent")
            frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            self._label(frame, label).pack(anchor="w", pady=(0, 5))
            entry = self._entry(frame, variable, placeholder)
            entry.pack(fill="x")
            if column == 1:
                self.start_entry = entry
            elif column == 2:
                self.end_entry = entry
        self.all_day_var = ctk.BooleanVar(value=bool(self.event and self.event.get("all_day")))
        ctk.CTkCheckBox(
            form,
            text="Tutto il giorno",
            variable=self.all_day_var,
            font=font(11),
            text_color=COLORS.text_muted,
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            border_color=COLORS.border,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=5,
            command=self._toggle_all_day,
        ).grid(row=8, column=0, sticky="w", pady=(4, 12))

        self._label(form, "Descrizione").grid(row=9, column=0, sticky="w", pady=(0, 5))
        self.description_box = ctk.CTkTextbox(
            form,
            height=96,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            border_width=1,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        self.description_box.grid(row=10, column=0, sticky="ew")
        if self.event and self.event.get("description"):
            self.description_box.insert("1.0", str(self.event["description"]))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=28, pady=(18, 22))
        buttons.grid_columnconfigure(1, weight=1)
        state = "disabled" if self.readonly_reason else "normal"
        if self.event:
            ctk.CTkButton(
                buttons,
                text="Elimina",
                width=96,
                height=40,
                font=font(12),
                fg_color="transparent",
                hover_color=COLORS.red_dark,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.red,
                corner_radius=10,
                state=state,
                command=self._delete,
            ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            buttons,
            text="Annulla",
            width=104,
            height=40,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self._close,
        ).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="Salva",
            width=120,
            height=40,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            state=state,
            command=self._save,
        ).grid(row=0, column=3, sticky="e")

        self._toggle_all_day()
        self._on_subject_change()

    # -------------------------------------------------------------- logica
    def _selected_subject(self) -> str:
        value = self.subject_var.get().strip()
        return "" if value == NO_SUBJECT_LABEL else value

    def _subject_color(self) -> str | None:
        _calendar, color = self.db.get_subject_calendar(self._selected_subject())
        return normalize_hex(color)

    def _on_subject_change(self) -> None:
        if not self.winfo_exists():
            return
        subject = self._selected_subject()
        calendar_id, _color = self.db.get_subject_calendar(subject)
        feed = self.sync.ical.for_subject(subject)
        if feed is not None and not (calendar_id and self.sync.is_connected()):
            text = (
                "Solo in StudyFlow: il calendario Google della materia e' collegato "
                "tramite indirizzo iCal (sola lettura)."
            )
        elif calendar_id and self.sync.is_connected():
            info = self.sync.calendar_info(calendar_id) or {}
            text = f"Calendario Google di destinazione: {info.get('summary', calendar_id)}"
            if not self.sync.is_calendar_writable(calendar_id):
                text += " (sola lettura: l'evento restera' solo in StudyFlow)"
        elif calendar_id:
            text = "Materia collegata a Google: l'evento verra' inviato quando ricolleghi l'account."
        elif self.event and self.event.get("google_calendar_id"):
            text = "Materia senza calendario Google: l'evento resta nel calendario attuale."
        else:
            text = "Solo in StudyFlow: la materia non ha un calendario Google collegato."
        self.destination_label.configure(text=text)
        self._refresh_swatches()

    def _pick_color(self, color_id: str | None) -> None:
        self._color_id = color_id
        self._refresh_swatches()

    def _refresh_swatches(self) -> None:
        subject_color = self._subject_color() or COLORS.blue
        self._default_swatch.configure(
            fg_color=subject_color,
            hover_color=subject_color,
            text_color=text_color_for(subject_color),
            border_color=COLORS.text if self._color_id is None else COLORS.bg,
        )
        for color_id, swatch in self._swatches.items():
            swatch.configure(border_color=COLORS.text if color_id == self._color_id else COLORS.bg)

    def _toggle_all_day(self) -> None:
        state = "disabled" if self.all_day_var.get() else "normal"
        self.start_entry.configure(state=state)
        self.end_entry.configure(state=state)

    def _focus_title(self) -> None:
        try:
            self.title_entry.focus_set()
        except tk.TclError:
            pass

    def _collect(self) -> dict[str, Any] | None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showerror("Titolo mancante", "Inserisci il titolo dell'evento.", parent=self)
            return None
        day = _parse_day(self.date_var.get())
        if day is None:
            messagebox.showerror("Data non valida", "Usa il formato GG/MM/AAAA.", parent=self)
            return None
        if self.all_day_var.get():
            start = datetime.combine(day, datetime.min.time())
            end = start + timedelta(days=max(1, self._day_offset))
        else:
            start_time = _parse_time(self.start_var.get())
            end_time = _parse_time(self.end_var.get())
            if start_time is None or end_time is None:
                messagebox.showerror("Ora non valida", "Usa il formato HH:MM.", parent=self)
                return None
            start = datetime.combine(day, datetime.min.time()).replace(
                hour=start_time[0], minute=start_time[1]
            )
            end_day = day + timedelta(days=self._day_offset if not (self.event and self.event.get("all_day")) else 0)
            end = datetime.combine(end_day, datetime.min.time()).replace(
                hour=end_time[0], minute=end_time[1]
            )
            if end <= start:
                messagebox.showerror(
                    "Orario non valido", "L'ora di fine deve essere successiva all'inizio.", parent=self
                )
                return None
        color_hex = None
        if self._color_id is not None:
            color_hex = (self.palette.get(self._color_id) or {}).get("background")
        elif self._subject_color() is None and self.event and self.event.get("google_calendar_id"):
            # Materia senza colore: resta il colore del calendario Google dell'evento.
            info = self.sync.calendar_info(self.event.get("google_calendar_id")) or {}
            color_hex = info.get("backgroundColor") or self.event.get("color_hex")
        return {
            "title": title,
            "subject": self._selected_subject(),
            "start": start,
            "end": end,
            "all_day": bool(self.all_day_var.get()),
            "description": self.description_box.get("1.0", "end").strip(),
            "color_id": self._color_id,
            "color_hex": color_hex,
        }

    def _save(self) -> None:
        values = self._collect()
        if values is None:
            return
        calendar_id, _color = self.db.get_subject_calendar(values["subject"])
        push = bool(calendar_id) and self.sync.is_connected() and self.sync.is_calendar_writable(calendar_id)
        try:
            self.db.save_local_event(
                int(self.event["id"]) if self.event else None,
                values["title"],
                values["subject"],
                values["start"],
                values["end"],
                all_day=values["all_day"],
                description=values["description"],
                color_hex=values["color_hex"],
                color_id=values["color_id"],
                google_connected=push,
            )
        except ValueError as exc:
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        except Exception as exc:
            self.app.logger.exception("Salvataggio evento non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        self._close()
        self.view.after_event_change("Evento salvato." if self.event else "Evento creato.")

    def _delete(self) -> None:
        if not self.event:
            return
        on_google = bool(self.event.get("google_event_id"))
        if not messagebox.askyesno(
            "Eliminare l'evento?",
            (
                "L'evento verra' eliminato da StudyFlow e da Google Calendar. "
                if on_google
                else "L'evento verra' eliminato da StudyFlow. "
            )
            + "Sessioni e materiali collegati restano, senza collegamento. Continuare?",
            parent=self,
        ):
            return
        try:
            self.db.delete_local_event(int(self.event["id"]))
        except Exception as exc:
            self.app.logger.exception("Eliminazione evento non riuscita.")
            messagebox.showerror("Eliminazione non riuscita", str(exc), parent=self)
            return
        self._close()
        self.view.after_event_change("Evento eliminato.")

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
