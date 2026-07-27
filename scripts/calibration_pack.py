#!/usr/bin/env python3
"""Create bounded non-playing calibration packs with source-bound manifests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import shutil
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "scripts" / "reference_signal.py"
SPEC = importlib.util.spec_from_file_location("reference_signal", REFERENCE_PATH)
REFERENCE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(REFERENCE)

PACKS: dict[str, dict[str, Any]] = {
    "headphone-reference": {
        "signal": {"kind": "tone", "dbfs": -20.0, "duration": 5.0, "frequency": 1000.0},
        "gates": [
            "Lake People volume at minimum before playback",
            "Only one analog control raised at a time",
            "Stop immediately on unexpected loudness",
        ],
        "records": [
            "motu_output_to_lake_people",
            "lake_people_gain_setting",
            "lake_people_volume_reference",
            "focal_connected_output",
        ],
    },
    "voice-gain": {
        "signal": None,
        "gates": [
            "Monitoring lowered",
            "Input gain low",
            "XLR connection visually checked",
            "48 V visually checked on",
            "Loudest realistic voice used",
        ],
        "records": [
            "rode_nt1a_connected",
            "rode_nt1a_motu_input",
            "motu_phantom_48v",
            "motu_input_gain_reference",
        ],
    },
    "receiver-reference": {
        "signal": {"kind": "tone", "dbfs": -20.0, "duration": 5.0, "frequency": 1000.0},
        "gates": [
            "Receiver volume at minimum before playback",
            "Stereo mode used until multichannel path is proven",
            "Stop immediately on unexpected routing",
        ],
        "records": [
            "pioneer_pc_connection",
            "pioneer_selected_input",
            "pioneer_listening_mode",
            "pioneer_reference_volume",
        ],
    },
    "motu-loopback": {
        "signal": {"kind": "impulse", "dbfs": -20.0, "duration": 1.0, "frequency": 1000.0},
        "gates": [
            "Headphones and receiver muted or lowered",
            "Confirmed line output connected to a line input",
            "48 V off on the loopback input",
            "No microphone connected to the loopback input",
        ],
        "records": ["physical loopback cable and input/output pair in the measurement receipt"],
    },
}


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(encoded)
        temp = pathlib.Path(handle.name)
    temp.replace(path)


def create_pack(name: str, output: pathlib.Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent)
    )
    try:
        spec = PACKS[name]
        artifacts: list[dict[str, object]] = []
        signal = spec["signal"]
        if signal:
            wav = staging / f"{name}.wav"
            samples = REFERENCE.generate_samples(
                signal["kind"],
                48000,
                signal["duration"],
                signal["dbfs"],
                signal["frequency"],
            )
            REFERENCE.write_wav(wav, samples, 48000)
            artifacts.append(
                {
                    "path": wav.name,
                    "sha256": sha256(wav),
                    "bytes": wav.stat().st_size,
                }
            )
        manifest = {
            "schema_version": 1,
            "kind": "audio_calibration_pack",
            "pack": name,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "automatic_playback": False,
            "signal": signal,
            "safety_gates": spec["gates"],
            "facts_or_receipts_to_record": spec["records"],
            "generator": {
                "calibration_pack_sha256": sha256(pathlib.Path(__file__)),
                "reference_signal_sha256": sha256(REFERENCE_PATH),
            },
            "artifacts": artifacts,
            "does_not_establish": [
                "gate-completion",
                "safe-listening-level",
                "physical-cable-correctness",
                "measurement-result",
            ],
        }
        manifest_path = staging / "manifest.v1.json"
        atomic_json(manifest_path, manifest)
        result = dict(manifest)
        result["manifest"] = {
            "path": manifest_path.name,
            "sha256": sha256(manifest_path),
            "bytes": manifest_path.stat().st_size,
        }
        staging.replace(output)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", choices=sorted(PACKS))
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(create_pack(args.pack, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
