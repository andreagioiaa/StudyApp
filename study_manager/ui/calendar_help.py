"""Tutorial in-app: come collegare i calendari Google a StudyFlow.

Due metodi, in due schede:

- **Indirizzo iCal** (consigliato): nessun login, sola lettura;
- **API Google** (avanzato): login OAuth, sincronizzazione bidirezionale.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from typing import Any, Callable

import customtkinter as ctk

from ..config import COLORS
from .theme import font, mono
from .widgets import center_window, clear_children, set_window_icon


GOOGLE_CALENDAR_URL = "https://calendar.google.com"
CLOUD_CONSOLE_URL = "https://console.cloud.google.com/"
CALENDAR_API_URL = "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"
CREDENTIALS_URL = "https://console.cloud.google.com/apis/credentials"

TAB_ICAL = "Indirizzo iCal (consigliato)"
TAB_API = "API Google (avanzato)"


# Ogni passo: (titolo, testo, esempio facoltativo in monospazio)
ICAL_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "Apri Google Calendar dal computer",
        "Vai su calendar.google.com con l'account che possiede il calendario. "
        "L'app per telefono non mostra questa impostazione.",
        "",
    ),
    (
        "Apri le impostazioni del calendario",
        "Nella colonna di sinistra, sotto \"I miei calendari\", passa il mouse sul "
        "calendario (es. \"Sistemi Operativi\"), clicca sui tre puntini ⋮ e scegli "
        "\"Impostazioni e condivisione\".",
        "",
    ),
    (
        "Copia l'indirizzo segreto",
        "Scorri fino alla sezione \"Integra calendario\". In \"Indirizzo segreto in "
        "formato iCal\" clicca sull'occhio per mostrarlo e poi sull'icona Copia. "
        "Non usare l'indirizzo pubblico in formato iCal: funziona solo se il calendario e' "
        "visibile a chiunque.",
        "https://calendar.google.com/calendar/ical/<id>/private-<codice>/basic.ics",
    ),
    (
        "Incollalo in StudyFlow",
        "Apri Impostazioni > \"Calendari tramite indirizzo iCal\", incolla "
        "l'indirizzo, scegli la materia (facoltativo) e premi \"Aggiungi\". "
        "Ripeti per ogni calendario.",
        "",
    ),
    (
        "Scegli il colore e sincronizza",
        "Clicca sul pallino colorato del calendario per sceglierne il colore (l'indirizzo "
        "iCal non contiene i colori di Google). Gli eventi arrivano all'avvio e con "
        "\"Sincronizza Ora\" nel Calendario.",
        "",
    ),
)

ICAL_NOTES: tuple[str, ...] = (
    "Sola lettura: gli eventi di Google si modificano su Google; quelli creati in "
    "StudyFlow restano solo in StudyFlow.",
    "Un evento eliminato su Google sparisce anche da StudyFlow; sessioni e "
    "materiali collegati restano.",
    "L'indirizzo segreto e' come una password: StudyFlow lo cifra e non lo mette "
    "nei backup. Se lo hai diffuso, usa \"Reimposta\" su Google e incolla quello nuovo.",
    "Google aggiorna questo indirizzo con un certo ritardo: una modifica appena "
    "fatta puo' comparire dopo un po'.",
)

API_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "Crea un progetto in Google Cloud",
        "Apri la Google Cloud Console con il tuo account Google e crea un nuovo "
        "progetto (es. \"StudyFlow\"). E' gratuito.",
        "",
    ),
    (
        "Abilita la Google Calendar API",
        "In \"API e servizi > Libreria\" cerca \"Google Calendar API\" e premi "
        "\"Abilita\".",
        "",
    ),
    (
        "Configura la schermata di consenso",
        "In \"API e servizi > Schermata consenso OAuth\" scegli il tipo \"Esterno\", "
        "inserisci nome app ed email. Poi pubblica l'app (\"In produzione\"): in "
        "modalita' \"Test\" Google fa scadere l'accesso ogni 7 giorni. Al primo login "
        "vedrai l'avviso \"app non verificata\": e' normale per un'app personale.",
        "",
    ),
    (
        "Crea le credenziali \"App desktop\"",
        "In \"API e servizi > Credenziali\" premi \"Crea credenziali > ID client "
        "OAuth\", tipo di applicazione \"App desktop\", poi scarica il file JSON.",
        "credentials.json  →  { \"installed\": { \"client_id\": ..., ... } }",
    ),
    (
        "Collega l'account in StudyFlow",
        "Apri Impostazioni > \"Google Calendar tramite API\", premi \"Carica "
        "credentials.json\" e poi \"Connetti account\": si apre il browser, accedi e "
        "accetta. I calendari con lo stesso nome di una materia vengono abbinati da soli.",
        "",
    ),
)

API_NOTES: tuple[str, ...] = (
    "Bidirezionale: gli eventi creati, modificati o eliminati in StudyFlow vengono "
    "inviati a Google, anche dopo un periodo offline.",
    "I colori dei calendari e dei singoli eventi arrivano direttamente da Google.",
    "Token e client_secret sono cifrati con Windows e non finiscono nei backup.",
)


class CalendarHelpDialog(ctk.CTkToplevel):
    """Finestra "Come Connettere Calendario"."""

    def __init__(
        self,
        master: Any,
        app: Any,
        initial: str = TAB_ICAL,
        open_settings: Callable[[], None] | None = None,
    ):
        super().__init__(master.winfo_toplevel())
        self.app = app
        self._open_settings = open_settings
        self.title("Come Connettere Calendario")
        self.configure(fg_color=COLORS.bg)
        self.transient(master.winfo_toplevel())
        try:
            set_window_icon(self, app.paths["icon"])
        except Exception:
            pass
        center_window(self, 760, 720)
        self.minsize(640, 520)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            self,
            text="Come Connettere Calendario",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(22, 2))
        ctk.CTkLabel(
            self,
            text=(
                "Due modi per portare i tuoi calendari Google in StudyFlow. Se vuoi solo "
                "vedere le lezioni nell'agenda, scegli l'indirizzo iCal: bastano 2 minuti."
            ),
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=690,
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 12))

        self.tabs = ctk.CTkSegmentedButton(
            self,
            values=[TAB_ICAL, TAB_API],
            height=34,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            selected_color=COLORS.accent_dark,
            selected_hover_color=COLORS.accent_dark,
            unselected_color=COLORS.surface_alt,
            unselected_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            command=self._show,
        )
        self.tabs.grid(row=2, column=0, sticky="w", padx=28, pady=(0, 12))

        self.body = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS.surface,
            corner_radius=14,
            border_width=1,
            border_color=COLORS.border,
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.body.grid(row=3, column=0, sticky="nsew", padx=28)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=28, pady=(14, 20))
        footer.grid_columnconfigure(0, weight=1)
        self.links = ctk.CTkFrame(footer, fg_color="transparent")
        self.links.grid(row=0, column=0, sticky="w")
        if self._open_settings is not None:
            ctk.CTkButton(
                footer,
                text="Vai alle Impostazioni",
                width=160,
                height=38,
                font=font(11, "bold"),
                fg_color=COLORS.accent,
                hover_color=COLORS.accent_hover,
                text_color=COLORS.bg,
                corner_radius=9,
                command=self._go_settings,
            ).grid(row=0, column=1, padx=(8, 8))
        ctk.CTkButton(
            footer,
            text="Chiudi",
            width=90,
            height=38,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=9,
            command=self.destroy,
        ).grid(row=0, column=2)

        self.bind("<Escape>", lambda _event: self.destroy())
        self.tabs.set(initial if initial in (TAB_ICAL, TAB_API) else TAB_ICAL)
        self._show(self.tabs.get())
        self.after(60, self._focus)

    def _focus(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- render
    def _show(self, tab: str) -> None:
        clear_children(self.body)
        clear_children(self.links)
        if tab == TAB_API:
            intro = (
                "Sincronizzazione completa nei due sensi, ma richiede una configurazione "
                "una tantum in Google Cloud (circa 10 minuti)."
            )
            steps, notes = API_STEPS, API_NOTES
            links = (
                ("Google Cloud Console", CLOUD_CONSOLE_URL),
                ("Calendar API", CALENDAR_API_URL),
                ("Credenziali", CREDENTIALS_URL),
            )
        else:
            intro = (
                "Nessun login e nessuna configurazione tecnica: copi un indirizzo per "
                "ogni calendario e StudyFlow legge gli eventi. Sola lettura."
            )
            steps, notes = ICAL_STEPS, ICAL_NOTES
            links = (("Apri Google Calendar", GOOGLE_CALENDAR_URL),)

        # Layout a righe con pack: ogni passo e' un frame (numero + testo), cosi'
        # le etichette lunghe non allargano una colonna condivisa.
        ctk.CTkLabel(
            self.body,
            text=intro,
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
            justify="left",
            wraplength=600,
        ).pack(fill="x", padx=18, pady=(16, 10))
        for number, (title, text, example) in enumerate(steps, start=1):
            step = ctk.CTkFrame(self.body, fg_color="transparent")
            step.pack(fill="x", padx=18, pady=(6, 4))
            ctk.CTkLabel(
                step,
                text=str(number),
                width=28,
                height=28,
                corner_radius=14,
                fg_color=COLORS.accent_dark,
                text_color=COLORS.text,
                font=font(12, "bold"),
            ).pack(side="left", anchor="n", padx=(0, 12))
            content = ctk.CTkFrame(step, fg_color="transparent")
            content.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                content,
                text=title,
                font=font(13, "bold"),
                text_color=COLORS.text,
                anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                content,
                text=text,
                font=font(11),
                text_color=COLORS.text_muted,
                anchor="w",
                justify="left",
                wraplength=560,
            ).pack(anchor="w", pady=(2, 0))
            if example:
                ctk.CTkLabel(
                    content,
                    text=example,
                    font=mono(10),
                    text_color=COLORS.accent,
                    fg_color=COLORS.surface_alt,
                    corner_radius=6,
                    anchor="w",
                    justify="left",
                    wraplength=560,
                ).pack(anchor="w", pady=(6, 0), ipadx=6, ipady=3)

        ctk.CTkLabel(
            self.body,
            text="Da sapere",
            font=font(12, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 4))
        for note in notes:
            ctk.CTkLabel(
                self.body,
                text=f"•  {note}",
                font=font(10),
                text_color=COLORS.text_faint,
                anchor="w",
                justify="left",
                wraplength=600,
            ).pack(fill="x", padx=18, pady=1)
        ctk.CTkFrame(self.body, height=14, fg_color="transparent").pack()

        for label, url in links:
            ctk.CTkButton(
                self.links,
                text=f"{label}  ↗",
                height=38,
                font=font(11),
                fg_color=COLORS.surface_alt,
                hover_color=COLORS.surface_hover,
                border_width=1,
                border_color=COLORS.border,
                text_color=COLORS.text,
                corner_radius=9,
                command=lambda value=url: webbrowser.open(value),
            ).pack(side="left", padx=(0, 6))

    def _go_settings(self) -> None:
        callback = self._open_settings
        self.destroy()
        if callback is not None:
            callback()


def open_calendar_help(
    master: Any,
    app: Any,
    initial: str = TAB_ICAL,
    open_settings: Callable[[], None] | None = None,
) -> CalendarHelpDialog:
    return CalendarHelpDialog(master, app, initial=initial, open_settings=open_settings)
