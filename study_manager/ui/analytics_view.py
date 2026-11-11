"""Dashboard Analisi e Statistiche con Matplotlib caricato in modo lazy."""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, time, timedelta
from typing import Any

import customtkinter as ctk

from ..config import COLORS, quality_score
from ..utils import month_bounds
from .theme import font
from .widgets import Card, PageHeader, StatCard, clear_children, separator, render_state


LOGGER = logging.getLogger(__name__)


class AnalyticsView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self._charts_ready = False
        self._charts_started = False
        self._range_days = 7
        self._chart1_mode = "Giorni"
        self._chart2_mode = "Fasce orarie"
        self._hours_canvas = None
        self._attempts_canvas = None
        self._hours_figure = None
        self._attempts_figure = None
        self._hour_data: list[dict[str, Any]] = []
        self._attempt_hour_data: list[dict[str, Any]] = []
        self._attempt_source_data: list[dict[str, Any]] = []

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

        header_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header_row.grid_columnconfigure(0, weight=1)
        PageHeader(
            header_row,
            "Analisi & Statistiche",
            "Studio effettivo, qualita' dell'attenzione e distrazioni intercettate.",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header_row,
            text="Aggiorna",
            width=98,
            height=36,
            font=font(11, "bold"),
            fg_color=COLORS.surface,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=self.refresh,
        ).grid(row=0, column=1, sticky="e", pady=(4, 0))

        self._build_kpis()
        self._build_charts()

    # ------------------------------------------------------------------ layout
    def _build_kpis(self) -> None:
        grid = ctk.CTkFrame(self.scroll, fg_color="transparent")
        grid.grid(row=1, column=0, sticky="ew")
        for column in range(3):
            grid.grid_columnconfigure(column, weight=1, uniform="kpi")
        self.today_card = StatCard(grid, "OGGI", "0.0 h", "Tempo studiato", COLORS.accent)
        self.today_card.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.week_card = StatCard(grid, "QUESTA SETTIMANA", "0.0 h", "Da lunedi'", COLORS.blue)
        self.week_card.grid(row=0, column=1, sticky="ew", padx=8, pady=(0, 8))
        self.month_card = StatCard(grid, "QUESTO MESE", "0.0 h", "Mese corrente", COLORS.purple)
        self.month_card.grid(row=0, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.quality_card = StatCard(grid, "QUALITY SCORE", "0%", "Mese corrente", COLORS.accent)
        self.quality_card.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(8, 0))
        self.clean_card = StatCard(grid, "POMODORI PULITI", "0%", "Senza distrazioni", COLORS.green)
        self.clean_card.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.attempts_card = StatCard(grid, "DISTRAZIONI BLOCCATE", "0", "Mese corrente", COLORS.red)
        self.attempts_card.grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(8, 0))

    def _build_charts(self) -> None:
        controls = ctk.CTkFrame(self.scroll, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", pady=(16, 8))
        controls.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            controls,
            text="Andamento dello studio",
            font=font(14, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.range_switch = ctk.CTkSegmentedButton(
            controls,
            values=["7 giorni", "30 giorni", "90 giorni"],
            width=250,
            height=32,
            font=font(11, "bold"),
            fg_color=COLORS.surface,
            selected_color=COLORS.accent_dark,
            selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface,
            unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            command=self._change_range,
        )
        self.range_switch.set("7 giorni")
        self.range_switch.grid(row=0, column=1, sticky="e")

        charts = ctk.CTkFrame(self.scroll, fg_color="transparent")
        charts.grid(row=3, column=0, sticky="ew")
        charts.grid_columnconfigure((0, 1), weight=1, uniform="charts")

        hours_card = Card(charts)
        hours_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        hours_card.grid_columnconfigure(0, weight=1)
        hours_header = ctk.CTkFrame(hours_card, fg_color="transparent")
        hours_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        hours_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hours_header,
            text="Ore studiate",
            font=font(13, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w")
        self.hours_switch = ctk.CTkSegmentedButton(
            hours_header,
            values=["Giorni", "Materie"],
            width=150,
            height=28,
            font=font(10, "bold"),
            fg_color=COLORS.surface_alt,
            selected_color=COLORS.accent_dark,
            selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt,
            unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            command=self._change_chart1,
        )
        self.hours_switch.set("Giorni")
        self.hours_switch.grid(row=0, column=1, sticky="e")
        self.hours_body = ctk.CTkFrame(
            hours_card, fg_color=COLORS.surface, height=300
        )
        self.hours_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))
        self.hours_body.grid_propagate(False)
        self.hours_loading = ctk.CTkLabel(
            self.hours_body,
            text="Grafico caricato alla prima apertura...",
            font=font(11),
            text_color=COLORS.text_faint,
        )
        self.hours_loading.place(relx=0.5, rely=0.5, anchor="center")

        attempts_card = Card(charts)
        attempts_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        attempts_card.grid_columnconfigure(0, weight=1)
        attempts_header = ctk.CTkFrame(attempts_card, fg_color="transparent")
        attempts_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        attempts_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            attempts_header,
            text="Tentativi di distrazione",
            font=font(13, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w")
        self.attempts_switch = ctk.CTkSegmentedButton(
            attempts_header,
            values=["Fasce orarie", "Sorgenti"],
            width=190,
            height=28,
            font=font(10, "bold"),
            fg_color=COLORS.surface_alt,
            selected_color=COLORS.accent_dark,
            selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt,
            unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            command=self._change_chart2,
        )
        self.attempts_switch.set("Fasce orarie")
        self.attempts_switch.grid(row=0, column=1, sticky="e")
        self.attempts_body = ctk.CTkFrame(
            attempts_card, fg_color=COLORS.surface, height=300
        )
        self.attempts_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))
        self.attempts_body.grid_propagate(False)
        self.attempts_loading = ctk.CTkLabel(
            self.attempts_body,
            text="Grafico caricato alla prima apertura...",
            font=font(11),
            text_color=COLORS.text_faint,
        )
        self.attempts_loading.place(relx=0.5, rely=0.5, anchor="center")

        self._build_quality_explanation()

    def _build_quality_explanation(self) -> None:
        card = Card(self.scroll)
        card.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="Come viene calcolato il Quality Score",
            font=font(12, "bold"),
            text_color=COLORS.text,
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            card,
            text=(
                "35% completamento dei Pomodori avviati · 40% Pomodori completati senza "
                "distrazioni · 25% attenzione, penalizzata dalle interruzioni per ora. "
                "Il punteggio e' 0 quando non ci sono sessioni nel periodo."
            ),
            font=font(10),
            text_color=COLORS.text_muted,
            anchor="w",
            wraplength=850,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))

    # ------------------------------------------------------------------ refresh
    def on_show(self) -> None:
        self.refresh()
        if not self._charts_started:
            self._charts_started = True
            self.after(80, self._init_matplotlib)

    def prepare_in_background(self) -> None:
        """Carica Matplotlib e disegna i grafici prima della prima apertura."""
        if self._charts_started:
            return
        self._charts_started = True
        self.refresh()
        self.after(50, self._init_matplotlib)

    def refresh_if_visible(self) -> None:
        if self.app.is_view_visible(self):
            self.refresh()

    def _change_range(self, value: str) -> None:
        self._range_days = int(value.split()[0])
        self.refresh()

    def _change_chart1(self, value: str) -> None:
        self._chart1_mode = value
        self._draw_charts()

    def _change_chart2(self, value: str) -> None:
        self._chart2_mode = value
        self._draw_charts()

    def refresh(self) -> None:
        now = datetime.now()
        today = now.date()
        today_start = datetime.combine(today, time.min)
        week_start = today - timedelta(days=today.weekday())
        month_start_date, _ = month_bounds(today)
        month_start = datetime.combine(month_start_date, time.min)
        end = now + timedelta(seconds=1)
        try:
            stats_today = self.db.period_stats(self._fmt(today_start), self._fmt(end))
            stats_week = self.db.period_stats(self._fmt(datetime.combine(week_start, time.min)), self._fmt(end))
            stats_month = self.db.period_stats(self._fmt(month_start), self._fmt(end))
        except Exception:
            LOGGER.exception("Lettura statistiche non riuscita.")
            return

        self.today_card.set(
            f"{stats_today['minutes'] / 60:.1f} h",
            f"{stats_today['completed']} Pomodori completati",
            COLORS.accent,
        )
        self.week_card.set(
            f"{stats_week['minutes'] / 60:.1f} h",
            f"{stats_week['completed']} Pomodori completati",
            COLORS.blue,
        )
        self.month_card.set(
            f"{stats_month['minutes'] / 60:.1f} h",
            f"{stats_month['completed']} Pomodori completati",
            COLORS.purple,
        )
        score = quality_score(stats_month)
        score_color = COLORS.green if score >= 75 else COLORS.amber if score >= 50 else COLORS.red
        self.quality_card.set(
            f"{score:.0f}%",
            self._quality_label(score),
            score_color,
        )
        clean_rate = (
            stats_month["clean_completed"] / stats_month["completed"] * 100
            if stats_month["completed"]
            else 0.0
        )
        self.clean_card.set(
            f"{clean_rate:.0f}%",
            f"{stats_month['clean_completed']}/{stats_month['completed']} Pomodori puliti",
            COLORS.green if clean_rate >= 70 else COLORS.amber if clean_rate >= 40 else COLORS.red,
        )
        self.attempts_card.set(
            str(stats_month["attempts"]),
            "Siti web e applicazioni",
            COLORS.red if stats_month["attempts"] else COLORS.text_faint,
        )

        chart_end_date = today + timedelta(days=1)
        chart_start_date = today - timedelta(days=self._range_days - 1)
        chart_start = datetime.combine(chart_start_date, time.min)
        chart_end = datetime.combine(chart_end_date, time.min)
        self._hour_data = self.db.hours_by_day(self._fmt(chart_start), self._fmt(chart_end))
        self._attempt_hour_data = self.db.attempts_by_hour(
            self._fmt(chart_start), self._fmt(chart_end)
        )
        self._attempt_source_data = self.db.attempts_by_target(
            self._fmt(chart_start), self._fmt(chart_end)
        )
        if self._charts_ready:
            self._draw_charts(only_if_changed=True)

    @staticmethod
    def _fmt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _quality_label(score: float) -> str:
        if score >= 85:
            return "Attenzione eccellente"
        if score >= 70:
            return "Buona qualita'"
        if score >= 50:
            return "Da migliorare"
        if score > 0:
            return "Molte interruzioni"
        return "Nessuna sessione"

    # ------------------------------------------------------------------ charts
    def _init_matplotlib(self) -> None:
        if self._charts_ready or not self.winfo_exists():
            return
        self.hours_loading.configure(text="Caricamento grafici...")
        self.attempts_loading.configure(text="Caricamento grafici...")

        # L'import di Matplotlib e la generazione della cache font possono
        # richiedere alcuni secondi su hardware lento: avvengono in background,
        # mentre la creazione dei canvas Tk resta sul thread principale.
        def worker() -> None:
            try:
                # In una build one-file la cache font non deve finire nella
                # cartella temporanea _MEIPASS, eliminata a ogni chiusura.
                mpl_config = self.app.paths["data"] / "matplotlib"
                mpl_config.mkdir(parents=True, exist_ok=True)
                os.environ["MPLCONFIGDIR"] = str(mpl_config)
                import matplotlib

                matplotlib.use("TkAgg", force=True)
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                from matplotlib.figure import Figure

                modules = {
                    "Figure": Figure,
                    "FigureCanvasTkAgg": FigureCanvasTkAgg,
                }
            except Exception as exc:
                self.app.post(lambda error=str(exc): self._fail_matplotlib(error))
                return
            self.app.post(lambda: self._finish_matplotlib(modules))

        threading.Thread(
            target=worker,
            name="StudyFlow-MatplotlibInit",
            daemon=True,
        ).start()

    def _finish_matplotlib(self, modules: dict[str, Any]) -> None:
        if self._charts_ready or not self.winfo_exists():
            return
        try:
            Figure = modules["Figure"]
            FigureCanvasTkAgg = modules["FigureCanvasTkAgg"]
            self._figure_factory = Figure
            self.hours_loading.destroy()
            self.attempts_loading.destroy()
            self._hours_figure = Figure(
                figsize=(5.2, 3.05),
                dpi=100,
                facecolor=COLORS.surface,
                layout="constrained",
            )
            self._hours_canvas = FigureCanvasTkAgg(
                self._hours_figure, master=self.hours_body
            )
            hours_widget = self._hours_canvas.get_tk_widget()
            hours_widget.configure(bg=COLORS.surface, highlightthickness=0, bd=0)
            hours_widget.pack(fill="both", expand=True, padx=2, pady=2)

            self._attempts_figure = Figure(
                figsize=(5.2, 3.05),
                dpi=100,
                facecolor=COLORS.surface,
                layout="constrained",
            )
            self._attempts_canvas = FigureCanvasTkAgg(
                self._attempts_figure, master=self.attempts_body
            )
            attempts_widget = self._attempts_canvas.get_tk_widget()
            attempts_widget.configure(bg=COLORS.surface, highlightthickness=0, bd=0)
            attempts_widget.pack(fill="both", expand=True, padx=2, pady=2)
            self._charts_ready = True
            self._draw_charts()
        except Exception as exc:
            self._fail_matplotlib(str(exc))

    def _fail_matplotlib(self, error: str) -> None:
        if not self.winfo_exists():
            return
        LOGGER.error("Inizializzazione Matplotlib non riuscita: %s", error)
        for body in (self.hours_body, self.attempts_body):
            clear_children(body)
            ctk.CTkLabel(
                body,
                text=f"Grafici non disponibili: {error}",
                font=font(10),
                text_color=COLORS.red,
                wraplength=330,
            ).place(relx=0.5, rely=0.5, anchor="center")

    def _draw_charts(self, only_if_changed: bool = False) -> None:
        if not self._charts_ready or not self._hours_figure or not self._attempts_figure:
            return
        # Ridisegnare un canvas Matplotlib lo cancella e lo ricompone: a dati
        # invariati (tipico cambio di sezione) si lascia l'immagine com'e'.
        state = render_state(self)
        signature = (
            self._range_days,
            self._chart1_mode,
            self._chart2_mode,
            self._hour_data,
            self._attempt_hour_data,
            self._attempt_source_data,
        )
        if only_if_changed:
            with state.only_if_changed():
                if not state.should_render("charts", *signature):
                    return
        else:
            state.should_render("charts", *signature)
        self._draw_hours_chart()
        self._draw_attempts_chart()

    def _style_axes(self, ax) -> None:
        ax.set_facecolor(COLORS.surface)
        ax.grid(True, axis="y", color=COLORS.chart_grid, linewidth=0.6, alpha=0.65)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(colors=COLORS.text_faint, labelsize=7.5, length=0)
        ax.yaxis.label.set_color(COLORS.text_faint)
        ax.xaxis.label.set_color(COLORS.text_faint)

    def _empty_chart(self, ax, message: str) -> None:
        ax.clear()
        ax.set_facecolor(COLORS.surface)
        ax.set_axis_off()
        ax.text(
            0.5,
            0.5,
            message,
            ha="center",
            va="center",
            color=COLORS.text_faint,
            fontsize=9,
            transform=ax.transAxes,
        )

    def _draw_hours_chart(self) -> None:
        fig = self._hours_figure
        assert fig is not None
        fig.clear()
        ax = fig.add_subplot(111)
        if self._chart1_mode == "Giorni":
            today = date.today()
            start = today - timedelta(days=self._range_days - 1)
            values = {str(row["day"]): float(row["hours"] or 0) for row in self._hour_data}
            days = [start + timedelta(days=index) for index in range(self._range_days)]
            hours = [values.get(day.strftime("%Y-%m-%d"), 0.0) for day in days]
            if not any(hours):
                self._empty_chart(ax, "Nessuna ora registrata nel periodo")
            else:
                self._style_axes(ax)
                positions = list(range(len(days)))
                ax.bar(
                    positions,
                    hours,
                    color=COLORS.accent,
                    width=0.62,
                    edgecolor="none",
                    zorder=3,
                )
                step = max(1, len(days) // 8)
                ticks = positions[::step]
                labels = [days[index].strftime("%d/%m") for index in ticks]
                ax.set_xticks(ticks)
                ax.set_xticklabels(labels)
                ax.set_ylabel("Ore", fontsize=8)
                ax.margins(x=0.02)
                ax.grid(True, axis="y", color=COLORS.chart_grid, linewidth=0.6, alpha=0.65)
        else:
            grouped: dict[str, float] = {}
            for row in self.db.hours_by_subject(
                self._fmt(datetime.combine(date.today() - timedelta(days=self._range_days - 1), time.min)),
                self._fmt(datetime.combine(date.today() + timedelta(days=1), time.min)),
            ):
                grouped[str(row["subject"])] = float(row["hours"] or 0)
            if not grouped:
                self._empty_chart(ax, "Nessuna materia nel periodo")
            else:
                self._style_axes(ax)
                top = sorted(grouped.items(), key=lambda item: item[1])[-8:]
                labels = [item[0][:22] for item in top]
                values = [item[1] for item in top]
                ax.barh(labels, values, color=COLORS.blue, height=0.6, edgecolor="none", zorder=3)
                ax.grid(True, axis="x", color=COLORS.chart_grid, linewidth=0.6, alpha=0.65)
                ax.grid(False, axis="y")
                ax.set_xlabel("Ore", fontsize=8)
                ax.tick_params(axis="y", labelsize=7.5)
        fig.patch.set_facecolor(COLORS.surface)
        if self._hours_canvas:
            self._hours_canvas.draw_idle()

    def _draw_attempts_chart(self) -> None:
        fig = self._attempts_figure
        assert fig is not None
        fig.clear()
        ax = fig.add_subplot(111)
        if self._chart2_mode == "Fasce orarie":
            values = {int(row["hour"]): int(row["attempts"]) for row in self._attempt_hour_data}
            counts = [values.get(hour, 0) for hour in range(24)]
            if not any(counts):
                self._empty_chart(ax, "Nessun tentativo registrato nel periodo")
            else:
                self._style_axes(ax)
                positions = list(range(24))
                ax.bar(
                    positions,
                    counts,
                    color=COLORS.red,
                    width=0.68,
                    edgecolor="none",
                    zorder=3,
                )
                ax.set_xticks(positions[::3])
                ax.set_xticklabels([f"{hour:02d}" for hour in positions[::3]])
                ax.set_ylabel("Tentativi", fontsize=8)
                ax.margins(x=0.02)
                ax.grid(True, axis="y", color=COLORS.chart_grid, linewidth=0.6, alpha=0.65)
        else:
            if not self._attempt_source_data:
                self._empty_chart(ax, "Nessun tentativo registrato nel periodo")
            else:
                self._style_axes(ax)
                top = list(reversed(self._attempt_source_data[:10]))
                labels = []
                values = []
                for row in top:
                    prefix = "WEB" if row["source_type"] == "website" else "APP"
                    labels.append(f"{prefix} · {str(row['target_name'])[:22]}")
                    values.append(int(row["attempts"]))
                ax.barh(labels, values, color=COLORS.amber, height=0.6, edgecolor="none", zorder=3)
                ax.grid(True, axis="x", color=COLORS.chart_grid, linewidth=0.6, alpha=0.65)
                ax.grid(False, axis="y")
                ax.set_xlabel("Tentativi", fontsize=8)
                ax.tick_params(axis="y", labelsize=7)
        fig.patch.set_facecolor(COLORS.surface)
        if self._attempts_canvas:
            self._attempts_canvas.draw_idle()
