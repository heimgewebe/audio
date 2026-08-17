#!/usr/bin/env python3
"""Deterministic offline song-grammar layer for Buckelwal studies.

This module deliberately does not alter the live keyboard instrument.  It builds
bounded, reproducible Unit -> Phrase -> Theme -> SongCycle -> Session plans and
translates those plans into ordinary ``MidiEvent`` gestures that can be rendered
through the existing Morph voice by a study runner.

The hierarchy and constrained repetition are evidence-backed.  Concrete motif
pitches, repeat counts and variation magnitudes below are engineering choices,
not measurements of a particular whale population or song.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable

from whale_live_engine import MidiEvent

PLAN_SCHEMA_VERSION = 1
MIN_MIDI_NOTE = 21
MAX_MIDI_NOTE = 108
MAX_SESSION_CYCLES = 4
MAX_THEME_COUNT = 6
MAX_PHRASE_REPEATS = 8
MAX_SESSION_UNITS = 512


@dataclass(frozen=True)
class UnitPrototype:
    """Engineering motif used to instantiate one audible song unit."""

    kind: str
    semitone_offset: int
    duration_seconds: float
    gap_seconds: float
    velocity: int
    bend_value: int = 0
    pulse_count: int = 1


@dataclass(frozen=True)
class UnitPlan:
    unit_id: str
    kind: str
    origin_theme_id: str
    start_seconds: float
    duration_seconds: float
    gap_seconds: float
    note: int
    velocity: int
    bend_value: int
    pulse_count: int
    flourish: bool = False

    @property
    def sound_end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds

    @property
    def end_seconds(self) -> float:
        return self.sound_end_seconds + self.gap_seconds


@dataclass(frozen=True)
class PhrasePlan:
    phrase_id: str
    family_id: str
    role: str
    variant_index: int
    start_seconds: float
    body_end_seconds: float
    end_seconds: float
    boundary_pause_seconds: float
    units: tuple[UnitPlan, ...]
    from_theme_id: str | None = None
    to_theme_id: str | None = None


@dataclass(frozen=True)
class ThemePlan:
    theme_id: str
    cycle_index: int
    start_seconds: float
    end_seconds: float
    phrases: tuple[PhrasePlan, ...]


@dataclass(frozen=True)
class TransitionPlan:
    transition_id: str
    cycle_index: int
    from_theme_id: str
    to_theme_id: str
    phrase: PhrasePlan


@dataclass(frozen=True)
class SongCyclePlan:
    cycle_index: int
    start_seconds: float
    end_seconds: float
    themes: tuple[ThemePlan, ...]
    transitions: tuple[TransitionPlan, ...]


@dataclass(frozen=True)
class SongSessionPlan:
    schema_version: int
    seed: int
    base_note: int
    start_seconds: float
    duration_seconds: float
    cycles: tuple[SongCyclePlan, ...]


@dataclass(frozen=True)
class SongGrammarConfig:
    """Bounded engineering configuration for an offline grammar study."""

    seed: int = 0xB0A7
    base_note: int = 45
    cycles: int = 2
    theme_count: int = 4
    phrase_repeats_min: int = 3
    phrase_repeats_max: int = 5
    phrase_pause_seconds: float = 0.82
    transition_pause_seconds: float = 1.35
    cycle_pause_seconds: float = 2.60

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if not 30 <= self.base_note <= 96:
            raise ValueError("base_note must stay between MIDI 30 and 96")
        if not 1 <= self.cycles <= MAX_SESSION_CYCLES:
            raise ValueError(f"cycles must be between 1 and {MAX_SESSION_CYCLES}")
        if not 2 <= self.theme_count <= MAX_THEME_COUNT:
            raise ValueError(f"theme_count must be between 2 and {MAX_THEME_COUNT}")
        if not 2 <= self.phrase_repeats_min <= MAX_PHRASE_REPEATS:
            raise ValueError("phrase_repeats_min is outside the bounded range")
        if not self.phrase_repeats_min <= self.phrase_repeats_max <= MAX_PHRASE_REPEATS:
            raise ValueError("phrase_repeats_max is outside the bounded range")
        for name, value, low, high in (
            ("phrase_pause_seconds", self.phrase_pause_seconds, 0.45, 2.5),
            ("transition_pause_seconds", self.transition_pause_seconds, 0.60, 4.0),
            ("cycle_pause_seconds", self.cycle_pause_seconds, 1.0, 8.0),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if not low <= float(value) <= high:
                raise ValueError(f"{name} is outside the bounded range")
        if not self.transition_pause_seconds > self.phrase_pause_seconds:
            raise ValueError("transition pauses must exceed ordinary phrase pauses")
        if not self.cycle_pause_seconds > self.transition_pause_seconds:
            raise ValueError("cycle pauses must exceed transition pauses")


# These six families are deliberately compact engineering motifs.  Their role is
# to make structural repetition and directed variation audible while the actual
# timbre remains source-derived in WhaleMorphVoice.  They are not transcriptions.
_THEME_MOTIFS: tuple[tuple[UnitPrototype, ...], ...] = (
    (
        UnitPrototype("low", 0, 1.18, 0.16, 47, -450),
        UnitPrototype("rise", 2, 0.92, 0.14, 58, 1900),
        UnitPrototype("pulse", 1, 0.88, 0.16, 72, 500, 3),
        UnitPrototype("fall", -1, 1.08, 0.18, 55, -1650),
    ),
    (
        UnitPrototype("tonal", 3, 1.02, 0.15, 57, 300),
        UnitPrototype("fall", 1, 0.90, 0.14, 64, -1850),
        UnitPrototype("broken", 0, 0.78, 0.17, 77, -350, 2),
        UnitPrototype("rise", 4, 1.16, 0.18, 61, 2100),
    ),
    (
        UnitPrototype("pulse", -2, 0.82, 0.14, 76, 350, 3),
        UnitPrototype("rise", 0, 1.04, 0.15, 62, 1850),
        UnitPrototype("tonal", 3, 1.20, 0.18, 52, 250),
        UnitPrototype("fall", 1, 0.98, 0.16, 60, -1700),
        UnitPrototype("low", -1, 1.10, 0.18, 49, -550),
    ),
    (
        UnitPrototype("tonal", 5, 0.94, 0.14, 55, 500),
        UnitPrototype("pulse", 4, 0.80, 0.14, 74, 450, 3),
        UnitPrototype("fall", 2, 1.08, 0.16, 61, -1900),
        UnitPrototype("low", 0, 1.24, 0.19, 46, -500),
    ),
    (
        UnitPrototype("broken", -3, 0.82, 0.16, 75, -300, 2),
        UnitPrototype("rise", -1, 1.02, 0.15, 59, 1950),
        UnitPrototype("tonal", 2, 1.16, 0.18, 51, 250),
        UnitPrototype("pulse", 1, 0.86, 0.15, 71, 450, 3),
    ),
    (
        UnitPrototype("low", 0, 1.22, 0.17, 45, -500),
        UnitPrototype("tonal", 1, 1.06, 0.15, 54, 300),
        UnitPrototype("rise", 4, 0.94, 0.14, 63, 2000),
        UnitPrototype("fall", 2, 1.12, 0.17, 58, -1800),
        UnitPrototype("broken", 0, 0.80, 0.16, 73, -250, 2),
    ),
)

_THEME_IDS = tuple(chr(ord("A") + index) for index in range(len(_THEME_MOTIFS)))


class _DeterministicRng:
    """Tiny revision-stable PRNG; avoids depending on random module details."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    @staticmethod
    def _mix32(value: int) -> int:
        value &= 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 0x7FEB352D) & 0xFFFFFFFF
        value ^= value >> 15
        value = (value * 0x846CA68B) & 0xFFFFFFFF
        value ^= value >> 16
        return value & 0xFFFFFFFF

    def next_u32(self) -> int:
        self.state = self._mix32(self.state + 0x9E3779B9)
        return self.state

    def randint(self, low: int, high: int) -> int:
        if low > high:
            raise ValueError("invalid randint range")
        return low + self.next_u32() % (high - low + 1)

    def centered(self, magnitude: float) -> float:
        if magnitude < 0:
            raise ValueError("magnitude must not be negative")
        fraction = self.next_u32() / 0xFFFFFFFF
        return (2.0 * fraction - 1.0) * magnitude


