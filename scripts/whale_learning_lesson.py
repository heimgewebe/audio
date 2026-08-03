#!/usr/bin/env python3
"""Validated read-only contract for the first Buckelwal learning lesson."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "inventory" / "buckelwal-learning-lesson.v1.json"
DEFAULT_UI_ROOT = ROOT / "ui"
MAX_MANIFEST_BYTES = 512_000
MAX_AUDIO_BYTES = 1_048_576
VARIANT_IDS = (
    "reference",
    "morph",
    "envelope",
    "periodicity",
    "articulation",
)


class LessonError(RuntimeError):
    """Controlled lesson-contract error."""


def _read_regular(path: pathlib.Path, *, label: str, maximum_bytes: int) -> bytes:
    absolute = pathlib.Path(os.path.abspath(os.fspath(path)))
    for candidate in [*reversed(absolute.parents), absolute]:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as error:
            if candidate == absolute:
                raise LessonError(f"{label} fehlt.") from error
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise LessonError(f"{label} enthält einen Symlink.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as error:
        raise LessonError(f"{label} ist nicht lesbar.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise LessonError(f"{label} hat eine unzulässige Form oder Größe.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise LessonError(f"{label} überschreitet die Größenbegrenzung.")
        return payload
    finally:
        os.close(descriptor)


def _checked_text(value: Any, *, label: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LessonError(f"{label} ist ungültig.")
    return value


def _checked_curve(value: Any, *, label: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 48
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and 0.0 <= float(item) <= 1.0
            for item in value
        )
    ):
        raise LessonError(f"{label} muss 48 normierte Werte enthalten.")
    return [float(item) for item in value]


def load_lesson_contract(
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    *,
    ui_root: pathlib.Path = DEFAULT_UI_ROOT,
) -> dict[str, Any]:
    payload = _read_regular(
        manifest_path,
        label="Buckelwal-Lektionsmanifest",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LessonError(
            "Buckelwal-Lektionsmanifest enthält kein gültiges JSON."
        ) from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "buckelwal_learning_lesson"
        or value.get("lesson_id") != "from-tone-to-whale-unit-v1"
        or value.get("read_only") is not True
        or value.get("authoritative") is not False
        or value.get("authority") != "educational-model"
    ):
        raise LessonError("Buckelwal-Lektionsmanifest hat den falschen Vertrag.")

    truth_layers = value.get("truth_layers")
    if (
        not isinstance(truth_layers, dict)
        or set(truth_layers) != {"observation", "model", "extrapolation"}
        or not all(isinstance(item, str) and item for item in truth_layers.values())
    ):
        raise LessonError("Buckelwal-Lektion trennt ihre Wahrheitsebenen nicht.")

    model_sources = value.get("model_sources")
    if (
        not isinstance(model_sources, dict)
        or set(model_sources) != {"morph_manifest", "source_manifest", "sources"}
        or not isinstance(model_sources.get("sources"), list)
        or not model_sources["sources"]
    ):
        raise LessonError("Buckelwal-Lektion bindet ihre Modellquellen nicht.")
    for binding_name in ("morph_manifest", "source_manifest"):
        binding = model_sources.get(binding_name)
        if (
            not isinstance(binding, dict)
            or not isinstance(binding.get("file"), str)
            or not isinstance(binding.get("sha256"), str)
            or len(binding["sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in binding["sha256"]
            )
        ):
            raise LessonError("Buckelwal-Lektion hat ungültige Modellbindungen.")
    checked_model_sources: list[dict[str, str]] = []
    seen_model_source_ids: set[str] = set()
    for raw_source in model_sources["sources"]:
        if not isinstance(raw_source, dict):
            raise LessonError("Buckelwal-Modellquelle ist kein Objekt.")
        source_id = _checked_text(
            raw_source.get("id"), label="Modellquellen-ID", maximum=120
        )
        if source_id in seen_model_source_ids:
            raise LessonError("Buckelwal-Modellquelle ist doppelt eingetragen.")
        seen_model_source_ids.add(source_id)
        checked_model_sources.append(
            {
                field: _checked_text(
                    raw_source.get(field),
                    label=f"Modellquelle {source_id}.{field}",
                    maximum=2000,
                )
                for field in (
                    "id",
                    "title",
                    "license",
                    "license_url",
                    "attribution",
                    "source_page",
                )
            }
        )

    variants = value.get("variants")
    if not isinstance(variants, list) or len(variants) != len(VARIANT_IDS):
        raise LessonError("Buckelwal-Lektion enthält die falsche Variantenanzahl.")
    observed_ids: list[str] = []
    public_variants: list[dict[str, Any]] = []
    for raw in variants:
        if not isinstance(raw, dict):
            raise LessonError("Buckelwal-Lektionsvariante ist kein Objekt.")
        variant_id = raw.get("id")
        if variant_id not in VARIANT_IDS or variant_id in observed_ids:
            raise LessonError("Buckelwal-Lektionsvariante hat eine ungültige ID.")
        observed_ids.append(str(variant_id))
        layer = raw.get("truth_layer")
        if layer not in {"observation", "model"}:
            raise LessonError(
                "Buckelwal-Lektionsvariante hat eine ungültige Wahrheitsebene."
            )
        audio_file = raw.get("audio_file")
        if (
            not isinstance(audio_file, str)
            or pathlib.PurePosixPath(audio_file).name != audio_file
            or not audio_file.endswith(".wav")
        ):
            raise LessonError(
                "Buckelwal-Lektionsvariante hat einen unsicheren Audiopfad."
            )
        expected_sha = raw.get("audio_sha256")
        expected_bytes = raw.get("audio_bytes")
        if (
            not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha)
            or type(expected_bytes) is not int
            or not 44 <= expected_bytes <= MAX_AUDIO_BYTES
        ):
            raise LessonError(
                "Buckelwal-Lektionsvariante hat ungültige Audiobindungen."
            )
        audio_payload = _read_regular(
            ui_root / audio_file,
            label=f"Buckelwal-Hörprobe {variant_id}",
            maximum_bytes=MAX_AUDIO_BYTES,
        )
        if len(audio_payload) != expected_bytes:
            raise LessonError(
                f"Buckelwal-Hörprobe {variant_id} hat die falsche Größe."
            )
        if hashlib.sha256(audio_payload).hexdigest() != expected_sha:
            raise LessonError(
                f"Buckelwal-Hörprobe {variant_id} hat den falschen SHA-256."
            )

        features = raw.get("features")
        if not isinstance(features, dict) or set(features) != {
            "envelope",
            "periodicity",
            "roughness",
        }:
            raise LessonError(
                "Buckelwal-Lektionsvariante hat unvollständige Merkmale."
            )
        checked_features = {
            name: _checked_curve(features[name], label=f"{variant_id}.{name}")
            for name in ("envelope", "periodicity", "roughness")
        }
        for periodicity, roughness in zip(
            checked_features["periodicity"], checked_features["roughness"]
        ):
            if abs(periodicity + roughness - 1.0) > 0.000002:
                raise LessonError(
                    "Buckelwal-Lektionsvariante verletzt Periodizität/Rauigkeit."
                )

        summary = raw.get("summary")
        if (
            not isinstance(summary, dict)
            or not isinstance(summary.get("voiced_fraction"), (int, float))
            or (
                summary.get("median_periodicity") is not None
                and not isinstance(summary.get("median_periodicity"), (int, float))
            )
        ):
            raise LessonError(
                "Buckelwal-Lektionsvariante hat eine ungültige Zusammenfassung."
            )

        public_variants.append(
            {
                **raw,
                "title": _checked_text(
                    raw.get("title"), label="Variantentitel", maximum=120
                ),
                "description": _checked_text(
                    raw.get("description"), label="Variantenbeschreibung"
                ),
                "listen_for": _checked_text(
                    raw.get("listen_for"), label="Hörauftrag", maximum=500
                ),
                "features": checked_features,
                "audio_url": "/" + audio_file,
            }
        )

    if tuple(observed_ids) != VARIANT_IDS:
        raise LessonError(
            "Buckelwal-Lektionsvarianten stehen in falscher Reihenfolge."
        )

    blind = value.get("blind_comparison")
    if (
        not isinstance(blind, dict)
        or blind.get("reference_id") != "reference"
        or blind.get("candidate_ids") != ["morph", "articulation"]
        or blind.get("stores_results") is not False
    ):
        raise LessonError(
            "Buckelwal-Blindvergleich verletzt seinen lokalen Vertrag."
        )

    steps = value.get("steps")
    if (
        not isinstance(steps, list)
        or len(steps) < 3
        or not all(
            isinstance(step, dict)
            and isinstance(step.get("title"), str)
            and isinstance(step.get("instruction"), str)
            for step in steps
        )
    ):
        raise LessonError(
            "Buckelwal-Lektion enthält keine belastbare Schrittfolge."
        )

    does_not_establish = value.get("does_not_establish")
    if (
        not isinstance(does_not_establish, list)
        or not all(isinstance(item, str) and item for item in does_not_establish)
        or "biological equivalence" not in does_not_establish
    ):
        raise LessonError(
            "Buckelwal-Lektion benennt ihre Nichtbehauptungen nicht."
        )

    return {
        **value,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "model_sources": {
            **model_sources,
            "sources": checked_model_sources,
        },
        "variants": public_variants,
    }


def check() -> dict[str, Any]:
    lesson = load_lesson_contract()
    return {
        "status": "ok",
        "kind": lesson["kind"],
        "lesson_id": lesson["lesson_id"],
        "manifest_sha256": lesson["manifest_sha256"],
        "variant_ids": [variant["id"] for variant in lesson["variants"]],
        "read_only": lesson["read_only"],
        "authoritative": lesson["authoritative"],
    }


if __name__ == "__main__":
    print(json.dumps(check(), indent=2, sort_keys=True))
