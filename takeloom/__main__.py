"""Entry point for Takeloom CLI."""

from __future__ import annotations

import os
import sys
import queue
import select
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .audio.engine import AudioEngine

from .config import (
    DEFAULT_CONFIG_PATH,
    StudioConfig,
    InputLabel,
    Instrument,
    VALID_SAMPLE_RATES,
    VALID_BUFFER_SIZES,
)
from .project import Project, Setlist, TrackEntry
from .audio.devices import resolve_device as _resolve_device
from .audio.formats import SUPPORTED_EXTS, get_duration
from .inspiration import (
    InspirationError,
    find_or_add_inspiration_track,
    query_inspiration_tracks,
)
from .utils import format_duration, take_filename, next_take_number, ensure_dir


@click.group()
def main() -> None:
    """Takeloom - Music Recording Session Manager."""


@main.command()
def setup_studio() -> None:
    """Configure studio name, location, musician, and backup server."""
    click.echo("=== Studio Setup ===\n")

    existing = StudioConfig.load()

    existing.studio_name = click.prompt("Studio name", default=existing.studio_name, show_default=bool(existing.studio_name))
    existing.studio_location = click.prompt("Studio location", default=existing.studio_location, show_default=bool(existing.studio_location))
    existing.studio_musician = click.prompt("Studio musician (default performer)", default=existing.studio_musician, show_default=bool(existing.studio_musician))
    existing.backup_server = click.prompt("Backup server (user@host:/path, or empty to skip)", default=existing.backup_server, show_default=bool(existing.backup_server))
    existing.inspiration_server = click.prompt("Inspiration server URL (or empty to skip)", default=existing.inspiration_server, show_default=bool(existing.inspiration_server))
    existing.inspiration_api_key = click.prompt("Inspiration API key (or empty to skip)", default=existing.inspiration_api_key, show_default=bool(existing.inspiration_api_key))

    errors = existing.validate()
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")


@main.command()
def setup_recording_devices() -> None:
    """Configure audio devices, sample rate, buffer size, and input labels."""
    click.echo("=== Recording Devices Setup ===\n")

    existing = StudioConfig.load()

    # Query available audio devices
    devices = []
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        click.echo("Available audio devices:")
        for i, d in enumerate(devices):
            ins = d["max_input_channels"]
            outs = d["max_output_channels"]
            click.echo(f"  [{i}] {d['name']}  (in={ins}, out={outs})")
        click.echo()
    except Exception:
        click.echo("Could not query audio devices (sounddevice unavailable).\n")

    # Sample rate
    sr_choices = [str(r) for r in VALID_SAMPLE_RATES]
    sample_rate = click.prompt(
        "Sample rate",
        type=click.Choice(sr_choices),
        default=str(existing.sample_rate),
    )

    # Buffer size
    buf_choices = [str(b) for b in VALID_BUFFER_SIZES]
    buffer_size = click.prompt(
        "Buffer size",
        type=click.Choice(buf_choices),
        default=str(existing.buffer_size),
    )

    # Output device
    if devices:
        click.echo("Output devices:")
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                click.echo(f"  [{i}] {d['name']}  ({d['max_output_channels']} channels)")
    out_idx = click.prompt("Output device index", type=int, default=0)
    output_device_name = devices[out_idx]["name"] if devices else ""
    if existing.output_device:
        output_device_name = click.prompt("Output device name", default=existing.output_device)
    else:
        click.echo(f"  Selected: {output_device_name}")
    max_out = devices[out_idx]["max_output_channels"] if devices else 2
    output_channels = click.prompt("Output channels", type=int, default=existing.output_channels if existing.output_channels <= max_out else min(2, max_out))

    # Latency compensation
    default_comp = existing.latency_compensation_ms
    if default_comp == 0.0:
        default_comp = round(int(buffer_size) / int(sample_rate) * 1000, 1)
    latency_compensation_ms = click.prompt(
        "Latency compensation (ms)",
        type=float,
        default=default_comp,
    )

    # --- Audio Interface & Input Setup ---
    input_labels: list[InputLabel] = list(existing.input_labels)
    if devices:
        click.echo("\n--- Audio Interface Setup ---")
        input_devs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        if input_devs:
            click.echo("Available input devices:")
            existing_dev_names = {il.device for il in input_labels}
            for i, d in input_devs:
                marker = " *" if d["name"] in existing_dev_names else ""
                click.echo(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch){marker}")
            if existing_dev_names:
                click.echo("  (* = already configured)")
            click.echo()

            existing_indices = []
            for i, d in input_devs:
                if d["name"] in existing_dev_names:
                    existing_indices.append(str(i))
            default_sel = ",".join(existing_indices) if existing_indices else ""

            sel = click.prompt(
                "Select interface(s) (comma-separated indices, or empty to skip)",
                default=default_sel, show_default=bool(default_sel),
            ).strip()

            selected_devs = []
            if sel:
                for s in sel.split(","):
                    s = s.strip()
                    if s.isdigit():
                        idx = int(s)
                        if 0 <= idx < len(devices) and devices[idx]["max_input_channels"] > 0:
                            selected_devs.append((idx, devices[idx]))

            new_labels: list[InputLabel] = []
            for dev_idx, dev in selected_devs:
                dev_name = dev["name"]
                max_ch = dev["max_input_channels"]
                click.echo(f"\n  Interface: {dev_name} ({max_ch} channels)")

                existing_for_dev = {il.channel: il.label for il in input_labels if il.device == dev_name}

                if existing_for_dev:
                    default_chs = ",".join(str(ch) for ch in sorted(existing_for_dev.keys()))
                else:
                    default_chs = "1"
                ch_sel = click.prompt(
                    f"  Channels to use (1-{max_ch}, comma-separated)",
                    default=default_chs,
                ).strip()

                channels = []
                for c in ch_sel.split(","):
                    c = c.strip()
                    if c.isdigit():
                        ch = int(c)
                        if 1 <= ch <= max_ch:
                            channels.append(ch)

                for ch in channels:
                    default_label = existing_for_dev.get(ch, f"{dev_name} Ch{ch}")
                    label = click.prompt(f"  Label for channel {ch}", default=default_label)
                    new_labels.append(InputLabel(label=label, device=dev_name, channel=ch))

            if new_labels:
                input_labels = new_labels

    # --- Camera Setup ---
    click.echo("\n--- Camera Setup ---")
    from .video.devices import list_cameras, ffmpeg_available
    camera_device = existing.camera_device
    camera_label = existing.camera_label
    if not ffmpeg_available():
        click.echo("ffmpeg not found; camera recording will be unavailable.")
    else:
        cameras = list_cameras()
        if cameras:
            click.echo("Available cameras:")
            for device_id, name in cameras:
                marker = " *" if device_id == existing.camera_device else ""
                click.echo(f"  [{device_id}] {name}{marker}")
            sel = click.prompt(
                "Select camera device id (leave empty to disable camera recording)",
                default=existing.camera_device, show_default=bool(existing.camera_device),
            ).strip()
            if not sel:
                camera_device = ""
                camera_label = ""
            else:
                camera_device = sel
                camera_label = next((name for dev_id, name in cameras if dev_id == sel), "")
        else:
            click.echo("No cameras detected.")

    existing.sample_rate = int(sample_rate)
    existing.buffer_size = int(buffer_size)
    existing.output_device = output_device_name
    existing.output_channels = output_channels
    existing.latency_compensation_ms = latency_compensation_ms
    existing.input_labels = input_labels
    existing.camera_device = camera_device
    existing.camera_label = camera_label

    errors = existing.validate()
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")
    if input_labels:
        click.echo("Inputs:")
        for il in input_labels:
            click.echo(f"  - {il.label} ({il.device} ch{il.channel})")
    if camera_device:
        click.echo(f"Camera: {camera_label} ({camera_device})")
    else:
        click.echo("Camera: none (video recording disabled)")


