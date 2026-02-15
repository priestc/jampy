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
def studio_setup() -> None:
    """Interactive wizard to configure studio audio settings."""
    click.echo("=== Studio Setup ===\n")

    # Load existing config for defaults
    existing = StudioConfig.load()

    # Studio info
    studio_name = click.prompt("Studio name", default=existing.studio_name, show_default=bool(existing.studio_name))
    studio_location = click.prompt("Studio location", default=existing.studio_location, show_default=bool(existing.studio_location))
    studio_musician = click.prompt("Studio musician (default performer)", default=existing.studio_musician, show_default=bool(existing.studio_musician))
    click.echo()

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
    output_device = click.prompt("Output device index", type=int, default=existing.output_device or 0)
    max_out = devices[output_device]["max_output_channels"] if devices else 2
    output_channels = click.prompt("Output channels", type=int, default=existing.output_channels if existing.output_channels <= max_out else min(2, max_out))

    # Instruments — start with any previously configured instruments
    instruments: list[Instrument] = list(existing.instruments)
    click.echo("\n--- Instrument Setup ---")
    if instruments:
        click.echo("Existing instruments:")
        for inst in instruments:
            click.echo(f"  - {inst.name} (device={inst.device}, input={inst.input_number})")
        click.echo()
    while True:
        if not click.confirm("Add an instrument?", default=bool(not instruments)):
            break
        name = click.prompt("  Instrument name")
        desktop_audio = click.confirm("  Capture from desktop audio?", default=False)
        if desktop_audio:
            device = ""
            input_number = 1
        else:
            device = click.prompt("  Device name or index", default=str(output_device))
            input_number = click.prompt("  Input number (channel)", type=int, default=1)
        musician = click.prompt("  Musician name", default="", show_default=False)
        instruments.append(Instrument(
            name=name, device=device, input_number=input_number,
            musician=musician, desktop_audio=desktop_audio,
        ))
        click.echo(f"  Added '{name}'.\n")

    config = StudioConfig(
        sample_rate=int(sample_rate),
        buffer_size=int(buffer_size),
        output_device=output_device,
        output_channels=output_channels,
        studio_musician=studio_musician,
        studio_name=studio_name,
        studio_location=studio_location,
        instruments=instruments,
    )

    errors = config.validate()
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    config.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")
    if instruments:
        click.echo("Instruments:")
        for inst in instruments:
            click.echo(f"  - {inst.name} (device={inst.device}, input={inst.input_number})")


@main.command()
def new_project() -> None:
    """Create a new recording project."""
    config = StudioConfig.load()
    projects_dir = Path(config.projects_dir)

    name = click.prompt("Project name")
    project = Project.create_new(projects_dir, name)
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

    # Load all preferred takes into mixer
    mixer = Mixer(config.sample_rate)
    click.echo(f"\nPlaying: {track.name}")
    for inst_name, take_info in track.preferred_takes.items():
        take_path = project.completed_takes_dir / take_info.filename
        if take_path.exists():
            mixer.add_source(f"take:{inst_name}", take_path, volume=take_info.volume)
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
    out_dev = config.output_device
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


def _find_monitor_device(sd: object) -> int | None:
    """Find a PulseAudio/PipeWire monitor/loopback source for desktop audio capture."""
    devices = sd.query_devices()  # type: ignore[union-attr]
    keywords = ["monitor", "loopback", "stereo mix", "what u hear", "wave out"]
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            name_lower = d["name"].lower()
            if any(kw in name_lower for kw in keywords):
                return i
    return None


