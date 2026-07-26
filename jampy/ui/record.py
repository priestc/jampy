"""Record page: pick an instrument + project, choose a track from the
project's playlist or from your inspiration library, preview the camera,
and record a take.

This is a first cut of the CLI's `start-session` / `inspiration` workflow:
picking a track loads its backing track (and any other instruments'
preferred takes) into the mixer and records a proper take file, the same
way `jampy start-session` does for a single track. The full multi-track
auto-advance flow (back-to-start / next-track) isn't wired up yet — start
a new recording for the next track when you're ready.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..config import Instrument, InputLabel, StudioConfig
from ..inspiration import (
    InspirationError,
    download_inspiration_track,
    find_or_add_inspiration_track,
    query_inspiration_tracks,
)
from ..project import Project, TakeInfo, TrackEntry
from ..utils import format_duration, next_take_number, take_filename, wall_timestamp

PREVIEW_WIDTH = 360


class RecordFrame(ttk.Frame):
    """Instrument/project/track picker, camera preview, and recording."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.config_obj = StudioConfig.load()
        self._projects = Project.list_projects(Path(self.config_obj.projects_dir))
        self._project: Project | None = None

        self._selected_track: TrackEntry | None = None
        self._selected_inspiration_info: dict | None = None
        self._selected_track_source: str | None = None  # "playlist" | "inspiration"
        self._inspiration_tracks: list[dict] = []

        self._engine = None
        self._video_recorder = None
        self._current_track: TrackEntry | None = None
        self._current_inst: Instrument | None = None
        self._current_take_num: int = 0
        self._rec_path: Path | None = None
        self._video_raw: Path | None = None
        self._mix_flac: Path | None = None
        self._final_video: Path | None = None
        self._record_start_wall_time: str = ""
        self._recording = False

        self._cv2 = None
        self._cap = None
        self._preview_job: str | None = None
        self._preview_imgtk = None

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(20, 0))
        self.columnconfigure(1, weight=1)

        self._build_left(left)
        self._build_right(right)

        self.bind("<Destroy>", self._on_destroy)
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
        ttk.Label(left, text="Instrument").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.instrument_var = tk.StringVar(value=instrument_names[0] if instrument_names else "")
        self.instrument_combo = ttk.Combobox(
            left, textvariable=self.instrument_var, values=instrument_names, state="readonly", width=28,
        )
        self.instrument_combo.grid(row=row, column=1, sticky="w")
        self.instrument_combo.bind("<<ComboboxSelected>>", self._on_instrument_change)
        row += 1

        project_names = [p.name for p in self._projects]
        ttk.Label(left, text="Project").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.project_var = tk.StringVar(value=project_names[0] if project_names else "")
        self.project_combo = ttk.Combobox(
            left, textvariable=self.project_var, values=project_names, state="readonly", width=28,
        )
        self.project_combo.grid(row=row, column=1, sticky="w")
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_change)
        row += 1

        if not instrument_names:
            ttk.Label(
                left, text="No instruments configured. Set them up on the Instruments tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
        if not project_names:
            ttk.Label(
                left, text=f"No projects found in {self.config_obj.projects_dir}.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        self.preview_label = tk.Label(left, background="#1a1a1a", foreground="white")
        self.preview_label.grid(row=row, column=0, columnspan=2, pady=(12, 12))
        row += 1

        self.selection_var = tk.StringVar(value="No track selected")
        ttk.Label(left, textvariable=self.selection_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.status_var, foreground="#2a7d2a", wraplength=PREVIEW_WIDTH).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row += 1

        self.record_button = ttk.Button(left, text="Start Recording", command=self._on_toggle_recording)
        self.record_button.grid(row=row, column=0, columnspan=2, pady=(8, 0))
        if not instrument_names or not project_names:
            self.record_button.state(["disabled"])

    # --- right column: playlist + inspiration lists ---

    def _build_right(self, right: ttk.Frame) -> None:
        ttk.Label(right, text="Playlist", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        playlist_wrap = ttk.Frame(right)
        playlist_wrap.pack(fill="both", expand=True, pady=(4, 16))
        playlist_scroll = ttk.Scrollbar(playlist_wrap, orient="vertical")
        self.playlist_listbox = tk.Listbox(
            playlist_wrap, height=10, width=44, exportselection=False,
            yscrollcommand=playlist_scroll.set,
        )
        playlist_scroll.configure(command=self.playlist_listbox.yview)
        self.playlist_listbox.pack(side="left", fill="both", expand=True)
        playlist_scroll.pack(side="right", fill="y")
        self.playlist_listbox.bind("<<ListboxSelect>>", self._on_playlist_select)

        inspiration_header = ttk.Frame(right)
        inspiration_header.pack(fill="x")
        ttk.Label(inspiration_header, text="Inspiration Tracks", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(inspiration_header, text="↻ Refresh", command=self._refresh_inspiration).pack(side="right")

        self.inspiration_status_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.inspiration_status_var, foreground="#666666").pack(anchor="w", pady=(2, 4))

        inspiration_wrap = ttk.Frame(right)
        inspiration_wrap.pack(fill="both", expand=True)
        inspiration_scroll = ttk.Scrollbar(inspiration_wrap, orient="vertical")
        self.inspiration_listbox = tk.Listbox(
            inspiration_wrap, height=10, width=44, exportselection=False,
            yscrollcommand=inspiration_scroll.set,
        )
        inspiration_scroll.configure(command=self.inspiration_listbox.yview)
        self.inspiration_listbox.pack(side="left", fill="both", expand=True)
        inspiration_scroll.pack(side="right", fill="y")
        self.inspiration_listbox.bind("<<ListboxSelect>>", self._on_inspiration_select)

    def _track_display(self, track: TrackEntry, inst_name: str) -> str:
        dur = format_duration(track.duration_seconds)
        mark = " ✓" if track.get_take_for_instrument(inst_name) else ""
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
        if not self._project:
            return
        inst_name = self.instrument_var.get()
        for track in self._project.setlist.tracks:
            self.playlist_listbox.insert(tk.END, self._track_display(track, inst_name))

    def _refresh_inspiration(self) -> None:
        self.inspiration_listbox.delete(0, tk.END)
        self._inspiration_tracks = []
        if not self._project:
            return
        self.inspiration_status_var.set("Loading inspiration tracks...")

        def worker() -> None:
            try:
                tracks = query_inspiration_tracks(self._project, self.config_obj)
                error = None
            except InspirationError as e:
                tracks = []
                error = str(e)
            self.after(0, lambda: self._on_inspiration_loaded(tracks, error))

        threading.Thread(target=worker, daemon=True).start()

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
        self._selected_inspiration_info = None
        self._selected_track_source = None
        self.selection_var.set("No track selected")
        self._update_start_button_state()

    def _on_project_change(self, _event: object = None) -> None:
        project_name = self.project_var.get()
        project_path = next((p for p in self._projects if p.name == project_name), None)
        self._project = Project.open(project_path) if project_path else None
        self._clear_selection()
        self._refresh_playlist()
        self._refresh_inspiration()

    def _on_instrument_change(self, _event: object = None) -> None:
        self._refresh_playlist()
        self._update_start_button_state()

    def _on_playlist_select(self, _event: object = None) -> None:
        sel = self.playlist_listbox.curselection()
        if not sel or not self._project:
            return
        self._selected_track = self._project.setlist.tracks[sel[0]]
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
        self._selected_inspiration_info = info
        self._selected_track_source = "inspiration"
        self.playlist_listbox.selection_clear(0, tk.END)
        artist = info.get("artist", "Unknown")
        title = info.get("title", "Unknown")
        self.selection_var.set(f"Selected: {artist} - {title} (inspiration)")
        self._update_start_button_state()

    def _update_start_button_state(self) -> None:
        if self._recording:
            self.record_button.state(["!disabled"])
            return
        ready = (
            bool(self.instrument_var.get())
            and self._project is not None
            and self._selected_track_source is not None
        )
        self.record_button.state(["!disabled"] if ready else ["disabled"])

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.instrument_combo.configure(state=state)
        self.project_combo.configure(state=state)
        list_state = "normal" if enabled else "disabled"
        self.playlist_listbox.configure(state=list_state)
        self.inspiration_listbox.configure(state=list_state)

    # --- camera preview ---

    def _start_preview(self) -> None:
        if not self.config_obj.camera_device:
            self.preview_label.configure(text="No camera configured", image="")
            return
        try:
            import cv2
        except ImportError:
            self.preview_label.configure(
                text="Camera preview unavailable\n(pip install jampy[camera-preview])",
                image="",
            )
            return

        self._cv2 = cv2
        device = self.config_obj.camera_device
        index = int(device) if device.isdigit() else device
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            self.preview_label.configure(text="Could not open camera", image="")
            self._cap = None
            return
        self._schedule_preview_frame()

    def _schedule_preview_frame(self) -> None:
        self._preview_job = self.after(33, self._update_preview)

    def _update_preview(self) -> None:
        if not self._cap:
            return
        ok, frame = self._cap.read()
        if ok:
            from PIL import Image, ImageTk
            cv2 = self._cv2
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = frame.shape[:2]
            new_h = max(1, int(PREVIEW_WIDTH * h / w))
            image = Image.fromarray(frame).resize((PREVIEW_WIDTH, new_h))
            self._preview_imgtk = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self._preview_imgtk, text="")
        self._schedule_preview_frame()

    def _stop_preview(self) -> None:
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _on_destroy(self, _event: object) -> None:
        self._stop_preview()
        self._stop_recording_internal()

    # --- recording ---

    def _on_toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _resolve_target(self) -> tuple[TrackEntry | None, bool, Path | None]:
        """Return (track, needs_download, backing_path) for the current selection."""
        if self._selected_track_source == "playlist" and self._selected_track is not None:
            track = self._selected_track
        elif self._selected_track_source == "inspiration" and self._selected_inspiration_info is not None:
            track = find_or_add_inspiration_track(self._project, self._selected_inspiration_info)
            self._project.save_setlist()
            self._refresh_playlist()
        else:
            return None, False, None
        backing_path = self._project.backing_tracks_dir / track.backing_track
        needs_download = bool(track.inspiration_track_id) and not backing_path.exists()
        return track, needs_download, backing_path

    def _start_recording(self) -> None:
        instrument_name = self.instrument_var.get()
        inst = self.config_obj.get_instrument(instrument_name)
        if inst is None or self._project is None:
            messagebox.showerror("Cannot start", "Select an instrument and a project first.")
            return

        track, needs_download, backing_path = self._resolve_target()
        if track is None:
            messagebox.showerror("Cannot start", "Select a track from the Playlist or Inspiration list first.")
            return

        input_info = self.config_obj.resolve_input(inst.input_label)
        if input_info is None:
            messagebox.showerror("Cannot start", f"Input label '{inst.input_label}' not found in config.")
            return

        self.record_button.state(["disabled"])
        self._set_controls_enabled(False)

        if needs_download:
            self.status_var.set(f"Downloading '{track.name}'...")

            def worker() -> None:
                try:
                    download_inspiration_track(track, backing_path, self.config_obj)
                    error = None
                except InspirationError as e:
                    error = str(e)
                self.after(0, lambda: self._continue_start(inst, input_info, track, error))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._continue_start(inst, input_info, track, None)

    def _continue_start(
        self, inst: Instrument, input_info: InputLabel, track: TrackEntry, error: str | None,
    ) -> None:
        if error:
            messagebox.showerror("Download failed", error)
            self._set_controls_enabled(True)
            self._update_start_button_state()
            return

        try:
            import sounddevice as sd
        except Exception as e:
            messagebox.showerror("Cannot start", f"sounddevice unavailable: {e}")
            self._set_controls_enabled(True)
            self._update_start_button_state()
            return

        from ..audio.devices import resolve_device
        out_dev = resolve_device(sd, self.config_obj.output_device, "output")
        in_dev = resolve_device(sd, input_info.device, "input")
        if in_dev is None:
            messagebox.showerror("Cannot start", f"Input device '{input_info.device}' not found.")
            self._set_controls_enabled(True)
            self._update_start_button_state()
            return

        in_info = sd.query_devices(in_dev, "input")
        out_info = sd.query_devices(out_dev, "output")
        max_in = in_info["max_input_channels"]
        if input_info.channel > max_in:
            messagebox.showerror(
                "Cannot start",
                f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                f"but device only has {max_in} channels.",
            )
            self._set_controls_enabled(True)
            self._update_start_button_state()
            return
        output_channels = min(self.config_obj.output_channels, out_info["max_output_channels"])

        from ..audio.engine import AudioEngine
        engine = AudioEngine(
            sample_rate=self.config_obj.sample_rate,
            buffer_size=self.config_obj.buffer_size,
            input_device=in_dev,
            output_device=out_dev,
            input_channels=max(input_info.channel, 1),
            output_channels=max(1, output_channels),
            monitor_channel=input_info.channel - 1,
        )

        backing_path = self._project.backing_tracks_dir / track.backing_track
        if backing_path.exists():
            engine.mixer.add_source("backing", backing_path, volume=track.volume / 100.0)

        # Layer in other instruments' preferred takes, same as jampy start-session.
        trim = int(self.config_obj.latency_compensation_ms / 1000.0 * self.config_obj.sample_rate)
        for other_inst, take_info in track.preferred_takes.items():
            if other_inst.lower() == inst.name.lower():
                continue
            take_path = self._project.completed_takes_dir / take_info.filename
            if take_path.exists():
                effective_vol = take_info.volume * (track.takes_volume / 100.0)
                engine.mixer.add_source(f"take:{other_inst}", take_path, volume=effective_vol, trim_frames=trim)

        # Release the preview's camera handle so ffmpeg can open it exclusively.
        self._stop_preview()

        self._record_start_wall_time = wall_timestamp()
        engine.start()
        engine.mixer.reset()
        engine.mixer.set_playing(True)

        take_num = next_take_number(self._project.completed_takes_dir, track.name, inst.name)
        fname = take_filename(track.name, inst.name, take_num, "flac")
        rec_path = self._project.completed_takes_dir / fname
        engine.start_recording(rec_path)

        video_recorder = None
        video_raw = mix_flac = final_video = None
        if self.config_obj.camera_device:
            from ..video.capture import VideoRecorder, ffmpeg_available
            if ffmpeg_available():
                video_raw = rec_path.with_name(rec_path.stem + "_video_raw.mp4")
                mix_flac = rec_path.with_name(rec_path.stem + "_mix.flac")
                final_video = rec_path.with_suffix(".mp4")
                video_recorder = VideoRecorder(self.config_obj.camera_device, video_raw)
                if video_recorder.start():
                    engine.start_mix_recording(mix_flac)
                else:
                    video_recorder = None
                    self.status_var.set("Warning: could not start camera recording.")

        self._engine = engine
        self._video_recorder = video_recorder
        self._current_track = track
        self._current_inst = inst
        self._current_take_num = take_num
        self._rec_path = rec_path
        self._video_raw = video_raw
        self._mix_flac = mix_flac
        self._final_video = final_video

        self._recording = True
        self.record_button.configure(text="Stop Recording")
        self.record_button.state(["!disabled"])
        self.status_var.set(f"Recording '{track.name}' — take {take_num}")

    def _stop_recording(self) -> None:
        self._stop_recording_internal()
        self.record_button.configure(text="Start Recording")
        self._set_controls_enabled(True)
        self._refresh_playlist()
        self._start_preview()

    def _stop_recording_internal(self) -> None:
        if not self._recording:
            return
        self._recording = False

        engine = self._engine
        self._engine = None
        if engine:
            engine.stop_recording()
            engine.mixer.set_playing(False)

        take_info = TakeInfo(
            instrument=self._current_inst.name, take_number=self._current_take_num,
            filename=self._rec_path.name,
        )
        self._current_track.set_preferred_take(self._current_inst.name, take_info)
        self._project.save_setlist()

        if engine:
            engine.stop()

        if self._video_recorder:
            self._video_recorder.stop()
            from ..video.capture import format_watermark_text, mux_video_audio
            musician = self._current_inst.musician or self.config_obj.studio_musician
            watermark_text = format_watermark_text(
                musician, self._current_inst.name, self._record_start_wall_time, self._current_track.name,
            )
            if mux_video_audio(
                self._video_raw, self._mix_flac, self._rec_path, self._final_video,
                watermark_text=watermark_text,
            ):
                self._video_raw.unlink(missing_ok=True)
                self._mix_flac.unlink(missing_ok=True)
                self.status_var.set(f"Saved take {self._current_take_num} + video for '{self._current_track.name}'")
            else:
                self.status_var.set(
                    f"Saved take {self._current_take_num} for '{self._current_track.name}'; video mux failed"
                )
            self._video_recorder = None
        else:
            self.status_var.set(f"Saved take {self._current_take_num} for '{self._current_track.name}'")

        self._update_start_button_state()
