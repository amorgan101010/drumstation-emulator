#!/usr/bin/env python3
"""Desktop control surface for the realtime DrumStation firmware emulator."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
from sequencer import (PAGE_COUNT, SCALES, SequencerClock, SequencerProject, StepEvent,
                       page_step, step_gesture_toggles)


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / "drumstation"
ROMS = ROOT / "roms"
# User programs 25–39 and the stored MIDI channels, kept between runs.
EEPROM = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "drumstation" / "eeprom.bin"
# What I/O SETUP was last set to. The MIDI channel is not here: it is in the EEPROM.
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "drumstation" / "panel.json"
SEQUENCER_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "drumstation" / "sequencer.json"
NO_MIDI_INPUT = "None"
ASSETS = Path(__file__).with_name("assets")
PANEL_W, PANEL_H = 1680, 945
SEQUENCER_SIZE = (1440, 440)
SEQUENCER_FULL_SIZE = (1440, 640)
SEQUENCER_ALL_SIZE = (1440, 900)

# These addresses were mapped from the firmware's own recalled-program image
# (RAM 0100..). The high bit is a kit/validity flag; the low seven bits are the
# value shown by the virtual panel. Voice edits temporarily use controller
# input until the hardware analogue scanner is mapped safely.
CC_ADDRESS = {
    23: 0x137, 24: 0x139, 25: 0x13A, 29: 0x145, 30: 0x147, 31: 0x148,
    35: 0x150, 36: 0x152, 40: 0x15A, 41: 0x15C, 45: 0x164, 46: 0x166,
    48: 0x13D, 50: 0x141, 53: 0x169, 55: 0x14B, 56: 0x14D, 58: 0x155, 59: 0x157,
    61: 0x160, 62: 0x161, 65: 0x175, 68: 0x171, 71: 0x16D, 73: 0x179, 75: 0x17D,
    76: 0x100, 77: 0x102, 78: 0x103, 79: 0x10A, 80: 0x10C, 81: 0x10D,
    85: 0x114, 86: 0x116, 90: 0x119, 91: 0x11B, 95: 0x123, 96: 0x125,
    98: 0x106, 100: 0x110, 102: 0x11E, 103: 0x120, 104: 0x132,
    113: 0x134, 115: 0x128, 116: 0x12A, 118: 0x12D, 119: 0x12F,
}

# Level has no MIDI controller on the DrumStation. The low seven bits of each
# voice's recalled program byte are the level; bit 7 is VL (velocity on).
LEVEL_ADDRESS = {
    "BASS DRUM": (0x138, 0x101), "SNARE": (0x146, 0x10B),
    "LOW TOM": (0x151, 0x115), "MID TOM": (0x15B, 0x11A),
    "HIGH TOM": (0x165, 0x124), "RIM SHOT": (0x13E, 0x107),
    "HAND CLAP": (0x142, 0x111), "COWBELL": (0x16A, 0x16A),
    "CLOSED HAT": (0x14C, 0x11F), "OPEN HAT": (0x156, 0x133),
    "CRASH": (0x15F, 0x129), "RIDE": (0x15F, 0x12E),
    "LOW CONGA": (0x176, 0x176), "MID CONGA": (0x172, 0x172),
    "HIGH CONGA": (0x16E, 0x16E), "MARACAS": (0x17A, 0x17A),
    "CLAVES": (0x17E, 0x17E),
}

# Utility-mode fields with no front-panel knob. Each voice's Pan byte holds
# Output Set in its low nibble (0..8 = L4..R4, 9..14 = individual outputs)
# and Distortion in its high nibble. The byte before it holds Note-Off in
# bit 7 and, on bass drum, snare and toms, Front Cut 0..99 ms in bits 0..6.
PAN_ADDRESS = {
    "BASS DRUM": (0x13C, 0x105), "SNARE": (0x14A, 0x10F),
    "LOW TOM": (0x154, 0x118), "MID TOM": (0x15E, 0x11D),
    "HIGH TOM": (0x168, 0x127), "RIM SHOT": (0x140, 0x109),
    "HAND CLAP": (0x144, 0x113), "COWBELL": (0x16C, 0x16C),
    "CLOSED HAT": (0x14F, 0x122), "OPEN HAT": (0x159, 0x136),
    "CRASH": (0x163, 0x12C), "RIDE": (0x163, 0x131),
    "LOW CONGA": (0x178, 0x178), "MID CONGA": (0x174, 0x174),
    "HIGH CONGA": (0x170, 0x170), "MARACAS": (0x17C, 0x17C),
    "CLAVES": (0x180, 0x180),
}
CARD_STRIP_H = 54
FRONT_CUT_VOICES = ("BASS DRUM", "SNARE", "LOW TOM", "MID TOM", "HIGH TOM")
PAN_NAMES = ("L4", "L3", "L2", "L1", "C", "R1", "R2", "R3", "R4")
# Assign-bank drum codes (RAM 0181..0184), in the firmware's display order.
DRUM_CODES = tuple(f"909 {n}" for n in ("BD", "SD", "LT", "MT", "HT", "RS", "CP", "CH", "OH", "CRASH", "RIDE")) + \
    tuple(f"808 {n}" for n in ("BD", "SD", "LT", "MT", "HT", "RS", "CP", "CB", "CH", "OH", "CYM",
                               "LC", "MC", "HC", "MA", "CL"))

# ROM table 0457 maps four scanned rows to numeric, edit, and panel-button
# codes. Code 0 is the zero key; code C is MODE, not the plus key.
KEY_POSITIONS = ((0, 2), (1, 2), (2, 2), (3, 2),
                 (0, 3), (1, 3), (2, 3), (3, 3),
                 (0, 1), (1, 1), (2, 0), (3, 1))
KEYS = (("1", "DRUM SELECT"), ("2", "FRONT CUT"), ("3", "CONT. / VELOCITY"),
        ("4", "NOTE-OFF REC."), ("5", "GEN. MIDI SET"), ("6", "OUTPUT SET"),
        ("7", "ASSIGN BANK"), ("8", "DISTORTION"), ("9", "DEMO"),
        ("−", "SAVE TYPE / COMPARE"), ("0", "TRIGGER START / STOP"), ("+", "SAVE / WRITE"))

# Both kits sound at once and keep separate settings, so each kit gets its own
# block of cards: (kit, left edge, rows of (voice, card width)). The hardware's
# TR808/TR909 buttons only pick which kit its shared knobs edit; the panel does
# not need them. Bass/snare, tom and hat/cymbal rows line up across the blocks,
# and within a block cards with the same number of knobs share one width.
KIT_BLOCKS = (
    ("808", 47, ((("BASS DRUM", 376), ("SNARE", 376), ("RIM SHOT", 184)),
                 (("LOW TOM", 248), ("MID TOM", 248), ("HIGH TOM", 248), ("COWBELL", 184)),
                 (("CLOSED HAT", 248), ("OPEN HAT", 248), ("CRASH", 248), ("HAND CLAP", 184)),
                 (("LOW CONGA", 184), ("MID CONGA", 184), ("HIGH CONGA", 184), ("MARACAS", 184), ("CLAVES", 184)))),
    ("909", 1023, ((("BASS DRUM", 301), ("SNARE", 301)),
                   (("LOW TOM", 198), ("MID TOM", 198), ("HIGH TOM", 198)),
                   (("CLOSED HAT", 198), ("OPEN HAT", 198), ("CRASH", 198)),
                   (("RIDE", 198), ("RIM SHOT", 198), ("HAND CLAP", 198)))),
)
TRACK_IDS = tuple(f"{kit}:{title}" for kit, _left, rows in KIT_BLOCKS
                  for cells in rows for title, _width in cells)
CARD_GAP, CARD_TOP, CARD_H, CARD_TITLE_H = 8, 161, 175, 27
MAIN_R, MINI_R, HEADER_R = 20, 10, 27
TICK = 7  # the printed tick ring reaches this far beyond a knob's radius

# Fixed header and frame geometry matching the bundled artwork.
CHASSIS = (14, 12, PANEL_W - 14, PANEL_H - 12)
SCREWS = tuple((x, y) for x in (28, PANEL_W - 28) for y in (31, PANEL_H - 31))
HEADER = (46, 29, PANEL_W - 46, 136)
STATUS = (47, 897, PANEL_W - 47, 926)
WORDMARK = (68, 57, 114)  # left, top and width of the two-line name painted on the plate
MASTER_KNOB = (239, 76)
# Output stage, after the firmware: key, caption and knob centre. COMP and
# REVERB are the engine's master effects 0 and 1.
OUTPUT_KNOBS = (("comp", "COMP", (1463, 84)), ("reverb", "REVERB", (1527, 84)), ("boost", "OUT BOOST", (1591, 84)))
# The reverb's two characters (master effect 2), on a button above its knob.
ROOM_BUTTON, ROOM_NAMES = (1501, 35), ("BIG", "TIGHT")
GM_BUTTONS = ((319, 48), (389, 48))
DISPLAY_LENS = (460, 41, 540, 95)
DIGITS = ((468, 48), (502, 48))
DIGIT_W, DIGIT_H = 34, 44
WRITE_BUTTON = (543, 48)
MODE_LEDS = tuple((616, 42 + index * 17) for index in range(5))
MODE_BUTTONS = ((721, 36), (721, 65), (721, 94))
KEYPAD, KEY_PITCH = (824, 33), (130, 25)
BANKS, BANK_PITCH = (1224, 54), (92, 24)
ENGINE_LED = (1338, 117)
IO_BUTTON = (1224, 105)
# Button artwork by name: (width, height). Every style of each is pre-rendered.
BUTTON_SIZES = {"gm": (62, 28), "write": (58, 40), "mode": (92, 25), "key": (126, 24),
                "bank": (88, 20), "io": (96, 25), "hit": (30, 18), "noteoff": (52, 18),
                "vel": (26, 12), "track": (16, 16), "seq": (88, 25), "seqstep": (62, 46),
                "seqctl": (30, 22), "seqpat": (42, 22), "seqpage": (42, 22),
                "seqcell": (16, 15), "seqrow": (56, 15)}
BUTTON_STYLES = ("plain", "lit", "red")
KNOB_FRAMES = 128
SEGMENT_BITS = (7, 3, 4, 2, 5, 1, 0, 6)  # a b c d e f g, decimal point

# (label, physical control group, 808 controller, 909 controller). A label
# written "A / B" names the 909's parameter first and the 808's second.
CARD_CONTROLS = {
    "BASS DRUM": (("LEVEL", 1, None, None), ("TUNE", 2, 23, 76), ("ATTACK / TONE", 3, 24, 77), ("DECAY", 4, 25, 78)),
    "SNARE": (("LEVEL", 5, None, None), ("TUNE", 6, 29, 79), ("TONE", 7, 30, 80), ("SNAPPY", 8, 31, 81)),
    "LOW TOM": (("LEVEL", 9, None, None), ("TUNE", 10, 35, 85), ("DECAY", 11, 36, 86)),
    "MID TOM": (("LEVEL", 9, None, None), ("TUNE", 10, 40, 90), ("DECAY", 11, 41, 91)),
    "HIGH TOM": (("LEVEL", 9, None, None), ("TUNE", 10, 45, 95), ("DECAY", 11, 46, 96)),
    "RIM SHOT": (("LEVEL", 12, None, None), ("TUNE", 13, 48, 98)),
    "HAND CLAP": (("LEVEL", 12, None, None), ("TUNE", 13, 50, 100)),
    "COWBELL": (("LEVEL", 12, None, None), ("TUNE", 13, 53, 53)),
    "CLOSED HAT": (("LEVEL", 14, None, None), ("TUNE", 15, 55, 102), ("DECAY", 16, 56, 103)),
    "OPEN HAT": (("LEVEL", 14, None, None), ("TUNE", 15, 58, 104), ("DECAY", 16, 59, 113)),
    "CRASH": (("LEVEL", 17, None, None), ("TUNE / TONE", 18, 61, 115), ("DECAY", 19, 62, 116)),
    "RIDE": (("LEVEL", 17, None, None), ("TUNE / TONE", 18, 61, 118), ("DECAY", 19, 62, 119)),
    "LOW CONGA": (("LEVEL", 20, None, None), ("TUNE", 21, 65, 65)),
    "MID CONGA": (("LEVEL", 20, None, None), ("TUNE", 21, 68, 68)),
    "HIGH CONGA": (("LEVEL", 20, None, None), ("TUNE", 21, 71, 71)),
    "MARACAS": (("LEVEL", 20, None, None), ("TUNE", 21, 73, 73)),
    "CLAVES": (("LEVEL", 20, None, None), ("TUNE", 21, 75, 75)),
}

VOICE_NOTES = {
    "BASS DRUM": 36, "SNARE": 38, "LOW TOM": 41, "MID TOM": 45,
    "HIGH TOM": 48, "RIM SHOT": 37, "HAND CLAP": 39, "COWBELL": 56,
    "CLOSED HAT": 42, "OPEN HAT": 46, "CRASH": 49, "RIDE": 51,
    "LOW CONGA": 64, "MID CONGA": 63, "HIGH CONGA": 62,
    "MARACAS": 70, "CLAVES": 75,
}
# VOICE_NOTES reaches the kit in the General MIDI map (RAM 018B). The other
# kit's shared voices answer these notes instead; verified on the firmware by
# muting each voice's level byte under both GM settings.
OTHER_KIT_NOTES = {
    "BASS DRUM": 24, "RIM SHOT": 25, "SNARE": 26, "HAND CLAP": 27,
    "LOW TOM": 28, "MID TOM": 29, "CLOSED HAT": 30, "HIGH TOM": 31,
    "CRASH": 32, "OPEN HAT": 34,
}


def hit_note(kit: str, title: str, gm: int) -> int:
    """MIDI note that plays `title` from `kit` while the GM map holds kit `gm` (0 = 808, 1 = 909)."""
    if kit == ("808", "909")[gm & 1]:
        return VOICE_NOTES[title]
    return OTHER_KIT_NOTES.get(title, VOICE_NOTES[title])


def card_rects():
    """Every voice card as (kit, title, x, y, width, height)."""
    for kit, left, rows in KIT_BLOCKS:
        for row, cells in enumerate(rows):
            x = left
            for title, width in cells:
                yield kit, title, x, CARD_TOP + row * (CARD_H + CARD_GAP), width, CARD_H
                x += width + CARD_GAP


def card_knobs(x: int, y: int, w: int, h: int, title: str):
    """Knob centres of one card: its main row, then the utility strip's minis."""
    # Main knobs sit in one row above the utility strip. Each knob block
    # reaches radius+7 above its centre (tick ring) and radius+45 below
    # (caption, value and VEL toggle).
    main_top, main_bottom = y + CARD_TITLE_H, y + h - CARD_STRIP_H
    knob_y = main_top + (main_bottom - main_top - (2 * MAIN_R + 52)) // 2 + MAIN_R + 7
    count = len(CARD_CONTROLS[title])
    minis = 3 if title in FRONT_CUT_VOICES else 2
    return ([(int(x + w * (index + 0.5) / count), knob_y) for index in range(count)],
            [(int(x + w * (index + 0.5) / minis), y + h - CARD_STRIP_H + 21) for index in range(minis)])


