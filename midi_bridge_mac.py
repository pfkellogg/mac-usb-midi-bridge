#!/usr/bin/env python3
"""MIDI Bridge for Mac — the twin of the Android "MIDI Bridge" app.

Forwards a USB keyboard (Keystation, MiniLab…) to the FM-1, either on its own
USB port ("USB Composite Device") or through a MIDI interface's 5-pin OUT
(AIR 192|6, MiniFuse…). Same look and behaviour as the tablet app:

  * FROM / TO picked automatically by name, changeable; the TO list hides the
    FROM device's own ports; the FM-1 as FROM is explained (it sends nothing)
  * ON/OFF remembered; reconnects by itself when devices are unplugged/replugged
  * real-time bytes (clock, active sensing…) dropped, everything else forwarded
  * big NOTE readout; playing pops up a large note / octave / frequency panel
    that folds away a moment after the music stops
  * All notes off — lit only while a note or the sustain pedal is down
  * a virtual "MIDI Bridge" input: other Mac apps (e.g. Karaoke MIDI Mash) can
    play the FM-1 through the bridge while it holds the keyboard
  * optional start at login

Run:  python3 midi_bridge_mac.py      (or the MIDI Bridge.app built by ./make_app.sh)
"""
import json
import os
import plistlib
import sys
import threading
import time
import tkinter as tk

import mido

VERSION = "1.2"
APP_NAME = "MIDI Bridge"
SETTINGS_DIR = os.path.expanduser("~/Library/Application Support/MIDI Bridge")
SETTINGS = os.path.join(SETTINGS_DIR, "settings.json")
AGENT_LABEL = "com.pfkellogg.midibridge"
AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")
VIRTUAL_NAME = "MIDI Bridge"

# Keyboards first; the Keystation's second port is DAW transport control, never a source.
FROM_HINTS = ["keystation", "m-audio", "minilab", "keylab", "arturia", "akai", "novation", "keyboard"]
# The FM-1 on USB first (its chip is Jieli; the Mac calls it "USB Composite Device"), then interfaces.
TO_HINTS = ["usb composite", "jieli", "fm-1", "air 192", "minifuse", "midi out", "interface"]
NEVER = ["iac driver", VIRTUAL_NAME.lower()]
SKIP_FROM = ["transport"]
SILENT_HINTS = ["usb composite", "jieli", "fm-1"]
SILENT_FROM = ("The FM-1 is all ears, no mouth — it plays the MIDI it gets, but never sends any. "
               "Choose your keyboard as FROM.")

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# White on black, every colour explicit (same palette as the tablet app).
BG, CARD, BORDER, TEXT, MUTED = "#000000", "#000000", "#5a5a5a", "#ffffff", "#c8c8c8"
BUTTON, ON_GREEN, OFF_GREY, ACCENT, DIM = "#1c1c1e", "#00963c", "#3c3c3c", "#50c8ff", "#6e6e6e"
POPUP_LINGER = 1.2


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def note_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def describe(msg):
    t = msg.type
    if t == "note_on" and msg.velocity > 0:
        return f"Note on   {note_name(msg.note)}  {note_hz(msg.note):.1f} Hz  vel {msg.velocity}  (ch {msg.channel + 1})"
    if t in ("note_on", "note_off"):
        return f"Note off  {note_name(msg.note)}  {note_hz(msg.note):.1f} Hz  (ch {msg.channel + 1})"
    if t == "control_change":
        if msg.control == 64:
            return "Sustain " + ("ON" if msg.value >= 64 else "off")
        if msg.control == 1:
            return f"Mod wheel {msg.value}"
        return f"CC {msg.control} = {msg.value}  (ch {msg.channel + 1})"
    if t == "program_change":
        return f"Program change {msg.program + 1}"
    if t == "pitchwheel":
        return f"Pitch bend {msg.pitch}"
    if t == "aftertouch":
        return f"Aftertouch {msg.value}"
    return str(msg)


REALTIME = {"clock", "start", "continue", "stop", "active_sensing", "reset"}


