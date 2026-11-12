"""Finestra principale e composizione dei moduli."""

from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk

from ..config import APP_TITLE, APP_VERSION, COLORS, PHASE_LABELS
from ..database import Database
from ..logging_setup import setup_logging
from ..paths import ensure_data_layout
from ..services.audio_service import AudioController, audio_backend_info
from ..services.blocker import BlockerController
from ..services.pomodoro_service import PomodoroService
from ..services.transcription import TranscriptionController, transcription_backend_info
from ..services.youtube_audio import youtube_backend_info
from ..utils import safe_float
from .analytics_view import AnalyticsView
from .audio_view import AudioView
from .blocker_view import BlockerView
from .calendar_view import CalendarView
from .flashcard_view import FlashcardView
from .pdf_view import PdfView
from .pomodoro_view import PomodoroView
from .settings_view import SettingsView
from .subjects_view import SubjectsView
from .theme import configure_theme, font
from .video_view import VideoView
from .widgets import render_state, set_window_icon


LOGGER = logging.getLogger(__name__)


class StudyFlowApp(ctk.CTk):
    def __init__(self):
        configure_theme()
        super().__init__()
        self.paths = ensure_data_layout()
        self.logger = logging.getLogger("studyflow.app")
        self.db = Database(self.paths["db"])
        interrupted = self.db.recover_interrupted_video_jobs()
        if interrupted:
            self.logger.info(
                "Ripristinate %s trascrizioni interrotte alla chiusura precedente.",
                interrupted,
            )
        self.blocker = BlockerController(self.db, self.paths)
        self.audio = AudioController(self.paths["sounds"])
        self.transcriptions = TranscriptionController(self.paths)
        self.pomodoro = PomodoroService(self.db, self.blocker)
        self._audio_autoplay = False
        self._audio_track = ""
        self._audio_volume = 0.55
        self._audio_loop = True
        self._audio_auto_started: set[int] = set()
        self._audio_auto_paused: int | None = None
        self._audio_last_error = ""
        self.reload_audio_settings()
        self._closing = False
        self._shutdown_done = False
        self._poll_job: str | None = None
        self._toast_job: str | None = None
        self._current_view: str | None = None
        self._title_signature = ""
        self._last_result_id: int | None = None
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.views: dict[str, Any] = {}

        self.title(APP_TITLE)
        self.configure(fg_color=COLORS.bg)
        self.minsize(1060, 680)
        self._set_initial_geometry()
        set_window_icon(self, self.paths["icon"])
        self._set_app_user_model_id()
        self.report_callback_exception = self._report_exception
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._nav_mode = ""
        self._nav_layout_job: str | None = None
        self._build_sidebar()
        self._build_content()
        self._build_toast()
        self._setup_signals()

        self._prefill_views(exclude="pomodoro")
        self.show_view("pomodoro")
        # I grafici della dashboard (Matplotlib) si preparano in sottofondo poco
        # dopo l'avvio, cosi' anche "Analisi" e' pronta al primo clic.
        self.after(1500, self._warm_up_views)
        self.after(1000, self._global_poll)

    # ------------------------------------------------------------------ chrome
    def _set_initial_geometry(self) -> None:
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(1320, max(1060, screen_w - 80))
        height = min(860, max(680, screen_h - 90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _set_app_user_model_id() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "StudyFlow.Desktop.1"
            )
        except (AttributeError, OSError):
            pass

    # Voci di navigazione: (chiave, icona, etichetta, etichetta breve)
    NAV_ITEMS = (
        ("pomodoro", "◷", "Pomodoro", "Pomodoro"),
        ("audio", "♫", "Audio", "Audio"),
        ("videos", "▷", "Video e Trascrizione", "Video"),
        ("pdf", "▤", "PDF e Slide", "PDF"),
        ("blocker", "◈", "Blocco distrazioni", "Blocco"),
        ("calendar", "▦", "Calendario", "Calendario"),
        ("subjects", "▥", "Materie", "Materie"),
        ("flashcards", "▣", "Flashcard", "Flashcard"),
        ("analytics", "◔", "Analisi", "Analisi"),
        ("settings", "⚙", "Impostazioni", "Impostazioni"),
    )

    def _build_sidebar(self) -> None:
        """Barra di navigazione in alto: marchio, sezioni affiancate e stato del blocco.

        Se la finestra e' stretta le etichette si accorciano e, al limite,
        restano solo le icone (vedi ``_layout_nav``).
        """
        bar = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color=COLORS.sidebar, border_width=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(0, weight=1)
        bar.grid_columnconfigure(1, weight=1)
        self.topbar = bar

        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w", padx=(18, 14))
        ctk.CTkLabel(brand, text="StudyFlow", font=font(17, "bold"), text_color=COLORS.text, anchor="w").pack(
            anchor="w"
        )
        ctk.CTkLabel(
            brand, text=f"v{APP_VERSION} · locale e offline", font=font(9), text_color=COLORS.accent, anchor="w",
        ).pack(anchor="w")

        nav = ctk.CTkFrame(bar, fg_color="transparent")
        nav.grid(row=0, column=1, sticky="w")
        self.nav_frame = nav
        for column, (key, icon, label, _short) in enumerate(self.NAV_ITEMS):
            button = ctk.CTkButton(
                nav,
                text=f"{icon}  {label}",
                width=20,
                height=36,
                font=font(11, "bold"),
                fg_color="transparent",
                hover_color=COLORS.surface_alt,
                text_color=COLORS.text_muted,
                corner_radius=9,
                command=lambda view_key=key: self.show_view(view_key),
            )
            button.grid(row=0, column=column, padx=1)
            self._nav_buttons[key] = button

        status_card = ctk.CTkFrame(
            bar, fg_color=COLORS.surface, corner_radius=10, border_width=1, border_color=COLORS.border,
        )
        status_card.grid(row=0, column=2, sticky="e", padx=(10, 14), pady=8)
        self.status_dot = ctk.CTkLabel(status_card, text="●", font=font(11), text_color=COLORS.text_faint, width=14)
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(10, 2))
        self.status_title = ctk.CTkLabel(
            status_card, text="Blocco inattivo", font=font(10, "bold"), text_color=COLORS.text_muted, anchor="w",
            height=16,
        )
        self.status_title.grid(row=0, column=1, sticky="w", padx=(2, 10), pady=(4, 0))
        self.status_detail = ctk.CTkLabel(
            status_card, text="Si attiva con il Pomodoro", font=font(9), text_color=COLORS.text_faint, anchor="w",
            height=14,
        )
        self.status_detail.grid(row=1, column=1, sticky="w", padx=(2, 10), pady=(0, 4))
        self.status_card = status_card

        bar.bind("<Configure>", lambda _e: self._schedule_nav_layout())
        self.after(50, self._layout_nav)

    def _schedule_nav_layout(self) -> None:
        if self._nav_layout_job is not None:
            try:
                self.after_cancel(self._nav_layout_job)
            except (tk.TclError, ValueError):
                pass
        self._nav_layout_job = self.after(40, self._layout_nav)

    def _nav_texts(self, mode: str) -> dict[str, str]:
        texts = {}
        for key, icon, label, short in self.NAV_ITEMS:
            if mode == "full":
                texts[key] = f"{icon}  {label}"
            elif mode == "short":
                texts[key] = f"{icon} {short}"
            else:
                texts[key] = icon
        return texts

    def _layout_nav(self) -> None:
        """Sceglie etichette complete, brevi o solo icone in base alla larghezza."""
        self._nav_layout_job = None
        try:
            available = self.topbar.winfo_width()
            if available <= 1:
                return
            fixed = self.topbar.grid_slaves(row=0, column=0)[0].winfo_reqwidth() + self.status_card.winfo_reqwidth() + 58
            for mode in ("full", "short", "icons"):
                texts = self._nav_texts(mode)
                for key, button in self._nav_buttons.items():
                    if button.cget("text") != texts[key]:
                        button.configure(text=texts[key], width=36 if mode == "icons" else 20)
                self.nav_frame.update_idletasks()
                if self.nav_frame.winfo_reqwidth() + fixed <= available or mode == "icons":
                    self._nav_mode = mode
                    break
        except tk.TclError:
            pass

    def _build_content(self) -> None:
        self.content = ctk.CTkFrame(self, fg_color=COLORS.bg, corner_radius=0)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        # Tutte le viste vengono create all'avvio e restano in vita fino alla
        # chiusura: sono impilate nella stessa cella della griglia e il cambio
        # di sezione porta in primo piano quella scelta (tkraise). Nessuna vista
        # viene nascosta con grid_forget o distrutta, quindi i widget non vanno
        # ridisegnati a ogni cambio e non c'e' sfarfallio.
        self._view_factories = {
            "pomodoro": PomodoroView,
            "audio": AudioView,
            "videos": VideoView,
            "pdf": PdfView,
            "blocker": BlockerView,
            "calendar": CalendarView,
            "subjects": SubjectsView,
            "flashcards": FlashcardView,
            "analytics": AnalyticsView,
            "settings": SettingsView,
        }
        self.views = {}
        for key in self._view_factories:
            self._ensure_view(key)

    def _ensure_view(self, key: str):
        view = self.views.get(key)
        if view is None:
            factory = self._view_factories[key]
            started = time.perf_counter()
            try:
                view = factory(self.content, self)
            except Exception as exc:
                # Una vista difettosa non deve impedire l'avvio delle altre.
                self.logger.exception("Creazione della vista '%s' non riuscita.", key)
                view = self._error_view(key, exc)
            view.grid(row=0, column=0, sticky="nsew", padx=24, pady=(18, 12))
            view.lower()
            self.views[key] = view
            self.logger.debug(
                "Vista '%s' creata in %.0f ms.", key, (time.perf_counter() - started) * 1000
            )
        return view

    def _error_view(self, key: str, error: Exception) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        ctk.CTkLabel(
            frame,
            text=f"La sezione '{key}' non e' disponibile.",
            font=font(18, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).pack(anchor="w", pady=(10, 6))
        ctk.CTkLabel(
            frame,
            text=f"{error}\n\nDettagli in studyflow.log.",
            font=font(12),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
        ).pack(anchor="w")
        return frame

    def is_view_visible(self, view: Any) -> bool:
        """True se ``view`` e' la sezione in primo piano.

        Con le viste sempre presenti ``winfo_ismapped`` e' vero per tutte,
        quindi le viste chiedono all'app quale sia quella visibile.
        """
        if self._closing or self._current_view is None:
            return False
        return self.views.get(self._current_view) is view

    def _prefill_views(self, exclude: str = "") -> None:
        """Popola subito tutte le sezioni con i dati, ancora coperte.

        ``on_show`` carica i dati e ``on_hide`` ferma gli eventuali timer di
        aggiornamento, cosi' le sezioni nascoste non consumano CPU ma alla
        prima apertura sono gia' complete.
        """
        for key, view in list(self.views.items()):
            if key == exclude:
                continue
            try:
                if hasattr(view, "on_show"):
                    view.on_show()
                if hasattr(view, "on_hide"):
                    view.on_hide()
            except Exception:
                self.logger.exception("Precaricamento della vista '%s' non riuscito.", key)

    def _warm_up_views(self) -> None:
        if self._closing:
            return
        for key, view in list(self.views.items()):
            prepare = getattr(view, "prepare_in_background", None)
            if prepare is None:
                continue
            try:
                prepare()
            except Exception:
                self.logger.exception("Preparazione della vista '%s' non riuscita.", key)

    def _build_toast(self) -> None:
        self._toast = ctk.CTkLabel(
            self,
            text="",
            font=font(11, "bold"),
            text_color=COLORS.text,
            fg_color=COLORS.surface_alt,
            corner_radius=10,
            padx=16,
            height=38,
        )
        self._toast.place_forget()

    # ------------------------------------------------------------------ nav
    def show_view(self, key: str) -> None:
        if key not in self._view_factories:
            return
        if self._current_view == key:
            view = self._ensure_view(key)
            if hasattr(view, "on_show"):
                view.on_show()
            return
        old = self.views.get(self._current_view) if self._current_view else None
        if old is not None and hasattr(old, "on_hide"):
            old.on_hide()
        view = self._ensure_view(key)
        self._current_view = key
        # La vista si aggiorna (solo se i dati sono cambiati) mentre e' ancora
        # coperta, poi viene portata in primo piano: nessuna rimozione dalla
        # griglia, nessun ridisegno completo.
        if hasattr(view, "on_show"):
            view.on_show()
        view.tkraise()
        for nav_key, button in self._nav_buttons.items():
            active = nav_key == key
            colors = (
                COLORS.surface_alt if active else "transparent",
                COLORS.accent if active else COLORS.text_muted,
            )
            if getattr(button, "_studyflow_colors", None) != colors:
                button._studyflow_colors = colors
                button.configure(fg_color=colors[0], text_color=colors[1])

    # ------------------------------------------------------------------ events
    def start_from_event(self, event: dict[str, Any]) -> None:
        if not self._confirm_interrupt_if_needed(
            f"Avviare lo studio per '{event.get('title', 'evento')}'?"
        ):
            return
        ok, error = self.pomodoro.start_from_event(event)
        if not ok:
            messagebox.showwarning("StudyFlow", error, parent=self)
            return
        self.show_view("pomodoro")
        self.toast(f"Sessione avviata: {event.get('title', 'evento')}")

    def start_from_material(self, material: dict[str, Any]) -> None:
        if not self._confirm_interrupt_if_needed(
            f"Avviare lo studio per '{material.get('title', 'materiale')}'?"
        ):
            return
        ok, error = self.pomodoro.start_from_material(material)
        if not ok:
            messagebox.showwarning("StudyFlow", error, parent=self)
            return
        self.show_view("pomodoro")
        self.toast(f"Studio avviato: {material.get('title', 'materiale')}")

    def _confirm_interrupt_if_needed(self, message: str) -> bool:
        snapshot = self.pomodoro.snapshot()
        if snapshot["status"] in {"running", "paused"}:
            return messagebox.askyesno(
                "Sessione in corso",
                f"{message}\n\nLa sessione corrente verra' salvata come incompleta. Continuare?",
                parent=self,
            ) and self._reset_for_new_session()
        return True

    def _reset_for_new_session(self) -> bool:
        self.pomodoro.reset()
        return True

    def on_session_finished(self, result: dict[str, Any]) -> None:
        result_id = int(result.get("session_id", 0) or 0)
        if result_id and result_id == self._last_result_id:
            return
        self._last_result_id = result_id or self._last_result_id
        subjects = self.views.get("subjects")
        if subjects is not None:
            subjects.refresh_if_visible()
        analytics = self.views.get("analytics")
        if analytics is not None:
            analytics.refresh_if_visible()
        minutes = result.get("minutes", 0)
        if result.get("completed"):
            self.toast(f"Pomodoro completato · {minutes} minuti salvati.")
        else:
            self.toast(f"Sessione interrotta · {minutes} minuti salvati.")

    def toast(self, message: str) -> None:
        if self._closing:
            return
        if self._toast_job is not None:
            try:
                self.after_cancel(self._toast_job)
            except (tk.TclError, ValueError):
                pass
        self._toast.configure(text=message)
        self._toast.place(relx=1.0, rely=1.0, x=-26, y=-22, anchor="se")
        self._toast.lift()
        self._toast_job = self.after(3600, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast.place_forget()
        self._toast_job = None

    def update_window_title(self, snapshot: dict[str, Any]) -> None:
        if snapshot["status"] in {"running", "paused"}:
            phase = PHASE_LABELS.get(snapshot["phase"], snapshot["phase"])
            title = f"{snapshot['remaining_label']} · {phase} · {snapshot['subject']} | StudyFlow"
        else:
            title = APP_TITLE
        if title != self._title_signature:
            self._title_signature = title
            self.title(title)

    # ------------------------------------------------------------------ polling
    def post(self, callback: Callable[[], None]) -> None:
        """Accoda un aggiornamento UI da un thread di background.

        Tkinter non e' thread-safe: nessun widget viene toccato fuori dal thread
        principale. La coda viene svuotata dal polling globale.
        """
        if self._closing:
            return
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            if self._closing:
                return
            try:
                callback()
            except Exception:
                self.logger.exception("Errore eseguendo un aggiornamento UI accodato.")

    @staticmethod
    def _setting_bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def reload_audio_settings(self) -> None:
        settings = self.db.get_settings()
        self._audio_autoplay = self._setting_bool(settings.get("audio_autoplay", "0"))
        self._audio_track = str(settings.get("audio_track", "") or "")
        if self._audio_track and not self.audio.has_track(self._audio_track):
            self._audio_track = ""
        self._audio_volume = max(
            0.0, min(1.0, safe_float(settings.get("audio_volume", "0.55"), 0.55))
        )
        self._audio_loop = self._setting_bool(settings.get("audio_loop", "1"))
        self.audio.set_volume(self._audio_volume)
        self.audio.set_loop(self._audio_loop)
        if not self._audio_autoplay and self.audio.status()["source"] == "auto":
            self.audio.stop()
            self._audio_auto_paused = None
            self._audio_auto_started.clear()

    def refresh_after_import(self) -> None:
        """Ricarica configurazione e viste dopo un ripristino dei dati."""
        self.reload_audio_settings()
        try:
            self.blocker.reload_config()
        except Exception:
            self.logger.exception("Ricarica blocker dopo importazione non riuscita.")
        try:
            self.pomodoro.reload_config()
        except Exception:
            self.logger.exception("Ricarica Pomodoro dopo importazione non riuscita.")
        # Solo la sezione visibile si aggiorna subito; le altre ricostruiscono
        # alla prossima apertura (i dati importati cambiano la loro firma).
        current = self.views.get(self._current_view) if self._current_view else None
        for key, view in list(self.views.items()):
            render_state(view).forget()
            if view is not current:
                continue
            try:
                if hasattr(view, "on_show"):
                    view.on_show()
                elif hasattr(view, "refresh"):
                    view.refresh()
            except Exception:
                self.logger.exception("Aggiornamento vista '%s' non riuscito.", key)

    def _sync_audio(self, snapshot: dict[str, Any]) -> None:
        """Allinea la musica automatica allo stato del Pomodoro."""
        audio_status = self.audio.status()
        if not self._audio_autoplay or not self._audio_track:
            if audio_status["source"] == "auto":
                self.audio.stop()
                self._audio_auto_paused = None
                self._audio_auto_started.clear()
            return

        phase = str(snapshot.get("phase", ""))
        status = str(snapshot.get("status", ""))
        session_id = snapshot.get("session_id")
        if not isinstance(session_id, int):
            session_id = None

        if phase == "work" and status == "running" and session_id is not None:
            if (
                self._audio_auto_paused == session_id
                and audio_status["source"] == "auto"
                and audio_status["paused"]
            ):
                if self.audio.resume():
                    self._audio_auto_paused = None
                return
            if session_id in self._audio_auto_started:
                return
            if audio_status["source"] == "manual":
                # Una scelta manuale dell'utente ha la precedenza.
                self._audio_auto_started.add(session_id)
                return
            if audio_status["source"] == "auto":
                self.audio.stop()
            ok, error = self.audio.play(
                self._audio_track,
                loop=self._audio_loop,
                source="auto",
            )
            self._audio_auto_started.add(session_id)
            if ok:
                self._audio_last_error = ""
            elif error and error != self._audio_last_error:
                self._audio_last_error = error
                self.toast(f"Audio non avviato: {error}")
            return

        if phase == "work" and status == "paused" and session_id is not None:
            if audio_status["source"] == "auto" and audio_status["playing"]:
                if self.audio.pause():
                    self._audio_auto_paused = session_id
            return

        # Pausa, fine fase o timer fermo: interrompe solo l'audio automatico.
        if audio_status["source"] == "auto":
            self.audio.stop()
        self._audio_auto_paused = None

    def _global_poll(self) -> None:
        if self._closing:
            return
        self._drain_ui_queue()
        try:
            status = self.blocker.status()
            snapshot = self.pomodoro.snapshot()
            self.update_window_title(snapshot)
            self._sync_audio(snapshot)
            result = snapshot.get("last_result")
            if isinstance(result, dict):
                self.on_session_finished(result)
            if status["hosts_residual"]:
                dot, title = COLORS.amber, "Hosts da ripristinare"
                detail = "Usa Blocco distrazioni > Ripristina file hosts."
            elif status["active"]:
                dot = COLORS.green
                title = f"Blocco attivo · {status['attempts']} tentativi"
                sites = (
                    "siti on"
                    if status["site_state"] == "active"
                    else "siti in avvio"
                    if status["site_state"] == "starting"
                    else "siti non attivi"
                )
                apps = "app on" if status["apps_enabled"] else "app off"
                detail = f"{sites} · {apps}"
            else:
                dot, title = COLORS.text_faint, "Blocco inattivo"
                detail = "Si attiva con il Pomodoro"
            self.status_dot.configure(text_color=dot)
            self.status_title.configure(text=title)
            self.status_detail.configure(text=detail)
        except Exception:
            self.logger.exception("Errore nel polling globale.")
        self._poll_job = self.after(1000, self._global_poll)

    # ------------------------------------------------------------------ diagnostics
    def run_smoke_test(self) -> int:
        """Apre tutte le viste e prova timer/chiusura senza interazione manuale."""
        try:
            self.withdraw()
            self.update()
            self.db.set_settings(
                {
                    "block_sites_enabled": "0",
                    "block_apps_enabled": "0",
                    "auto_start_next": "0",
                    "web_trap_enabled": "0",
                    "audio_autoplay": "0",
                }
            )
            self.blocker.reload_config()
            self.reload_audio_settings()
            try:
                pygame_version = audio_backend_info()
                yt_version = youtube_backend_info()
                transcription_version = transcription_backend_info()
            except Exception as exc:
                raise RuntimeError(f"Backend audio/trascrizione non disponibile: {exc}") from exc
            self.logger.info(
                "Backend pronti: pygame %s, yt-dlp %s, %s.",
                pygame_version,
                yt_version,
                transcription_version,
            )
            smoke_video = os.environ.get("STUDYFLOW_SMOKE_TRANSCRIBE_VIDEO", "").strip()
            if smoke_video:
                smoke_target = Path(self.paths["transcripts"]) / "smoke-trascrizione.txt"
                outcome: dict[str, Any] = {}

                def on_smoke_done(event: dict[str, Any]) -> None:
                    outcome.update(event)

                started, start_error = self.transcriptions.start(
                    lesson_id=0,
                    video_path=smoke_video,
                    transcript_path=str(smoke_target),
                    model="base-q5_1",
                    language="it",
                    title="Smoke trascrizione",
                    subject="Smoke Test",
                    on_progress=lambda event: None,
                    on_done=on_smoke_done,
                )
                if not started:
                    raise RuntimeError(
                        f"Trascrizione di prova non avviata: {start_error}"
                    )
                deadline = time.monotonic() + 180.0
                while not outcome and time.monotonic() < deadline:
                    self.update()
                    self.update_idletasks()
                    time.sleep(0.05)
                if not outcome:
                    self.transcriptions.cancel()
                    self.transcriptions.shutdown()
                    raise RuntimeError("La trascrizione di prova non e' terminata in tempo.")
                self.transcriptions.shutdown()
                if not outcome.get("ok"):
                    raise RuntimeError(
                        f"Trascrizione di prova fallita: {outcome.get('error')}"
                    )
                result = dict(outcome.get("result") or {})
                output = Path(str(result.get("transcript_path") or ""))
                if not output.is_file() or output.stat().st_size <= 0:
                    raise RuntimeError("La trascrizione di prova non ha prodotto un file .txt.")
                self.logger.info(
                    "Smoke trascrizione completato: %s (%s blocchi).",
                    output,
                    result.get("blocks"),
                )
            smoke_url = os.environ.get("STUDYFLOW_SMOKE_YOUTUBE_URL", "").strip()
            if smoke_url:
                from ..services.youtube_audio import download_audio, probe_video

                info = probe_video(smoke_url)
                track = download_audio(
                    smoke_url,
                    self.paths["sounds"],
                    str(info["title"]),
                )
                ok, error = self.audio.play(track.name, loop=False, source="manual")
                if not ok:
                    raise RuntimeError(f"Riproduzione audio di prova fallita: {error}")
                self.audio.stop()
                self.logger.info("Smoke audio YouTube completato: %s.", track.name)
            for key in self._view_factories:
                self.show_view(key)
                for _ in range(8):
                    self.update()
                    self.update_idletasks()
                    time.sleep(0.03)
            analytics = self.views.get("analytics")
            deadline = time.monotonic() + 20.0
            while (
                analytics is not None
                and not analytics._charts_ready
                and time.monotonic() < deadline
            ):
                self.update()
                self.update_idletasks()
                time.sleep(0.05)
            if analytics is not None and not analytics._charts_ready:
                raise RuntimeError("I grafici non sono stati inizializzati.")
            self.show_view("pomodoro")
            started, error = self.pomodoro.start_current(subject="Smoke Test")
            if not started:
                raise RuntimeError(error or "Timer non avviato.")
            for _ in range(6):
                self.update()
                time.sleep(0.03)
            self.pomodoro.reset()
            self.update()
            self._closing = True
            self._shutdown_services()
            try:
                self.destroy()
            except tk.TclError:
                pass
            self.logger.info("Smoke test completato correttamente.")
            return 0
        except Exception:
            self.logger.exception("Smoke test fallito.")
            self.emergency_shutdown()
            try:
                self.destroy()
            except tk.TclError:
                pass
            return 1

    # ------------------------------------------------------------------ lifecycle
    def _report_exception(self, *args) -> None:
        if len(args) == 3:
            exc_type, exc_value, exc_tb = args
        else:
            exc_type, exc_value, exc_tb = sys.exc_info()
        self.logger.error(
            "Eccezione non gestita nella UI.",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        try:
            messagebox.showerror(
                "Errore imprevisto",
                f"Si e' verificato un errore:\n{exc_value}\n\nDettagli in studyflow.log.",
                parent=self,
            )
        except tk.TclError:
            pass

    def _setup_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                pass

    def _handle_signal(self, signum: int, frame: Any) -> None:
        del frame
        self.logger.warning("Segnale %s ricevuto: chiusura d'emergenza.", signum)
        try:
            self.after(0, self._emergency_ui_close)
        except (tk.TclError, RuntimeError):
            self.emergency_shutdown()

    def _emergency_ui_close(self) -> None:
        self.emergency_shutdown()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def on_close(self) -> None:
        if self._closing:
            return
        warnings: list[str] = []
        snapshot = self.pomodoro.snapshot()
        if snapshot["status"] in {"running", "paused"} and snapshot["phase"] == "work":
            warnings.append(
                "La sessione di studio corrente verra' salvata come incompleta."
            )
        transcription_job = self.transcriptions.current_job()
        if transcription_job is not None:
            warnings.append(
                "La trascrizione in corso verra' annullata. Il file .txt precedente, "
                "se presente, non verra' modificato."
            )
        pdf_view = self.views.get("pdf")
        if pdf_view is not None and pdf_view.is_busy():
            warnings.append("L'elaborazione PDF in corso verra' interrotta.")
        if warnings and not messagebox.askyesno(
            "Uscire da StudyFlow?",
            "\n\n".join(warnings) + "\n\nUscire comunque?",
            parent=self,
        ):
            return
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except (tk.TclError, ValueError):
                pass
            self._poll_job = None
        try:
            self.title("StudyFlow - chiusura e ripristino hosts...")
            self.update_idletasks()
        except tk.TclError:
            pass
        self._shutdown_services()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def emergency_shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._closing = True
        self._shutdown_services()

    def _shutdown_services(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.pomodoro.shutdown()
        except Exception:
            self.logger.exception("Chiusura Pomodoro non riuscita.")
        try:
            self.blocker.shutdown()
        except Exception:
            self.logger.exception("Chiusura blocker non riuscita.")
        try:
            self.audio.shutdown()
        except Exception:
            self.logger.exception("Chiusura audio non riuscita.")
        pdf_view = self.views.get("pdf")
        if pdf_view is not None:
            try:
                pdf_view.shutdown()
            except Exception:
                self.logger.exception("Chiusura modulo PDF non riuscita.")
        job = self.transcriptions.current_job()
        try:
            self.transcriptions.cancel()
        except Exception:
            self.logger.exception("Annullamento trascrizione non riuscito.")
        if job is not None:
            try:
                self.db.set_video_lesson_job(
                    int(job["lesson_id"]),
                    "cancelled",
                    0.0,
                    "Trascrizione interrotta alla chiusura dell'applicazione.",
                )
            except Exception:
                self.logger.exception("Salvataggio interruzione trascrizione non riuscito.")
        try:
            self.transcriptions.shutdown()
        except Exception:
            self.logger.exception("Chiusura trascrizione non riuscita.")
        try:
            self.db.close_all()
        except Exception:
            self.logger.exception("Chiusura database non riuscita.")


def run_app(debug: bool = False, smoke_test: bool = False) -> int:
    setup_logging(debug=debug)
    LOGGER.info("Avvio StudyFlow %s", APP_VERSION)
    app = StudyFlowApp()
    import atexit

    atexit.register(app.emergency_shutdown)
    if smoke_test:
        return app.run_smoke_test()
    app.mainloop()
    return 0