def plate_geometry() -> dict:
    """Everything the pre-rendered plate depicts, for the renderer and its staleness check."""
    cards = [list(card) for card in card_rects()]
    knobs = [(*MASTER_KNOB, HEADER_R)] + [(*centre, MAIN_R) for _key, _label, centre in OUTPUT_KNOBS]
    for _kit, title, x, y, w, h in cards:
        main, minis = card_knobs(x, y, w, h, title)
        knobs += [(kx, ky, MAIN_R) for kx, ky in main] + [(kx, ky, MINI_R) for kx, ky in minis]
    return {"size": (PANEL_W, PANEL_H),
            "sequencer": (SEQUENCER_SIZE, SEQUENCER_FULL_SIZE, SEQUENCER_ALL_SIZE),
            "chassis": CHASSIS, "screws": SCREWS, "header": HEADER, "wordmark": WORDMARK,
            "status": STATUS, "cards": cards, "title": CARD_TITLE_H, "strip": CARD_STRIP_H, "knobs": knobs, "tick": TICK,
            "lens": DISPLAY_LENS, "digits": DIGITS, "digit": (DIGIT_W, DIGIT_H),
            "mode_leds": MODE_LEDS, "engine_led": ENGINE_LED,
            "buttons": BUTTON_SIZES, "frames": KNOB_FRAMES}


def layout_fingerprint() -> str:
    return hashlib.sha1(json.dumps(plate_geometry(), sort_keys=True).encode()).hexdigest()[:16]


class Skin:
    """Bundled panel artwork, loaded with Tk alone."""

    def __init__(self, master: tk.Misc):
        self.meta = json.loads((ASSETS / "skin.json").read_text())
        self.plate = tk.PhotoImage(master=master, file=ASSETS / "plate.png")
        self.sequencer_plate = tk.PhotoImage(master=master, file=ASSETS / "sequencer.png")
        self.sequencer_full_plate = tk.PhotoImage(master=master, file=ASSETS / "sequencer_full.png")
        self.sequencer_all_plate = tk.PhotoImage(master=master, file=ASSETS / "sequencer_all.png")
        atlas = tk.PhotoImage(master=master, file=ASSETS / "sprites.png")
        self.sprites: dict[str, tuple[tk.PhotoImage, int, int]] = {}
        for name, (x, y, w, h, dx, dy) in self.meta["sprites"].items():
            image = tk.PhotoImage(master=master, width=w, height=h)
            master.tk.call(image, "copy", atlas, "-from", x, y, x + w, y + h, "-to", 0, 0,
                           "-compositingrule", "set")
            self.sprites[name] = (image, dx, dy)

    def place(self, canvas: tk.Canvas, name: str, x: int, y: int) -> int:
        """Draw sprite `name` with its origin (a button's corner, a knob's centre) at x, y."""
        image, dx, dy = self.sprites[name]
        return canvas.create_image(x + dx, y + dy, image=image, anchor="nw")


class MidiClockReceiver:
    """Receive engine clock datagrams off Tk and report gaps from their bounded queues."""

    def __init__(self, path: str, transport: SequencerClock):
        self.path = path
        self.transport = transport
        Path(path).unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        self.socket.bind(path)
        os.chmod(path, 0o600)
        self.socket.settimeout(0.1)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_sequence: int | None = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="drumstation-midi-clock", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = self.socket.recv(64)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) not in (9, 13) or data[:3] != b"\xf0\x7dK" or data[-1] != 0xF7:
                continue
            sequence = int.from_bytes(data[3:7], "big")
            arrival = time.monotonic()
            if len(data) == 13:
                delta_us = int.from_bytes(data[7:11], "big", signed=True)
                event_time = max(0.0, arrival + delta_us / 1_000_000)
                status = data[11]
            else:
                event_time = arrival
                status = data[7]
            if status not in (0xF8, 0xFA, 0xFB, 0xFC):
                continue
            if self.last_sequence is not None:
                delta = (sequence - self.last_sequence) & 0xFFFFFFFF
                if delta != 1:
                    lost = max(1, delta - 1) if delta < 0x80000000 else 1
                    try:
                        self.transport.transport_gap(lost)
                    except Exception as error:
                        self.transport.record_error(error)
                    if status != 0xFA:
                        self.last_sequence = sequence
                        continue
            self.last_sequence = sequence
            try:
                self.transport.external_message(status, event_time)
            except Exception as error:
                self.transport.record_error(error)

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)
        self.socket.close()
        Path(self.path).unlink(missing_ok=True)
        self.thread = None


@dataclass
class Knob:
    label: str
    analog: int
    key: str
    x: int
    y: int
    radius: int
    maximum: int = 127
    track: str | None = None


