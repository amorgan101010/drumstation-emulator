#!/usr/bin/env python3
"""Render MIDI-triggered original DrumStation firmware to a stereo WAV (experimental)."""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import wave
from array import array
import sys

from roms import verify
from factory_demo import decode

ROOT = Path(__file__).resolve().parents[1]
TIMER_PERIOD = 189
STEPS_PER_FRAME = TIMER_PERIOD * 4


def test_eeprom():
    """Synthetic system settings, not a dump of a physical DrumStation."""
    image = bytearray([255] * 2048)
    # U15:581b reads last program, receive channel, transmit channel, demo tempo.
    image[0x7FB:0x7FF] = bytes([10, 12, 0, 10])
    return image


def prepare(directory, notes, velocity, program, rate, spacing=0.5, channel=10):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test-eeprom.bin").write_bytes(test_eeprom())
    events = []
    virtual_hz = rate * STEPS_PER_FRAME
    byte_steps = round(virtual_hz * 10 / 31250)

    def message(seconds, data):
        tick = round(seconds * virtual_hz)
        events.extend((tick + i * byte_steps, byte) for i, byte in enumerate(data))

    ch = channel - 1
    message(0.10, [0xC0 | ch, program])
    for i, note in enumerate(notes):
        message(0.20 + i * spacing, [0x90 | ch, note, velocity])
        message(0.30 + i * spacing, [0x80 | ch, note, 0])
    events.sort()
    (directory / "midi-events.txt").write_text("".join(f"{tick} {byte:02x}\n" for tick, byte in events))


def extract_stereo(trace, target, rate):
    """Map TX1 phases 2/4 to master L/R (DM 3fcd/3fcf).

    Firmware Pan L4 silences phase 4, and the panned factory demo's stereo
    balance matches the hardware recording with phase 2 as left.
    Preserve level exactly; no normalization, resampling, or synthetic sound.
    """
    left = None
    frames = bytearray()
    peak = 0
    nonzero = 0
    with Path(trace).open() as stream:
        for line in stream:
            tick, port, sample, phase = map(int, line.split())
            if port != 1:
                continue
            if phase == 2:
                left = sample
            elif phase == 4 and left is not None:
                frames.extend(struct.pack("<hh", left, sample))
                peak = max(peak, abs(sample), abs(left))
                nonzero += bool(sample or left)
                left = None
    if not frames:
        raise RuntimeError("No complete stereo SPORT frames were emitted")
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return {"frames": len(frames) // 4, "peak_pcm16": peak, "nonzero_frames": nonzero}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", default="36", help="Comma-separated MIDI notes, spaced 0.5 seconds apart")
    parser.add_argument("--factory-demo", action="store_true", help="Play the factory demo decoded from U15")
    parser.add_argument("--velocity", type=int, default=100)
    parser.add_argument("--program", type=int, default=0, help="Factory program 0..24")
    parser.add_argument("--rate", type=int, default=44100, help="Provisional sample clock; not hardware-calibrated")
    parser.add_argument("--tail", type=float, default=1.5)
    parser.add_argument("--out", type=Path, default=ROOT / "build/drumstation.wav")
    parser.add_argument("--rom-dir", type=Path, default=ROOT / "roms")
    parser.add_argument("--binary", type=Path, default=ROOT / "build/drumstation")
    args = parser.parse_args()
    try:
        notes = [int(n) for n in args.notes.split(",")]
    except ValueError:
        parser.error("notes must be comma-separated integers")
    if not notes or any(n < 0 or n > 127 for n in notes):
        parser.error("notes must be in 0..127")
    if not 0 <= args.velocity <= 127 or not 0 <= args.program <= 24:
        parser.error("velocity must be 0..127 and program 0..24")
    if not 8000 <= args.rate <= 192000 or not 0.1 <= args.tail <= 60:
        parser.error("rate must be 8000..192000 and tail 0.1..60 seconds")
    verify(args.rom_dir)
    args.out = args.out.resolve()
    job = args.out.with_suffix(".run")
    prepare(job, notes, args.velocity, args.program, args.rate)
    duration = .4 + (len(notes) - 1) * .5 + args.tail
    demo_count = None
    if args.factory_demo:
        demo, end = decode((args.rom_dir / "v1.2.u15").read_bytes())
        demo_count = len(demo)
        hz = args.rate * STEPS_PER_FRAME
        wire_free = 0
        wire = []
        for tick, data in demo:
            if data[0] & 15 != 9:
                continue
            when = max(round((.2 + tick * .004) * hz), wire_free)
            for byte in data:
                wire.append((when, byte))
                when += round(hz * 10 / 31250)
            wire_free = when
        (job / "midi-events.txt").write_text("".join(f"{tick} {byte:02x}\n" for tick, byte in wire))
        duration = .4 + end * .004 + args.tail
    result = subprocess.run([
        str(args.binary.resolve()), str(args.rom_dir.resolve()), str(job),
        str(round(duration * args.rate * STEPS_PER_FRAME)),
        "--experimental-timers", "--eeprom", str(job / "test-eeprom.bin"),
        "--midi-events", str(job / "midi-events.txt"),
        "--no-sport-log",
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Firmware execution failed:\n{result.stderr.strip()}")
    raw = (job / "stereo.pcm").read_bytes()
    if not raw or len(raw) % 4:
        raise RuntimeError("Invalid/empty stereo PCM output")
    with wave.open(str(args.out), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(args.rate)
        wav.writeframes(raw)
    samples = array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    stats = {"frames": len(raw) // 4, "peak_pcm16": max(abs(v) for v in samples),
             "nonzero_frames": sum(bool(samples[i] or samples[i + 1]) for i in range(0, len(samples), 2))}
    metadata = {"firmware": "v1.2", "notes": None if args.factory_demo else notes, "velocity": args.velocity,
                "program": args.program, "sample_rate_provisional": args.rate,
                "eeprom": "synthetic system defaults, erased user programs",
                "mcu_timer": "experimental IRQ6 every 16000 DSP steps",
                "output_routing": "inferred TX1 phases 4/2 to L/R",
                "hardware_match_validated": False, "board": result.stdout.strip(), **stats}
    if args.factory_demo:
        metadata.update(factory_demo_events=demo_count, demo_tick_seconds=.004)
    args.out.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(result.stdout.strip())
    print(f"Wrote {args.out} ({stats['frames']} frames; peak {stats['peak_pcm16']}/32768)")


if __name__ == "__main__":
    main()
