#!/usr/bin/env python3
"""Build a controlled, anonymous humpback-song hierarchy listening test.

The legacy exploratory pair changes phrase-block order.  This stricter protocol
keeps the exact concrete unit inventory and order fixed and ablates only the
*distribution* of inter-phrase boundary pauses.  The total boundary-pause budget,
voice, render engine, gain and output duration remain matched.

This module is offline-only.  It never starts the whale service and never mutates
PipeWire, MIDI, device, profile or live-default state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import secrets
import sys
from dataclasses import replace
from typing import Sequence

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_morph_bank import (  # noqa: E402
    reject_symlink_components,
    validated_output_path,
    write_atomic,
)
from evaluate_whale_song_grammar_structure import fitted_config  # noqa: E402
from study_whale_song_grammar import MAX_RENDER_SECONDS, render_prefix, sha256_file  # noqa: E402
from whale_live_engine import signal_metrics, write_stereo_wav  # noqa: E402
from whale_song_corpus import (  # noqa: E402
    build_corpus,
    split_summary,
    training_recommendations,
)
from whale_song_grammar import (  # noqa: E402
    PhrasePlan,
    SongCyclePlan,
    SongSessionPlan,
    ThemePlan,
    UnitPlan,
    WhaleSongGrammar,
    iter_phrases,
    iter_units,
    plan_sha256,
)

DEFAULT_CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"
DEFAULT_TRIALS = 4
MAX_TRIALS = 12
SAMPLE_RATE_HZ = 48_000


def _round(value: float) -> float:
    return round(float(value), 6)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _unit_identity(unit: UnitPlan) -> dict[str, object]:
    """Return the concrete non-timing identity preserved across conditions."""

    return {
        "unit_id": unit.unit_id,
        "kind": unit.kind,
        "origin_theme_id": unit.origin_theme_id,
        "duration_seconds": unit.duration_seconds,
        "gap_seconds": unit.gap_seconds,
        "note": unit.note,
        "velocity": unit.velocity,
        "bend_value": unit.bend_value,
        "pulse_count": unit.pulse_count,
        "flourish": unit.flourish,
    }


def _unit_identities(session: SongSessionPlan) -> list[dict[str, object]]:
    return [_unit_identity(unit) for unit in iter_units(session)]


def _select_control_window(
    session: SongSessionPlan,
    *,
    max_seconds: float,
) -> tuple[PhrasePlan, ...]:
    """Choose a deterministic bounded phrase window around a real transition.

    A valid window contains a transition phrase plus at least one phrase before
    and after it.  Among windows containing the first transition that can satisfy
    the bound, prefer the largest number of complete phrase blocks, then the
    earliest source start.  The final phrase's trailing boundary pause is excluded
    from the audible window; every inter-block boundary is retained.
    """

    if not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_RENDER_SECONDS:
        raise ValueError(f"seconds must be finite and in (0, {MAX_RENDER_SECONDS:g}]")
    phrases = list(iter_phrases(session))
    transition_indexes = [
        index
        for index, phrase in enumerate(phrases)
        if phrase.role == "transition" and index > 0 and index + 1 < len(phrases)
    ]
    if not transition_indexes:
        raise RuntimeError("controlled hierarchy test requires a bounded transition phrase")

    minimum_required: float | None = None
    for transition_index in transition_indexes:
        candidates: list[tuple[int, int, float]] = []
        for start in range(0, transition_index):
            for end in range(transition_index + 1, len(phrases)):
                span = phrases[end].body_end_seconds - phrases[start].start_seconds
                if start == transition_index - 1 and end == transition_index + 1:
                    minimum_required = span if minimum_required is None else min(minimum_required, span)
                if span <= max_seconds + 1.0e-9:
                    candidates.append((start, end, span))
                else:
                    break
        if candidates:
            start, end, _span = max(
                candidates,
                key=lambda item: (
                    item[1] - item[0] + 1,
                    -item[0],
                    item[1],
                ),
            )
            return tuple(phrases[start : end + 1])

    detail = ""
    if minimum_required is not None:
        detail = f"; first usable transition window needs at least {minimum_required:.6f} seconds"
    raise ValueError(f"seconds are too short for a before/transition/after control window{detail}")


def _flat_container(
    source: SongSessionPlan,
    units: Sequence[UnitPlan],
    *,
    duration_seconds: float,
    condition_id: str,
) -> SongSessionPlan:
    if not units:
        raise ValueError("controlled hierarchy condition requires at least one unit")
    duration = _round(duration_seconds)
    body_end = units[-1].sound_end_seconds
    if body_end > duration + 1.0e-6:
        raise ValueError("condition units exceed the common render duration")
    phrase = PhrasePlan(
        phrase_id=f"controlled-{condition_id}",
        family_id="controlled",
        role="theme",
        variant_index=0,
        start_seconds=0.0,
        body_end_seconds=_round(body_end),
        end_seconds=duration,
        boundary_pause_seconds=_round(max(0.0, duration - body_end)),
        units=tuple(units),
    )
    theme = ThemePlan(
        theme_id="controlled",
        cycle_index=0,
        start_seconds=0.0,
        end_seconds=duration,
        phrases=(phrase,),
    )
    cycle = SongCyclePlan(
        cycle_index=0,
        start_seconds=0.0,
        end_seconds=duration,
        themes=(theme,),
        transitions=(),
    )
    return SongSessionPlan(
        schema_version=source.schema_version,
        seed=source.seed,
        base_note=source.base_note,
        start_seconds=0.0,
        duration_seconds=duration,
        cycles=(cycle,),
    )


def _structured_window_units(phrases: Sequence[PhrasePlan]) -> tuple[list[UnitPlan], float]:
    source_start = phrases[0].start_seconds
    units = [
        replace(unit, start_seconds=_round(unit.start_seconds - source_start))
        for phrase in phrases
        for unit in phrase.units
    ]
    duration = _round(phrases[-1].body_end_seconds - source_start)
    return units, duration


def _flattened_boundary_units(
    phrases: Sequence[PhrasePlan],
) -> tuple[list[UnitPlan], float, float, float]:
    """Retain phrase/unit order but equalize inter-block boundary pauses."""

    if len(phrases) < 3:
        raise ValueError("controlled hierarchy window requires at least three phrase blocks")
    boundaries = [float(phrase.boundary_pause_seconds) for phrase in phrases[:-1]]
    if not boundaries:
        raise ValueError("controlled hierarchy window has no inter-block boundary")
    total_boundary = math.fsum(boundaries)
    flat_boundary = total_boundary / len(boundaries)

    units: list[UnitPlan] = []
    cursor = 0.0
    for phrase_index, phrase in enumerate(phrases):
        phrase_offset = cursor
        for unit in phrase.units:
            units.append(
                replace(
                    unit,
                    start_seconds=_round(
                        phrase_offset + (unit.start_seconds - phrase.start_seconds)
                    ),
                )
            )
        cursor = phrase_offset + (phrase.body_end_seconds - phrase.start_seconds)
        if phrase_index < len(phrases) - 1:
            cursor += flat_boundary
    return units, _round(cursor), flat_boundary, total_boundary


def build_condition_plans(
    corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT,
    *,
    seconds: float = 30.0,
) -> tuple[SongSessionPlan, SongSessionPlan, dict[str, object]]:
    """Build matched structured/flat-timing plans without rendering audio."""

    corpus = build_corpus(corpus_root)
    development = split_summary(corpus, "development")
    recommendations = training_recommendations(development)
    source = WhaleSongGrammar(fitted_config(recommendations)).generate()
    phrases = _select_control_window(source, max_seconds=seconds)

    structured_units, structured_duration = _structured_window_units(phrases)
    flat_units, flat_duration, flat_boundary, total_boundary = _flattened_boundary_units(phrases)
    structured_identities = [_unit_identity(unit) for unit in structured_units]
    flat_identities = [_unit_identity(unit) for unit in flat_units]
    if structured_identities != flat_identities:
        raise RuntimeError("controlled hierarchy ablation changed concrete unit inventory or order")

    common_duration = max(structured_duration, flat_duration)
    if common_duration > seconds + 1.0e-5:
        raise RuntimeError("controlled hierarchy timing normalization exceeded requested duration")
    if abs(structured_duration - flat_duration) > 2.0e-5:
        raise RuntimeError("controlled hierarchy timing normalization changed total audible span")

    structured = _flat_container(
        source,
        structured_units,
        duration_seconds=common_duration,
        condition_id="structured-timing",
    )
    flat = _flat_container(
        source,
        flat_units,
        duration_seconds=common_duration,
        condition_id="flat-boundary-timing",
    )
    unit_identity_sha = _canonical_sha256(structured_identities)
    unit_order_sha = _canonical_sha256([item["unit_id"] for item in structured_identities])
    source_boundaries = [
        {
            "after_phrase_id": phrase.phrase_id,
            "after_role": phrase.role,
            "boundary_pause_seconds": phrase.boundary_pause_seconds,
        }
        for phrase in phrases[:-1]
    ]
    control: dict[str, object] = {
        "corpus_sha256": corpus["corpus_sha256"],
        "development_only_selection": recommendations,
        "source_plan_sha256": plan_sha256(source),
        "source_phrase_ids": [phrase.phrase_id for phrase in phrases],
        "source_phrase_roles": [phrase.role for phrase in phrases],
        "source_boundaries": source_boundaries,
        "source_window_start_seconds": phrases[0].start_seconds,
        "source_window_body_end_seconds": phrases[-1].body_end_seconds,
        "requested_max_seconds": seconds,
        "render_seconds": common_duration,
        "unit_count": len(structured_identities),
        "unit_identity_sha256": unit_identity_sha,
        "unit_order_sha256": unit_order_sha,
        "structured_plan_sha256": plan_sha256(structured),
        "flat_boundary_plan_sha256": plan_sha256(flat),
        "structured_duration_seconds": structured_duration,
        "flat_boundary_duration_seconds": flat_duration,
        "source_inter_block_pause_total_seconds": total_boundary,
        "flat_boundary_pause_seconds": flat_boundary,
        "same_concrete_unit_inventory": True,
        "same_unit_order": True,
        "same_total_inter_block_pause_budget": True,
    }
    return structured, flat, control


def _trial_schedule(assignment_seed: bytes, trials: int) -> list[dict[str, object]]:
    if isinstance(trials, bool) or not isinstance(trials, int):
        raise ValueError("trials must be an integer")
    if not 4 <= trials <= MAX_TRIALS or trials % 4:
        raise ValueError(f"trials must be a multiple of 4 between 4 and {MAX_TRIALS}")
    if not isinstance(assignment_seed, bytes) or len(assignment_seed) < 16:
        raise ValueError("assignment_seed must contain at least 128 bits of secret entropy")
    assignment_offset = assignment_seed[0] % 2
    order_offset = assignment_seed[1] % 2
    schedule: list[dict[str, object]] = []
    for index in range(trials):
        swap_assignment = (index + assignment_offset) % 2 == 1
        assignment = (
            {"A": "flat_boundary_timing", "B": "structured_timing"}
            if swap_assignment
            else {"A": "structured_timing", "B": "flat_boundary_timing"}
        )
        swap_order = ((index // 2) + order_offset) % 2 == 1
        presentation_order = ["B", "A"] if swap_order else ["A", "B"]
        schedule.append(
            {
                "trial_id": f"trial-{index + 1:02d}",
                "assignment": assignment,
                "presentation_order": presentation_order,
            }
        )
    return schedule


def _level_match(
    rendered: dict[str, tuple[list[float], dict[str, object]]],
) -> tuple[dict[str, tuple[list[float], dict[str, object]]], dict[str, object]]:
    audible_rms = [float(metrics["rms"]) for _audio, metrics in rendered.values()]
    if any(rms <= 0.0 for rms in audible_rms):
        raise RuntimeError("controlled hierarchy comparison requires two non-silent renders")
    if any(float(metrics["peak"]) > 1.0 for _audio, metrics in rendered.values()):
        raise RuntimeError("controlled hierarchy source render would clip at WAV output")
    target_rms = min(audible_rms)
    matched: dict[str, tuple[list[float], dict[str, object]]] = {}
    scales: dict[str, float] = {}
    for condition, (audio, original_metrics) in rendered.items():
        scale = target_rms / float(original_metrics["rms"])
        if not 0 < scale <= 1.0 + 1.0e-12:
            raise RuntimeError("controlled hierarchy level matching must only attenuate")
        scale = min(scale, 1.0)
        normalized = [sample * scale for sample in audio]
        metrics = dict(original_metrics)
        metrics.update(signal_metrics(normalized))
        metrics["level_match_scale"] = round(scale, 9)
        metrics["level_match_target_rms"] = target_rms
        metrics["source_peak_before_level_match"] = float(original_metrics["peak"])
        metrics["source_rms_before_level_match"] = float(original_metrics["rms"])
        if float(metrics["peak"]) > float(original_metrics["peak"]) + 1.0e-12:
            raise RuntimeError("level matching amplified a source render")
        matched[condition] = (normalized, metrics)
        scales[condition] = round(scale, 9)
    return matched, {
        "method": "attenuation_only_to_lower_source_rms",
        "target_rms": target_rms,
        "condition_scales": scales,
        "clipping_allowed": False,
        "amplification_allowed": False,
    }


def build_controlled_blind_test(
    output_dir: pathlib.Path,
    *,
    corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT,
    seconds: float = 30.0,
    gain: float = 0.16,
    trials: int = DEFAULT_TRIALS,
    assignment_seed: bytes | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    if not math.isfinite(gain) or not 0 < gain <= 0.25:
        raise ValueError("gain must be finite and in (0, 0.25]")
    structured, flat, control = build_condition_plans(corpus_root, seconds=seconds)
    render_seconds = float(control["render_seconds"])
    rendered = {
        "structured_timing": render_prefix(structured, seconds=render_seconds, gain=gain),
        "flat_boundary_timing": render_prefix(flat, seconds=render_seconds, gain=gain),
    }
    matched, level_rule = _level_match(rendered)

    pair_identity = _canonical_sha256(
        {
            "protocol": "matched-inventory-boundary-timing-ablation-v1",
            "corpus_sha256": control["corpus_sha256"],
            "source_plan_sha256": control["source_plan_sha256"],
            "source_phrase_ids": control["source_phrase_ids"],
            "unit_identity_sha256": control["unit_identity_sha256"],
            "structured_plan_sha256": control["structured_plan_sha256"],
            "flat_boundary_plan_sha256": control["flat_boundary_plan_sha256"],
            "render_seconds": render_seconds,
            "gain": gain,
            "level_rule": level_rule["method"],
        }
    )
    secret_assignment_seed = assignment_seed if assignment_seed is not None else secrets.token_bytes(32)
    schedule = _trial_schedule(secret_assignment_seed, trials)
    assignment_seed_sha256 = hashlib.sha256(secret_assignment_seed).hexdigest()

    safe_output_dir = reject_symlink_components(
        output_dir, "whale hierarchy blind output directory"
    )
    safe_output_dir.mkdir(parents=True, exist_ok=True)
    safe_output_dir = reject_symlink_components(
        safe_output_dir, "whale hierarchy blind output directory"
    )
    if not safe_output_dir.is_dir():
        raise RuntimeError("whale hierarchy blind output must be a directory")

    public_trials: list[dict[str, object]] = []
    answer_trials: list[dict[str, object]] = []
    for trial in schedule:
        trial_id = str(trial["trial_id"])
        assignment = dict(trial["assignment"])
        public_samples: dict[str, object] = {}
        for label in ("A", "B"):
            condition = str(assignment[label])
            audio, metrics = matched[condition]
            target = validated_output_path(safe_output_dir / f"{trial_id}-{label}.wav")
            write_stereo_wav(target, audio, SAMPLE_RATE_HZ)
            public_samples[label] = {
                "file": target.name,
                "sha256": sha256_file(target),
                "duration_seconds": render_seconds,
                "signal_metrics": metrics,
            }
        public_trials.append(
            {
                "trial_id": trial_id,
                "presentation_order": list(trial["presentation_order"]),
                "samples": public_samples,
            }
        )
        answer_trials.append(
            {
                "trial_id": trial_id,
                "assignment": assignment,
                "presentation_order": list(trial["presentation_order"]),
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 2,
        "kind": "humpback_whale_song_hierarchy_controlled_blind_test",
        "protocol": "matched-inventory-boundary-timing-ablation-v1",
        "pair_identity_sha256": pair_identity,
        "assignment_seed_sha256": assignment_seed_sha256,
        "corpus_sha256": control["corpus_sha256"],
        "requested_max_seconds": seconds,
        "render_seconds": render_seconds,
        "voice": "WhaleMorphVoice",
        "render_engine": "study_whale_song_grammar.render_prefix",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "input_gain": gain,
        "stimulus_control": {
            "source_phrase_count": len(control["source_phrase_ids"]),
            "source_phrase_roles": control["source_phrase_roles"],
            "unit_count": control["unit_count"],
            "unit_identity_sha256": control["unit_identity_sha256"],
            "unit_order_sha256": control["unit_order_sha256"],
            "same_concrete_unit_inventory": True,
            "same_unit_order": True,
            "same_voice": True,
            "same_render_engine": True,
            "same_output_parameters": True,
            "same_total_inter_block_pause_budget": True,
            "source_inter_block_pause_total_seconds": control[
                "source_inter_block_pause_total_seconds"
            ],
            "flat_boundary_pause_seconds": control["flat_boundary_pause_seconds"],
            "conditions_differ_only_in": [
                "distribution of inter-phrase boundary timing inside the matched source window"
            ],
        },
        "level_and_duration_control": level_rule,
        "trials": public_trials,
        "instructions": [
            "Keep answer-key.json unavailable to the listener until responses are frozen.",
            "Use exactly one assigned trial per human listening session; the trial set exists for counterbalancing, not repeated pseudo-replication.",
            "Follow each trial's presentation_order and use the same playback chain and gain for both files.",
            "Record responses in a separate copy of response-template.json before reading the answer key.",
        ],
        "response_contract": {
            "template_file": "response-template.json",
            "unit": "one blinded trial per human listening session",
            "required_response_fields": [
                "listener_id",
                "trial_id",
                "hierarchy_guess",
                "preference",
            ],
            "hierarchy_guess_values": ["A", "B", "unsure"],
            "preference_values": ["A", "B", "no_preference"],
            "evaluation_rule": [
                "Join a frozen human response to the hidden trial assignment only after response capture.",
                "Hierarchy recognition is descriptive correct/incorrect/unsure against the structured_timing label; unsure is never converted to correct.",
                "Preference is descriptive structured/flat/no_preference after unblinding; no_preference stays in the denominator as an explicit abstention when counts are reported.",
                "Do not infer perceptual preference, whale-likeness or hierarchy detectability from generated audio, automated tests or an empty response set.",
            ],
            "status_without_human_responses": "indeterminate",
        },
        "perceptual_result": {
            "status": "indeterminate",
            "reason": "builder creates stimuli and protocol evidence only; no human responses are generated",
        },
        "remaining_confounders": [
            "The exact unit sequence and transition-unit content remain present in both conditions; this test isolates boundary-timing distribution, not all possible hierarchy cues.",
            "One deterministic source-derived engineering grammar window does not establish population-wide humpback realism.",
            "Human listening-chain and listener effects require separately recorded response metadata and are not controlled by this builder.",
        ],
        "does_not_establish": [
            "human preference without real frozen responses",
            "human hierarchy detection without real frozen responses",
            "population-wide whale likeness",
            "a causal effect of hierarchy cues other than inter-phrase boundary timing distribution",
            "biological correctness",
            "a live-mode, service, device, profile or default change",
        ],
    }
    answer_key: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_song_hierarchy_controlled_answer_key",
        "pair_identity_sha256": pair_identity,
        "assignment_seed_hex": secret_assignment_seed.hex(),
        "assignment_seed_sha256": assignment_seed_sha256,
        "protocol": manifest["protocol"],
        "source_window": {
            "source_plan_sha256": control["source_plan_sha256"],
            "source_phrase_ids": control["source_phrase_ids"],
            "source_phrase_roles": control["source_phrase_roles"],
            "source_boundaries": control["source_boundaries"],
            "source_window_start_seconds": control["source_window_start_seconds"],
            "source_window_body_end_seconds": control["source_window_body_end_seconds"],
        },
        "condition_plans": {
            "structured_timing": control["structured_plan_sha256"],
            "flat_boundary_timing": control["flat_boundary_plan_sha256"],
        },
        "unit_identity_sha256": control["unit_identity_sha256"],
        "unit_order_sha256": control["unit_order_sha256"],
        "trial_assignments": answer_trials,
        "development_only_selection": control["development_only_selection"],
    }
    response_template: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_song_hierarchy_blind_human_responses",
        "pair_identity_sha256": pair_identity,
        "capture_rule": "copy this file, add only real human responses while answer-key.json remains hidden, then freeze the response file before unblinding",
        "response_fields": {
            "listener_id": "pseudonymous non-empty text",
            "trial_id": [item["trial_id"] for item in public_trials],
            "hierarchy_guess": ["A", "B", "unsure"],
            "preference": ["A", "B", "no_preference"],
        },
        "responses": [],
        "perceptual_result_without_responses": "indeterminate",
    }
    write_atomic(
        validated_output_path(safe_output_dir / "blind-manifest.json"), manifest
    )
    write_atomic(
        validated_output_path(safe_output_dir / "answer-key.json"), answer_key
    )
    write_atomic(
        validated_output_path(safe_output_dir / "response-template.json"), response_template
    )
    return manifest, answer_key, response_template


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an offline controlled A/B test that preserves exact unit inventory/order "
            "and ablates only hierarchy boundary-timing distribution."
        )
    )
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--gain", type=float, default=0.16)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest, _answer, _responses = build_controlled_blind_test(
        args.output_dir,
        corpus_root=args.corpus_root,
        seconds=args.seconds,
        gain=args.gain,
        trials=args.trials,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