class SequencerWindow:
    """Hardware-style sequencer with optional full-pattern and all-track grids."""

    WIDTH, HEIGHT = SEQUENCER_SIZE
    STEP_X, STEP_Y, STEP_PITCH, STEP_START = 62, 278, 84, 59
    ALL_GRID_Y, ALL_ROW_PITCH = 282, 19

    def __init__(self, panel: DrumStationPanel):
        self.panel = panel
        self.top = tk.Toplevel(panel.root)
        self.top.title("DrumStation — Step Sequencer")
        self.top.resizable(False, False)
        self.canvas = tk.Canvas(self.top, width=self.WIDTH, height=self.HEIGHT,
                                highlightthickness=0, bg="#141a1b")
        self.canvas.pack(fill="both", expand=True)
        self.all_tracks = False
        self.full_pattern = False
        self.buttons: list[tuple[tuple[int, int, int, int], object]] = []
        self.step_buttons: list[tuple[tuple[int, int, int, int], str, int]] = []
        self.track_labels: list[tuple[tuple[int, int, int, int], str]] = []
        self.cell_bounds: dict[tuple[str, int], tuple[int, int, int, int]] = {}
        self.cell_images: dict[tuple[str, int], int] = {}
        self.cell_lock_lamps: dict[tuple[str, int], int] = {}
        self.playback_text: int | None = None
        self.hover_text: int | None = None
        self._transport_signature: tuple | None = None
        self._current_step: int | None = None
        self.down = None
        self.held_step_bounds = None
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Button-1>", self.press)
        self.top.bind("<ButtonRelease-1>", self.release)
        self.top.bind("<Button-3>", self.press_back)
        self.top.bind("<Motion>", self.hover)
        self.top.bind("<Escape>", lambda _event: panel.cancel_step_gesture())
        self.top.bind("<FocusOut>", panel.clear_lost_focus, add="+")
        self.draw()
        self.top.deiconify()
        self.top.lift()

    def close(self) -> None:
        self.panel.cancel_step_gesture()
        self.panel.sequence_window = None
        self.top.destroy()

    def toggle_view(self, which: str) -> None:
        self.panel.cancel_step_gesture()
        self.held_step_bounds = None
        self.down = None
        if which == "tracks":
            self.all_tracks = not self.all_tracks
        else:
            self.full_pattern = not self.full_pattern
        height = (SEQUENCER_ALL_SIZE[1] if self.all_tracks else
                  SEQUENCER_FULL_SIZE[1] if self.full_pattern else SEQUENCER_SIZE[1])
        self.canvas.configure(height=height)
        x = max(0, self.top.winfo_x())
        y = max(0, min(self.top.winfo_y(), self.top.winfo_screenheight() - height - 60))
        self.top.geometry(f"{self.WIDTH}x{height}+{x}+{y}")
        self.draw()

    def button(self, x: int, y: int, kind: str, label: str, action, *,
               lit: bool = False, red: bool = False, size: int = 10) -> tuple[int, int, int, int]:
        w, h = BUTTON_SIZES[kind]
        bounds = (x, y, x + w, y + h)
        down = self.down == bounds
        style = "red" if red else ("lit" if lit else "plain")
        self.last_button_image = self.panel.skin.place(self.canvas, f"{kind}:{style}:{int(down)}", x, y)
        if label:
            self.canvas.create_text(x + w / 2, y + h / 2 + down, text=label,
                                    fill="#fff4d4" if lit or red else "#d5ddda",
                                    font=(self.panel.sans, -size, "bold"))
        self.buttons.append((bounds, action))
        return bounds

    def text(self, x: int, y: int, value: str, *, fill: str = "#c5d1c9",
             size: int = 10, anchor: str = "center", mono: bool = False) -> int:
        family = self.panel.mono if mono else self.panel.sans
        return self.canvas.create_text(x, y, text=value, fill=fill, anchor=anchor,
                                       font=(family, -size, "bold"))

    def draw(self) -> None:
        if not self.top.winfo_exists():
            return
        c = self.canvas
        c.delete("all")
        if self.all_tracks:
            height, plate = SEQUENCER_ALL_SIZE[1], self.panel.skin.sequencer_all_plate
        elif self.full_pattern:
            height, plate = SEQUENCER_FULL_SIZE[1], self.panel.skin.sequencer_full_plate
        else:
            height, plate = SEQUENCER_SIZE[1], self.panel.skin.sequencer_plate
        c.create_image(0, 0, image=plate, anchor="nw")
        self.buttons, self.step_buttons = [], []
        self.track_labels, self.cell_bounds = [], {}
        self.cell_images, self.cell_lock_lamps = {}, {}
        state = self.panel.sequence_clock.snapshot()
        pattern_index = int(state["edit_pattern"])
        current_pattern = int(state["current_pattern"])
        queued_pattern = state["queued_pattern"]
        pattern = self.panel.sequencer.pattern(pattern_index)
        track = self.panel.selected_track
        kit, title = track.split(":", 1)
        self.text(58, 38, "DRUMSTATION  /  STEP SEQUENCER", fill="#f0d15b", size=15, anchor="w")
        self.text(58, 58, f"TR-{kit}   {title}", fill="#c9d5cb", size=12, anchor="w")
        self.text(1380, 45, "27 VOICES   •   8 PATTERNS   •   64 STEPS",
                  fill="#99aaa4", size=11, anchor="e")

        self.text(58, 111, "PATTERN", fill="#d5c979", size=11, anchor="w")
        for index, letter in enumerate("ABCDEFGH"):
            x = 152 + index * 56
            queued = queued_pattern == index
            self.button(x, 100, "seqpat", letter,
                        lambda _event, i=index: self.panel.select_pattern(i),
                        lit=pattern_index == index and not queued, red=queued, size=11)
        self.text(1040, 112, "VIEW", fill="#d5c979", size=10, anchor="w")
        self.button(1090, 99, "seq", "ALL TRACKS", lambda _event: self.toggle_view("tracks"),
                    lit=self.all_tracks, size=10)
        self.button(1190, 99, "seq", "64 STEPS", lambda _event: self.toggle_view("steps"),
                    lit=self.full_pattern, size=10)

        self.text(58, 169, "TRANSPORT", fill="#d5c979", size=10, anchor="w")
        self.button(58, 186, "seq", "PLAY", lambda _event: self.panel.start_sequence(),
                    lit=not bool(state["playing"]), size=11)
        self.button(154, 186, "seq", "STOP", lambda _event: self.panel.stop_sequence(),
                    red=bool(state["playing"]), size=11)
        source = str(state["clock_source"])
        self.button(900, 99, "seq", "EXT MIDI" if source == "external" else "INTERNAL",
                    lambda _event: self.panel.toggle_clock_source(), lit=source == "external", size=10)
        self.text(290, 169, "TEMPO", fill="#d5c979", size=10, anchor="w")
        self.button(290, 188, "seqctl", "−", lambda event: self.panel.change_tempo(-10 if event.state & 0x0001 else -1))
        self.text(383, 199, f"{float(state['bpm']):.0f} BPM", fill="#fff0bd", size=14, mono=True)
        self.button(449, 188, "seqctl", "+", lambda event: self.panel.change_tempo(10 if event.state & 0x0001 else 1))

        self.text(530, 169, "SCALE", fill="#d5c979", size=10, anchor="w")
        for index, scale in enumerate(SCALES):
            self.button(530 + index * 58, 188, "seqpat", scale,
                        lambda _event, value=scale: self.panel.set_sequence_scale(value),
                        lit=pattern.scale == scale, size=11)

        self.text(730, 169, "PATTERN LENGTH", fill="#d5c979", size=10, anchor="w")
        self.button(730, 188, "seqctl", "−", lambda event: self.panel.change_length(-16 if event.state & 0x0001 else -1))
        self.text(832, 199, f"{pattern.length:02d} STEPS", fill="#fff0bd", size=13, mono=True)
        self.button(904, 188, "seqctl", "+", lambda event: self.panel.change_length(16 if event.state & 0x0001 else 1))

        self.text(986, 169, "PAGE", fill="#d5c979", size=10, anchor="w")
        for page in range(PAGE_COUNT):
            x = 986 + page * 52
            self.button(x, 188, "seqpage", f"{page + 1}",
                        lambda _event, p=page: self.panel.select_sequence_page(p),
                        lit=self.panel.sequence_page == page, size=11)

        self.text(720, 99, "PLAYBACK", fill="#d5c979", size=10, anchor="w")
        self.playback_text = self.text(720, 118, "", fill="#98a49d", size=11, anchor="w")
        self._draw_grid(pattern_index, pattern, track)
        self._draw_help(height)
        self._transport_signature = (state["playing"], current_pattern, queued_pattern, pattern_index,
                                     state["clock_source"], state["clock_status"], state["overruns"])
        self._draw_playhead(state)

    def _large_step(self, x: int, y: int, track: str, step: int, label: str,
                    pattern_index: int, pattern) -> None:
        state = pattern.tracks.get(track)
        has_trig = bool(state and step in state.trigs)
        has_lock = bool(state and state.locks.get(step))
        bounds = self.button(x, y, "seqstep", label, lambda _event: None,
                             lit=has_trig and not has_lock, red=has_lock, size=11)
        self.step_buttons.append((bounds, track, step))
        self.cell_bounds[(track, step)] = bounds
        self.cell_images[(track, step)] = self.last_button_image
        if has_lock:
            self.cell_lock_lamps[(track, step)] = self.panel.skin.place(self.canvas, "led:red", x + 10, y + 8)
        if self.panel.selected_trig == (pattern_index, track, step):
            self.canvas.create_rectangle(x + 2, y + 2, x + self.STEP_X - 2, y + 44,
                                         outline="#e7ca62", width=1, tags="selected_trig")

    def _draw_grid(self, pattern_index: int, pattern, track: str) -> None:
        c = self.canvas
        if self.all_tracks:
            full = self.full_pattern
            x0, pitch, kind = (224, 18, "seqcell") if full else (280, 68, "seqrow")
            cell_w, cell_h = BUTTON_SIZES[kind]
            steps = range(64) if full else range(self.panel.sequence_page * 16,
                                                  self.panel.sequence_page * 16 + 16)
            self.text(59, 258, "ALL 27 TRACKS", fill="#d5c979", size=11, anchor="w")
            if full:
                for page in range(4):
                    centre = x0 + (page * 16 + 7.5) * pitch + cell_w / 2
                    self.text(int(centre), 253, f"PAGE {page + 1}", fill="#d5c979", size=10)
                for step in steps:
                    if step % 4 == 0:
                        self.text(x0 + step * pitch + cell_w // 2, 268,
                                  f"{step + 1:02d}", fill="#aebdb5", size=9)
                for page in range(1, 4):
                    divider = x0 + page * 16 * pitch - 1
                    c.create_line(divider, 278, divider, 797, fill="#796d49", width=1)
            else:
                for col, step in enumerate(steps):
                    self.text(x0 + col * pitch + cell_w // 2, 264,
                              f"{step + 1:02d}", fill="#c4d0c7", size=11)
                for beat in range(1, 4):
                    divider = x0 + beat * 4 * pitch - 6
                    c.create_line(divider, 277, divider, 797, fill="#59645e", width=1)
            for row, row_track in enumerate(TRACK_IDS):
                y = self.ALL_GRID_Y + row * self.ALL_ROW_PITCH
                if row_track == self.panel.selected_track:
                    c.create_rectangle(54, y - 2, 1384, y + 17,
                                       fill="#303326", outline="")
                kit, title = row_track.split(":", 1)
                self.text(59, y + 7, f"{kit}  {title}",
                          fill="#f0d15b" if row_track == self.panel.selected_track else "#c6d1ca",
                          size=10, anchor="w")
                self.track_labels.append(((54, y - 2, x0 - 8, y + 17), row_track))
                row_state = pattern.tracks.get(row_track)
                trigs = row_state.trigs if row_state else set()
                locks = row_state.locks if row_state else {}
                for col, step in enumerate(steps):
                    x = x0 + col * pitch
                    bounds = (x, y, x + cell_w, y + cell_h)
                    style = "red" if locks.get(step) else "lit" if step in trigs else "plain"
                    down = int(self.down == bounds)
                    item = self.panel.skin.place(c, f"{kind}:{style}:{down}", x, y)
                    self.step_buttons.append((bounds, row_track, step))
                    self.cell_bounds[(row_track, step)] = bounds
                    self.cell_images[(row_track, step)] = item
                    if self.panel.selected_trig == (pattern_index, row_track, step):
                        c.create_rectangle(x - 1, y - 1, x + cell_w + 1, y + cell_h + 1,
                                           outline="#f3db78", width=1, tags="selected_trig")
                if row == 15:
                    c.create_line(54, y + 17, 1384, y + 17, fill="#796d49", width=1)
            return

        if self.full_pattern:
            self.text(59, 258, "FULL PATTERN  •  64 STORED STEPS", fill="#d5c979", size=11, anchor="w")
            self.text(1381, 258, f"PLAY LENGTH {pattern.length:02d}",
                      fill="#aebdb5", size=10, anchor="e")
            for page in range(4):
                y = 280 + page * 59
                self.text(59, y + 23, f"PAGE {page + 1}",
                          fill="#f0d15b" if self.panel.sequence_page == page else "#aebdb5",
                          size=11, anchor="w")
                for visible in range(16):
                    step = page_step(page, visible)
                    self._large_step(180 + visible * 73, y, track, step, f"{step + 1:02d}",
                                     pattern_index, pattern)
                for visible in (3, 7, 11):
                    divider = 180 + visible * 73 + self.STEP_X + 5
                    c.create_line(divider, y + 2, divider, y + 44, fill="#65716c", width=1)
            return

        page_start = self.panel.sequence_page * 16 + 1
        self.text(self.STEP_START, 258, f"STEPS {page_start:02d}–{page_start + 15:02d}",
                  fill="#d5c979", size=11, anchor="w")
        self.text(1381, 258, f"PAGE {self.panel.sequence_page + 1} / 4",
                  fill="#aebdb5", size=10, anchor="e")
        for visible in range(16):
            step = page_step(self.panel.sequence_page, visible)
            x = self.STEP_START + visible * self.STEP_PITCH
            self._large_step(x, self.STEP_Y, track, step, f"{visible + 1:02d}",
                             pattern_index, pattern)
            if visible in (3, 7, 11):
                divider = x + self.STEP_X + 11
                c.create_line(divider, self.STEP_Y + 2, divider, self.STEP_Y + 43,
                              fill="#65716c", width=1)

    def _draw_help(self, height: int) -> None:
        y = height - 87
        for x, lamp, label in ((59, "led:amber", "TRIG"), (177, "led:red", "PARAMETER LOCK"),
                               (391, "led:green", "PLAYHEAD")):
            self.panel.skin.place(self.canvas, lamp, x, y + 16)
            self.text(x + 16, y + 16, label, fill="#cbd4ce", size=10, anchor="w")
        self.hover_text = self.text(1381, y + 16, "", fill="#f0d15b", size=10, anchor="e")
        self.text(59, y + 37,
                  "STEP  Click to toggle    •    LOCK  Hold step + wheel over its voice knob    •    EDIT  Shift/Ctrl-drag a selected trig's knob",
                  fill="#b9c8c0", size=11, anchor="w")
        self.text(59, y + 56,
                  "CLEAR  Right-click step or knob    •    TRACK  Click a card or track row    •    PATTERN  Queues at loop end    •    SHIFT  Tempo ±10 / length ±16",
                  fill="#b9c8c0", size=11, anchor="w")

    def _draw_playhead(self, state: dict) -> None:
        c = self.canvas
        c.delete("seqplayhead")
        step = state["current_step"]
        if state["playing"] and step is not None:
            text = f"RUNNING {chr(ord('A') + int(state['current_pattern']))}  •  STEP {int(step) + 1:02d}"
            if state["queued_pattern"] is not None:
                text += f"  •  NEXT {chr(ord('A') + int(state['queued_pattern']))}"
            fill = "#9ee5aa"
            pattern_x = 152 + int(state["current_pattern"]) * 56
            item = self.panel.skin.place(c, "led:green", pattern_x + 21, 129)
            c.addtag_withtag("seqplayhead", item)
            if int(state["current_pattern"]) == int(state["edit_pattern"]):
                page = int(step) // 16
                item = self.panel.skin.place(c, "led:green", 986 + page * 52 + 14, 216)
                c.addtag_withtag("seqplayhead", item)
                if self.all_tracks:
                    first = self.cell_bounds.get((TRACK_IDS[0], int(step)))
                    last = self.cell_bounds.get((TRACK_IDS[-1], int(step)))
                    if first is not None and last is not None:
                        c.create_rectangle(first[0] - 2, first[1] - 2, first[2] + 2, last[3] + 2,
                                           outline="#56d878", width=1, tags="seqplayhead")
                else:
                    bounds = self.cell_bounds.get((self.panel.selected_track, int(step)))
                    if bounds is not None:
                        item = self.panel.skin.place(c, "led:green", bounds[2] - 10, bounds[1] + 8)
                        c.addtag_withtag("seqplayhead", item)
        else:
            text = f"STOPPED  •  LAST STEP {int(step) + 1:02d}" if step is not None else "STOPPED"
            fill = "#98a49d"
        text += f"  •  {state['clock_status']}"
        if state["overruns"]:
            text += f"  •  OVERRUNS {int(state['overruns'])}"
        if self.playback_text is not None:
            c.itemconfigure(self.playback_text, text=text, fill=fill)
        self._current_step = int(step) if step is not None else None

    def refresh_playhead(self) -> None:
        state = self.panel.sequence_clock.snapshot()
        signature = (state["playing"], state["current_pattern"],
                     state["queued_pattern"], state["edit_pattern"],
                     state["clock_source"], state["clock_status"], state["overruns"])
        if signature != self._transport_signature:
            self.draw()
        elif state["current_step"] != self._current_step:
            self._draw_playhead(state)

    def refresh_step(self, track: str, step: int) -> None:
        key = (track, step)
        bounds = self.cell_bounds.get(key)
        image = self.cell_images.get(key)
        if bounds is None or image is None:
            return
        state = self.panel.sequencer.pattern().tracks.get(track)
        has_trig = bool(state and step in state.trigs)
        has_lock = bool(state and state.locks.get(step))
        kind = "seqcell" if self.all_tracks and self.full_pattern else "seqrow" if self.all_tracks else "seqstep"
        style = "red" if has_lock else "lit" if has_trig else "plain"
        sprite = self.panel.skin.sprites[f"{kind}:{style}:{int(self.down == bounds)}"][0]
        self.canvas.itemconfigure(image, image=sprite)
        lamp = self.cell_lock_lamps.pop(key, None)
        if lamp is not None:
            self.canvas.delete(lamp)
        if has_lock and not self.all_tracks:
            self.cell_lock_lamps[key] = self.panel.skin.place(
                self.canvas, "led:red", bounds[0] + 10, bounds[1] + 8)
        self.canvas.delete("selected_trig")
        selected = self.panel.selected_trig
        if selected is not None and selected[0] == self.panel.sequencer.edit_pattern:
            selected_bounds = self.cell_bounds.get((selected[1], selected[2]))
            if selected_bounds is not None:
                x0, y0, x1, y1 = selected_bounds
                inset = 1 if self.all_tracks else 2
                self.canvas.create_rectangle(x0 - inset if self.all_tracks else x0 + inset,
                                             y0 - inset if self.all_tracks else y0 + inset,
                                             x1 + inset if self.all_tracks else x1 - inset,
                                             y1 + inset if self.all_tracks else y1 - inset,
                                             outline="#f3db78", width=1, tags="selected_trig")

    def hover(self, event: tk.Event) -> None:
        if self.hover_text is None:
            return
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        label = ""
        for bounds, track, step in self.step_buttons:
            if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                kit, title = track.split(":", 1)
                count = len(self.panel.sequencer.locks_for(track, step))
                label = f"TR-{kit} {title}  •  STEP {step + 1:02d}"
                if count:
                    label += f"  •  {count} LOCK{'S' if count != 1 else ''}"
                break
        self.canvas.itemconfigure(self.hover_text, text=label)

    def press(self, event: tk.Event) -> None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for bounds, track, step in reversed(self.step_buttons):
            x0, y0, x1, y1 = bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.down = bounds
                self.held_step_bounds = bounds
                if track != self.panel.selected_track:
                    self.panel.selected_track = track
                    self.panel.draw()
                self.panel.begin_step_gesture(step)
                self.refresh_step(track, step)
                return
        for bounds, track in self.track_labels:
            if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                self.panel.selected_track = track
                self.panel.draw()
                self.draw()
                return
        for bounds, action in reversed(self.buttons):
            x0, y0, x1, y1 = bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.down = bounds
                action(event)
                self.draw()
                return

    def release(self, event: tk.Event) -> None:
        changed_step = None
        if self.panel.held_trig is not None:
            track, step = self.panel.held_trig[1:]
            changed_step = (track, step)
            x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
            same_step = (self.held_step_bounds is not None and
                         self.held_step_bounds[0] <= x <= self.held_step_bounds[2] and
                         self.held_step_bounds[1] <= y <= self.held_step_bounds[3])
            self.panel.end_step_gesture(same_step)
        self.held_step_bounds = None
        self.down = None
        if changed_step is not None:
            self.refresh_step(*changed_step)
        else:
            self.draw()

    def press_back(self, event: tk.Event) -> None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for bounds, track, step in reversed(self.step_buttons):
            x0, y0, x1, y1 = bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                if track != self.panel.selected_track:
                    self.panel.selected_track = track
                    self.panel.draw()
                    self.draw()
                self.panel.show_lock_menu(self.panel.sequencer.edit_pattern,
                                          track, step,
                                          event.x_root, event.y_root)
                return


