#!/usr/bin/env python3
"""Temporal source-filter foundation for the playable humpback-whale voice.

The main fundamental remains bound to the played MIDI note. Source-derived
control trajectories move spectral tilt, formant emphasis, periodicity,
pulsation, subharmonics, and a bounded secondary frequency component. No
recorded phrase is played and no independent noise generator exists.
"""

from __future__ import annotations

import bisect
import json
import math
import pathlib
from collections import OrderedDict
from dataclasses import dataclass

from build_whale_morph_bank import (
    read_bound_regular_bytes,
    regular_file_path,
    sha256_bytes,
)
from whale_live_engine import WhaleVoiceConfig, clamp
from whale_morph_engine import (
    MAX_MASTER_GAIN,
    WhaleMorphBank,
    WhaleMorphVoice,
    frequency_to_midi_note,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    ROOT / "assets" / "whale-sources" / "voice-model" / "manifest.json"
)
EXPECTED_MANIFEST_SHA256 = "1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a"
EXPECTED_SOURCE_IDS = (
    "humpback-moo-nps",
    "humpback-song-cc0",
    "humpback-wheezeblow-nps",
    "song-antarctic-area-v-2010",
    "song-eastern-australia-2010",
    "song-foraging-mn132a",
    "song-foraging-mn133a",
    "song-new-caledonia-2010",
)
TAIL_THRESHOLD = 1.0e-8


@dataclass(frozen=True)
class SourceFilterPoint:
    phase: float
    envelope: float
    periodicity: float
    roughness: float
    high_band_ratio: float
    spectral_tilt: float
    resonance_ratio_1: float
    resonance_ratio_2: float
    harmonic_profile: tuple[float, ...]
    pulse_rate_hz: float
    pulse_strength: float
    subharmonic_strength: float
    secondary_ratio: float
    secondary_strength: float


@dataclass(frozen=True)
class SourceFilterTrajectory:
    trajectory_id: str
    clip_id: str
    source_id: str
    category: str
    duration_seconds: float
    median_f0_hz: float
    points: tuple[SourceFilterPoint, ...]


@dataclass(frozen=True)
class SourceFilterControl:
    envelope: float
    periodicity: float
    roughness: float
    high_band_ratio: float
    spectral_tilt: float
    resonance_ratio_1: float
    resonance_ratio_2: float
    harmonic_profile: tuple[float, ...]
    pulse_rate_hz: float
    pulse_strength: float
    subharmonic_strength: float
    secondary_ratio: float
    secondary_strength: float


SOURCE_FILTER_COMPONENT_NAMES = (
    "source_envelope",
    "periodicity_roughness",
    "pulse",
    "subharmonic",
    "secondary_frequency",
    "resonance_focus",
    "harmonic_profile",
)


@dataclass(frozen=True)
class SourceFilterComponentConfig:
    """Immutable switches for source-derived Organic components.

    Every disabled component is returned to a Morph-neutral contribution.
    The all-enabled configuration preserves the pre-study Organic signal path.
    """

    source_envelope: bool = True
    periodicity_roughness: bool = True
    pulse: bool = True
    subharmonic: bool = True
    secondary_frequency: bool = True
    resonance_focus: bool = True
    harmonic_profile: bool = True

    @classmethod
    def morph_neutral(cls) -> "SourceFilterComponentConfig":
        return cls(**{name: False for name in SOURCE_FILTER_COMPONENT_NAMES})

    @classmethod
    def from_enabled(cls, enabled: frozenset[str]) -> "SourceFilterComponentConfig":
        unknown = enabled - set(SOURCE_FILTER_COMPONENT_NAMES)
        if unknown:
            raise ValueError(f"unknown source-filter components: {sorted(unknown)}")
        return cls(**{name: name in enabled for name in SOURCE_FILTER_COMPONENT_NAMES})

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(name for name in SOURCE_FILTER_COMPONENT_NAMES if getattr(self, name))

    def any_enabled(self) -> bool:
        return any(getattr(self, name) for name in SOURCE_FILTER_COMPONENT_NAMES)