@main.command(name="ui")
@click.option(
    "--remote", "remote_ip", default=None,
    help="Connect to a remote takeloom instance at this IP/host on launch (e.g. --remote=192.168.1.190). "
         "Uses a stored token if this host is already a known remote (Remote tab); otherwise this "
         "triggers a pairing request that the other machine's user must approve.",
)
def ui_command(remote_ip: str | None) -> None:
    """Launch the graphical Takeloom interface."""
    from . import update_check
    update_check.check_and_restart(log=click.echo)

    from .ui.app import run
    run(remote_ip=remote_ip)


@main.command(name="server")
@click.option(
    "--disable-color", is_flag=True, default=False,
    help="Strip ANSI color codes from server log output.",
)
def server_command(disable_color: bool) -> None:
    """Run the remote-control server. This is the only way to host a
    takeloom instance for other clients to connect to — the GUI's Remote tab
    is connect-only. Always listens on the fixed remote.protocol.
    REMOTE_SERVER_PORT (not configurable — see that module for why), and
    only accepts connections from the local network.
    """
    from .backend import LocalBackend, StartRecordingRequest
    from .device_check import check_configured_devices
    from .recording_driver import RecordingDeckDriver
    from .remote.protocol import REMOTE_SERVER_PORT
    from .remote.server import RemoteServer

    def log(msg: str, err: bool = False) -> None:
        # Errors print in red so they stand out in a scrolling headless log;
        # everything else stays the terminal's default color. Detected by the
        # explicit `err` flag or an "error" substring, since most log lines
        # here (and the ones threaded through RemoteServer/RecordingDeckDriver,
        # whose `log` callback only takes a message) don't carry a separate
        # severity of their own.
        is_error = err or "error" in msg.lower()
        if disable_color:
            click.echo(msg, err=err)
        else:
            click.secho(msg, fg="red" if is_error else None, err=err)

    backend = LocalBackend()
    listen_port = REMOTE_SERVER_PORT

    # Deliberately no sleep_guard.track_backend(backend) here: a headless
    # server machine should be free to let its own screensaver/sleep kick
    # in regardless of recording state — only a UI actually being watched
    # (local or a Remote client) has a reason to hold it off. See
    # AppState.recording_active, which does that for the UI side.

    def on_streaming_event(event: str, data: dict) -> None:
        # The only backend event this headless console prints on its own
        # (everything else is either driven by StreamDeck key presses,
        # which RecordingDeckDriver already narrates via `log`, or is
        # display-only state a connected Remote client would show). A
        # streaming session has no on-screen status anywhere in this
        # context otherwise, so without this, a stream starting, YouTube
        # accepting (or rejecting) the title API calls, and the stream
        # ending would all happen invisibly here.
        if event == "streaming_status" and "status" in data:
            log(data["status"])

    backend.on_event(on_streaming_event)

    def request_authorization(ip: str, client_name: str) -> bool:
        log(f"\nPairing request from '{client_name}' ({ip})")
        try:
            return click.confirm("Approve this connection?", default=False)
        except click.exceptions.Abort:
            # No TTY attached to stdin (e.g. this process was started
            # detached/backgrounded) — click.confirm can't prompt at all in
            # that case and raises instead of returning. Deny rather than
            # let this crash the connection-handler thread with no response
            # ever sent back to the waiting client.
            log("Cannot prompt for approval (no interactive terminal attached) — denying.", err=True)
            return False

    server = RemoteServer(backend, listen_port, request_authorization, log=log)
    try:
        server.start()
    except OSError as e:
        log(f"Error: could not start server: {e}", err=True)
        raise SystemExit(1)

    log(f"takeloom server listening on port {listen_port} (host: {backend.hostname()}, ip: {backend.ip_address()})")
    # StreamDeck is checked separately below, via the real connection attempt
    # (driver.connect()), which reports a more specific error than a plain
    # not-found when a device is selected but fails to open.
    for warning in check_configured_devices(backend, include_streamdeck=False):
        log(warning, err=True)

    # Best-effort: open live monitoring for the last-used instrument right
    # away, so the operator can hear themselves in headphones immediately —
    # not only once a take/session/video-check actually starts. See
    # LocalBackend.start_monitoring().
    if backend.start_monitoring():
        log(f"Live-monitoring '{backend.get_config().last_selected_instrument}'.")

    # Optional attached StreamDeck: fully drives a session with no UI client
    # needed at all, via the same RecordingDeckDriver the Tk UI uses. With
    # no track picker of its own, this context always targets the last-used
    # project + instrument (from config) and the next untaken track.
    def _resolve_headless_request() -> StartRecordingRequest | None:
        cfg = backend.get_config()
        project_name, instrument_name = cfg.last_selected_project, cfg.last_selected_instrument
        if not project_name or not instrument_name:
            log("StreamDeck: no last-used project/instrument — start one from a connected client first.")
            return None
        index = backend.next_untaken_track_index(project_name, instrument_name)
        if index is None:
            log(f"StreamDeck: no more tracks in '{project_name}' need a take for '{instrument_name}'.")
            return None
        return StartRecordingRequest(
            project_name=project_name, instrument_name=instrument_name,
            track_source="setlist", track_index=index,
        )

    def _open_video_check_result(path: Path, has_video: bool) -> None:
        # Always open locally — the server is often where the real
        # monitoring (headphones/speakers) actually lives, so whoever's
        # sitting at it needs to review the result too, not just whoever's
        # on a connected Remote client. Also send it to any connected Remote
        # client (chunked, since it can be tens of MB) so whichever machine
        # an operator is actually sitting at can review it in its own
        # native player.
        from .video.capture import open_in_default_player
        open_in_default_player(path)
        if server.client_count > 0:
            server.broadcast_file("video_check_result", path, extra={"has_video": has_video})

    driver = RecordingDeckDriver(
        backend, resolve_start_request=_resolve_headless_request,
        on_video_check_result=_open_video_check_result, log=log,
    )
    if driver.connect():
        log("StreamDeck connected.")
    elif driver.streamdeck.last_error:
        log(f"StreamDeck: found a device but could not connect — {driver.streamdeck.last_error}", err=True)

    log("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log("\nStopping server...")
        server.stop()
        driver.disconnect()
        try:
            if backend.is_session_active():
                log("Ending active session...")
                backend.stop_recording()
        except BackendError as e:
            log(f"Error ending session: {e}", err=True)
        backend.join_session_processing()


@main.command()
def setup_instruments() -> None:
    """Configure instruments and their input assignments."""
    click.echo("=== Instrument Setup ===\n")

    existing = StudioConfig.load()

    if existing.input_labels:
        click.echo("Available inputs:")
        for i, il in enumerate(existing.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        click.echo()
    else:
        click.echo("No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
        raise SystemExit(1)

    if existing.instruments:
        click.echo("Existing instruments:")
        for inst in existing.instruments:
            click.echo(f"  - {inst.name} ({inst.input_label})")
        click.echo()

    instruments: list[Instrument] = []
    while True:
        if not click.confirm("Add an instrument?", default=bool(not instruments)):
            break
        name = click.prompt("  Instrument name")
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(existing.input_labels):
            input_label_name = existing.input_labels[choice - 1].label
        else:
            click.echo(f"  Invalid choice, using first input.")
            input_label_name = existing.input_labels[0].label
        full_name = click.prompt("  Full name (manufacturer & model)", default="", show_default=False)
        musician = click.prompt("  Musician name", default="", show_default=False)
        instruments.append(Instrument(
            name=name, input_label=input_label_name,
            full_name=full_name, musician=musician,
        ))
        click.echo(f"  Added '{name}'.\n")

    if instruments:
        existing.instruments = instruments
    else:
        click.echo("No instruments added; keeping existing config.")

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")
    if existing.instruments:
        click.echo("Instruments:")
        for inst in existing.instruments:
            click.echo(f"  - {inst.name} ({inst.input_label})")


@main.command()
def new_project() -> None:
    """Create a new recording project."""
    config = StudioConfig.load()
    projects_dir = Path(config.projects_dir)

    name = click.prompt("Project name")
    project = Project.create_new(projects_dir, name)
    if config.backup_server:
        project.setlist.backup_server = config.backup_server
        project.save_setlist()

    click.echo(f"Created project: {project.path}")
    click.echo("  backing_tracks/")
    click.echo("  completed_takes/")
    click.echo("  sessions/")
    click.echo("  setlist.json")

    # Scan backing_tracks/ and update setlist
    saved_cwd = Path.cwd()
    try:
        os.chdir(project.path)
        ctx = click.get_current_context()
        ctx.invoke(update_setlist)
    except SystemExit:
        pass
    finally:
        os.chdir(saved_cwd)

    click.echo()
    click.echo("To enable inspiration features, add an \"inspiration\" key to setlist.json:")
    click.echo('  "inspiration": [')
    click.echo('    {"genre": "Rock"},')
    click.echo('    {"artist": "Miles Davis"},')
    click.echo('    {"genre": "Blues", "decade": "1960s"}')
    click.echo('  ]')


@main.command()
def sync_push() -> None:
    """Push local project files to the backup server."""
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    remote = project.setlist.backup_server or StudioConfig.load().backup_server
    if not remote:
        click.echo("Error: No backup server configured.", err=True)
        click.echo("Set it in setlist.json or via 'takeloom setup-studio'.")
        raise SystemExit(1)

    from .sync import sync_up
    sync_up(project.path, remote)


@main.command()
def sync_pull() -> None:
    """Pull project files from the backup server."""
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    remote = project.setlist.backup_server or StudioConfig.load().backup_server
    if not remote:
        click.echo("Error: No backup server configured.", err=True)
        click.echo("Set it in setlist.json or via 'takeloom setup-studio'.")
        raise SystemExit(1)

    from .sync import sync_down
    sync_down(project.path, remote)


@main.command()
def update_setlist() -> None:
    """Scan backing_tracks/ and update setlist.json in the current directory."""
    cwd = Path.cwd()
    setlist_path = cwd / "setlist.json"
    backing_dir = cwd / "backing_tracks"

    if not setlist_path.exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    if not backing_dir.exists():
        click.echo("Error: No backing_tracks/ directory found.", err=True)
        raise SystemExit(1)

    # Load existing setlist
    project = Project.open(cwd)
    existing_files = {t.backing_track for t in project.setlist.tracks}

    # Scan for backing-track-able files (audio, or video for its audio stream)
    found_files = {
        f.name for f in backing_dir.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
    }

    # Add new tracks
    added = 0
    for fname in sorted(found_files - existing_files):
        fpath = backing_dir / fname
        try:
            duration = get_duration(fpath)
        except Exception:
            duration = 0.0
        track = TrackEntry(
            name=fpath.stem,
            backing_track=fname,
            duration_seconds=duration,
        )
        project.setlist.add_track(track)
        click.echo(f"  + {fname} ({format_duration(duration)})")
        added += 1

    # Remove tracks whose files no longer exist
    removed = 0
    kept_tracks = []
    for track in project.setlist.tracks:
        if track.backing_track in found_files:
            kept_tracks.append(track)
        else:
            click.echo(f"  - {track.backing_track} (removed)")
            removed += 1
    project.setlist.tracks = kept_tracks

    project.save_setlist()
    click.echo(f"\nSetlist updated: {added} added, {removed} removed, {len(kept_tracks)} total.")


@main.command()
def listen() -> None:
    """Listen to mixed takes for a track (without backing track)."""
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    if not project.setlist.tracks:
        click.echo("No tracks in setlist.")
        raise SystemExit(1)

    # Display tracks with their available takes
    click.echo("=== Tracks ===\n")
    tracks_with_takes = []
    for i, track in enumerate(project.setlist.tracks):
        instruments = list(track.preferred_takes.keys())
        if instruments:
            click.echo(f"  [{i + 1}] {track.name}  ({', '.join(instruments)})")
            tracks_with_takes.append(i)
        else:
            click.echo(f"  [{i + 1}] {track.name}  (no takes)")
    click.echo()

    if not tracks_with_takes:
        click.echo("No tracks have recorded takes yet.")
        raise SystemExit(1)

    choice = click.prompt("Select track number", type=int)
    idx = choice - 1
    if idx < 0 or idx >= len(project.setlist.tracks):
        click.echo("Invalid track number.", err=True)
        raise SystemExit(1)

    track = project.setlist.tracks[idx]
    if not track.preferred_takes:
        click.echo(f"No takes recorded for '{track.name}'.")
        raise SystemExit(1)

    # Import audio modules
    import sounddevice as sd
    from .audio.mixer import Mixer

    config = StudioConfig.load()

    # Load all preferred takes into mixer with latency compensation
    mixer = Mixer(config.sample_rate)
    trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
    click.echo(f"\nPlaying: {track.name}")
    for inst_name, take_info in track.preferred_takes.items():
        take_path = project.completed_takes_dir / take_info.filename
        if take_path.exists():
            mixer.add_source(f"take:{inst_name}", take_path, volume=take_info.volume, trim_frames=trim)
            click.echo(f"  + {inst_name}: {take_info.filename}")
        else:
            click.echo(f"  ! {inst_name}: {take_info.filename} (file missing)")

    if not mixer.sources:
        click.echo("No take files found on disk.")
        raise SystemExit(1)

    click.echo(f"\nDuration: {format_duration(mixer.duration_seconds)}")
    click.echo("Press Ctrl+C to stop.\n")

    mixer.set_playing(True)

    # Play through output device
    out_dev = _resolve_device(sd, config.output_device, "output")
    out_info = sd.query_devices(out_dev, "output")
    out_channels = min(config.output_channels, out_info["max_output_channels"])

    def callback(outdata, frames, time_info, status):
        mix = mixer.read(frames)
        if out_channels == 2:
            outdata[:] = mix
        else:
            outdata[:, 0] = mix[:, 0]
        if mixer.is_finished:
            raise sd.CallbackStop

    try:
        with sd.OutputStream(
            samplerate=config.sample_rate,
            blocksize=config.buffer_size,
            device=out_dev,
            channels=max(1, out_channels),
            dtype="float32",
            callback=callback,
        ):
            while mixer.is_playing and not mixer.is_finished:
                sd.sleep(100)
    except KeyboardInterrupt:
        pass

    click.echo("Done.")


@main.command()
@click.argument("instrument")
def start_session(instrument: str) -> None:
    """Start a recording session for INSTRUMENT.

    Runs on LocalBackend — the same recording engine and Stream Deck driver
    (RecordingDeckDriver) as the Tk UI and `takeloom server`, so behavior is
    identical across all three. Layered on top: a continuous whole-session
    audio+video recording spanning every track (begin_session()/
    end_session()), which the UI/server don't use.
    """
    from .backend import BackendError, LocalBackend, StartRecordingRequest
    from .recording_driver import RecordingDeckDriver

    # Load config and validate instrument
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
            raise SystemExit(1)
        click.echo(f"Instrument '{instrument}' not found in config. Let's set it up.\n")
        click.echo("Available inputs:")
        for i, il in enumerate(config.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(config.input_labels):
            input_label_name = config.input_labels[choice - 1].label
        else:
            click.echo(f"  Invalid choice, using first input.")
            input_label_name = config.input_labels[0].label
        full_name = click.prompt("  Full name (manufacturer & model)", default="", show_default=False)
        musician = click.prompt("  Musician name", default=config.studio_musician, show_default=bool(config.studio_musician))
        inst = Instrument(
            name=instrument, input_label=input_label_name,
            full_name=full_name, musician=musician,
        )
        config.instruments.append(inst)
        config.save()
        click.echo(f"  Saved '{instrument}' to config.\n")

    # Check we're in a project directory. Note: unlike the pre-Backend
    # version of this command, the project must also be discoverable under
    # config.projects_dir by name — LocalBackend resolves projects by name,
    # same requirement the UI and `takeloom server` already have.
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)

    if project.setlist.backup_server:
        from .sync import sync_down
        sync_down(project.path, project.setlist.backup_server)
        project.load_setlist()  # reload after sync may have updated it

    if not project.setlist.tracks:
        click.echo("Error: Setlist is empty. Run 'takeloom update-setlist' first.", err=True)
        raise SystemExit(1)

    backend = LocalBackend()
    # Deliberately no sleep_guard.track_backend(backend) here — see the
    # matching comment in server_command; a CLI recording session is no
    # different from a headless server in that regard.

    click.echo(f"=== Recording Session: {project.name} / {inst.name} ===")
    click.echo(f"Tracks: {len(project.setlist.tracks)}")
    click.echo("Controls: [r] record/unpause/stop  [c] video check  [n]ext track  [b] restart take")
    click.echo("          [l]ower volume  [u]p volume  [[]lower takes  []]raise takes")
    click.echo("          [q]uit\n")

    try:
        backend.begin_session(project.name, inst.name)
    except BackendError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    def _resolve_cli_request() -> StartRecordingRequest | None:
        index = backend.next_untaken_track_index(project.name, inst.name)
        if index is None:
            click.echo(f"All tracks already have a take for '{inst.name}'.")
            return None
        return StartRecordingRequest(
            project_name=project.name, instrument_name=inst.name,
            track_source="setlist", track_index=index,
        )

    def _open_video_check_result(path: Path, has_video: bool) -> None:
        from .video.capture import open_in_default_player
        open_in_default_player(path)

    driver = RecordingDeckDriver(
        backend, resolve_start_request=_resolve_cli_request,
        on_video_check_result=_open_video_check_result, log=click.echo,
    )
    if driver.connect():
        click.echo("StreamDeck connected.")
    elif driver.streamdeck.last_error:
        click.echo(f"StreamDeck: found a device but could not connect — {driver.streamdeck.last_error}")

    # Load the first untaken track, same as the headless server's "r" does
    # on first press — here it happens automatically on launch, matching
    # this command's previous behavior of loading a track immediately.
    req = _resolve_cli_request()
    if req is not None:
        try:
            backend.start_recording(req)
        except BackendError as e:
            click.echo(f"Error: {e}", err=True)
    click.echo()

    # Terminal keystrokes feed the exact same driver.handle_key() the
    # Stream Deck uses — one dispatcher, two input sources (recording_driver.py).
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            if select.select([sys.stdin], [], [], 0.2)[0]:
                key = sys.stdin.read(1).lower()
                if key == "q":
                    break
                driver.handle_key(key)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    driver.disconnect()
    try:
        backend.stop_recording()  # ends the session; no-op if "r" already did
    except BackendError as e:
        click.echo(f"Error ending session: {e}", err=True)
    click.echo("Processing session takes...")
    backend.join_session_processing()

    # Re-opened fresh: post-session processing (see processing/splicer.py)
    # just wrote every completed take and the setlist to disk — this cleanup
    # step needs that current on-disk state, not the stale copy opened
    # before the session.
    fresh_project = Project.open(cwd)
    for track in fresh_project.setlist.tracks:
        if track.inspiration_track_id:
            bt_path = fresh_project.backing_tracks_dir / track.backing_track
            if bt_path.exists():
                bt_path.unlink()

    remote = fresh_project.setlist.backup_server or config.backup_server
    if remote:
        from .sync import sync_up
        sync_up(fresh_project.path, remote)


@contextmanager
def _recording_context(project=None, config=None):
    """Context manager for any recording mode.

    Yields (streamdeck, sd_keys). On exit, disconnects the StreamDeck and
    syncs to the backup server if a project and remote are configured.
    Any future recording mode gets these behaviours for free by using:

        with _recording_context(project, config) as (streamdeck, sd_keys):
            ...
    """
    from .streamdeck_controller import StreamDeckController
    sd_keys: queue.Queue[str] = queue.Queue()
    streamdeck = StreamDeckController()
    device_id = config.streamdeck_id if config else ""
    if not device_id:
        click.echo("Skipping StreamDeck initialization, as none are configured.")
    elif streamdeck.connect(sd_keys.put, device_id=device_id):
        click.echo("StreamDeck connected.")
    elif streamdeck.last_error:
        click.echo(f"StreamDeck: found a device but could not connect — {streamdeck.last_error}")
    try:
        yield streamdeck, sd_keys
    finally:
        streamdeck.disconnect()
        if project is not None:
            remote = project.setlist.backup_server or (config.backup_server if config else None)
            if remote:
                from .sync import sync_up
                sync_up(project.path, remote)


@main.command()
@click.argument("instrument")
def measure_latency(instrument: str) -> None:
    """Measure and calibrate latency compensation by ear for INSTRUMENT."""
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
            raise SystemExit(1)
        click.echo(f"Instrument '{instrument}' not found in config. Let's set it up.\n")
        click.echo("Available inputs:")
        for i, il in enumerate(config.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(config.input_labels):
            input_label_name = config.input_labels[choice - 1].label
        else:
            click.echo("  Invalid choice, using first input.")
            input_label_name = config.input_labels[0].label
        full_name = click.prompt("  Full name (manufacturer & model)", default="", show_default=False)
        musician = click.prompt("  Musician name", default=config.studio_musician, show_default=bool(config.studio_musician))
        inst = Instrument(
            name=instrument, input_label=input_label_name,
            full_name=full_name, musician=musician,
        )
        config.instruments.append(inst)
        config.save()
        click.echo(f"  Saved '{instrument}' to config.\n")

    import sounddevice as sd
    from .audio.engine import AudioEngine

    input_info = config.resolve_input(inst.input_label)
    if input_info is None:
        click.echo(f"Error: Input label '{inst.input_label}' not found in config.", err=True)
        raise SystemExit(1)
    in_dev = _resolve_device(sd, input_info.device, "input")
    if in_dev is None:
        click.echo(f"Error: Input device '{input_info.device}' not found.", err=True)
        raise SystemExit(1)
    out_dev = _resolve_device(sd, config.output_device, "output")

    in_info = sd.query_devices(in_dev, "input")
    out_info = sd.query_devices(out_dev, "output")
    input_channel_index = input_info.channel - 1
    input_channels = max(input_info.channel, 1)
    output_channels = min(config.output_channels, out_info["max_output_channels"])

    if input_channels > in_info["max_input_channels"]:
        click.echo(
            f"Error: Instrument '{inst.name}' needs input channel {input_channels} "
            f"but device only has {in_info['max_input_channels']} channels.",
            err=True,
        )
        raise SystemExit(1)

    ref_wav = Path(__file__).parent / "data" / "measure_latency.wav"
    if not ref_wav.exists():
        click.echo(f"Error: Reference audio not found at {ref_wav}", err=True)
        raise SystemExit(1)

    import tempfile
    tmp_recording = Path(tempfile.mktemp(suffix=".flac", prefix="takeloom_latency_"))

    engine = AudioEngine(
        sample_rate=config.sample_rate,
        buffer_size=config.buffer_size,
        input_device=in_dev,
        output_device=out_dev,
        input_channels=input_channels,
        output_channels=max(1, output_channels),
        monitor_channel=input_channel_index,
    )
    engine.start()

    try:
        click.echo("=== Latency Measurement ===\n")
        click.echo(f"  Instrument:  {inst.name}")
        click.echo(f"  Input:       {input_info.label} ({input_info.device} ch{input_info.channel})")
        click.echo(f"  Output:      {config.output_device}")
        click.echo()
        click.echo("You'll hear a rhythm of beeps ending with a loud HIT tone.")
        click.echo("Clap or hit your instrument exactly on the HIT.\n")

        if _latency_record_phase(engine, ref_wav, tmp_recording):
            _latency_adjust_phase(engine, ref_wav, tmp_recording, config)
    finally:
        engine.stop()
        if tmp_recording.exists():
            tmp_recording.unlink()


def _latency_record_phase(engine: AudioEngine, ref_wav: Path, tmp_recording: Path) -> bool:
    """Record phase: play reference, record clap. Returns True to continue to adjust."""
    while True:
        engine.mixer.clear()
        engine.mixer.add_source("ref", ref_wav)
        engine.start_recording(tmp_recording)
        engine.mixer.reset()
        engine.mixer.set_playing(True)

        click.echo("  Playing reference... clap/hit on the HIT tone!")

        import sounddevice as sd
        while not engine.mixer.is_finished:
            sd.sleep(100)

        engine.stop_recording()
        engine.mixer.set_playing(False)

        click.echo("  Recording captured.")
        action = click.prompt("  [r]etry, [c]ontinue to adjust, [q]uit", type=click.Choice(["r", "c", "q"]))

        if action == "c":
            return True
        elif action == "q":
            return False
        else:
            # Retry — delete recording and loop
            if tmp_recording.exists():
                tmp_recording.unlink()


def _latency_adjust_phase(
    engine: AudioEngine, ref_wav: Path, tmp_recording: Path, config: StudioConfig
) -> None:
    """Adjustment phase: play ref + recording together, adjust trim with u/d keys."""
    latency_ms = config.latency_compensation_ms
    sample_rate = config.sample_rate

    def _load_and_play() -> None:
        trim = int(latency_ms / 1000.0 * sample_rate)
        engine.mixer.clear()
        engine.mixer.add_source("ref", ref_wav)
        engine.mixer.add_source("recording", tmp_recording, trim_frames=trim)
        engine.mixer.reset()
        engine.mixer.set_playing(True)

    _load_and_play()

    click.echo(f"\n  Current latency: {latency_ms:.0f} ms")
    click.echo("  Controls: [u] +5ms  [d] -5ms  [r] replay  [s] save  [q] quit")
    click.echo("  Listening... adjust until the clap aligns with the HIT tone.\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        import sounddevice as sd
        while True:
            if select.select([sys.stdin], [], [], 0.2)[0]:
                key = sys.stdin.read(1).lower()

                if key == "u":
                    latency_ms += 5
                    trim = int(latency_ms / 1000.0 * sample_rate)
                    engine.mixer.set_trim("recording", trim)
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo(f"  Latency: {latency_ms:.0f} ms")

                elif key == "d":
                    latency_ms = max(0, latency_ms - 5)
                    trim = int(latency_ms / 1000.0 * sample_rate)
                    engine.mixer.set_trim("recording", trim)
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo(f"  Latency: {latency_ms:.0f} ms")

                elif key == "r":
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo("  Replaying...")

                elif key == "s":
                    config.latency_compensation_ms = latency_ms
                    config.save()
                    click.echo(f"\n  Saved latency_compensation_ms = {latency_ms:.0f} ms to {DEFAULT_CONFIG_PATH}")
                    return

                elif key == "q":
                    click.echo("\n  Quit without saving.")
                    return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _query_inspiration_tracks() -> tuple[list[dict], StudioConfig]:
    """Query inspiration tracks from radioserver. Returns (tracks, config)."""
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    config = StudioConfig.load()

    click.echo("Querying inspiration tracks...")
    try:
        tracks = query_inspiration_tracks(project, config)
    except InspirationError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    return tracks, config


@main.command()
def record_inspiration() -> None:
    """Pick inspiration tracks to add to the setlist for recording."""
    tracks, config = _query_inspiration_tracks()

    click.echo(f"\n{len(tracks)} tracks:\n")
    for i, t in enumerate(tracks):
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        album = t.get("album", "")
        year = t.get("year", "")
        dur = format_duration(t.get("duration") or 0)
        year_str = f" ({year})" if year else ""
        album_str = f" [{album}]" if album else ""
        click.echo(f"  {i + 1:3}. {artist} - {title}{album_str}{year_str}  {dur}")

    click.echo()
    selection = click.prompt("Select tracks (comma-separated numbers, e.g. 1,3,7)")
    indices = []
    for s in selection.split(","):
        s = s.strip()
        if s.isdigit():
            idx = int(s) - 1
            if 0 <= idx < len(tracks):
                indices.append(idx)
            else:
                click.echo(f"  Skipping invalid number: {int(s)}")

    if not indices:
        click.echo("No valid tracks selected.")
        raise SystemExit(1)

    cwd = Path.cwd()
    project = Project.open(cwd)

    added = 0
    for idx in indices:
        t = tracks[idx]
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        year = t.get("year", "")
        track_id = t["id"]
        fmt = t.get("format", "flac") or "flac"
        duration = t.get("duration") or 0

        year_str = f" ({year})" if year else ""
        name = f"{artist} - {title}{year_str}"
        backing_file = f"inspiration_{track_id}.{fmt}"

        entry = TrackEntry(
            name=name,
            backing_track=backing_file,
            duration_seconds=duration,
            inspiration_track_id=track_id,
        )
        project.setlist.add_track(entry)
        click.echo(f"  + {name}")
        added += 1

    project.save_setlist()
    click.echo(f"\nAdded {added} tracks to setlist.")


@main.command()
def list_inspirations() -> None:
    """List tracks matching the current project's inspiration filters."""
    tracks, config = _query_inspiration_tracks()
    click.echo(f"\n{len(tracks)} tracks:\n")
    for i, t in enumerate(tracks):
        artist = t.get("artist", "Unknown")
        title = t.get("title", "Unknown")
        album = t.get("album", "")
        year = t.get("year", "")
        dur = format_duration(t.get("duration") or 0)
        year_str = f" ({year})" if year else ""
        album_str = f" [{album}]" if album else ""
        click.echo(f"  {i + 1:3}. {artist} - {title}{album_str}{year_str}  {dur}")


@main.command()
@click.option("--verbose", "-v", is_flag=True, default=False, help="Print debug info.")
@click.argument("instrument", required=False, default=None)
def inspiration(instrument: str | None, verbose: bool) -> None:
    """Play tracks from your music library for inspiration.

    Pass an INSTRUMENT name to start a recording session: each track is
    recorded automatically and added to the setlist with your take.
    """
    import tempfile
    import urllib.request
    import urllib.error

    def vlog(msg: str) -> None:
        if verbose:
            click.echo(msg)

    tracks, config = _query_inspiration_tracks()
    server = config.inspiration_server.rstrip("/")

    import sounddevice as sd
    from .audio.mixer import Mixer

    out_dev = _resolve_device(sd, config.output_device, "output")
    out_info = sd.query_devices(out_dev, "output")
    out_channels = min(config.output_channels, out_info["max_output_channels"])

    is_recording = instrument is not None
    engine = None
    project = None

    if is_recording:
        from .audio.engine import AudioEngine

        cwd = Path.cwd()
        if not (cwd / "setlist.json").exists():
            click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
            raise SystemExit(1)
        project = Project.open(cwd)
        ensure_dir(project.completed_takes_dir)

        inst_obj = config.get_instrument(instrument)
        if inst_obj is None:
            if not config.input_labels:
                click.echo("Error: No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
                raise SystemExit(1)
            click.echo(f"Instrument '{instrument}' not found in config. Let's set it up.\n")
            click.echo("Available inputs:")
            for i, il in enumerate(config.input_labels):
                click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
            choice = click.prompt("  Input number", type=int, default=1)
            if 1 <= choice <= len(config.input_labels):
                input_label_name = config.input_labels[choice - 1].label
            else:
                click.echo("  Invalid choice, using first input.")
                input_label_name = config.input_labels[0].label
            full_name = click.prompt("  Full name (manufacturer & model)", default="", show_default=False)
            musician = click.prompt("  Musician name", default=config.studio_musician, show_default=bool(config.studio_musician))
            inst_obj = Instrument(
                name=instrument, input_label=input_label_name,
                full_name=full_name, musician=musician,
            )
            config.instruments.append(inst_obj)
            config.save()
            click.echo(f"  Saved '{instrument}' to config.\n")

        input_info = config.resolve_input(inst_obj.input_label)
        if input_info is None:
            click.echo(f"Error: Input label '{inst_obj.input_label}' not found in config.", err=True)
            raise SystemExit(1)
        in_dev = _resolve_device(sd, input_info.device, "input")
        if in_dev is None:
            click.echo(f"Error: Input device '{input_info.device}' not found.", err=True)
            raise SystemExit(1)
        input_channel_index = input_info.channel - 1
        input_channels = max(input_info.channel, 1)
        in_info = sd.query_devices(in_dev, "input")
        if input_channels > in_info["max_input_channels"]:
            click.echo(
                f"Error: Instrument '{inst_obj.name}' needs input channel {input_channels} "
                f"but device only has {in_info['max_input_channels']} channels.",
                err=True,
            )
            raise SystemExit(1)
        output_channels_e = min(config.output_channels, out_info["max_output_channels"])
        engine = AudioEngine(
            sample_rate=config.sample_rate,
            buffer_size=config.buffer_size,
            input_device=in_dev,
            output_device=out_dev,
            input_channels=input_channels,
            output_channels=max(1, output_channels_e),
            monitor_channel=input_channel_index,
        )
        engine.start()
        playback_sr = config.sample_rate
        click.echo(f"=== Inspiration Recording Session: {instrument} ===")
        click.echo(f"Project: {project.name}")
    else:
        playback_sr = int(out_info["default_samplerate"])

    click.echo(f"Found {len(tracks)} tracks. Playing radio-style.")
    if is_recording:
        click.echo("Controls: [space] pause/play  [s]kip (discard take)  [l]ower volume  [u]p volume  [q]uit\n")
    else:
        click.echo("Controls: [space] pause/play  [s]kip  [l]ower volume  [u]p volume  [q]uit\n")
    vlog(f"[audio] device={out_info['name']!r}  channels={out_channels}  sample_rate={playback_sr} Hz")

    tmpdir = tempfile.mkdtemp(prefix="takeloom_inspiration_")
    vlog(f"[audio] tmp dir: {tmpdir}")
    volume = config.inspiration_volume

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    from wakepy import keep as _keep
    import threading as _threading
    _wake_ctx = _keep.running()
    _wakelock = _wake_ctx.__enter__()
    vlog(f"[system] sleep inhibit: {_wakelock!r}  (type={type(_wakelock).__name__})")

    def _prefetch(track_info):
        """Start downloading track_info in a background thread.
        Returns a callable that blocks until done and returns (Path, error_str)."""
        track_id = track_info["id"]
        fmt = track_info.get("format", "flac") or "flac"
        title = track_info.get("title", "Unknown")
        artist = track_info.get("artist", "Unknown")
        tmp_path = Path(tmpdir) / f"track_{track_id}.{fmt}"
        result = [None, None]  # [path, error]

        def _run():
            url = f"{server}/library/api/tracks/{track_id}/download/"
            dl_req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
            )
            try:
                with urllib.request.urlopen(dl_req, timeout=30) as resp:
                    data = resp.read()
                    tmp_path.write_bytes(data)
                    result[0] = tmp_path
                vlog(f"  [prefetch] done: {artist} - {title} ({len(data) // 1024} KB)")
            except Exception as e:
                import traceback
                result[1] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                click.echo(f"  [prefetch] failed: {artist} - {title}: {type(e).__name__}: {e}")

        t = _threading.Thread(target=_run, daemon=True)
        t.start()

        def _wait():
            while t.is_alive():
                t.join(timeout=0.2)
            return result[0], result[1]

        return _wait

    _now_playing = [None]  # mutable slot shared with status thread
    _stop_status = _threading.Event()

    def _status_printer():
        while not _stop_status.wait(timeout=30):
            info = _now_playing[0]
            if info:
                vlog(f"  [now playing] {info['artist']} - {info['title']}"
                     + (f" ({info['year']})" if info.get('year') else ""))

    _status_thread = _threading.Thread(target=_status_printer, daemon=True)
    _status_thread.start()

    with _recording_context(project if is_recording else None, config) as (streamdeck, sd_keys):
        if streamdeck.connected:
            streamdeck.use_inspiration_layout(recording=is_recording)
        try:
            tty.setcbreak(fd)

            auto_play_next = False  # set True when a track ends naturally; auto-starts next track

            while True:
                # Kick off download of the first track before the loop starts
                vlog(f"[prefetch] starting download of track 1/{len(tracks)}")
                _wait_download = _prefetch(tracks[0])

                for i, track_info in enumerate(tracks):
                    title = track_info.get("title", "Unknown")
                    artist = track_info.get("artist", "Unknown")
                    album = track_info.get("album", "")
                    year = track_info.get("year") or ""
                    dur = track_info.get("duration") or 0
                    dur_str = format_duration(dur)
                    year_str = f" ({year})" if year else ""
                    _now_playing[0] = {"artist": artist, "title": title, "year": year}
                    click.echo(f"[{i + 1}/{len(tracks)}] {artist} - {title}{year_str}")
                    if album:
                        click.echo(f"         {album} ({dur_str})")
                    else:
                        click.echo(f"         ({dur_str})")

                    # Wait for this track's download to finish
                    vlog(f"  [download] waiting for track {i + 1} (id={track_info['id']})...")
                    tmp_path, dl_error = _wait_download()
                    if dl_error:
                        click.echo(f"  [download] FAILED for track {i + 1} (id={track_info['id']}):\n{dl_error}")
                        if i + 1 < len(tracks):
                            vlog(f"  [prefetch] starting download of track {i + 2}/{len(tracks)}")
                            _wait_download = _prefetch(tracks[i + 1])
                        continue
                    vlog(f"  [download] OK — {tmp_path} ({tmp_path.stat().st_size // 1024} KB)")

                    # Play via Mixer — apply ReplayGain if available
                    rg_gain = track_info.get("replaygain_track_gain")
                    rg_linear = 10 ** (rg_gain / 20.0) if rg_gain is not None else 1.0
                    vlog(f"  [mixer] replaygain={rg_gain} ({rg_linear:.3f}x)  volume={volume:.2f}")
                    skip = False

                    if is_recording:
                        track_entry = find_or_add_inspiration_track(project, track_info)
                        take_num = next_take_number(project.completed_takes_dir, track_entry.name, instrument)
                        fname = take_filename(track_entry.name, instrument, take_num, "flac")
                        rec_path = project.completed_takes_dir / fname
                        engine.mixer.clear()
                        engine.mixer.add_source("inspiration", tmp_path, volume=volume * rg_linear)
                        engine.mixer.reset()
                        if auto_play_next:
                            engine.start_recording(rec_path)
                            engine.mixer.set_playing(True)
                            recording_active = True
                            auto_play_next = False
                            click.echo(f"  [rec] Auto-recording take {take_num}: {fname}")
                            streamdeck.update_inspiration(True, f"{artist} - {title}")
                        else:
                            engine.mixer.set_playing(False)
                            recording_active = False
                            click.echo(f"  Ready — press [space] to start recording")
                            streamdeck.update_inspiration(False, f"{artist} - {title}")
                        if i + 1 < len(tracks):
                            vlog(f"  [prefetch] starting download of track {i + 2}/{len(tracks)}")
                            _wait_download = _prefetch(tracks[i + 1])
                        while not engine.mixer.is_finished and not skip:
                            key = None
                            try:
                                key = sd_keys.get_nowait()
                            except queue.Empty:
                                if select.select([sys.stdin], [], [], 0.2)[0]:
                                    key = sys.stdin.read(1).lower()
                            if key == "q":
                                engine.mixer.set_playing(False)
                                if recording_active:
                                    engine.stop_recording()
                                if rec_path.exists():
                                    rec_path.unlink()
                                click.echo("\nQuitting inspiration mode.")
                                return
                            elif key == "s":
                                click.echo("  >> Skip (take discarded)")
                                skip = True
                                engine.mixer.set_playing(False)
                            elif key == " ":
                                if engine.mixer.is_playing:
                                    engine.mixer.set_playing(False)
                                    click.echo("  || Paused")
                                else:
                                    if not recording_active:
                                        engine.start_recording(rec_path)
                                        click.echo(f"  [rec] Recording take {take_num}: {fname}")
                                        recording_active = True
                                    engine.mixer.set_playing(True)
                                    click.echo("  >> Playing")
                                streamdeck.update_inspiration(engine.mixer.is_playing, f"{artist} - {title}")
                            elif key == "b":
                                # Restart: discard current take, reset to beginning, wait for play
                                engine.mixer.set_playing(False)
                                if recording_active:
                                    engine.stop_recording()
                                    if rec_path.exists():
                                        rec_path.unlink()
                                    recording_active = False
                                take_num = next_take_number(project.completed_takes_dir, track_entry.name, instrument)
                                fname = take_filename(track_entry.name, instrument, take_num, "flac")
                                rec_path = project.completed_takes_dir / fname
                                engine.mixer.reset()
                                click.echo("  >> Restarted — press [space] to record again")
                                streamdeck.update_inspiration(False, f"{artist} - {title}")
                            elif key == "l":
                                volume = max(0.0, volume - 0.1)
                                engine.mixer.set_volume("inspiration", volume * rg_linear)
                                config.inspiration_volume = volume
                                config.save()
                                click.echo(f"  Volume: {int(volume * 100)}%")
                            elif key == "u":
                                volume = min(2.0, volume + 0.1)
                                engine.mixer.set_volume("inspiration", volume * rg_linear)
                                config.inspiration_volume = volume
                                config.save()
                                click.echo(f"  Volume: {int(volume * 100)}%")
                        if recording_active:
                            engine.stop_recording()
                        if not skip and recording_active:
                            from .project import TakeInfo
                            take = TakeInfo(instrument=instrument, take_number=take_num, filename=fname)
                            track_entry.set_preferred_take(instrument, take)
                            project.save_setlist()
                            click.echo(f"  [rec] Saved take: {fname}")
                            auto_play_next = True
                        else:
                            if rec_path.exists():
                                rec_path.unlink()
                            auto_play_next = True  # skipped — keep music going
                        if tmp_path.exists():
                            tmp_path.unlink()
                    else:
                        mixer = Mixer(playback_sr)
                        mixer.add_source("inspiration", tmp_path, volume=volume * rg_linear)
                        mixer.set_playing(True)

                        def callback(outdata, frames, time_info, status):
                            mix = mixer.read(frames)
                            if out_channels == 2:
                                outdata[:] = mix
                            else:
                                outdata[:, 0] = mix[:, 0]
                            if mixer.is_finished:
                                raise sd.CallbackStop

                        import time as _time
                        _stream = None
                        for _attempt in range(5):
                            _stream = None
                            vlog(f"  [stream] opening OutputStream (attempt {_attempt + 1}/5)...")
                            try:
                                _stream = sd.OutputStream(
                                    samplerate=playback_sr,
                                    device=out_dev,
                                    channels=max(1, out_channels),
                                    dtype="float32",
                                    callback=callback,
                                )
                                vlog(f"  [stream] calling start()...")
                                _stream.start()
                                vlog(f"  [stream] started OK")
                                break
                            except sd.PortAudioError as _pa_err:
                                import traceback as _tb
                                click.echo(f"  [audio error] attempt {_attempt + 1}/5: {_pa_err}")
                                if verbose:
                                    click.echo(f"    cpu={_stream.cpu_load if _stream else 'n/a'}\n{_tb.format_exc()}")
                                if _stream is not None:
                                    try:
                                        _stream.close()
                                    except Exception as _ce:
                                        vlog(f"  [stream] close() also failed: {_ce}")
                                    _stream = None
                                if _attempt == 4:
                                    click.echo(f"  [audio error] all 5 attempts failed — skipping track")
                                    break
                                vlog(f"  [stream] waiting 2s before retry...")
                                _time.sleep(2.0)
                                mixer.set_playing(False)
                                mixer = Mixer(playback_sr)
                                mixer.add_source("inspiration", tmp_path, volume=volume * rg_linear)
                                mixer.set_playing(True)
                        if _stream is None:
                            if tmp_path.exists():
                                tmp_path.unlink()
                            if i + 1 < len(tracks):
                                vlog(f"  [prefetch] starting download of track {i + 2}/{len(tracks)}")
                                _wait_download = _prefetch(tracks[i + 1])
                            continue
                        # Stream started — prefetch next track while this one plays
                        if i + 1 < len(tracks):
                            vlog(f"  [prefetch] starting download of track {i + 2}/{len(tracks)}")
                            _wait_download = _prefetch(tracks[i + 1])
                        streamdeck.update_inspiration(True, f"{artist} - {title}")
                        try:
                            while not mixer.is_finished and not skip:
                                key = None
                                try:
                                    key = sd_keys.get_nowait()
                                except queue.Empty:
                                    if select.select([sys.stdin], [], [], 0.2)[0]:
                                        key = sys.stdin.read(1).lower()
                                if key == "q":
                                    click.echo("\nQuitting inspiration mode.")
                                    return
                                elif key == "s":
                                    click.echo("  >> Skip")
                                    skip = True
                                    mixer.set_playing(False)
                                    break
                                elif key == " ":
                                    if mixer.is_playing:
                                        mixer.set_playing(False)
                                        click.echo("  || Paused")
                                    else:
                                        mixer.set_playing(True)
                                        click.echo("  >> Playing")
                                    streamdeck.update_inspiration(mixer.is_playing, f"{artist} - {title}")
                                elif key == "l":
                                    volume = max(0.0, volume - 0.1)
                                    mixer.set_volume("inspiration", volume * rg_linear)
                                    config.inspiration_volume = volume
                                    config.save()
                                    click.echo(f"  Volume: {int(volume * 100)}%")
                                elif key == "u":
                                    volume = min(2.0, volume + 0.1)
                                    mixer.set_volume("inspiration", volume * rg_linear)
                                    config.inspiration_volume = volume
                                    config.save()
                                    click.echo(f"  Volume: {int(volume * 100)}%")
                        finally:
                            vlog(f"  [stream] stopping and closing...")
                            try:
                                _stream.stop()
                                _stream.close()
                            except Exception as _se:
                                vlog(f"  [stream] stop/close error: {_se}")
                            _time.sleep(0.3)

                        if tmp_path.exists():
                            tmp_path.unlink()

                # Batch exhausted — fetch next batch from server (obeys playlist settings)
                vlog("[playlist] batch complete, fetching next batch...")
                new_tracks, _ = _query_inspiration_tracks()
                if not new_tracks:
                    click.echo("\nNo more tracks available.")
                    break
                tracks = new_tracks
                click.echo(f"\n[playlist] fetched {len(tracks)} more tracks.")

        except KeyboardInterrupt:
            click.echo("\nInterrupted.")
        except Exception as _top_err:  # noqa: F841
            import traceback as _tb
            click.echo(f"\n[FATAL] Unhandled exception:\n{_tb.format_exc()}", err=True)
            raise
        finally:
            _stop_status.set()
            # Must run in the same thread where __enter__ was called (wakepy is not thread-safe).
            # Use SIGALRM to enforce a 3-second timeout so a hung wakepy release can't freeze exit.
            import signal as _signal
            def _wake_timeout(signum, frame): raise TimeoutError
            _old_handler = _signal.signal(_signal.SIGALRM, _wake_timeout)
            _signal.alarm(3)
            try:
                _wake_ctx.__exit__(None, None, None)
            except Exception:
                pass
            finally:
                _signal.alarm(0)
                _signal.signal(_signal.SIGALRM, _old_handler)
            # TCSANOW applies immediately; TCSADRAIN waits for output to drain and
            # can block indefinitely if the screensaver has locked the terminal.
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
            if engine is not None:
                engine.stop()
            if project is not None:
                project.save_setlist()
    # _recording_context __exit__: StreamDeck disconnect + backup sync


if __name__ == "__main__":
    main()