class WhaleSongGrammar:
    """Generate bounded hierarchical sessions without touching live voice state."""

    def __init__(self, config: SongGrammarConfig | None = None) -> None:
        self.config = config or SongGrammarConfig()
        self.rng = _DeterministicRng(self.config.seed)

    @staticmethod
    def _bounded_note(note: int) -> int:
        return max(MIN_MIDI_NOTE, min(MAX_MIDI_NOTE, int(note)))

    @staticmethod
    def _bounded_velocity(value: int) -> int:
        return max(1, min(127, int(value)))

    @staticmethod
    def _bounded_bend(value: int) -> int:
        return max(-3600, min(3600, int(value)))

    def _instantiate_phrase(
        self,
        *,
        cycle_index: int,
        theme_index: int,
        repeat_index: int,
        start_seconds: float,
        boundary_pause_seconds: float,
    ) -> PhrasePlan:
        theme_id = _THEME_IDS[theme_index]
        motif = _THEME_MOTIFS[theme_index]
        direction = 1 if (theme_index + cycle_index) % 2 == 0 else -1
        evolution = min(4, cycle_index + repeat_index)
        focus_index = (theme_index + cycle_index + repeat_index) % len(motif)
        cursor = start_seconds
        units: list[UnitPlan] = []
        has_flourish = evolution >= 2 and (cycle_index + repeat_index) % 3 == 2

        for unit_index, prototype in enumerate(motif):
            directed_scale = 1.0 + direction * 0.012 * evolution
            jitter_scale = 1.0 + self.rng.centered(0.018)
            duration = max(0.48, prototype.duration_seconds * directed_scale * jitter_scale)
            gap = (
                0.0
                if unit_index == len(motif) - 1 and not has_flourish
                else max(0.08, prototype.gap_seconds * (1.0 + self.rng.centered(0.08)))
            )
            semitone_delta = 0
            if evolution and unit_index == focus_index and evolution % 2:
                semitone_delta = direction
            note = self._bounded_note(
                self.config.base_note + prototype.semitone_offset + semitone_delta
            )
            velocity = self._bounded_velocity(
                prototype.velocity + direction * min(6, evolution * 2)
            )
            bend = self._bounded_bend(
                prototype.bend_value + direction * min(360, evolution * 90)
            )
            units.append(
                UnitPlan(
                    unit_id=f"c{cycle_index + 1}-{theme_id}-r{repeat_index + 1}-u{unit_index + 1}",
                    kind=prototype.kind,
                    origin_theme_id=theme_id,
                    start_seconds=round(cursor, 6),
                    duration_seconds=round(duration, 6),
                    gap_seconds=round(gap, 6),
                    note=note,
                    velocity=velocity,
                    bend_value=bend,
                    pulse_count=prototype.pulse_count,
                )
            )
            cursor += duration + gap

        # Every third developed repetition receives one related terminal
        # flourish.  It is derived from the motif tail instead of being sampled
        # from an unrelated unit family.
        if has_flourish:
            tail = motif[-1]
            duration = max(0.42, tail.duration_seconds * 0.48)
            flourish = UnitPlan(
                unit_id=f"c{cycle_index + 1}-{theme_id}-r{repeat_index + 1}-flourish",
                kind="flourish",
                origin_theme_id=theme_id,
                start_seconds=round(cursor, 6),
                duration_seconds=round(duration, 6),
                gap_seconds=0.0,
                note=self._bounded_note(
                    self.config.base_note + tail.semitone_offset + direction
                ),
                velocity=self._bounded_velocity(tail.velocity - 4),
                bend_value=self._bounded_bend(tail.bend_value + direction * 300),
                pulse_count=1,
                flourish=True,
            )
            units.append(flourish)
            cursor += flourish.duration_seconds + flourish.gap_seconds

        body_end = units[-1].sound_end_seconds
        end_seconds = body_end + boundary_pause_seconds
        return PhrasePlan(
            phrase_id=f"c{cycle_index + 1}-{theme_id}-r{repeat_index + 1}",
            family_id=theme_id,
            role="theme",
            variant_index=cycle_index * MAX_PHRASE_REPEATS + repeat_index,
            start_seconds=round(start_seconds, 6),
            body_end_seconds=round(body_end, 6),
            end_seconds=round(end_seconds, 6),
            boundary_pause_seconds=round(boundary_pause_seconds, 6),
            units=tuple(units),
        )

    def _transition_phrase(
        self,
        *,
        cycle_index: int,
        from_theme_index: int,
        to_theme_index: int,
        start_seconds: float,
    ) -> PhrasePlan:
        from_id = _THEME_IDS[from_theme_index]
        to_id = _THEME_IDS[to_theme_index]
        left = _THEME_MOTIFS[from_theme_index][-2:]
        right = _THEME_MOTIFS[to_theme_index][:2]
        cursor = start_seconds
        units: list[UnitPlan] = []
        transition_units = (
            tuple((from_id, item) for item in left)
            + tuple((to_id, item) for item in right)
        )
        for unit_index, (origin_id, prototype) in enumerate(transition_units):
            duration = max(0.44, prototype.duration_seconds * (0.72 + self.rng.centered(0.025)))
            gap = (
                0.0
                if unit_index == len(transition_units) - 1
                else max(0.08, prototype.gap_seconds * 0.78)
            )
            units.append(
                UnitPlan(
                    unit_id=f"c{cycle_index + 1}-{from_id}-{to_id}-x-u{unit_index + 1}",
                    kind=prototype.kind,
                    origin_theme_id=origin_id,
                    start_seconds=round(cursor, 6),
                    duration_seconds=round(duration, 6),
                    gap_seconds=round(gap, 6),
                    note=self._bounded_note(self.config.base_note + prototype.semitone_offset),
                    velocity=self._bounded_velocity(prototype.velocity - 3),
                    bend_value=self._bounded_bend(prototype.bend_value),
                    pulse_count=prototype.pulse_count,
                )
            )
            cursor += duration + gap
        body_end = units[-1].sound_end_seconds
        boundary = self.config.transition_pause_seconds + self.rng.centered(0.08)
        return PhrasePlan(
            phrase_id=f"c{cycle_index + 1}-{from_id}-{to_id}-transition",
            family_id=f"{from_id}>{to_id}",
            role="transition",
            variant_index=cycle_index,
            start_seconds=round(start_seconds, 6),
            body_end_seconds=round(body_end, 6),
            end_seconds=round(body_end + boundary, 6),
            boundary_pause_seconds=round(boundary, 6),
            units=tuple(units),
            from_theme_id=from_id,
            to_theme_id=to_id,
        )

    def generate(self) -> SongSessionPlan:
        cursor = 0.0
        cycles: list[SongCyclePlan] = []
        unit_count = 0

        for cycle_index in range(self.config.cycles):
            cycle_start = cursor
            themes: list[ThemePlan] = []
            transitions: list[TransitionPlan] = []
            for theme_index in range(self.config.theme_count):
                theme_id = _THEME_IDS[theme_index]
                theme_start = cursor
                repeat_count = self.rng.randint(
                    self.config.phrase_repeats_min,
                    self.config.phrase_repeats_max,
                )
                phrases: list[PhrasePlan] = []
                for repeat_index in range(repeat_count):
                    is_last = repeat_index == repeat_count - 1
                    if is_last and theme_index == self.config.theme_count - 1:
                        boundary = self.config.cycle_pause_seconds
                    elif is_last:
                        # The actual theme handoff is articulated by an explicit
                        # transition phrase, so the theme's own last pause stays
                        # phrase-sized rather than duplicating the transition gap.
                        boundary = self.config.phrase_pause_seconds
                    else:
                        boundary = self.config.phrase_pause_seconds + self.rng.centered(0.07)
                    phrase = self._instantiate_phrase(
                        cycle_index=cycle_index,
                        theme_index=theme_index,
                        repeat_index=repeat_index,
                        start_seconds=cursor,
                        boundary_pause_seconds=boundary,
                    )
                    phrases.append(phrase)
                    unit_count += len(phrase.units)
                    cursor = phrase.end_seconds
                theme = ThemePlan(
                    theme_id=theme_id,
                    cycle_index=cycle_index,
                    start_seconds=round(theme_start, 6),
                    end_seconds=round(cursor, 6),
                    phrases=tuple(phrases),
                )
                themes.append(theme)

                if theme_index < self.config.theme_count - 1:
                    transition_phrase = self._transition_phrase(
                        cycle_index=cycle_index,
                        from_theme_index=theme_index,
                        to_theme_index=theme_index + 1,
                        start_seconds=cursor,
                    )
                    transition = TransitionPlan(
                        transition_id=transition_phrase.phrase_id,
                        cycle_index=cycle_index,
                        from_theme_id=theme_id,
                        to_theme_id=_THEME_IDS[theme_index + 1],
                        phrase=transition_phrase,
                    )
                    transitions.append(transition)
                    unit_count += len(transition_phrase.units)
                    cursor = transition_phrase.end_seconds

                if unit_count > MAX_SESSION_UNITS:
                    raise RuntimeError("generated session exceeds the bounded unit budget")

            cycles.append(
                SongCyclePlan(
                    cycle_index=cycle_index,
                    start_seconds=round(cycle_start, 6),
                    end_seconds=round(cursor, 6),
                    themes=tuple(themes),
                    transitions=tuple(transitions),
                )
            )

        return SongSessionPlan(
            schema_version=PLAN_SCHEMA_VERSION,
            seed=self.config.seed,
            base_note=self.config.base_note,
            start_seconds=0.0,
            duration_seconds=round(cursor, 6),
            cycles=tuple(cycles),
        )


