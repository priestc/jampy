"""Record page: pick an instrument + project, choose a track from the
project's playlist or from your inspiration library, preview the camera,
and record a take.

Everything here goes through `app_state.backend` — in local mode that's a
`LocalBackend` talking directly to this machine's hardware; in remote mode
it's a `RemoteBackend` talking over the network to another takeloom instance.
This frame itself never touches `sounddevice`/`cv2`/`ffmpeg` directly: all
device/take/camera-preview work happens inside the backend, and this frame
just reflects whatever state it reports back (via `recording_status`/
`preview_paused`/`preview_resumed` events, and streamed preview frames).
"""

from __future__ import annotations

import io
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..audio.filters import COMPRESSOR_PRESETS
from ..backend import BackendError, StartRecordingRequest
from ..config import StudioConfig
from ..project import Setlist, TrackEntry
from ..recording_driver import RecordingDeckDriver
from ..utils import format_duration
from .app_state import AppState
from .level_meter import LevelMeter
from .new_project_dialog import NewProjectDialog
from .sound_check_dialog import SoundCheckDialog


class RecordFrame(ttk.Frame):
    """Instrument/project/track picker, camera preview, and recording."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._project_names: list[str] = []
        self._project_name: str | None = None
        self._setlist: Setlist | None = None

        self._selected_track: TrackEntry | None = None
        self._selected_track_index: int | None = None
        self._selected_inspiration_info: dict | None = None
        self._selected_track_source: str | None = None  # "playlist" | "inspiration"
        self._inspiration_tracks: list[dict] = []

        self._phase = "idle"  # "idle" | "waiting" (loaded, not yet unpaused) | "recording"
        self._sound_check_phase = "idle"  # "idle" | "recording"
        self._preview_sub = None
        self._preview_imgtk = None
        self._preview_width = 0  # current width available for the preview label, tracked via <Configure>

        self._current_backend = None
        self._streamdeck_driver = RecordingDeckDriver(
            self.app_state.backend,
            resolve_start_request=self._build_selected_request,
            on_sound_check_result=self._on_streamdeck_sound_check_result,
            log=self._log_streamdeck,
        )
        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._attach_backend()

        threading.Thread(target=self._connect_streamdeck, daemon=True).start()

        self.after(50, self._poll_levels)

    # --- StreamDeck (shared RecordingDeckDriver — see recording_driver.py) ---

    def _connect_streamdeck(self) -> None:
        if not self._streamdeck_driver.connect(key_callback=self._on_streamdeck_key):
            if self._streamdeck_driver.streamdeck.last_error:
                print(
                    f"StreamDeck: found a device but could not connect — "
                    f"{self._streamdeck_driver.streamdeck.last_error}"
                )

    def _on_streamdeck_key(self, key: str) -> None:
        # Marshal onto the Tk thread before the driver runs — resolve_start_
        # request() reads Tk StringVars and on_sound_check_result() may open
        # a Toplevel, neither of which is safe off the Tk thread.
        self.after(0, lambda: self._streamdeck_driver.handle_key(key))

    def _on_streamdeck_sound_check_result(self, path: Path, has_video: bool) -> None:
        self.after(0, lambda: SoundCheckDialog(self, path, has_video) if self.winfo_exists() else None)

    def _log_streamdeck(self, message: str) -> None:
        def apply() -> None:
            if self.winfo_exists() and hasattr(self, "status_var"):
                self.status_var.set(message)
        self.after(0, apply)

    # --- async backend calls (thread hop + marshal back onto the Tk thread) ---

    def _run_backend(self, fn, on_done=None) -> None:
        """Run `fn()` on a worker thread and, once it finishes, call
        `on_done(result, error)` back on the Tk thread (`error` is a message
        string if `fn` raised BackendError, else None and `result` holds
        whatever `fn` returned). Pass no `on_done` for fire-and-forget calls."""
        def worker() -> None:
            try:
                result, error = fn(), None
            except BackendError as e:
                result, error = None, str(e)
            if on_done is not None:
                self.after(0, lambda: on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    # --- backend attach / (re)load ---

    def _attach_backend(self) -> None:
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
        self._stop_preview()
        self._current_backend = self.app_state.backend
        self._current_backend.on_event(self._on_backend_event)
        self._streamdeck_driver.rebind_backend(self._current_backend)
        self._clear_selection()
        self._load()

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._current_backend:
            self._attach_backend()

    def _load(self) -> None:
        backend = self.app_state.backend

        def on_done(result: tuple | None, error: str | None) -> None:
            config, projects = result if result is not None else (None, [])
            self._on_loaded(config, projects, error)

        self._run_backend(lambda: (backend.get_config(), backend.list_projects()), on_done)

    def _on_loaded(self, config: StudioConfig | None, projects: list[str], error: str | None) -> None:
        if not self.winfo_exists():
            return  # app closed (or frame torn down) before this load finished
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._project_names = projects

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="new", padx=(0, 10))
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.columnconfigure(0, weight=1, uniform="record_halves")
        self.columnconfigure(1, weight=1, uniform="record_halves")
        self.rowconfigure(0, weight=1)

        self._build_left(left)
        self._build_right(right)

        self._start_preview()
        self._on_project_change()

    # --- left column: instrument/project/preview/controls ---

    def _build_left(self, left: ttk.Frame) -> None:
        row = 0
        ttk.Label(left, text="Record", font=("TkDefaultFont", 14, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        row += 1

        instrument_names = [inst.name for inst in self.config_obj.instruments]
        default_instrument = self.config_obj.last_selected_instrument
        if default_instrument not in instrument_names:
            default_instrument = instrument_names[0] if instrument_names else ""
        ttk.Label(left, text="Instrument").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.instrument_var = tk.StringVar(value=default_instrument)
        self.instrument_combo = ttk.Combobox(
            left, textvariable=self.instrument_var, values=instrument_names, state="readonly", width=28,
        )
        self.instrument_combo.grid(row=row, column=1, sticky="w")
        self.instrument_combo.bind("<<ComboboxSelected>>", self._on_instrument_change)
        row += 1

        default_project = self.config_obj.last_selected_project
        if default_project not in self._project_names:
            default_project = self._project_names[0] if self._project_names else ""
        ttk.Label(left, text="Project").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        project_row = ttk.Frame(left)
        project_row.grid(row=row, column=1, sticky="w")
        self.project_var = tk.StringVar(value=default_project)
        self.project_combo = ttk.Combobox(
            project_row, textvariable=self.project_var, values=self._project_names, state="readonly", width=22,
        )
        self.project_combo.pack(side="left")
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_change)
        ttk.Button(project_row, text="+ New Project", command=self._on_new_project).pack(side="left", padx=(6, 0))
        row += 1

        if not instrument_names:
            ttk.Label(
                left, text="No instruments configured. Set them up on the Instruments tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
        if not self._project_names:
            ttk.Label(
                left, text=f"No projects found in {self.config_obj.projects_dir}.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        left.columnconfigure(1, weight=1)
        self.refresh_devices_button = ttk.Button(
            left, text="↻ Refresh Devices", command=self._on_refresh_devices,
        )
        self.refresh_devices_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        self.preview_label = tk.Label(left, background="#1a1a1a", foreground="white", text="No camera preview")
        self.preview_label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.preview_label.bind("<Configure>", self._on_preview_label_resize)
        row += 1

        ttk.Label(left, text="Instrument", foreground="#666666").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.instrument_meter = LevelMeter(left)
        self.instrument_meter.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row += 1

        ttk.Label(left, text="Backing Track", foreground="#666666").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.backing_meter = LevelMeter(left)
        self.backing_meter.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        row += 1

        row = self._build_audio_filters(left, row)

        self.selection_var = tk.StringVar(value="No track selected")
        ttk.Label(left, textvariable=self.selection_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.status_var, foreground="#2a7d2a", wraplength=360).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row += 1

        self.record_button = ttk.Button(left, text="Start Recording", command=self._on_toggle_recording)
        self.record_button.grid(row=row, column=0, columnspan=2, pady=(8, 0))
        if not instrument_names or not self._project_names:
            self.record_button.state(["disabled"])
        row += 1

        self.sound_check_button = ttk.Button(
            left, text="Sound Check", command=self._on_toggle_sound_check,
        )
        self.sound_check_button.grid(row=row, column=0, columnspan=2, pady=(4, 0))
        if self.app_state.backend.is_remote():
            self.sound_check_button.state(["disabled"])
            self.sound_check_button.grid_remove()
        elif not instrument_names or not self._project_names:
            self.sound_check_button.state(["disabled"])

    # --- audio filters (compressor now, more later) ---

    def _build_audio_filters(self, left: ttk.Frame, row: int) -> int:
        # Attack/release aren't exposed here (yet) — carried through unchanged
        # from whatever's in studio_config.json so a hand-edited value isn't
        # clobbered by this section's Threshold/Ratio/Makeup controls.
        self._compressor_attack_ms = self.config_obj.compressor_attack_ms
        self._compressor_release_ms = self.config_obj.compressor_release_ms

        ttk.Label(left, text="Audio Filters", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1

        self.compressor_var = tk.BooleanVar(value=self.config_obj.compressor_enabled)
        ttk.Checkbutton(
            left, text="Compressor", variable=self.compressor_var, command=self._on_compressor_change,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        preset_row = ttk.Frame(left)
        preset_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=(16, 0), pady=(2, 0))
        row += 1
        ttk.Label(preset_row, text="Preset").pack(side="left")
        self.compressor_preset_var = tk.StringVar(value="")
        self.compressor_preset_combo = ttk.Combobox(
            preset_row, textvariable=self.compressor_preset_var,
            values=list(COMPRESSOR_PRESETS.keys()), state="readonly", width=18,
        )
        self.compressor_preset_combo.pack(side="left", padx=(4, 0))
        self.compressor_preset_combo.bind("<<ComboboxSelected>>", self._on_compressor_preset_selected)

        params_row = ttk.Frame(left)
        params_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=(16, 0), pady=(2, 8))
        row += 1

        self.compressor_threshold_var = tk.DoubleVar(value=self.config_obj.compressor_threshold_db)
        self.compressor_ratio_var = tk.DoubleVar(value=self.config_obj.compressor_ratio)
        self.compressor_makeup_var = tk.DoubleVar(value=self.config_obj.compressor_makeup_db)

        ttk.Label(params_row, text="Threshold (dB)").grid(row=0, column=0, sticky="w")
        self.compressor_threshold_spin = ttk.Spinbox(
            params_row, from_=-60, to=0, increment=1, width=6,
            textvariable=self.compressor_threshold_var, command=self._on_compressor_change,
        )
        self.compressor_threshold_spin.grid(row=0, column=1, sticky="w", padx=(4, 12))
        self.compressor_threshold_spin.bind("<Return>", lambda _e: self._on_compressor_change())
        self.compressor_threshold_spin.bind("<FocusOut>", lambda _e: self._on_compressor_change())

        ttk.Label(params_row, text="Ratio").grid(row=0, column=2, sticky="w")
        self.compressor_ratio_spin = ttk.Spinbox(
            params_row, from_=1, to=20, increment=0.5, width=6,
            textvariable=self.compressor_ratio_var, command=self._on_compressor_change,
        )
        self.compressor_ratio_spin.grid(row=0, column=3, sticky="w", padx=(4, 0))
        self.compressor_ratio_spin.bind("<Return>", lambda _e: self._on_compressor_change())
        self.compressor_ratio_spin.bind("<FocusOut>", lambda _e: self._on_compressor_change())

        ttk.Label(params_row, text="Makeup Gain (dB)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.compressor_makeup_spin = ttk.Spinbox(
            params_row, from_=0, to=24, increment=0.5, width=6,
            textvariable=self.compressor_makeup_var, command=self._on_compressor_change,
        )
        self.compressor_makeup_spin.grid(row=1, column=1, sticky="w", padx=(4, 12), pady=(4, 0))
        self.compressor_makeup_spin.bind("<Return>", lambda _e: self._on_compressor_change())
        self.compressor_makeup_spin.bind("<FocusOut>", lambda _e: self._on_compressor_change())

        self._set_compressor_controls_enabled(self.config_obj.compressor_enabled)
        return row

    def _set_compressor_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.compressor_threshold_spin.configure(state=state)
        self.compressor_ratio_spin.configure(state=state)
        self.compressor_makeup_spin.configure(state=state)

    def _on_compressor_preset_selected(self, _event: object = None) -> None:
        preset = COMPRESSOR_PRESETS.get(self.compressor_preset_var.get())
        if preset is None:
            return
        # Picking a preset both fills in the numbers and turns the compressor
        # on — selecting one only to have it silently stay disabled would be
        # a confusing dead click.
        self.compressor_var.set(preset.enabled)
        self.compressor_threshold_var.set(preset.threshold_db)
        self.compressor_ratio_var.set(preset.ratio)
        self.compressor_makeup_var.set(preset.makeup_gain_db)
        self._compressor_attack_ms = preset.attack_ms
        self._compressor_release_ms = preset.release_ms
        self._on_compressor_change()

    def _on_compressor_change(self) -> None:
        enabled = self.compressor_var.get()
        self._set_compressor_controls_enabled(enabled)
        try:
            threshold_db = self.compressor_threshold_var.get()
            ratio = self.compressor_ratio_var.get()
            makeup_gain_db = self.compressor_makeup_var.get()
        except tk.TclError:
            return  # a spinbox is mid-edit with invalid/empty text; ignore until it settles
        settings = {
            "enabled": enabled,
            "threshold_db": threshold_db,
            "ratio": ratio,
            "attack_ms": self._compressor_attack_ms,
            "release_ms": self._compressor_release_ms,
            "makeup_gain_db": makeup_gain_db,
        }
        backend = self.app_state.backend
        self._run_backend(lambda: backend.set_compressor_settings(settings))

    # --- right column: Setlist / Inspiration tabs ---

    def _build_right(self, right: ttk.Frame) -> None:
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(right)
        notebook.grid(row=0, column=0, sticky="nsew")

        setlist_tab = ttk.Frame(notebook)
        inspiration_tab = ttk.Frame(notebook)
        notebook.add(setlist_tab, text="Setlist")
        notebook.add(inspiration_tab, text="Inspiration")

        setlist_tab.columnconfigure(0, weight=1)
        setlist_tab.rowconfigure(0, weight=1)
        playlist_wrap = ttk.Frame(setlist_tab)
        playlist_wrap.grid(row=0, column=0, sticky="nsew", pady=(8, 0))
        playlist_scroll = ttk.Scrollbar(playlist_wrap, orient="vertical")
        self.playlist_listbox = tk.Listbox(
            playlist_wrap, height=10, exportselection=False,
            yscrollcommand=playlist_scroll.set,
        )
        playlist_scroll.configure(command=self.playlist_listbox.yview)
        self.playlist_listbox.pack(side="left", fill="both", expand=True)
        playlist_scroll.pack(side="right", fill="y")
        self.playlist_listbox.bind("<<ListboxSelect>>", self._on_playlist_select)

        inspiration_tab.columnconfigure(0, weight=1)
        inspiration_tab.rowconfigure(2, weight=1)
        inspiration_header = ttk.Frame(inspiration_tab)
        inspiration_header.grid(row=0, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(inspiration_header, text="Inspiration Tracks", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(inspiration_header, text="↻ Refresh", command=self._refresh_inspiration).pack(side="right")

        self.inspiration_status_var = tk.StringVar(value="")
        ttk.Label(inspiration_tab, textvariable=self.inspiration_status_var, foreground="#666666").grid(
            row=1, column=0, sticky="w", pady=(2, 4)
        )

        inspiration_wrap = ttk.Frame(inspiration_tab)
        inspiration_wrap.grid(row=2, column=0, sticky="nsew")
        inspiration_scroll = ttk.Scrollbar(inspiration_wrap, orient="vertical")
        self.inspiration_listbox = tk.Listbox(
            inspiration_wrap, height=10, exportselection=False,
            yscrollcommand=inspiration_scroll.set,
        )
        inspiration_scroll.configure(command=self.inspiration_listbox.yview)
        self.inspiration_listbox.pack(side="left", fill="both", expand=True)
        inspiration_scroll.pack(side="right", fill="y")
        self.inspiration_listbox.bind("<<ListboxSelect>>", self._on_inspiration_select)

    def _track_display(self, track: TrackEntry, inst_name: str) -> str:
        dur = format_duration(track.duration_seconds)
        take = track.get_take_for_instrument(inst_name)
        if take is None:
            mark = ""
        elif take.has_video:
            mark = " ✓"
        else:
            mark = " ✓ (audio only)"
        return f"{track.name}  ({dur}){mark}"

    def _inspiration_display(self, t: dict) -> str:
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        year = t.get("year", "")
        dur = format_duration(t.get("duration") or 0)
        year_str = f" ({year})" if year else ""
        return f"{artist} - {title}{year_str}  {dur}"

    def _refresh_playlist(self) -> None:
        self.playlist_listbox.delete(0, tk.END)
        if not self._setlist:
            return
        inst_name = self.instrument_var.get()
        for track in self._setlist.tracks:
            self.playlist_listbox.insert(tk.END, self._track_display(track, inst_name))

    def _refresh_inspiration(self) -> None:
        self.inspiration_listbox.delete(0, tk.END)
        self._inspiration_tracks = []
        if not self._project_name:
            return
        self.inspiration_status_var.set("Loading inspiration tracks...")
        backend = self.app_state.backend
        project_name = self._project_name

        self._run_backend(
            lambda: backend.query_inspiration_tracks(project_name),
            lambda tracks, error: self._on_inspiration_loaded(tracks or [], error),
        )

    def _on_inspiration_loaded(self, tracks: list[dict], error: str | None) -> None:
        self._inspiration_tracks = tracks
        self.inspiration_listbox.delete(0, tk.END)
        if error:
            self.inspiration_status_var.set(error)
            return
        self.inspiration_status_var.set(f"{len(tracks)} tracks")
        for t in tracks:
            self.inspiration_listbox.insert(tk.END, self._inspiration_display(t))

    def _clear_selection(self) -> None:
        self._selected_track = None
        self._selected_track_index = None
        self._selected_inspiration_info = None
        self._selected_track_source = None
        if hasattr(self, "selection_var"):
            self.selection_var.set("No track selected")
        self._update_start_button_state()

    def _on_project_change(self, _event: object = None) -> None:
        self._clear_selection()
        if hasattr(self, "playlist_listbox"):
            self.playlist_listbox.delete(0, tk.END)
        self._setlist = None
        project_name = self.project_var.get() if hasattr(self, "project_var") else ""
        self._project_name = project_name or None
        if _event is not None:
            self._persist_last_selection()
        if not self._project_name:
            self._refresh_inspiration()
            return

        backend = self.app_state.backend
        project_name = self._project_name
        self._run_backend(lambda: backend.get_setlist(project_name), self._on_setlist_loaded)

    def _on_new_project(self) -> None:
        NewProjectDialog(self, self.app_state.backend, self._on_project_created)

    def _on_project_created(self, project_name: str) -> None:
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.list_projects(),
            lambda projects, error: self._apply_new_project_list(
                projects if error is None else self._project_names, project_name
            ),
        )

    def _apply_new_project_list(self, projects: list[str], project_name: str) -> None:
        if not self.winfo_exists():
            return
        self._project_names = projects
        self.project_combo.configure(values=projects)
        self.project_var.set(project_name)
        self._on_project_change()
        self._persist_last_selection()

    def _on_setlist_loaded(self, data: dict | None, error: str | None) -> None:
        if error or data is None:
            self.selection_var.set(f"Could not load project: {error}")
            return
        self._setlist = Setlist.from_dict(data)
        self._refresh_playlist()
        self._refresh_inspiration()

    def _refresh_playlist_from_server(self) -> None:
        """Re-fetch just the current project's setlist (e.g. after a take
        finishes) without disturbing the current project/instrument selection
        or re-querying inspiration tracks."""
        if not self._project_name:
            return
        backend = self.app_state.backend
        project_name = self._project_name
        self._run_backend(
            lambda: backend.get_setlist(project_name),
            lambda data, error: None if error else self._apply_refreshed_setlist(data),
        )

    def _apply_refreshed_setlist(self, data: dict) -> None:
        self._setlist = Setlist.from_dict(data)
        self._refresh_playlist()

    def _on_instrument_change(self, _event: object = None) -> None:
        self._refresh_playlist()
        self._update_start_button_state()
        if _event is not None:
            self._persist_last_selection()

    def _persist_last_selection(self) -> None:
        """Remembered so the Record tab reopens on the same pair next launch."""
        if self.config_obj is None:
            return
        self.config_obj.last_selected_instrument = self.instrument_var.get()
        self.config_obj.last_selected_project = self.project_var.get()
        backend = self.app_state.backend
        config = self.config_obj
        self._run_backend(lambda: backend.save_config(config))

    def _on_playlist_select(self, _event: object = None) -> None:
        sel = self.playlist_listbox.curselection()
        if not sel or not self._setlist:
            return
        self._selected_track = self._setlist.tracks[sel[0]]
        self._selected_track_index = sel[0]
        self._selected_inspiration_info = None
        self._selected_track_source = "playlist"
        self.inspiration_listbox.selection_clear(0, tk.END)
        self.selection_var.set(f"Selected: {self._selected_track.name} (playlist)")
        self._update_start_button_state()

    def _on_inspiration_select(self, _event: object = None) -> None:
        sel = self.inspiration_listbox.curselection()
        if not sel:
            return
        info = self._inspiration_tracks[sel[0]]
        self._selected_track = None
        self._selected_track_index = None
        self._selected_inspiration_info = info
        self._selected_track_source = "inspiration"
        self.playlist_listbox.selection_clear(0, tk.END)
        artist = info.get("artist", "Unknown")
        title = info.get("title", "Unknown")
        self.selection_var.set(f"Selected: {artist} - {title} (inspiration)")
        self._update_start_button_state()

    def _update_start_button_state(self) -> None:
        if not hasattr(self, "record_button"):
            return
        ready = (
            bool(self.instrument_var.get())
            and self._project_name is not None
            and self._selected_track_source is not None
        )
        if self._phase != "idle":
            self.record_button.state(["!disabled"])
        else:
            # Also blocked while a sound check has the audio/camera hardware —
            # the two are mutually exclusive at the backend level.
            self.record_button.state(["!disabled"] if (ready and self._sound_check_phase == "idle") else ["disabled"])

        if not hasattr(self, "sound_check_button"):
            return
        if self._sound_check_phase != "idle":
            self.sound_check_button.state(["!disabled"])
        else:
            self.sound_check_button.state(["!disabled"] if (ready and self._phase == "idle") else ["disabled"])

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.instrument_combo.configure(state=state)
        self.project_combo.configure(state=state)
        list_state = "normal" if enabled else "disabled"
        self.playlist_listbox.configure(state=list_state)
        self.inspiration_listbox.configure(state=list_state)
        self.refresh_devices_button.state(["!disabled"] if enabled else ["disabled"])

    # --- refresh devices (camera/audio plugged in after the UI was launched) ---

    def _on_refresh_devices(self) -> None:
        if self._phase != "idle":
            return  # camera's held exclusively by the active recording; nothing to refresh into
        self.refresh_devices_button.state(["disabled"])
        self.status_var.set("Refreshing devices...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.refresh_devices(), lambda _result, error: self._on_refresh_devices_result(error)
        )

    def _on_refresh_devices_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.refresh_devices_button.state(["!disabled"])
        if error:
            self.status_var.set(f"Refresh failed: {error}")
            return
        self.status_var.set("Devices refreshed.")
        # The backend's camera capture thread has been restarted by
        # refresh_devices(); tear down and reopen this frame's own
        # subscription so a preview that never came up (no camera at launch)
        # gets a fresh "Waiting for camera preview..." rather than staying on
        # "No camera preview" from before the config even had one attached.
        self._stop_preview()
        self._start_preview()

    # --- camera preview (frames streamed from the backend, local or remote) ---

    def _start_preview(self) -> None:
        if self._preview_sub is not None:
            return
        if self.config_obj is not None and not self.config_obj.camera_device:
            self.preview_label.configure(text="No camera configured", image="")
            return
        self.preview_label.configure(text="Waiting for camera preview...", image="")
        self._preview_sub = self.app_state.backend.open_camera_preview(self._on_preview_frame)

    def _on_preview_label_resize(self, event: object) -> None:
        self._preview_width = event.width  # type: ignore[attr-defined]

    def _on_preview_frame(self, jpeg: bytes) -> None:
        self.after(0, lambda: self._render_preview_frame(jpeg))

    def _render_preview_frame(self, jpeg: bytes) -> None:
        if self._preview_sub is None:
            return  # unsubscribed since this frame was queued onto the Tk thread
        from PIL import Image, ImageTk
        try:
            image = Image.open(io.BytesIO(jpeg))
        except Exception:
            return
        # Camera frames come in at a fixed, deliberately small capture size (see
        # backend.py) to keep local/remote streaming cheap. Upscale to fill the
        # width Tk has actually given the label, preserving aspect ratio.
        target_width = self._preview_width
        if target_width and target_width != image.width:
            new_height = max(1, round(image.height * target_width / image.width))
            image = image.resize((target_width, new_height), Image.LANCZOS)
        self._preview_imgtk = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self._preview_imgtk, text="")

    def _stop_preview(self) -> None:
        if self._preview_sub is not None:
            self._preview_sub.close()
            self._preview_sub = None

    # --- VU meters (polled directly — get_levels() is cheap: a local attribute
    # read for LocalBackend, and a no-op returning silence for RemoteBackend,
    # so there's no need to hop to a worker thread like the other backend calls) ---

    def _poll_levels(self) -> None:
        if not self.winfo_exists():
            return
        if hasattr(self, "instrument_meter"):
            try:
                instrument_level, backing_level = self.app_state.backend.get_levels()
            except BackendError:
                instrument_level, backing_level = 0.0, 0.0
            self.instrument_meter.set_level(instrument_level)
            self.backing_meter.set_level(backing_level)
        self.after(50, self._poll_levels)

    def _on_destroy(self, _event: object) -> None:
        self._stop_preview()
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
        self.app_state.remove_listener(self._on_app_state_changed)
        self._streamdeck_driver.disconnect()

    # --- recording ---

    def _on_toggle_recording(self) -> None:
        if self._phase == "idle":
            self._start_recording()
        elif self._phase == "waiting":
            self._unpause_recording()
        elif self._phase == "recording":
            self._stop_recording()

    def _build_selected_request(self) -> StartRecordingRequest | None:
        """Build a StartRecordingRequest from the current instrument/project/
        track selection — shared by _start_recording and _start_sound_check,
        which both act on whatever's currently picked in the Setlist/
        Inspiration list. Shows an error dialog and returns None if
        incomplete."""
        instrument_name = self.instrument_var.get()
        if not instrument_name or not self._project_name:
            messagebox.showerror("Cannot start", "Select an instrument and a project first.")
            return None

        if self._selected_track_source == "playlist" and self._selected_track_index is not None:
            return StartRecordingRequest(
                project_name=self._project_name, instrument_name=instrument_name,
                track_source="playlist", track_index=self._selected_track_index,
            )
        elif self._selected_track_source == "inspiration" and self._selected_inspiration_info is not None:
            return StartRecordingRequest(
                project_name=self._project_name, instrument_name=instrument_name,
                track_source="inspiration", inspiration_info=self._selected_inspiration_info,
            )
        messagebox.showerror("Cannot start", "Select a track from the Playlist or Inspiration list first.")
        return None

    def _start_recording(self) -> None:
        req = self._build_selected_request()
        if req is None:
            return

        self.record_button.state(["disabled"])
        self._set_controls_enabled(False)
        self.status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(lambda: backend.start_recording(req), lambda _result, error: self._on_start_result(error))

    def _on_start_result(self, error: str | None) -> None:
        # On success, the "recording_status" event the backend emits before
        # start_recording() returns already updated button/state via
        # _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot start", error)
            self._set_controls_enabled(True)
            self.status_var.set("")
            self._update_start_button_state()

    def _unpause_recording(self) -> None:
        self.record_button.state(["disabled"])
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.unpause_recording(), lambda _result, error: self._on_unpause_result(error)
        )

    def _on_unpause_result(self, error: str | None) -> None:
        # On success, the "recording_status" event already updated the button
        # via _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot unpause", error)
            self.record_button.state(["!disabled"])

    def _stop_recording(self) -> None:
        self.record_button.state(["disabled"])
        backend = self.app_state.backend
        self._run_backend(lambda: backend.stop_recording(), lambda _result, error: self._on_stop_result(error))

    def _on_stop_result(self, error: str | None) -> None:
        self.record_button.state(["!disabled"])
        if error:
            messagebox.showerror("Stop failed", error)

    # --- sound check ---

    def _on_toggle_sound_check(self) -> None:
        if self._sound_check_phase == "idle":
            self._start_sound_check()
        else:
            self._stop_sound_check()

    def _start_sound_check(self) -> None:
        req = self._build_selected_request()
        if req is None:
            return

        self.sound_check_button.state(["disabled"])
        self._set_controls_enabled(False)
        self.status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.start_sound_check(req), lambda _result, error: self._on_sound_check_start_result(error)
        )

    def _on_sound_check_start_result(self, error: str | None) -> None:
        # On success, the "sound_check_status" event the backend emits before
        # start_sound_check() returns already updated button/state via
        # _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot start sound check", error)
            self._set_controls_enabled(True)
            self.status_var.set("")
            self._update_start_button_state()

    def _stop_sound_check(self) -> None:
        self.sound_check_button.state(["disabled"])
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.stop_sound_check(), lambda _result, error: self._on_sound_check_stop_result(error)
        )

    def _on_sound_check_stop_result(self, error: str | None) -> None:
        self.sound_check_button.state(["!disabled"])
        if error:
            messagebox.showerror("Stop failed", error)

    # --- backend events (recording_status / preview_paused / preview_resumed) ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        self.after(0, lambda: self._handle_backend_event(event, data))

    _PHASE_BUTTON_TEXT = {"idle": "Start Recording", "waiting": "Unpause", "recording": "Stop Recording"}
    _SOUND_CHECK_BUTTON_TEXT = {"idle": "Sound Check", "recording": "Stop Sound Check"}

    def _update_recording_active(self) -> None:
        self.app_state.recording_active = (
            self._phase in ("waiting", "recording") or self._sound_check_phase == "recording"
        )

    def _handle_backend_event(self, event: str, data: dict) -> None:
        if event == "recording_status":
            if "status" in data:
                self.status_var.set(data["status"])
            if "phase" in data:
                self._phase = data["phase"]
                self._update_recording_active()
                self.record_button.configure(text=self._PHASE_BUTTON_TEXT[self._phase])
                self.record_button.state(["!disabled"])
                self._set_controls_enabled(self._phase == "idle")
                if self._phase == "idle":
                    self._refresh_playlist_from_server()
                self._update_start_button_state()
        elif event == "sound_check_status":
            if "status" in data:
                self.status_var.set(data["status"])
            if "phase" in data:
                self._sound_check_phase = data["phase"]
                self._update_recording_active()
                self.sound_check_button.configure(text=self._SOUND_CHECK_BUTTON_TEXT[self._sound_check_phase])
                self.sound_check_button.state(["!disabled"])
                self._set_controls_enabled(self._sound_check_phase == "idle")
                self._update_start_button_state()
                # Dialog opening is handled by _streamdeck_driver's own
                # on_sound_check_result hook (see _on_streamdeck_sound_check_
                # result) — it's subscribed to this same event independently,
                # regardless of whether Sound Check was triggered by mouse or
                # by the physical Stream Deck.
        elif event == "preview_paused":
            self.preview_label.configure(text="Recording — preview paused", image="")
        elif event == "preview_resumed":
            self.preview_label.configure(text="Waiting for camera preview...", image="")
        elif event == "preview_error":
            self.preview_label.configure(text=data.get("message", "Camera preview error."), image="")
