"""Record page: pick an instrument + project, preview the camera, and
start/stop a continuous recording.

This is a first cut of the CLI's `start-session` workflow: it captures a
continuous take (audio + optional video) for the selected instrument, the
same way session.flac / session_video.mp4 work under `jampy start-session`.
The per-track backing-track flow (record/back/end/next-track) isn't wired
up yet — this just runs a single continuous take.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..config import StudioConfig
from ..project import Project
from ..utils import ensure_dir, wall_timestamp

PREVIEW_WIDTH = 360


class RecordFrame(ttk.Frame):
    """Instrument/project picker, camera preview, and start/stop recording."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.config_obj = StudioConfig.load()
        self._projects = Project.list_projects(Path(self.config_obj.projects_dir))

        self._engine = None
        self._video_recorder = None
        self._session_dir: Path | None = None
        self._session_video_raw: Path | None = None
        self._session_mix_flac: Path | None = None
        self._recording = False

        self._cv2 = None
        self._cap = None
        self._preview_job: str | None = None
        self._preview_imgtk = None

        row = 0
        ttk.Label(self, text="Record", font=("TkDefaultFont", 14, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        row += 1

        instrument_names = [inst.name for inst in self.config_obj.instruments]
        ttk.Label(self, text="Instrument").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.instrument_var = tk.StringVar(value=instrument_names[0] if instrument_names else "")
        ttk.Combobox(
            self, textvariable=self.instrument_var, values=instrument_names, state="readonly", width=28,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        project_names = [p.name for p in self._projects]
        ttk.Label(self, text="Project").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.project_var = tk.StringVar(value=project_names[0] if project_names else "")
        ttk.Combobox(
            self, textvariable=self.project_var, values=project_names, state="readonly", width=28,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        if not instrument_names:
            ttk.Label(
                self, text="No instruments configured. Set them up on the Instruments tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
        if not project_names:
            ttk.Label(
                self, text=f"No projects found in {self.config_obj.projects_dir}.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        self.preview_label = tk.Label(self, background="#1a1a1a", foreground="white")
        self.preview_label.grid(row=row, column=0, columnspan=2, pady=(12, 12))
        row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        self.record_button = ttk.Button(self, text="Start Recording", command=self._on_toggle_recording)
        self.record_button.grid(row=row, column=0, columnspan=2, pady=(8, 0))
        if not instrument_names or not project_names:
            self.record_button.state(["disabled"])

        self.bind("<Destroy>", self._on_destroy)
        self._start_preview()

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

    def _start_recording(self) -> None:
        instrument_name = self.instrument_var.get()
        project_name = self.project_var.get()
        inst = self.config_obj.get_instrument(instrument_name)
        project_path = next((p for p in self._projects if p.name == project_name), None)
        if inst is None or project_path is None:
            messagebox.showerror("Cannot start", "Select an instrument and a project first.")
            return

        input_info = self.config_obj.resolve_input(inst.input_label)
        if input_info is None:
            messagebox.showerror("Cannot start", f"Input label '{inst.input_label}' not found in config.")
            return

        try:
            import sounddevice as sd
        except Exception as e:
            messagebox.showerror("Cannot start", f"sounddevice unavailable: {e}")
            return

        from ..audio.devices import resolve_device
        out_dev = resolve_device(sd, self.config_obj.output_device, "output")
        in_dev = resolve_device(sd, input_info.device, "input")
        if in_dev is None:
            messagebox.showerror("Cannot start", f"Input device '{input_info.device}' not found.")
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
            return
        output_channels = min(self.config_obj.output_channels, out_info["max_output_channels"])

        project = Project.open(project_path)

        from ..audio.engine import AudioEngine
        self._engine = AudioEngine(
            sample_rate=self.config_obj.sample_rate,
            buffer_size=self.config_obj.buffer_size,
            input_device=in_dev,
            output_device=out_dev,
            input_channels=max(input_info.channel, 1),
            output_channels=max(1, output_channels),
            monitor_channel=input_info.channel - 1,
        )

        session_name = wall_timestamp().replace(":", "-").replace(" ", "_")
        self._session_dir = ensure_dir(project.sessions_dir / f"{session_name}_{inst.name}")

        # Release the preview's camera handle so ffmpeg can open it exclusively.
        self._stop_preview()

        self._engine.start()
        self._engine.start_session_recording(self._session_dir / "session.flac")

        self._video_recorder = None
        if self.config_obj.camera_device:
            from ..video.capture import VideoRecorder, ffmpeg_available
            if ffmpeg_available():
                self._session_video_raw = self._session_dir / "session_video_raw.mp4"
                self._session_mix_flac = self._session_dir / "session_mix.flac"
                self._video_recorder = VideoRecorder(self.config_obj.camera_device, self._session_video_raw)
                if self._video_recorder.start():
                    self._engine.start_mix_recording(self._session_mix_flac)
                else:
                    self._video_recorder = None
                    self.status_var.set("Warning: could not start camera recording.")

        self._recording = True
        self.record_button.configure(text="Stop Recording")
        self.status_var.set(f"Recording to {self._session_dir}")

    def _stop_recording(self) -> None:
        self._stop_recording_internal()
        self.record_button.configure(text="Start Recording")
        self._start_preview()  # camera is free again

    def _stop_recording_internal(self) -> None:
        if not self._recording:
            return
        self._recording = False
        if self._engine:
            self._engine.stop()
            self._engine = None
        if self._video_recorder:
            self._video_recorder.stop()
            from ..video.capture import mux_video_audio
            session_video = self._session_dir / "session_video.mp4"
            if mux_video_audio(self._session_video_raw, self._session_mix_flac, session_video):
                self._session_video_raw.unlink(missing_ok=True)
                self._session_mix_flac.unlink(missing_ok=True)
                self.status_var.set(f"Saved to {session_video}")
            else:
                self.status_var.set(f"Saved; video mux failed (raw files kept in {self._session_dir})")
            self._video_recorder = None
        else:
            self.status_var.set(f"Saved to {self._session_dir / 'session.flac'}")
