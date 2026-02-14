# Jam.py

A TUI application for musicians to record instruments over backing tracks, manage songs/albums, and track completed takes.

Jam.py handles continuous audio recording, playback mixing, session logging, and automatic splicing of completed takes. Record one instrument at a time — each new session plays back your previous takes alongside the backing track so you can layer parts.

## Requirements

- Python 3.9+
- ffmpeg (for MP3/M4A decoding)
- PortAudio (for real-time audio I/O)

### System Dependencies

**Ubuntu/Debian:**
```bash
sudo apt install ffmpeg libportaudio2 libsndfile1
```

**macOS:**
```bash
brew install ffmpeg portaudio libsndfile
```

**Arch:**
```bash
sudo pacman -S ffmpeg portaudio libsndfile
```

## Install

```bash
pip install -e .
```

## Usage

```bash
jampy
```

Or:

```bash
python -m jampy
```

### First Run

On first launch you'll see the home screen. Start with **Studio Setup** to configure your audio devices, sample rate, buffer size, and projects directory. The config is saved to `~/studio_config.json`.

### Creating a Project

1. Select **New Project** and enter a name (e.g. "My Album")
2. On the project screen, add backing tracks by entering the full path to each audio file (FLAC, WAV, MP3, M4A)
3. Enter an instrument name (e.g. "acoustic guitar")
4. Press **Start Session**

### Recording Session

Audio capture runs continuously from the moment the session starts until it ends. The session follows this flow:

| Key | Action | When |
|-----|--------|------|
| `r` | Start recording (plays backing track) | Waiting |
| `b` | Back to start (restart the take) | Playing |
| `e` | Mark song end (complete the take) | Playing |
| `n` | Move to next track | Between tracks |
| `q` / `Esc` | End session | Any time |

**Completing a take:** Press `r` to start, then `e` when the song finishes. The take is marked as completed.

**Restarting a take:** If you make a mistake, press `b` to loop back to the beginning. The backing track restarts and you keep playing. Takes with a restart are marked as mistakes and won't be spliced.

### Post-Session Processing

When you end a session, Jam.py automatically:

1. Parses the session log to find completed takes (no restarts)
2. Splices the audio from the raw recording
3. Saves each take to `completed_takes/` as `Track Name - instrument - takeN.flac`
4. Updates the setlist so the new take becomes the preferred take for that instrument

### Multi-Instrument Layering

Start a new session with a different instrument (e.g. "electric bass"). The backing track now plays back mixed with your previously recorded preferred takes, so you hear everything together while recording the new part.

## Project Structure

Each project creates this directory layout:

```
My Album/
├── setlist.json
├── backing_tracks/
│   ├── song1.mp3
│   └── song2.flac
├── completed_takes/
│   ├── song1 - acoustic guitar - take1.flac
│   ├── song1 - electric bass - take1.flac
│   └── song2 - acoustic guitar - take2.flac
└── sessions/
    └── 2025-01-15_14-30-00_acoustic guitar/
        ├── raw_recording.flac
        └── session_log.json
```

Existing take files are never deleted. New takes for the same instrument increment the take number and replace the preferred take in the setlist.
