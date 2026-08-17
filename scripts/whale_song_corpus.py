#!/usr/bin/env python3
"""Bounded, source-verified structural corpus helpers for humpback-whale song.

The Ecuador Raven tables contain human-logged phrase windows.  Their category
nomenclature exposes a two-letter phrase identity plus a raw repetition code,
but decoding that suffix into unit sequences requires author-side catalogue rules
whose public implementation is explicitly marked unfinished.  This module keeps
that suffix opaque and uses only peer-reviewed aggregate unit counts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import pathlib
import re
import statistics
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Sequence

from build_whale_morph_bank import read_bound_regular_bytes, regular_file_path

CORPUS_SCHEMA_VERSION = 1
SESSION_BREAK_SECONDS = 60.0
MAX_MANIFEST_BYTES = 256_000
MAX_ANNOTATION_BYTES = 1_000_000
MODEL_ENSEMBLE_SEEDS = tuple(
    int.from_bytes(
        hashlib.sha256(f"whale-song-model-seed-v1:{index}".encode("utf-8")).digest()[:4],
        "big",
    )
    for index in range(8)
)
CATEGORY_RE = re.compile(r"^([A-Z][a-z])([0-9]*)$")
RECORDING_RE = re.compile(r"^HS(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})-")
REQUIRED_RAVEN_COLUMNS = frozenset(
    {
        "Selection",
        "Begin File",
        "Begin Time (s)",
        "End Time (s)",
        "Low Freq (Hz)",
        "High Freq (Hz)",
        "Category",
    }
)
STRUCTURAL_FEATURES = (
    "mean_phrase_duration_seconds",
    "mean_interphrase_gap_seconds",
    "mean_phrase_type_run_length",
    "mean_theme_sequence_length",
    "mean_phrases_per_published_song",
    "mean_published_units_per_song",
    "mean_analyzed_span_per_published_song_seconds",
)


@dataclass(frozen=True)
class PhraseObservation:
    recording_id: str
    year: int
    split: str
    source_row: int
    source_selection: str
    begin_file: str
    begin_seconds: float
    end_seconds: float
    duration_seconds: float
    low_hz: float
    high_hz: float
    category: str
    phrase_type: str | None
    repetition_code: str | None
    unit_timing: str
    gap_before_seconds: float | None
    overlap_before_seconds: float
    session_index: int


@dataclass(frozen=True)
class CategoryParse:
    phrase_type: str | None
    repetition_code: str | None
    status: str


def _round(value: float) -> float:
    return round(float(value), 6)


def _finite_number(value: str, field: str, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be >= {minimum:g}")
    return result


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def md5_bytes(payload: bytes) -> str:
    # MD5 is used only to match the upstream Figshare file metadata.  Local
    # corpus identity is SHA-256 bound.
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()  # noqa: S324


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_category(category: str) -> CategoryParse:
    raw = category.strip()
    match = CATEGORY_RE.fullmatch(raw)
    if match is None:
        return CategoryParse(None, None, "unclassified")
    phrase_type, code = match.groups()
    if not code:
        return CategoryParse(phrase_type, None, "phrase-only")
    return CategoryParse(
        phrase_type,
        code,
        "repetition-code-preserved-unparsed",
    )


def load_source_manifest(corpus_root: pathlib.Path) -> dict[str, object]:
    path = regular_file_path(
        corpus_root / "source-manifest.json", "song corpus source manifest"
    )
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("song corpus source manifest exceeds the bounded size")
    payload = read_bound_regular_bytes(path, "song corpus source manifest")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("song corpus source manifest must be an object")
    if data.get("schema_version") != 1 or data.get("kind") != "humpback_whale_song_corpus_source_manifest":
        raise ValueError("song corpus source manifest identity is invalid")
    dataset = data.get("dataset")
    if not isinstance(dataset, dict) or dataset.get("license") != "CC BY 4.0":
        raise ValueError("song corpus source manifest must bind the CC BY 4.0 dataset")
    split = data.get("split")
    if not isinstance(split, dict):
        raise ValueError("song corpus split is missing")
    development = split.get("development_years")
    holdout = split.get("holdout_years")
    if development != [2012, 2013, 2014, 2015, 2016] or holdout != [2017, 2018, 2019]:
        raise ValueError("song corpus development/holdout split drifted")
    records = data.get("records")
    if not isinstance(records, list) or len(records) != 26:
        raise ValueError("song corpus must bind exactly 26 annotated recordings")
    ids = [item.get("recording_id") for item in records if isinstance(item, dict)]
    if len(ids) != len(records) or len(set(ids)) != len(ids):
        raise ValueError("song corpus recording identities are invalid")
    development_years = set(development)
    holdout_years = set(holdout)
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("song corpus record must be an object")
        year = record.get("year")
        record_split = record.get("split")
        if isinstance(year, bool) or not isinstance(year, int):
            raise ValueError("song corpus record year must be an integer")
        if year in development_years:
            expected_split = "development"
        elif year in holdout_years:
            expected_split = "holdout"
        else:
            raise ValueError(f"song corpus record year is outside the frozen split: {year}")
        if record_split != expected_split:
            raise ValueError(
                f"song corpus record split mismatches frozen year contract: {year}"
            )
    return data


def _annotation_payload(corpus_root: pathlib.Path, record: dict[str, object]) -> tuple[pathlib.Path, bytes]:
    annotation = record.get("annotation")
    if not isinstance(annotation, dict):
        raise ValueError("record annotation binding is missing")
    relative = annotation.get("file")
    if not isinstance(relative, str):
        raise ValueError("annotation file must be text")
    pure = pathlib.PurePosixPath(relative)
    if (
        pure.is_absolute()
        or len(pure.parts) != 2
        or pure.parts[0] != "raw"
        or pure.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("annotation file must be one plain file below raw/")
    expected_size = annotation.get("bytes")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or not 0 < expected_size <= MAX_ANNOTATION_BYTES
    ):
        raise ValueError(f"annotation size binding is invalid for {relative}")
    path = regular_file_path(corpus_root / pure, f"song annotation {relative}")
    if path.stat().st_size != expected_size:
        raise ValueError(f"annotation size mismatch for {relative}")
    payload = read_bound_regular_bytes(path, f"song annotation {relative}")
    if len(payload) != expected_size:
        raise ValueError(f"annotation size changed while reading {relative}")
    if md5_bytes(payload) != annotation.get("md5"):
        raise ValueError(f"annotation upstream MD5 mismatch for {relative}")
    if sha256_bytes(payload) != annotation.get("sha256"):
        raise ValueError(f"annotation SHA-256 mismatch for {relative}")
    return path, payload


def parse_recording(corpus_root: pathlib.Path, record: dict[str, object]) -> list[PhraseObservation]:
    recording_id = record.get("recording_id")
    year = record.get("year")
    split = record.get("split")
    if not isinstance(recording_id, str) or not isinstance(year, int) or split not in {"development", "holdout"}:
        raise ValueError("record identity fields are invalid")
    name_match = RECORDING_RE.match(recording_id)
    if name_match is None or 2000 + int(name_match.group("yy")) != year:
        raise ValueError(f"record year does not match recording id: {recording_id}")
    audio = record.get("audio_external")
    if not isinstance(audio, dict) or audio.get("name") != f"{recording_id}.wav":
        raise ValueError(f"audio binding is invalid for {recording_id}")
    _path, payload = _annotation_payload(corpus_root, record)
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    fields = set(reader.fieldnames or [])
    if not REQUIRED_RAVEN_COLUMNS.issubset(fields):
        missing = sorted(REQUIRED_RAVEN_COLUMNS - fields)
        raise ValueError(f"Raven table {recording_id} misses columns: {missing}")

    indexed_rows: list[tuple[float, float, int, dict[str, str]]] = []
    for source_row, row in enumerate(reader, start=2):
        begin = _finite_number(row["Begin Time (s)"], "Begin Time (s)", minimum=0.0)
        end = _finite_number(row["End Time (s)"], "End Time (s)", minimum=0.0)
        if end <= begin:
            raise ValueError(f"non-positive phrase duration at {recording_id}:{source_row}")
        indexed_rows.append((begin, end, source_row, row))
    indexed_rows.sort(key=lambda item: (item[0], item[1], item[2]))

    result: list[PhraseObservation] = []
    previous_end: float | None = None
    session_index = 0
    for begin, end, source_row, row in indexed_rows:
        raw_gap = None if previous_end is None else begin - previous_end
        if raw_gap is not None and raw_gap > SESSION_BREAK_SECONDS:
            session_index += 1
        overlap = 0.0 if raw_gap is None or raw_gap >= 0 else -raw_gap
        gap = None if raw_gap is None else max(0.0, raw_gap)
        category = (row.get("Category") or "").strip()
        parsed = parse_category(category)
        begin_file = (row.get("Begin File") or "").strip()
        if begin_file and begin_file != audio["name"]:
            raise ValueError(f"Raven Begin File mismatches audio binding at {recording_id}:{source_row}")
        result.append(
            PhraseObservation(
                recording_id=recording_id,
                year=year,
                split=str(split),
                source_row=source_row,
                source_selection=(row.get("Selection") or "").strip(),
                begin_file=begin_file,
                begin_seconds=_round(begin),
                end_seconds=_round(end),
                duration_seconds=_round(end - begin),
                low_hz=_round(_finite_number(row["Low Freq (Hz)"], "Low Freq (Hz)", minimum=0.0)),
                high_hz=_round(_finite_number(row["High Freq (Hz)"], "High Freq (Hz)", minimum=0.0)),
                category=category,
                phrase_type=parsed.phrase_type,
                repetition_code=parsed.repetition_code,
                unit_timing="unobserved",
                gap_before_seconds=None if gap is None else _round(gap),
                overlap_before_seconds=_round(overlap),
                session_index=session_index,
            )
        )
        previous_end = end
    if not result:
        raise ValueError(f"Raven table contains no selections: {recording_id}")
    return result


def run_lengths(values: Iterable[str | None]) -> list[int]:
    result: list[int] = []
    previous: str | None = None
    count = 0
    for value in values:
        if value is None:
            if count:
                result.append(count)
            previous = None
            count = 0
            continue
        if value == previous:
            count += 1
        else:
            if count:
                result.append(count)
            previous = value
            count = 1
    if count:
        result.append(count)
    return result


def summarize_values(values: Sequence[float | int]) -> dict[str, float | int | None]:
    items = sorted(float(value) for value in values)
    if not items:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None, "mean": None}

    def percentile(q: float) -> float:
        if len(items) == 1:
            return items[0]
        position = (len(items) - 1) * q
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return items[lower]
        fraction = position - lower
        return items[lower] * (1.0 - fraction) + items[upper] * fraction

    return {
        "count": len(items),
        "min": _round(items[0]),
        "p25": _round(percentile(0.25)),
        "median": _round(percentile(0.5)),
        "p75": _round(percentile(0.75)),
        "max": _round(items[-1]),
        "mean": _round(statistics.fmean(items)),
    }


def recording_summary(record: dict[str, object], phrases: Sequence[PhraseObservation]) -> dict[str, object]:
    known_types = [p.phrase_type for p in phrases if p.phrase_type is not None]
    coded = [p for p in phrases if p.repetition_code is not None]
    gaps = [
        p.gap_before_seconds
        for p in phrases
        if p.gap_before_seconds is not None
        and p.gap_before_seconds <= SESSION_BREAK_SECONDS
    ]
    songs = int(record["published_song_count"])
    if songs <= 0:
        raise ValueError("published song count must be positive")
    published_units = record.get("published_mean_units_per_song")
    if (
        isinstance(published_units, bool)
        or not isinstance(published_units, (int, float))
        or not math.isfinite(float(published_units))
        or float(published_units) <= 0
    ):
        raise ValueError("published mean units per song must be finite and positive")
    span = phrases[-1].end_seconds - phrases[0].begin_seconds
    theme_sequence = record["published_median_theme_sequence"]
    if not isinstance(theme_sequence, list) or not theme_sequence:
        raise ValueError("published median theme sequence is invalid")
    return {
        "recording_id": record["recording_id"],
        "year": record["year"],
        "split": record["split"],
        "phrase_count": len(phrases),
        "classified_phrase_count": len(known_types),
        "unclassified_phrase_count": sum(p.phrase_type is None for p in phrases),
        "repetition_coded_phrase_count": len(coded),
        "repetition_code_fraction": _round(len(coded) / len(phrases)),
        "unique_phrase_type_count": len(set(known_types)),
        "session_count_from_gt_60s_silence": max(p.session_index for p in phrases) + 1,
        "analyzed_span_seconds": _round(span),
        "published_song_count": songs,
        "song_boundary_source": "not-exposed-in-released-raven-table",
        "published_mean_units_per_song": _round(float(published_units)),
        "published_median_theme_sequence": theme_sequence,
        "published_median_theme_sequence_length": len(theme_sequence),
        "phrases_per_published_song": _round(len(phrases) / songs),
        "analyzed_span_per_published_song_seconds": _round(span / songs),
        "phrase_duration_seconds": summarize_values(
            [p.duration_seconds for p in phrases]
        ),
        "interphrase_gap_seconds": summarize_values(gaps),
        "phrase_type_run_length": summarize_values(
            run_lengths(p.phrase_type for p in phrases)
        ),
        "overlap_phrase_count": sum(p.overlap_before_seconds > 0 for p in phrases),
        "source_table_reordered": [p.source_row for p in phrases]
        != sorted(p.source_row for p in phrases),
        "source_rows_moved_by_time_sort": sum(
            p.source_row != expected_row
            for expected_row, p in enumerate(phrases, start=2)
        ),
    }


def build_corpus(corpus_root: pathlib.Path) -> dict[str, object]:
    manifest = load_source_manifest(corpus_root)
    records_out: list[dict[str, object]] = []
    for record in manifest["records"]:
        if not isinstance(record, dict):
            raise ValueError("source record must be an object")
        phrases = parse_recording(corpus_root, record)
        records_out.append(
            {
                "source": record,
                "summary": recording_summary(record, phrases),
                "phrases": [asdict(phrase) for phrase in phrases],
            }
        )
    result: dict[str, object] = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "kind": "humpback_whale_song_structural_corpus",
        "source_manifest_sha256": sha256_bytes(
            read_bound_regular_bytes(
                regular_file_path(corpus_root / "source-manifest.json", "song corpus source manifest"),
                "song corpus source manifest",
            )
        ),
        "split": manifest["split"],
        "record_count": len(records_out),
        "phrase_count": sum(len(record["phrases"]) for record in records_out),
        "records": records_out,
        "truth_levels": {
            "observed": [
                "phrase time windows",
                "frequency bounds",
                "source categories",
            ],
            "parsed_without_unit_decoding": [
                "two-letter phrase identity",
                "raw repetition-code suffix preserved verbatim",
            ],
            "published_summary": [
                "song count per recording",
                "median theme sequence per recording",
                "mean units per song per recording",
            ],
            "derived_aggregate": [
                "phrases per published song = released phrase rows / published song count",
                "analyzed span per published song = first-to-last released phrase span / published song count",
            ],
            "unknown": [
                "per-unit timestamp boundaries inside each phrase",
                "unit sequence/count per phrase from raw repetition code alone",
                "individual song boundaries inside each released Raven table",
            ],
        },
        "does_not_establish": manifest.get("does_not_establish", []),
    }
    result["corpus_sha256"] = sha256_bytes(canonical_json_bytes(result))
    return result


def _recordings_for_split(corpus: dict[str, object], split: str) -> list[dict[str, object]]:
    if split not in {"development", "holdout"}:
        raise ValueError("split must be development or holdout")
    records = corpus.get("records")
    if not isinstance(records, list):
        raise ValueError("corpus records are missing")
    selected = [item for item in records if isinstance(item, dict) and item.get("summary", {}).get("split") == split]
    if not selected:
        raise ValueError(f"corpus split is empty: {split}")
    return selected


def split_summary(corpus: dict[str, object], split: str) -> dict[str, object]:
    records = _recordings_for_split(corpus, split)
    phrases = [phrase for record in records for phrase in record["phrases"]]
    gaps = [
        p["gap_before_seconds"]
        for p in phrases
        if p["gap_before_seconds"] is not None
        and p["gap_before_seconds"] <= SESSION_BREAK_SECONDS
    ]
    runs: list[int] = []
    for record in records:
        runs.extend(run_lengths(p["phrase_type"] for p in record["phrases"]))
    summaries = [record["summary"] for record in records]
    published_song_count = sum(
        int(summary["published_song_count"]) for summary in summaries
    )
    if published_song_count <= 0:
        raise ValueError("split must contain at least one published song")
    pooled_phrases_per_song = len(phrases) / published_song_count
    pooled_units_per_song = (
        sum(
            float(summary["published_mean_units_per_song"])
            * int(summary["published_song_count"])
            for summary in summaries
        )
        / published_song_count
    )
    pooled_span_per_song = (
        sum(float(summary["analyzed_span_seconds"]) for summary in summaries)
        / published_song_count
    )
    feature_distributions = {
        "phrase_duration_seconds": summarize_values(
            [p["duration_seconds"] for p in phrases]
        ),
        "interphrase_gap_seconds": summarize_values(gaps),
        "phrase_type_run_length": summarize_values(runs),
        "theme_sequence_length": summarize_values(
            [summary["published_median_theme_sequence_length"] for summary in summaries]
        ),
    }
    recording_equal_weight_summaries = {
        "phrases_per_published_song": summarize_values(
            [summary["phrases_per_published_song"] for summary in summaries]
        ),
        "published_units_per_song": summarize_values(
            [summary["published_mean_units_per_song"] for summary in summaries]
        ),
        "analyzed_span_per_published_song_seconds": summarize_values(
            [summary["analyzed_span_per_published_song_seconds"] for summary in summaries]
        ),
    }
    return {
        "split": split,
        "record_count": len(records),
        "phrase_count": len(phrases),
        "classified_phrase_count": sum(p["phrase_type"] is not None for p in phrases),
        "repetition_coded_phrase_count": sum(
            p["repetition_code"] is not None for p in phrases
        ),
        "published_song_count": published_song_count,
        "aggregation_contract": {
            "phrase_duration_seconds": "phrase-weighted across released phrase windows",
            "interphrase_gap_seconds": "gap-weighted after excluding >60 s session breaks",
            "phrase_type_run_length": "run-weighted within each recording; individual song boundaries are unavailable",
            "theme_sequence_length": "equal-weighted across recording-level published median theme strings",
            "phrases_per_published_song": "pooled released phrase rows divided by pooled published song count",
            "published_units_per_song": "published recording means weighted by published song count",
            "analyzed_span_per_published_song_seconds": "pooled first-to-last analyzed spans divided by pooled published song count",
        },
        "feature_distributions": feature_distributions,
        "recording_equal_weight_summaries": recording_equal_weight_summaries,
        "feature_vector": {
            "mean_phrase_duration_seconds": feature_distributions["phrase_duration_seconds"]["mean"],
            "mean_interphrase_gap_seconds": feature_distributions["interphrase_gap_seconds"]["mean"],
            "mean_phrase_type_run_length": feature_distributions["phrase_type_run_length"]["mean"],
            "mean_theme_sequence_length": feature_distributions["theme_sequence_length"]["mean"],
            "mean_phrases_per_published_song": _round(pooled_phrases_per_song),
            "mean_published_units_per_song": _round(pooled_units_per_song),
            "mean_analyzed_span_per_published_song_seconds": _round(
                pooled_span_per_song
            ),
        },
    }


def training_recommendations(development: dict[str, object]) -> dict[str, object]:
    """Project empirical development structure into the current safe grammar bounds.

    This is deliberately a bounded exhaustive search over *valid* current
    SongGrammarConfig values.  The held-out years are never consulted.
    """

    from whale_song_grammar import SongGrammarConfig

    if development.get("split") != "development":
        raise ValueError("training recommendations require the development split")
    distributions = development["feature_distributions"]
    feature_vector = development["feature_vector"]
    themes = distributions["theme_sequence_length"]
    runs = distributions["phrase_type_run_length"]
    gaps = distributions["interphrase_gap_seconds"]
    raw_theme = int(round(float(themes["median"])))
    raw_min = max(1, int(math.floor(float(runs["p25"]))))
    raw_max = max(raw_min, int(math.ceil(float(runs["p75"]))))
    raw_pause = float(gaps["median"])
    projected_pause = max(0.45, min(1.19, raw_pause))
    fitted_features = (
        "mean_interphrase_gap_seconds",
        "mean_phrase_type_run_length",
        "mean_theme_sequence_length",
        "mean_phrases_per_published_song",
        "mean_published_units_per_song",
    )
    candidates: list[tuple[float, int, int, int, dict[str, float]]] = []
    for theme_count in range(2, 7):
        for repeats_min in range(2, 9):
            for repeats_max in range(repeats_min, 9):
                try:
                    config = SongGrammarConfig(
                        theme_count=theme_count,
                        phrase_repeats_min=repeats_min,
                        phrase_repeats_max=repeats_max,
                        phrase_pause_seconds=projected_pause,
                    )
                except ValueError:
                    continue
                vector = model_ensemble_feature_vector(config)
                errors = [
                    abs(float(vector[key]) - float(feature_vector[key]))
                    / max(abs(float(feature_vector[key])), 1.0e-9)
                    for key in fitted_features
                ]
                candidates.append(
                    (
                        statistics.fmean(errors),
                        theme_count,
                        repeats_min,
                        repeats_max,
                        vector,
                    )
                )
    if not candidates:
        raise ValueError("no valid current grammar configuration fits the development corpus")
    score, theme_count, repeats_min, repeats_max, fitted_vector = min(
        candidates, key=lambda item: (item[0], item[1], item[2], item[3])
    )
    return {
        "source_split": "development",
        "uses_holdout": False,
        "selection_method": "exhaustive-valid-current-grammar-grid-8-seed-ensemble",
        "candidate_count": len(candidates),
        "model_ensemble_seeds": list(MODEL_ENSEMBLE_SEEDS),
        "fit_features": list(fitted_features),
        "development_fit_mean_relative_absolute_error": _round(score),
        "observed": {
            "theme_count_median": raw_theme,
            "phrase_repeat_run_p25": raw_min,
            "phrase_repeat_run_p75": raw_max,
            "interphrase_gap_median_seconds": _round(raw_pause),
        },
        "projected_current_config": {
            "theme_count": theme_count,
            "phrase_repeats_min": repeats_min,
            "phrase_repeats_max": repeats_max,
            "phrase_pause_seconds": _round(projected_pause),
        },
        "projected_feature_vector": fitted_vector,
        "clamped_or_jointly_constrained": {
            "theme_count": theme_count != raw_theme,
            "phrase_repeats_min": repeats_min != raw_min,
            "phrase_repeats_max": repeats_max != raw_max,
            "phrase_pause_seconds": not math.isclose(projected_pause, raw_pause, abs_tol=1e-9),
            "search_space_enforces_joint_unit_budget": True,
        },
        "not_fitted": [
            "transition_pause_seconds: Raven phrase tables do not label transition gaps as a separate timing population",
            "cycle_pause_seconds: explicit cycle-boundary pause timing is not directly annotated",
            "motif pitches/timbre: structural tables do not measure synthesis pitch choices",
            "per-unit timing: phrase tables do not expose unit timestamps",
        ],
        "does_not_establish": [
            "a production default change",
            "causal improvement",
            "perceptual realism",
        ],
    }


def grammar_feature_vector(session: object) -> dict[str, float]:
    from whale_song_grammar import iter_phrases, iter_units

    phrases = list(iter_phrases(session))
    units = list(iter_units(session))
    theme_phrases = [phrase for phrase in phrases if phrase.role == "theme"]
    runs = run_lengths(phrase.family_id for phrase in theme_phrases)
    cycles = list(session.cycles)
    return {
        "mean_phrase_duration_seconds": _round(
            statistics.fmean(
                phrase.body_end_seconds - phrase.start_seconds for phrase in phrases
            )
        ),
        "mean_interphrase_gap_seconds": _round(
            statistics.fmean(phrase.boundary_pause_seconds for phrase in phrases)
        ),
        "mean_phrase_type_run_length": _round(statistics.fmean(runs)),
        "mean_theme_sequence_length": _round(
            statistics.fmean(len(cycle.themes) for cycle in cycles)
        ),
        "mean_phrases_per_published_song": _round(len(phrases) / len(cycles)),
        "mean_published_units_per_song": _round(len(units) / len(cycles)),
        "mean_analyzed_span_per_published_song_seconds": _round(
            session.duration_seconds / len(cycles)
        ),
    }


def model_ensemble_feature_vector(config: object) -> dict[str, float]:
    """Average model structure across fixed data-independent PRNG seeds."""

    from whale_song_grammar import WhaleSongGrammar

    vectors = [
        grammar_feature_vector(
            WhaleSongGrammar(replace(config, seed=seed)).generate()
        )
        for seed in MODEL_ENSEMBLE_SEEDS
    ]
    return {
        key: _round(statistics.fmean(vector[key] for vector in vectors))
        for key in vectors[0]
    }


def structural_distance(model: dict[str, float], reference: dict[str, object]) -> dict[str, object]:
    details: dict[str, object] = {}
    errors: list[float] = []
    for key in STRUCTURAL_FEATURES:
        model_value = float(model[key])
        ref_value = reference.get(key)
        if ref_value is None:
            raise ValueError(f"reference feature is missing: {key}")
        empirical = float(ref_value)
        denominator = max(abs(empirical), 1.0e-9)
        error = abs(model_value - empirical) / denominator
        errors.append(error)
        details[key] = {
            "model": _round(model_value),
            "empirical": _round(empirical),
            "relative_absolute_error": _round(error),
        }
    return {
        "feature_count": len(errors),
        "mean_relative_absolute_error": _round(statistics.fmean(errors)),
        "features": details,
        "interpretation": "lower is structurally closer; this engineering diagnostic is not a biological or perceptual realism score",
    }


def make_structure_ablation(session: object, *, seed: int) -> object:
    """Shuffle phrase blocks and remove hierarchical boundary pauses.

    Every unit keeps its note, duration, velocity, bend, pulse count and internal
    order within its source phrase.  Only macro phrase order and phrase-boundary
    timing are ablated, giving a controlled Morph-voice listening baseline.
    """

    from whale_song_grammar import (
        PhrasePlan,
        SongCyclePlan,
        SongSessionPlan,
        ThemePlan,
        iter_phrases,
    )

    phrases = list(iter_phrases(session))
    phrases.sort(
        key=lambda phrase: hashlib.sha256(f"{seed}:{phrase.phrase_id}".encode("utf-8")).digest()
    )
    cursor = 0.0
    flattened = []
    for block_index, phrase in enumerate(phrases):
        for unit_index, unit in enumerate(phrase.units):
            is_last = unit_index == len(phrase.units) - 1
            gap = 0.15 if is_last else unit.gap_seconds
            item = replace(
                unit,
                unit_id=f"ablate-p{block_index + 1}-u{unit_index + 1}",
                start_seconds=_round(cursor),
                gap_seconds=_round(gap),
            )
            flattened.append(item)
            cursor = item.end_seconds
    body_end = flattened[-1].sound_end_seconds
    flat_phrase = PhrasePlan(
        phrase_id="structure-ablation",
        family_id="flat",
        role="theme",
        variant_index=0,
        start_seconds=0.0,
        body_end_seconds=_round(body_end),
        end_seconds=_round(cursor),
        boundary_pause_seconds=0.0,
        units=tuple(flattened),
    )
    theme = ThemePlan(
        theme_id="flat",
        cycle_index=0,
        start_seconds=0.0,
        end_seconds=_round(cursor),
        phrases=(flat_phrase,),
    )
    cycle = SongCyclePlan(
        cycle_index=0,
        start_seconds=0.0,
        end_seconds=_round(cursor),
        themes=(theme,),
        transitions=(),
    )
    return SongSessionPlan(
        schema_version=session.schema_version,
        seed=seed,
        base_note=session.base_note,
        start_seconds=0.0,
        duration_seconds=_round(cursor),
        cycles=(cycle,),
    )
