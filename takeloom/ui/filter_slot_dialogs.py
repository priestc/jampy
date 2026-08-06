"""Dialogs for an existing inspiration filter slot's setlist context
menu: editing its criteria, and previewing every track it currently
matches on the inspiration server.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import Backend, BackendError
from ..utils import format_duration
from .filter_fields import FilterCriteriaFields


class EditFilterDialog(tk.Toplevel):
    """Edit an inspiration filter slot's criteria in place. `on_save`
    is called with the new filter_criteria dict once the user confirms —
    the caller (record.py) owns actually updating the TrackEntry and
    re-deriving its label, since this dialog only knows about criteria,
    not the setlist."""

    def __init__(
        self, master: tk.Misc, backend: Backend, filter_criteria: dict, on_save: Callable[[dict], None],
    ) -> None:
        super().__init__(master)
        self.title("Edit Filter")
        self.transient(master)
        self.resizable(False, False)
        self._on_save = on_save

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        self.fields = FilterCriteriaFields(frame, backend, dialog_parent=self, start_row=0)
        self.fields.set_criteria(filter_criteria)
        self.fields.bind_return(lambda _e: self._on_save_clicked())

        button_row = ttk.Frame(frame)
        button_row.grid(row=self.fields.next_row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(button_row, text="Save", command=self._on_save_clicked).pack(side="right")

        self.grab_set()
        self.fields.artist_field.focus_set()

    def _on_save_clicked(self) -> None:
        criteria = self.fields.get_criteria()
        if criteria is None:
            return
        self._on_save(criteria)
        self.destroy()


class ShowTracksDialog(tk.Toplevel):
    """Lists every inspiration-server track currently matching a filter
    slot's criteria — artist, title, genre, and length — so an operator
    can see what a filter slot might draw before it comes up in a
    session, rather than only finding out after the fact."""

    def __init__(self, master: tk.Misc, backend: Backend, label: str, filter_criteria: dict) -> None:
        super().__init__(master)
        self.title(f"Tracks matching “{label}”")
        self.geometry("640x420")
        self.transient(master)

        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(self, textvariable=self.status_var, foreground="#666666").pack(anchor="w", padx=12, pady=(10, 4))

        columns = ("artist", "title", "genre", "duration")
        self.tree = ttk.Treeview(self, columns=columns, show="headings")
        self.tree.heading("artist", text="Artist")
        self.tree.heading("title", text="Title")
        self.tree.heading("genre", text="Genre")
        self.tree.heading("duration", text="Length")
        self.tree.column("artist", width=180)
        self.tree.column("title", width=220)
        self.tree.column("genre", width=110)
        self.tree.column("duration", width=70, anchor="e")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
        scrollbar.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))

        self._load(backend, filter_criteria)

    def _load(self, backend: Backend, filter_criteria: dict) -> None:
        def worker() -> None:
            try:
                tracks, error = backend.search_inspiration_by_filter(filter_criteria), None
            except BackendError as e:
                tracks, error = None, str(e)
            self.after(0, lambda: self._apply(tracks, error))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, tracks: list[dict] | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            self.status_var.set(f"Error: {error}")
            return
        tracks = tracks or []
        tracks.sort(key=lambda t: ((t.get("artist") or "").lower(), (t.get("title") or "").lower()))
        for t in tracks:
            duration = t.get("duration")
            self.tree.insert("", "end", values=(
                t.get("artist", "Unknown"), t.get("title", "Unknown"), t.get("genre", ""),
                format_duration(duration) if duration else "",
            ))
        count = len(tracks)
        self.status_var.set(f"{count} matching track{'s' if count != 1 else ''}")