class DrumStationPanel:
    def __init__(self, root: tk.Tk, eeprom: Path = EEPROM, settings: Path | None = SETTINGS,
                 overrides: dict[str, str] | None = None, sequencer_path: Path | None = SEQUENCER_DATA,
                 engine_socket: Path | None = None):
        self.root = root
        self.eeprom = eeprom
        self.settings = settings
        self.process: subprocess.Popen[str] | None = None
        token = str(os.getpid())
        self.control_path = f"/tmp/drumstation-panel-{token}.sock"
        self.reply_path = f"/tmp/drumstation-panel-reply-{token}.sock"
        self.sequence_path = f"/tmp/drumstation-sequencer-{token}.sock"
        self.clock_path = f"/tmp/drumstation-clock-{token}.sock"
        # In the elektremu studio the board runs in the studio's engine (gm-engine), which serves
        # this panel's protocol on a socket of its own: talk to that, and start no engine here.
        self.engine_socket = engine_socket
        if engine_socket is not None:
            self.control_path = str(engine_socket)
        Path(self.reply_path).unlink(missing_ok=True)
        self.sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sender.bind(self.reply_path)
        self.sender.settimeout(0.16)
        Path(self.sequence_path).unlink(missing_ok=True)
        self.sequence_sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sequence_sender.bind(self.sequence_path)
        os.chmod(self.sequence_path, 0o600)
        self.sequence_sender.setblocking(False)
        self._clock_registration_due = 0.0
        # midi_input is a sequencer port as CLIENT:PORT names, or "" for none.
        self.audio, self.midi_input = "default", ""
        self.channel, self.rate, self.period = 15, 44100, 128
        self.load_settings()
        # A launcher's choices (--audio, --midi-input) apply to this run only.
        self.overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
        self.audio = self.overrides.get("audio", self.audio)
        self.midi_input = self.overrides.get("midi_input", self.midi_input)
        self.channel_override = False
        self.program, self.mode = 0, 0
        self.display_segments = (0xBE, 0xBE)
        self.program_image: bytes | None = None
        self.utility_function = 0
        self.values = {f"analog:{i}": 64 for i in range(22)}
        self.values["analog:0"] = 127
        self.values.update(boost=64, comp=0, reverb=0, room=0)
        self.sequencer = SequencerProject(sequencer_path, TRACK_IDS)
        self.sequencer.set_base_values(self.values)
        self.sequence_clock = SequencerClock(self.sequencer, self.send_sequence_event)
        self.clock_receiver = MidiClockReceiver(self.clock_path, self.sequence_clock)
        self.clock_receiver.start()
        self.sequence_window: SequencerWindow | None = None
        self.selected_track = TRACK_IDS[0]
        self.sequence_page = 0
        self.selected_trig: tuple[int, str, int] | None = None
        self.held_trig: tuple[int, str, int] | None = None
        self.held_trig_started = 0.0
        self.held_trig_consumed = False
        self.lock_display: dict[str, int] = {}
        self.lock_save_after: str | None = None
        self.knob_visuals: dict[tuple[str | None, str], tuple[int, int, Knob]] = {}
        self.drag_lock_target: tuple[int, str, int] | None = None
        self.output_sent = False
        self.knobs: list[Knob] = []
        self.cards: list[tuple[str, tuple[int, int, int, int]]] = []
        self.button_hits: list[tuple[tuple[int, int, int, int], object]] = []
        self.back_hits: list[tuple[tuple[int, int, int, int], object]] = []
        self.drag: Knob | None = None
        self.drag_origin_y = self.drag_origin_value = 0
        self.pending_drag: tuple[Knob, int, tuple[int, str, int] | None] | None = None
        self.drag_flush_scheduled = False
        self.last_edit = 0.0
        self.write_enabled = False
        self.audition_held = False
        self.restart_pending = False
        self.demo_stop = threading.Event()
        self.demo_thread: threading.Thread | None = None
        self.demo_updates: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.closed = False
        self.status = "STARTING REALTIME ENGINE"
        self.skin = Skin(root)
        self.down: tuple[int, int, int, int] | None = None
        # Pixel-sized fonts keep captions at the plate's scale on any display.
        families = set(tkfont.families(root))
        self.sans = next((f for f in ("Roboto Condensed", "Liberation Sans Narrow") if f in families), "Helvetica")
        self.mono = next((f for f in ("DejaVu Sans Mono", "Liberation Mono") if f in families), "Courier")
        self.canvas = tk.Canvas(root, width=PANEL_W, height=PANEL_H, highlightthickness=0, bg="#101415")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<Button-3>", self.press_back)
        self.canvas.bind("<B1-Motion>", self.drag_knob)
        self.canvas.bind("<ButtonRelease-1>", self.release_drag)
        self.root.bind("<KeyPress-space>", self.hold_audition)
        self.root.bind("<KeyRelease-space>", self.release_audition)
        self.root.bind("<FocusOut>", self.clear_lost_focus, add="+")
        self.root.bind("<Escape>", lambda _event: self.cancel_step_gesture())
        self.root.bind_all("<MouseWheel>", self.mouse_wheel, add="+")
        self.root.bind_all("<Button-4>", self.mouse_wheel, add="+")
        self.root.bind_all("<Button-5>", self.mouse_wheel, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.draw()
        self.poll()
        self.root.after(50, self.start_engine)
        self.root.after(250, self.poll_display)

    def is_running(self) -> bool:
        if self.engine_socket is not None:
            return self.engine_socket.exists()
        return self.process is not None and self.process.poll() is None

    def send(self, data: bytes) -> bool:
        if not self.is_running():
            return False
        try:
            self.sender.sendto(data, self.control_path)
            return True
        except OSError as error:
            self.status = f"PANEL LINK ERROR  •  {error.strerror or error}"
            return False

    def open_sequencer(self, track: str | None = None) -> None:
        if track in TRACK_IDS:
            self.selected_track = track
        if self.sequence_window is None or not self.sequence_window.top.winfo_exists():
            self.sequence_window = SequencerWindow(self)
        else:
            self.sequence_window.top.deiconify()
            self.sequence_window.top.lift()
            self.sequence_window.draw()
        self.status = f"SEQUENCER  •  TR-{self.selected_track.replace(':', ' ')}"
        self.draw()

    def select_sequence_track(self, track: str) -> None:
        if track not in TRACK_IDS:
            return
        self.selected_track = track
        self.open_sequencer(track)

    def select_sequence_page(self, page: int) -> None:
        if 0 <= page < PAGE_COUNT:
            self.sequence_page = page
            self.draw_sequence()

    def select_pattern(self, index: int) -> None:
        self.sequencer.select_pattern(index)
        self.draw_sequence()
        self.draw()

    def change_tempo(self, change: int) -> None:
        self.sequencer.set_bpm(self.sequencer.snapshot()["bpm"] + change)
        self.draw_sequence()

    def change_length(self, change: int) -> None:
        self.sequencer.set_length(self.sequencer.pattern().length + change)
        self.draw_sequence()

    def set_sequence_scale(self, scale: str) -> None:
        self.sequencer.set_scale(scale)
        self.draw_sequence()

    def draw_sequence(self) -> None:
        if self.sequence_window is not None and self.sequence_window.top.winfo_exists():
            self.sequence_window.draw()

    def toggle_clock_source(self) -> None:
        source = "external" if self.sequencer.snapshot()["clock_source"] == "internal" else "internal"
        self.sequence_clock.set_source(source)
        self.status = "CLOCK SOURCE  •  EXTERNAL MIDI" if source == "external" else "CLOCK SOURCE  •  INTERNAL"
        self.draw_sequence()
        self.draw()

    def _register_clock_receiver(self) -> None:
        now = time.monotonic()
        if now < self._clock_registration_due or not self.is_running():
            return
        self._clock_registration_due = now + 1.0
        data = b"\xf0\x7dU" + os.fsencode(self.clock_path) + b"\xf7"
        try:
            self.sequence_sender.sendto(data, self.control_path)
        except OSError:
            # The engine may still be opening or restarting; the next poll retries.
            pass

    def start_sequence(self) -> None:
        if not self.is_running():
            self.status = "ENGINE IS STARTING  •  TRY PLAY AGAIN IN A MOMENT"
            self.draw()
            return
        if self.program_image is None:
            self.status = "READING FIRMWARE STATE  •  TRY PLAY AGAIN IN A MOMENT"
            self.draw()
            return
        if self.demo_thread is not None and self.demo_thread.is_alive():
            self.demo_stop.set()
            self.demo_thread.join(timeout=0.5)
        try:
            started = self.sequence_clock.start()
            state = self.sequence_clock.snapshot()
            if state["clock_source"] == "external" and not state["external_started"]:
                self.status = "EXTERNAL MIDI  •  WAITING FOR START"
            elif started:
                self.status = "SEQUENCER PLAYING"
            else:
                self.status = "SEQUENCER ALREADY PLAYING OR WAITING FOR EXTERNAL START"
        except RuntimeError as error:
            self.status = f"SEQUENCER  •  {error}"
        self.draw_sequence()
        self.draw()

    def stop_sequence(self) -> None:
        self.sequence_clock.stop()
        self.status = "SEQUENCER STOPPED"
        self.draw_sequence()
        self.draw()

    def _sequence_record(self, key: str, value: int, base: int | None = None) -> list[int] | None:
        locking = base is not None
        value = max(0, min(127, int(value)))
        if key.startswith("cc:"):
            cc = int(key[3:])
            if locking:
                return [0x11, self.channel - 1, cc, value, max(0, min(127, int(base)))]
            return [0x10, self.channel - 1, cc, value]
        if key.startswith("level:"):
            address, mask = int(key[6:], 16), 0x7f
        elif key.startswith("pan:"):
            address, mask = int(key[4:], 16), 0x0f
        elif key.startswith("dist:"):
            address, mask = int(key[5:], 16), 0xf0
            value <<= 4
            if locking:
                base = int(base) << 4
        elif key.startswith("cut:"):
            address, mask = int(key[4:], 16), 0x7f
        else:
            return None
        if locking:
            return [0x21, address >> 8, address & 0xff, value, int(base), mask]
        return [0x20, address >> 8, address & 0xff, value, mask]

    def send_sequence_event(self, event: StepEvent) -> None:
        """Encode one ordered firmware batch; safe to call from the clock worker."""
        records: list[list[int]] = []
        for key, value in event.restore.items():
            record = self._sequence_record(key, value)
            if record is not None:
                records.append(record)
        for hit in event.hits:
            for key, value in hit.locks.items():
                base = self.sequencer.base_value(key, 64)
                record = self._sequence_record(key, value, base)
                if record is not None:
                    records.append(record)
        for hit in event.hits:
            kit, title = hit.track.split(":", 1)
            note = hit_note(kit, title, self.sequencer.base_value("gm", 1))
            records.append([0x30, self.channel - 1, note, 110])
        if not records:
            return
        if len(records) > 255:
            raise RuntimeError("sequencer event exceeds the 255 operation limit")
        packet = bytearray((0xf0, 0x7d, ord("T"), len(records)))
        for record in records:
            packet.extend(record)
        packet.append(0xf7)
        try:
            self.sequence_sender.sendto(packet, self.control_path)
        except OSError as error:
            raise RuntimeError(f"sequencer panel link failed: {error}") from error

    def _target_for_knob(self, knob: Knob) -> tuple[int, str, int] | None:
        target = self.held_trig or self.selected_trig
        if target is None or target[1] != knob.track or target[0] != self.sequencer.edit_pattern:
            return None
        if not self.sequencer.trig_on(target[1], target[2], target[0]):
            return None
        return target

    def begin_step_gesture(self, step: int) -> None:
        pattern = self.sequencer.edit_pattern
        self.held_trig = (pattern, self.selected_track, step)
        self.held_trig_started = time.monotonic()
        self.held_trig_consumed = False
        self.lock_display.clear()

    def end_step_gesture(self, released_on_grid: bool) -> None:
        target = self.held_trig
        if target is None:
            return
        pattern, track, step = target
        elapsed = time.monotonic() - self.held_trig_started
        if step_gesture_toggles(self.held_trig_consumed, elapsed, released_on_grid):
            self.sequencer.toggle_trig(track, step, pattern)
            if self.sequencer.trig_on(track, step, pattern):
                self.selected_trig = target
            elif self.selected_trig == target:
                self.selected_trig = None
        else:
            self.selected_trig = target
        self.held_trig = None
        feedback = [visual[2] for (_track, key), visual in self.knob_visuals.items()
                    if key in self.lock_display]
        self.lock_display.clear()
        for knob in feedback:
            self.draw_lock_feedback(knob)

    def cancel_step_gesture(self) -> None:
        if self.held_trig is not None:
            self.held_trig = None
            self.held_trig_consumed = True
        self.lock_display.clear()
        self.drag_lock_target = None
        self.drag = None
        self.draw_sequence()
        self.draw()

    def clear_lost_focus(self, _event: tk.Event | None = None) -> None:
        def check_focus() -> None:
            focused = self.root.focus_get()
            if focused is None:
                self.cancel_step_gesture()
                return
            try:
                top = focused.winfo_toplevel()
                if top is not self.root and (self.sequence_window is None or top is not self.sequence_window.top):
                    self.cancel_step_gesture()
            except tk.TclError:
                self.cancel_step_gesture()
        self.root.after_idle(check_focus)

    def mouse_wheel(self, event: tk.Event) -> str | None:
        if self.held_trig is None:
            return None
        if getattr(event, "num", None) == 4:
            delta = 1
        elif getattr(event, "num", None) == 5:
            delta = -1
        else:
            delta = 1 if getattr(event, "delta", 0) > 0 else -1
        x = event.x_root - self.canvas.winfo_rootx()
        y = event.y_root - self.canvas.winfo_rooty()
        for knob in self.knobs:
            if (x - knob.x) ** 2 + (y - knob.y) ** 2 <= knob.radius ** 2:
                if knob.track != self.held_trig[1]:
                    return "break"
                self.adjust_lock(knob, delta * (5 if event.state & 0x0001 else 1), self.held_trig)
                return "break"
        return "break"

    def adjust_lock(self, knob: Knob, delta: int, target: tuple[int, str, int]) -> None:
        pattern, track, step = target
        current = self.lock_display.get(knob.key, self.sequencer.locks_for(track, step, pattern).get(
            knob.key, self.values.get(knob.key, 64)))
        value = max(0, min(knob.maximum, current + delta))
        if value == current and knob.key in self.lock_display:
            return
        self.sequencer.set_lock(track, step, knob.key, value, pattern, save=False)
        self.schedule_lock_save()
        self.lock_display[knob.key] = value
        self.held_trig_consumed = True
        self.draw_lock_feedback(knob)

    def schedule_lock_save(self) -> None:
        if self.lock_save_after is not None:
            self.root.after_cancel(self.lock_save_after)
        self.lock_save_after = self.root.after(350, self.flush_lock_save)

    def flush_lock_save(self) -> None:
        self.lock_save_after = None
        self.sequencer.save()

    def draw_lock_feedback(self, knob: Knob) -> None:
        visual = self.knob_visuals.get((knob.track, knob.key))
        if visual is None:
            return
        image_item, value_item, _ = visual
        value = self.draw_knob_value(knob.key)
        frame = round(min(value, knob.maximum) * (KNOB_FRAMES - 1) / knob.maximum)
        self.canvas.itemconfigure(image_item, image=self.skin.sprites[f"knob{knob.radius}:{frame}"][0])
        self.canvas.itemconfigure(value_item, text=(f"{knob.label} {value}" if knob.radius == MINI_R
                                                     else f"{value:03d}"))

    def show_lock_menu(self, pattern: int, track: str, step: int, x: int, y: int) -> None:
        locks = self.sequencer.locks_for(track, step, pattern)
        menu = tk.Menu(self.root, tearoff=False)
        if locks:
            for key in sorted(locks):
                label = next((knob.label for knob in self.knobs if knob.track == track and knob.key == key), key)
                menu.add_command(label=f"Remove {label} lock", command=lambda k=key: self.remove_lock(pattern, track, step, k))
            menu.add_separator()
            menu.add_command(label="Clear all locks", command=lambda: self.clear_locks(pattern, track, step))
        else:
            menu.add_command(label="No parameter locks", state="disabled")
        menu.tk_popup(x, y)

    def remove_lock(self, pattern: int, track: str, step: int, key: str) -> None:
        self.sequencer.remove_lock(track, step, key, pattern)
        self.lock_display.pop(key, None)
        self.draw_sequence()
        self.draw()

    def clear_locks(self, pattern: int, track: str, step: int) -> None:
        self.sequencer.clear_locks(track, step, pattern)
        self.lock_display.clear()
        self.draw_sequence()
        self.draw()

    def draw_knob_value(self, key: str) -> int:
        return self.lock_display.get(key, self.values.get(key, 64))

    def panel_analog(self, channel: int, value: int, refresh: bool = True) -> None:
        value = max(0, min(127, value))
        if self.send(bytes((0xF0, 0x7D, ord("A"), channel, value, 0xF7))):
            self.status = f"PANEL CONTROL {channel + 1:02d}  •  {value:03d}"
            if refresh:
                self.draw()

    def panel_button(self, port: int, bit: int = 4, hold_ms: int = 75) -> None:
        if self.send(bytes((0xF0, 0x7D, ord("B"), port, bit, 1, 0xF7))):
            self.root.after(hold_ms, lambda: self.send(bytes((0xF0, 0x7D, ord("B"), port, bit, 0, 0xF7))))

    def panel_key(self, row: int, column: int, hold_ms: int = 180) -> None:
        if self.send(bytes((0xF0, 0x7D, ord("K"), row, column, 1, 0xF7))):
            self.root.after(hold_ms, lambda: self.send(bytes((0xF0, 0x7D, ord("K"), row, column, 0, 0xF7))))

    def send_output(self, key: str, value: int) -> None:
        """Set one output-stage knob in the engine: the boost or a master effect."""
        value = max(0, min(127, value))
        if key == "boost":
            if self.send(bytes((0xF0, 0x7D, ord("G"), value, 0xF7))):
                self.status = f"POST-DSP BOOST  •  {value:03d}  •  unity 016  •  max about +18 dB"
        elif self.send(bytes((0xF0, 0x7D, ord("X"), ("comp", "reverb", "room").index(key), value, 0xF7))):
            if key == "room":
                self.status = ("MASTER REVERB  •  TIGHT ROOM  •  short, no lows in the room" if value else
                               "MASTER REVERB  •  BIG ROOM  •  longer, bass drum rumbles in it")
            else:
                name = "MASTER COMPRESSOR" if key == "comp" else "MASTER REVERB"
                self.status = f"{name}  •  {value:03d}" if value else f"{name}  •  OFF"

    def toggle_room(self) -> None:
        self.values["room"] ^= 1
        self.send_output("room", self.values["room"])
        self.draw()

    def request_page(self, page: int) -> bytes | None:
        if not self.is_running():
            return None
        try:
            self.sender.sendto(bytes((0xF0, 0x7D, ord("Q"), page, 0xF7)), self.control_path)
            reply, _ = self.sender.recvfrom(512)
            if len(reply) == 261 and reply[:3] == bytes((0xF0, 0x7D, ord("R"))) and reply[3] == page:
                return reply[4:-1]
        except OSError:
            return None
        return None

    def refresh_firmware_state(self) -> None:
        page0, page1, page2, system = self.request_page(0), self.request_page(1), self.request_page(2), self.request_page(0xb7)
        if page0 is None or page1 is None or page2 is None or system is None:
            return
        if not self.output_sent:
            self.output_sent = True
            # The studio's engine keeps them (BOOST its gain for this unit, COMP and REVERB its
            # sends, ROOM its reverb's) and answers page FD with them: show those, not reset them.
            stage = self.request_page(0xFD) if self.engine_socket is not None else None
            if stage is not None:
                self.values.update(boost=stage[0], comp=stage[1], reverb=stage[2], room=stage[3] & 1)
            else:
                # A new engine starts at its own defaults, and only now is it
                # known to be listening: give it the output knobs as they stand.
                for key in ("room", *(key for key, _label, _centre in OUTPUT_KNOBS)):
                    self.send_output(key, self.values[key])
        self.sync_firmware_display(page0, page2)
        self.sync_receive_channel(system)
        self.program_image = page1[:0x90]
        transient = self.sequencer.snapshot()["transient_keys"]
        self.values["analog:0"] = page0[0x92] & 0x7F
        for cc, address in CC_ADDRESS.items():
            key = f"cc:{cc}"
            if key not in transient:
                self.values[key] = page1[address - 0x100] & 0x7F
        for addresses in LEVEL_ADDRESS.values():
            for address in addresses:
                key = f"level:{address:03x}"
                if key not in transient:
                    self.values[key] = page1[address - 0x100] & 0x7F
        for address in range(0x100, 0x188):
            self.values[f"flag:{address:03x}"] = page1[address - 0x100] >> 7
        for addresses in PAN_ADDRESS.values():
            for address in addresses:
                byte = page1[address - 0x100]
                for key, value in ((f"pan:{address:03x}", byte & 0x0F),
                                   (f"dist:{address:03x}", byte >> 4),
                                   (f"cut:{address - 1:03x}", min(99, page1[address - 0x101] & 0x7F))):
                    if key not in transient:
                        self.values[key] = value
        for bank in range(4):
            self.values[f"bank:{bank}"] = page1[0x81 + bank]
        self.values["gm"] = page1[0x8B]
        self.sequencer.set_base_values(self.values)
        if self.demo_thread is None or not self.demo_thread.is_alive():
            self.status = f"FIRMWARE MODE {self.mode + 1}  •  controls read from program state"
        self.draw()

    def sync_firmware_display(self, page0: bytes, page2: bytes) -> bool:
        old = (self.mode, self.program, self.utility_function, self.display_segments)
        mode = page0[0x8e] & 7
        if mode <= 4:
            self.mode = mode
        if page2[0x1d] < 40:
            self.program = page2[0x1d]
        self.display_segments = (page2[0x37], page2[0x38])
        if self.mode == 3:
            self.utility_function = {0x51: 1, 0x52: 2, 0x53: 3, 0x54: 4,
                                     0x55: 5, 0x56: 6, 0x58: 7, 0x59: 8,
                                     0x57: 9}.get(page0[0x8c], self.utility_function)
        return old != (self.mode, self.program, self.utility_function, self.display_segments)

    def sync_receive_channel(self, system: bytes) -> None:
        channel = system[0xfe]
        if 1 <= channel <= 16:
            self.channel = channel

    def poll_display(self) -> None:
        if not self.closed and self.is_running():
            page0, page1, page2 = self.request_page(0), self.request_page(1), self.request_page(2)
            if page0 is not None and page1 is not None and page2 is not None:
                previous_program = self.program
                if self.sync_firmware_display(page0, page2):
                    self.draw()
                # Edits reach firmware through the UART queue; while one is in
                # flight, program RAM can briefly lag the knob being dragged.
                settled = (self.drag is None and not self.drag_flush_scheduled and
                           time.monotonic() - self.last_edit > 0.3)
                if (self.program != previous_program or settled and
                    (page1[:0x90] != self.program_image or
                     (page0[0x92] & 0x7F) != self.values["analog:0"])):
                    self.refresh_firmware_state()
                elif self.mode == 1:
                    system = self.request_page(0xb7)
                    if system is not None:
                        self.sync_receive_channel(system)
        if not self.closed:
            self.root.after(180, self.poll_display)

    def keypad(self, index: int) -> None:
        label, action = KEYS[index]
        self.panel_key(*KEY_POSITIONS[index])
        if self.mode == 3 and index == 8 and not self.audition_held:
            self.toggle_demo()
            return
        # Program recall depends on the WRITE switch; read the result back
        # from firmware rather than predicting it.
        self.status = f"DATA ENTRY  •  {label}  •  {action}"
        self.draw()
        self.root.after(230, self.refresh_firmware_state)

    def next_mode(self) -> None:
        self.mode = (self.mode + 1) % 5
        self.panel_key(3, 0)
        self.status = ("PROGRAM", "MIDI RX CH.", "MIDI TX CH.", "UTILITY", "SAVE")[self.mode] + " MODE"
        self.draw()
        self.root.after(230, self.refresh_firmware_state)

    def toggle_write(self) -> None:
        # Rear WRITE switch, scan column FE bit 7: high is Enable. In Enable,
        # digits choose a save slot and + writes; Protect recalls programs.
        enabled = not self.write_enabled
        if self.send(bytes((0xF0, 0x7D, ord("S"), 0, 7, int(enabled), 0xF7))):
            self.write_enabled = enabled
            self.status = ("WRITE ENABLE  •  DIGITS PICK A USER SLOT 25–39, + WRITES" if enabled
                           else "WRITE PROTECT  •  DIGITS RECALL PROGRAMS")
        self.draw()

    def audition(self) -> None:
        self.panel_button(0x02, 4)
        self.status = "AUDITION PANEL BUTTON"
        self.draw()

    def hold_audition(self, _event: tk.Event) -> None:
        if not self.audition_held:
            self.audition_held = True
            self.send(bytes((0xF0, 0x7D, ord("B"), 0x02, 4, 1, 0xF7)))
            self.status = "AUDITION HELD  •  0 TO TOGGLE AUTO-TRIGGER  •  −/+ FOR TEMPO"
            self.draw()

    def release_audition(self, _event: tk.Event) -> None:
        if self.audition_held:
            self.audition_held = False
            self.send(bytes((0xF0, 0x7D, ord("B"), 0x02, 4, 0, 0xF7)))

    def hit_voice(self, kit: str, title: str) -> None:
        note = hit_note(kit, title, self.values.get("gm", 1))
        midi_channel = self.channel - 1
        if self.send(bytes((0x90 | midi_channel, note, 110))):
            self.status = f"TR-{kit} {title}  •  HIT  •  NOTE {note}"
            self.draw()
            self.root.after(120, lambda: self.send(bytes((0x80 | midi_channel, note, 0))))

    def toggle_demo(self) -> None:
        if self.demo_thread is not None and self.demo_thread.is_alive():
            self.demo_stop.set()
            self.status = "STOPPING FACTORY DEMO"
            self.draw()
            return
        if not self.is_running():
            self.status = "ENGINE IS STARTING  •  TRY DEMO AGAIN IN A MOMENT"
            self.draw()
            return
        if self.sequencer.snapshot()["playing"]:
            self.stop_sequence()
        self.demo_stop = threading.Event()
        self.demo_thread = threading.Thread(target=self.play_demo, daemon=True)
        self.demo_thread.start()
        self.status = "FACTORY DEMO PLAYING  •  PRESS DEMO TO STOP"
        self.draw()

    def play_demo(self) -> None:
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from factory_demo import decode
            events, total_ticks = decode((ROMS / "v1.2.u15").read_bytes())
            channel = self.channel - 1
            while not self.demo_stop.is_set():
                started = time.monotonic()
                for tick, data in events:
                    if self.demo_stop.wait(max(0.0, started + tick * 0.004 - time.monotonic())):
                        break
                    if data[0] & 15 != 9:
                        continue  # BassStation accompaniment is external.
                    self.sender.sendto(bytes(((data[0] & 0xF0) | channel, *data[1:])), self.control_path)
                if self.demo_stop.wait(max(0.0, started + total_ticks * 0.004 + 2.0 - time.monotonic())):
                    break
            self.sender.sendto(bytes((0xB0 | channel, 123, 0)), self.control_path)
            self.demo_updates.put(("status", "FACTORY DEMO STOPPED"))
        except OSError as error:
            # If the engine has gone, the poll reports why; do not cover it.
            if not self.demo_stop.is_set() and not self.closed and self.is_running():
                self.demo_updates.put(("status", f"FACTORY DEMO ERROR  •  {error}"))
        except Exception as error:
            if not self.closed:
                self.demo_updates.put(("status", f"FACTORY DEMO ERROR  •  {error}"))

    def press(self, event: tk.Event) -> None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for bounds, action in reversed(self.button_hits):
            x0, y0, x1, y1 = bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.down = bounds  # drawn pressed until the pointer lets go
                action()
                return
        for knob in self.knobs:
            if (x - knob.x) ** 2 + (y - knob.y) ** 2 <= knob.radius ** 2:
                self.drag, self.drag_origin_y = knob, y
                modifier = bool(event.state & (0x0001 | 0x0004))
                self.drag_lock_target = self._target_for_knob(knob) if modifier else None
                if self.drag_lock_target:
                    start_value = self.sequencer.locks_for(
                        self.drag_lock_target[1], self.drag_lock_target[2], self.drag_lock_target[0]
                    ).get(knob.key, self.values.get(knob.key, 64))
                    self.lock_display[knob.key] = start_value
                else:
                    start_value = self.values.get(knob.key, 64)
                self.drag_origin_value = start_value
                return

    def press_back(self, event: tk.Event) -> None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for (x0, y0, x1, y1), action in reversed(self.back_hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                action()
                return
        for knob in self.knobs:
            if (x - knob.x) ** 2 + (y - knob.y) ** 2 <= knob.radius ** 2:
                target = self._target_for_knob(knob)
                if target is not None:
                    self.remove_lock(target[0], target[1], target[2], knob.key)
                return

    def drag_knob(self, event: tk.Event) -> None:
        if not self.drag:
            return
        # A full sweep is about 254 pixels whatever the parameter's range.
        step = 254 / self.drag.maximum
        origin = min(self.drag_origin_value, self.drag.maximum)
        value = max(0, min(self.drag.maximum, origin + int((self.drag_origin_y - self.canvas.canvasy(event.y)) / step)))
        current = self.draw_knob_value(self.drag.key) if self.drag_lock_target else self.values.get(self.drag.key, 64)
        if value != current:
            if self.drag_lock_target:
                self.lock_display[self.drag.key] = value
                self.held_trig_consumed = self.held_trig is not None
            else:
                self.values[self.drag.key] = value
                self.sequencer.set_base_value(self.drag.key, value)
            self.pending_drag = (self.drag, value, self.drag_lock_target)
            if not self.drag_flush_scheduled:
                self.drag_flush_scheduled = True
                self.root.after(16, self.flush_drag)

    def flush_drag(self) -> None:
        self.drag_flush_scheduled = False
        pending, self.pending_drag = self.pending_drag, None
        if not pending:
            return
        knob, value, lock_target = pending
        if lock_target is not None:
            self.sequencer.set_lock(lock_target[1], lock_target[2], knob.key, value,
                                    lock_target[0], save=False)
            self.schedule_lock_save()
            self.draw_lock_feedback(knob)
            return
        self.last_edit = time.monotonic()
        if any(knob.key == key for key, _label, _centre in OUTPUT_KNOBS):
            self.send_output(knob.key, value)
        else:
            if knob.analog >= 0:
                self.panel_analog(knob.analog, value, refresh=False)
            # The panel ADC transport is retained for hardware-model work.
            # Until its peripheral scan is complete, route voice edits through
            # the firmware's proven controller input so drags remain audible.
            if knob.key.startswith("cc:"):
                self.send(bytes((0xB0 | (self.channel - 1), int(knob.key[3:]), value)))
            elif knob.key.startswith("level:"):
                address = int(knob.key[6:], 16)
                self.send(bytes((0xF0, 0x7D, ord("L"), address >> 8, address & 0xFF,
                                 value, 0xF7)))
            elif knob.key.startswith("pan:"):
                self.write_field(int(knob.key[4:], 16), value, 0x0F)
            elif knob.key.startswith("dist:"):
                self.write_field(int(knob.key[5:], 16), value << 4, 0xF0)
            elif knob.key.startswith("cut:"):
                self.write_field(int(knob.key[4:], 16), value, 0x7F)
            elif knob.analog == 0:
                self.send(bytes((0xB0 | (self.channel - 1), 7, value)))
        self.draw()

    def write_field(self, address: int, value: int, mask: int) -> None:
        # Utility-mode program fields: masked write into the current program.
        self.last_edit = time.monotonic()
        self.send(bytes((0xF0, 0x7D, ord("P"), address >> 8, address & 0xFF, value & mask, mask, 0xF7)))

    def toggle_flag(self, address: int, name: str) -> None:
        key = f"flag:{address:03x}"
        self.values[key] = 0 if self.values.get(key) else 1
        self.write_field(address, self.values[key] << 7, 0x80)
        self.status = f"{name}  •  {'ON' if self.values[key] else 'OFF'}"
        self.draw()

    def set_gm_kit(self, kit: int) -> None:
        self.values["gm"] = kit
        self.write_field(0x18B, kit, 0xFF)
        self.status = f"GENERAL MIDI MAP  •  TR-{('808', '909')[kit]}"
        self.draw()

    def step_bank(self, bank: int, step: int) -> None:
        key = f"bank:{bank}"
        self.values[key] = (self.values.get(key, 0) + step) % len(DRUM_CODES)
        self.write_field(0x181 + bank, self.values[key], 0xFF)
        self.status = f"ASSIGN BANK {'ABCD'[bank]}  •  {DRUM_CODES[self.values[key]]}"
        self.draw()

    def release_drag(self, _event: tk.Event) -> None:
        was_lock_edit = self.drag_lock_target is not None
        if self.drag_flush_scheduled:
            self.flush_drag()
        self.drag = None
        self.drag_lock_target = None
        if was_lock_edit and self.held_trig is None:
            self.lock_display.clear()
            self.draw_sequence()
            self.draw()
        if self.down is not None:
            self.down = None
            self.draw()

    def text(self, x: float, y: float, text: str, fill: str, size: int = 10, *, anchor: str = "center",
             mono: bool = False) -> int:
        font = (self.mono, -size) if mono else (self.sans, -size, "bold")
        return self.canvas.create_text(x, y, text=text, fill=fill, anchor=anchor, font=font)

    def button(self, x: int, y: int, kind: str, label: str, action, *, lit=False, red=False, size: int = 10,
               reach: int = 0) -> bool:
        """Draw a pre-rendered button; `reach` widens its hit box. Returns whether it is held down."""
        w, h = BUTTON_SIZES[kind]
        bounds = (x - reach, y - reach, x + w + reach, y + h + reach)
        down = self.down == bounds
        style = "red" if red else ("lit" if lit else "plain")
        self.skin.place(self.canvas, f"{kind}:{style}:{int(down)}", x, y)
        if label:
            self.text(x + w / 2, y + h / 2 + down, label, "#fff4d4" if lit or red else "#d5ddda", size)
        self.button_hits.append((bounds, action))
        return down

    def knob(self, x: int, y: int, label: str, analog: int, key: str, radius: int = MAIN_R, *,
             maximum: int = 127, text: str | None = None, flag: int | None = None,
             track: str | None = None) -> None:
        self.values.setdefault(key, 64 if maximum == 127 else 0)
        value = self.draw_knob_value(key)
        self.knobs.append(Knob(label, analog, key, x, y, radius, maximum, track))
        # The tick ring and shadow are on the plate; the frame sweeps from
        # lower-left at zero to lower-right at maximum.
        frame = round(min(value, maximum) * (KNOB_FRAMES - 1) / maximum)
        image_item = self.skin.place(self.canvas, f"knob{radius}:{frame}", x, y)
        if radius == MINI_R:
            # Mini knobs carry their value in the caption to save height.
            value_item = self.text(x, y + radius + 13, f"{label} {text if text is not None else value}", "#ded6b8", 10)
            self.knob_visuals[(track, key)] = (image_item, value_item, self.knobs[-1])
            return
        self.text(x, y + radius + 14, label, "#ded6b8", 10)
        # Not the mono face: at this size its 8 reads as B and its dotted 0 as 8.
        value_item = self.text(x, y + radius + 26, text if text is not None else f"{value:03d}", "#a3aea8", 9)
        self.knob_visuals[(track, key)] = (image_item, value_item, self.knobs[-1])
        if flag is not None:
            # Utility 3 Controller/Velocity: bit 7 makes this control follow
            # note velocity; on LEVEL it is VL.
            self.button(x - 13, y + radius + 33, "vel", "VEL",
                        lambda a=flag, n=label: self.toggle_flag(a, f"{n} VELOCITY"),
                        lit=bool(self.values.get(f"flag:{flag:03x}")), size=8, reach=2)

    def display_digit(self, x: int, y: int, bits: int) -> None:
        # The firmware's two LED bytes already contain segment patterns.
        # Bit 6 is the decimal point; it also marks edit/write states.
        for bit in SEGMENT_BITS:
            if bits & (1 << bit):
                self.skin.place(self.canvas, f"segment:{bit}", x, y)

    def voice_module(self, x: int, y: int, w: int, h: int, kit_name: str, title: str) -> None:
        controls = CARD_CONTROLS[title]
        kit = int(kit_name == "909")
        track_id = f"{kit_name}:{title}"
        self.cards.append((f"{kit_name} {title}", (x, y, x + w, y + h)))
        self.button(x + 4, y + 5, "track", "", lambda t=track_id: self.select_sequence_track(t),
                    lit=track_id == self.selected_track)
        self.text(x + 25, y + 15, title, "#f0d15b", 11, anchor="w")
        self.button(x + w - 38, y + 5, "hit", "HIT", lambda k=kit_name, t=title: self.hit_voice(k, t),
                    red=True, size=10)
        pan = PAN_ADDRESS[title][kit]
        noteoff = bool(self.values.get(f"flag:{pan - 1:03x}"))
        self.button(x + w - 94, y + 5, "noteoff", "NOTE-OFF",
                    lambda a=pan - 1, n=f"TR-{kit_name} {title}": self.toggle_flag(a, f"{n} NOTE-OFF"),
                    lit=noteoff, size=9)
        main, minis = card_knobs(x, y, w, h, title)
        for (knob_x, knob_y), (label, analog, cc808, cc909) in zip(main, controls):
            label = label.split(" / ")[0 if kit else -1]
            if label == "LEVEL":
                address = LEVEL_ADDRESS[title][kit]
                key = f"level:{address:03x}"
            else:
                cc = cc909 if kit else cc808
                address, key = CC_ADDRESS[cc], f"cc:{cc}"
            self.knob(knob_x, knob_y, label, analog, key, MAIN_R, flag=address, track=track_id)
        # Utility strip: values the hardware only reaches through Utility mode.
        fields = []
        if title in FRONT_CUT_VOICES:
            cut = self.values.get(f"cut:{pan - 1:03x}", 0)
            fields.append(("CUT", f"cut:{pan - 1:03x}", 99, f"{cut}ms"))
        output = self.values.get(f"pan:{pan:03x}", 4)
        fields.append(("PAN", f"pan:{pan:03x}", 8, PAN_NAMES[output] if output <= 8 else f"OUT{output - 8}"))
        fields.append(("DIST", f"dist:{pan:03x}", 15, None))
        for (knob_x, knob_y), (label, key, maximum, text) in zip(minis, fields):
            self.knob(knob_x, knob_y, label, -1, key, MINI_R, maximum=maximum, text=text, track=track_id)

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.knobs, self.button_hits, self.back_hits, self.cards = [], [], [], []
        self.knob_visuals = {}
        c.create_image(0, 0, image=self.skin.plate, anchor="nw")
        self.knob(*MASTER_KNOB, "MASTER VOLUME", 0, "analog:0", HEADER_R)
        # General MIDI map (RAM 018B): which kit answers the GM drum notes.
        for index, (kit_name, (bx, by)) in enumerate(zip(("808", "909"), GM_BUTTONS)):
            self.button(bx, by, "gm", f"TR-{kit_name}", lambda v=index: self.set_gm_kit(v),
                        lit=self.values.get("gm") == index, size=12)
        self.text(385, 104, "GM MAP KIT", "#d5c979", 11)
        for (dx, dy), bits in zip(DIGITS, self.display_segments):
            self.display_digit(dx, dy, bits)
        wx, wy = WRITE_BUTTON
        down = self.button(wx, wy, "write", "", self.toggle_write, red=self.write_enabled)
        self.text(wx + 29, wy + 13 + down, "WRITE", "#fff4d4" if self.write_enabled else "#d5ddda", 11)
        self.text(wx + 29, wy + 27 + down, "ENABLE" if self.write_enabled else "PROTECT",
                  "#fff4d4" if self.write_enabled else "#e6d493", 10)
        display_label = f"UTILITY {self.utility_function}" if self.mode == 3 and self.utility_function else "DISPLAY"
        self.text(500, 108, display_label, "#bebfac", 10)
        for idx, (label, (lx, ly)) in enumerate(zip(("PROGRAM", "MIDI RX CH.", "MIDI TX CH.", "UTILITY", "SAVE"),
                                                    MODE_LEDS)):
            if self.mode == idx:
                self.skin.place(c, "led:amber", lx, ly)
            self.text(lx + 14, ly, label, "#d8dfd7", 10, anchor="w")
        demo = self.demo_thread is not None and self.demo_thread.is_alive()
        for (bx, by), (label, action, style) in zip(MODE_BUTTONS, (
                ("MODE", self.next_mode, {}), ("AUDITION", self.audition, {"red": True}),
                ("FACTORY DEMO", self.toggle_demo, {"lit": demo}))):
            self.button(bx, by, "mode", label, action, size=11, **style)
        key_w, key_h = BUTTON_SIZES["key"]
        for index, (key, caption) in enumerate(KEYS):
            row, col = divmod(index, 3)
            key_x, key_y = KEYPAD[0] + col * KEY_PITCH[0], KEYPAD[1] + row * KEY_PITCH[1]
            down = self.button(key_x, key_y, "key", "", lambda i=index: self.keypad(i))
            self.text(key_x + 11, key_y + key_h / 2 + down, key, "#f0f0e6", 13)
            self.text(key_x + 22, key_y + key_h / 2 + down, caption, "#e6d493", 10, anchor="w")
        # Pitch-play Assign Banks A..D (RAM 0181..0184).
        self.text(BANKS[0], 42, "ASSIGN BANKS  (click +, right-click \u2212)", "#d5c979", 10, anchor="w")
        bank_w, bank_h = BUTTON_SIZES["bank"]
        for bank in range(4):
            bx, by = BANKS[0] + (bank % 2) * BANK_PITCH[0], BANKS[1] + (bank // 2) * BANK_PITCH[1]
            code = self.values.get(f"bank:{bank}", 0)
            name = DRUM_CODES[code] if code < len(DRUM_CODES) else f"{code:02d}"
            self.button(bx, by, "bank", f"{'ABCD'[bank]}   {name}", lambda b=bank: self.step_bank(b, 1))
            self.back_hits.append(((bx, by, bx + bank_w, by + bank_h), lambda b=bank: self.step_bank(b, -1)))
        self.skin.place(c, "led:green" if self.is_running() else "led:red", *ENGINE_LED)
        self.text(ENGINE_LED[0] + 15, ENGINE_LED[1], "ENGINE", "#e5e9e0", 11, anchor="w")
        self.button(*IO_BUTTON, "io", "I/O SETUP", self.io_dialog)
        self.button(PANEL_W - 138, STATUS[1] + 2, "seq", "SEQUENCER", self.open_sequencer,
                    lit=self.sequence_window is not None and self.sequence_window.top.winfo_exists(), size=9)
        self.text(OUTPUT_KNOBS[0][2][0] - MAIN_R - TICK, 44, "MASTER FX", "#d5c979", 10, anchor="w")
        self.button(*ROOM_BUTTON, "noteoff", ROOM_NAMES[self.values["room"]], self.toggle_room,
                    lit=bool(self.values["room"]), size=9)
        for key, label, centre in OUTPUT_KNOBS:
            self.knob(*centre, label, -1, key, text=None if self.values[key] or key == "boost" else "OFF")
        for kit_name, left, rows in KIT_BLOCKS:
            count = sum(len(cells) for cells in rows)
            self.text(left, 149, f"TR-{kit_name}  \u2022  {count} VOICES", "#dae2da", 12, anchor="w")
        for kit_name, title, x, y, width, height in card_rects():
            self.voice_module(x, y, width, height, kit_name, title)
        self.text(STATUS[0] + 14, (STATUS[1] + STATUS[3]) / 2, self.status, "#c9d5cb", 11, anchor="w", mono=True)

    def load_settings(self) -> None:
        try:
            saved = json.loads(self.settings.read_text()) if self.settings else {}
        except (OSError, ValueError):
            saved = {}
        self.saved = saved if isinstance(saved, dict) else {}
        audio, midi_input, rate, period = (self.saved.get(key) for key in ("audio", "midi_input", "rate", "period"))
        if isinstance(audio, str) and audio: self.audio = audio
        if isinstance(midi_input, str): self.midi_input = midi_input
        if isinstance(rate, int) and 8000 <= rate <= 192000: self.rate = rate
        if isinstance(period, int) and 16 <= period <= 2048: self.period = period

    def save_settings(self) -> None:
        if not self.settings: return
        current = {"audio": self.audio, "midi_input": self.midi_input, "rate": self.rate, "period": self.period}
        # What a launcher set for this run is not the user's choice, unless
        # the dialog then changed it.
        self.saved.update({key: value for key, value in current.items() if self.overrides.get(key) != value})
        try:
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            self.settings.write_text(json.dumps(self.saved, indent=2) + "\n")
        except OSError as error:
            messagebox.showwarning("Settings not saved", str(error), parent=self.root)

    def midi_inputs(self) -> dict[str, str]:
        """Sequencer ports that can send: what the dialog calls each -> CLIENT:PORT."""
        try:
            listing = subprocess.run([str(BINARY), "--realtime", "--list-midi"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        inputs = {NO_MIDI_INPUT: ""}
        for line in listing.splitlines():
            client, tab, port = line.partition("\t")
            if tab:
                inputs.setdefault(port if port.startswith(client) else f"{client}: {port}", f"{client}:{port}")
        if self.midi_input and self.midi_input not in inputs.values():
            inputs[f"{self.midi_input} (not plugged in)"] = self.midi_input
        return inputs

    def io_dialog(self) -> None:
        if self.engine_socket is not None:
            messagebox.showinfo("DrumStation I/O", "This DrumStation runs inside the elektremu studio's engine: "
                                "its audio and MIDI are cabled in the studio.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("DrumStation I/O")
        frame = ttk.Frame(dialog, padding=16)
        frame.grid(sticky="nsew")
        inputs = self.midi_inputs()
        fields = {"Audio device": tk.StringVar(value=self.audio),
                  "MIDI input": tk.StringVar(value=next(name for name, source in inputs.items() if source == self.midi_input)),
                  "MIDI channel": tk.StringVar(value=str(self.channel)), "Sample rate": tk.StringVar(value=str(self.rate)),
                  "Audio period": tk.StringVar(value=str(self.period))}
        for row, (label, var) in enumerate(fields.items()):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            if label == "Audio device": field = ttk.Combobox(frame, textvariable=var, values=("default", "pipewire", "pulse"), width=40)
            elif label == "MIDI input": field = ttk.Combobox(frame, textvariable=var, values=tuple(inputs), state="readonly", width=40)
            else: field = ttk.Entry(frame, textvariable=var, width=42)
            field.grid(row=row, column=1, padx=(12, 0), pady=3, sticky="ew")
        def apply() -> None:
            try:
                channel, rate, period = int(fields["MIDI channel"].get()), int(fields["Sample rate"].get()), int(fields["Audio period"].get())
                if not (1 <= channel <= 16 and 8000 <= rate <= 192000 and 16 <= period <= 2048): raise ValueError
            except ValueError:
                messagebox.showerror("Invalid I/O", "Channel 1–16, rate 8000–192000, and period 16–2048 are required.", parent=dialog)
                return
            self.channel_override = channel != self.channel
            self.audio, self.midi_input = fields["Audio device"].get().strip() or "default", inputs[fields["MIDI input"].get()]
            self.channel, self.rate, self.period = channel, rate, period
            self.save_settings()
            dialog.destroy(); self.restart_pending = True; self.stop_engine()
        ttk.Button(frame, text="Apply", command=apply).grid(row=6, column=1, sticky="e", pady=(10, 0))

    def start_engine(self) -> None:
        if self.engine_socket is not None:
            # The studio's engine has the board running already: read its state once it answers.
            self.write_enabled = False
            self.program_image, self.output_sent = None, False
            self.status = "IN THE STUDIO ENGINE"
            self.draw(); self.root.after(700, self.refresh_firmware_state)
            return
        if self.is_running(): return
        command = [str(BINARY), "--realtime", "--rom-dir", str(ROMS), "--audio", self.audio, "--midi", "seq",
                   "--rate", str(self.rate), "--period", str(self.period),
                   "--control-socket", self.control_path, "--eeprom", str(self.eeprom), "--quiet"]
        # The channel lives in the EEPROM file. Only an I/O dialog change
        # overrides it; the panel then reads the stored one back.
        if self.channel_override:
            command += ["--channel", str(self.channel)]
        if self.midi_input:
            command += ["--midi-connect", self.midi_input]
        self.channel_override = False
        try:
            self.eeprom.parent.mkdir(parents=True, exist_ok=True)
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            messagebox.showerror("Could not start engine", str(error)); return
        self.write_enabled = False  # The engine boots with WRITE in Protect.
        # What was read from the last engine is not this one's state: the
        # display poll reads it all again once the new engine answers.
        self.program_image, self.output_sent = None, False
        self.status = f"BOOTING  •  {self.rate} Hz  •  {self.midi_input or 'no MIDI device'}"
        self.draw(); self.root.after(700, self.refresh_firmware_state)

    def stop_engine(self) -> None:
        self.sequence_clock.stop()
        self.demo_stop.set()
        if self.process and self.process.poll() is None: self.process.terminate()
        self.status = "STOPPING ENGINE"; self.draw()

    def poll(self) -> None:
        self._register_clock_receiver()
        redraw = False
        while not self.demo_updates.empty():
            kind, value = self.demo_updates.get_nowait()
            if kind == "status":
                self.status = str(value)
                redraw = True
        if redraw:
            self.draw()
        if self.process and self.process.poll() is not None:
            code, detail = self.process.returncode, self.process.stderr.read().strip() if self.process.stderr else ""
            self.process = None
            self.sequence_clock.stop()
            if self.restart_pending:
                self.restart_pending = False; self.start_engine()
            else:
                self.status = "ENGINE STOPPED" if code == 0 else f"ENGINE ERROR  •  {detail or code}"; self.draw()
        if self.engine_socket is not None and not self.engine_socket.exists() and self.status != "STUDIO ENGINE STOPPED":
            self.sequence_clock.stop()
            self.status = "STUDIO ENGINE STOPPED"; self.draw()
        if self.sequence_window is not None and self.sequence_window.top.winfo_exists():
            self.sequence_window.refresh_playhead()
        self.root.after(100, self.poll)

    def close(self) -> None:
        self.closed = True
        if self.lock_save_after is not None:
            self.root.after_cancel(self.lock_save_after)
            self.flush_lock_save()
        self.sequence_clock.close()
        self.clock_receiver.close()
        if self.sequence_window is not None and self.sequence_window.top.winfo_exists():
            self.sequence_window.top.destroy()
            self.sequence_window = None
        self.stop_engine()
        self.sequence_sender.close()
        Path(self.sequence_path).unlink(missing_ok=True)
        self.sender.close()
        Path(self.reply_path).unlink(missing_ok=True)
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate prerequisites without opening a window")
    parser.add_argument("--eeprom", type=Path, default=EEPROM, metavar="FILE",
                        help="Where saved programs and MIDI channels are kept (default: %(default)s)")
    parser.add_argument("--settings", type=Path, default=SETTINGS, metavar="FILE",
                        help="Where the I/O SETUP choices are kept (default: %(default)s)")
    parser.add_argument("--sequencer", type=Path, default=SEQUENCER_DATA, metavar="FILE",
                        help="Where sequencer patterns are kept (default: %(default)s)")
    parser.add_argument("--audio", metavar="DEVICE", help="Audio device for this run, whatever is saved")
    parser.add_argument("--midi-input", metavar="CLIENT:PORT",
                        help="MIDI device for this run, whatever is saved; 'none' for none")
    parser.add_argument("--engine-socket", type=Path, metavar="PATH",
                        help="Drive the board in the elektremu studio's engine through its panel socket, "
                             "instead of starting an engine (audio, MIDI and EEPROM are then the studio's)")
    args = parser.parse_args()
    needed = (ROMS / "v1.2.u15", ROMS / "v1.2.u28") if args.engine_socket else (BINARY, ROMS / "v1.2.u15", ROMS / "v1.2.u28")
    missing = [str(path.relative_to(ROOT)) for path in needed if not path.exists()]
    if missing:
        print("Missing prerequisite(s): " + ", ".join(missing), file=sys.stderr); return 1
    if any(not (ASSETS / name).exists() for name in
           ("plate.png", "sprites.png", "skin.json", "sequencer.png",
            "sequencer_full.png", "sequencer_all.png")):
        print("Missing panel artwork in tools/assets; restore the complete release checkout", file=sys.stderr); return 1
    if args.check:
        print("Realtime performance panel prerequisites available."); return 0
    root = tk.Tk(className="drumstation"); root.title("DrumStation — Firmware Panel"); root.geometry(f"{PANEL_W}x{PANEL_H}"); root.minsize(1020, 650)
    icons = [tk.PhotoImage(master=root, file=str(path)) for path in sorted((ASSETS / "icons").glob("drumstation-*.png"))]
    if icons: root.iconphoto(True, *icons)
    midi_input = "" if args.midi_input == "none" else args.midi_input
    DrumStationPanel(root, args.eeprom, args.settings, {"audio": args.audio, "midi_input": midi_input}, args.sequencer,
                     args.engine_socket)
    root.mainloop(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
