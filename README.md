# Jam.py

A CLI application for musicians to record instruments over backing tracks, manage songs/albums, and track completed takes.

Jam.py handles continuous audio recording, playback mixing, session logging, and per-take file management. Record one instrument at a time — each new session plays back your previous takes alongside the backing track so you can layer parts.

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

### Studio Setup

Configure your audio devices, output settings, and instruments:

```bash
jampy studio-setup
```

The wizard asks for:
- Studio name, location, and default musician name (all optional)
- Sample rate and buffer size
- Output device and channel count
- Audio interfaces: select devices, label input channels
- Instruments: name, input label, musician name

Config is saved to `~/studio_config.json`.

### Creating a Project

```bash
jampy new-project
```

Enter a project name (e.g. "My Album"). Creates the project folder structure in `~/JamPy Projects/`.

### Updating the Setlist

Copy audio files (FLAC, WAV, MP3, M4A) into the project's `backing_tracks/` directory, then:

```bash
cd ~/JamPy\ Projects/My\ Album
jampy update-setlist
```

Scans `backing_tracks/`, adds new files to `setlist.json`, and removes entries for deleted files. Each track in `setlist.json` includes a `volume` field (default 100%) that you can edit manually to adjust backing track playback level.

### Recording Session

```bash
cd ~/JamPy\ Projects/My\ Album
jampy start-session guitar
```

The session plays the backing track through your speakers, monitors your instrument input in real-time, and records your take. Controls:

| Key | Action | When |
|-----|--------|------|
| `r` | Start recording (plays backing track) | Waiting |
| `b` | Back to start (restart the take) | Playing |
| `e` | Mark song end (complete the take) | Playing |
| `n` | Move to next track (auto-starts recording) | Between tracks |
| `l` | Lower backing track volume by 5% | Any time |
| `u` | Raise backing track volume by 5% | Any time |
| `q` | End session | Any time |

**Completing a take:** Press `r` to start, then `e` when the song finishes. The take is saved to `completed_takes/` and set as the preferred take in the setlist.

**Restarting a take:** Press `b` to loop back to the beginning. The backing track restarts and a new take file begins.

**Volume adjustments** are saved back to `setlist.json` at the end of the session.

### Multi-Instrument Layering

Start a new session with a different instrument (e.g. "bass"). The backing track plays mixed with your previously recorded preferred takes, so you hear everything together while recording the new part.

### Desktop Audio Capture

To record virtual instruments, software synths, or system audio, you need a virtual audio loopback device. See [Desktop Audio Capture Setup](docs/desktop-audio-capture.md) for instructions.

## Project Structure

Each project creates this directory layout:

```
My Album/
├── setlist.json
├── backing_tracks/
│   ├── song1.mp3
│   └── song2.flac
├── completed_takes/
│   ├── song1 - guitar - take1.flac
│   ├── song1 - bass - take1.flac
│   └── song2 - guitar - take2.flac
└── sessions/
    └── 2025-01-15_14-30-00_guitar/
        ├── session.flac
        └── session_log.json
```

- `completed_takes/` — individual per-song recordings, one file per take
- `sessions/` — continuous raw recording (`session.flac`) spanning the full session, plus the session log with musician, studio, and event data
- Existing take files are never deleted. New takes increment the take number and replace the preferred take in the setlist.
