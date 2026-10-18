"""Pattern data and deterministic internal/external clocks for the DrumStation panel."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time


STEPS_PER_PAGE = 16
PAGE_COUNT = 4
MAX_STEPS = STEPS_PER_PAGE * PAGE_COUNT
PATTERN_COUNT = 8
SCALES = ("1/8", "1/16", "1/32")
CLOCK_SOURCES = ("internal", "external")
DEFAULT_BPM = 120.0
EXTERNAL_CLOCK_TIMEOUT = 0.5
MAX_TRANSITION_SCAN = MAX_STEPS * 2 + 2


def page_step(page: int, visible_step: int) -> int:
    """Map zero-based page and visible-step indices to a zero-based pattern step."""
    if not 0 <= page < PAGE_COUNT:
        raise ValueError("page must be in 0..3")
    if not 0 <= visible_step < STEPS_PER_PAGE:
        raise ValueError("visible_step must be in 0..15")
    return page * STEPS_PER_PAGE + visible_step


def step_seconds(bpm: float, scale: str) -> float:
    """Duration of one eighth, sixteenth, or thirty-second note."""
    if not 20 <= float(bpm) <= 300:
        raise ValueError("tempo must be between 20 and 300 BPM")
    if scale not in SCALES:
        raise ValueError(f"unsupported rhythmic scale: {scale}")
    denominator = int(scale.split("/")[1])
    return 60.0 / float(bpm) * 4.0 / denominator


def ticks_per_step(scale: str) -> int:
    """Integer MIDI Clock ticks in an eighth, sixteenth, or thirty-second note."""
    if scale not in SCALES:
        raise ValueError(f"unsupported rhythmic scale: {scale}")
    return 96 // int(scale.split("/")[1])


def step_gesture_toggles(consumed: bool, elapsed: float, released_on_step: bool,
                         hold_threshold: float = 0.45) -> bool:
    """Only an unconsumed short press released on its cell toggles a trig."""
    return not consumed and released_on_step and elapsed < hold_threshold


@dataclass
class TrackPattern:
    trigs: set[int] = field(default_factory=set)
    locks: dict[int, dict[str, int]] = field(default_factory=dict)


@dataclass
class Pattern:
    length: int = 16
    scale: str = "1/16"
    tracks: dict[str, TrackPattern] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackHit:
    track: str
    locks: dict[str, int]


@dataclass(frozen=True)
class StepEvent:
    pattern: int
    step: int | None
    duration: float
    hits: tuple[TrackHit, ...] = ()
    restore: dict[str, int] = field(default_factory=dict)
    stopped: bool = False
    scale: str = "1/16"


class SequencerProject:
    """Eight independent 64-step patterns with atomic, coherent JSON persistence."""

    def __init__(self, path: Path | None = None, tracks: tuple[str, ...] = ()):
        self.path = Path(path) if path is not None else None
        self.track_ids = tuple(tracks)
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._revision = 0
        self.bpm = DEFAULT_BPM
        self.clock_source = "internal"
        self.patterns = [Pattern(tracks={track: TrackPattern() for track in tracks})
                         for _ in range(PATTERN_COUNT)]
        self.edit_pattern = 0
        self.current_pattern = 0
        self.queued_pattern: int | None = None
        self.current_step: int | None = None
        self.playing = False
        self.base_values: dict[str, int] = {}
        self.transient_keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1 or len(raw.get("patterns", ())) != PATTERN_COUNT:
                return
            bpm = float(raw.get("bpm", DEFAULT_BPM))
            if not 20 <= bpm <= 300:
                return
            source = raw.get("clock_source", "internal")
            if source not in CLOCK_SOURCES:
                source = "internal"
            patterns: list[Pattern] = []
            for item in raw["patterns"]:
                length = int(item.get("length", 16))
                scale = item.get("scale", "1/16")
                if not 1 <= length <= MAX_STEPS or scale not in SCALES:
                    return
                tracks = {}
                for track, state in item.get("tracks", {}).items():
                    trigs = {int(step) for step in state.get("trigs", ()) if 0 <= int(step) < MAX_STEPS}
                    locks = {}
                    for step, values in state.get("locks", {}).items():
                        index = int(step)
                        if index not in trigs or not 0 <= index < MAX_STEPS:
                            continue
                        clean = {str(key): max(0, min(127, int(value)))
                                 for key, value in values.items() if isinstance(key, str)}
                        if clean:
                            locks[index] = clean
                    tracks[str(track)] = TrackPattern(trigs, locks)
                patterns.append(Pattern(length, scale, tracks))
            for pattern in patterns:
                for track in self.track_ids:
                    pattern.tracks.setdefault(track, TrackPattern())
            selected = int(raw.get("selected_pattern", 0))
            if not 0 <= selected < PATTERN_COUNT:
                selected = 0
            self.bpm = bpm
            self.clock_source = source
            self.patterns = patterns
            self.edit_pattern = self.current_pattern = selected
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            # A partial or older file must never keep the panel from opening.
            return

    def _track(self, pattern: int, track: str) -> TrackPattern:
        return self.patterns[pattern].tracks.setdefault(track, TrackPattern())

    def _mark_dirty_locked(self) -> None:
        self._revision += 1

    def _snapshot_locked(self) -> dict:
        return {
            "version": 1,
            "bpm": self.bpm,
            "clock_source": self.clock_source,
            "selected_pattern": self.edit_pattern,
            "patterns": [
                {
                    "length": pattern.length,
                    "scale": pattern.scale,
                    "tracks": {
                        track: {
                            "trigs": sorted(state.trigs),
                            "locks": {str(step): values.copy() for step, values in sorted(state.locks.items())},
                        }
                        for track, state in sorted(pattern.tracks.items())
                    },
                }
                for pattern in self.patterns
            ],
        }

    def _write_snapshot(self, raw: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            encoded = (json.dumps(raw, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with temporary.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _persist_latest(self) -> None:
        if self.path is None:
            return
        # Every writer snapshots the current model after acquiring this lock, so a slow older
        # save cannot replace a newer project image.
        with self._save_lock:
            with self._lock:
                raw = self._snapshot_locked()
            self._write_snapshot(raw)

    def save(self) -> None:
        self._persist_latest()

    def set_base_values(self, values: dict[str, int]) -> None:
        with self._lock:
            self.base_values = {str(key): max(0, min(127, int(value))) for key, value in values.items()}

    def set_base_value(self, key: str, value: int) -> None:
        with self._lock:
            self.base_values[key] = max(0, min(127, int(value)))

    def base_value(self, key: str, fallback: int = 64) -> int:
        with self._lock:
            return self.base_values.get(key, fallback)

    def pattern(self, index: int | None = None) -> Pattern:
        """Return a detached pattern copy so a GUI reader cannot race a model edit."""
        with self._lock:
            source = self.patterns[self.edit_pattern if index is None else index]
            return Pattern(source.length, source.scale, {
                track: TrackPattern(set(state.trigs), {step: values.copy() for step, values in state.locks.items()})
                for track, state in source.tracks.items()
            })

    def trig_on(self, track: str, step: int, pattern: int | None = None) -> bool:
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            return step in self._track(p, track).trigs

    def toggle_trig(self, track: str, step: int, pattern: int | None = None) -> bool:
        if not 0 <= step < MAX_STEPS:
            raise ValueError("step must be in 0..63")
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            state = self._track(p, track)
            if step in state.trigs:
                state.trigs.remove(step)
                state.locks.pop(step, None)
                enabled = False
            else:
                state.trigs.add(step)
                enabled = True
            self._mark_dirty_locked()
        self._persist_latest()
        return enabled

    def ensure_trig(self, track: str, step: int, pattern: int | None = None) -> None:
        if not 0 <= step < MAX_STEPS:
            raise ValueError("step must be in 0..63")
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            self._track(p, track).trigs.add(step)
            self._mark_dirty_locked()
        self._persist_latest()

    def locks_for(self, track: str, step: int, pattern: int | None = None) -> dict[str, int]:
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            return dict(self._track(p, track).locks.get(step, {}))

    def set_lock(self, track: str, step: int, key: str, value: int,
                 pattern: int | None = None, *, save: bool = True) -> None:
        if not key or not 0 <= step < MAX_STEPS:
            raise ValueError("parameter lock needs a key and a step in 0..63")
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            state = self._track(p, track)
            state.trigs.add(step)
            state.locks.setdefault(step, {})[key] = max(0, min(127, int(value)))
            if save:
                self._mark_dirty_locked()
        if save:
            self._persist_latest()

    def remove_lock(self, track: str, step: int, key: str,
                    pattern: int | None = None) -> bool:
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            state = self._track(p, track)
            values = state.locks.get(step)
            removed = bool(values and values.pop(key, None) is not None)
            if values == {}:
                state.locks.pop(step, None)
            if removed:
                self._mark_dirty_locked()
        if removed:
            self._persist_latest()
        return removed

    def clear_locks(self, track: str, step: int, pattern: int | None = None) -> int:
        with self._lock:
            p = self.edit_pattern if pattern is None else pattern
            state = self._track(p, track)
            removed = len(state.locks.pop(step, {}))
            if removed:
                self._mark_dirty_locked()
        if removed:
            self._persist_latest()
        return removed

    def set_bpm(self, bpm: float) -> None:
        bpm = max(20.0, min(300.0, float(bpm)))
        with self._lock:
            self.bpm = bpm
            self._mark_dirty_locked()
        self._persist_latest()

    def set_clock_source(self, source: str) -> None:
        if source not in CLOCK_SOURCES:
            raise ValueError(f"unsupported clock source: {source}")
        with self._lock:
            if self.clock_source == source:
                return
            self.clock_source = source
            self._mark_dirty_locked()
        self._persist_latest()

    def set_length(self, length: int) -> None:
        with self._lock:
            self.patterns[self.edit_pattern].length = max(1, min(MAX_STEPS, int(length)))
            self._mark_dirty_locked()
        self._persist_latest()

    def set_scale(self, scale: str) -> None:
        if scale not in SCALES:
            raise ValueError(f"unsupported rhythmic scale: {scale}")
        with self._lock:
            self.patterns[self.edit_pattern].scale = scale
            self._mark_dirty_locked()
        self._persist_latest()

    def select_pattern(self, index: int) -> None:
        if not 0 <= index < PATTERN_COUNT:
            raise ValueError("pattern must be in 0..7")
        with self._lock:
            self.edit_pattern = index
            if self.playing:
                self.queued_pattern = index
            else:
                self.current_pattern = index
                self.current_step = None
                self.queued_pattern = None
            self._mark_dirty_locked()
        self._persist_latest()

    def _restore_locked(self) -> dict[str, int]:
        return {key: self.base_values.get(key, 64) for key in self.transient_keys}

    def _event_locked(self, pattern: int, step: int, restore: dict[str, int] | None = None) -> StepEvent:
        current = self.patterns[pattern]
        restore = self._restore_locked() if restore is None else restore
        hits = []
        active_keys: set[str] = set()
        for track, state in current.tracks.items():
            if step in state.trigs:
                values = dict(state.locks.get(step, {}))
                active_keys.update(values)
                hits.append(TrackHit(track, values))
        self.transient_keys = active_keys
        self.current_step = step
        return StepEvent(pattern, step, step_seconds(self.bpm, current.scale), tuple(hits), restore,
                         scale=current.scale)

    def start_transport(self) -> StepEvent:
        """Reset to step zero. Existing locks are restored in the same ordered event."""
        with self._lock:
            self.playing = True
            self.current_pattern = self.edit_pattern
            self.queued_pattern = None
            self.current_step = -1
            return self._event_locked(self.current_pattern, 0)

    def resume_transport(self) -> bool:
        with self._lock:
            if self.current_step is None:
                return False
            self.playing = True
            return True

    def advance_transport(self) -> StepEvent:
        with self._lock:
            if not self.playing or self.current_step is None:
                raise RuntimeError("sequencer is stopped")
            current = self.patterns[self.current_pattern]
            next_step = self.current_step + 1
            if next_step >= current.length:
                if self.queued_pattern is not None:
                    self.current_pattern = self.queued_pattern
                    self.edit_pattern = self.queued_pattern
                    self.queued_pattern = None
                next_step = 0
            return self._event_locked(self.current_pattern, next_step)

    def skip_transport(self, steps: int = 1) -> StepEvent:
        """Move the playhead without triggers, restoring locks from the last emitted step."""
        if steps < 1:
            raise ValueError("at least one transport step must be skipped")
        with self._lock:
            if not self.playing or self.current_step is None:
                raise RuntimeError("sequencer is stopped")
            restore = self._restore_locked()
            self.transient_keys.clear()
            pattern = self.patterns[self.current_pattern]
            until_wrap = pattern.length - self.current_step
            if steps < until_wrap:
                self.current_step += steps
            else:
                steps -= until_wrap
                if self.queued_pattern is not None:
                    self.current_pattern = self.edit_pattern = self.queued_pattern
                    self.queued_pattern = None
                self.current_step = 0
                length = self.patterns[self.current_pattern].length
                self.current_step = (self.current_step + steps) % length
            current = self.patterns[self.current_pattern]
            return StepEvent(self.current_pattern, self.current_step,
                             step_seconds(self.bpm, current.scale), (), restore,
                             scale=current.scale)

    def stop_transport(self) -> StepEvent:
        with self._lock:
            pattern = self.current_pattern
            restore = self._restore_locked()
            self.transient_keys.clear()
            self.playing = False
            if self.queued_pattern is not None:
                self.current_pattern = self.edit_pattern = self.queued_pattern
                pattern = self.current_pattern
                self.queued_pattern = None
                if self.current_step is not None:
                    self.current_step %= self.patterns[pattern].length
            current = self.patterns[pattern]
            return StepEvent(pattern, self.current_step, 0.0, (), restore, stopped=True,
                             scale=current.scale)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            pattern = self.patterns[self.edit_pattern]
            return {
                "bpm": self.bpm,
                "clock_source": self.clock_source,
                "edit_pattern": self.edit_pattern,
                "current_pattern": self.current_pattern,
                "queued_pattern": self.queued_pattern,
                "current_step": self.current_step,
                "playing": self.playing,
                "length": pattern.length,
                "scale": pattern.scale,
                "transient_keys": set(self.transient_keys),
            }


class InternalDeadline:
    """Absolute monotonic step deadlines; lateness never shifts the stored phase."""

    def __init__(self, start: float, first_duration: float):
        self.deadline = float(start) + float(first_duration)
        self.interval = float(first_duration)

    def due(self, now: float) -> bool:
        return now >= self.deadline

    def advance(self, duration: float) -> None:
        self.deadline += float(duration)
        self.interval = float(duration)


class SequencerClock:
    """Internal absolute-deadline worker and tick-counted external MIDI transport."""

    def __init__(self, project: SequencerProject, on_step, *, clock=time.monotonic):
        self.project = project
        self.on_step = on_step
        self._now = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._timeline: InternalDeadline | None = None
        self.error: Exception | None = None
        self.overruns = 0
        self.late_wakeups = 0
        self.max_lateness = 0.0
        self._last_clock: float | None = None
        self._clock_count = 0
        self._clock_gap_count = 0
        self._external_started = False
        self._ticks_into_step = 0
        self._ticks_for_step = ticks_per_step("1/16")
        self._closed = False

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        now = self._now() if now is None else float(now)
        state = self.project.snapshot()
        with self._lock:
            source = str(state["clock_source"])
            last = self._last_clock
            if source == "internal":
                clock_status = "INTERNAL"
                present = False
            elif last is None:
                clock_status = "WAITING FOR CLOCK"
                present = False
            elif now - last > EXTERNAL_CLOCK_TIMEOUT:
                clock_status = "CLOCK LOST"
                present = False
            else:
                clock_status = "CLOCK PRESENT"
                present = True
            if source == "external" and not self._external_started:
                clock_status += " • WAITING FOR START"
            if self.error is not None:
                clock_status = f"CLOCK ERROR: {self.error}"
            return {
                **state,
                "clock_status": clock_status,
                "clock_present": present,
                "external_started": self._external_started,
                "ticks_into_step": self._ticks_into_step,
                "clock_count": self._clock_count,
                "clock_gaps": self._clock_gap_count,
                "overruns": self.overruns,
                "late_wakeups": self.late_wakeups,
                "max_lateness": self.max_lateness,
                "error": self.error,
            }

    def _emit_locked(self, event: StepEvent) -> None:
        self.on_step(event)

    def start(self, *, worker: bool = True, now: float | None = None) -> bool:
        now = self._now() if now is None else float(now)
        with self._lock:
            if self._closed:
                return False
            state = self.project.snapshot()
            if state["clock_source"] == "external":
                if not self._external_started or state["playing"]:
                    return False
                return self.project.resume_transport()
            if state["playing"] or (self._thread is not None and self._thread.is_alive()):
                return False
            self.error = None
            self._generation += 1
            generation = self._generation
            self._stop = threading.Event()
            first = self.project.start_transport()
            self._timeline = InternalDeadline(now, first.duration)
            self._emit_locked(first)
            if worker:
                thread = threading.Thread(target=self._run, args=(generation, self._stop),
                                          name="drumstation-sequencer", daemon=True)
                self._thread = thread
                thread.start()
            else:
                self._thread = None
            return True

    def _skip_overdue_locked(self, now: float, timeline: InternalDeadline) -> tuple[int, StepEvent | None]:
        skipped = 0
        restore: dict[str, int] = {}
        final: StepEvent | None = None
        while timeline.due(now):
            if skipped >= MAX_TRANSITION_SCAN:
                # At this point any queued pattern transition has been crossed. Future steps have
                # one fixed duration, so skip the remaining whole intervals arithmetically.
                count = max(1, int((now - timeline.deadline) // timeline.interval) + 1)
                final = self.project.skip_transport(count)
                timeline.deadline += count * timeline.interval
                skipped += count
                break
            final = self.project.skip_transport(1)
            restore.update(final.restore)
            timeline.advance(final.duration)
            skipped += 1
            if self.project.snapshot()["queued_pattern"] is None and timeline.due(now):
                count = max(1, int((now - timeline.deadline) // final.duration) + 1)
                final = self.project.skip_transport(count)
                timeline.deadline += count * final.duration
                timeline.interval = final.duration
                skipped += count
                break
        if final is not None and restore:
            final = StepEvent(final.pattern, final.step, final.duration, (), restore,
                              scale=final.scale)
        return skipped, final

    def pump_internal(self, now: float) -> tuple[StepEvent, ...]:
        """Process one virtual-time wakeup. Used by the worker and deterministic tests."""
        emitted: list[StepEvent] = []
        with self._lock:
            if self.project.snapshot()["clock_source"] != "internal" or not self.project.playing:
                return ()
            timeline = self._timeline
            if timeline is None or not timeline.due(now):
                return ()
            lateness = max(0.0, now - timeline.deadline)
            if lateness:
                self.late_wakeups += 1
                self.max_lateness = max(self.max_lateness, lateness)
            if lateness <= timeline.interval * 0.5:
                event = self.project.advance_transport()
                timeline.advance(event.duration)
                self._emit_locked(event)
                emitted.append(event)
            skipped, event = self._skip_overdue_locked(now, timeline)
            if skipped:
                self.overruns += skipped
                if event is not None:
                    self._emit_locked(event)
                    emitted.append(event)
            return tuple(emitted)

    def _run(self, generation: int, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                with self._lock:
                    if generation != self._generation or self._timeline is None:
                        break
                    delay = self._timeline.deadline - self._now()
                if delay > 0 and stop.wait(delay):
                    break
                if stop.is_set():
                    break
                self.pump_internal(self._now())
        except Exception as error:
            with self._lock:
                if generation == self._generation:
                    self.error = error
                    self._generation += 1
                    self._stop.set()
                    if self.project.playing:
                        stopped = self.project.stop_transport()
                        try:
                            self._emit_locked(stopped)
                        except Exception:
                            pass
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def external_message(self, status: int, now: float | None = None) -> bool:
        """Consume one MIDI System Real-Time transport byte; ordinary MIDI is never passed here."""
        if status not in (0xF8, 0xFA, 0xFB, 0xFC):
            return False
        now = self._now() if now is None else float(now)
        with self._lock:
            if self._closed:
                return False
            if self.project.snapshot()["clock_source"] != "external":
                return False
            if status == 0xF8:
                self._last_clock = now
                self._clock_count += 1
                if not self._external_started or not self.project.playing:
                    return True
                self._ticks_into_step += 1
                if self._ticks_into_step >= self._ticks_for_step:
                    self._ticks_into_step = 0
                    event = self.project.advance_transport()
                    self._ticks_for_step = ticks_per_step(event.scale)
                    self._emit_locked(event)
                return True
            if status == 0xFA:
                self.error = None
                self._last_clock = now
                self._external_started = True
                self._ticks_into_step = 0
                self._generation += 1
                event = self.project.start_transport()
                self._ticks_for_step = ticks_per_step(event.scale)
                self._emit_locked(event)
                return True
            if status == 0xFB:
                if not self._external_started or self.project.playing:
                    return True
                self.project.resume_transport()
                return True
            if self.project.playing:
                self._emit_locked(self.project.stop_transport())
            return True

    def transport_gap(self, count: int = 1) -> None:
        """Stop and require a new Start when a bounded clock queue/IPC reports lost messages."""
        with self._lock:
            self._clock_gap_count += max(1, int(count))
            if self.project.snapshot()["clock_source"] != "external":
                return
            self._external_started = False
            self._ticks_into_step = 0
            self._last_clock = None
            if self.project.playing:
                self._emit_locked(self.project.stop_transport())
            self.error = f"{max(1, int(count))} MIDI clock message(s) dropped; waiting for Start"

    def record_error(self, error: Exception) -> None:
        with self._lock:
            self.error = error
            self._external_started = False
            self._timeline = None
            self._generation += 1
            self._stop.set()
            if self.project.playing:
                event = self.project.stop_transport()
                try:
                    self._emit_locked(event)
                except Exception:
                    pass

    def set_source(self, source: str) -> None:
        if source not in CLOCK_SOURCES:
            raise ValueError(f"unsupported clock source: {source}")
        self.stop()
        with self._lock:
            self._generation += 1
            self._timeline = None
            self._external_started = False
            self._ticks_into_step = 0
            self._ticks_for_step = ticks_per_step("1/16")
            self._last_clock = None
            self.error = None
            self.project.set_clock_source(source)

    def stop(self) -> None:
        thread: threading.Thread | None
        with self._lock:
            self._generation += 1
            self._stop.set()
            thread = self._thread
            self._timeline = None
            if self.project.playing:
                self._emit_locked(self.project.stop_transport())
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if self._thread is thread and thread is not None and not thread.is_alive():
                self._thread = None

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._closed = True
            self._generation += 1
