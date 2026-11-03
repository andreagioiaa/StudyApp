"""Vista Materiali e gestione PDF/documenti."""

from __future__ import annotations

import os
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Any

import customtkinter as ctk

from ..config import COLORS
from ..utils import format_hours, safe_float
from .formatting import format_event_datetime
from .theme import font
from .widgets import Card, PageHeader, StatCard, center_window, clear_children, set_window_icon


class MaterialsView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.db = app.db
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header_row.grid_columnconfigure(0, weight=1)
        PageHeader(
            header_row,
            "Materiali di studio",
            "PDF, documenti e note collegati alle materie e agli eventi del calendario.",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header_row,
            text="+  Nuovo materiale",
            height=40,
            width=170,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=lambda: self._open_dialog(None),
        ).grid(row=0, column=1, sticky="e", pady=(4, 0))

        summary = ctk.CTkFrame(self, fg_color="transparent")
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for column in range(3):
            summary.grid_columnconfigure(column, weight=1, uniform="materials")
        self.count_card = StatCard(summary, "MATERIALI", "0", "File collegati", COLORS.blue)
        self.count_card.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.target_card = StatCard(summary, "ORE PREVISTE", "0.0 h", "Somma obiettivi", COLORS.purple)
        self.target_card.grid(row=0, column=1, sticky="ew", padx=8)
        self.logged_card = StatCard(summary, "ORE REGISTRATE", "0.0 h", "Studio effettivo", COLORS.accent)
        self.logged_card.grid(row=0, column=2, sticky="ew", padx=(8, 0))

        self.hint = ctk.CTkLabel(
            self,
            text="",
            font=font(10),
            text_color=COLORS.text_faint,
            anchor="w",
        )
        self.hint.grid(row=2, column=0, sticky="w", pady=(0, 6))

        self.materials_scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS.border,
            scrollbar_button_hover_color=COLORS.surface_hover,
        )
        self.materials_scroll.grid(row=3, column=0, sticky="nsew")
        self.materials_scroll.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        self.refresh()

    def refresh_if_visible(self) -> None:
        if self.winfo_ismapped():
            self.refresh()

    def refresh(self) -> None:
        materials = self.db.list_materials()
        clear_children(self.materials_scroll)
        self.count_card.set(str(len(materials)), "File collegati", COLORS.blue)
        target = sum(safe_float(row.get("target_hours", 0)) for row in materials)
        logged = sum(safe_float(row.get("logged_hours", 0)) for row in materials)
        self.target_card.set(format_hours(target), "Somma obiettivi", COLORS.purple)
        self.logged_card.set(format_hours(logged), "Studio effettivo", COLORS.accent)
        self.hint.configure(
            text=(
                "Le ore si aggiornano automaticamente quando avvii un Pomodoro da un materiale. "
                "Puoi anche registrarle manualmente."
            )
        )
        if not materials:
            empty = Card(self.materials_scroll)
            empty.grid(row=0, column=0, sticky="ew", pady=(4, 0))
            ctk.CTkLabel(
                empty,
                text="Nessun materiale ancora.",
                font=font(14, "bold"),
                text_color=COLORS.text_muted,
            ).pack(anchor="w", padx=22, pady=(24, 3))
            ctk.CTkLabel(
                empty,
                text="Aggiungi un PDF o un documento e collegalo a una materia o a un evento.",
                font=font(11),
                text_color=COLORS.text_faint,
            ).pack(anchor="w", padx=22, pady=(0, 24))
            return
        for index, material in enumerate(materials):
            self._material_card(material, index)

    def _material_card(self, material: dict[str, Any], row: int) -> None:
        card = ctk.CTkFrame(
            self.materials_scroll,
            fg_color=COLORS.surface,
            corner_radius=12,
            border_width=1,
            border_color=COLORS.border,
        )
        card.grid(row=row, column=0, sticky="ew", pady=4)
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 2))
        top.grid_columnconfigure(0, weight=1)
        title = str(material.get("title") or "Materiale")
        ctk.CTkLabel(
            top,
            text=title,
            font=font(15, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        subject = str(material.get("subject") or "Senza materia")
        ctk.CTkLabel(
            top,
            text=subject,
            font=font(9, "bold"),
            text_color=COLORS.accent,
            fg_color=COLORS.accent_dark,
            corner_radius=6,
            padx=8,
            height=22,
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))

        meta_parts = []
        if material.get("event_title"):
            event_text = str(material["event_title"])
            if material.get("event_start"):
                event_text = f"{event_text} · {format_event_datetime(str(material['event_start']))}"
            meta_parts.append(event_text)
        file_path = str(material.get("file_path") or "")
        path_exists = bool(file_path) and Path(file_path).exists()
        if file_path:
            meta_parts.append(Path(file_path).name + ("" if path_exists else " (non trovato)"))
        if meta_parts:
            ctk.CTkLabel(
                card,
                text="  ·  ".join(meta_parts),
                font=font(10),
                text_color=COLORS.red if file_path and not path_exists else COLORS.text_faint,
                anchor="w",
                wraplength=720,
                justify="left",
            ).grid(row=1, column=0, sticky="ew", padx=18)

        progress_frame = ctk.CTkFrame(card, fg_color="transparent")
        progress_frame.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 5))
        progress_frame.grid_columnconfigure(0, weight=1)
        target_hours = safe_float(material.get("target_hours", 0))
        logged_hours = safe_float(material.get("logged_hours", 0))
        ratio = min(1.0, logged_hours / target_hours) if target_hours > 0 else 0.0
        progress_text = (
            f"{logged_hours:.1f} / {target_hours:.1f} ore · {ratio * 100:.0f}%"
            if target_hours > 0
            else f"{logged_hours:.1f} ore registrate · nessun obiettivo"
        )
        ctk.CTkLabel(
            progress_frame,
            text=progress_text,
            font=font(10, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        bar = ctk.CTkProgressBar(
            progress_frame,
            height=8,
            corner_radius=4,
            fg_color=COLORS.surface_alt,
            progress_color=COLORS.accent if ratio < 1 else COLORS.green,
        )
        bar.grid(row=1, column=0, sticky="ew")
        bar.set(ratio)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=18, pady=(8, 14))
        ctk.CTkButton(
            actions,
            text="Apri file",
            height=32,
            width=92,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda path=file_path: self._open_file(path),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="Studia ora",
            height=32,
            width=104,
            font=font(11, "bold"),
            fg_color=COLORS.accent_dark,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.text,
            corner_radius=8,
            command=lambda value=dict(material): self.app.start_from_material(value),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="Aggiungi ore",
            height=32,
            width=100,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda material_id=int(material["id"]): self._add_hours(material_id),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="Modifica",
            height=32,
            width=82,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=8,
            command=lambda value=dict(material): self._open_dialog(value),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="Elimina",
            height=32,
            width=76,
            font=font(11),
            fg_color="transparent",
            hover_color=COLORS.red_dark,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.red,
            corner_radius=8,
            command=lambda value=dict(material): self._delete(value),
        ).pack(side="right")

    # ------------------------------------------------------------------ actions
    def _open_dialog(self, material: dict[str, Any] | None) -> None:
        MaterialDialog(self, material)

    def _open_file(self, file_path: str) -> None:
        if not file_path:
            messagebox.showwarning("File non impostato", "Nessun file associato a questo materiale.", parent=self.winfo_toplevel())
            return
        path = Path(file_path)
        if not path.exists():
            messagebox.showerror(
                "File non trovato",
                f"Il file non esiste piu' nel percorso indicato:\n{file_path}",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Impossibile aprire il file", str(exc), parent=self.winfo_toplevel())

    def _add_hours(self, material_id: int) -> None:
        value = simpledialog.askfloat(
            "Registra ore",
            "Ore da aggiungere (es. 0.5):",
            parent=self.winfo_toplevel(),
            initialvalue=0.5,
            minvalue=0.01,
            maxvalue=24.0,
        )
        if value is None:
            return
        self.db.add_material_hours(material_id, float(value))
        self.refresh()
        self.app.toast(f"Aggiunte {value:g} ore al materiale.")

    def _delete(self, material: dict[str, Any]) -> None:
        if not messagebox.askyesno(
            "Eliminare il materiale?",
            f"Eliminare '{material.get('title', 'Materiale')}'? Il file originale non verra' toccato.",
            parent=self.winfo_toplevel(),
        ):
            return
        self.db.delete_material(int(material["id"]))
        self.refresh()


class MaterialDialog(ctk.CTkToplevel):
    def __init__(self, view: MaterialsView, material: dict[str, Any] | None):
        super().__init__(view.winfo_toplevel())
        self.view = view
        self.app = view.app
        self.db = view.db
        self.material = material
        self._event_ids: list[int | None] = [None]
        self._event_labels: list[str] = ["Nessun evento"]
        self.title("Modifica materiale" if material else "Nuovo materiale")
        self.configure(fg_color=COLORS.bg)
        self.resizable(False, False)
        self.transient(view.winfo_toplevel())
        set_window_icon(self, self.app.paths["icon"])
        center_window(self, 660, 610)
        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.grab_set()
        self.after(100, self._populate_events)
        self.bind("<Escape>", lambda _event: self.destroy())

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="Modifica materiale" if self.material else "Nuovo materiale",
            font=font(22, "bold"),
            text_color=COLORS.text,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=28, pady=(24, 2))
        ctk.CTkLabel(
            self,
            text="Collega un PDF o documento a una materia e, se vuoi, a un evento.",
            font=font(11),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 18))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=28)
        form.grid_columnconfigure(0, weight=1)

        self.title_var = ctk.StringVar()
        self.subject_var = ctk.StringVar()
        self.target_var = ctk.StringVar(value="0")
        self.file_var = ctk.StringVar()
        self._field(form, 0, "Titolo", self.title_var, "es. Dispense Capitolo 3")
        self._field(form, 1, "Materia", self.subject_var, "es. Analisi Matematica")

        file_frame = ctk.CTkFrame(form, fg_color="transparent")
        file_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        file_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            file_frame,
            text="File PDF / documento",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ctk.CTkEntry(
            file_frame,
            textvariable=self.file_var,
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        ).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            file_frame,
            text="Sfoglia",
            width=92,
            height=38,
            font=font(11, "bold"),
            fg_color=COLORS.surface_alt,
            hover_color=COLORS.surface_hover,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text,
            corner_radius=9,
            command=self._browse,
        ).grid(row=1, column=1)

        row = ctk.CTkFrame(form, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        row.grid_columnconfigure((0, 1), weight=1)
        target_frame, _entry = self._labeled_entry(
            row, "Ore previste", self.target_var, "es. 8"
        )
        target_frame.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        event_frame = ctk.CTkFrame(row, fg_color="transparent")
        event_frame.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(
            event_frame,
            text="Evento collegato",
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        self.event_combo = ctk.CTkComboBox(
            event_frame,
            values=["Nessun evento"],
            height=38,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            button_color=COLORS.surface_hover,
            button_hover_color=COLORS.border,
            dropdown_fg_color=COLORS.surface,
            dropdown_hover_color=COLORS.surface_hover,
            text_color=COLORS.text,
            font=font(11),
            dropdown_font=font(11),
            corner_radius=9,
        )
        self.event_combo.pack(fill="x")
        self.event_combo.set("Nessun evento")

        info = Card(self)
        info.grid(row=3, column=0, sticky="ew", padx=28, pady=(4, 16))
        ctk.CTkLabel(
            info,
            text="Le ore studiate si aggiungono automaticamente quando completi un Pomodoro "
            "avviato da questo materiale. Il file originale non viene copiato ne' modificato.",
            font=font(10),
            text_color=COLORS.text_muted,
            wraplength=580,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=16, pady=12)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=28, pady=(0, 24))
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            buttons,
            text="Annulla",
            height=40,
            width=110,
            font=font(12),
            fg_color="transparent",
            hover_color=COLORS.surface_alt,
            border_width=1,
            border_color=COLORS.border,
            text_color=COLORS.text_muted,
            corner_radius=10,
            command=self.destroy,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            buttons,
            text="Salva materiale",
            height=40,
            width=170,
            font=font(12, "bold"),
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
            text_color=COLORS.bg,
            corner_radius=10,
            command=self._save,
        ).grid(row=0, column=1, sticky="e")

    def _field(self, parent, row: int, label: str, variable: ctk.StringVar, placeholder: str) -> None:
        frame, _entry = self._labeled_entry(parent, label, variable, placeholder)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 12))

    def _labeled_entry(
        self, parent, label: str, variable: ctk.StringVar, placeholder: str
    ) -> tuple[ctk.CTkFrame, ctk.CTkEntry]:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(
            frame,
            text=label,
            font=font(11, "bold"),
            text_color=COLORS.text_muted,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        entry = ctk.CTkEntry(
            frame,
            textvariable=variable,
            height=38,
            placeholder_text=placeholder,
            fg_color=COLORS.surface_alt,
            border_color=COLORS.border,
            text_color=COLORS.text,
            font=font(12),
            corner_radius=9,
        )
        entry.pack(fill="x")
        return frame, entry

    def _populate_events(self) -> None:
        if self.material:
            self.title_var.set(str(self.material.get("title") or ""))
            self.subject_var.set(str(self.material.get("subject") or ""))
            self.target_var.set(f"{safe_float(self.material.get('target_hours', 0)):g}")
            self.file_var.set(str(self.material.get("file_path") or ""))
        try:
            events = self.db.list_events_for_materials()
        except Exception:
            events = []
        self._event_ids = [None]
        self._event_labels = ["Nessun evento"]
        for event in events:
            label = f"{str(event['title'])[:60]} · {format_event_datetime(str(event['start']))}"
            if label in self._event_labels:
                label = f"{label} (#{event['id']})"
            self._event_labels.append(label)
            self._event_ids.append(int(event["id"]))
        linked_id: int | None = None
        if self.material and self.material.get("calendar_event_id"):
            linked_id = int(self.material["calendar_event_id"])
        if linked_id is not None and linked_id not in self._event_ids:
            # Un evento passato puo' non rientrare nell'elenco dei materiali:
            # va comunque mostrato per non perdere il collegamento in modifica.
            try:
                linked_event = self.db.get_event(linked_id)
            except Exception:
                linked_event = None
            if linked_event:
                label = (
                    f"{str(linked_event['title'])[:60]} · "
                    f"{format_event_datetime(str(linked_event['start']))}"
                )
                if label in self._event_labels:
                    label = f"{label} (#{linked_event['id']})"
                self._event_labels.append(label)
                self._event_ids.append(linked_id)
        self.event_combo.configure(values=self._event_labels)
        selected = "Nessun evento"
        if linked_id is not None:
            try:
                index = self._event_ids.index(linked_id)
                selected = self._event_labels[index]
            except ValueError:
                selected = "Nessun evento"
        self.event_combo.set(selected)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Seleziona materiale",
            filetypes=[
                ("Documenti", "*.pdf *.doc *.docx *.txt *.md *.ppt *.pptx *.xlsx"),
                ("PDF", "*.pdf"),
                ("Tutti i file", "*.*"),
            ],
        )
        if path:
            self.file_var.set(path)
            if not self.title_var.get().strip():
                self.title_var.set(Path(path).stem)

    def _save(self) -> None:
        title = self.title_var.get().strip()
        file_path = self.file_var.get().strip()
        subject = self.subject_var.get().strip()
        target = safe_float(self.target_var.get(), -1)
        if not title:
            messagebox.showerror("Titolo mancante", "Inserisci un titolo per il materiale.", parent=self)
            return
        if not file_path:
            messagebox.showerror("File mancante", "Seleziona un file PDF o documento.", parent=self)
            return
        if not Path(file_path).exists():
            if not messagebox.askyesno(
                "File non trovato",
                "Il file indicato non esiste. Salvarlo comunque?",
                parent=self,
            ):
                return
        if target < 0 or target > 10000:
            messagebox.showerror("Valore non valido", "Le ore previste devono essere tra 0 e 10000.", parent=self)
            return
        label = self.event_combo.get()
        try:
            event_id = self._event_ids[self._event_labels.index(label)]
        except (ValueError, IndexError):
            event_id = None
        try:
            if self.material:
                self.db.update_material(
                    int(self.material["id"]),
                    title,
                    file_path,
                    subject,
                    target,
                    event_id,
                )
            else:
                self.db.add_material(title, file_path, subject, target, event_id)
        except Exception as exc:
            self.app.logger.exception("Salvataggio materiale non riuscito.")
            messagebox.showerror("Salvataggio non riuscito", str(exc), parent=self)
            return
        self.grab_release()
        self.destroy()
        self.view.refresh()
        self.app.toast("Materiale salvato.")