def iter_phrases(session: SongSessionPlan) -> Iterable[PhrasePlan]:
    """Yield theme and transition phrases in chronological order."""

    ordered: list[PhrasePlan] = []
    for cycle in session.cycles:
        ordered.extend(phrase for theme in cycle.themes for phrase in theme.phrases)
        ordered.extend(transition.phrase for transition in cycle.transitions)
    yield from sorted(ordered, key=lambda phrase: phrase.start_seconds)


def iter_units(session: SongSessionPlan) -> Iterable[UnitPlan]:
    for phrase in iter_phrases(session):
        yield from phrase.units


def structural_metrics(session: SongSessionPlan) -> dict[str, object]:
    phrases = list(iter_phrases(session))
    units = list(iter_units(session))
    theme_phrases = [phrase for phrase in phrases if phrase.role == "theme"]
    transitions = [phrase for phrase in phrases if phrase.role == "transition"]
    unit_kinds = Counter(unit.kind for unit in units)
    unit_gaps = [unit.gap_seconds for unit in units]
    boundary_pauses = [phrase.boundary_pause_seconds for phrase in phrases]
    return {
        "cycle_count": len(session.cycles),
        "theme_count_total": sum(len(cycle.themes) for cycle in session.cycles),
        "theme_count_per_cycle": [len(cycle.themes) for cycle in session.cycles],
        "theme_order_per_cycle": [
            [theme.theme_id for theme in cycle.themes] for cycle in session.cycles
        ],
        "theme_phrase_count": len(theme_phrases),
        "transition_phrase_count": len(transitions),
        "phrase_count_total": len(phrases),
        "unit_count": len(units),
        "flourish_unit_count": sum(1 for unit in units if unit.flourish),
        "duration_seconds": session.duration_seconds,
        "unit_kind_counts": dict(sorted(unit_kinds.items())),
        "phrase_repeats": [
            {
                "cycle_index": cycle.cycle_index,
                "theme_id": theme.theme_id,
                "count": len(theme.phrases),
            }
            for cycle in session.cycles
            for theme in cycle.themes
        ],
        "transition_pairs": [
            [transition.from_theme_id, transition.to_theme_id]
            for cycle in session.cycles
            for transition in cycle.transitions
        ],
        "maximum_unit_gap_seconds": round(max(unit_gaps, default=0.0), 6),
        "minimum_phrase_boundary_pause_seconds": round(
            min(boundary_pauses, default=0.0), 6
        ),
    }


