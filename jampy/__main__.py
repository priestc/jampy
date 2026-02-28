"""Entry point for Jam.py CLI."""

from __future__ import annotations

import sys
import select
import termios
import tty
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
from .audio.formats import get_duration
from .session import Session, SessionState
from .utils import format_duration, take_filename, next_take_number


@click.group()
def main() -> None:
    """Jam.py - Music Recording Session Manager."""


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

    existing.sample_rate = int(sample_rate)
    existing.buffer_size = int(buffer_size)
    existing.output_device = output_device_name
    existing.output_channels = output_channels
    existing.latency_compensation_ms = latency_compensation_ms
    existing.input_labels = input_labels

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
        click.echo("No inputs configured. Run 'jampy setup-recording-devices' first.", err=True)
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

    # Scan for audio files
    audio_exts = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
    found_files = {
        f.name for f in backing_dir.iterdir()
        if f.is_file() and f.suffix.lower() in audio_exts
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


def _resolve_device(sd: object, device_name: str, kind: str) -> int | None:
    """Resolve a device name to its index. Returns None if not found."""
    if not device_name:
        return None
    devices = sd.query_devices()  # type: ignore[union-attr]
    for i, d in enumerate(devices):
        if d["name"] == device_name:
            return i
    # Partial match fallback
    for i, d in enumerate(devices):
        if device_name.lower() in d["name"].lower():
            return i
    return None


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
    """Start a recording session for INSTRUMENT."""
    # Load config and validate instrument
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'jampy setup-recording-devices' first.", err=True)
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

    # Check we're in a project directory
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
        click.echo("Error: Setlist is empty. Run 'jampy update-setlist' first.", err=True)
        raise SystemExit(1)

    # Import AudioEngine here to avoid top-level sounddevice import
    # (allows non-audio commands to work without PortAudio)
    from .audio.engine import AudioEngine
    import sounddevice as sd

    # Resolve output device name to index
    out_dev = _resolve_device(sd, config.output_device, "output")

    input_info = config.resolve_input(inst.input_label)
    if input_info is None:
        click.echo(f"Error: Input label '{inst.input_label}' not found in config.", err=True)
        raise SystemExit(1)
    in_dev = _resolve_device(sd, input_info.device, "input")
    if in_dev is None:
        click.echo(f"Error: Input device '{input_info.device}' not found.", err=True)
        raise SystemExit(1)
    input_channel_index = input_info.channel - 1
    input_channels = max(input_info.channel, 1)

    # Query actual device capabilities to avoid channel count mismatches
    in_info = sd.query_devices(in_dev, "input")
    out_info = sd.query_devices(out_dev, "output")
    max_in = in_info["max_input_channels"]
    output_channels = min(config.output_channels, out_info["max_output_channels"])

    if input_channels > max_in:
        click.echo(
            f"Error: Instrument '{inst.name}' needs input channel {input_channels} "
            f"but device only has {max_in} channels.",
            err=True,
        )
        raise SystemExit(1)

    engine = AudioEngine(
        sample_rate=config.sample_rate,
        buffer_size=config.buffer_size,
        input_device=in_dev,
        output_device=out_dev,
        input_channels=input_channels,
        output_channels=max(1, output_channels),
        monitor_channel=input_channel_index,
    )

    # Resolve musician name: instrument → studio default → prompt
    musician = inst.musician or config.studio_musician
    if not musician:
        musician = click.prompt("Musician name")

    session = Session(project=project, instrument=inst.name)
    session.musician = musician
    session.instrument_full_name = inst.full_name
    session.studio_name = config.studio_name
    session.studio_location = config.studio_location
    # Compute latency compensation in frames for take playback
    session._latency_trim_frames = int(
        config.latency_compensation_ms / 1000.0 * config.sample_rate
    )
    session.start()

    # Skip tracks that already have a preferred take for this instrument
    tracks = project.setlist.tracks
    for i, track in enumerate(tracks):
        if track.get_take_for_instrument(inst.name) is None:
            session.current_track_index = i
            break
    else:
        click.echo(f"All tracks already have a take for '{inst.name}'.")
        click.echo("Starting from the first track anyway.\n")
        session.current_track_index = 0

    click.echo(f"=== Recording Session: {project.name} / {inst.name} ===")
    click.echo(f"Tracks: {len(project.setlist.tracks)}")
    click.echo("Controls: [r]ecord  [b]ack to start  [e]nd song  [n]ext track")
    click.echo("          [l]ower volume  [u]p volume  [[]lower takes  []]raise takes")
    click.echo("          [q]uit\n")

    # Start the audio stream and continuous session recording
    engine.start()
    session_flac = session.session_dir / "session.flac" if session.session_dir else None
    if session_flac:
        engine.start_session_recording(session_flac)
    try:
        _run_session_loop(session, engine)
    finally:
        engine.stop()

    if project.setlist.backup_server:
        from .sync import sync_up
        sync_up(project.path, project.setlist.backup_server)


def _load_backing_track(session: Session, engine: AudioEngine) -> None:
    """Load the current track's backing file and preferred takes into the mixer."""
    track = session.current_track
    if not track:
        return
    engine.mixer.clear()
    backing_path = session.project.backing_tracks_dir / track.backing_track
    if backing_path.exists():
        engine.mixer.add_source("backing", backing_path, volume=track.volume / 100.0)

    # Load preferred takes from other instruments, trimming for latency compensation
    trim = getattr(session, '_latency_trim_frames', 0)
    for inst_name, take_info in track.preferred_takes.items():
        if inst_name.lower() == session.instrument.lower():
            continue  # skip the instrument we're currently recording
        take_path = session.project.completed_takes_dir / take_info.filename
        if take_path.exists():
            effective_vol = take_info.volume * (track.takes_volume / 100.0)
            engine.mixer.add_source(
                f"take:{inst_name}", take_path, volume=effective_vol,
                trim_frames=trim,
            )


def _run_session_loop(session: Session, engine: AudioEngine) -> None:
    """Interactive single-key recording loop."""
    # Load the first backing track into the mixer
    _load_backing_track(session, engine)

    _show_status(session)

    # Set terminal to raw mode for single-key input
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while session.state != SessionState.ENDED:
            # Check if backing track finished naturally
            if (session.state == SessionState.PLAYING
                    and engine.mixer.is_finished
                    and not engine.mixer.is_playing):
                engine.stop_recording()
                _save_preferred_take(session)
                session.song_end(engine.mixer.position)
                _show_status(session)
                continue

            # Wait for keypress (0.2s timeout to keep polling responsive)
            if select.select([sys.stdin], [], [], 0.2)[0]:
                key = sys.stdin.read(1).lower()
                _handle_key(session, engine, key)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Session ended — save volume changes back to setlist
    session.project.save_setlist()

    log_path = session.save_log()
    click.echo(f"\nSession ended.")
    if log_path:
        click.echo(f"Log saved to {log_path}")

    # Print completed takes summary
    completed = getattr(session, '_completed_takes', [])
    click.echo(f"\nCompleted takes: {len(completed)}")
    for name in completed:
        click.echo(f"  - {name}")


def _save_preferred_take(session: Session) -> None:
    """Save the current take as the preferred take for this track/instrument."""
    track = session.current_track
    if not track or not hasattr(session, '_current_take_info'):
        return
    take_info = session._current_take_info
    if take_info:
        track.set_preferred_take(session.instrument, take_info)
        if not hasattr(session, '_completed_takes'):
            session._completed_takes = []
        session._completed_takes.append(track.name)
        session._current_take_info = None


def _delete_partial_take(session: Session) -> None:
    """Delete the current in-progress take file (incomplete recording)."""
    if hasattr(session, '_current_take_info') and session._current_take_info:
        partial = session.project.completed_takes_dir / session._current_take_info.filename
        if partial.exists():
            partial.unlink()
        session._current_take_info = None


def _start_recording(session: Session, engine: AudioEngine) -> None:
    """Start recording and playing the current track."""
    track = session.current_track
    if track:
        take_num = next_take_number(
            session.project.completed_takes_dir, track.name, session.instrument
        )
        fname = take_filename(track.name, session.instrument, take_num, "flac")
        rec_path = session.project.completed_takes_dir / fname
        engine.start_recording(rec_path)

        # Track the current take so we can save it as preferred when the song ends
        from .project import TakeInfo
        session._current_take_info = TakeInfo(
            instrument=session.instrument,
            take_number=take_num,
            filename=fname,
        )

    engine.mixer.reset()
    engine.mixer.set_playing(True)
    session.start_recording(engine.mixer.position)


def _handle_key(session: Session, engine: AudioEngine, key: str) -> None:
    """Process a single keypress."""
    if key == "q":
        engine.stop_recording()
        _delete_partial_take(session)
        engine.mixer.set_playing(False)
        session.end_session()
        return

    if key == "r" and session.state == SessionState.WAITING:
        _start_recording(session, engine)
        _show_status(session)

    elif key == "b" and session.state == SessionState.PLAYING:
        # Restart from beginning — stop current take, delete partial file, start new take
        engine.stop_recording()
        _delete_partial_take(session)
        engine.mixer.reset()

        track = session.current_track
        if track:
            take_num = next_take_number(
                session.project.completed_takes_dir, track.name, session.instrument
            )
            fname = take_filename(track.name, session.instrument, take_num, "flac")
            rec_path = session.project.completed_takes_dir / fname
            engine.start_recording(rec_path)

            from .project import TakeInfo
            session._current_take_info = TakeInfo(
                instrument=session.instrument,
                take_number=take_num,
                filename=fname,
            )

        session.back_to_start(engine.mixer.position)
        click.echo("  >> Back to start")

    elif key == "e" and session.state == SessionState.PLAYING:
        # Early end — mistake take, not saved as preferred
        engine.stop_recording()
        _delete_partial_take(session)
        engine.mixer.set_playing(False)
        session.song_end(engine.mixer.position)
        click.echo("  (take discarded — song ended early)")
        _show_status(session)

    elif key == "l":
        track = session.current_track
        if track:
            track.volume = max(0, track.volume - 5)
            engine.mixer.set_volume("backing", track.volume / 100.0)
            click.echo(f"  Volume: {track.volume}%")

    elif key == "u":
        track = session.current_track
        if track:
            track.volume = track.volume + 5
            engine.mixer.set_volume("backing", track.volume / 100.0)
            click.echo(f"  Volume: {track.volume}%")

    elif key == "[":
        track = session.current_track
        if track:
            track.takes_volume = max(0, track.takes_volume - 5)
            for src in engine.mixer.sources:
                if src.name.startswith("take:"):
                    inst_name = src.name[5:]
                    take_info = track.preferred_takes.get(inst_name)
                    base_vol = take_info.volume if take_info else 1.0
                    engine.mixer.set_volume(src.name, base_vol * (track.takes_volume / 100.0))
            click.echo(f"  Takes volume: {track.takes_volume}%")

    elif key == "]":
        track = session.current_track
        if track:
            track.takes_volume = track.takes_volume + 5
            for src in engine.mixer.sources:
                if src.name.startswith("take:"):
                    inst_name = src.name[5:]
                    take_info = track.preferred_takes.get(inst_name)
                    base_vol = take_info.volume if take_info else 1.0
                    engine.mixer.set_volume(src.name, base_vol * (track.takes_volume / 100.0))
            click.echo(f"  Takes volume: {track.takes_volume}%")

    elif key == "n" and session.state == SessionState.BETWEEN_TRACKS:
        # Advance to next track without a take for this instrument
        tracks = session.project.setlist.tracks
        found = False
        while session.current_track_index < len(tracks) - 1:
            session.next_track()
            if session.state == SessionState.ENDED:
                break
            track = session.current_track
            if track and track.get_take_for_instrument(session.instrument) is None:
                found = True
                break
            click.echo(f"  Skipping '{track.name}' (already has a take)")
        if not found and session.state != SessionState.ENDED:
            session.end_session()
        if session.state == SessionState.WAITING:
            _load_backing_track(session, engine)
            _start_recording(session, engine)
        _show_status(session)


def _show_status(session: Session) -> None:
    """Print current session status."""
    track = session.current_track
    state = session.state.name
    idx = session.current_track_index + 1
    total = len(session.project.setlist.tracks)

    if track:
        dur = format_duration(track.duration_seconds)
        takes_vol = f" takes:{track.takes_volume}%" if track.preferred_takes else ""
        click.echo(f"[{idx}/{total}] {track.name} ({dur}) vol:{track.volume}%{takes_vol} — {state}")
    else:
        click.echo(f"[{idx}/{total}] — {state}")

    if session.state == SessionState.WAITING:
        click.echo("  Press [r] to record")
    elif session.state == SessionState.PLAYING:
        click.echo("  Recording... [b]ack [e]nd song [q]uit")
    elif session.state == SessionState.BETWEEN_TRACKS:
        if session.has_more_tracks:
            click.echo("  Press [n] for next track, [q] to quit")
        else:
            click.echo("  Last track done! Press [q] to finish")


@main.command()
@click.argument("instrument")
def measure_latency(instrument: str) -> None:
    """Measure and calibrate latency compensation by ear for INSTRUMENT."""
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'jampy setup-recording-devices' first.", err=True)
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
    tmp_recording = Path(tempfile.mktemp(suffix=".flac", prefix="jampy_latency_"))

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
    import json
    import urllib.request
    import urllib.error

    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    if not project.setlist.inspiration:
        click.echo("Error: No inspiration filters in setlist.json.", err=True)
        click.echo('Add an "inspiration" key with filter sets, e.g.:')
        click.echo('  "inspiration": [{"genre": "Rock"}, {"artist": "Miles Davis"}]')
        raise SystemExit(1)

    config = StudioConfig.load()
    if not config.inspiration_server or not config.inspiration_api_key:
        click.echo("Error: inspiration_server and inspiration_api_key must be set.", err=True)
        click.echo("Run 'jampy setup-studio' to configure them.")
        raise SystemExit(1)

    server = config.inspiration_server.rstrip("/")

    click.echo("Querying inspiration tracks...")
    payload = json.dumps({"filters": project.setlist.inspiration}).encode()
    req = urllib.request.Request(
        f"{server}/library/api/tracks/",
        data=payload,
        headers={"Authorization": f"Bearer {config.inspiration_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        click.echo(f"Error contacting server: {e}", err=True)
        raise SystemExit(1)

    tracks = data.get("tracks", [])
    if not tracks:
        click.echo("No tracks matched the inspiration filters.")
        raise SystemExit(1)

    return tracks, config


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
def inspiration() -> None:
    """Play tracks from your music library for inspiration."""
    import tempfile
    import urllib.request
    import urllib.error

    tracks, config = _query_inspiration_tracks()
    server = config.inspiration_server.rstrip("/")
    click.echo(f"Found {len(tracks)} tracks. Playing radio-style.")
    click.echo("Controls: [s]kip  [l]ower volume  [u]p volume  [q]uit\n")

    import sounddevice as sd
    from .audio.mixer import Mixer

    out_dev = _resolve_device(sd, config.output_device, "output")
    out_info = sd.query_devices(out_dev, "output")
    out_channels = min(config.output_channels, out_info["max_output_channels"])

    tmpdir = tempfile.mkdtemp(prefix="jampy_inspiration_")
    volume = config.inspiration_volume

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setcbreak(fd)

        for i, track_info in enumerate(tracks):
            title = track_info.get("title", "Unknown")
            artist = track_info.get("artist", "Unknown")
            album = track_info.get("album", "")
            year = track_info.get("year") or ""
            dur = track_info.get("duration") or 0
            dur_str = format_duration(dur)
            year_str = f" ({year})" if year else ""
            click.echo(f"[{i + 1}/{len(tracks)}] {artist} - {title}{year_str}")
            if album:
                click.echo(f"         {album} ({dur_str})")
            else:
                click.echo(f"         ({dur_str})")

            # Download track
            track_id = track_info["id"]
            fmt = track_info.get("format", "flac") or "flac"
            tmp_path = Path(tmpdir) / f"track_{track_id}.{fmt}"
            dl_req = urllib.request.Request(
                f"{server}/library/api/tracks/{track_id}/download/",
                headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
            )
            try:
                with urllib.request.urlopen(dl_req) as resp:
                    tmp_path.write_bytes(resp.read())
            except urllib.error.URLError as e:
                click.echo(f"  Download failed: {e}")
                continue

            # Play via Mixer — apply ReplayGain if available
            rg_gain = track_info.get("replaygain_track_gain")
            rg_linear = 10 ** (rg_gain / 20.0) if rg_gain is not None else 1.0
            mixer = Mixer(config.sample_rate)
            mixer.add_source("inspiration", tmp_path, volume=volume * rg_linear)
            mixer.set_playing(True)

            skip = False

            def callback(outdata, frames, time_info, status):
                mix = mixer.read(frames)
                if out_channels == 2:
                    outdata[:] = mix
                else:
                    outdata[:, 0] = mix[:, 0]
                if mixer.is_finished:
                    raise sd.CallbackStop

            with sd.OutputStream(
                samplerate=config.sample_rate,
                blocksize=config.buffer_size,
                device=out_dev,
                channels=max(1, out_channels),
                dtype="float32",
                callback=callback,
            ):
                while mixer.is_playing and not mixer.is_finished:
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

            # Clean up downloaded file
            if tmp_path.exists():
                tmp_path.unlink()

        click.echo("\nAll tracks played.")

    except KeyboardInterrupt:
        click.echo("\nInterrupted.")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        # Clean up temp directory
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