class Bridge:
    """Ports, forwarding and what's sounding. MIDI callbacks run on rtmidi threads."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.inp = self.out = self.virtual = None
        self.connected_from = self.connected_to = None
        self.error = None
        self.msg_count = 0
        self.last_text = ""
        self.last_note = None
        self.note_on_count = 0
        self.held = {}          # (channel, note) -> count
        self.sustain = set()    # channels with the pedal down
        self.sources, self.dests = [], []
        self.refresh_lists()

    # ---- device lists and choices

    def refresh_lists(self):
        def ok(n):
            return not any(x in n.lower() for x in NEVER)
        try:
            self.sources = [n for n in mido.get_input_names() if ok(n)]
            self.dests = [n for n in mido.get_output_names() if ok(n)]
        except Exception:
            self.sources, self.dests = [], []

    @staticmethod
    def _pick(names, saved, hints, exclude=None, skip=()):
        names = [n for n in names if n != exclude and not any(s in n.lower() for s in skip)]
        if saved and saved in names:
            return saved
        for h in hints:
            for n in names:
                if h in n.lower():
                    return n
        return names[0] if names else None

    @staticmethod
    def _device(name):
        """'Keystation 49 MK3 (USB MIDI)' and '… (Transport)' are one device."""
        return name.split(" (")[0].strip().lower() if name else ""

    def from_port(self):
        return self._pick(self.sources, self.settings.get("from"), FROM_HINTS, skip=SKIP_FROM)

    def to_choices(self):
        f = self.from_port()
        return [d for d in self.dests if self._device(d) != self._device(f)]

    def to_port(self):
        choices = self.to_choices()
        # The FM-1 on its own USB cable always wins: a MIDI interface is only the fallback
        # for when it isn't plugged in (a stray click on the AIR mustn't silently stick).
        fm1 = [d for d in choices if any(h in d.lower() for h in SILENT_HINTS)]
        if fm1:
            saved = self.settings.get("to")
            return saved if saved in fm1 else fm1[0]
        return self._pick(choices, self.settings.get("to"), TO_HINTS)

    def from_is_silent(self):
        f = self.from_port()
        return bool(f) and any(h in f.lower() for h in SILENT_HINTS)

    def choose(self, frm=None, to=None):
        if frm:
            self.settings["from"] = frm
        if to:
            self.settings["to"] = to
        save_settings(self.settings)
        if self.settings.get("on"):
            self.disconnect()
            self.connect()

    # ---- connection

    def connect(self):
        with self.lock:
            if self.inp and self.out:
                return
            self.error = None
            frm, to = self.from_port(), self.to_port()
            if not frm or not to or self.from_is_silent():
                return
            try:
                out = mido.open_output(to)
            except Exception as e:
                self.error = f"Couldn't open {to}: {e}"
                return
            try:
                inp = mido.open_input(frm, callback=self._from_keys)
            except Exception as e:
                out.close()
                self.error = f"Couldn't open {frm} — is another app using it? ({e})"
                return
            self.inp, self.out = inp, out
            self.connected_from, self.connected_to = frm, to
            if self.virtual is None:
                try:
                    self.virtual = mido.open_input(VIRTUAL_NAME, virtual=True, callback=self._from_app)
                except Exception:
                    self.virtual = None  # not fatal: just no sharing

    def disconnect(self, close_virtual=False):
        with self.lock:
            for p in (self.inp, self.out):
                try:
                    if p:
                        p.close()
                except Exception:
                    pass
            self.inp = self.out = None
            self.connected_from = self.connected_to = None
            self.held.clear()
            self.sustain.clear()
            if close_virtual and self.virtual:
                try:
                    self.virtual.close()
                except Exception:
                    pass
                self.virtual = None

    def set_on(self, want):
        self.settings["on"] = want
        save_settings(self.settings)
        if want:
            self.connect()
        else:
            self.all_notes_off()
            self.disconnect(close_virtual=True)

    def check(self):
        """Every second or so: notice unplugged/replugged devices."""
        self.refresh_lists()
        with self.lock:
            gone = (self.connected_from and self.connected_from not in self.sources) or \
                   (self.connected_to and self.connected_to not in self.dests)
        if gone:
            self.disconnect()
        if self.settings.get("on") and not (self.inp and self.out):
            self.connect()

    # ---- forwarding

    def _send(self, msg):
        out = self.out
        if out is None:
            return
        try:
            out.send(msg)
        except Exception:
            pass
        self._track(msg)

    def _from_keys(self, msg):
        if msg.type in REALTIME:
            return
        # The FM-1 only listens on channel 1; a Keystation knocked onto another
        # channel would show notes here but stay silent.
        if hasattr(msg, "channel") and msg.channel != 0:
            msg = msg.copy(channel=0)
        self._send(msg)
        self.msg_count += 1
        self.last_text = describe(msg)
        if msg.type == "note_on" and msg.velocity > 0:
            self.last_note = msg.note
            self.note_on_count += 1

    def _from_app(self, msg):
        if msg.type in REALTIME:
            return
        self._send(msg)

    def _track(self, msg):
        with self.lock:
            if msg.type == "note_on" and msg.velocity > 0:
                k = (msg.channel, msg.note)
                self.held[k] = self.held.get(k, 0) + 1
            elif msg.type in ("note_on", "note_off"):
                k = (msg.channel, msg.note)
                if self.held.get(k, 0) > 1:
                    self.held[k] -= 1
                else:
                    self.held.pop(k, None)
            elif msg.type == "control_change":
                if msg.control == 64:
                    (self.sustain.add if msg.value >= 64 else self.sustain.discard)(msg.channel)
                elif msg.control in (120, 123):
                    for k in [k for k in self.held if k[0] == msg.channel]:
                        del self.held[k]
                    if msg.control == 120:
                        self.sustain.discard(msg.channel)

    def sounding(self):
        with self.lock:
            return bool(self.held) or bool(self.sustain)

    def all_notes_off(self):
        out = self.out
        if out is None:
            return
        for ch in range(16):
            for cc, val in ((64, 0), (123, 0), (120, 0)):
                try:
                    out.send(mido.Message("control_change", channel=ch, control=cc, value=val))
                except Exception:
                    pass
        with self.lock:
            self.held.clear()
            self.sustain.clear()

    def status(self):
        if self.from_is_silent():
            return SILENT_FROM
        if self.inp and self.out:
            return f"Forwarding\n{self.connected_from}\n→ {self.connected_to}"
        if self.error:
            return self.error
        if not self.sources and not self.dests:
            return "No MIDI devices found — check the cables."
        if not self.to_port():
            return ("Only the keyboard is plugged in. The Mac doesn't see the FM-1 — "
                    "check its USB cable and that it's switched on.")
        if not self.settings.get("on"):
            return "Stopped."
        return "Waiting for both devices to be plugged in…"


# ---------------------------------------------------------------------------
# settings and start-at-login

def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def launch_command():
    """How to start this app again: the .app bundle if we're inside one, else this script."""
    exe = os.path.abspath(sys.executable)
    if ".app/Contents/" in exe:
        return ["/usr/bin/open", "-a", exe.split(".app/Contents/")[0] + ".app"]
    return [exe, os.path.abspath(__file__)]


