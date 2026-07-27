"""New Project dialog: a name entry plus a drop zone for local audio files
and YouTube URLs, used to seed a fresh project's backing tracks in one step.

All the actual work (creating the project directory, copying files, running
yt-dlp) happens through `app_state.backend`, so this dialog behaves the same
whether it's pointed at local hardware or a connected remote studio — except
that dropping local files isn't supported over Remote yet (see
`Backend.add_local_backing_track`'s docstring).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from tkinterdnd2 import DND_FILES, DND_TEXT

from ..backend import Backend, BackendError
from ..youtube import is_youtube_url


class NewProjectDialog(tk.Toplevel):
    """Popup for creating a project: name + drag/drop (or Browse/paste) backing tracks."""

    _PLACEHOLDER = "Drag audio files or YouTube URLs here, or use the buttons below"

    def __init__(self, master: tk.Misc, backend: Backend, on_created: Callable[[str], None]) -> None:
        super().__init__(master)
        self.title("New Project")
        self.resizable(False, False)
        self.transient(master)

        self._backend = backend
        self._on_created = on_created
        self._items: list[dict] = []  # {"source": str, "kind": "file"|"youtube", "status": str}
        self._working = False
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Project name").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(frame, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=0, column=1, sticky="w")
        self.name_entry.focus_set()

        ttk.Label(frame, text="Backing tracks").grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 2))

        self.drop_zone = tk.Listbox(
            frame, height=8, width=56, background="#1a1a1a", foreground="white",
            selectbackground="#2a6db0", highlightthickness=1, highlightbackground="#444444",
        )
        self.drop_zone.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.drop_zone.drop_target_register(DND_FILES, DND_TEXT)
        self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(button_row, text="Add Files...", command=self._on_browse_files).pack(side="left")
        ttk.Button(button_row, text="Add YouTube URL...", command=self._on_add_url).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="Remove Selected", command=self._on_remove_selected).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#2a7d2a", wraplength=400).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        footer = ttk.Frame(frame)
        footer.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.create_button = ttk.Button(footer, text="Create", command=self._on_create)
        self.create_button.pack(side="left")

        self._refresh_list()

    # --- collecting backing-track sources ---

    def _add_item(self, source: str, kind: str) -> None:
        source = source.strip()
        if not source or any(i["source"] == source for i in self._items):
            return
        self._items.append({"source": source, "kind": kind, "status": "Pending"})
        self._refresh_list()

    def _refresh_list(self) -> None:
        self.drop_zone.delete(0, tk.END)
        if not self._items:
            self.drop_zone.insert(0, self._PLACEHOLDER)
            self.drop_zone.itemconfig(0, foreground="#888888")
            return
        for item in self._items:
            self.drop_zone.insert(tk.END, f"{item['source']}  —  {item['status']}")

    def _on_drop(self, event: object) -> None:
        for raw in self._split_dnd_data(event.data):  # type: ignore[attr-defined]
            kind = "youtube" if is_youtube_url(raw) else "file"
            self._add_item(raw, kind)

    @staticmethod
    def _split_dnd_data(data: str) -> list[str]:
        """tkinterdnd2 wraps each dropped item in {...} when it contains
        whitespace, and separates items by whitespace otherwise."""
        items = []
        buf = ""
        depth = 0
        for ch in data:
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                continue
            if ch.isspace() and depth == 0:
                if buf:
                    items.append(buf)
                    buf = ""
                continue
            buf += ch
        if buf:
            items.append(buf)
        return items

    def _on_browse_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add Backing Tracks",
            filetypes=[("Audio files", "*.flac *.wav *.mp3 *.m4a *.aac *.ogg *.opus"), ("All files", "*.*")],
            parent=self,
        )
        for p in paths:
            self._add_item(p, "file")

    def _on_add_url(self) -> None:
        dialog = _URLPromptDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if not is_youtube_url(dialog.result):
            messagebox.showerror("Not a YouTube URL", "Only YouTube URLs are supported here.", parent=self)
            return
        self._add_item(dialog.result, "youtube")

    def _on_window_close(self) -> None:
        # A YouTube download can take a while with no progress feedback other
        # than the disabled buttons; closing the titlebar wouldn't actually
        # stop the background thread, just orphan its result, so block the
        # close instead of letting it look cancelled when it isn't.
        if self._working:
            return
        self.destroy()

    def _on_remove_selected(self) -> None:
        sel = list(self.drop_zone.curselection())
        if not sel or not self._items:
            return
        for index in sorted(sel, reverse=True):
            if index < len(self._items):
                del self._items[index]
        self._refresh_list()

    # --- create ---

    def _on_create(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Cannot create project", "Enter a project name.", parent=self)
            return

        self._working = True
        self.create_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.name_entry.state(["disabled"])
        self.status_var.set("Creating project... (YouTube downloads can take a while — please wait)")

        items = list(self._items)
        backend = self._backend

        def worker() -> None:
            try:
                project_name = backend.create_project(name)
            except BackendError as e:
                self.after(0, lambda err=str(e): self._on_create_failed(err))
                return

            for item in items:
                self.after(0, lambda i=item: self._set_item_status(i, "Working..."))
                try:
                    if item["kind"] == "youtube":
                        backend.add_youtube_backing_track(project_name, item["source"])
                    else:
                        backend.add_local_backing_track(project_name, item["source"])
                    self.after(0, lambda i=item: self._set_item_status(i, "Done"))
                except BackendError as e:
                    self.after(0, lambda i=item, err=str(e): self._set_item_status(i, f"Failed: {err}"))

            self.after(0, lambda: self._on_create_finished(project_name))

        threading.Thread(target=worker, daemon=True).start()

    def _set_item_status(self, item: dict, status: str) -> None:
        item["status"] = status
        if self.winfo_exists():
            self._refresh_list()

    def _on_create_failed(self, error: str) -> None:
        self._working = False
        if not self.winfo_exists():
            return
        messagebox.showerror("Cannot create project", error, parent=self)
        self.create_button.state(["!disabled"])
        self.cancel_button.state(["!disabled"])
        self.name_entry.state(["!disabled"])
        self.status_var.set("")

    def _on_create_finished(self, project_name: str) -> None:
        # Notify the Record tab regardless of whether this dialog window is
        # still around: a long yt-dlp download can look "frozen" (no progress
        # bar, everything disabled) for a while, and a user closing it via the
        # titlebar doesn't stop the background thread — the project and its
        # tracks are already written to disk by the time this fires, so the
        # Record tab still needs to pick them up even with no dialog left to
        # show a result in.
        self._working = False
        self._on_created(project_name)
        if not self.winfo_exists():
            return
        failed = [i for i in self._items if i["status"].startswith("Failed")]
        if not failed:
            self.destroy()
            return
        self.status_var.set(f"Project created — {len(failed)} track(s) failed to add.")
        self.cancel_button.configure(text="Close")
        self.cancel_button.state(["!disabled"])


class _URLPromptDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Add YouTube URL")
        self.resizable(False, False)
        self.transient(master)
        self.result: str | None = None

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="YouTube URL").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.url_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.url_var, width=44)
        entry.grid(row=0, column=1, sticky="w")
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._submit())

        button_row = ttk.Frame(frame)
        button_row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Add", command=self._submit).pack(side="left")

    def _submit(self) -> None:
        self.result = self.url_var.get().strip()
        self.destroy()
