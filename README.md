# Mac USB MIDI Bridge

Play a synth from a **USB-only MIDI keyboard** on a Mac: the app forwards the keyboard to the synth, either over the synth's own USB port or through a MIDI interface's 5-pin OUT.

The Mac twin of [Android USB MIDI Bridge](https://github.com/pfkellogg/android-usb-midi-bridge). It was built for an M-Audio Keystation 49 MK3 playing an M-VAVE FM-1, but it works with any MIDI devices the Mac can see.

## Features

- **FROM** (keyboard) and **TO** (synth) are picked automatically by name, and you can change them. The FM-1 shows up as "USB Composite Device".
- ON/OFF is remembered, and the bridge reconnects by itself when devices are unplugged and plugged back in.
- **Keyboard notes always go out on MIDI channel 1.** The FM-1 only listens on channel 1, so a keyboard accidentally switched to another channel still plays.
- Real-time bytes (clock, active sensing) are dropped. Everything else is forwarded.
- A big note readout, plus a large note / octave / frequency panel that pops up while you play.
- **All notes off**, lit only while a note or the sustain pedal is down.
- A virtual **"MIDI Bridge"** input, so other Mac apps can play the synth through the bridge while it holds the keyboard. Their channels are passed through unchanged.
- Optional start at login.

## Run

Needs Python 3 with `mido`, `python-rtmidi` and `tkinter`:

```
pip install mido python-rtmidi
python3 midi_bridge_mac.py
```

To build and install `/Applications/MIDI Bridge.app`:

```
./make_app.sh                          # uses ~/miniforge3/bin/python3
PY=/path/to/python3 ./make_app.sh      # or another Python
```

On Apple-silicon Macs, use a native arm64 Python.

Settings are kept in `~/Library/Application Support/MIDI Bridge/settings.json`.

## License

MIT
