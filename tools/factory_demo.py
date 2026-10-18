#!/usr/bin/env python3
"""Decode v1.2's factory demo stream using the U15:30e6..3168 format."""
import argparse
from pathlib import Path
import struct
from roms import verify


def decode(rom):
    pos, tick, status = 0x674A, 0, None
    events = []

    def take():
        nonlocal pos
        if pos >= len(rom):
            raise ValueError("Truncated factory demo")
        value = rom[pos]
        pos += 1
        return value

    while True:
        value = take()
        if value >= 0xF0:
            if value == 0xFE:
                tick += take() & 127
                continue
            if value != 0xF1:
                raise ValueError(f"Unexpected demo terminator {value:02x}")
            return events, tick
        if value & 128:
            status = value
            value = take()
        if status is None or value & 128:
            raise ValueError("Invalid running status/demo data")
        data = [status, value]
        value = take()
        if value & 128:
            delay = value & 127
        else:
            data.append(value)
            delay = take() & 127
        expected = 2 if status & 0xF0 in (0xC0, 0xD0) else 3
        if len(data) != expected:
            raise ValueError("Demo message length disagrees with MIDI status")
        events.append((tick, bytes(data)))
        tick += delay


def vlq(value):
    result = [value & 127]
    while value >> 7:
        value >>= 7
        result.insert(0, (value & 127) | 128)
    return bytes(result)


def write_midi(path, events, end_tick, channel=10):
    # 500000 us/quarter, 125 ticks/quarter = 4 ms/tick.
    # Timing inferred from the physical unit's opening snare roll.
    track = bytearray(b"\x00\xff\x51\x03\x07\xa1\x20")
    previous = 0
    for tick, data in events:
        if data[0] & 15 != 9:
            continue  # BassStation accompaniment is external to the DrumStation.
        track.extend(vlq(tick - previous))
        track.extend(bytes([(data[0] & 0xF0) | (channel - 1)]) + data[1:])
        previous = tick
    track.extend(vlq(end_tick - previous) + b"\xff\x2f\x00")
    Path(path).write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 125) + b"MTrk" + struct.pack(">I", len(track)) + track)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom-dir", type=Path, default=root / "roms")
    parser.add_argument("--out", type=Path, default=root / "build/factory-demo.mid")
    parser.add_argument("--channel", type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.channel <= 16:
        parser.error("channel must be 1..16")
    verify(args.rom_dir)
    events, end = decode((args.rom_dir / "v1.2.u15").read_bytes())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_midi(args.out, events, end, args.channel)
    print(f"Decoded {len(events)} events / {end} ticks; wrote drum part on channel {args.channel} to {args.out}")


if __name__ == "__main__":
    main()