@main.command()
@click.argument("instrument")
def start_session(instrument: str) -> None:
    """Start a recording session for INSTRUMENT."""
    # Load config and validate instrument
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        available = [i.name for i in config.instruments]
        click.echo(f"Error: Unknown instrument '{instrument}'.", err=True)
        if available:
            click.echo(f"Available: {', '.join(available)}", err=True)
        raise SystemExit(1)

    # Check we're in a project directory
    cwd = Path.cwd()
    if not (cwd / "setlist.json").exists():
        click.echo("Error: No setlist.json in current directory. Are you in a project folder?", err=True)
        raise SystemExit(1)

    project = Project.open(cwd)
    if not project.setlist.tracks:
        click.echo("Error: Setlist is empty. Run 'jampy update-setlist' first.", err=True)
        raise SystemExit(1)

    # Import AudioEngine here to avoid top-level sounddevice import
    # (allows non-audio commands to work without PortAudio)
    from .audio.engine import AudioEngine
    import sounddevice as sd

    # Determine input device
    out_dev = config.output_device

    if inst.desktop_audio:
        # Use manually specified device if set, otherwise auto-detect
        if inst.device:
            try:
                in_dev = int(inst.device)
            except ValueError:
                in_dev = inst.device  # type: ignore[assignment]
        else:
            in_dev = _find_monitor_device(sd)
        if in_dev is None:
            click.echo("Error: No desktop audio monitor device found.", err=True)
            click.echo("\nAvailable input devices:", err=True)
            all_devices = sd.query_devices()
            for i, d in enumerate(all_devices):
                if d["max_input_channels"] > 0:
                    click.echo(f"  [{i}] {d['name']}", err=True)
            click.echo(
                "\nTo fix: set this instrument's 'device' field in studio_config.json "
                "to the index of your loopback/monitor device.",
                err=True,
            )
            raise SystemExit(1)
        click.echo(f"Using desktop audio: {sd.query_devices(in_dev)['name']}")
    else:
        # Use the instrument's device for input
        in_dev: int | None = None
        try:
            in_dev = int(inst.device)
        except ValueError:
            in_dev = inst.device  # type: ignore[assignment]

    # Query actual device capabilities to avoid channel count mismatches
    in_info = sd.query_devices(in_dev, "input")
    out_info = sd.query_devices(out_dev, "output")
    max_in = in_info["max_input_channels"]
    output_channels = min(config.output_channels, out_info["max_output_channels"])

    # Open enough input channels to reach the instrument's input number
    # input_number is 1-based (channel 1 = index 0)
    input_channel_index = inst.input_number - 1
    input_channels = max(inst.input_number, 1)
    if input_channels > max_in:
        click.echo(
            f"Error: Instrument '{inst.name}' needs input channel {inst.input_number} "
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
    session.studio_name = config.studio_name
    session.studio_location = config.studio_location
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
    click.echo("          [l]ower volume  [u]p volume  [q]uit\n")

    # Start the audio stream and continuous session recording
    engine.start()
    session_flac = session.session_dir / "session.flac" if session.session_dir else None
    if session_flac:
        engine.start_session_recording(session_flac)
    try:
        _run_session_loop(session, engine)
    finally:
        engine.stop()


def _load_backing_track(session: Session, engine: AudioEngine) -> None:
    """Load the current track's backing file and preferred takes into the mixer."""
    track = session.current_track
    if not track:
        return
    engine.mixer.clear()
    backing_path = session.project.backing_tracks_dir / track.backing_track
    if backing_path.exists():
        engine.mixer.add_source("backing", backing_path, volume=track.volume / 100.0)

    # Load preferred takes from other instruments
    for inst_name, take_info in track.preferred_takes.items():
        if inst_name.lower() == session.instrument.lower():
            continue  # skip the instrument we're currently recording
        take_path = session.project.completed_takes_dir / take_info.filename
        if take_path.exists():
            engine.mixer.add_source(
                f"take:{inst_name}", take_path, volume=take_info.volume
            )


def _run_session_loop(session: Session, engine: AudioEngine) -> None:
    """Interactive single-key recording loop."""
    # Load the first backing track into the mixer
    _load_backing_track(session, engine)

    # When the backing track finishes, auto-trigger song_end
    def on_song_end() -> None:
        if session.state == SessionState.PLAYING:
            engine.stop_recording()
            _save_preferred_take(session)
            session.song_end(engine.mixer.position)
            _show_status(session)

    engine.set_on_song_end(on_song_end)

    _show_status(session)

    # Set terminal to raw mode for single-key input
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while session.state != SessionState.ENDED:
            # Wait for keypress
            if select.select([sys.stdin], [], [], 0.5)[0]:
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


def _save_preferred_take(session: Session) -> None:
    """Save the current take as the preferred take for this track/instrument."""
    track = session.current_track
    if not track or not hasattr(session, '_current_take_info'):
        return
    take_info = session._current_take_info
    if take_info:
        track.set_preferred_take(session.instrument, take_info)
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
        engine.mixer.set_playing(False)
        session.end_session()
        return

    if key == "r" and session.state == SessionState.WAITING:
        _start_recording(session, engine)
        _show_status(session)

    elif key == "b" and session.state == SessionState.PLAYING:
        # Restart from beginning — stop current take, reset mixer, start new take
        engine.stop_recording()
        engine.mixer.reset()

        track = session.current_track
        if track:
            take_num = next_take_number(
                session.project.completed_takes_dir, track.name, session.instrument
            )
            fname = take_filename(track.name, session.instrument, take_num, "flac")
            rec_path = session.project.completed_takes_dir / fname
            engine.start_recording(rec_path)

        session.back_to_start(engine.mixer.position)
        click.echo("  >> Back to start")

    elif key == "e" and session.state == SessionState.PLAYING:
        # End the current song — stop recording and playback
        engine.stop_recording()
        engine.mixer.set_playing(False)
        _save_preferred_take(session)
        session.song_end(engine.mixer.position)
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
        click.echo(f"[{idx}/{total}] {track.name} ({dur}) vol:{track.volume}% — {state}")
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


if __name__ == "__main__":
    main()
