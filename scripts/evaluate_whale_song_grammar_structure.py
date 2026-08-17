#!/usr/bin/env python3
"""Evaluate WhaleSongGrammar against a frozen later-year structural holdout."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_morph_bank import validated_output_path, write_atomic  # noqa: E402
from whale_song_corpus import (  # noqa: E402
    build_corpus,
    canonical_json_bytes,
    MODEL_ENSEMBLE_SEEDS,
    grammar_feature_vector,
    model_ensemble_feature_vector,
    split_summary,
    structural_distance,
    summarize_values,
    training_recommendations,
)
from whale_song_grammar import SongGrammarConfig, WhaleSongGrammar, plan_sha256  # noqa: E402

DEFAULT_CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def fitted_config(recommendations: dict[str, object]) -> SongGrammarConfig:
    projected = recommendations.get("projected_current_config")
    if not isinstance(projected, dict):
        raise ValueError("projected current grammar config is missing")
    return SongGrammarConfig(
        theme_count=int(projected["theme_count"]),
        phrase_repeats_min=int(projected["phrase_repeats_min"]),
        phrase_repeats_max=int(projected["phrase_repeats_max"]),
        phrase_pause_seconds=float(projected["phrase_pause_seconds"]),
    )


def model_record(config: SongGrammarConfig) -> dict[str, object]:
    representative = WhaleSongGrammar(config).generate()
    return {
        "config": dataclasses.asdict(config),
        "representative_plan_sha256": plan_sha256(representative),
        "representative_duration_seconds": representative.duration_seconds,
        "model_ensemble_seeds": list(MODEL_ENSEMBLE_SEEDS),
        "feature_aggregation": "arithmetic-mean-across-fixed-model-seeds",
        "features": model_ensemble_feature_vector(config),
    }


def seed_distance_distribution(
    config: SongGrammarConfig, reference: dict[str, object]
) -> dict[str, object]:
    distances = []
    for seed in MODEL_ENSEMBLE_SEEDS:
        seeded = dataclasses.replace(config, seed=seed)
        session = WhaleSongGrammar(seeded).generate()
        distance = structural_distance(
            grammar_feature_vector(session), reference
        )["mean_relative_absolute_error"]
        distances.append(float(distance))
    return {
        "seeds": list(MODEL_ENSEMBLE_SEEDS),
        "distances": distances,
        "summary": summarize_values(distances),
    }


def build_reports(corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT) -> tuple[dict[str, object], dict[str, object]]:
    corpus = build_corpus(corpus_root)

    # Training-only derivation happens before the holdout is summarized or used.
    development = split_summary(corpus, "development")
    recommendations = training_recommendations(development)
    default = model_record(SongGrammarConfig())
    fitted = model_record(fitted_config(recommendations))

    # Only now expose the frozen later-year holdout.
    holdout = split_summary(corpus, "holdout")
    empirical = {
        "schema_version": 1,
        "kind": "humpback_whale_empirical_song_structure",
        "corpus_sha256": corpus["corpus_sha256"],
        "source_manifest_sha256": corpus["source_manifest_sha256"],
        "development": development,
        "holdout": holdout,
        "recommendations": recommendations,
        "truth_levels": corpus["truth_levels"],
        "does_not_establish": [
            "per-unit timestamp boundaries",
            "transition-specific pause timing",
            "cycle-boundary pause timing",
            "a production default change",
            "perceptual realism",
        ],
    }
    empirical["empirical_sha256"] = sha256_json(empirical)

    default_dev = structural_distance(default["features"], development["feature_vector"])
    default_holdout = structural_distance(default["features"], holdout["feature_vector"])
    fitted_dev = structural_distance(fitted["features"], development["feature_vector"])
    fitted_holdout = structural_distance(fitted["features"], holdout["feature_vector"])
    default_holdout_by_seed = seed_distance_distribution(
        SongGrammarConfig(), holdout["feature_vector"]
    )
    fitted_config_value = fitted_config(recommendations)
    fitted_holdout_by_seed = seed_distance_distribution(
        fitted_config_value, holdout["feature_vector"]
    )
    seed_wins = sum(
        fitted_value < default_value
        for fitted_value, default_value in zip(
            fitted_holdout_by_seed["distances"],
            default_holdout_by_seed["distances"],
            strict=True,
        )
    )
    holdout_delta = round(
        float(fitted_holdout["mean_relative_absolute_error"])
        - float(default_holdout["mean_relative_absolute_error"]),
        6,
    )
    evaluation = {
        "schema_version": 1,
        "kind": "humpback_whale_song_grammar_structural_evaluation",
        "corpus_sha256": corpus["corpus_sha256"],
        "empirical_sha256": empirical["empirical_sha256"],
        "split_contract": {
            "fit": "2012-2016 development only",
            "evaluation": "2017-2019 frozen later-year holdout",
            "holdout_used_for_selection": False,
        },
        "default": {
            **default,
            "development_distance": default_dev,
            "holdout_distance": default_holdout,
        },
        "development_fitted_projection": {
            **fitted,
            "selection": recommendations,
            "development_distance": fitted_dev,
            "holdout_distance": fitted_holdout,
        },
        "holdout_result": {
            "metric": "mean_relative_absolute_error_across_seven_structural_features",
            "model_feature_aggregation": "arithmetic mean across eight fixed data-independent PRNG seeds",
            "lower_is_better": True,
            "default": default_holdout["mean_relative_absolute_error"],
            "development_fitted": fitted_holdout["mean_relative_absolute_error"],
            "delta_fitted_minus_default": holdout_delta,
            "fitted_improves_holdout": holdout_delta < 0,
        },
        "seed_robustness": {
            "default_holdout_distance": default_holdout_by_seed,
            "development_fitted_holdout_distance": fitted_holdout_by_seed,
            "fitted_beats_default_seed_count": seed_wins,
            "seed_count": len(MODEL_ENSEMBLE_SEEDS),
        },
        "interpretation": {
            "supported_if_improved": "The development-derived projection generalizes better on the frozen later years for this structural diagnostic.",
            "not_supported_if_not_improved": "Do not promote the development-derived projection; the later-year holdout did not improve.",
        },
        "does_not_establish": [
            "causality",
            "acoustic timbre realism",
            "human perceptual preference",
            "population-wide biological optimality",
            "permission to change the live or default grammar",
        ],
    }
    evaluation["evaluation_sha256"] = sha256_json(evaluation)
    return empirical, evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit only on 2012-2016 and evaluate song structure on frozen 2017-2019."
    )
    parser.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--empirical-output", type=pathlib.Path)
    parser.add_argument("--evaluation-output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    empirical, evaluation = build_reports(args.corpus_root)
    if args.empirical_output is not None:
        write_atomic(validated_output_path(args.empirical_output), empirical)
    if args.evaluation_output is not None:
        write_atomic(validated_output_path(args.evaluation_output), evaluation)
    if args.empirical_output is None and args.evaluation_output is None:
        print(json.dumps(evaluation, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
