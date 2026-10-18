"""Focused model and transport tests for the DrumStation step sequencer."""
from pathlib import Path
import sys
import socket
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from sequencer import (CLOCK_SOURCES, EXTERNAL_CLOCK_TIMEOUT, PAGE_COUNT, PATTERN_COUNT,
                       SCALES, InternalDeadline, SequencerClock, SequencerProject,
                       page_step, step_gesture_toggles, step_seconds, ticks_per_step)
from gui import MidiClockReceiver


class SequencerModelTests(unittest.TestCase):
    def project(self, tracks=("808:BASS DRUM", "909:BASS DRUM")):
        return SequencerProject(None, tracks)

    def test_page_step_mapping_covers_all_64_steps(self):
        mapped = [page_step(page, step) for page in range(PAGE_COUNT) for step in range(16)]
        self.assertEqual(mapped, list(range(64)))
        self.assertEqual(page_step(3, 15), 63)
        with self.assertRaises(ValueError):
            page_step(4, 0)
        with self.assertRaises(ValueError):
            page_step(0, 16)

    def test_voice_tracks_are_independent(self):
        project = self.project()
        project.toggle_trig("808:BASS DRUM", 0)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 17)
        self.assertTrue(project.trig_on("808:BASS DRUM", 0))
        self.assertFalse(project.trig_on("909:BASS DRUM", 0))
        self.assertEqual(project.locks_for("909:BASS DRUM", 0), {})

    def test_patterns_are_independent_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequencer.json"
            project = SequencerProject(path, ("808:BASS DRUM", "909:BASS DRUM"))
            project.toggle_trig("808:BASS DRUM", 2)
            project.set_lock("808:BASS DRUM", 2, "cc:23", 91)
            project.select_pattern(1)
            project.toggle_trig("909:BASS DRUM", 37)
            project.set_lock("909:BASS DRUM", 37, "level:10b", 44)
            project.set_length(42)
            project.set_scale("1/32")
            project.set_bpm(137)
            loaded = SequencerProject(path, ("808:BASS DRUM", "909:BASS DRUM"))
            self.assertEqual(loaded.edit_pattern, 1)
            self.assertEqual((loaded.bpm, loaded.pattern().length, loaded.pattern().scale), (137, 42, "1/32"))
            self.assertFalse(loaded.trig_on("808:BASS DRUM", 2, 1))
            self.assertTrue(loaded.trig_on("808:BASS DRUM", 2, 0))
            self.assertEqual(loaded.locks_for("808:BASS DRUM", 2, 0), {"cc:23": 91})
            self.assertTrue(loaded.trig_on("909:BASS DRUM", 37, 1))
            self.assertEqual(loaded.locks_for("909:BASS DRUM", 37, 1), {"level:10b": 44})

    def test_tempo_scale_length_and_queued_pattern_wrap(self):
        project = self.project()
        self.assertEqual([step_seconds(120, scale) for scale in SCALES], [0.25, 0.125, 0.0625])
        project.set_bpm(300)
        project.set_scale("1/8")
        project.set_length(2)
        project.toggle_trig("808:BASS DRUM", 0)
        project.toggle_trig("808:BASS DRUM", 63)
        project.set_length(1)
        self.assertTrue(project.trig_on("808:BASS DRUM", 63))  # length does not erase stored steps
        project.set_length(2)
        first = project.start_transport()
        self.assertEqual((first.pattern, first.step, first.duration), (0, 0, 0.1))
        project.select_pattern(1)
        self.assertEqual(project.snapshot()["queued_pattern"], 1)
        second = project.advance_transport()
        self.assertEqual((second.pattern, second.step), (0, 1))
        wrapped = project.advance_transport()
        self.assertEqual((wrapped.pattern, wrapped.step), (1, 0))
        project.stop_transport()
        project.select_pattern(2)
        self.assertEqual(project.snapshot()["current_pattern"], 2)
        self.assertIsNone(project.snapshot()["queued_pattern"])

    def test_locked_hit_restores_base_before_unlocked_hit(self):
        project = self.project(("808:BASS DRUM",))
        project.set_base_values({"cc:23": 52})
        project.toggle_trig("808:BASS DRUM", 0)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 9)
        project.toggle_trig("808:BASS DRUM", 1)
        locked = project.start_transport()
        self.assertEqual(locked.hits[0].locks, {"cc:23": 9})
        self.assertEqual(project.transient_keys, {"cc:23"})
        unlocked = project.advance_transport()
        self.assertEqual(unlocked.restore, {"cc:23": 52})
        self.assertEqual(unlocked.hits[0].locks, {})
        self.assertEqual(project.transient_keys, set())
        self.assertEqual(project.base_value("cc:23"), 52)
        project.stop_transport()

    def test_consecutive_locks_on_different_parameters_do_not_carry(self):
        project = self.project(("808:BASS DRUM",))
        project.set_base_values({"cc:23": 60, "level:101": 83})
        for step in (0, 1):
            project.toggle_trig("808:BASS DRUM", step)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 5)
        project.set_lock("808:BASS DRUM", 1, "level:101", 12)
        first = project.start_transport()
        self.assertEqual(first.hits[0].locks, {"cc:23": 5})
        second = project.advance_transport()
        self.assertEqual(second.restore, {"cc:23": 60})
        self.assertEqual(second.hits[0].locks, {"level:101": 12})
        self.assertEqual(project.transient_keys, {"level:101"})
        project.stop_transport()

    def test_multiple_locks_clear_one_clear_all_and_follow_base_edits(self):
        project = self.project(("808:BASS DRUM",))
        project.set_base_values({"cc:23": 40, "level:101": 70})
        project.toggle_trig("808:BASS DRUM", 0)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 10)
        project.set_lock("808:BASS DRUM", 0, "level:101", 20)
        self.assertTrue(project.remove_lock("808:BASS DRUM", 0, "cc:23"))
        self.assertEqual(project.locks_for("808:BASS DRUM", 0), {"level:101": 20})
        project.set_lock("808:BASS DRUM", 0, "cc:23", 10)
        self.assertEqual(project.clear_locks("808:BASS DRUM", 0), 2)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 10)
        project.start_transport()
        project.set_base_value("cc:23", 99)
        unlocked = project.advance_transport()
        self.assertEqual(unlocked.restore, {"cc:23": 99})
        project.stop_transport()

    def test_simultaneous_voices_keep_locks_separate(self):
        project = self.project(("808:BASS DRUM", "909:SNARE"))
        project.set_base_values({"cc:23": 80, "cc:29": 65})
        for track, key, value in (("808:BASS DRUM", "cc:23", 10), ("909:SNARE", "cc:29", 120)):
            project.toggle_trig(track, 0)
            project.set_lock(track, 0, key, value)
        event = project.start_transport()
        self.assertEqual({hit.track for hit in event.hits}, {"808:BASS DRUM", "909:SNARE"})
        self.assertEqual({hit.track: hit.locks for hit in event.hits},
                         {"808:BASS DRUM": {"cc:23": 10}, "909:SNARE": {"cc:29": 120}})
        next_event = project.advance_transport()
        self.assertEqual(next_event.restore, {"cc:23": 80, "cc:29": 65})
        self.assertFalse(next_event.hits)
        project.stop_transport()

    def test_hold_edit_release_never_toggles_the_trig(self):
        self.assertTrue(step_gesture_toggles(False, 0.1, True))
        self.assertFalse(step_gesture_toggles(True, 0.1, True))
        self.assertFalse(step_gesture_toggles(False, 0.1, False))
        self.assertFalse(step_gesture_toggles(False, 0.6, True))

    def test_internal_deadline_has_no_accumulated_virtual_time_drift(self):
        project = self.project(("808:BASS DRUM",))
        project.set_length(64)
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        self.assertTrue(clock.start(worker=False, now=0.0))
        for _ in range(100_000):
            deadline = clock._timeline.deadline
            self.assertEqual(len(clock.pump_internal(deadline)), 1)
        self.assertAlmostEqual(clock._timeline.deadline, 100_001 * 0.125, places=9)
        self.assertEqual(clock.overruns, 0)
        clock.stop()

    def test_late_wakeup_skips_overdue_steps_without_a_catchup_burst(self):
        project = self.project(("808:BASS DRUM",))
        project.set_length(64)
        project.toggle_trig("808:BASS DRUM", 1)
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        clock.start(worker=False, now=0.0)
        on_time_phase = clock.pump_internal(0.145)
        self.assertEqual([(event.step, event.hits != ()) for event in on_time_phase], [(1, True)])
        self.assertAlmostEqual(clock._timeline.deadline, 0.25)
        late_phase = clock.pump_internal(1.0)
        self.assertEqual(len(late_phase), 1)
        self.assertEqual((late_phase[0].step, late_phase[0].hits, late_phase[0].restore), (8, (), {}))
        self.assertAlmostEqual(clock._timeline.deadline, 1.125)
        self.assertEqual(clock.overruns, 7)
        clock.stop()

    def test_internal_start_stop_restart_cancels_old_worker(self):
        project = self.project(("808:BASS DRUM",))
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        self.assertTrue(clock.start())
        clock.stop()
        self.assertFalse(clock.alive)
        self.assertTrue(clock.start())
        clock.stop()
        self.assertFalse(clock.alive)
        self.assertEqual([(event.step, event.stopped) for event in emitted],
                         [(0, False), (0, True), (0, False), (0, True)])

    def test_edits_during_internal_playback_apply_at_safe_step_boundaries(self):
        project = self.project(("808:BASS DRUM",))
        project.set_length(2)
        project.toggle_trig("808:BASS DRUM", 0)
        project.toggle_trig("808:BASS DRUM", 1)
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        clock.start(worker=False, now=0.0)
        project.set_bpm(60)
        project.set_scale("1/8")
        self.assertEqual(clock.pump_internal(0.125)[0].step, 1)
        self.assertEqual(emitted[-1].duration, 0.5)
        project.set_length(1)
        project.select_pattern(1)
        wrapped = clock.pump_internal(0.625)
        self.assertEqual((wrapped[-1].pattern, wrapped[-1].step), (1, 0))
        clock.stop()

    def test_external_tick_mapping_is_exact_for_every_scale(self):
        for scale in SCALES:
            with self.subTest(scale=scale):
                project = self.project(("808:BASS DRUM",))
                project.set_scale(scale)
                project.set_length(4)
                project.toggle_trig("808:BASS DRUM", 0)
                project.toggle_trig("808:BASS DRUM", 1)
                emitted = []
                clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
                clock.set_source("external")
                self.assertEqual(ticks_per_step(scale), {"1/8": 12, "1/16": 6, "1/32": 3}[scale])
                clock.external_message(0xF8, now=0.0)  # tick before Start is status only
                clock.external_message(0xFB, now=0.001)  # Continue before Start is ignored
                self.assertIsNone(project.snapshot()["current_step"])
                clock.external_message(0xFA, now=0.002)
                for tick in range(ticks_per_step(scale) - 1):
                    clock.external_message(0xF8, now=0.01 + tick * 0.003)
                self.assertEqual(len(emitted), 1)
                clock.external_message(0xF8, now=0.08)
                self.assertEqual((emitted[-1].step, emitted[-1].hits[0].track), (1, "808:BASS DRUM"))
                clock.stop()

    def test_external_start_continue_stop_duplicates_and_partial_phase(self):
        project = self.project(("808:BASS DRUM",))
        project.set_clock_source("external")
        project.set_base_values({"cc:23": 61})
        project.toggle_trig("808:BASS DRUM", 0)
        project.toggle_trig("808:BASS DRUM", 1)
        project.set_lock("808:BASS DRUM", 0, "cc:23", 10)
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        clock.external_message(0xFA, now=0.0)
        self.assertEqual((emitted[-1].step, emitted[-1].hits[0].locks), (0, {"cc:23": 10}))
        clock.external_message(0xFA, now=0.001)  # duplicate Start restarts at zero and restores
        self.assertEqual((emitted[-1].step, emitted[-1].restore), (0, {"cc:23": 61}))
        for tick in range(2):
            clock.external_message(0xF8, now=0.01 + tick * 0.01)
        clock.external_message(0xFC, now=0.03)
        self.assertTrue(emitted[-1].stopped)
        count = len(emitted)
        clock.external_message(0xFC, now=0.04)  # duplicate Stop does not restore twice
        self.assertEqual(len(emitted), count)
        clock.external_message(0xFB, now=0.05)
        clock.external_message(0xFB, now=0.06)  # duplicate Continue does not reset phase
        for tick in range(4):
            clock.external_message(0xF8, now=0.07 + tick * 0.02)
        self.assertEqual((emitted[-1].step, emitted[-1].hits[0].track), (1, "808:BASS DRUM"))

    def test_external_dropout_waits_preserves_partial_phase_and_reports_presence(self):
        project = self.project(("808:BASS DRUM",))
        project.set_clock_source("external")
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        clock.external_message(0xFA, now=0.0)
        clock.external_message(0xF8, now=0.01)
        clock.external_message(0xF8, now=0.02)
        count = len(emitted)
        self.assertEqual(clock.snapshot(now=0.02)["clock_status"], "CLOCK PRESENT")
        self.assertEqual(clock.snapshot(now=0.02 + EXTERNAL_CLOCK_TIMEOUT + 0.01)["clock_status"], "CLOCK LOST")
        clock.external_message(0xFB, now=0.6)
        for tick in range(3):
            clock.external_message(0xF8, now=0.7 + tick * 0.01)
        self.assertEqual(len(emitted), count)
        clock.external_message(0xF8, now=0.74)
        self.assertEqual(emitted[-1].step, 1)

    def test_external_queue_gap_stops_until_a_fresh_start(self):
        project = self.project(("808:BASS DRUM",))
        project.set_clock_source("external")
        emitted = []
        clock = SequencerClock(project, emitted.append, clock=lambda: 0.0)
        clock.external_message(0xFA, now=0.0)
        clock.external_message(0xF8, now=0.01)
        clock.transport_gap(2)
        self.assertTrue(emitted[-1].stopped)
        self.assertFalse(project.playing)
        clock.external_message(0xFB, now=0.02)
        clock.external_message(0xF8, now=0.03)
        self.assertFalse(project.playing)
        self.assertEqual(clock.snapshot(now=0.03)["clock_gaps"], 2)
        clock.external_message(0xFA, now=0.04)
        self.assertEqual(emitted[-1].step, 0)

    def test_source_switch_stops_and_project_defaults_legacy_files_to_internal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequencer.json"
            project = SequencerProject(path, ("808:BASS DRUM",))
            project.toggle_trig("808:BASS DRUM", 0)
            raw = __import__("json").loads(path.read_text())
            raw.pop("clock_source")
            path.write_text(__import__("json").dumps(raw))
            legacy = SequencerProject(path, ("808:BASS DRUM",))
            self.assertEqual(legacy.snapshot()["clock_source"], "internal")
            emitted = []
            clock = SequencerClock(legacy, emitted.append, clock=lambda: 0.0)
            clock.start(worker=False, now=0.0)
            clock.set_source("external")
            self.assertEqual(legacy.snapshot()["clock_source"], "external")
            self.assertFalse(legacy.snapshot()["playing"])
            self.assertTrue(emitted[-1].stopped)
            before_tick = len(emitted)
            clock.external_message(0xF8, now=1.0)
            self.assertEqual(legacy.snapshot()["current_step"], 0)
            self.assertEqual(len(emitted), before_tick)
            clock.external_message(0xFA, now=1.01)
            self.assertEqual(emitted[-1].step, 0)
            self.assertEqual(CLOCK_SOURCES, ("internal", "external"))

    def test_concurrent_snapshots_and_saves_are_valid_and_coherent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequencer.json"
            project = self.project(("808:BASS DRUM", "909:SNARE"))
            project.path = path
            project.start_transport()
            project.save()
            barrier = __import__("threading").Barrier(3)
            finished = __import__("threading").Event()
            errors = []

            def editor():
                barrier.wait()
                for i in range(120):
                    step = i % 64
                    project.set_lock("808:BASS DRUM", step, "cc:23", i % 128, save=False)
                    project.ensure_trig("909:SNARE", step)
                    if i % 12 == 0:
                        project.save()
                project.save()
                finished.set()

            def reader():
                barrier.wait()
                while not finished.is_set():
                    try:
                        raw = __import__("json").loads(path.read_text())
                        for track_state in raw["patterns"][0]["tracks"].values():
                            trigs = set(track_state["trigs"])
                            self.assertTrue(set(map(int, track_state["locks"])).issubset(trigs))
                        project.snapshot()
                        project.pattern(0)
                    except Exception as error:
                        errors.append(error)
                        return

            threads = [__import__("threading").Thread(target=editor),
                       __import__("threading").Thread(target=reader)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            loaded = SequencerProject(path, ("808:BASS DRUM", "909:SNARE"))
            self.assertTrue(loaded.locks_for("808:BASS DRUM", 0, 0))
            self.assertTrue(loaded.trig_on("909:SNARE", 55, 0))
            project.stop_transport()

    def test_pattern_reads_are_detached_and_burst_hits_keep_all_locks(self):
        tracks = tuple(f"TR:{i}" for i in range(27))
        project = SequencerProject(None, tracks)
        for track in tracks:
            project.toggle_trig(track, 0)
            for lock in range(4):
                project.set_lock(track, 0, f"cc:{lock}", lock + 1)
        detached = project.pattern(0)
        detached.tracks[tracks[0]].trigs.clear()
        event = project.start_transport()
        self.assertEqual(len(event.hits), 27)
        self.assertEqual(sum(len(hit.locks) for hit in event.hits), 108)
        next_event = project.advance_transport()
        self.assertEqual(len(next_event.restore), 4)
        self.assertTrue(project.trig_on(tracks[0], 0, 0))
        project.stop_transport()


class MidiClockReceiverTests(unittest.TestCase):
    def test_protocol_preserves_timestamp_and_reports_sequence_gaps(self):
        class Probe:
            def __init__(self):
                self.events = []
                self.gaps = []
                self.errors = []
                self.lock = threading.Lock()
                self.ready = threading.Event()

            def external_message(self, status, now):
                with self.lock:
                    self.events.append((status, now, time.monotonic()))
                    if len(self.events) >= 2:
                        self.ready.set()

            def transport_gap(self, lost):
                with self.lock:
                    self.gaps.append(lost)

            def record_error(self, error):
                with self.lock:
                    self.errors.append(error)

        with tempfile.TemporaryDirectory() as directory:
            receiver_path = Path(directory) / "clock.sock"
            sender_path = Path(directory) / "sender.sock"
            probe = Probe()
            receiver = MidiClockReceiver(str(receiver_path), probe)
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sender.bind(str(sender_path))
            receiver.start()
            try:
                def packet(sequence, status, delta_us):
                    return (b"\xf0\x7dK" + sequence.to_bytes(4, "big") +
                            delta_us.to_bytes(4, "big", signed=True) + bytes((status, 0xf7)))

                sender.sendto(packet(10, 0xF8, -20_000), str(receiver_path))
                sender.sendto(packet(12, 0xFA, 0), str(receiver_path))
                self.assertTrue(probe.ready.wait(1.0), "clock receiver did not handle its datagrams")
            finally:
                receiver.close()
                sender.close()
            self.assertEqual([status for status, _when, _arrival in probe.events], [0xF8, 0xFA])
            self.assertEqual(probe.gaps, [1])
            self.assertFalse(probe.errors)
            self.assertAlmostEqual(probe.events[0][2] - probe.events[0][1], 0.02, delta=0.01)


if __name__ == "__main__":
    unittest.main()