def set_start_at_login(on):
    try:
        if on:
            os.makedirs(os.path.dirname(AGENT), exist_ok=True)
            with open(AGENT, "wb") as f:
                plistlib.dump({"Label": AGENT_LABEL, "ProgramArguments": launch_command(),
                               "RunAtLoad": True, "ProcessType": "Interactive"}, f)
        elif os.path.exists(AGENT):
            os.remove(AGENT)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# window

class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.bridge = Bridge(self.settings)
        self.shown_count = -1
        self.shown_ons = None
        self.popup_shown = False
        self.last_note_at = 0.0
        self.sound_shown = None
        self.build()
        if self.settings.get("on"):
            self.bridge.set_on(True)
        self.refresh()
        self.tick()
        self.poll_devices()

    # ---- layout helpers

    def font(self, size, bold=False):
        return ("Helvetica Neue", size, "bold" if bold else "normal")

    def card(self, parent):
        f = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=10)
        f.pack(fill="x", pady=(14, 0))
        return f

    def label(self, parent, text):
        tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=self.font(16, True), anchor="w").pack(fill="x", pady=(8, 2))

    def big_button(self, parent, text, cmd, size=20, bg=BUTTON, fg=TEXT):
        # macOS ignores bg on tk.Button, so buttons are clickable Labels.
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=self.font(size), padx=14, pady=12, anchor="w",
                     highlightbackground=BORDER, highlightthickness=1, cursor="hand2", justify="left", wraplength=600)
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def build(self):
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        r.minsize(640, 700)
        outer = tk.Frame(r, bg=BG, padx=24, pady=18)
        outer.pack(fill="both", expand=True)
        self.outer = outer
        tk.Label(outer, text="MIDI Bridge", bg=BG, fg=TEXT, font=self.font(30, True), anchor="w").pack(fill="x")
        tk.Label(outer, text=f"Keyboard → FM-1      Mac v{VERSION}", bg=BG, fg=MUTED, font=self.font(17),
                 anchor="w").pack(fill="x", pady=(2, 4))

        dev = self.card(outer)
        self.label(dev, "FROM  ·  keyboard")
        self.from_btn = self.big_button(dev, "", lambda: self.pick(True))
        self.from_btn.pack(fill="x")
        self.label(dev, "TO  ·  FM-1 (USB) or MIDI interface")
        self.to_btn = self.big_button(dev, "", lambda: self.pick(False))
        self.to_btn.pack(fill="x", pady=(0, 6))

        self.toggle = tk.Label(outer, text="", font=self.font(22, True), fg="white", pady=18, cursor="hand2")
        self.toggle.bind("<Button-1>", lambda e: self.on_toggle())
        self.toggle.pack(fill="x", pady=(16, 0))
        tk.Label(outer, text="Keeps forwarding while this window is open (it can be minimised). "
                             "Other apps can play the FM-1 through the \"MIDI Bridge\" MIDI input.",
                 bg=BG, fg=MUTED, font=self.font(15), anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(6, 0))

        self.panic = tk.Label(outer, text="All notes off", font=self.font(19), pady=10, cursor="hand2",
                              highlightthickness=1)
        self.panic.bind("<Button-1>", lambda e: self.on_panic())
        self.panic.pack(fill="x", pady=(10, 0))

        info = self.card(outer)
        self.label(info, "STATUS")
        self.status = tk.Label(info, text="", bg=CARD, fg=TEXT, font=self.font(20), anchor="w", justify="left",
                               wraplength=600)
        self.status.pack(fill="x")
        self.label(info, "NOTE")
        self.big_note = tk.Label(info, text="—", bg=CARD, fg=TEXT, font=self.font(40, True), anchor="w")
        self.big_note.pack(fill="x")
        self.label(info, "LAST MESSAGE")
        self.last_msg = tk.Label(info, text="—", bg=CARD, fg=TEXT, font=("Menlo", 17), anchor="w", justify="left")
        self.last_msg.pack(fill="x", pady=(0, 6))

        self.login_var = tk.BooleanVar(value=os.path.exists(AGENT))
        tk.Checkbutton(outer, text="Start MIDI Bridge when I log in", variable=self.login_var,
                       command=self.on_login, bg=BG, fg=TEXT, selectcolor=BG, activebackground=BG,
                       activeforeground=TEXT, font=self.font(17), anchor="w").pack(fill="x", pady=(12, 0))

        # the big note pop-up: covers the window while playing, folds away after
        self.popup = tk.Frame(r, bg="#0b0b0d")
        self.popup.bind("<Button-1>", lambda e: self.hide_popup())
        panel = tk.Frame(self.popup, bg="#0e141a", highlightbackground=ACCENT, highlightthickness=3, padx=26, pady=18)
        panel.place(relx=0.5, rely=0.5, anchor="center")
        self.pop_name = tk.Label(panel, text="", bg="#0e141a", fg=TEXT, font=self.font(150, True))
        self.pop_name.pack()
        row = tk.Frame(panel, bg="#0e141a")
        row.pack(fill="x", pady=(8, 0))
        self.pop_oct = self.tile(row, "OCTAVE")
        self.pop_hz = self.tile(row, "FREQUENCY")
        for w in (panel, self.pop_name, row):
            w.bind("<Button-1>", lambda e: self.hide_popup())

    def tile(self, row, caption):
        t = tk.Frame(row, bg="#000000", highlightbackground=BORDER, highlightthickness=1, padx=18, pady=8)
        t.pack(side="left", expand=True, fill="x", padx=6)
        tk.Label(t, text=caption, bg="#000000", fg=ACCENT, font=self.font(16, True)).pack()
        v = tk.Label(t, text="", bg="#000000", fg=TEXT, font=self.font(44, True))
        v.pack()
        for w in (t, v):
            w.bind("<Button-1>", lambda e: self.hide_popup())
        return v

    # ---- actions

    def pick(self, is_from):
        b = self.bridge
        b.refresh_lists()
        items = [n for n in b.sources if not any(s in n.lower() for s in SKIP_FROM)] if is_from else b.to_choices()
        if not is_from and b.from_is_silent():
            self.show_note_box("Nothing to send", SILENT_FROM)
            return
        if not items:
            self.show_note_box("FROM — the keyboard you play" if is_from else "TO — the FM-1 (or MIDI interface)",
                               "No MIDI devices found. Check the cables." if is_from or not b.sources else
                               "Only the keyboard is plugged in. The Mac doesn't see the FM-1 — "
                               "check its USB cable and that it's switched on.")
            return
        m = tk.Menu(self.root, tearoff=0, font=self.font(18))
        for n in items:
            m.add_command(label=n, command=lambda n=n: (b.choose(frm=n) if is_from else b.choose(to=n), self.refresh()))
        btn = self.from_btn if is_from else self.to_btn
        m.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())

    def show_note_box(self, title, text):
        from tkinter import messagebox
        messagebox.showinfo(title, text, parent=self.root)

    def on_toggle(self):
        self.bridge.set_on(not self.settings.get("on"))
        self.refresh()

    def on_panic(self):
        if self.bridge.sounding():
            self.bridge.all_notes_off()

    def on_login(self):
        if not set_start_at_login(self.login_var.get()):
            self.login_var.set(os.path.exists(AGENT))

    # ---- refresh

    def refresh(self):
        b = self.bridge
        f, t = b.from_port(), b.to_port()
        self.from_btn.config(text=(f or "(no keyboard found)") + "   ▾")
        if b.from_is_silent():
            self.to_btn.config(text="Nothing to send — the FM-1 doesn't send MIDI   ⓘ", fg=DIM)
        else:
            self.to_btn.config(text=(t or "(nothing found — click to see devices)") + "   ▾", fg=TEXT)
        self.status.config(text=b.status())
        on = bool(self.settings.get("on"))
        self.toggle.config(text="●  ON  —  click to stop" if on else "OFF  —  click to start",
                           bg=ON_GREEN if on else OFF_GREY)

    def poll_devices(self):
        self.bridge.check()
        self.refresh()
        self.root.after(1500, self.poll_devices)

    def tick(self):
        b = self.bridge
        if b.msg_count != self.shown_count:
            self.shown_count = b.msg_count
            self.last_msg.config(text=f"{b.last_text}\n{b.msg_count} messages forwarded" if b.msg_count else "—")
            if b.last_note is not None:
                self.big_note.config(text=f"{note_name(b.last_note)}   {note_hz(b.last_note):.1f} Hz")
        if self.shown_ons is None:
            self.shown_ons = b.note_on_count
        elif b.note_on_count != self.shown_ons:
            self.shown_ons = b.note_on_count
            self.last_note_at = time.monotonic()
            self.show_popup(b.last_note)
        sounding = b.sounding()
        if self.popup_shown and not sounding and time.monotonic() - self.last_note_at > POPUP_LINGER:
            self.hide_popup()
        if sounding != self.sound_shown:
            self.sound_shown = sounding
            self.panic.config(bg=CARD, fg=TEXT if sounding else DIM,
                              highlightbackground=BORDER if sounding else "#323232")
        self.root.after(40, self.tick)

    # ---- pop-up: grows in, folds away

    def show_popup(self, note):
        if note is None:
            return
        self.pop_name.config(text=NOTE_NAMES[note % 12])
        self.pop_oct.config(text=str(note // 12 - 1))
        self.pop_hz.config(text=f"{note_hz(note):.1f} Hz")
        if not self.popup_shown:
            self.popup_shown = True
            self.popup.place(x=0, y=0, relwidth=1, relheight=1)
            self.popup.lift()
            self.animate([60, 105, 150])
        else:
            self.animate([165, 150])   # a little pulse for each new key

    def hide_popup(self):
        if not self.popup_shown:
            return
        self.popup_shown = False
        self.animate([110, 70], then=self.popup.place_forget)

    def animate(self, sizes, then=None):
        def step(i=0):
            if i < len(sizes):
                self.pop_name.config(font=self.font(sizes[i], True))
                self.root.after(45, lambda: step(i + 1))
            elif then and not self.popup_shown:
                then()
                self.pop_name.config(font=self.font(150, True))
        step()


def main():
    root = tk.Tk()
    app = App(root)

    def quit_():
        app.bridge.all_notes_off()
        app.bridge.disconnect(close_virtual=True)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", quit_)
    root.createcommand("::tk::mac::Quit", quit_)
    root.mainloop()


if __name__ == "__main__":
    main()
