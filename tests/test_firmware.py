"""Integration tests with locally supplied ROMs; skip if ROMs are unavailable."""
import os
from pathlib import Path
import struct
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from render import prepare, extract_stereo, STEPS_PER_FRAME
from roms import verify
from factory_demo import decode

BINARY = Path(os.environ.get("DRUMSTATION_BINARY", ROOT / "build/drumstation"))


@unittest.skipUnless((ROOT / "roms/v1.2.u28").exists() and BINARY.exists(), "Requires local ROMs and build")
class FirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        verify(ROOT / "roms")

    def render(self, notes, velocity=100, channel=10):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            prepare(path, notes, velocity, 0, 44100, channel=channel)
            result = subprocess.run([str(BINARY), str(ROOT / "roms"), temp, "20000000",
                "--experimental-timers", "--eeprom", str(path / "test-eeprom.bin"),
                "--midi-events", str(path / "midi-events.txt")], check=True, capture_output=True, text=True)
            stats = extract_stereo(path / "sport.tsv", path / "out.wav", 44100)
            with wave.open(str(path / "out.wav")) as wav:
                pcm = wav.readframes(wav.getnframes())
            events = (path / "events.tsv").read_text()
            pm = (path / "dsp-pm.bin").read_bytes()
            return stats, pcm, events, pm, result.stdout

    def test_pan_left_reaches_left_channel(self):
        # CC 106 is TR909 bass drum Pan; 0 is L4. TX1 phase 2 must be left.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            prepare(path, [36], 100, 0, 44100)
            virtual_hz = 44100 * STEPS_PER_FRAME
            pan = [(round(0.15 * virtual_hz) + i * 400, byte) for i, byte in enumerate((0xB9, 106, 0))]
            lines = (path / "midi-events.txt").read_text().splitlines()
            events = sorted([tuple(int(x, 16) if i else int(x) for i, x in enumerate(l.split())) for l in lines] + pan)
            (path / "midi-events.txt").write_text("".join(f"{t} {b:02x}\n" for t, b in events))
            subprocess.run([str(BINARY), str(ROOT / "roms"), temp, "20000000", "--experimental-timers",
                "--eeprom", str(path / "test-eeprom.bin"), "--midi-events", str(path / "midi-events.txt")],
                check=True, capture_output=True)
            extract_stereo(path / "sport.tsv", path / "out.wav", 44100)
            with wave.open(str(path / "out.wav")) as wav:
                frames = struct.unpack(f"<{wav.getnframes() * 2}h", wav.readframes(wav.getnframes()))
            left = sum(x * x for x in frames[0::2]); right = sum(x * x for x in frames[1::2])
            self.assertGreater(left, 1000 * max(right, 1))

    def test_boot_without_notes_is_silent(self):
        stats, _, events, pm, _ = self.render([])
        self.assertEqual(stats["peak_pcm16"], 0)
        self.assertEqual(events.count("bdma\t"), 3)
        self.assertEqual(int.from_bytes(pm[:3], "little"), 0x18030F)
        self.assertIn("bdma\t70\t1867\t0\t9e7\t0", events)

    def test_note_executes_both_processors_and_produces_audio(self):
        stats, _, events, _, status = self.render([36])
        self.assertGreater(stats["nonzero_frames"], 100)
        self.assertGreater(stats["peak_pcm16"], 100)
        self.assertIn("midi-rx\t99", events)
        self.assertNotIn("IDMA writes=0 ", status)

    def test_zero_velocity_note_is_silent(self):
        stats, *_ = self.render([36], velocity=0)
        self.assertEqual(stats["peak_pcm16"], 0)

    def test_wrong_channel_is_ignored(self):
        stats, *_ = self.render([36], channel=11)
        self.assertEqual(stats["peak_pcm16"], 0)

    def test_different_notes_produce_different_output(self):
        kick = self.render([36])
        snare = self.render([38])
        self.assertGreater(snare[0]["peak_pcm16"], 100)
        self.assertNotEqual(kick[1], snare[1])

    def test_factory_demo_decoder_has_known_v12_shape(self):
        events, ticks = decode((ROOT / "roms/v1.2.u15").read_bytes())
        self.assertEqual((len(events), ticks), (1801, 16577))
        self.assertEqual(events[:3], [(0, bytes.fromhex("c9 01")),
                                     (96, bytes.fromhex("b9 07 50")),
                                     (119, bytes.fromhex("99 28 2d"))])

    def test_live_midi_burst_does_not_overflow_receive_ring(self):
        # 24 note-ons (72 bytes) arrive in one engine period. Real MIDI can't
        # deliver that faster than 31250 baud; the firmware's 32-byte ring
        # flags an overflow at U15 3274 if the emulator does.
        with tempfile.TemporaryDirectory() as temp:
            control, reply = f"{temp}/control.sock", f"{temp}/reply.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(reply); sock.settimeout(2)
            engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"), "--audio", "null",
                                       "--midi", "seq", "--channel", "15", "--control-socket", control, "--quiet"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def overflows():
                sock.sendto(bytes((0xF0, 0x7D, ord("C"), 0x32, 0x74, 0xF7)), control)
                return sock.recvfrom(512)[0][4]
            try:
                time.sleep(1.0)
                before = overflows()
                for i in range(24):
                    sock.sendto(bytes((0x9E, 36 + i % 12, 1)), control)
                time.sleep(0.5)
                self.assertEqual(overflows(), before)
            finally:
                engine.terminate(); engine.wait(); sock.close()

    def test_realtime_parameter_lock_is_transient_and_audible_for_one_hit(self):
        # The file PCM writes the realtime engine's actual S16 output to a
        # temporary raw file while its slave is ALSA null; no system device or
        # audio configuration is changed by this test.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            control, reply = path / "control.sock", path / "reply.sock"
            capture = path / "sequencer.raw"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(str(reply)); sock.settimeout(2)
            engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"),
                "--audio", f"file:{capture},raw", "--midi", "none", "--channel", "15",
                "--control-socket", str(control), "--eeprom", str(path / "eeprom.bin"), "--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not control.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(control.exists(), "realtime engine did not create its control socket")

                # CC batch records have shorter payloads than RAM records.
                # Exercise a persistent CC set used for transient restoration.
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), 1, 0xF7)), str(control))
                program = sock.recvfrom(512)[0][4:-1]
                base_cc23 = program[0x37] & 0x7F
                sock.sendto(bytes((0xF0, 0x7D, ord("T"), 1,
                                   0x10, 14, 23, 40, 0xF7)), str(control))
                time.sleep(0.05)
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), 1, 0xF7)), str(control))
                changed = sock.recvfrom(512)[0][4:-1]
                self.assertEqual(changed[0x37] & 0x7F, 40,
                                 "sequencer CC_SET did not reach the firmware")
                sock.sendto(bytes((0xF0, 0x7D, ord("T"), 1,
                                   0x10, 14, 23, base_cc23, 0xF7)), str(control))
                time.sleep(0.05)

                # TR-909 bass level is RAM 0101 on this program. Lock it to
                # zero for one note, with base 64; the engine restores it
                # after the note's UART bytes reach the firmware.
                locked_hit = bytes((0xF0, 0x7D, ord("T"), 2,
                    0x21, 0x01, 0x01, 0, 64, 0x7F,
                    0x30, 14, 36, 110, 0xF7))
                sock.sendto(locked_hit, str(control))
                time.sleep(0.12)
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), 1, 0xF7)), str(control))
                program = sock.recvfrom(512)[0][4:-1]
                self.assertEqual(program[0x01] & 0x7F, 64)

                # The next note carries no lock. It must be audible from the
                # restored base level, with no level-zero override left active.
                before_unlocked = capture.stat().st_size // 2
                unlocked_hit = bytes((0xF0, 0x7D, ord("T"), 1, 0x30, 14, 36, 110, 0xF7))
                sock.sendto(unlocked_hit, str(control))
                time.sleep(0.12)

                # After a gap, exercise CC_LOCK + note + restore through the
                # firmware. A rejected batch would enqueue no note at all.
                time.sleep(0.4)
                before_cc_lock = capture.stat().st_size // 2
                cc_locked_hit = bytes((0xF0, 0x7D, ord("T"), 2,
                    0x11, 14, 23, 0, base_cc23,
                    0x30, 14, 36, 110, 0xF7))
                sock.sendto(cc_locked_hit, str(control))
                time.sleep(0.12)
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), 1, 0xF7)), str(control))
                restored = sock.recvfrom(512)[0][4:-1]
                self.assertEqual(restored[0x37] & 0x7F, base_cc23,
                                 "sequencer CC_LOCK did not restore its base value")
            finally:
                engine.terminate(); engine.wait(timeout=5)
                if engine.stderr:
                    engine.stderr.read()
                    engine.stderr.close()
                sock.close()
            raw = capture.read_bytes()
            samples = struct.unpack(f"<{len(raw) // 2}h", raw[:len(raw) // 2 * 2])
            self.assertTrue(samples[:before_unlocked], "realtime capture did not reach the first hit")
            self.assertEqual(max(map(abs, samples[:before_unlocked]), default=0), 0,
                             "zero-level parameter lock leaked audio before the unlocked hit")
            self.assertGreater(max(map(abs, samples[before_unlocked:before_cc_lock]), default=0), 100,
                               "unlocked hit did not return to its base level")
            self.assertGreater(max(map(abs, samples[before_cc_lock:]), default=0), 100,
                               "CC_LOCK batch did not send its firmware note")

    def test_keypad_recalls_program_unless_write_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            control, reply = f"{temp}/control.sock", f"{temp}/reply.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(reply); sock.settimeout(2)
            engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"), "--audio", "null",
                                       "--midi", "seq", "--channel", "15", "--control-socket", control, "--quiet"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def page(n):
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), n, 0xF7)), control)
                return sock.recvfrom(512)[0][4:-1]
            def key(row, column):
                for down in (1, 0):
                    sock.sendto(bytes((0xF0, 0x7D, ord("K"), row, column, down, 0xF7)), control); time.sleep(0.25)
            try:
                time.sleep(1.0)
                sock.sendto(bytes((0xCE, 5)), control); time.sleep(0.4)
                program5 = page(1)[:0x88]
                sock.sendto(bytes((0xCE, 0)), control); time.sleep(0.4)
                program0 = page(1)[:0x88]
                self.assertEqual(page(0)[0x8C] & 0x40, 0)  # WRITE boots in Protect.
                key(2, 0); key(0, 3); time.sleep(0.3)  # keys 0, 5
                self.assertEqual(page(2)[0x1D], 5)
                self.assertEqual(page(1)[:0x88], program5)
                sock.sendto(bytes((0xF0, 0x7D, ord("S"), 0, 7, 1, 0xF7)), control); time.sleep(0.3)
                sock.sendto(bytes((0xCE, 0)), control); time.sleep(0.4)
                key(2, 0); key(0, 3); time.sleep(0.3)  # Enable: 05 is a save slot.
                self.assertEqual(page(2)[0x1D], 5)
                self.assertEqual(page(1)[:0x88], program0)
            finally:
                engine.terminate(); engine.wait(); sock.close()

    def test_named_midi_source_is_cabled_once(self):
        source = "Midi Through:Midi Through Port-0"
        listed = subprocess.run([str(BINARY), "--realtime", "--list-midi"], capture_output=True, text=True).stdout
        if source.replace(":", "\t", 1) not in listed.splitlines():
            self.skipTest("no Midi Through sequencer port")
        def cables(pid):
            # /proc lists each client's ports with what is subscribed to them.
            found, client, mine = [], "", False
            for line in Path("/proc/asound/seq/clients").read_text().splitlines():
                if line.startswith("Client"): client, mine = line.split()[1], False
                elif line.lstrip().startswith("Port"): mine = client == owner(pid) and "DrumStation MIDI In" in line
                elif mine and "Connected From" in line: found.append(line.split(":", 1)[1].strip())
            return found
        def owner(pid):
            for line in subprocess.run(["aconnect", "-l"], capture_output=True, text=True).stdout.splitlines():
                if f"pid={pid}]" in line: return line.split()[1].rstrip(":")
            return None
        with tempfile.TemporaryDirectory() as temp:
            engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"), "--audio", "null",
                                       "--midi-connect", source, "--eeprom", f"{temp}/eeprom.bin", "--quiet"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1.0)
                if owner(engine.pid) is None: self.skipTest("aconnect unavailable")
                self.assertEqual(cables(engine.pid), ["14:0"])
                # Unplugged by hand, it stays unplugged while the device stays.
                subprocess.run(["aconnect", "-d", "14:0", f"{owner(engine.pid)}:0"], check=True, capture_output=True)
                time.sleep(2.5)
                self.assertEqual(cables(engine.pid), [])
            finally:
                engine.terminate(); engine.wait()

    def test_saved_program_survives_engine_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            control, reply, eeprom = f"{temp}/control.sock", f"{temp}/reply.sock", Path(temp) / "eeprom.bin"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(reply); sock.settimeout(2)
            def start(*extra):
                engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"), "--audio", "null",
                                           "--midi", "none", "--control-socket", control, "--eeprom", str(eeprom),
                                           "--quiet", *extra], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.0)
                return engine
            def page(n):
                sock.sendto(bytes((0xF0, 0x7D, ord("Q"), n, 0xF7)), control)
                return sock.recvfrom(512)[0][4:-1]
            def keys(*positions):
                for row, column in positions:
                    for down in (1, 0):
                        sock.sendto(bytes((0xF0, 0x7D, ord("K"), row, column, down, 0xF7)), control); time.sleep(0.25)
                time.sleep(0.3)
            def write_switch(enabled):
                sock.sendto(bytes((0xF0, 0x7D, ord("S"), 0, 7, enabled, 0xF7)), control); time.sleep(0.3)
            zero, two, five, plus = (2, 0), (1, 2), (0, 3), (3, 1)
            try:
                engine = start("--channel", "7")
                try:
                    self.assertEqual(eeprom.stat().st_size, 2048)  # Created at startup.
                    factory = page(1)[:0x88]
                    sock.sendto(bytes((0xF0, 0x7D, ord("P"), 0x01, 0x01, 0x23, 0x7F, 0xF7)), control); time.sleep(0.3)
                    edited = page(1)[:0x88]
                    self.assertNotEqual(edited, factory)
                    write_switch(1); keys(two, five, plus); write_switch(0)
                    time.sleep(1.0)  # The file follows the save while the engine runs.
                    # Slot 25 is at the start; bytes past 0x80 are stored packed.
                    self.assertEqual(eeprom.read_bytes()[:0x80], edited[:0x80])
                finally:
                    engine.terminate(); engine.wait()
                engine = start()  # No --channel: the stored one is kept.
                try:
                    self.assertEqual(page(0xB7)[0xFE], 7)
                    # The firmware stores the last program and boots into it.
                    self.assertEqual(page(2)[0x1D], 25)
                    self.assertEqual(page(1)[:0x88], edited)
                    keys(zero, zero)
                    self.assertEqual(page(1)[:0x88], factory)
                    keys(two, five)
                    self.assertEqual(page(1)[:0x88], edited)
                finally:
                    engine.terminate(); engine.wait()
            finally:
                sock.close()

    def test_each_kit_card_hits_its_own_kit(self):
        # Both kits are live at once. The panel's HIT note for a kit's bass
        # drum must go silent when that kit's level byte is zeroed and keep
        # sounding when the other kit's is, whichever kit holds the GM map.
        from gui import LEVEL_ADDRESS, hit_note
        def peak(gm, note, muted):
            with tempfile.TemporaryDirectory() as temp:
                control, raw = f"{temp}/control.sock", f"{temp}/out.raw"
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"),
                                           "--audio", f"file:FILE={raw},FORMAT=raw", "--midi", "none", "--channel", "15",
                                           "--control-socket", control, "--quiet"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    time.sleep(1.0)
                    sock.sendto(bytes((0xF0, 0x7D, ord("P"), 0x01, 0x8B, gm, 0xFF, 0xF7)), control)
                    sock.sendto(bytes((0xF0, 0x7D, ord("L"), muted >> 8, muted & 0xFF, 0, 0xF7)), control)
                    time.sleep(0.3)
                    sock.sendto(bytes((0x9E, note, 110)), control); time.sleep(0.6)
                finally:
                    engine.terminate(); engine.wait(); sock.close()
                pcm = Path(raw).read_bytes()
                return max((abs(x[0]) for x in struct.iter_unpack("<h", pcm[:len(pcm) // 2 * 2])), default=0)
        level808, level909 = LEVEL_ADDRESS["BASS DRUM"]
        for gm in (0, 1):
            for kit, own, other in (("808", level808, level909), ("909", level909, level808)):
                note = hit_note(kit, "BASS DRUM", gm)
                with self.subTest(gm=gm, kit=kit, note=note):
                    self.assertEqual(peak(gm, note, own), 0)
                    self.assertGreater(peak(gm, note, other), 1000)

    def test_master_effects(self):
        # The engine's own compressor and reverb sit after the firmware's
        # output. Four loud bass drum hits, a silence, then a quiet one:
        # the left channel of the whole run and of the quiet hit alone.
        def run(compressor, reverb, tight=0):
            with tempfile.TemporaryDirectory() as temp:
                control, raw = f"{temp}/control.sock", f"{temp}/out.raw"
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                engine = subprocess.Popen([str(BINARY), "--realtime", "--rom-dir", str(ROOT / "roms"),
                                           "--audio", f"file:FILE={raw},FORMAT=raw", "--midi", "none", "--channel", "15",
                                           "--control-socket", control, "--quiet"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    time.sleep(1.0)
                    sock.sendto(bytes((0xF0, 0x7D, ord("X"), 0, compressor, 0xF7)), control)
                    sock.sendto(bytes((0xF0, 0x7D, ord("X"), 1, reverb, 0xF7)), control)
                    sock.sendto(bytes((0xF0, 0x7D, ord("X"), 2, tight, 0xF7)), control)
                    time.sleep(0.3)
                    for _ in range(4):
                        sock.sendto(bytes((0x9E, 36, 127)), control); time.sleep(0.2)
                    time.sleep(0.6)
                    sock.sendto(bytes((0x9E, 36, 20)), control); time.sleep(0.6)
                finally:
                    engine.terminate(); engine.wait(); sock.close()
                pcm = Path(raw).read_bytes()
                left = [x[0] for x in struct.iter_unpack("<hh", pcm[:len(pcm) // 4 * 4])]
                # The sink is not paced, so find the hits by the silence
                # before them: the last such onset is the quiet hit.
                onsets, silent = [], 4410
                for index, sample in enumerate(left):
                    if abs(sample) > 2:
                        if silent >= 4410: onsets.append(index)
                        silent = 0
                    else:
                        silent += 1
                return left[onsets[0]:], left[onsets[-1]:onsets[-1] + 8820]
        def rms(samples):
            return (sum(x * x for x in samples) / len(samples)) ** 0.5
        dry, dry_quiet = run(0, 0)
        self.assertEqual(rms(dry[-4410:]), 0)
        # The compressor brings the quiet hit up towards the loud ones, and
        # its make-up gain leaves the whole about as loud as it was.
        wet, wet_quiet = run(127, 0)
        self.assertGreater(rms(wet_quiet), 2 * rms(dry_quiet))
        self.assertLess(abs(rms(wet) / rms(dry) - 1), 0.16)
        # The reverb rings on in the silences the dry drum leaves, but it
        # is a short room: it too has died away by the end.
        def sounding(samples):
            return sum(abs(x) > 2 for x in samples) / len(samples)
        room = run(0, 127)[0]
        self.assertGreater(sounding(room), sounding(dry) + 0.15)
        self.assertLess(rms(room[-4410:]), 20)
        # Its other character, the tight room, is done sooner.
        self.assertGreater(sounding(run(0, 64)[0]), sounding(run(0, 64, 1)[0]) + 0.04)


if __name__ == "__main__":
    unittest.main()
