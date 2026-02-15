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

    # Studio info
    studio_name = click.prompt("Studio name", default="", show_default=False)
    studio_location = click.prompt("Studio location", default="", show_default=False)
    studio_musician = click.prompt("Studio musician (default performer)", default="", show_default=False)
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
        default="48000",
    )

    # Buffer size
    buf_choices = [str(b) for b in VALID_BUFFER_SIZES]
    buffer_size = click.prompt(
        "Buffer size",
        type=click.Choice(buf_choices),
        default="512",
    )

    # Output device
    if devices:
        click.echo("Output devices:")
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                click.echo(f"  [{i}] {d['name']}  ({d['max_output_channels']} channels)")
    output_device = click.prompt("Output device index", type=int, default=0)
    max_out = devices[output_device]["max_output_channels"] if devices else 2
    output_channels = click.prompt("Output channels", type=int, default=min(2, max_out))

    # Instruments — start with any previously configured instruments
    existing_config = StudioConfig.load()
    instruments: list[Instrument] = list(existing_config.instruments)
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


def _find_monitor_device(sd: object) -> int | None:
    """Find a PulseAudio/PipeWire monitor source for desktop audio capture."""
    devices = sd.query_devices()  # type: ignore[union-attr]
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "monitor" in d["name"].lower():
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
        # Find a monitor/loopback device for desktop audio capture
        in_dev = _find_monitor_device(sd)
        if in_dev is None:
            click.echo("Error: No desktop audio monitor device found.", err=True)
            click.echo("On PulseAudio/PipeWire, ensure a monitor source is available.", err=True)
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
        session.next_track()
        if session.state == SessionState.WAITING:
            _load_backing_track(session, engine)
            # Automatically start recording the next track
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
