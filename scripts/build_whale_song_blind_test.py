#!/usr/bin/env python3
"""Build an anonymous bounded Morph listening pair for song-structure comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

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
    make_structure_ablation,
    split_summary,
    training_recommendations,
)
from whale_song_grammar import WhaleSongGrammar, plan_sha256  # noqa: E402

DEFAULT_CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


def _anonymous_assignment(structured_sha: str, ablated_sha: str) -> dict[str, str]:
    selector = hashlib.sha256(f"{structured_sha}:{ablated_sha}:blind-v1".encode("utf-8")).digest()[0]
    if selector % 2:
        return {"A": "structured", "B": "structure_ablation"}
    return {"A": "structure_ablation", "B": "structured"}


def build_blind_pair(
    output_dir: pathlib.Path,
    *,
    corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT,
    seconds: float = 30.0,
    gain: float = 0.16,
) -> tuple[dict[str, object], dict[str, object]]:
    if not 0 < seconds <= MAX_RENDER_SECONDS:
        raise ValueError(f"seconds must be in (0, {MAX_RENDER_SECONDS:g}]")
    corpus = build_corpus(corpus_root)
    development = split_summary(corpus, "development")
    recommendations = training_recommendations(development)
    structured = WhaleSongGrammar(fitted_config(recommendations)).generate()
    ablated = make_structure_ablation(structured, seed=structured.seed ^ 0x51A7)
    structured_sha = plan_sha256(structured)
    ablated_sha = plan_sha256(ablated)
    assignment = _anonymous_assignment(structured_sha, ablated_sha)
    sessions = {"structured": structured, "structure_ablation": ablated}

    safe_output_dir = reject_symlink_components(
        output_dir, "whale song blind output directory"
    )
    safe_output_dir.mkdir(parents=True, exist_ok=True)
    safe_output_dir = reject_symlink_components(
        safe_output_dir, "whale song blind output directory"
    )
    if not safe_output_dir.is_dir():
        raise RuntimeError("whale song blind output must be a directory")
    rendered: dict[str, tuple[str, float, list[float], dict[str, object]]] = {}
    for label, condition in sorted(assignment.items()):
        session = sessions[condition]
        render_seconds = min(seconds, session.duration_seconds)
        audio, metrics = render_prefix(session, seconds=render_seconds, gain=gain)
        rendered[label] = (condition, render_seconds, audio, metrics)

    audible_rms = [float(item[3]["rms"]) for item in rendered.values() if float(item[3]["rms"]) > 0.0]
    if len(audible_rms) != len(rendered):
        raise RuntimeError("blind comparison requires two non-silent renders")
    target_rms = min(audible_rms)
    samples: dict[str, object] = {}
    for label, (_condition, render_seconds, audio, original_metrics) in sorted(rendered.items()):
        scale = target_rms / float(original_metrics["rms"])
        if not 0 < scale <= 1.0 + 1.0e-12:
            raise RuntimeError("blind comparison RMS normalization must only attenuate")
        normalized = [sample * min(scale, 1.0) for sample in audio]
        metrics = dict(original_metrics)
        metrics.update(signal_metrics(normalized))
        metrics["level_match_scale"] = round(min(scale, 1.0), 9)
        metrics["level_match_target_rms"] = target_rms
        target = validated_output_path(safe_output_dir / f"sample-{label}.wav")
        write_stereo_wav(target, normalized, 48_000)
        samples[label] = {
            "file": target.name,
            "sha256": sha256_file(target),
            "duration_seconds": render_seconds,
            "signal_metrics": metrics,
        }

    manifest: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_song_structure_blind_pair",
        "corpus_sha256": corpus["corpus_sha256"],
        "sample_seconds": seconds,
        "voice": "WhaleMorphVoice",
        "same_source_session_inventory": True,
        "bounded_excerpt_inventory_matched": False,
        "conditions_differ_in": [
            "phrase-block order",
            "hierarchical phrase/transition/cycle boundary timing",
        ],
        "conditions_do_not_change": [
            "voice implementation",
            "per-unit note",
            "per-unit duration",
            "per-unit velocity",
            "per-unit pitch bend",
            "per-unit pulse count",
            "within-source-phrase unit order",
        ],
        "samples": samples,
        "instructions": [
            "Listen without opening answer-key.json.",
            "Use level-matched playback and the same listening chain for A and B.",
            "Rate each sample independently for whale-song coherence, natural phrasing, repetitiveness-with-variation and preference.",
            "This <=30 s pair probes local phrase/theme organization; it cannot establish full song-cycle perceptual realism.",
        ],
        "does_not_establish": [
            "full-song-cycle perceptual preference",
            "biological correctness",
            "a production default change",
            "causal preference for hierarchy independent of which units fall inside the bounded excerpt",
        ],
    }
    answer_key: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_song_structure_blind_answer_key",
        "assignment": assignment,
        "structured_plan_sha256": structured_sha,
        "structure_ablation_plan_sha256": ablated_sha,
        "development_only_selection": recommendations,
    }
    write_atomic(
        validated_output_path(safe_output_dir / "blind-manifest.json"), manifest
    )
    write_atomic(
        validated_output_path(safe_output_dir / "answer-key.json"), answer_key
    )
    return manifest, answer_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render an anonymous <=30 s Morph pair: fitted structure vs macro-structure ablation."
    )
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--gain", type=float, default=0.16)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest, _answer = build_blind_pair(
        args.output_dir,
        corpus_root=args.corpus_root,
        seconds=args.seconds,
        gain=args.gain,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
