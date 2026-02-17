# Desktop Audio Capture Setup

To record desktop audio (virtual instruments, software synths, browser audio, etc.) with Jam.py, you need a virtual audio loopback device. This creates a fake audio input that captures whatever your system is playing.

## macOS: BlackHole

[BlackHole](https://github.com/ExistentialAudio/BlackHole) is a free, open-source virtual audio driver.

### Install

```bash
brew install blackhole-2ch
```

Or download from [github.com/ExistentialAudio/BlackHole](https://github.com/ExistentialAudio/BlackHole).

### Configure

1. Open **Audio MIDI Setup** (search in Spotlight)
2. Click **+** in the bottom left, select **Create Multi-Output Device**
3. Check both your real speakers/headphones and **BlackHole 2ch**
4. Set this Multi-Output Device as your system output (System Settings > Sound > Output)

Now anything playing through your system is simultaneously sent to BlackHole. In `jampy studio-setup`, select **BlackHole 2ch** as one of your audio interfaces and label its input.

### Teardown

When you're done recording, switch your system output back to your normal speakers/headphones.

## Linux: PipeWire / PulseAudio

Most modern Linux desktops using PipeWire or PulseAudio already expose monitor sources.

### PipeWire (recommended)

PipeWire provides monitor sources automatically. Run `jampy studio-setup` and look for a device with "Monitor" in the name (e.g. "Monitor of Built-in Audio Analog Stereo"). Select it as an interface and label its input.

### PulseAudio

Load the null sink module to create a virtual loopback:

```bash
pactl load-module module-null-sink sink_name=JamPyLoopback sink_properties=device.description="JamPy Loopback"
```

Then route your application's audio to the "JamPy Loopback" sink. The corresponding monitor source will appear as an input device in `jampy studio-setup`.

To make it permanent, add the line to `/etc/pulse/default.pa`.

## Windows: VB-CABLE

[VB-CABLE](https://vb-audio.com/Cable/) is a free virtual audio cable for Windows.

1. Download and install from [vb-audio.com/Cable](https://vb-audio.com/Cable/)
2. Set **CABLE Input** as your system playback device (Settings > Sound > Output)
3. In `jampy studio-setup`, select **CABLE Output** as an audio interface and label its input

## Usage in Jam.py

Once your virtual loopback device is set up:

1. Run `jampy studio-setup`
2. Select the virtual device as one of your audio interfaces
3. Label the input (e.g. "Desktop Audio")
4. Create an instrument that uses that input label
5. Record as normal with `jampy start-session <instrument>`
