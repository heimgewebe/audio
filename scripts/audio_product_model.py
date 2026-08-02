#!/usr/bin/env python3
"""Validate the versioned Audiozentrale v2 product and workspace contracts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import stat
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "inventory/audiozentrale-product-model.v1.json"
PROFILE_CATALOG_PATH = ROOT / "profiles/audio-profiles.v1.json"
WORKSPACE_EXAMPLE_PATH = ROOT / "profiles/audiozentrale-workspace.example.v1.json"
PRODUCT_SCHEMA_PATH = ROOT / "schemas/audiozentrale-product-model.v1.schema.json"
WORKSPACE_SCHEMA_PATH = ROOT / "schemas/audiozentrale-workspace-state.v1.schema.json"
MAX_JSON_BYTES = 1_048_576
ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
FORBIDDEN_WORKSPACE_KEYS = frozenset(
    {
        "edges",
        "connections",
        "ports",
        "script",
        "scripts",
        "sidechains",
        "timeline",
        "comping",
        "clips",
        "clip_edits",
        "clip-editing",
        "free_graph",
    }
)


class ContractError(ValueError):
    """Raised when a product contract fails closed."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ContractError("value is not canonical JSON") from error


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _absolute_without_resolution(path: pathlib.Path, label: str) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise ContractError(f"{label} contains an unsafe path component")
    return expanded


def _open_regular_no_follow(path: pathlib.Path, label: str) -> tuple[pathlib.Path, int]:
    absolute = _absolute_without_resolution(path, label)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    directory = os.open("/", directory_flags)
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(absolute.name, file_flags, dir_fd=directory)
    finally:
        os.close(directory)
    return absolute, descriptor


def read_regular_bytes(
    path: pathlib.Path, label: str, *, maximum_bytes: int = MAX_JSON_BYTES
) -> bytes:
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
        raise ContractError(f"{label} maximum size is invalid")
    if maximum_bytes <= 0 or maximum_bytes > MAX_JSON_BYTES:
        raise ContractError(f"{label} maximum size is outside the contract")
    try:
        _, descriptor = _open_regular_no_follow(path, label)
    except OSError as error:
        raise ContractError(
            f"{label} cannot be opened safely: {error.strerror}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"{label} is not a regular file")
        if metadata.st_size > maximum_bytes:
            raise ContractError(f"{label} exceeds {maximum_bytes} bytes")
        data = bytearray()
        while len(data) <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum_bytes:
            raise ContractError(f"{label} exceeds {maximum_bytes} bytes")
        final_metadata = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(final_metadata, field) != getattr(metadata, field)
            for field in stable_fields
        ):
            raise ContractError(f"{label} changed while being read")
        if len(data) != metadata.st_size:
            raise ContractError(f"{label} changed while being read")
        return bytes(data)
    finally:
        os.close(descriptor)


def sha256_path(path: pathlib.Path, label: str) -> str:
    return hashlib.sha256(read_regular_bytes(path, label)).hexdigest()


