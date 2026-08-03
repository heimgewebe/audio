#!/usr/bin/env python3
"""Build deterministic audio and feature evidence for Buckelwal lesson v1."""

from __future__ import annotations

import argparse
import array
import hashlib
import io
import json
import math
import pathlib
import sys
import wave
from typing import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_morph_bank import (  # noqa: E402
    read_bound_regular_bytes,
    read_pcm16_mono_bytes,
)
from evaluate_whale_f0_v2 import analyze_samples  # noqa: E402
from whale_live_engine import WhaleVoiceConfig  # noqa: E402
from whale_morph_engine import WhaleMorphVoice  # noqa: E402
from whale_organic_engine import (  # noqa: E402
    OrganicComponentConfig,
    OrganicWhaleMorphVoice,
)

SAMPLE_RATE = 48_000
DURATION_SECONDS = 3.2
ACTIVE_SECONDS = 2.55
TAIL_SECONDS = DURATION_SECONDS - ACTIVE_SECONDS
REFERENCE_PATH = (
    ROOT / "assets" / "whale-sources" / "processed" / "humpback-song-cc0-01.wav"
)
SOURCE_MANIFEST = ROOT / "assets" / "whale-sources" / "processed" / "manifest.json"
MORPH_MANIFEST = ROOT / "assets" / "whale-sources" / "morph" / "manifest.json"
MANIFEST_PATH = ROOT / "inventory" / "buckelwal-learning-lesson.v1.json"
UI_ROOT = ROOT / "ui"
TARGET_RMS = 0.055
PEAK_LIMIT = 0.30
VARIANTS = (
    ("reference", "whale-learning-reference.wav"),
    ("morph", "whale-learning-morph.wav"),
    ("envelope", "whale-learning-envelope.wav"),
    ("periodicity", "whale-learning-periodicity.wav"),
    ("articulation", "whale-learning-articulation.wav"),
)


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def rms(samples: Iterable[float]) -> float:
    total = 0.0
    count = 0
    for value in samples:
        total += value * value
        count += 1
    return math.sqrt(total / max(1, count))


def normalize(samples: list[float]) -> list[float]:
    measured = rms(value for value in samples if abs(value) >= 1.0e-5)
    gain = TARGET_RMS / measured if measured > 1.0e-12 else 1.0
    peak = max((abs(value) for value in samples), default=0.0)
    if peak * gain > PEAK_LIMIT:
        gain = PEAK_LIMIT / peak
    return [max(-PEAK_LIMIT, min(PEAK_LIMIT, value * gain)) for value in samples]