def plan_dict(session: SongSessionPlan) -> dict[str, object]:
    return asdict(session)


def canonical_plan_json(session: SongSessionPlan) -> str:
    return json.dumps(
        plan_dict(session),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_sha256(session: SongSessionPlan) -> str:
    return hashlib.sha256(canonical_plan_json(session).encode("utf-8")).hexdigest()


def events_for_session(
    session: SongSessionPlan,
    *,
    until_seconds: float | None = None,
) -> list[tuple[float, MidiEvent]]:
    """Translate a structural plan into bounded ordinary Morph MIDI gestures."""

    limit = session.duration_seconds if until_seconds is None else float(until_seconds)
    if not math.isfinite(limit) or not 0 < limit <= session.duration_seconds:
        raise ValueError("until_seconds must be positive and within the session")

    queued: list[tuple[float, int, MidiEvent]] = []
    for unit in iter_units(session):
        if unit.start_seconds >= limit:
            break
        start = unit.start_seconds
        sound_end = min(unit.sound_end_seconds, limit)
        if sound_end <= start:
            continue
        queued.append((start, 20, MidiEvent("note_on", note=unit.note, velocity=unit.velocity)))
        if unit.pulse_count > 1:
            for pulse_index in range(1, unit.pulse_count):
                pulse_time = start + unit.duration_seconds * pulse_index / unit.pulse_count
                if pulse_time < sound_end:
                    queued.append(
                        (
                            pulse_time,
                            20,
                            MidiEvent(
                                "note_on",
                                note=unit.note,
                                velocity=min(127, unit.velocity + 5),
                            ),
                        )
                    )
        if unit.bend_value:
            bend_time = start + min(unit.duration_seconds * 0.34, 0.42)
            reset_time = start + unit.duration_seconds * 0.78
            if bend_time < sound_end:
                queued.append((bend_time, 10, MidiEvent("pitch_bend", value=unit.bend_value)))
            if reset_time < sound_end:
                queued.append((reset_time, 5, MidiEvent("pitch_bend", value=0)))
        queued.append((sound_end, 5, MidiEvent("pitch_bend", value=0)))
        queued.append((sound_end, 10, MidiEvent("note_off", note=unit.note)))

    # Ensure a truncated study excerpt cannot leave the monophonic voice held.
    queued.append((limit, 0, MidiEvent("pitch_bend", value=0)))
    queued.append((limit, 30, MidiEvent("control_change", controller=123, value=0)))
    queued.sort(key=lambda item: (item[0], item[1]))
    return [(round(time_value, 6), event) for time_value, _priority, event in queued]
