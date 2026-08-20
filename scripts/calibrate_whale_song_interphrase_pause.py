#!/usr/bin/env python3
"""Development-only T044 interphrase-pause calibration with one-shot holdout evaluation."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_morph_bank import (  # noqa: E402
    read_bound_regular_bytes,
    regular_file_path,
    validated_output_path,
    write_atomic,
)
from whale_song_corpus import (  # noqa: E402
    MODEL_ENSEMBLE_SEEDS,
    STRUCTURAL_FEATURES,
    build_split_corpus,
    canonical_json_bytes,
    model_ensemble_feature_vector,
    split_source_bindings,
    split_summary,
    structural_distance,
    training_recommendations,
)
from whale_song_grammar import (  # noqa: E402
    BOUNDARY_MARGIN_SECONDS,
    PHRASE_PAUSE_JITTER_SECONDS,
    TRANSITION_PAUSE_JITTER_SECONDS,
    SongGrammarConfig,
)

TASK_ID = "AUDIO-CONTROL-PLANE-V1-T044"
EXPERIMENT_ID = "audio-whale-interphrase-pause-t044-v1"
DEFAULT_CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"
BASELINE_EVALUATION = DEFAULT_CORPUS_ROOT / "evaluation.json"
EVIDENCE_ROOT = DEFAULT_CORPUS_ROOT / "pause-calibration-v1"
CANONICAL_CANDIDATE = EVIDENCE_ROOT / "candidate.json"
CANONICAL_HOLDOUT_EVALUATION = EVIDENCE_ROOT / "holdout-evaluation.json"
FROZEN_V1_CANDIDATE_SHA256 = (
    "f343b326b149f0ea4b6c76bb0a0db0123eb8a7e91ca0c84a500f05a0ca22521e"
)
FROZEN_V1_CODE_BINDINGS = {
    "scripts/calibrate_whale_song_interphrase_pause.py": (
        "a7d49ccf73e77e65262168adfd95fa634db8f0e33133b76af27ac81bd98b47a0"
    ),
    "scripts/evaluate_whale_song_grammar_structure.py": (
        "bb8e3218894f66d9cf7d4fd9b19d7f4ef3456a6522b3ddb2e303c5a8a96e6889"
    ),
    "scripts/whale_song_corpus.py": (
        "b84bc7fddeac07edefaf425fd2bfedceedbdca5c47eff24588c3bd0b00d6b3b4"
    ),
    "scripts/whale_song_grammar.py": (
        "49a3a23fa3c3a34ed2f3baff313c4d023e03a6fc8ae58646221343b456dbec71"
    ),
}
CALIBRATION_SCRIPT_BINDING_PATH = "scripts/calibrate_whale_song_interphrase_pause.py"
EMPIRICAL_STRUCTURE_NAME = "empirical-structure.json"
FROZEN_BASELINE_EVALUATION_SHA256 = (
    "435e2c3132ea393c22a18c2dac78dd68b4d3f04917fb076a08f17e4476136617"
)
FROZEN_BASELINE_FILE_SHA256 = (
    "d617953522890c4668885bc38e0bcd6ece17eb2295b6a9bdaa63b56655f8e6f4"
)
HISTORICAL_TASK_INTAKE = {
    "paused_gap_relative_error": 0.22786836858667633,
    "unpaused_baseline_gap_relative_error": 0.08101189807106207,
    "source": "Bureau AUDIO-CONTROL-PLANE-V1-T044 task intake",
    "use_as_acceptance_threshold": False,
}
CODE_BINDING_PATHS = (
    "scripts/whale_song_corpus.py",
    "scripts/whale_song_grammar.py",
    "scripts/evaluate_whale_song_grammar_structure.py",
    "scripts/calibrate_whale_song_interphrase_pause.py",
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: pathlib.Path) -> str:
    safe = regular_file_path(path, f"T044 bound file {path.name}")
    return sha256_bytes(read_bound_regular_bytes(safe, f"T044 bound file {path.name}"))


def code_bindings() -> dict[str, str]:
    return {relative: sha256_file(ROOT / relative) for relative in CODE_BINDING_PATHS}


def config_from_projection(
    recommendation: dict[str, object], *, phrase_pause_seconds: float | None = None
) -> SongGrammarConfig:
    projected = recommendation.get("projected_current_config")
    if not isinstance(projected, dict):
        raise ValueError("projected current grammar config is missing")
    pause = (
        float(projected["phrase_pause_seconds"])
        if phrase_pause_seconds is None
        else float(phrase_pause_seconds)
    )
    return SongGrammarConfig(
        theme_count=int(projected["theme_count"]),
        phrase_repeats_min=int(projected["phrase_repeats_min"]),
        phrase_repeats_max=int(projected["phrase_repeats_max"]),
        phrase_pause_seconds=pause,
    )


def phrase_pause_bounds(config: SongGrammarConfig) -> tuple[float, float]:
    low = 0.45
    ordering_margin = (
        PHRASE_PAUSE_JITTER_SECONDS
        + TRANSITION_PAUSE_JITTER_SECONDS
        + BOUNDARY_MARGIN_SECONDS
    )
    high = min(2.5, config.transition_pause_seconds - ordering_margin)
    if high < low:
        raise ValueError("current grammar leaves no valid phrase-pause calibration range")
    return round(low, 6), round(high, 6)


def _gap_rae(model_gap: float, empirical_gap: float) -> float:
    return abs(model_gap - empirical_gap) / max(abs(empirical_gap), 1.0e-9)


def calibrate_development_pause(
    development: dict[str, object], recommendation: dict[str, object]
) -> dict[str, object]:
    """Select the pause using development data only.

    The fixed structural projection is retained. The model's mean hierarchical
    boundary pause is monotonic and affine over this one-dimensional parameter
    with the fixed deterministic seed ensemble, so two endpoint observations can
    propose the target crossing. The rounded proposal and its immediate
    microsecond neighbours are then evaluated and selected strictly by the
    development interphrase-gap relative absolute error.
    """

    if development.get("split") != "development":
        raise ValueError("pause calibration requires the development split")
    reference = development.get("feature_vector")
    if not isinstance(reference, dict):
        raise ValueError("development feature vector is missing")
    target = float(reference["mean_interphrase_gap_seconds"])
    base = config_from_projection(recommendation)
    low, high = phrase_pause_bounds(base)
    low_gap = float(
        model_ensemble_feature_vector(
            dataclasses.replace(base, phrase_pause_seconds=low)
        )["mean_interphrase_gap_seconds"]
    )
    high_gap = float(
        model_ensemble_feature_vector(
            dataclasses.replace(base, phrase_pause_seconds=high)
        )["mean_interphrase_gap_seconds"]
    )
    if math.isclose(high_gap, low_gap, abs_tol=1.0e-12):
        proposed = low
    else:
        proposed = low + (target - low_gap) * (high - low) / (high_gap - low_gap)
        proposed = min(high, max(low, proposed))
    rounded = round(proposed, 6)
    candidate_pauses = sorted(
        {
            low,
            high,
            round(min(high, max(low, rounded - 0.000001)), 6),
            rounded,
            round(min(high, max(low, rounded + 0.000001)), 6),
        }
    )
    candidates: list[dict[str, object]] = []
    for pause in candidate_pauses:
        config = dataclasses.replace(base, phrase_pause_seconds=pause)
        vector = model_ensemble_feature_vector(config)
        model_gap = float(vector["mean_interphrase_gap_seconds"])
        candidates.append(
            {
                "phrase_pause_seconds": pause,
                "model_mean_interphrase_gap_seconds": model_gap,
                "development_gap_relative_absolute_error": round(
                    _gap_rae(model_gap, target), 9
                ),
                "feature_vector": vector,
            }
        )
    winner = min(
        candidates,
        key=lambda item: (
            float(item["development_gap_relative_absolute_error"]),
            float(item["phrase_pause_seconds"]),
        ),
    )
    final_config = dataclasses.replace(
        base, phrase_pause_seconds=float(winner["phrase_pause_seconds"])
    )
    final_vector = winner["feature_vector"]
    if not isinstance(final_vector, dict):
        raise RuntimeError("calibration winner feature vector is invalid")
    return {
        "selection_method": "development-only-affine-crossing-plus-microsecond-neighbours",
        "objective": "mean_interphrase_gap_seconds_relative_absolute_error",
        "model_ensemble_seeds": list(MODEL_ENSEMBLE_SEEDS),
        "valid_phrase_pause_range_seconds": [low, high],
        "empirical_development_mean_interphrase_gap_seconds": target,
        "endpoint_model_gaps_seconds": {"low": low_gap, "high": high_gap},
        "unrounded_crossing_seconds": round(proposed, 12),
        "evaluated_candidates": candidates,
        "winner": winner,
        "final_config": dataclasses.asdict(final_config),
        "development_distance_all_current_features": structural_distance(
            final_vector, reference
        ),
    }


def _gap_definition(development: dict[str, object]) -> dict[str, object]:
    distributions = development.get("feature_distributions")
    if not isinstance(distributions, dict):
        raise ValueError("development feature distributions are missing")
    gaps = distributions.get("interphrase_gap_seconds")
    if not isinstance(gaps, dict):
        raise ValueError("development interphrase-gap distribution is missing")
    return {
        "name": "adjacent-released-phrase-window-gap-within-continuous-recording-session",
        "derivation": (
            "current phrase begin minus previous chronological phrase end; negative overlap is "
            "clamped to zero; gaps greater than 60 seconds start a new session and are excluded"
        ),
        "population": "released Raven phrase windows, gap-weighted",
        "session_break_seconds_exclusive_upper_bound": 60.0,
        "not_unit_or_intra_phrase_gap": True,
        "theme_boundary_status": (
            "not separately labelled by the released Raven tables; no special theme-boundary "
            "population is invented"
        ),
        "model_proxy": (
            "mean hierarchical phrase boundary pause; proxy mapping is retained from the "
            "canonical structural evaluator and is not asserted semantically identical"
        ),
        "development_reference_distribution": gaps,
    }


def freeze_candidate(
    corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT,
) -> dict[str, object]:
    """Freeze the final candidate without opening any holdout annotation payload."""

    development_corpus = build_split_corpus(corpus_root, "development")
    development = split_summary(development_corpus, "development")
    recommendation = training_recommendations(development)
    calibration = calibrate_development_pause(development, recommendation)
    bindings = split_source_bindings(corpus_root, "development")
    candidate: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_interphrase_pause_calibration_candidate",
        "experiment_id": EXPERIMENT_ID,
        "task_id": TASK_ID,
        "freeze_contract": {
            "selection_split": "2012-2016 development only",
            "holdout_split": "2017-2019",
            "holdout_annotation_payload_access_during_freeze": "forbidden",
            "implementation": "build_split_corpus(corpus_root, 'development')",
            "holdout_evaluation_allowed_after_freeze": True,
            "post_holdout_tuning_within_same_experiment": False,
        },
        "development_source_binding": {
            "source_manifest_sha256": development_corpus["source_manifest_sha256"],
            "split_corpus_sha256": development_corpus["corpus_sha256"],
            "record_count": development_corpus["record_count"],
            "phrase_count": development_corpus["phrase_count"],
            "annotation_bindings": bindings,
            "summary_sha256": sha256_json(development),
        },
        "gap_definition": _gap_definition(development),
        "structural_projection_before_pause_calibration": recommendation,
        "calibration": calibration,
        "final_config": calibration["final_config"],
        "metric_contract": {
            "historical_task_wording_feature_count": 6,
            "canonical_evaluator_feature_count": len(STRUCTURAL_FEATURES),
            "canonical_features": list(STRUCTURAL_FEATURES),
            "resolution": (
                "report the current seven-feature evaluator superset; do not omit the added "
                "analyzed-span metric to match historical six-feature wording"
            ),
        },
        "historical_task_intake": HISTORICAL_TASK_INTAKE,
        "baseline_validation": {
            "status": "deferred-until-post-freeze-holdout-evaluation",
            "reason": "the baseline artifact contains holdout results and is not read during calibration",
        },
        "code_bindings": code_bindings(),
        "does_not_establish": [
            "a production or live default change",
            "holdout improvement before the one-shot evaluation",
            "biological identity between model boundary pauses and empirical phrase gaps",
            "acoustic timbre or perceptual realism",
        ],
    }
    candidate["candidate_sha256"] = sha256_json(candidate)
    return candidate


def validate_candidate(candidate: dict[str, object]) -> None:
    if candidate.get("kind") != "humpback_whale_interphrase_pause_calibration_candidate":
        raise ValueError("candidate kind is invalid")
    expected = candidate.get("candidate_sha256")
    if not isinstance(expected, str):
        raise ValueError("candidate sha256 is missing")
    payload = dict(candidate)
    payload.pop("candidate_sha256")
    if sha256_json(payload) != expected:
        raise ValueError("candidate sha256 mismatch")

    stored_bindings = candidate.get("code_bindings")
    current_bindings = code_bindings()
    if stored_bindings != current_bindings:
        is_frozen_v1 = (
            expected == FROZEN_V1_CANDIDATE_SHA256
            and stored_bindings == FROZEN_V1_CODE_BINDINGS
        )
        if not is_frozen_v1:
            raise ValueError(
                "candidate code binding matches neither the current evaluator nor the canonical frozen v1 revision"
            )
        for relative in CODE_BINDING_PATHS:
            if relative == CALIBRATION_SCRIPT_BINDING_PATH:
                continue
            if current_bindings.get(relative) != FROZEN_V1_CODE_BINDINGS[relative]:
                raise ValueError(
                    "canonical frozen v1 candidate is incompatible with a changed computational dependency"
                )

    final_config = candidate.get("final_config")
    if not isinstance(final_config, dict):
        raise ValueError("candidate final config is missing")
    SongGrammarConfig(**final_config)


def load_candidate(path: pathlib.Path) -> dict[str, object]:
    payload = read_bound_regular_bytes(
        regular_file_path(path, "T044 frozen candidate"), "T044 frozen candidate"
    )
    candidate = json.loads(payload)
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a JSON object")
    validate_candidate(candidate)
    return candidate


def load_baseline_evaluation(
    path: pathlib.Path = BASELINE_EVALUATION,
) -> dict[str, object]:
    payload = read_bound_regular_bytes(
        regular_file_path(path, "T044 baseline evaluation"), "T044 baseline evaluation"
    )
    baseline = json.loads(payload)
    if not isinstance(baseline, dict):
        raise ValueError("baseline evaluation must be a JSON object")
    expected = baseline.get("evaluation_sha256")
    if not isinstance(expected, str):
        raise ValueError("baseline evaluation sha256 is missing")
    unhashed = dict(baseline)
    unhashed.pop("evaluation_sha256")
    if sha256_json(unhashed) != expected:
        raise ValueError("baseline evaluation internal sha256 mismatch")
    comparison = baseline.get("comparison_contract")
    if not isinstance(comparison, dict) or set(comparison) != set(STRUCTURAL_FEATURES):
        raise ValueError("baseline comparison contract no longer matches canonical features")
    return baseline


def resolve_repository_path(path: pathlib.Path, label: str) -> pathlib.Path:
    candidate = path if path.is_absolute() else ROOT / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the repository") from exc
    return resolved


def load_empirical_structure(path: pathlib.Path) -> dict[str, object]:
    payload = read_bound_regular_bytes(
        regular_file_path(path, "T044 empirical structure"), "T044 empirical structure"
    )
    empirical = json.loads(payload)
    if not isinstance(empirical, dict):
        raise ValueError("empirical structure must be a JSON object")
    expected = empirical.get("empirical_sha256")
    if not isinstance(expected, str):
        raise ValueError("empirical structure sha256 is missing")
    unhashed = dict(empirical)
    unhashed.pop("empirical_sha256")
    if sha256_json(unhashed) != expected:
        raise ValueError("empirical structure internal sha256 mismatch")
    return empirical


def validate_evaluation_inputs(
    candidate: dict[str, object],
    corpus_root: pathlib.Path,
    baseline_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, dict[str, object]]:
    normalized_corpus_root = resolve_repository_path(corpus_root, "T044 corpus root")
    normalized_baseline = resolve_repository_path(baseline_path, "T044 baseline")

    development_binding = candidate.get("development_source_binding")
    if not isinstance(development_binding, dict):
        raise ValueError("candidate development source binding is missing")
    candidate_manifest_sha = development_binding.get("source_manifest_sha256")
    if not isinstance(candidate_manifest_sha, str):
        raise ValueError("candidate source manifest sha256 is missing")

    corpus_manifest_sha = sha256_file(normalized_corpus_root / "source-manifest.json")
    if corpus_manifest_sha != candidate_manifest_sha:
        raise ValueError("candidate and holdout corpus source manifest mismatch")

    baseline = load_baseline_evaluation(normalized_baseline)
    if baseline.get("evaluation_sha256") != FROZEN_BASELINE_EVALUATION_SHA256:
        raise ValueError("baseline evaluation is not the frozen T044 oracle revision")
    if sha256_file(normalized_baseline) != FROZEN_BASELINE_FILE_SHA256:
        raise ValueError("baseline file is not the frozen T044 oracle artifact")
    empirical = load_empirical_structure(
        normalized_baseline.parent / EMPIRICAL_STRUCTURE_NAME
    )
    if baseline.get("empirical_sha256") != empirical.get("empirical_sha256"):
        raise ValueError("baseline evaluation and empirical structure identity mismatch")
    if baseline.get("corpus_sha256") != empirical.get("corpus_sha256"):
        raise ValueError("baseline evaluation and empirical corpus identity mismatch")
    empirical_manifest_sha = empirical.get("source_manifest_sha256")
    if empirical_manifest_sha != candidate_manifest_sha:
        raise ValueError("candidate and baseline source manifest mismatch")
    baseline_manifest_sha = sha256_file(
        normalized_baseline.parent / "source-manifest.json"
    )
    if baseline_manifest_sha != empirical_manifest_sha:
        raise ValueError("baseline and its source manifest identity mismatch")

    return normalized_corpus_root, normalized_baseline, baseline


def _distance_metrics(distance: dict[str, object]) -> dict[str, dict[str, object]]:
    features = distance.get("features")
    if not isinstance(features, dict) or set(features) != set(STRUCTURAL_FEATURES):
        raise ValueError("structural distance feature set drifted")
    return {key: features[key] for key in STRUCTURAL_FEATURES}


def evaluate_frozen_candidate(
    candidate: dict[str, object],
    corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT,
    baseline_path: pathlib.Path = BASELINE_EVALUATION,
) -> dict[str, object]:
    """Evaluate the already-frozen candidate against the holdout without retuning."""

    if CANONICAL_HOLDOUT_EVALUATION.exists():
        raise RuntimeError(
            "holdout evaluation already exists; T044 canonical evaluation is globally single-use"
        )
    validate_candidate(candidate)
    corpus_root, baseline_path, baseline = validate_evaluation_inputs(
        candidate, corpus_root, baseline_path
    )
    holdout_corpus = build_split_corpus(corpus_root, "holdout")
    holdout = split_summary(holdout_corpus, "holdout")
    final_config = candidate["final_config"]
    if not isinstance(final_config, dict):
        raise ValueError("candidate final config is missing")
    config = SongGrammarConfig(**final_config)
    calibrated_vector = model_ensemble_feature_vector(config)
    calibrated_distance = structural_distance(
        calibrated_vector, holdout["feature_vector"]
    )

    default_distance = baseline["default"]["holdout_distance"]
    historical_distance = baseline["development_fitted_projection"]["holdout_distance"]
    default_metrics = _distance_metrics(default_distance)
    historical_metrics = _distance_metrics(historical_distance)
    calibrated_metrics = _distance_metrics(calibrated_distance)
    per_feature = {
        key: {
            "empirical": calibrated_metrics[key]["empirical"],
            "default_relative_absolute_error": default_metrics[key][
                "relative_absolute_error"
            ],
            "historical_projection_relative_absolute_error": historical_metrics[key][
                "relative_absolute_error"
            ],
            "calibrated_relative_absolute_error": calibrated_metrics[key][
                "relative_absolute_error"
            ],
        }
        for key in STRUCTURAL_FEATURES
    }
    calibrated_mean = float(calibrated_distance["mean_relative_absolute_error"])
    default_mean = float(default_distance["mean_relative_absolute_error"])
    historical_mean = float(historical_distance["mean_relative_absolute_error"])
    gap_key = "mean_interphrase_gap_seconds"
    evaluation: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_interphrase_pause_holdout_evaluation",
        "experiment_id": EXPERIMENT_ID,
        "task_id": TASK_ID,
        "candidate_sha256": candidate["candidate_sha256"],
        "holdout_evaluation_ordinal": 1,
        "split_contract": {
            "calibration": "2012-2016 development only",
            "evaluation": "2017-2019 frozen holdout",
            "candidate_frozen_before_holdout_payload_access": True,
            "holdout_used_for_selection": False,
            "post_holdout_tuning_within_same_experiment": False,
        },
        "holdout_source_binding": {
            "source_manifest_sha256": holdout_corpus["source_manifest_sha256"],
            "split_corpus_sha256": holdout_corpus["corpus_sha256"],
            "record_count": holdout_corpus["record_count"],
            "phrase_count": holdout_corpus["phrase_count"],
            "annotation_bindings": split_source_bindings(corpus_root, "holdout"),
            "summary_sha256": sha256_json(holdout),
        },
        "oracle_binding": {
            "baseline_path": str(baseline_path.relative_to(ROOT)),
            "baseline_file_sha256": sha256_file(baseline_path),
            "source_manifest_sha256": candidate["development_source_binding"][
                "source_manifest_sha256"
            ],
            "baseline_evaluation_sha256": baseline["evaluation_sha256"],
            "comparison_contract": baseline["comparison_contract"],
            "canonical_features": list(STRUCTURAL_FEATURES),
            "feature_count": len(STRUCTURAL_FEATURES),
        },
        "final_config": final_config,
        "model_ensemble_seeds": list(MODEL_ENSEMBLE_SEEDS),
        "calibrated_feature_vector": calibrated_vector,
        "calibrated_holdout_distance": calibrated_distance,
        "comparators": {
            "default_holdout_distance": default_distance,
            "historical_development_projection_holdout_distance": historical_distance,
        },
        "per_feature": per_feature,
        "holdout_result": {
            "metric": "mean_relative_absolute_error_across_all_current_structural_features",
            "lower_is_better": True,
            "feature_count": len(STRUCTURAL_FEATURES),
            "default": default_mean,
            "historical_development_projection": historical_mean,
            "calibrated": calibrated_mean,
            "delta_calibrated_minus_historical": round(
                calibrated_mean - historical_mean, 9
            ),
            "delta_calibrated_minus_default": round(calibrated_mean - default_mean, 9),
            "calibrated_improves_historical_projection": calibrated_mean
            < historical_mean,
            "calibrated_improves_default": calibrated_mean < default_mean,
            "interphrase_gap": {
                "default_relative_absolute_error": default_metrics[gap_key][
                    "relative_absolute_error"
                ],
                "historical_projection_relative_absolute_error": historical_metrics[
                    gap_key
                ]["relative_absolute_error"],
                "calibrated_relative_absolute_error": calibrated_metrics[gap_key][
                    "relative_absolute_error"
                ],
            },
        },
        "historical_task_intake": HISTORICAL_TASK_INTAKE,
        "metric_contract_resolution": candidate["metric_contract"],
        "decision_boundary": {
            "changes_live_or_default": False,
            "separate_default_decision_required_if_evidence_warrants": True,
            "this_evaluation_does_not_authorize_default_change": True,
        },
        "does_not_establish": [
            "causal biological optimality",
            "acoustic timbre realism",
            "human perceptual preference",
            "permission to change the live or default grammar",
        ],
    }
    evaluation["evaluation_sha256"] = sha256_json(evaluation)
    return evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="select and freeze using development only")
    freeze.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    freeze.add_argument("--output", type=pathlib.Path, required=True)

    evaluate = subparsers.add_parser(
        "evaluate", help="evaluate one frozen candidate against the holdout exactly once"
    )
    evaluate.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    evaluate.add_argument("--candidate", type=pathlib.Path, required=True)
    evaluate.add_argument("--baseline", type=pathlib.Path, default=BASELINE_EVALUATION)
    evaluate.add_argument("--output", type=pathlib.Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = validated_output_path(args.output)
    if args.command == "freeze":
        if CANONICAL_CANDIDATE.exists():
            raise RuntimeError(
                "T044 canonical candidate already exists; the development freeze is immutable"
            )
        if output.resolve() != CANONICAL_CANDIDATE.resolve():
            raise ValueError("T044 freeze output must use the canonical candidate path")
        candidate = freeze_candidate(args.corpus_root)
        write_atomic(output, candidate)
        print(
            json.dumps(
                {
                    "state": "candidate-frozen",
                    "output": str(output),
                    "candidate_sha256": candidate["candidate_sha256"],
                    "phrase_pause_seconds": candidate["final_config"][
                        "phrase_pause_seconds"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0

    if CANONICAL_HOLDOUT_EVALUATION.exists():
        raise RuntimeError(
            "holdout evaluation already exists; T044 canonical evaluation is globally single-use"
        )
    if output.resolve() != CANONICAL_HOLDOUT_EVALUATION.resolve():
        raise ValueError("T044 holdout output must use the canonical evaluation path")
    candidate = load_candidate(resolve_repository_path(args.candidate, "T044 candidate"))
    evaluation = evaluate_frozen_candidate(candidate, args.corpus_root, args.baseline)
    write_atomic(output, evaluation)
    print(
        json.dumps(
            {
                "state": "holdout-evaluated-once",
                "output": str(output),
                "evaluation_sha256": evaluation["evaluation_sha256"],
                "holdout_result": evaluation["holdout_result"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
