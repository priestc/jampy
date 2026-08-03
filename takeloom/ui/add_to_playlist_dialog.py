"""Add to Playlist dialog: a drop zone for local audio/video files and
YouTube URLs, used to add backing tracks to a project already selected on
the Record tab.

Shares its drag-and-drop/queue mechanics with NewProjectDialog, but skips
the project-creation step (the project already exists) and starts
processing each item the moment it's queued rather than waiting for a
single "Create" click, so tracks appear in the Setlist one at a time as
they finish instead of all at once at the end.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from tkinterdnd2 import DND_FILES, DND_TEXT

from ..backend import Backend, BackendError
from ..youtube import is_youtube_url
from .new_project_dialog import URLPromptDialog


class _AutocompleteEntry(ttk.Frame):
    """An Entry that debounces keystrokes, fetches suggestions on a
    background thread via `fetch(text) -> list[str]` (a Backend call —
    see docs/inspiration-server-autocomplete-api.md — so this works the
    same over Remote as it does locally), and shows them in a small popup
    below itself.

    Deliberately not a ttk.Combobox: posting a Combobox's built-in
    dropdown (via event_generate("<Down>") or the Tcl Post proc) moves
    keyboard focus into its internal listbox, so a stray letter keystroke
    typed right after suggestions appear lands in the listbox's own
    type-ahead handling instead of the entry — text stops updating
    mid-word. The popup here is a plain, separate Toplevel that never
    takes focus on its own, only when explicitly navigated into (Down
    arrow) or clicked."""

    _DEBOUNCE_MS = 200
    _MAX_VISIBLE = 8

    def __init__(self, master: tk.Misc, fetch: Callable[[str], list[str]], width: int = 30) -> None:
        super().__init__(master)
        self._fetch = fetch
        self._pending_after_id: str | None = None
        self._popup: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None
        self._matches: list[str] = []

        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Down>", self._on_down_from_entry)
        self.entry.bind("<Escape>", lambda _e: self._close_popup())
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Destroy>", self._on_destroy)

    def _on_key_release(self, event: object) -> None:
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):  # type: ignore[attr-defined]
            return
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
        self._pending_after_id = self.after(self._DEBOUNCE_MS, self._kick_off_fetch)

    def _kick_off_fetch(self) -> None:
        self._pending_after_id = None
        text = self.var.get().strip()
        if not text:
            self._close_popup()
            return

        def worker() -> None:
            matches = self._fetch(text)
            self.after(0, lambda: self._apply_results(text, matches))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_results(self, text: str, matches: list[str]) -> None:
        if not self.winfo_exists() or self.var.get().strip() != text:
            return  # stale response for text the user has since changed — discard it
        if matches:
            self._show_popup(matches)
        else:
            self._close_popup()

    # --- popup ---

    def _show_popup(self, matches: list[str]) -> None:
        self._matches = matches
        visible = min(len(matches), self._MAX_VISIBLE)
        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._popup.wm_attributes("-topmost", True)
            self._listbox = tk.Listbox(
                self._popup, background="#1a1a1a", foreground="white",
                selectbackground="#2a6db0", highlightthickness=1,
                highlightbackground="#444444", activestyle="none", exportselection=False,
            )
            self._listbox.pack(fill="both", expand=True)
            self._listbox.bind("<ButtonRelease-1>", self._on_listbox_commit)
            self._listbox.bind("<Return>", self._on_listbox_commit)
            self._listbox.bind("<Escape>", lambda _e: self._cancel_popup_navigation())

        self._listbox.configure(height=visible)
        self._listbox.delete(0, tk.END)
        for m in matches:
            self._listbox.insert(tk.END, m)

        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        width = self.entry.winfo_width()
        self._popup.update_idletasks()
        height = self._listbox.winfo_reqheight()
        self._popup.wm_geometry(f"{width}x{height}+{x}+{y}")
        self._popup.deiconify()

    def _on_down_from_entry(self, _event: object) -> str | None:
        if self._popup is None or self._listbox is None:
            return None
        self._listbox.focus_set()
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(0)
        self._listbox.activate(0)
        return "break"  # swallow the keystroke — don't let it also move the entry's cursor

    def _on_listbox_commit(self, _event: object) -> None:
        if self._listbox is None:
            return
        sel = self._listbox.curselection()
        if sel:
            self.var.set(self._matches[sel[0]])
        self._close_popup()
        self.entry.focus_set()
        self.entry.icursor(tk.END)

    def _cancel_popup_navigation(self) -> None:
        self._close_popup()
        self.entry.focus_set()

    def _on_focus_out(self, _event: object) -> None:
        # Moving focus into the popup's own listbox (via the Down-arrow
        # handler above) also fires the entry's FocusOut — give that a
        # moment to land before deciding the popup should actually close,
        # otherwise it vanishes the instant Down is pressed.
        self.after(150, self._close_popup_if_focus_elsewhere)

    def _close_popup_if_focus_elsewhere(self) -> None:
        if not self.winfo_exists():
            return
        focused = self.focus_get()
        if focused not in (self.entry, self._listbox):
            self._close_popup()

    def _close_popup(self) -> None:
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None
            self._listbox = None

    def _on_destroy(self, _event: object) -> None:
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
            self._pending_after_id = None
        self._close_popup()

    def get(self) -> str:
        return self.var.get()

    def focus_set(self) -> None:
        self.entry.focus_set()

    def bind_return(self, callback: Callable[[object], None]) -> None:
        self.entry.bind("<Return>", callback)


class InspirationSearchDialog(tk.Toplevel):
    """Prompt for an artist and/or title to add from the inspiration
    server. Both fields autocomplete live against the inspiration
    server's autocomplete endpoints via `backend` — the Title field's
    suggestions narrow to whatever's currently typed in Artist, if
    anything."""

    def __init__(self, master: tk.Misc, backend: Backend) -> None:
        super().__init__(master)
        self.title("Add from Inspiration")
        self.resizable(False, False)
        self.transient(master)
        self.result: tuple[str, str] | None = None

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Artist").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.artist_field = _AutocompleteEntry(frame, fetch=backend.search_inspiration_artists)
        self.artist_field.grid(row=0, column=1, sticky="ew")

        ttk.Label(frame, text="Title").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.title_field = _AutocompleteEntry(
            frame,
            fetch=lambda text: backend.search_inspiration_titles(text, artist=self.artist_field.get().strip()),
        )
        self.title_field.grid(row=1, column=1, sticky="ew")

        frame.columnconfigure(1, weight=1)
        self.artist_field.focus_set()
        self.artist_field.bind_return(lambda _e: self._submit())
        self.title_field.bind_return(lambda _e: self._submit())

        button_row = ttk.Frame(frame)
        button_row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Add", command=self._submit).pack(side="left")

    def _submit(self) -> None:
        artist = self.artist_field.get().strip()
        title = self.title_field.get().strip()
        if not artist and not title:
            return
        self.result = (artist, title)
        self.destroy()


class AddToPlaylistDialog(tk.Toplevel):
    """Popup for adding local files / YouTube URLs as backing tracks to an
    existing project. `on_track_added` fires after every successful add
    (not just once at the end) so the caller can refresh its track list
    live as items finish."""

    _PLACEHOLDER = "Drag audio files or YouTube URLs here, or use the buttons below"

    def __init__(
        self, master: tk.Misc, backend: Backend, project_name: str, on_track_added: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("Add to Playlist")
        self.resizable(False, False)
        self.transient(master)

        self._backend = backend
        self._project_name = project_name
        self._on_track_added = on_track_added
        self._items: list[dict] = []  # {"source": str, "kind": "file"|"youtube", "status": str}
        self._processing = False
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=f"Adding to: {project_name}", font=("TkDefaultFont", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        self.drop_zone = tk.Listbox(
            frame, height=8, width=56, background="#1a1a1a", foreground="white",
            selectbackground="#2a6db0", highlightthickness=1, highlightbackground="#444444",
        )
        self.drop_zone.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.drop_zone.drop_target_register(DND_FILES, DND_TEXT)
        self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)

        button_row = ttk.Frame(frame)
        button_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(button_row, text="Add Files...", command=self._on_browse_files).pack(side="left")
        ttk.Button(button_row, text="Add YouTube URL...", command=self._on_add_url).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="Add from Inspiration...", command=self._on_add_inspiration).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(button_row, text="Remove Selected", command=self._on_remove_selected).pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=400)
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#2a7d2a", wraplength=400).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        footer = ttk.Frame(frame)
        footer.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.close_button = ttk.Button(footer, text="Done", command=self.destroy)
        self.close_button.pack(side="left")

        self._refresh_list()

    # --- collecting sources ---

    def _add_item(self, source: str, kind: str, **extra: str) -> None:
        source = source.strip()
        if not source or any(i["source"] == source for i in self._items):
            return
        self._items.append({"source": source, "kind": kind, "status": "Pending", **extra})
        self._refresh_list()
        self._process_next()

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
            filetypes=[
                ("Audio/video files", "*.flac *.wav *.mp3 *.m4a *.aac *.ogg *.opus "
                                       "*.mp4 *.m4v *.mov *.mkv *.webm *.avi"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        for p in paths:
            self._add_item(p, "file")

    def _on_add_url(self) -> None:
        dialog = URLPromptDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if not is_youtube_url(dialog.result):
            messagebox.showerror("Not a YouTube URL", "Only YouTube URLs are supported here.", parent=self)
            return
        self._add_item(dialog.result, "youtube")

    def _on_add_inspiration(self) -> None:
        dialog = InspirationSearchDialog(self, self._backend)
        self.wait_window(dialog)
        if not dialog.result:
            return
        artist, title = dialog.result
        label = " - ".join(part for part in (artist, title) if part)
        self._add_item(label, "inspiration", artist=artist, title=title)

    def _on_remove_selected(self) -> None:
        # Only "Pending" (not yet started) items can be pulled back out —
        # one already downloading/copying can't be cancelled mid-flight,
        # and a finished one is either already on disk or worth keeping
        # visible as a record of the failure.
        sel = list(self.drop_zone.curselection())
        if not sel or not self._items:
            return
        for index in sorted(sel, reverse=True):
            if index < len(self._items) and self._items[index]["status"] == "Pending":
                del self._items[index]
        self._refresh_list()

    def _on_window_close(self) -> None:
        if self._processing:
            return  # a download in flight would just be orphaned, not stopped — see NewProjectDialog
        self.destroy()

    # --- sequential processing (one at a time, so the progress bar means something) ---

    def _process_next(self) -> None:
        if self._processing:
            return
        item = next((i for i in self._items if i["status"] == "Pending"), None)
        if item is None:
            return
        self._processing = True
        item["status"] = "Working..."
        if self.winfo_exists():
            self.close_button.state(["disabled"])
            self.progress["value"] = 0
            self._refresh_list()

        backend = self._backend
        project_name = self._project_name

        def worker() -> None:
            try:
                if item["kind"] == "youtube":
                    def on_progress(percent: float | None, message: str) -> None:
                        self.after(0, lambda: self._update_progress(percent, message))
                    backend.add_youtube_backing_track(project_name, item["source"], on_progress=on_progress)
                elif item["kind"] == "inspiration":
                    backend.add_inspiration_backing_track(project_name, item["artist"], item["title"])
                else:
                    backend.add_local_backing_track(project_name, item["source"])
                self.after(0, lambda: self._on_item_done(item, None))
            except BackendError as e:
                self.after(0, lambda err=str(e): self._on_item_done(item, err))

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, percent: float | None, message: str) -> None:
        if not self.winfo_exists():
            return
        if percent is not None:
            self.progress["value"] = percent
        self.status_var.set(message)

    def _on_item_done(self, item: dict, error: str | None) -> None:
        item["status"] = f"Failed: {error}" if error else "Done"
        self._processing = False
        if not error:
            self._on_track_added()
        if self.winfo_exists():
            self.progress["value"] = 0
            self.status_var.set("")
            self._refresh_list()
            self.close_button.state(["!disabled"])
        self._process_next()