def _strict_json_loads(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{label} is not valid UTF-8 JSON") from error

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ContractError(f"{label} contains non-finite JSON number {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} root must be an object")
    return value


def read_json_regular(
    path: pathlib.Path, label: str, *, maximum_bytes: int = MAX_JSON_BYTES
) -> dict[str, Any]:
    return _strict_json_loads(
        read_regular_bytes(path, label, maximum_bytes=maximum_bytes), label
    )


def require_exact_keys(value: Any, keys: Iterable[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractError(f"{label} keys differ; missing={missing}, extra={extra}")
    return value


def require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    return value


def require_id(value: Any, label: str) -> str:
    text = require_nonempty_string(value, label)
    if ID_PATTERN.fullmatch(text) is None:
        raise ContractError(f"{label} is not a stable identifier")
    return text


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} is not a SHA-256 digest")
    return value


def require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"{label} must be finite")
    return number


def require_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{label} is below its minimum")
    if maximum is not None and value > maximum:
        raise ContractError(f"{label} exceeds its maximum")
    return value


def require_id_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    if not allow_empty and not value:
        raise ContractError(f"{label} must not be empty")
    result = [require_id(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(set(result)) != len(result):
        raise ContractError(f"{label} contains duplicates")
    return result


def require_timestamp(value: Any, label: str) -> dt.datetime:
    text = require_nonempty_string(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ContractError(f"{label} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label} must include a UTC offset")
    return parsed


def require_unique_ids(items: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ContractError(f"{label} must be a list")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ContractError(f"{label}[{index}] must be an object")
        identifier = require_id(item.get("id"), f"{label}[{index}].id")
        if identifier in seen:
            raise ContractError(f"{label} contains duplicate id {identifier}")
        seen.add(identifier)
        result.append(item)
    return result


def profile_ids_from_catalog(catalog: Any) -> set[str]:
    require_exact_keys(
        catalog, {"schema_version", "kind", "profiles"}, "profile catalog"
    )
    if catalog["schema_version"] != 1 or catalog["kind"] != "audio_profile_catalog":
        raise ContractError("profile catalog contract is unsupported")
    profiles = catalog["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise ContractError("profile catalog profiles are missing")
    result: set[str] = set()
    for profile_id, profile in profiles.items():
        stable_id = require_id(profile_id, "profile id")
        if not isinstance(profile, dict):
            raise ContractError(f"profile {stable_id} must be an object")
        result.add(stable_id)
    return result


def load_profile_ids() -> set[str]:
    return profile_ids_from_catalog(
        read_json_regular(PROFILE_CATALOG_PATH, "audio profile catalog")
    )


def validate_schema_document(
    path: pathlib.Path,
    title: str,
    *,
    expected_id: str,
) -> str:
    data = read_regular_bytes(path, title)
    schema = _strict_json_loads(data, title)
    for key in ("$schema", "$id", "title", "type", "required", "properties"):
        if key not in schema:
            raise ContractError(f"{title} misses {key}")
    if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        raise ContractError(f"{title} draft is unsupported")
    if schema["$id"] != expected_id:
        raise ContractError(f"{title} id differs")
    require_nonempty_string(schema["title"], f"{title} title")
    if schema["type"] != "object" or schema.get("additionalProperties") is not False:
        raise ContractError(f"{title} root must reject additional properties")
    if not isinstance(schema["required"], list) or not isinstance(
        schema["properties"], dict
    ):
        raise ContractError(f"{title} required/properties contract is invalid")
    if any(not isinstance(item, str) for item in schema["required"]):
        raise ContractError(f"{title} required list is invalid")
    if len(set(schema["required"])) != len(schema["required"]):
        raise ContractError(f"{title} required list contains duplicates")
    if set(schema["required"]) != set(schema["properties"]):
        raise ContractError(f"{title} required/properties differ")
    return hashlib.sha256(data).hexdigest()


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in items]


def validate_parameter_contract(parameter: Any, label: str) -> dict[str, Any]:
    if not isinstance(parameter, dict):
        raise ContractError(f"{label} must be an object")
    ptype = parameter.get("type")
    common = {"id", "type", "default", "modulatable"}
    if ptype == "number":
        require_exact_keys(parameter, common | {"unit", "minimum", "maximum"}, label)
        minimum = require_number(parameter["minimum"], f"{label}.minimum")
        maximum = require_number(parameter["maximum"], f"{label}.maximum")
        default = require_number(parameter["default"], f"{label}.default")
        if minimum >= maximum or not minimum <= default <= maximum:
            raise ContractError(f"{label} numeric range/default is invalid")
        require_nonempty_string(parameter["unit"], f"{label}.unit")
    elif ptype == "enum":
        require_exact_keys(parameter, common | {"values"}, label)
        values = parameter["values"]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item for item in values)
        ):
            raise ContractError(f"{label}.values is invalid")
        if len(set(values)) != len(values) or parameter["default"] not in values:
            raise ContractError(f"{label} enum values/default is invalid")
    elif ptype == "boolean":
        require_exact_keys(parameter, common, label)
        if not isinstance(parameter["default"], bool):
            raise ContractError(f"{label}.default must be boolean")
    else:
        raise ContractError(f"{label}.type is unsupported")
    require_id(parameter["id"], f"{label}.id")
    if not isinstance(parameter["modulatable"], bool):
        raise ContractError(f"{label}.modulatable must be boolean")
    return parameter


def validate_product_model(model: Any) -> dict[str, Any]:
    model = require_exact_keys(
        model,
        {
            "schema_version",
            "kind",
            "model_id",
            "plan_binding",
            "schema_bindings",
            "profile_catalog_binding",
            "navigation",
            "contracts",
            "signal_types",
            "insertion_slots",
            "module_catalog",
            "modulation_contract",
            "setup_templates",
        },
        "product model",
    )
    if model["schema_version"] != 1 or model["kind"] != "audiozentrale_product_model":
        raise ContractError("product model schema or kind is unsupported")
    if model["model_id"] != "audiozentrale-v2":
        raise ContractError("product model id is unsupported")

    plan = require_exact_keys(
        model["plan_binding"],
        {"repository", "path", "commit", "document_sha256"},
        "plan binding",
    )
    expected_plan = {
        "repository": "heimgewebe/audio",
        "path": "docs/plans/audiozentrale-product-v2.md",
        "commit": "25d790ff2fee589f725b8f96a3fd1e9d8ec34dfc",
        "document_sha256": "61979e0e4182291b460274153bee4ce2c770225a5d6c7940ee52bdc4785e47ae",
    }
    if plan != expected_plan:
        raise ContractError("plan binding differs from the approved product plan")
    plan_path = ROOT / plan["path"]
    if sha256_path(plan_path, "bound plan document") != plan["document_sha256"]:
        raise ContractError("bound plan document digest differs")

    bindings = require_exact_keys(
        model["schema_bindings"],
        {"product_model", "workspace_state"},
        "schema bindings",
    )
    schema_specs = {
        "product_model": (
            PRODUCT_SCHEMA_PATH,
            "product model JSON schema",
            "https://heimgewebe.local/schemas/audiozentrale-product-model.v1.schema.json",
        ),
        "workspace_state": (
            WORKSPACE_SCHEMA_PATH,
            "workspace JSON schema",
            "https://heimgewebe.local/schemas/audiozentrale-workspace-state.v1.schema.json",
        ),
    }
    for binding_id, (schema_path, title, expected_id) in schema_specs.items():
        binding = require_exact_keys(
            bindings[binding_id], {"path", "sha256"}, f"{binding_id} schema binding"
        )
        expected_path = schema_path.relative_to(ROOT).as_posix()
        if binding["path"] != expected_path:
            raise ContractError(f"{binding_id} schema path differs")
        expected_sha = require_sha256(binding["sha256"], f"{binding_id} schema digest")
        observed_sha = validate_schema_document(
            schema_path, title, expected_id=expected_id
        )
        if observed_sha != expected_sha:
            raise ContractError(f"{binding_id} schema digest differs")

    profile_binding = require_exact_keys(
        model["profile_catalog_binding"],
        {"path", "sha256"},
        "profile catalog binding",
    )
    expected_profile_path = PROFILE_CATALOG_PATH.relative_to(ROOT).as_posix()
    if profile_binding["path"] != expected_profile_path:
        raise ContractError("profile catalog path differs")
    expected_profile_sha = require_sha256(
        profile_binding["sha256"], "profile catalog digest"
    )
    profile_catalog_bytes = read_regular_bytes(
        PROFILE_CATALOG_PATH, "audio profile catalog"
    )
    if hashlib.sha256(profile_catalog_bytes).hexdigest() != expected_profile_sha:
        raise ContractError("profile catalog digest differs")
    profile_ids = profile_ids_from_catalog(
        _strict_json_loads(profile_catalog_bytes, "audio profile catalog")
    )

    navigation = require_exact_keys(
        model["navigation"],
        {"places", "truth_layers", "display_depths", "legacy_area_migration"},
        "navigation",
    )
    places = require_unique_ids(navigation["places"], "navigation.places")
    if _ids(places) != ["now", "setups", "library", "system"]:
        raise ContractError("product places differ from the v2 contract")
    for index, place in enumerate(places):
        require_exact_keys(place, {"id", "label", "purpose"}, f"place[{index}]")
        require_nonempty_string(place["label"], f"place[{index}].label")
        require_nonempty_string(place["purpose"], f"place[{index}].purpose")

    truths = require_unique_ids(navigation["truth_layers"], "navigation.truth_layers")
    if _ids(truths) != ["observed", "configured", "physical-open", "executable"]:
        raise ContractError("truth layer order or inventory differs")
    for index, layer in enumerate(truths):
        require_exact_keys(layer, {"id", "label", "authority"}, f"truth layer[{index}]")
        require_nonempty_string(layer["authority"], f"truth layer[{index}].authority")

    depths = require_unique_ids(
        navigation["display_depths"], "navigation.display_depths"
    )
    if _ids(depths) != ["compact", "expanded", "focus"]:
        raise ContractError("display depth order or inventory differs")
    expected_depth_limits = {"compact": 2, "expanded": 8, "focus": 1}
    for index, depth in enumerate(depths):
        keys = {"id", "label", "maximum_primary_controls"}
        limit_field = "maximum_primary_controls"
        if depth["id"] == "focus":
            keys = {"id", "label", "maximum_simultaneous_focus"}
            limit_field = "maximum_simultaneous_focus"
        require_exact_keys(depth, keys, f"display depth[{index}]")
        require_nonempty_string(depth["label"], f"display depth[{index}].label")
        limit = require_integer(
            depth[limit_field], f"display depth[{index}].{limit_field}", minimum=1
        )
        if limit != expected_depth_limits[depth["id"]]:
            raise ContractError(f"display depth[{index}] limit differs")

    migrations = navigation["legacy_area_migration"]
    if not isinstance(migrations, list):
        raise ContractError("legacy migration must be a list")
    legacy_expected = ["start", "hoeren", "spielen", "aufnehmen", "system"]
    if [
        item.get("source") for item in migrations if isinstance(item, dict)
    ] != legacy_expected:
        raise ContractError(
            "legacy migration does not cover the shipped five areas exactly"
        )
    place_ids = set(_ids(places))
    for index, migration in enumerate(migrations):
        migration = require_exact_keys(
            migration, {"source", "destinations", "primary"}, f"migration[{index}]"
        )
        destinations = require_id_list(
            migration["destinations"],
            f"migration[{index}] destinations",
            allow_empty=False,
        )
        if any(item not in place_ids for item in destinations):
            raise ContractError(f"migration[{index}] destination is unknown")
        primary = require_id(migration["primary"], f"migration[{index}] primary")
        if primary not in destinations:
            raise ContractError(f"migration[{index}] primary is not a destination")

    contracts = require_exact_keys(
        model["contracts"],
        {
            "maximum_active_setups",
            "module_topology",
            "internal_modules_only_through_commercial_v1",
            "free_audio_or_midi_ports",
            "arbitrary_sidechains",
            "user_scripts",
            "routing_cycles",
            "takes_immutable",
            "timeline",
            "comping",
            "clip_editing",
            "cloud_required",
            "account_required",
            "ai_or_ml_required",
        },
        "product contracts",
    )
    maximum_active_setups = require_integer(
        contracts["maximum_active_setups"],
        "maximum active setups",
        minimum=1,
        maximum=1,
    )
    if (
        maximum_active_setups != 1
        or contracts["module_topology"] != "linear-typed-chain"
    ):
        raise ContractError("active setup or topology contract differs")
    required_true = {"internal_modules_only_through_commercial_v1", "takes_immutable"}
    required_false = (
        set(contracts) - {"maximum_active_setups", "module_topology"} - required_true
    )
    if any(contracts[key] is not True for key in required_true):
        raise ContractError("required positive product contract is false")
    if any(contracts[key] is not False for key in required_false):
        raise ContractError("excluded product scope is enabled")

    signal_types = require_id_list(
        model["signal_types"], "signal types", allow_empty=False
    )
    if signal_types != ["audio-mono", "audio-stereo", "midi"]:
        raise ContractError("signal type inventory or order differs")
    signal_type_set = set(signal_types)

    slots = model["insertion_slots"]
    if (
        not isinstance(slots, list)
        or not slots
        or any(require_id(item, "insertion slot") != item for item in slots)
    ):
        raise ContractError("insertion slots are invalid")
    if len(set(slots)) != len(slots):
        raise ContractError("insertion slots are duplicated")

    modules = require_unique_ids(model["module_catalog"], "module catalog")
    module_map: dict[str, dict[str, Any]] = {}
    latency_classes = {"zero", "low", "buffered"}
    cpu_classes = {"low", "medium", "high"}
    failure_modes = {
        "bypass-and-report",
        "silence-and-report",
        "preserve-and-bypass",
    }
    device_loss_modes = {"silence-and-report", "stop-and-report"}
    for index, module in enumerate(modules):
        require_exact_keys(
            module,
            {
                "id",
                "label",
                "slot",
                "input_signal_types",
                "output_signal_type",
                "latency_class",
                "cpu_class",
                "failure_mode",
                "device_loss_mode",
                "modulation_source_kinds",
                "parameters",
            },
            f"module[{index}]",
        )
        if module["slot"] not in slots:
            raise ContractError(f"module[{index}] insertion slot is unknown")
        require_nonempty_string(module["label"], f"module[{index}].label")
        input_signal_types = require_id_list(
            module["input_signal_types"],
            f"module[{index}].input_signal_types",
            allow_empty=False,
        )
        if not set(input_signal_types) <= signal_type_set:
            raise ContractError(f"module[{index}] input signal type is unknown")
        output_signal_type = require_id(
            module["output_signal_type"], f"module[{index}].output_signal_type"
        )
        if output_signal_type not in signal_type_set | {"same-as-input"}:
            raise ContractError(f"module[{index}] output signal type is unknown")
        latency_class = require_id(
            module["latency_class"], f"module[{index}] latency class"
        )
        if latency_class not in latency_classes:
            raise ContractError(f"module[{index}] latency class is unsupported")
        cpu_class = require_id(module["cpu_class"], f"module[{index}] CPU class")
        if cpu_class not in cpu_classes:
            raise ContractError(f"module[{index}] CPU class is unsupported")
        failure_mode = require_id(
            module["failure_mode"], f"module[{index}] failure mode"
        )
        if failure_mode not in failure_modes:
            raise ContractError(f"module[{index}] failure mode is unsupported")
        device_loss_mode = require_id(
            module["device_loss_mode"], f"module[{index}] device-loss mode"
        )
        if device_loss_mode not in device_loss_modes:
            raise ContractError(f"module[{index}] device-loss mode is unsupported")
        module_source_kinds = require_id_list(
            module["modulation_source_kinds"],
            f"module[{index}].modulation_source_kinds",
        )
        parameters = module["parameters"]
        if not isinstance(parameters, list):
            raise ContractError(f"module[{index}].parameters must be a list")
        seen_parameters: set[str] = set()
        parameter_map: dict[str, dict[str, Any]] = {}
        for pindex, parameter in enumerate(parameters):
            validated = validate_parameter_contract(
                parameter, f"module[{index}].parameter[{pindex}]"
            )
            pid = validated["id"]
            if pid in seen_parameters:
                raise ContractError(f"module {module['id']} duplicates parameter {pid}")
            seen_parameters.add(pid)
            parameter_map[pid] = validated
        module_map[module["id"]] = {
            **module,
            "input_signal_types": input_signal_types,
            "modulation_source_kinds": module_source_kinds,
            "parameter_map": parameter_map,
        }

    modulation = require_exact_keys(
        model["modulation_contract"],
        {
            "source_kinds",
            "target_kinds",
            "forbidden_target_kinds",
            "maximum_links_per_setup",
            "maximum_absolute_depth",
            "maximum_smoothing_ms",
        },
        "modulation contract",
    )
    expected_targets = ["module-parameter", "lane-macro"]
    if modulation["target_kinds"] != expected_targets:
        raise ContractError("modulation target kinds differ")
    forbidden = {
        "recording",
        "transport",
        "output-selection",
        "master-gain",
        "panic-mute",
        "safety-action",
    }
    if set(modulation["forbidden_target_kinds"]) != forbidden:
        raise ContractError("forbidden modulation target inventory differs")
    for label in ("source_kinds", "target_kinds", "forbidden_target_kinds"):
        values = require_id_list(
            modulation[label], f"modulation {label}", allow_empty=False
        )
        modulation[label] = values
    maximum_links = require_integer(
        modulation["maximum_links_per_setup"],
        "maximum modulation links",
        minimum=1,
    )
    if maximum_links != 64:
        raise ContractError("maximum modulation links differ")
    if (
        require_number(modulation["maximum_absolute_depth"], "maximum modulation depth")
        != 1.0
    ):
        raise ContractError("maximum modulation depth differs")
    maximum_smoothing = require_integer(
        modulation["maximum_smoothing_ms"],
        "maximum modulation smoothing",
        minimum=0,
    )
    if maximum_smoothing != 10000:
        raise ContractError("maximum smoothing differs")
    allowed_source_kinds = set(modulation["source_kinds"])
    for module_id, module in module_map.items():
        if not set(module["modulation_source_kinds"]) <= allowed_source_kinds:
            raise ContractError(
                f"module {module_id} exposes an unknown modulation source kind"
            )

    templates = require_unique_ids(model["setup_templates"], "setup templates")
    for index, template in enumerate(templates):
        require_exact_keys(
            template,
            {"id", "label", "profile_refs", "feature_refs"},
            f"template[{index}]",
        )
        require_nonempty_string(template["label"], f"template[{index}].label")
        profiles = require_id_list(
            template["profile_refs"],
            f"template[{index}].profile_refs",
            allow_empty=False,
        )
        features = require_id_list(
            template["feature_refs"], f"template[{index}].feature_refs"
        )
        if any(profile not in profile_ids for profile in profiles):
            raise ContractError(f"template[{index}] references an unknown profile")
        template["profile_refs"] = profiles
        template["feature_refs"] = features

    return {
        "model": model,
        "model_sha256": sha256_json(model),
        "places": place_ids,
        "truth_layers": set(_ids(truths)),
        "display_depths": set(_ids(depths)),
        "signal_types": signal_type_set,
        "modules": module_map,
        "profile_ids": profile_ids,
        "modulation": modulation,
        "templates": {item["id"]: item for item in templates},
    }


def _reject_forbidden_keys(value: Any, path: str = "workspace") -> None:
    if isinstance(value, dict):
        hits = sorted(set(value) & FORBIDDEN_WORKSPACE_KEYS)
        if hits:
            raise ContractError(f"{path} contains forbidden free-graph/DAW keys {hits}")
        for key, child in value.items():
            _reject_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")


def _validate_truth(value: Any, contract: dict[str, Any], label: str) -> None:
    if not isinstance(value, dict) or set(value) != contract["truth_layers"]:
        raise ContractError(f"{label} must contain exactly the four truth layers")
    statuses = {
        "observed": {"observed", "absent", "unknown", "unavailable"},
        "configured": {"configured", "not-configured", "unavailable"},
        "physical-open": {"confirmed", "open", "not-required", "unavailable"},
        "executable": {"executable", "blocked", "planned", "unavailable"},
    }
    for layer, projection in value.items():
        projection = require_exact_keys(
            projection, {"status", "detail", "evidence_refs"}, f"{label}.{layer}"
        )
        status = require_id(projection["status"], f"{label}.{layer}.status")
        if status not in statuses[layer]:
            raise ContractError(f"{label}.{layer}.status is invalid")
        require_nonempty_string(projection["detail"], f"{label}.{layer}.detail")
        evidence = projection["evidence_refs"]
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item for item in evidence
        ):
            raise ContractError(f"{label}.{layer}.evidence_refs is invalid")
        if len(set(evidence)) != len(evidence):
            raise ContractError(f"{label}.{layer}.evidence_refs contains duplicates")


def _validate_parameter_value(
    value: Any, parameter: dict[str, Any], label: str
) -> None:
    if parameter["type"] == "number":
        number = require_number(value, label)
        if not float(parameter["minimum"]) <= number <= float(parameter["maximum"]):
            raise ContractError(f"{label} is outside the parameter range")
    elif parameter["type"] == "enum":
        if value not in parameter["values"]:
            raise ContractError(f"{label} is outside the enum")
    elif parameter["type"] == "boolean" and not isinstance(value, bool):
        raise ContractError(f"{label} must be boolean")


def validate_workspace(workspace: Any, contract: dict[str, Any]) -> dict[str, Any]:
    _reject_forbidden_keys(workspace)
    workspace = require_exact_keys(
        workspace,
        {
            "schema_version",
            "kind",
            "product_model_sha256",
            "workspace_id",
            "active_setup_id",
            "setups",
            "takes",
            "ui_state",
        },
        "workspace",
    )
    if (
        workspace["schema_version"] != 1
        or workspace["kind"] != "audiozentrale_workspace_state"
    ):
        raise ContractError("workspace schema or kind is unsupported")
    if workspace["product_model_sha256"] != contract["model_sha256"]:
        raise ContractError("workspace product model binding differs")
    require_id(workspace["workspace_id"], "workspace id")
    requested_active_setup_id = workspace["active_setup_id"]
    if requested_active_setup_id is not None:
        requested_active_setup_id = require_id(
            requested_active_setup_id, "active setup id"
        )

    setups = require_unique_ids(workspace["setups"], "workspace setups")
    setup_map: dict[str, dict[str, Any]] = {}
    active_ids: list[str] = []
    module_instances: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    macros: dict[tuple[str, str], dict[str, Any]] = {}
    scenes_by_setup: dict[str, set[str]] = {}
    lane_sources: dict[tuple[str, str], str] = {}
    lane_target_kinds: dict[tuple[str, str], str] = {}
    for sindex, setup in enumerate(setups):
        setup = require_exact_keys(
            setup,
            {
                "id",
                "name",
                "status",
                "template_ref",
                "profile_refs",
                "truth",
                "lanes",
                "modulation_links",
                "scenes",
                "active_scene_id",
            },
            f"setup[{sindex}]",
        )
        sid = setup["id"]
        require_nonempty_string(setup["name"], f"setup[{sindex}].name")
        status = require_id(setup["status"], f"setup[{sindex}].status")
        if status not in {"active", "draft", "template"}:
            raise ContractError(f"setup[{sindex}].status is invalid")
        if status == "active":
            active_ids.append(sid)
        template_ref = require_id(
            setup["template_ref"], f"setup[{sindex}].template_ref"
        )
        if template_ref not in contract["templates"]:
            raise ContractError(f"setup[{sindex}] template is unknown")
        profile_refs = require_id_list(
            setup["profile_refs"],
            f"setup[{sindex}].profile_refs",
            allow_empty=False,
        )
        if any(item not in contract["profile_ids"] for item in profile_refs):
            raise ContractError(f"setup[{sindex}] profile ref is unknown")
        template_profiles = set(contract["templates"][template_ref]["profile_refs"])
        if not set(profile_refs) <= template_profiles:
            raise ContractError(f"setup[{sindex}] escapes its template profile refs")
        _validate_truth(setup["truth"], contract, f"setup[{sindex}].truth")

        lanes = require_unique_ids(setup["lanes"], f"setup[{sindex}].lanes")
        if status == "active" and not lanes:
            raise ContractError(f"setup[{sindex}] active setup has no signal lane")
        for lindex, lane in enumerate(lanes):
            lane = require_exact_keys(
                lane,
                {"id", "name", "source", "target", "modules", "macros"},
                f"setup[{sindex}].lane[{lindex}]",
            )
            lid = lane["id"]
            require_nonempty_string(
                lane["name"], f"setup[{sindex}].lane[{lindex}].name"
            )
            source = require_exact_keys(
                lane["source"],
                {"kind", "ref", "label", "signal_type"},
                f"lane {lid} source",
            )
            source_kind = require_id(source["kind"], f"lane {lid} source kind")
            if source_kind not in {
                "hardware-input",
                "midi-input",
                "software-source",
                "system-playback",
            }:
                raise ContractError(f"lane {lid} source kind is unsupported")
            target = require_exact_keys(
                lane["target"],
                {"kind", "ref", "label", "signal_type"},
                f"lane {lid} target",
            )
            target_kind = require_id(target["kind"], f"lane {lid} target kind")
            if target_kind not in {
                "hardware-output",
                "software-bus",
                "recorder",
                "none",
            }:
                raise ContractError(f"lane {lid} target kind is unsupported")
            for endpoint, name in ((source, "source"), (target, "target")):
                require_nonempty_string(endpoint["ref"], f"lane {lid} {name} ref")
                require_nonempty_string(endpoint["label"], f"lane {lid} {name} label")
            source_signal_type = require_id(
                source["signal_type"], f"lane {lid} source signal type"
            )
            target_signal_type = require_id(
                target["signal_type"], f"lane {lid} target signal type"
            )
            if source_signal_type not in contract["signal_types"]:
                raise ContractError(f"lane {lid} source signal type is unsupported")
            if target_signal_type not in contract["signal_types"]:
                raise ContractError(f"lane {lid} target signal type is unsupported")
            current_signal_type = source_signal_type
            lane_sources[(sid, lid)] = source["ref"]
            lane_target_kinds[(sid, lid)] = target_kind

            instances = require_unique_ids(lane["modules"], f"lane {lid} modules")
            for mindex, instance in enumerate(instances):
                instance = require_exact_keys(
                    instance,
                    {"id", "module_id", "bypassed", "parameters"},
                    f"lane {lid} module[{mindex}]",
                )
                module_id = require_id(instance["module_id"], f"lane {lid} module id")
                if module_id not in contract["modules"]:
                    raise ContractError(
                        f"lane {lid} module is not in the internal catalog"
                    )
                if not isinstance(instance["bypassed"], bool):
                    raise ContractError(f"lane {lid} bypass flag is invalid")
                parameters = instance["parameters"]
                module_contract = contract["modules"][module_id]
                if current_signal_type not in module_contract["input_signal_types"]:
                    raise ContractError(
                        f"lane {lid} module {instance['id']} rejects signal type {current_signal_type}"
                    )
                parameter_map = module_contract["parameter_map"]
                if not isinstance(parameters, dict) or set(parameters) != set(
                    parameter_map
                ):
                    raise ContractError(
                        f"lane {lid} module parameters differ from the catalog"
                    )
                for pid, parameter in parameter_map.items():
                    _validate_parameter_value(
                        parameters[pid], parameter, f"lane {lid}.{instance['id']}.{pid}"
                    )
                key = (sid + "/" + lid, instance["id"])
                if key in module_instances:
                    raise ContractError(f"setup {sid} duplicates module instance {key}")
                module_instances[key] = (instance, module_contract)
                output_signal_type = module_contract["output_signal_type"]
                if output_signal_type != "same-as-input":
                    current_signal_type = output_signal_type

            if current_signal_type != target_signal_type:
                raise ContractError(
                    f"lane {lid} ends with signal type {current_signal_type}, expected {target_signal_type}"
                )

            lane_macros = require_unique_ids(lane["macros"], f"lane {lid} macros")
            for macro in lane_macros:
                macro = require_exact_keys(
                    macro,
                    {"id", "label", "minimum", "maximum", "value", "modulatable"},
                    f"lane {lid} macro {macro.get('id')}",
                )
                minimum = require_number(macro["minimum"], f"lane {lid} macro minimum")
                maximum = require_number(macro["maximum"], f"lane {lid} macro maximum")
                value = require_number(macro["value"], f"lane {lid} macro value")
                if minimum >= maximum or not minimum <= value <= maximum:
                    raise ContractError(f"lane {lid} macro range/value is invalid")
                if not isinstance(macro["modulatable"], bool):
                    raise ContractError(f"lane {lid} macro modulatable flag is invalid")
                macros[(sid + "/" + lid, macro["id"])] = macro

        links = require_unique_ids(
            setup["modulation_links"], f"setup[{sindex}].modulation_links"
        )
        if len(links) > contract["modulation"]["maximum_links_per_setup"]:
            raise ContractError(f"setup[{sindex}] has too many modulation links")
        for link in links:
            link = require_exact_keys(
                link,
                {"id", "source", "target", "depth", "smoothing_ms", "enabled"},
                f"modulation link {link.get('id')}",
            )
            source = require_exact_keys(
                link["source"], {"kind", "ref"}, "modulation source"
            )
            source_kind = require_id(source["kind"], "modulation source kind")
            if source_kind not in contract["modulation"]["source_kinds"]:
                raise ContractError("modulation source kind is not allowlisted")
            require_nonempty_string(source["ref"], "modulation source ref")
            target = require_exact_keys(
                link["target"],
                {"kind", "lane_id", "module_instance_id", "parameter_id", "macro_id"},
                "modulation target",
            )
            target_kind = require_id(target["kind"], "modulation target kind")
            if target_kind not in contract["modulation"]["target_kinds"]:
                raise ContractError("modulation target kind is forbidden")
            lane_key = (
                sid + "/" + require_id(target["lane_id"], "modulation target lane")
            )
            if target_kind == "module-parameter":
                if target["macro_id"] is not None:
                    raise ContractError("module target must not carry a macro id")
                instance_id = require_id(
                    target["module_instance_id"], "target module instance"
                )
                parameter_id = require_id(target["parameter_id"], "target parameter")
                instance_key = (lane_key, instance_id)
                if instance_key not in module_instances:
                    raise ContractError("modulation target module instance is unknown")
                _, module_contract = module_instances[instance_key]
                parameter = module_contract["parameter_map"].get(parameter_id)
                if parameter is None or parameter["modulatable"] is not True:
                    raise ContractError(
                        "modulation target parameter is absent or not modulatable"
                    )
            else:
                if (
                    target["module_instance_id"] is not None
                    or target["parameter_id"] is not None
                ):
                    raise ContractError("macro target must not carry module fields")
                macro_id = require_id(target["macro_id"], "target macro")
                macro = macros.get((lane_key, macro_id))
                if macro is None or macro["modulatable"] is not True:
                    raise ContractError(
                        "modulation target macro is absent or not modulatable"
                    )
            depth = require_number(link["depth"], "modulation depth")
            if abs(depth) > contract["modulation"]["maximum_absolute_depth"]:
                raise ContractError("modulation depth exceeds the contract")
            smoothing = require_integer(
                link["smoothing_ms"], "modulation smoothing", minimum=0
            )
            if smoothing > contract["modulation"]["maximum_smoothing_ms"]:
                raise ContractError("modulation smoothing is invalid")
            if not isinstance(link["enabled"], bool):
                raise ContractError("modulation enabled flag is invalid")

        scenes = require_unique_ids(setup["scenes"], f"setup[{sindex}].scenes")
        scene_ids = set(_ids(scenes))
        scenes_by_setup[sid] = scene_ids
        active_scene = setup["active_scene_id"]
        if active_scene is not None:
            active_scene = require_id(active_scene, f"setup[{sindex}] active scene id")
            if active_scene not in scene_ids:
                raise ContractError(f"setup[{sindex}] active scene is unknown")
        for scene in scenes:
            scene = require_exact_keys(
                scene,
                {"id", "name", "parameter_overrides", "macro_overrides"},
                f"scene {scene.get('id')}",
            )
            require_nonempty_string(scene["name"], f"scene {scene['id']} name")
            overrides = scene["parameter_overrides"]
            if not isinstance(overrides, list):
                raise ContractError(
                    f"scene {scene['id']} parameter overrides must be a list"
                )
            seen_override: set[tuple[str, str, str]] = set()
            for override in overrides:
                override = require_exact_keys(
                    override,
                    {"lane_id", "module_instance_id", "parameter_id", "value"},
                    f"scene {scene['id']} parameter override",
                )
                lane_key = (
                    sid + "/" + require_id(override["lane_id"], "scene override lane")
                )
                instance_id = require_id(
                    override["module_instance_id"], "scene override instance"
                )
                parameter_id = require_id(
                    override["parameter_id"], "scene override parameter"
                )
                key = (lane_key, instance_id)
                if key not in module_instances:
                    raise ContractError("scene override module instance is unknown")
                _, module_contract = module_instances[key]
                parameter = module_contract["parameter_map"].get(parameter_id)
                if parameter is None:
                    raise ContractError("scene override parameter is unknown")
                _validate_parameter_value(
                    override["value"], parameter, "scene override value"
                )
                identity = (override["lane_id"], instance_id, parameter_id)
                if identity in seen_override:
                    raise ContractError("scene duplicates a parameter override")
                seen_override.add(identity)
            macro_overrides = scene["macro_overrides"]
            if not isinstance(macro_overrides, list):
                raise ContractError(
                    f"scene {scene['id']} macro overrides must be a list"
                )
            seen_macro_override: set[tuple[str, str]] = set()
            for override in macro_overrides:
                override = require_exact_keys(
                    override, {"lane_id", "macro_id", "value"}, "scene macro override"
                )
                lane_key = (
                    sid + "/" + require_id(override["lane_id"], "scene macro lane")
                )
                macro_id = require_id(override["macro_id"], "scene macro id")
                macro = macros.get((lane_key, macro_id))
                if macro is None:
                    raise ContractError("scene macro override target is unknown")
                identity = (override["lane_id"], macro_id)
                if identity in seen_macro_override:
                    raise ContractError("scene duplicates a macro override")
                seen_macro_override.add(identity)
                value = require_number(override["value"], "scene macro override value")
                if not float(macro["minimum"]) <= value <= float(macro["maximum"]):
                    raise ContractError("scene macro override is outside the range")
        setup_map[sid] = setup

    if len(active_ids) > 1:
        raise ContractError("more than one setup is active")
    active_setup_id = requested_active_setup_id
    if active_setup_id is None:
        if active_ids:
            raise ContractError("active setup id is missing")
    elif active_ids != [active_setup_id]:
        raise ContractError("active setup id differs from the active setup")

    takes = require_unique_ids(workspace["takes"], "workspace takes")
    for tindex, take in enumerate(takes):
        take = require_exact_keys(
            take,
            {
                "id",
                "immutable",
                "setup_id",
                "scene_id",
                "source_binding",
                "format",
                "monitoring",
                "started_at",
                "ended_at",
                "file_status",
                "artifact_sha256",
            },
            f"take[{tindex}]",
        )
        if take["immutable"] is not True:
            raise ContractError(f"take[{tindex}] must be immutable")
        take_setup_id = require_id(take["setup_id"], f"take[{tindex}] setup id")
        if take_setup_id not in setup_map:
            raise ContractError(f"take[{tindex}] setup is unknown")
        take_scene_id = take["scene_id"]
        if take_scene_id is not None:
            take_scene_id = require_id(take_scene_id, f"take[{tindex}] scene id")
            if take_scene_id not in scenes_by_setup[take_setup_id]:
                raise ContractError(f"take[{tindex}] scene is unknown")
        binding = require_exact_keys(
            take["source_binding"],
            {"lane_id", "source_ref"},
            f"take[{tindex}].source_binding",
        )
        lane_id = require_id(binding["lane_id"], f"take[{tindex}] lane id")
        source_ref = require_nonempty_string(
            binding["source_ref"], f"take[{tindex}] source ref"
        )
        expected_source_ref = lane_sources.get((take_setup_id, lane_id))
        if expected_source_ref is None or source_ref != expected_source_ref:
            raise ContractError(
                f"take[{tindex}] source binding differs from its setup lane"
            )
        if lane_target_kinds.get((take_setup_id, lane_id)) != "recorder":
            raise ContractError(f"take[{tindex}] must bind to a recorder lane")
        fmt = require_exact_keys(
            take["format"],
            {"rate_hz", "channels", "sample_format"},
            f"take[{tindex}].format",
        )
        require_integer(
            fmt["rate_hz"], f"take[{tindex}] sample rate", minimum=1, maximum=384000
        )
        require_integer(
            fmt["channels"], f"take[{tindex}] channel count", minimum=1, maximum=32
        )
        sample_format = require_id(
            fmt["sample_format"], f"take[{tindex}] sample format"
        )
        if sample_format not in {"s24le", "s32le", "f32le"}:
            raise ContractError(f"take[{tindex}] sample format is unsupported")

        monitoring = require_exact_keys(
            take["monitoring"],
            {"mode", "target_ref", "evidence_refs"},
            f"take[{tindex}].monitoring",
        )
        monitoring_mode = require_id(
            monitoring["mode"], f"take[{tindex}] monitoring mode"
        )
        if monitoring_mode not in {"direct", "software", "mixed", "none", "unknown"}:
            raise ContractError(f"take[{tindex}] monitoring mode is unsupported")
        monitoring_target = monitoring["target_ref"]
        if monitoring_mode in {"direct", "software", "mixed"}:
            require_nonempty_string(
                monitoring_target, f"take[{tindex}] monitoring target"
            )
        elif monitoring_target is not None:
            raise ContractError(
                f"take[{tindex}] monitoring target must be absent for {monitoring_mode}"
            )
        monitoring_evidence = monitoring["evidence_refs"]
        if not isinstance(monitoring_evidence, list) or any(
            not isinstance(item, str) or not item for item in monitoring_evidence
        ):
            raise ContractError(f"take[{tindex}] monitoring evidence is invalid")
        if len(set(monitoring_evidence)) != len(monitoring_evidence):
            raise ContractError(
                f"take[{tindex}] monitoring evidence contains duplicates"
            )

        started_at = require_timestamp(take["started_at"], f"take[{tindex}].started_at")
        ended_at = None
        if take["ended_at"] is not None:
            ended_at = require_timestamp(take["ended_at"], f"take[{tindex}].ended_at")
            if ended_at < started_at:
                raise ContractError(f"take[{tindex}] ends before it starts")
        file_status = require_id(take["file_status"], f"take[{tindex}] file status")
        if file_status not in {"recording", "finalized", "recoverable", "failed"}:
            raise ContractError(f"take[{tindex}] file status is invalid")
        digest = take["artifact_sha256"]
        if file_status == "recording":
            if ended_at is not None or digest is not None:
                raise ContractError(
                    f"take[{tindex}] recording state must not be finalized"
                )
        elif file_status == "finalized":
            if ended_at is None:
                raise ContractError(f"take[{tindex}] finalized take has no end time")
            require_sha256(digest, f"take[{tindex}] artifact digest")
        elif digest is not None:
            require_sha256(digest, f"take[{tindex}] artifact digest")

    ui_state = require_exact_keys(
        workspace["ui_state"], {"place", "depth", "focus_id"}, "workspace ui_state"
    )
    place = require_id(ui_state["place"], "workspace UI place")
    if place not in contract["places"]:
        raise ContractError("workspace UI place is unknown")
    depth = require_id(ui_state["depth"], "workspace UI depth")
    if depth not in contract["display_depths"]:
        raise ContractError("workspace UI depth is unknown")
    if depth == "focus":
        require_nonempty_string(ui_state["focus_id"], "workspace focus id")
    elif ui_state["focus_id"] is not None:
        raise ContractError("non-focus UI depth must not carry a focus id")

    return {
        "workspace_id": workspace["workspace_id"],
        "active_setup_id": active_setup_id,
        "setup_count": len(setups),
        "take_count": len(takes),
        "workspace_sha256": sha256_json(workspace),
    }


def load_contract() -> dict[str, Any]:
    return validate_product_model(read_json_regular(MODEL_PATH, "product model"))


def check_contract() -> dict[str, Any]:
    contract = load_contract()
    workspace = read_json_regular(WORKSPACE_EXAMPLE_PATH, "workspace example")
    workspace_result = validate_workspace(workspace, contract)
    return {
        "schema_version": 1,
        "kind": "audiozentrale_product_model_check",
        "status": "ok",
        "model_id": contract["model"]["model_id"],
        "model_sha256": contract["model_sha256"],
        "places": _ids(contract["model"]["navigation"]["places"]),
        "truth_layers": _ids(contract["model"]["navigation"]["truth_layers"]),
        "display_depths": _ids(contract["model"]["navigation"]["display_depths"]),
        "module_count": len(contract["modules"]),
        "template_count": len(contract["templates"]),
        "workspace": workspace_result,
    }


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audio-product-model")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("workspace", type=pathlib.Path)
    subparsers.add_parser("model")
    args = parser.parse_args(argv)
    try:
        contract = load_contract()
        if args.command == "check":
            emit(check_contract())
        elif args.command == "validate":
            workspace = read_json_regular(args.workspace, "workspace")
            emit(
                {
                    "schema_version": 1,
                    "kind": "audiozentrale_workspace_validation",
                    "status": "ok",
                    **validate_workspace(workspace, contract),
                }
            )
        else:
            emit(
                {
                    "schema_version": 1,
                    "kind": "audiozentrale_product_model_projection",
                    "model_sha256": contract["model_sha256"],
                    "model": contract["model"],
                }
            )
    except (ContractError, OSError) as error:
        emit(
            {
                "schema_version": 1,
                "kind": "audiozentrale_product_model_error",
                "status": "error",
                "error": str(error),
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
