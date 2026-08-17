#!/usr/bin/env python3
"""Build the revision-bound Ecuador humpback-song structural corpus."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_morph_bank import validated_output_path, write_atomic  # noqa: E402
from whale_song_corpus import build_corpus, split_summary  # noqa: E402

DEFAULT_CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


def build_report(corpus_root: pathlib.Path = DEFAULT_CORPUS_ROOT) -> dict[str, object]:
    corpus = build_corpus(corpus_root)
    return {
        "schema_version": 1,
        "kind": "humpback_whale_song_corpus_build",
        "corpus": corpus,
        "development": split_summary(corpus, "development"),
        "holdout": split_summary(corpus, "holdout"),
        "does_not_establish": [
            "per-unit timestamp boundaries",
            "a production grammar default change",
            "perceptual realism",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize the committed CC-BY Raven phrase tables into one deterministic corpus."
    )
    parser.add_argument("--corpus-root", type=pathlib.Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(args.corpus_root)
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        output = validated_output_path(args.output)
        write_atomic(output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