class WhaleSourceFilterBank:
    """Validated temporal feature bank with optional family exclusions."""

    def __init__(
        self,
        manifest_path: pathlib.Path = DEFAULT_MANIFEST,
        *,
        expected_manifest_sha256: str | None = EXPECTED_MANIFEST_SHA256,
        excluded_source_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.manifest_path = regular_file_path(
            manifest_path, "whale source-filter manifest"
        )
        manifest_payload = read_bound_regular_bytes(
            self.manifest_path, "whale source-filter manifest"
        )
        self.manifest_sha256 = sha256_bytes(manifest_payload)
        self.expected_manifest_sha256 = expected_manifest_sha256
        if (
            expected_manifest_sha256 is not None
            and self.manifest_sha256 != expected_manifest_sha256
        ):
            raise RuntimeError("whale source-filter manifest hash mismatch")
        value = json.loads(manifest_payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("whale source-filter manifest root must be an object")
        if (
            value.get("schema_version") != 2
            or value.get("kind") != "humpback_whale_temporal_source_filter_bank"
            or value.get("sample_rate_hz") != 48_000
            or value.get("analysis_rate_hz") != 4_000
            or value.get("note_range") != [21, 108]
            or value.get("tuning") != "twelve-tone-equal-temperament-a4-440"
            or value.get("voice_count") != 1
        ):
            raise RuntimeError("whale source-filter manifest has the wrong schema")
        analysis_filter = value.get("analysis_filter")
        if (
            not isinstance(analysis_filter, dict)
            or analysis_filter.get("kind")
            != "butterworth-lowpass-before-decimation"
            or analysis_filter.get("order") != 8
            or analysis_filter.get("decimation_factor") != 12
            or not isinstance(analysis_filter.get("cutoff_hz"), (int, float))
            or not 1_400.0 <= float(analysis_filter["cutoff_hz"]) <= 1_800.0
        ):
            raise RuntimeError("whale source-filter analysis filter is invalid")
        control_points = value.get("control_points")
        harmonic_count = value.get("harmonic_count")
        if (
            not isinstance(control_points, int)
            or not 16 <= control_points <= 256
            or not isinstance(harmonic_count, int)
            or not 4 <= harmonic_count <= 32
        ):
            raise RuntimeError("whale source-filter dimensions are invalid")
        self.control_points = control_points
        self.harmonic_count = harmonic_count

        source_manifest = value.get("source_sample_manifest")
        source_sha = value.get("source_sample_manifest_sha256")
        if not isinstance(source_manifest, str) or not isinstance(source_sha, str):
            raise RuntimeError("whale source-filter provenance is incomplete")
        relative = pathlib.PurePosixPath(source_manifest)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RuntimeError("whale source-filter source path is invalid")
        source_path = regular_file_path(
            ROOT.joinpath(*relative.parts), "whale source-filter source manifest"
        )
        try:
            source_path.relative_to(ROOT)
        except ValueError as error:
            raise RuntimeError(
                "whale source-filter source manifest escapes repository root"
            ) from error
        source_payload = read_bound_regular_bytes(
            source_path, "whale source-filter source manifest"
        )
        if sha256_bytes(source_payload) != source_sha:
            raise RuntimeError("whale source-filter source manifest hash mismatch")
        source_value = json.loads(source_payload.decode("utf-8"))
        if (
            not isinstance(source_value, dict)
            or source_value.get("schema_version") != 2
            or source_value.get("kind") != "humpback_whale_sample_bank"
            or not isinstance(source_value.get("clips"), list)
        ):
            raise RuntimeError("whale source-filter source manifest has wrong schema")
        source_clips: dict[str, dict[str, object]] = {}
        for record in source_value["clips"]:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise RuntimeError("whale source-filter source clip is invalid")
            source_clips[record["id"]] = record

        source_ids = value.get("source_ids")
        if (
            not isinstance(source_ids, list)
            or source_ids != list(EXPECTED_SOURCE_IDS)
            or not all(isinstance(item, str) and item for item in source_ids)
        ):
            raise RuntimeError("whale source-filter source family catalog changed")
        excluded = frozenset(excluded_source_ids)
        if not excluded <= set(source_ids):
            raise RuntimeError("whale source-filter exclusions contain unknown families")
        self.source_ids = tuple(source_ids)
        self.excluded_source_ids = tuple(sorted(excluded))

        raw_trajectories = value.get("trajectories")
        if not isinstance(raw_trajectories, list) or len(raw_trajectories) < 6:
            raise RuntimeError("whale source-filter bank lacks trajectories")
        trajectories: list[SourceFilterTrajectory] = []
        seen_ids: set[str] = set()
        for raw in raw_trajectories:
            if not isinstance(raw, dict):
                raise RuntimeError("whale source-filter trajectory must be an object")
            trajectory_id = raw.get("id")
            clip_id = raw.get("clip_id")
            source_id = raw.get("source_id")
            source_file = raw.get("source_file")
            source_clip_sha = raw.get("source_sha256")
            category = raw.get("category")
            summary = raw.get("summary")
            raw_points = raw.get("points")
            if (
                not all(
                    isinstance(item, str) and item
                    for item in (
                        trajectory_id,
                        clip_id,
                        source_id,
                        source_file,
                        source_clip_sha,
                        category,
                    )
                )
                or trajectory_id in seen_ids
                or source_id not in self.source_ids
                or not isinstance(summary, dict)
                or not isinstance(raw_points, list)
                or len(raw_points) != control_points
            ):
                raise RuntimeError("whale source-filter trajectory metadata is invalid")
            source_record = source_clips.get(clip_id)
            if (
                source_record is None
                or source_record.get("source_id") != source_id
                or source_record.get("file") != source_file
                or source_record.get("sha256") != source_clip_sha
            ):
                raise RuntimeError(
                    "whale source-filter trajectory provenance does not match source"
                )
            duration = summary.get("duration_seconds")
            median_f0 = summary.get("median_f0_hz")
            if (
                not isinstance(duration, (int, float))
                or not 1.0 <= float(duration) <= 30.0
                or not isinstance(median_f0, (int, float))
                or not 0.0 <= float(median_f0) <= 2_000.0
            ):
                raise RuntimeError("whale source-filter summary is invalid")
            points = tuple(
                self._parse_point(point, index, control_points, harmonic_count)
                for index, point in enumerate(raw_points)
            )
            trajectories.append(
                SourceFilterTrajectory(
                    trajectory_id=trajectory_id,
                    clip_id=clip_id,
                    source_id=source_id,
                    category=category,
                    duration_seconds=float(duration),
                    median_f0_hz=float(median_f0),
                    points=points,
                )
            )
            seen_ids.add(trajectory_id)
        self.trajectories = tuple(trajectories)
        self.live = tuple(
            trajectory
            for trajectory in trajectories
            if trajectory.source_id not in excluded
        )
        groups: dict[str, list[SourceFilterTrajectory]] = {}
        for trajectory in self.live:
            groups.setdefault(trajectory.source_id, []).append(trajectory)
        self.live_by_source = {
            source_id: tuple(sorted(items, key=lambda item: item.trajectory_id))
            for source_id, items in sorted(groups.items())
        }
        if len(self.live_by_source) < 2 or len(self.live) < 4:
            raise RuntimeError("whale source-filter exclusions leave too little diversity")
        self._timeline_cache: OrderedDict[tuple[int, int, int], list[int]] = OrderedDict()
        self._timeline_cache_limit = 32

    @staticmethod
    def _bounded_number(
        raw: dict[str, object], key: str, low: float, high: float
    ) -> float:
        value = raw.get(key)
        if not isinstance(value, (int, float)) or not low <= float(value) <= high:
            raise RuntimeError(f"whale source-filter point {key} is invalid")
        return float(value)

    @classmethod
    def _parse_point(
        cls,
        raw: object,
        index: int,
        count: int,
        harmonic_count: int,
    ) -> SourceFilterPoint:
        if not isinstance(raw, dict):
            raise RuntimeError("whale source-filter point must be an object")
        expected_phase = index / (count - 1)
        phase = cls._bounded_number(raw, "phase", 0.0, 1.0)
        if abs(phase - expected_phase) > 1.0e-6:
            raise RuntimeError("whale source-filter point phase grid is invalid")
        profile = raw.get("harmonic_profile")
        if (
            not isinstance(profile, list)
            or len(profile) != harmonic_count
            or not all(
                isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0
                for value in profile
            )
            or sum(float(value) for value in profile) > 1.000001
        ):
            raise RuntimeError("whale source-filter harmonic profile is invalid")
        periodicity = cls._bounded_number(raw, "periodicity", 0.0, 1.0)
        roughness = cls._bounded_number(raw, "roughness", 0.0, 1.0)
        if abs(periodicity + roughness - 1.0) > 2.0e-6:
            raise RuntimeError("whale source-filter periodicity complement is invalid")
        return SourceFilterPoint(
            phase=phase,
            envelope=cls._bounded_number(raw, "envelope", 0.0, 1.0),
            periodicity=periodicity,
            roughness=roughness,
            high_band_ratio=cls._bounded_number(raw, "high_band_ratio", 0.0, 1.0),
            spectral_tilt=cls._bounded_number(raw, "spectral_tilt", 0.0, 1.0),
            resonance_ratio_1=cls._bounded_number(
                raw, "resonance_ratio_1", 1.2, 8.0
            ),
            resonance_ratio_2=cls._bounded_number(
                raw, "resonance_ratio_2", 1.8, 12.0
            ),
            harmonic_profile=tuple(float(value) for value in profile),
            pulse_rate_hz=cls._bounded_number(raw, "pulse_rate_hz", 1.2, 8.0),
            pulse_strength=cls._bounded_number(raw, "pulse_strength", 0.0, 1.0),
            subharmonic_strength=cls._bounded_number(
                raw, "subharmonic_strength", 0.0, 1.0
            ),
            secondary_ratio=cls._bounded_number(raw, "secondary_ratio", 0.55, 2.40),
            secondary_strength=cls._bounded_number(
                raw, "secondary_strength", 0.0, 1.0
            ),
        )

    def status(self) -> dict[str, object]:
        return {
            "ready": True,
            "manifest": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "expected_manifest_sha256": self.expected_manifest_sha256,
            "trajectory_count": len(self.trajectories),
            "live_trajectory_count": len(self.live),
            "source_ids": list(self.source_ids),
            "excluded_source_ids": list(self.excluded_source_ids),
            "permanent_noise_layer": False,
            "recorded_phrase_playback": False,
        }

    @staticmethod
    def _mix32(value: int) -> int:
        value &= 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 0x7FEB352D) & 0xFFFFFFFF
        value ^= value >> 15
        value = (value * 0x846CA68B) & 0xFFFFFFFF
        value ^= value >> 16
        return value & 0xFFFFFFFF

    def _candidate_families(
        self, note: int
    ) -> tuple[tuple[str, tuple[SourceFilterTrajectory, ...]], ...]:
        if note <= 42:
            preferred = {"low", "song"}
        elif note >= 84:
            preferred = {"high", "song"}
        else:
            preferred = {"song"}
        groups: list[tuple[str, tuple[SourceFilterTrajectory, ...]]] = []
        for source_id, trajectories in self.live_by_source.items():
            matches = tuple(
                trajectory
                for trajectory in trajectories
                if trajectory.category in preferred
            )
            if matches:
                groups.append((source_id, matches))
        return tuple(groups) or tuple(self.live_by_source.items())

    def _trajectory(
        self, note: int, seed: int, unit_index: int
    ) -> SourceFilterTrajectory:
        families = self._candidate_families(note)
        mixed = self._mix32(
            seed + unit_index * 0x9E3779B9 + note * 0x85EBCA6B
        )
        _source_id, candidates = families[mixed % len(families)]
        clip_mixed = self._mix32(mixed ^ 0xA511E9B3)
        return candidates[clip_mixed % len(candidates)]

    @staticmethod
    def _interpolate_point(
        trajectory: SourceFilterTrajectory, phase: float
    ) -> SourceFilterControl:
        bounded = clamp(phase, 0.0, 1.0)
        position = bounded * (len(trajectory.points) - 1)
        left_index = int(position)
        right_index = min(left_index + 1, len(trajectory.points) - 1)
        amount = position - left_index
        left = trajectory.points[left_index]
        right = trajectory.points[right_index]

        def mix(name: str) -> float:
            return float(getattr(left, name)) + (
                float(getattr(right, name)) - float(getattr(left, name))
            ) * amount

        profile = tuple(
            left_value + (right_value - left_value) * amount
            for left_value, right_value in zip(
                left.harmonic_profile, right.harmonic_profile
            )
        )
        return SourceFilterControl(
            envelope=mix("envelope"),
            periodicity=mix("periodicity"),
            roughness=mix("roughness"),
            high_band_ratio=mix("high_band_ratio"),
            spectral_tilt=mix("spectral_tilt"),
            resonance_ratio_1=mix("resonance_ratio_1"),
            resonance_ratio_2=mix("resonance_ratio_2"),
            harmonic_profile=profile,
            pulse_rate_hz=mix("pulse_rate_hz"),
            pulse_strength=mix("pulse_strength"),
            subharmonic_strength=mix("subharmonic_strength"),
            secondary_ratio=mix("secondary_ratio"),
            secondary_strength=mix("secondary_strength"),
        )

    @staticmethod
    def _mix_control(
        left: SourceFilterControl, right: SourceFilterControl, amount: float
    ) -> SourceFilterControl:
        shaped = clamp(amount, 0.0, 1.0)
        shaped = shaped * shaped * (3.0 - 2.0 * shaped)

        def mix(name: str) -> float:
            return float(getattr(left, name)) + (
                float(getattr(right, name)) - float(getattr(left, name))
            ) * shaped

        return SourceFilterControl(
            envelope=mix("envelope"),
            periodicity=mix("periodicity"),
            roughness=mix("roughness"),
            high_band_ratio=mix("high_band_ratio"),
            spectral_tilt=mix("spectral_tilt"),
            resonance_ratio_1=mix("resonance_ratio_1"),
            resonance_ratio_2=mix("resonance_ratio_2"),
            harmonic_profile=tuple(
                left_value + (right_value - left_value) * shaped
                for left_value, right_value in zip(
                    left.harmonic_profile, right.harmonic_profile
                )
            ),
            pulse_rate_hz=mix("pulse_rate_hz"),
            pulse_strength=mix("pulse_strength"),
            subharmonic_strength=mix("subharmonic_strength"),
            secondary_ratio=mix("secondary_ratio"),
            secondary_strength=mix("secondary_strength"),
        )

    @staticmethod
    def _unit_frames(
        trajectory: SourceFilterTrajectory, sample_rate: int
    ) -> int:
        seconds = clamp(trajectory.duration_seconds * 0.55, 1.45, 4.80)
        return max(1, round(seconds * sample_rate))

    def _timeline_ends(
        self,
        *,
        note: int,
        seed: int,
        age_frames: int,
        sample_rate: int,
    ) -> list[int]:
        key = (note, seed & 0xFFFFFFFF, sample_rate)
        ends = self._timeline_cache.pop(key, None)
        if ends is None:
            ends = []
        self._timeline_cache[key] = ends
        while len(self._timeline_cache) > self._timeline_cache_limit:
            self._timeline_cache.popitem(last=False)
        target = max(0, age_frames)
        while not ends or ends[-1] <= target:
            unit_index = len(ends)
            trajectory = self._trajectory(note, seed, unit_index)
            frames = self._unit_frames(trajectory, sample_rate)
            ends.append((ends[-1] if ends else 0) + frames)
        return ends

    def _unit_position(
        self,
        *,
        note: int,
        seed: int,
        age_frames: int,
        sample_rate: int,
    ) -> tuple[int, SourceFilterTrajectory, int, int]:
        target = max(0, age_frames)
        ends = self._timeline_ends(
            note=note,
            seed=seed,
            age_frames=target,
            sample_rate=sample_rate,
        )
        unit_index = bisect.bisect_right(ends, target)
        start = ends[unit_index - 1] if unit_index else 0
        trajectory = self._trajectory(note, seed, unit_index)
        frames = ends[unit_index] - start
        return unit_index, trajectory, frames, target - start

    def control(
        self,
        *,
        note: int,
        seed: int,
        age_frames: int,
        sample_rate: int,
    ) -> SourceFilterControl:
        unit_index, current, unit_frames, local_frames = self._unit_position(
            note=note,
            seed=seed,
            age_frames=age_frames,
            sample_rate=sample_rate,
        )
        phase = local_frames / unit_frames
        current_control = self._interpolate_point(current, phase)
        if unit_index == 0 or phase >= 0.14:
            return current_control
        previous = self._trajectory(note, seed, unit_index - 1)
        previous_control = self._interpolate_point(previous, 1.0)
        return self._mix_control(previous_control, current_control, phase / 0.14)

def source_filter_bank_status(
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    *,
    expected_manifest_sha256: str | None = EXPECTED_MANIFEST_SHA256,
) -> dict[str, object]:
    try:
        return WhaleSourceFilterBank(
            manifest_path,
            expected_manifest_sha256=expected_manifest_sha256,
        ).status()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return {
            "ready": False,
            "manifest": str(manifest_path),
            "blocking_reason": str(error),
        }


class WhaleSourceFilterVoice(WhaleMorphVoice):
    """Morph excitation shaped by source-derived temporal vocal trajectories."""

    def __init__(
        self,
        config: WhaleVoiceConfig | None = None,
        *,
        source_filter_bank: WhaleSourceFilterBank | None = None,
        morph_bank: WhaleMorphBank | None = None,
        component_config: SourceFilterComponentConfig | None = None,
    ) -> None:
        super().__init__(config, bank=morph_bank)
        self.source_filter_bank = source_filter_bank or WhaleSourceFilterBank()
        self.source_filter_components = component_config or SourceFilterComponentConfig()
        self.source_filter_phrase_serial = 0
        self.source_filter_seed = 0
        self.source_filter_lowpass = 0.0
        self.source_filter_activity = 0.0
        self.source_filter_mode_a_1 = 0.0
        self.source_filter_mode_a_2 = 0.0
        self.source_filter_mode_b_1 = 0.0
        self.source_filter_mode_b_2 = 0.0
        self.source_filter_secondary_phase = 0.17
        self.source_filter_sub_phase = 0.61
        self.source_filter_control_remaining = 0
        self.source_filter_control_position = 0
        self.source_filter_control_age_start = 0
        self.source_filter_control_frequency = self.current_frequency
        self.source_filter_control_velocity = self.velocity
        self.source_filter_control = self.source_filter_bank.control(
            note=60,
            seed=0,
            age_frames=0,
            sample_rate=self.config.sample_rate,
        )

    def note_on(self, note: int, velocity: int) -> None:
        detached = not self.gate and not self.held_notes
        super().note_on(note, velocity)
        if not 21 <= note <= 108:
            return
        self.source_filter_control_remaining = 0
        if detached:
            self.source_filter_phrase_serial += 1
            self.source_filter_seed = self.source_filter_bank._mix32(
                note * 0x45D9F3B
                + int(clamp(velocity, 1, 127)) * 0x119DE1F3
                + self.source_filter_phrase_serial * 0x27D4EB2D
            )
            self.source_filter_secondary_phase = (
                (self.source_filter_seed >> 7) & 0xFFFF
            ) / 65536.0
            self.source_filter_sub_phase = (
                (self.source_filter_seed >> 18) & 0xFFFF
            ) / 65536.0

    def note_off(self, note: int) -> None:
        super().note_off(note)
        self.source_filter_control_remaining = 0

    def control_change(self, controller: int, value: int) -> None:
        super().control_change(controller, value)
        if controller in {1, 11, 64, 67, 120, 123}:
            self.source_filter_control_remaining = 0

    def pitch_bend(self, value: int) -> None:
        super().pitch_bend(value)
        self.source_filter_control_remaining = 0

    def _silence_immediately(self) -> None:
        super()._silence_immediately()
        self.source_filter_lowpass = 0.0
        self.source_filter_activity = 0.0
        self.source_filter_mode_a_1 = 0.0
        self.source_filter_mode_a_2 = 0.0
        self.source_filter_mode_b_1 = 0.0
        self.source_filter_mode_b_2 = 0.0
        self.source_filter_control_remaining = 0
        self.source_filter_control_position = 0

    def _start_source_filter_segment(self) -> None:
        note = self.active_note if self.active_note is not None else 60
        self.source_filter_control = self.source_filter_bank.control(
            note=note,
            seed=self.source_filter_seed,
            age_frames=self.note_age_frames,
            sample_rate=self.config.sample_rate,
        )
        self.source_filter_control_remaining = self.config.block_frames
        self.source_filter_control_position = 0
        self.source_filter_control_age_start = self.note_age_frames
        self.source_filter_control_frequency = max(self.current_frequency, 1.0)
        self.source_filter_control_velocity = clamp(self.velocity, 0.0, 1.0)

    @staticmethod
    def _resonator(
        excitation: float,
        first: float,
        second: float,
        coefficient: float,
        radius_squared: float,
        excitation_gain: float,
    ) -> tuple[float, float, float]:
        value = (
            excitation * excitation_gain
            + coefficient * first
            - radius_squared * second
        )
        value = clamp(value, -0.7, 0.7)
        return value, value, first

    def _process_source_filter_block(self, base: list[float]) -> list[float]:
        components = self.source_filter_components
        if not components.any_enabled():
            self.source_filter_control_position += len(base)
            return list(base)
        sample_rate = self.config.sample_rate
        control = self.source_filter_control
        frequency = max(self.source_filter_control_frequency, 1.0)
        velocity = self.source_filter_control_velocity
        register_note = frequency_to_midi_note(frequency)
        register_bass_weight = clamp((55.0 - register_note) / 24.0, 0.0, 1.0)
        register_bass_weight = (
            register_bass_weight
            * register_bass_weight
            * (3.0 - 2.0 * register_bass_weight)
        )
        register_gain = (
            1.0 - 0.74 * register_bass_weight
            if components.resonance_focus
            else 1.0
        )
        first_resonance = clamp(
            frequency * control.resonance_ratio_1, 92.0, 1_650.0
        )
        second_resonance = clamp(
            frequency * control.resonance_ratio_2, 180.0, 3_300.0
        )
        low_cutoff = clamp(
            first_resonance * 0.72
            if components.resonance_focus
            else frequency * 3.0,
            110.0,
            3_400.0,
        )
        low_alpha = 1.0 - math.exp(-2.0 * math.pi * low_cutoff / sample_rate)
        radius_a = math.exp(-1.0 / (sample_rate * (0.018 + 0.020 * self.distance)))
        radius_b = math.exp(-1.0 / (sample_rate * (0.012 + 0.014 * self.distance)))
        coefficient_a = 2.0 * radius_a * math.cos(
            2.0 * math.pi * first_resonance / sample_rate
        )
        coefficient_b = 2.0 * radius_b * math.cos(
            2.0 * math.pi * second_resonance / sample_rate
        )
        roughness = (
            clamp(control.roughness, 0.0, 1.0)
            if components.periodicity_roughness
            else 0.0
        )
        periodicity = (
            clamp(control.periodicity, 0.0, 1.0)
            if components.periodicity_roughness
            else 0.0
        )
        spectral_tilt = clamp(control.spectral_tilt, 0.0, 1.0)
        high_band = clamp(control.high_band_ratio, 0.0, 1.0)
        profile = control.harmonic_profile
        harmonic_second = profile[1] if len(profile) > 1 else 0.0
        harmonic_third = profile[2] if len(profile) > 2 else 0.0
        low_harmonics = sum(profile[:2])
        middle_harmonics = sum(profile[2:5])
        upper_harmonics = sum(profile[5:])
        even_harmonics = sum(
            value for index, value in enumerate(profile, start=1) if index % 2 == 0
        )
        harmonic_centroid = sum(
            index * value for index, value in enumerate(profile, start=1)
        )
        low_gain = 1.0
        high_gain = 1.0
        if components.periodicity_roughness:
            low_gain += -0.14 + 0.26 * (1.0 - spectral_tilt)
            high_gain += 0.30 + 0.34 * spectral_tilt + 0.20 * high_band
        if components.harmonic_profile:
            low_gain += 0.16 * low_harmonics
            high_gain += 0.22 * upper_harmonics
        envelope_gain = (
            0.78 + 0.34 * math.sqrt(clamp(control.envelope, 0.0, 1.0))
            if components.source_envelope
            else 1.0
        )
        resonance_mix = (
            0.014
            + (0.046 * periodicity if components.periodicity_roughness else 0.0)
            + (0.018 * middle_harmonics if components.harmonic_profile else 0.0)
            if components.resonance_focus
            else 0.0
        )
        pulse_depth = (
            0.03 + 0.10 * control.pulse_strength if components.pulse else 0.0
        )
        activity_attack = 1.0 - math.exp(-1.0 / (sample_rate * 0.006))
        activity_release = 1.0 - math.exp(-1.0 / (sample_rate * 0.055))
        output: list[float] = []
        for index, source_sample in enumerate(base):
            self.source_filter_lowpass += (
                source_sample - self.source_filter_lowpass
            ) * low_alpha
            low = self.source_filter_lowpass
            high = source_sample - low
            activity_target = clamp(
                abs(source_sample) / max(self.config.master_gain * 0.08, 1.0e-6),
                0.0,
                1.0,
            )
            activity_alpha = (
                activity_attack
                if activity_target > self.source_filter_activity
                else activity_release
            )
            self.source_filter_activity += (
                activity_target - self.source_filter_activity
            ) * activity_alpha
            residual = (
                math.tanh(
                    high * (9.0 + 17.0 * roughness)
                    + source_sample * (1.2 + 2.8 * roughness)
                )
                * roughness
                * (0.220 + 0.320 * high_band)
                if components.periodicity_roughness
                else 0.0
            )
            harmonic_edge = 0.0
            if components.harmonic_profile:
                harmonic_edge = math.tanh(
                    source_sample
                    * (2.8 + 4.0 * harmonic_second + 0.7 * harmonic_centroid)
                )
                harmonic_edge -= source_sample * (
                    2.4 + 3.2 * harmonic_third + 1.2 * even_harmonics
                )
                harmonic_edge *= (
                    0.014
                    + 0.020 * (harmonic_second + harmonic_third)
                    + 0.018 * upper_harmonics
                )
            excitation = source_sample + 0.24 * residual + 0.18 * harmonic_edge
            (
                mode_a,
                self.source_filter_mode_a_1,
                self.source_filter_mode_a_2,
            ) = self._resonator(
                excitation,
                self.source_filter_mode_a_1,
                self.source_filter_mode_a_2,
                coefficient_a,
                radius_a * radius_a,
                1.0 - radius_a,
            )
            (
                mode_b,
                self.source_filter_mode_b_1,
                self.source_filter_mode_b_2,
            ) = self._resonator(
                excitation,
                self.source_filter_mode_b_1,
                self.source_filter_mode_b_2,
                coefficient_b,
                radius_b * radius_b,
                1.0 - radius_b,
            )
            age_frames = (
                self.source_filter_control_age_start
                + self.source_filter_control_position
            )
            self.source_filter_control_position += 1
            age_seconds = age_frames / sample_rate
            pulse_phase = (
                age_seconds * control.pulse_rate_hz
                + ((self.source_filter_seed >> 9) & 0xFFFF) / 65536.0
            ) % 1.0
            pulse_wave = 1.0 - abs(2.0 * pulse_phase - 1.0)
            pulse_wave = pulse_wave * pulse_wave * (3.0 - 2.0 * pulse_wave)
            pulse_gain = 1.0 - pulse_depth * (1.0 - pulse_wave)

            self.source_filter_sub_phase = (
                self.source_filter_sub_phase + frequency * 0.5 / sample_rate
            ) % 1.0
            subharmonic = (
                math.sin(2.0 * math.pi * self.source_filter_sub_phase)
                * self.source_filter_activity
                * self.expression
                * self.config.master_gain
                * clamp(control.subharmonic_strength, 0.0, 0.55)
                * 0.010
                if components.subharmonic
                else 0.0
            )
            secondary_ratio = clamp(control.secondary_ratio, 0.62, 2.20)
            self.source_filter_secondary_phase = (
                self.source_filter_secondary_phase
                + frequency * secondary_ratio / sample_rate
            ) % 1.0
            secondary = (
                math.sin(2.0 * math.pi * self.source_filter_secondary_phase)
                * self.source_filter_activity
                * self.expression
                * self.config.master_gain
                * clamp(control.secondary_strength, 0.0, 0.45)
                * (0.006 + 0.008 * velocity)
                if components.secondary_frequency
                else 0.0
            )
            shaped_source = low_gain * low + high_gain * high
            raw = register_gain * (
                shaped_source
                + residual
                + harmonic_edge
                + resonance_mix * (0.68 * mode_a + 0.32 * mode_b)
                + subharmonic
                + secondary
            ) * envelope_gain * pulse_gain
            sample = raw / (1.0 + 1.12 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))
        if super().silent:
            self.source_filter_lowpass = 0.0
            self.source_filter_activity = 0.0
            self.source_filter_mode_a_1 = 0.0
            self.source_filter_mode_a_2 = 0.0
            self.source_filter_mode_b_1 = 0.0
            self.source_filter_mode_b_2 = 0.0
        return output

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []
        if self.silent:
            self._silence_immediately()
            return [0.0] * frames
        output: list[float] = []
        remaining = frames
        while remaining:
            if self.source_filter_control_remaining <= 0:
                self._start_source_filter_segment()
            block = min(remaining, self.source_filter_control_remaining)
            base = super().render(block)
            output.extend(self._process_source_filter_block(base))
            self.source_filter_control_remaining -= block
            remaining -= block
            if self.silent and remaining:
                output.extend([0.0] * remaining)
                self._silence_immediately()
                break
        return output