def fade_edges(samples: list[float], seconds: float = 0.04) -> list[float]:
    output = list(samples)
    count = min(round(seconds * SAMPLE_RATE), len(output) // 2)
    for index in range(count):
        amount = (index + 1) / count
        shaped = amount * amount * (3.0 - 2.0 * amount)
        output[index] *= shaped
        output[-index - 1] *= shaped
    return output


def pcm16_samples(samples: Iterable[float]) -> array.array:
    return array.array(
        "h",
        (
            int(round(max(-1.0, min(1.0, value)) * 32767.0))
            for value in samples
        ),
    )


def wav_bytes(pcm: array.array) -> bytes:
    if pcm.typecode != "h":
        raise ValueError("whale lesson PCM must use signed 16-bit samples")
    little_endian_pcm = pcm
    if sys.byteorder != "little":
        little_endian_pcm = array.array("h", pcm)
        little_endian_pcm.byteswap()
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(little_endian_pcm.tobytes())
    return stream.getvalue()


def reference_samples() -> tuple[list[float], dict[str, object]]:
    source_manifest_payload = read_bound_regular_bytes(
        SOURCE_MANIFEST, "whale lesson source manifest"
    )
    source_manifest = json.loads(source_manifest_payload.decode("utf-8"))
    record = next(
        item
        for item in source_manifest["clips"]
        if item["id"] == "humpback-song-cc0-01"
    )
    payload = read_bound_regular_bytes(REFERENCE_PATH, "whale lesson reference")
    if sha256(payload) != record["sha256"]:
        raise RuntimeError("whale lesson reference source hash mismatch")
    pcm = read_pcm16_mono_bytes(payload, str(REFERENCE_PATH))
    count = round(DURATION_SECONDS * SAMPLE_RATE)
    selected = [sample / 32768.0 for sample in pcm[:count]]
    if len(selected) != count:
        raise RuntimeError("whale lesson reference is too short")
    source = next(
        item for item in source_manifest["sources"] if item["id"] == record["source_id"]
    )
    return fade_edges(normalize(selected)), {
        "clip_id": record["id"],
        "source_id": record["source_id"],
        "file": str(REFERENCE_PATH.relative_to(ROOT)),
        "sha256": record["sha256"],
        "license": source["license"],
        "attribution": source["attribution"],
    }


def model_provenance() -> dict[str, object]:
    source_payload = read_bound_regular_bytes(
        SOURCE_MANIFEST, "whale lesson model source manifest"
    )
    morph_payload = read_bound_regular_bytes(
        MORPH_MANIFEST, "whale lesson morph manifest"
    )
    source_manifest = json.loads(source_payload.decode("utf-8"))
    morph_manifest = json.loads(morph_payload.decode("utf-8"))
    clips = {record["id"]: record for record in source_manifest["clips"]}
    sources = {record["id"]: record for record in source_manifest["sources"]}
    used_source_ids: list[str] = []
    for anchor_record in morph_manifest["anchors"]:
        source_id = clips[anchor_record["clip_id"]]["source_id"]
        if source_id not in used_source_ids:
            used_source_ids.append(source_id)
    return {
        "morph_manifest": {
            "file": str(MORPH_MANIFEST.relative_to(ROOT)),
            "sha256": sha256(morph_payload),
            "anchor_count": len(morph_manifest["anchors"]),
        },
        "source_manifest": {
            "file": str(SOURCE_MANIFEST.relative_to(ROOT)),
            "sha256": sha256(source_payload),
        },
        "sources": [
            {
                "id": sources[source_id]["id"],
                "title": sources[source_id]["title"],
                "license": sources[source_id]["license"],
                "license_url": sources[source_id]["license_url"],
                "attribution": sources[source_id]["attribution"],
                "source_page": sources[source_id]["source_page"],
            }
            for source_id in used_source_ids
        ],
    }


def render_model(enabled: frozenset[str] | None) -> list[float]:
    config = WhaleVoiceConfig(
        sample_rate=SAMPLE_RATE,
        block_frames=128,
        master_gain=0.16,
    )
    if enabled is None:
        voice = WhaleMorphVoice(config)
    else:
        voice = OrganicWhaleMorphVoice(
            config,
            component_config=OrganicComponentConfig.from_enabled(enabled),
        )
    voice.note_on(50, 66)
    output = voice.render(round(ACTIVE_SECONDS * SAMPLE_RATE))
    voice.note_off(50)
    output.extend(voice.render(round(TAIL_SECONDS * SAMPLE_RATE)))
    expected = round(DURATION_SECONDS * SAMPLE_RATE)
    if len(output) != expected:
        raise RuntimeError("whale lesson render length changed")
    return fade_edges(normalize(output))


def feature_curves(
    samples: list[float],
    pcm: array.array,
) -> tuple[dict[str, list[float]], dict[str, object]]:
    analysis = analyze_samples(
        pcm,
        source_nyquist_hz=SAMPLE_RATE / 2,
        input_scale=32768.0,
    )
    frames = analysis["frames"]
    periodicity = [round(float(frame["periodicity"]), 8) for frame in frames]
    roughness = [round(1.0 - value, 8) for value in periodicity]
    radius = round(0.09 * SAMPLE_RATE)
    envelopes: list[float] = []
    for index in range(48):
        center = round(index / 47 * (len(samples) - 1))
        start = max(0, center - radius)
        stop = min(len(samples), center + radius)
        envelopes.append(rms(samples[position] for position in range(start, stop)))
    peak = max(envelopes) or 1.0
    envelope = [round(min(1.0, value / peak), 8) for value in envelopes]
    summary = analysis["summary"]
    return {
        "envelope": envelope,
        "periodicity": periodicity,
        "roughness": roughness,
    }, {
        "voiced_fraction": summary["voiced_fraction"],
        "median_periodicity": summary["median_periodicity"],
        "boundary_hits": summary["boundary_hits"],
        "reason_counts": summary["reason_counts"],
    }


def build() -> tuple[dict[str, bytes], bytes]:
    reference, source = reference_samples()
    model_sources = model_provenance()
    rendered = {
        "reference": reference,
        "morph": render_model(None),
        "envelope": render_model(frozenset({"source_envelope"})),
        "periodicity": render_model(
            frozenset({"source_envelope", "periodicity_roughness"})
        ),
        "articulation": render_model(
            frozenset(
                {
                    "source_envelope",
                    "periodicity_roughness",
                    "articulation_states",
                }
            )
        ),
    }
    metadata = {
        "reference": {
            "title": "Echte Referenz",
            "description": (
                "Echter lizenzierter Buckelwal-Ausschnitt: auf die ersten "
                "3,2 Sekunden gekürzt, an den Rändern weich ausgeblendet und "
                "für den Hörvergleich pegelnormalisiert. Aufnahmebedingungen "
                "und Hintergrund bleiben Teil der Beobachtung."
            ),
            "listen_for": (
                "Achte auf wechselnde Ordnung, Kanten und einen nicht "
                "symmetrischen Verlauf."
            ),
            "truth_layer": "observation",
            "enabled_components": [],
        },
        "morph": {
            "title": "Stufe 1 · periodischer Morph",
            "description": (
                "Die aktuelle sichere Grundengine: ein tastengebundener, "
                "quellengestützter Zyklus ohne zusätzliche Organic-Schichten."
            ),
            "listen_for": (
                "Achte darauf, wie stabil und instrumentenartig der Rufkörper bleibt."
            ),
            "truth_layer": "model",
            "enabled_components": [],
        },
        "envelope": {
            "title": "Stufe 2 · zeitliche Hüllkurve",
            "description": (
                "Nur die aus Aufnahmen abgeleitete Lautstärkeentwicklung wird "
                "über den Morph gelegt. Das ist ein Modellversuch, kein "
                "biologischer Stimmapparat."
            ),
            "listen_for": (
                "Vergleiche Beginn, Entwicklung und Ausklang mit Stufe 1."
            ),
            "truth_layer": "model",
            "enabled_components": ["source_envelope"],
        },
        "periodicity": {
            "title": "Stufe 3 · Ordnung und Rauigkeit",
            "description": (
                "Hüllkurve plus quellgebundener Periodizitäts-/Rauigkeitsverlauf. "
                "Diese Schicht war intern am vielversprechendsten, aber nicht "
                "familienrobust genug für den Standard."
            ),
            "listen_for": (
                "Suche kurze Veränderungen zwischen geordnetem Ton und rauerer Textur."
            ),
            "truth_layer": "model",
            "enabled_components": ["source_envelope", "periodicity_roughness"],
        },
        "articulation": {
            "title": "Stufe 4 · Artikulationszustände",
            "description": (
                "Zusätzlich werden begrenzte tonale, gepulste, raue und "
                "gebrochene Modellzustände hörbar. Sie erklären eine Hypothese, "
                "nicht die echte Biomechanik."
            ),
            "listen_for": (
                "Prüfe, ob die Übergänge tierischer wirken oder bereits nach "
                "Effektgerät klingen."
            ),
            "truth_layer": "model",
            "enabled_components": [
                "source_envelope",
                "periodicity_roughness",
                "articulation_states",
            ],
        },
    }
    filename_by_id = dict(VARIANTS)
    audio_payloads: dict[str, bytes] = {}
    variants: list[dict[str, object]] = []
    for variant_id, samples in rendered.items():
        pcm = pcm16_samples(samples)
        payload = wav_bytes(pcm)
        filename = filename_by_id[variant_id]
        audio_payloads[filename] = payload
        features, summary = feature_curves(samples, pcm)
        variants.append(
            {
                "id": variant_id,
                **metadata[variant_id],
                "audio_file": filename,
                "audio_sha256": sha256(payload),
                "audio_bytes": len(payload),
                "sample_rate_hz": SAMPLE_RATE,
                "channels": 1,
                "duration_seconds": DURATION_SECONDS,
                "normalization": "active-rms-target-0.055-peak-limit-0.30",
                "features": features,
                "summary": summary,
            }
        )
    order = [item[0] for item in VARIANTS]
    variants.sort(key=lambda item: order.index(str(item["id"])))
    manifest = {
        "schema_version": 1,
        "kind": "buckelwal_learning_lesson",
        "lesson_id": "from-tone-to-whale-unit-v1",
        "title": "Vom reinen Ton zur Buckelwaleinheit",
        "question": (
            "Welche zeitlichen Merkmale machen aus einem Ton einen "
            "walähnlichen Ruf?"
        ),
        "read_only": True,
        "authoritative": False,
        "authority": "educational-model",
        "source_revision_contract": (
            "generated-from-current-bound-repository-assets"
        ),
        "truth_layers": {
            "observation": (
                "Echte, lizenzierte Aufnahme mit Aufnahmebedingungen."
            ),
            "model": (
                "Reproduzierbare technische Annäherung mit einzeln benannten "
                "Komponenten."
            ),
            "extrapolation": (
                "Die chromatische 88-Tasten-Spielbarkeit ist musikalisch und "
                "nicht biologisch."
            ),
        },
        "reference_source": source,
        "model_sources": model_sources,
        "steps": [
            {
                "id": "hear",
                "title": "1 · Hören",
                "instruction": (
                    "Höre zuerst Referenz und Morph, ohne die Kurven zu betrachten."
                ),
            },
            {
                "id": "separate",
                "title": "2 · Zerlegen",
                "instruction": (
                    "Schalte die Modellstufen nacheinander frei und achte jeweils "
                    "nur auf den benannten Unterschied."
                ),
            },
            {
                "id": "inspect",
                "title": "3 · Sehen",
                "instruction": (
                    "Vergleiche Hüllkurve, Periodizität und Rauigkeit. Die Kurven "
                    "sind Messungen, keine Bedeutungsübersetzung."
                ),
            },
            {
                "id": "judge",
                "title": "4 · Blind vergleichen",
                "instruction": (
                    "Vergleiche A und B gegen die Referenz. Das lokale Urteil wird "
                    "nicht gespeichert und ist kein wissenschaftlicher Test."
                ),
            },
        ],
        "variants": variants,
        "blind_comparison": {
            "reference_id": "reference",
            "candidate_ids": ["morph", "articulation"],
            "prompt": (
                "Welche Modellvariante wirkt im Vergleich zur Referenz weniger "
                "instrumentenartig?"
            ),
            "stores_results": False,
        },
        "does_not_establish": [
            "biological equivalence",
            "species identity",
            "semantic meaning of calls",
            "perceptual superiority without blinded participants",
            "natural realism across all 88 piano keys",
        ],
    }
    return audio_payloads, canonical_json(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    audio_payloads, manifest_payload = build()
    expected = {
        **{
            str(UI_ROOT / name): payload
            for name, payload in audio_payloads.items()
        },
        str(MANIFEST_PATH): manifest_payload,
    }
    if args.check:
        mismatches = [
            path
            for path, payload in expected.items()
            if not pathlib.Path(path).is_file()
            or pathlib.Path(path).read_bytes() != payload
        ]
        if mismatches:
            raise SystemExit(
                "Buckelwal-Lektionsartefakte sind nicht reproduzierbar: "
                + ", ".join(mismatches)
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "manifest_sha256": sha256(manifest_payload),
                    "audio_files": sorted(audio_payloads),
                },
                sort_keys=True,
            )
        )
        return 0
    for name, payload in audio_payloads.items():
        (UI_ROOT / name).write_bytes(payload)
    MANIFEST_PATH.write_bytes(manifest_payload)
    print(
        json.dumps(
            {
                "status": "written",
                "manifest": str(MANIFEST_PATH),
                "manifest_sha256": sha256(manifest_payload),
                "audio_files": sorted(audio_payloads),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
