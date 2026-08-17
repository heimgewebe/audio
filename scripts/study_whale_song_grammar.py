#!/usr/bin/env python3
"""Build and optionally render a bounded humpback-whale song-grammar study.

The structural plan may span minutes.  Audio rendering is deliberately capped
at 30 seconds and uses the existing ``WhaleMorphVoice`` unchanged.  This keeps
song-form experiments separate from the live Roland instrument and from the
frozen Morph-vs-Organic acoustic model selection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from whale_live_engine import WhaleVoiceConfig, signal_metrics, write_stereo_wav  # noqa: E402
from whale_morph_engine import WhaleMorphVoice  # noqa: E402
from whale_song_grammar import (  # noqa: E402
    SongGrammarConfig,
    SongSessionPlan,
    WhaleSongGrammar,
    events_for_session,
    plan_dict,
    plan_sha256,
    structural_metrics,
)

REPORT_SCHEMA_VERSION = 1
MAX_RENDER_SECONDS = 30.0
SOURCE_BINDING_PATHS = (
    pathlib.Path("assets/whale-sources/SOURCES.json"),
    pathlib.Path("assets/whale-sources/morph/manifest.json"),
    pathlib.Path("docs/knowledge/buckelwal-stimme-und-gesang.md"),
    pathlib.Path("scripts/whale_song_grammar.py"),
)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_bindings(root: pathlib.Path = ROOT) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for relative in SOURCE_BINDING_PATHS:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required song-grammar source is unavailable: {relative}")
        bindings[relative.as_posix()] = sha256_file(path)
    return bindings


def git_identity(root: pathlib.Path = ROOT) -> dict[str, object]:
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    return {"head": head, "dirty": bool(status.strip())}


def build_report(
    session: SongSessionPlan,
    *,
    root: pathlib.Path = ROOT,
    render: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a source-bound report while keeping scientific truth levels explicit."""

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "humpback_whale_song_grammar_study",
        "git": git_identity(root),
        "source_bindings": source_bindings(root),
        "plan_sha256": plan_sha256(session),
        "structural_metrics": structural_metrics(session),
        "plan": plan_dict(session),
        "truth_levels": {
            "evidence_backed": [
                "humpback song has a nested unit-phrase-theme-song-cycle organization",
                "phrase repetitions are constrained variants rather than bit-identical copies",
                "longer pauses can mark phrase boundaries",
                "multiple temporal scales contribute to song organization",
            ],
            "engineering_hypotheses": [
                "the concrete motif pitches and durations in this study",
                "the configured phrase repeat range",
                "the amount and direction of per-repeat variation",
                "the explicit two-family transition-phrase construction",
                "the cadence used for optional terminal flourishes",
            ],
            "open_questions": [
                "whether these engineering defaults match a particular population or season",
                "whether the grammar improves human-perceived humpback realism",
                "how parameters should change after phrase/theme annotation of real songs",
            ],
        },
        "does_not_establish": [
            "a biological model of the humpback vocal apparatus",
            "a transcription of a specific whale song",
            "species classification accuracy",
            "perceptual realism",
            "fitness of the engineering parameters to a population or season",
            "a change to the live Morph, Organic, realistic or UFO modes",
        ],
        "render": render,
    }


def render_prefix(
    session: SongSessionPlan,
    *,
    seconds: float,
    gain: float = 0.16,
) -> tuple[list[float], dict[str, object]]:
    """Render only a bounded prefix through the unchanged source-derived Morph voice."""

    if not math.isfinite(seconds) or not 0 < seconds <= MAX_RENDER_SECONDS:
        raise ValueError(f"render seconds must be in (0, {MAX_RENDER_SECONDS:g}]")
    if seconds > session.duration_seconds:
        raise ValueError("render seconds exceed the generated session")
    if not math.isfinite(gain) or not 0 < gain <= 0.25:
        raise ValueError("gain must be finite and in (0, 0.25]")

    config = WhaleVoiceConfig(sample_rate=48_000, block_frames=128, master_gain=gain)
    voice = WhaleMorphVoice(config)
    events = events_for_session(session, until_seconds=seconds)
    output: list[float] = []
    cursor = 0
    total = round(seconds * config.sample_rate)
    for timestamp, event in events:
        target = min(total, round(timestamp * config.sample_rate))
        if target > cursor:
            output.extend(voice.render(target - cursor))
            cursor = target
        voice.dispatch(event)
    if cursor < total:
        output.extend(voice.render(total - cursor))
    metrics = signal_metrics(output)
    metrics.update(
        {
            "duration_seconds": seconds,
            "sample_rate_hz": config.sample_rate,
            "voice": "WhaleMorphVoice",
            "event_count": len(events),
        }
    )
    return output, metrics


def parse_int(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer, optionally 0x-prefixed") from error


def write_json(path: pathlib.Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a deterministic offline humpback-whale song-grammar study."
    )
    parser.add_argument("--seed", type=parse_int, default=0xB0A7)
    parser.add_argument("--base-note", type=int, default=45)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--themes", type=int, default=4)
    parser.add_argument("--phrase-repeats-min", type=int, default=3)
    parser.add_argument("--phrase-repeats-max", type=int, default=5)
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--render-wav", type=pathlib.Path)
    parser.add_argument("--render-seconds", type=float, default=12.0)
    parser.add_argument("--gain", type=float, default=0.16)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = SongGrammarConfig(
        seed=args.seed,
        base_note=args.base_note,
        cycles=args.cycles,
        theme_count=args.themes,
        phrase_repeats_min=args.phrase_repeats_min,
        phrase_repeats_max=args.phrase_repeats_max,
    )
    session = WhaleSongGrammar(config).generate()
    render_record: dict[str, object] | None = None
    if args.render_wav is not None:
        samples, render_record = render_prefix(
            session,
            seconds=args.render_seconds,
            gain=args.gain,
        )
        write_stereo_wav(args.render_wav, samples, 48_000)
        render_record = dict(render_record)
        render_record["wav"] = str(args.render_wav)
        render_record["wav_sha256"] = sha256_file(args.render_wav)

    report = build_report(session, render=render_record)
    if args.report is not None:
        write_json(args.report, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
