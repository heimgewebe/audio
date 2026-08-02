#!/usr/bin/env python3
"""Export a deterministic tabular summary from a whale external-study JSON."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import pathlib
from typing import Any

FIELDS = (
    "source_id",
    "population",
    "recording_conditions",
    "call_type",
    "variant_id",
    "similarity_score_0_to_1",
    "temporal_total_distance",
    "envelope_distance",
    "periodicity_distance",
    "spectral_tilt_distance",
    "high_band_distance",
    "harmonic_profile_distance",
    "resonance_focus_distance",
    "pulse_rate_distance",
    "pulse_strength_distance",
    "subharmonic_distance",
    "secondary_ratio_distance",
    "secondary_strength_distance",
    "peak",
    "cpu_seconds_per_audio_second",
    "raw_sha256",
    "processed_sha256",
)


def regular_payload(path: pathlib.Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"not a regular file: {path}")
    return path.read_bytes()


def build_csv(report_payload: bytes) -> bytes:
    report = json.loads(report_payload.decode("utf-8"))
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("kind")
        != "humpback_whale_organic_external_generalization_study"
        or not isinstance(report.get("clips"), list)
    ):
        raise RuntimeError("invalid external study report")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for clip in report["clips"]:
        if not isinstance(clip, dict) or not isinstance(clip.get("results"), list):
            raise RuntimeError("invalid external study clip")
        common = {
            "source_id": clip.get("source_id"),
            "population": clip.get("population"),
            "recording_conditions": clip.get("recording_conditions"),
            "call_type": clip.get("call_type"),
            "raw_sha256": clip.get("raw_sha256"),
            "processed_sha256": clip.get("processed_sha256"),
        }
        for result in clip["results"]:
            if not isinstance(result, dict):
                raise RuntimeError("invalid external study result")
            row: dict[str, Any] = {**common, **result}
            writer.writerow({field: row.get(field, "") for field in FIELDS})
    return output.getvalue().encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report_payload = regular_payload(args.report)
    csv_payload = build_csv(report_payload)
    if args.check:
        if regular_payload(args.output) != csv_payload:
            raise RuntimeError("external CSV summary is not reproducible")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(csv_payload)
    print(
        json.dumps(
            {
                "report_sha256": hashlib.sha256(report_payload).hexdigest(),
                "csv_sha256": hashlib.sha256(csv_payload).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
