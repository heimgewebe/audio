#!/usr/bin/env python3
"""Local, task-oriented control service for the audio repository."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import math
import os
import pathlib
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ui"
PROFILE_CATALOG = ROOT / "profiles" / "audio-profiles.v1.json"
WHALE_PROFILE = ROOT / "profiles" / "buckelwal-live-voice-v1.json"
WHALE_SCRIPT = ROOT / "scripts" / "whale_live.py"
DAUERSONG_SCRIPT = ROOT / "scripts" / "dauersong_live.py"
DOCTOR_SCRIPT = ROOT / "scripts" / "audio_doctor.py"
PLANNER_SCRIPT = ROOT / "scripts" / "profile_planner.py"
PROFILE_TRANSITION_SCRIPT = ROOT / "scripts" / "profile_transition.py"
REPLAY_SCRIPT = ROOT / "scripts" / "audio_telemetry_replay.py"
WHALE_LESSON_SCRIPT = ROOT / "scripts" / "whale_learning_lesson.py"
LIVE_TELEMETRY_SCRIPT = ROOT / "scripts" / "audio_live_telemetry.py"
RECORDING_SCRIPT = ROOT / "scripts" / "audio-record"
RECORDING_PRODUCT_SCRIPT = ROOT / "scripts" / "recording_product.py"
VOICE_CAPTURE_OBSERVER_SCRIPT = ROOT / "scripts" / "voice_capture_observer.py"
RATE_POLICY_OBSERVER_SCRIPT = ROOT / "scripts" / "rate_policy_observer.py"
LABORATORY_GATE_SCRIPT = ROOT / "scripts" / "laboratory_gate.py"
QOBUZ_DESKTOP_RECOVERY_SCRIPT = ROOT / "scripts" / "qobuz_desktop_recovery.py"
MOTU_CAPTURE_IDENTITY_SCRIPT = ROOT / "scripts" / "motu_capture_identity.py"
VOICE_LEVEL_ACCEPTANCE_SECONDS = 10
RECORDING_CATALOG = ROOT / "profiles" / "recording-sessions.v1.json"
REFERENCE_LEVELS = ROOT / "profiles" / "reference-levels.v1.json"
_REPLAY_SPEC = importlib.util.spec_from_file_location(
    "audio_control_telemetry_replay", REPLAY_SCRIPT
)
if _REPLAY_SPEC is None or _REPLAY_SPEC.loader is None:
    raise RuntimeError("Telemetry-Replay-Modul kann nicht geladen werden.")
TELEMETRY_REPLAY = importlib.util.module_from_spec(_REPLAY_SPEC)
sys.modules[_REPLAY_SPEC.name] = TELEMETRY_REPLAY
_REPLAY_SPEC.loader.exec_module(TELEMETRY_REPLAY)
_LESSON_SPEC = importlib.util.spec_from_file_location(
    "audio_control_whale_learning_lesson", WHALE_LESSON_SCRIPT
)
if _LESSON_SPEC is None or _LESSON_SPEC.loader is None:
    raise RuntimeError("Buckelwal-Lektionsmodul kann nicht geladen werden.")
WHALE_LESSON = importlib.util.module_from_spec(_LESSON_SPEC)
sys.modules[_LESSON_SPEC.name] = WHALE_LESSON
_LESSON_SPEC.loader.exec_module(WHALE_LESSON)
_LIVE_TELEMETRY_MODULE: Any | None = None
_LIVE_TELEMETRY_IMPORT_ERROR: BaseException | None = None
_LIVE_TELEMETRY_IMPORT_LOCK = threading.Lock()
_PROFILE_TRANSITION_MODULE: Any | None = None
_PROFILE_TRANSITION_IMPORT_ERROR: BaseException | None = None
_PROFILE_TRANSITION_IMPORT_LOCK = threading.Lock()
_VOICE_LEVEL_ACCEPTANCE_MODULES: tuple[Any, Any, Any] | None = None
_VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR: BaseException | None = None
_VOICE_LEVEL_ACCEPTANCE_IMPORT_LOCK = threading.Lock()
_RECORDING_RECOVERY_MODULES: tuple[Any, Any] | None = None
_RECORDING_RECOVERY_IMPORT_ERROR: BaseException | None = None
_RECORDING_RECOVERY_IMPORT_LOCK = threading.Lock()


def load_live_telemetry() -> Any:
    """Load optional live telemetry without making service import depend on it."""

    global _LIVE_TELEMETRY_MODULE, _LIVE_TELEMETRY_IMPORT_ERROR
    if _LIVE_TELEMETRY_MODULE is not None:
        return _LIVE_TELEMETRY_MODULE
    if _LIVE_TELEMETRY_IMPORT_ERROR is not None:
        raise RuntimeError("Live-Telemetriemodul ist nicht verfügbar.") from _LIVE_TELEMETRY_IMPORT_ERROR
    with _LIVE_TELEMETRY_IMPORT_LOCK:
        if _LIVE_TELEMETRY_MODULE is not None:
            return _LIVE_TELEMETRY_MODULE
        if _LIVE_TELEMETRY_IMPORT_ERROR is not None:
            raise RuntimeError("Live-Telemetriemodul ist nicht verfügbar.") from _LIVE_TELEMETRY_IMPORT_ERROR
        spec = importlib.util.spec_from_file_location(
            "audio_control_live_telemetry", LIVE_TELEMETRY_SCRIPT
        )
        if spec is None or spec.loader is None:
            error = RuntimeError("Live-Telemetriemodul kann nicht geladen werden.")
            _LIVE_TELEMETRY_IMPORT_ERROR = error
            raise error
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            sys.modules.pop(spec.name, None)
            _LIVE_TELEMETRY_IMPORT_ERROR = error
            raise RuntimeError("Live-Telemetriemodul ist nicht verfügbar.") from error
        _LIVE_TELEMETRY_MODULE = module
        return module


class _LazyLiveTelemetry:
    def __getattr__(self, name: str) -> Any:
        return getattr(load_live_telemetry(), name)


LIVE_TELEMETRY = _LazyLiveTelemetry()


def load_profile_transition() -> Any:
    """Load the typed desktop transition only after release binding is proven."""

    verify_profile_transition_release_binding()
    global _PROFILE_TRANSITION_MODULE, _PROFILE_TRANSITION_IMPORT_ERROR
    if _PROFILE_TRANSITION_MODULE is not None:
        return _PROFILE_TRANSITION_MODULE
    if _PROFILE_TRANSITION_IMPORT_ERROR is not None:
        raise ControlError(
            "Desktop-Transition ist nicht verfügbar."
        ) from _PROFILE_TRANSITION_IMPORT_ERROR
    with _PROFILE_TRANSITION_IMPORT_LOCK:
        if _PROFILE_TRANSITION_MODULE is not None:
            return _PROFILE_TRANSITION_MODULE
        spec = importlib.util.spec_from_file_location(
            "audio_control_profile_transition", PROFILE_TRANSITION_SCRIPT
        )
        if spec is None or spec.loader is None:
            error = RuntimeError("Profiltransition kann nicht geladen werden.")
            _PROFILE_TRANSITION_IMPORT_ERROR = error
            raise ControlError("Desktop-Transition ist nicht verfügbar.") from error
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            sys.modules.pop(spec.name, None)
            _PROFILE_TRANSITION_IMPORT_ERROR = error
            raise ControlError("Desktop-Transition ist nicht verfügbar.") from error
        _PROFILE_TRANSITION_MODULE = module
        return module


def load_voice_level_acceptance_modules() -> tuple[Any, Any, Any]:
    """Load voice-capture and laboratory modules only after an explicit action."""

    global _VOICE_LEVEL_ACCEPTANCE_MODULES, _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR
    if _VOICE_LEVEL_ACCEPTANCE_MODULES is not None:
        return _VOICE_LEVEL_ACCEPTANCE_MODULES
    if _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR is not None:
        raise ControlError("Pegelabnahme ist nicht verfügbar.") from _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR
    with _VOICE_LEVEL_ACCEPTANCE_IMPORT_LOCK:
        if _VOICE_LEVEL_ACCEPTANCE_MODULES is not None:
            return _VOICE_LEVEL_ACCEPTANCE_MODULES
        if _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR is not None:
            raise ControlError("Pegelabnahme ist nicht verfügbar.") from _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR
        loaded: list[Any] = []
        for module_name, path in (
            ("audio_control_laboratory_gate", LABORATORY_GATE_SCRIPT),
            ("audio_control_voice_capture_observer", VOICE_CAPTURE_OBSERVER_SCRIPT),
            ("audio_control_rate_policy_observer", RATE_POLICY_OBSERVER_SCRIPT),
        ):
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                error = RuntimeError(f"Modul kann nicht geladen werden: {path.name}")
                _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR = error
                raise ControlError("Pegelabnahme ist nicht verfügbar.") from error
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException as error:
                if isinstance(error, KeyboardInterrupt):
                    raise
                sys.modules.pop(spec.name, None)
                _VOICE_LEVEL_ACCEPTANCE_IMPORT_ERROR = error
                raise ControlError("Pegelabnahme ist nicht verfügbar.") from error
            loaded.append(module)
        laboratory, voice_capture, rate_policy = loaded
        _VOICE_LEVEL_ACCEPTANCE_MODULES = (voice_capture, laboratory, rate_policy)
        return _VOICE_LEVEL_ACCEPTANCE_MODULES


def load_recording_recovery_modules() -> tuple[Any, Any]:
    """Load shared MOTU recovery safety contracts only for explicit prepare."""

    global _RECORDING_RECOVERY_MODULES, _RECORDING_RECOVERY_IMPORT_ERROR
    if _RECORDING_RECOVERY_MODULES is not None:
        return _RECORDING_RECOVERY_MODULES
    if _RECORDING_RECOVERY_IMPORT_ERROR is not None:
        raise ControlError("Aufnahmepfad-Reparatur ist nicht verfügbar.") from _RECORDING_RECOVERY_IMPORT_ERROR
    with _RECORDING_RECOVERY_IMPORT_LOCK:
        if _RECORDING_RECOVERY_MODULES is not None:
            return _RECORDING_RECOVERY_MODULES
        if _RECORDING_RECOVERY_IMPORT_ERROR is not None:
            raise ControlError("Aufnahmepfad-Reparatur ist nicht verfügbar.") from _RECORDING_RECOVERY_IMPORT_ERROR
        loaded: list[Any] = []
        for module_name, path in (
            ("audio_control_qobuz_desktop_recovery", QOBUZ_DESKTOP_RECOVERY_SCRIPT),
            ("audio_control_motu_capture_identity", MOTU_CAPTURE_IDENTITY_SCRIPT),
        ):
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                error = RuntimeError(f"Modul kann nicht geladen werden: {path.name}")
                _RECORDING_RECOVERY_IMPORT_ERROR = error
                raise ControlError("Aufnahmepfad-Reparatur ist nicht verfügbar.") from error
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException as error:
                if isinstance(error, KeyboardInterrupt):
                    raise
                sys.modules.pop(spec.name, None)
                _RECORDING_RECOVERY_IMPORT_ERROR = error
                raise ControlError("Aufnahmepfad-Reparatur ist nicht verfügbar.") from error
            loaded.append(module)
        qobuz_recovery, motu_identity = loaded
        _RECORDING_RECOVERY_MODULES = (qobuz_recovery, motu_identity)
        return _RECORDING_RECOVERY_MODULES


def ensure_profile_transition_state_root() -> pathlib.Path:
    """Prepare the canonical CLI/UI transition journal before sandboxing."""

    transition = load_profile_transition()
    state_root = transition.DEFAULT_STATE_ROOT
    if (
        not isinstance(state_root, pathlib.Path)
        or not state_root.is_absolute()
        or state_root != PROFILE_TRANSITION_STATE_ROOT
    ):
        raise ControlError("Der kanonische Transition-State-Root ist ungültig.")
    try:
        operations = transition.ensure_state_root(state_root)
    except (transition.TransitionError, OSError) as error:
        raise ControlError(
            "Der kanonische Transition-State-Root konnte nicht sicher vorbereitet werden."
        ) from error
    if operations != state_root / "operations":
        raise ControlError("Der kanonische Transition-State-Root ist widersprüchlich.")
    return state_root

SPEC_BASE_REVISION = "81fab5c57a3609b8b931a2ee5251c4f576368298"
API_VERSION = "v1"
UNIT_NAME = "audio-control-ui-v1.service"
UNIT_MANAGED_BY = "audio-control-ui-v1"
QOBUZ_DESKTOP_RECOVERY_UNIT = "audio-qobuz-desktop-recovery-v1.service"
QBZD_QCONNECT_RECOVERY_UNIT = "audio-qbzd-qconnect-recovery-v1.service"
QOBUZ_RECOVERY_UNITS = (QOBUZ_DESKTOP_RECOVERY_UNIT, QBZD_QCONNECT_RECOVERY_UNIT)
RECORDING_PRECHECK_STOP_UNITS = ("audio-control-level-observer-v1.service",)
RECORDING_PATH_RECOVERY_UNITS = (
    *RECORDING_PRECHECK_STOP_UNITS,
    QOBUZ_DESKTOP_RECOVERY_UNIT,
    QBZD_QCONNECT_RECOVERY_UNIT,
)
RECORDING_AUDIO_CORE_UNITS = (
    "pipewire.service",
    "pipewire-pulse.service",
    "wireplumber.service",
)
RECORDING_SOURCE_READBACK_ATTEMPTS = 6
RECORDING_SOURCE_READBACK_INTERVAL_SECONDS = 1.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CACHE_SECONDS = 4.0
RELEASE_MARKER = ROOT / ".audio-control-release.json"
PROFILE_TRANSITION_RELEASE_BINDING_FILES = frozenset(
    {
        "scripts/profile_transition.py",
        "scripts/profile_planner.py",
        "scripts/audio_doctor.py",
        "scripts/physical_verification.py",
        "scripts/laboratory_gate.py",
        "profiles/audio-profiles.v1.json",
        "inventory/physical-facts.v1.json",
        "inventory/physical-verification.v1.json",
        "inventory/laboratory-gates.v1.json",
        "tests/test_audio_control_profile_transition_release.py",
    }
)
MAX_RELEASE_MARKER_BYTES = 1_048_576
_STATE_HOME = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", str(pathlib.Path.home() / ".local" / "state"))
).expanduser()
PROFILE_TRANSITION_STATE_ROOT = _STATE_HOME / "audio" / "profile-transitions-v1"
STATIC_RECORDING_OUTPUT_ROOT = pathlib.Path.home() / "Music" / "Audio-Aufnahmen"
STATIC_RECORDING_STATE_ROOT = (
    pathlib.Path.home() / ".local" / "state" / "audio" / "recordings-v1"
)
STATIC_PROFILE_TRANSITION_STATE_ROOT = (
    pathlib.Path.home() / ".local" / "state" / "audio" / "profile-transitions-v1"
)
STATIC_LABORATORY_STATE_ROOT = (
    pathlib.Path.home() / ".local" / "state" / "audio" / "laboratory"
)
LABORATORY_STATE_ROOT = _STATE_HOME / "audio" / "laboratory"
DEPLOY_RELEASE_ROOT = (
    pathlib.Path.home() / ".local" / "share" / "audio-control-ui" / "releases"
)
DEPLOY_STATE_ROOT = pathlib.Path.home() / ".local" / "state" / "audio-control-deploy"
DEPLOY_LATEST = DEPLOY_STATE_ROOT / "latest.json"
RECORDING_OUTPUT_ROOT = pathlib.Path(
    os.environ.get("AUDIO_RECORDING_ROOT", str(pathlib.Path.home() / "Music" / "Audio-Aufnahmen"))
).expanduser()
RECORDING_STATE_ROOT = (
    pathlib.Path(
        os.environ.get("XDG_STATE_HOME", str(pathlib.Path.home() / ".local" / "state"))
    ).expanduser()
    / "audio"
    / "recordings-v1"
)
RECORDING_SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")
RECORDING_MEDIA_PATH_RE = re.compile(
    rf"^/api/{API_VERSION}/recordings/([0-9a-f]{{24}})/(audio|midi)$"
)
MAX_DEPLOY_RECEIPT_BYTES = 1_048_576
MAX_REQUEST_BYTES = 4096
MAX_REQUEST_LINE_BYTES = 2048
MAX_HEADER_BYTES = 16_384
MAX_RANGE_HEADER_BYTES = 128
MAX_STATIC_BYTES = 1_048_576
MAX_SUBPROCESS_OUTPUT_BYTES = 1_048_576
MAX_CONCURRENT_REQUESTS = 12
REQUEST_IO_TIMEOUT_SECONDS = 5.0
MAX_RUNTIME_SECONDS = 21_600
MAX_OPERATING_MODE_STATE_BYTES = 16_384
_SYSTEMD_STATE_DIRECTORY = pathlib.Path(os.environ.get("STATE_DIRECTORY", ""))
OPERATING_MODE_STATE_PATH = (
    _SYSTEMD_STATE_DIRECTORY / "operating-mode-v1.json"
    if _SYSTEMD_STATE_DIRECTORY.is_absolute()
    else (
        pathlib.Path(
            os.environ.get(
                "XDG_STATE_HOME", str(pathlib.Path.home() / ".local" / "state")
            )
        ).expanduser()
        / "audio"
        / "operating-mode-v1.json"
    )
)
OPERATING_MODE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,79}")
OPERATING_MODE_STATES = frozenset(
    {"ready", "transitioning", "attention", "blocked", "recovering"}
)
OPERATING_MODES = {
    "desktop-listening": {
        "label": "Desktop / Spotify / Browser",
        "effect": "desktop-mixed-transition-v1",
        "actionable": True,
    },
    "qobuz-reference": {
        "label": "Qobuz High Quality",
        "effect": "bind-existing-qbzd-qconnect-v1",
        "actionable": True,
    },
    "recording": {
        "label": "Aufnahme",
        "effect": "recorder-plan-hash-and-current-readback",
        "actionable": False,
    },
    "performance": {
        "label": "Performance",
        "effect": None,
        "actionable": False,
    },
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/whale-lesson.js": ("whale-lesson.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/whale-learning-reference.wav": ("whale-learning-reference.wav", "audio/wav"),
    "/whale-learning-morph.wav": ("whale-learning-morph.wav", "audio/wav"),
    "/whale-learning-envelope.wav": ("whale-learning-envelope.wav", "audio/wav"),
    "/whale-learning-periodicity.wav": ("whale-learning-periodicity.wav", "audio/wav"),
    "/whale-learning-articulation.wav": ("whale-learning-articulation.wav", "audio/wav"),
}
ALLOWED_WHALE_MODES = frozenset({"morph", "organic", "realistic", "ufo"})
RECORDING_MODES = {
    "voice": {"session_type": "voice-recording", "label": "Nur Gesang"},
    "piano-vocal": {
        "session_type": "piano-vocal-performance",
        "label": "Klavier + Gesang",
    },
}
RECORDING_SESSION_TYPES = frozenset(
    mode["session_type"] for mode in RECORDING_MODES.values()
)
RECORDING_LIBRARY_CATEGORIES = frozenset(
    {"unsorted", "song", "practice", "idea", "test", "finished"}
)
RECORDING_LIBRARY_OPERATIONS = frozenset({"categorize", "trash", "restore"})
RECORDING_READINESS_CHECK_IDS = (
    "output",
    "physical",
    "laboratory",
    "source",
    "tools",
    "storage",
    "session",
)
RECORDING_ADVISORY_IDS = ("voice-level",)
ONSITE_WARNING_CODES = frozenset({"voice-source-not-motu"})
PROFILE_AREAS = {
    "desktop-mixed": "listening",
    "reference-listening": "listening",
    "qobuz-exclusive": "listening",
    "receiver": "listening",
    "bluetooth-convenience": "listening",
    "voice-recording": "recording",
    "piano-digital-recording": "recording",
    "production": "recording",
    "piano-software-live": "playing",
    "experimental": "sounds",
}


#: Sentinel so an explicit ``telemetry=None`` means "run without telemetry".
BUILD_DEFAULT_TELEMETRY = object()


class ControlError(RuntimeError):
    """Expected service error with a safe user-facing message."""


class OperatingModeError(ControlError):
    """Typed operating-mode failure with a stable public error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ActionBusy(ControlError):
    """Another state-changing action is already running."""


class OutputLimitExceeded(ControlError):
    """A bounded subprocess emitted more output than the service accepts."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(self, argv: list[str], *, timeout: float) -> CommandResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ControlError("Interner Kommandovertrag ist ungültig.")
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C.UTF-8"},
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise ControlError(f"Benötigtes Programm fehlt: {argv[0]}") from error
        except OSError as error:
            raise ControlError(
                f"Benötigtes Programm konnte nicht gestartet werden: {argv[0]}"
            ) from error

        assert process.stdout is not None
        assert process.stderr is not None
        streams = {
            process.stdout: bytearray(),
            process.stderr: bytearray(),
        }
        selector = selectors.DefaultSelector()
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        total_bytes = 0
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_process_group(process)
                    raise ControlError(
                        "Die lokale Audioabfrage hat das Zeitlimit erreicht."
                    )
                for key, _mask in selector.select(timeout=min(remaining, 0.1)):
                    stream = key.fileobj
                    chunk = os.read(stream.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_SUBPROCESS_OUTPUT_BYTES:
                        self._kill_process_group(process)
                        raise OutputLimitExceeded(
                            "Die lokale Audioabfrage lieferte zu viele Daten."
                        )
                    streams[stream].extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_process_group(process)
                raise ControlError(
                    "Die lokale Audioabfrage hat das Zeitlimit erreicht."
                )
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            self._kill_process_group(process)
            raise ControlError(
                "Die lokale Audioabfrage hat das Zeitlimit erreicht."
            ) from error
        finally:
            selector.close()
            for stream in streams:
                if not stream.closed:
                    stream.close()

        return CommandResult(
            tuple(argv),
            returncode,
            bytes(streams[process.stdout]).decode("utf-8", errors="replace"),
            bytes(streams[process.stderr]).decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def load_json_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ControlError(
            f"Repositoryvertrag ist nicht lesbar: {path.name}"
        ) from error
    if not isinstance(value, dict):
        raise ControlError(f"Repositoryvertrag ist kein JSON-Objekt: {path.name}")
    return value


def parse_json_output(result: CommandResult, *, label: str) -> dict[str, Any]:
    candidates = (result.stdout.strip(), result.stderr.strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ControlError(f"{label} lieferte keinen lesbaren Zustandsbericht.")


def safe_error_message(report: dict[str, Any], fallback: str) -> str:
    value = report.get("error")
    if not isinstance(value, str) or not value.strip():
        return fallback
    compact = " ".join(value.split())
    return compact[:300]


def require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlError(f"{label} ist kein JSON-Objekt.")
    return value


def require_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ControlError(f"{label} ist keine JSON-Liste.")
    return value


def default_operating_mode_configuration() -> dict[str, Any]:
    """Return the declared default without writing state during GET or refresh."""

    return {
        "schema_version": 1,
        "kind": "audio_operating_mode_configuration",
        "configured_mode": "desktop-listening",
        "transition": None,
        "last_request": None,
        "updated_at": None,
        "source": "declared-default",
    }


def _validate_operating_mode_request_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or OPERATING_MODE_REQUEST_ID_RE.fullmatch(value) is None
    ):
        raise OperatingModeError(
            "operating_mode_request_invalid",
            "Die Modustransition benötigt eine gültige Request-ID.",
        )
    return value


def _validate_operating_mode_transition(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "from_mode",
        "target_mode",
        "state",
        "effect_started",
        "reason",
    }:
        raise ControlError("Betriebsmoduszustand enthält eine ungültige Transition.")
    request_id = _validate_operating_mode_request_id(value.get("request_id"))
    if (
        value.get("from_mode") not in OPERATING_MODES
        or value.get("target_mode") not in OPERATING_MODES
        or value.get("state") not in {"transitioning", "recovering"}
        or not isinstance(value.get("effect_started"), bool)
        or (
            value.get("reason") is not None
            and (
                not isinstance(value.get("reason"), str)
                or not value["reason"]
                or len(value["reason"]) > 160
            )
        )
    ):
        raise ControlError("Betriebsmoduszustand enthält eine ungültige Transition.")
    return {**value, "request_id": request_id}


def _validate_operating_mode_receipt(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "target_mode",
        "status",
        "configuration_changed",
        "audio_mutated",
    }:
        raise ControlError("Betriebsmoduszustand enthält einen ungültigen Request-Beleg.")
    request_id = _validate_operating_mode_request_id(value.get("request_id"))
    if (
        value.get("target_mode") not in OPERATING_MODES
        or value.get("status") != "ready"
        or not isinstance(value.get("configuration_changed"), bool)
        or (
            value.get("audio_mutated") is not None
            and not isinstance(value.get("audio_mutated"), bool)
        )
    ):
        raise ControlError("Betriebsmoduszustand enthält einen ungültigen Request-Beleg.")
    return {**value, "request_id": request_id}


def validate_operating_mode_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "configured_mode",
        "transition",
        "last_request",
        "updated_at",
    }:
        raise ControlError("Betriebsmoduszustand verletzt den Zustandsvertrag.")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "audio_operating_mode_configuration"
        or value.get("configured_mode") not in OPERATING_MODES
        or not isinstance(value.get("updated_at"), str)
        or not value["updated_at"]
        or len(value["updated_at"]) > 80
    ):
        raise ControlError("Betriebsmoduszustand verletzt den Zustandsvertrag.")
    transition = _validate_operating_mode_transition(value.get("transition"))
    receipt = _validate_operating_mode_receipt(value.get("last_request"))
    if transition is not None and transition["from_mode"] != value["configured_mode"]:
        raise ControlError("Betriebsmodus-Transition ist nicht an den Sollmodus gebunden.")
    return {**value, "transition": transition, "last_request": receipt, "source": "persisted"}


def read_operating_mode_configuration(path: pathlib.Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return default_operating_mode_configuration()
    except OSError as error:
        raise ControlError("Betriebsmoduszustand ist nicht lesbar.") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_OPERATING_MODE_STATE_BYTES
        ):
            raise ControlError("Betriebsmoduszustand ist nicht sicher gebunden.")
        encoded = b""
        while len(encoded) <= MAX_OPERATING_MODE_STATE_BYTES:
            chunk = os.read(
                descriptor, MAX_OPERATING_MODE_STATE_BYTES + 1 - len(encoded)
            )
            if not chunk:
                break
            encoded += chunk
        if len(encoded) > MAX_OPERATING_MODE_STATE_BYTES:
            raise ControlError("Betriebsmoduszustand überschreitet die Größenbegrenzung.")
        value = json.loads(encoded.decode("utf-8"))
    except ControlError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("Betriebsmoduszustand ist nicht lesbar.") from error
    finally:
        os.close(descriptor)
    return validate_operating_mode_configuration(value)


def write_operating_mode_configuration(
    path: pathlib.Path, configuration: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        key: configuration[key]
        for key in (
            "schema_version",
            "kind",
            "configured_mode",
            "transition",
            "last_request",
            "updated_at",
        )
    }
    validate_operating_mode_configuration(payload)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_OPERATING_MODE_STATE_BYTES:
        raise ControlError("Betriebsmoduszustand überschreitet die Größenbegrenzung.")
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if (
            stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
        ):
            raise ControlError("Betriebsmodusverzeichnis ist nicht sicher gebunden.")
        parent.chmod(0o700)
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                raise ControlError("Betriebsmoduszustand ist nicht sicher gebunden.")
        temporary: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=parent, prefix=".operating-mode-", delete=False
            ) as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = pathlib.Path(handle.name)
            temporary.replace(path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    except ControlError:
        raise
    except OSError as error:
        raise ControlError("Betriebsmoduszustand konnte nicht sicher gespeichert werden.") from error
    return {**payload, "source": "persisted"}


def _qobuz_projection(doctor: dict[str, Any]) -> dict[str, Any]:
    streaming = doctor.get("streaming_sources")
    if not isinstance(streaming, dict):
        streaming = {}
    qobuz = streaming.get("qobuz")
    if not isinstance(qobuz, dict):
        qobuz = {}
    qbzd = qobuz.get("qbzd")
    if not isinstance(qbzd, dict):
        qbzd = {}
    qconnect = qbzd.get("qconnect")
    if not isinstance(qconnect, dict):
        qconnect = {}
    playback = qobuz.get("motu_hardware_playback")
    if not isinstance(playback, dict):
        playback = {}
    qconnect_state = qconnect.get("state")
    current_qbzd_playback = (
        playback.get("owner_class") == "qbzd"
        and playback.get("pcm_state") == "RUNNING"
        and playback.get("open") is True
    )
    reference_ready = (
        qobuz.get("selected_reference_provider") == "qbzd-qconnect"
        and qobuz.get("reference_provider_ready") is True
        and qbzd.get("status") == "available"
        and qconnect_state == "connected"
        and qconnect.get("session_active") is True
    )
    track_native = (
        reference_ready
        and current_qbzd_playback
        and qobuz.get("track_native_proven") is True
    )
    return {
        "provider": qobuz.get("selected_reference_provider"),
        "reference_ready": reference_ready,
        "qbzd_status": qbzd.get("status"),
        "qconnect_state": qconnect_state,
        "qconnect_session_active": qconnect.get("session_active") is True,
        "current_qbzd_playback": current_qbzd_playback,
        "track_native_proven": track_native,
        "rate_proof_state": qobuz.get("rate_proof_state", "blocked"),
        "track_sample_rate_hz": playback.get("rate_hz") if track_native else None,
    }


def operating_mode_target_ready(
    target_mode: str, doctor_status: str, doctor: dict[str, Any]
) -> bool:
    if doctor_status != "ok":
        return False
    hardware = doctor.get("hardware")
    if not isinstance(hardware, dict) or hardware.get("motu_m2") is not True:
        return False
    qobuz = _qobuz_projection(doctor)
    if target_mode == "qobuz-reference":
        return qobuz["reference_ready"]
    if target_mode == "desktop-listening":
        graph = doctor.get("graph")
        if not isinstance(graph, dict):
            return False
        return (
            not qobuz["current_qbzd_playback"]
            and graph.get("default_sink") == "motu-m2"
            and graph.get("force_rate_hz") == 48_000
            and graph.get("force_quantum_frames") == 1_024
        )
    return False


def project_operating_modes(
    configuration: dict[str, Any],
    *,
    doctor_status: str,
    doctor: dict[str, Any],
    recording_status: str = "unavailable",
    recording: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project configured, observed, physical and executable truth orthogonally."""

    hardware = doctor.get("hardware")
    if not isinstance(hardware, dict):
        hardware = {}
    motu_present = hardware.get("motu_m2") is True if doctor_status == "ok" else None
    graph = doctor.get("graph")
    if not isinstance(graph, dict):
        graph = {}
    qobuz = _qobuz_projection(doctor)
    recording = recording if isinstance(recording, dict) else {}
    recording_session = recording.get("session")
    recording_session = recording_session if isinstance(recording_session, dict) else {}
    recording_active = (
        recording_status == "ok"
        and recording.get("status") == "running"
        and recording_session.get("active") is True
    )
    recording_recovery = (
        recording_status == "ok"
        and recording_session.get("recovery_required") is True
    )
    if recording_active:
        observed_mode = "recording"
        signal_state = "recording"
        signal_path = ["RØDE NT1-A", "MOTU M2", "Recorder"]
    elif qobuz["current_qbzd_playback"]:
        observed_mode = "qobuz-reference"
        signal_state = "playing"
        signal_path = ["Qobuz Connect", "QBZD / ALSA Direct", "MOTU M2"]
    elif graph.get("default_sink") == "motu-m2" and doctor_status == "ok":
        observed_mode = "desktop-listening"
        signal_state = "prepared"
        signal_path = ["Desktop / Spotify / Browser", "PipeWire gemischt", "MOTU M2"]
    else:
        observed_mode = None
        signal_state = "unknown"
        signal_path = []

    desktop_ready = operating_mode_target_ready(
        "desktop-listening", doctor_status, doctor
    )
    qobuz_ready = operating_mode_target_ready(
        "qobuz-reference", doctor_status, doctor
    )
    if doctor_status != "ok":
        desktop_state, desktop_reason = "blocked", "doctor-unavailable"
        qobuz_state, qobuz_reason = "blocked", "doctor-unavailable"
    elif motu_present is not True:
        desktop_state, desktop_reason = "blocked", "motu-not-observed"
        qobuz_state, qobuz_reason = "blocked", "motu-not-observed"
    else:
        desktop_state = "ready" if desktop_ready else "attention"
        desktop_reason = (
            None
            if desktop_ready
            else "qobuz-playback-running"
            if qobuz["current_qbzd_playback"]
            else "desktop-transition-required"
        )
        if qobuz_ready:
            qobuz_state, qobuz_reason = "ready", None
        elif qobuz["qconnect_state"] in {"retrying", "reconnecting"}:
            qobuz_state, qobuz_reason = "recovering", "qconnect-retrying"
        elif qobuz["qbzd_status"] in {"api-unavailable", "api-invalid", "not-observed"}:
            qobuz_state, qobuz_reason = "attention", "qbzd-readback-unavailable"
        else:
            qobuz_state, qobuz_reason = "blocked", "qconnect-not-ready"

    if recording_status != "ok":
        recording_state, recording_reason = "blocked", "recorder-unavailable"
    elif recording_recovery:
        recording_state, recording_reason = "recovering", "recording-recovery-required"
    elif recording_active:
        recording_state, recording_reason = "ready", None
    else:
        recording_state, recording_reason = "attention", "recording-preflight-required"

    modes: list[dict[str, Any]] = [
        {
            "id": "desktop-listening",
            **OPERATING_MODES["desktop-listening"],
            "state": desktop_state,
            "reason": desktop_reason,
            "configured": configuration["configured_mode"] == "desktop-listening",
            "quality": {
                "path": "shared-pipewire-motu",
                "sample_rate_hz": graph.get("force_rate_hz"),
                "track_native_proven": False,
            },
        },
        {
            "id": "qobuz-reference",
            **OPERATING_MODES["qobuz-reference"],
            "state": qobuz_state,
            "reason": qobuz_reason,
            "configured": configuration["configured_mode"] == "qobuz-reference",
            "quality": {
                "path": "qbzd-alsa-direct-motu",
                "sample_rate_hz": qobuz["track_sample_rate_hz"],
                "track_native_proven": qobuz["track_native_proven"],
                "rate_proof_state": qobuz["rate_proof_state"],
            },
            "qconnect": {
                "state": qobuz["qconnect_state"],
                "session_active": qobuz["qconnect_session_active"],
            },
        },
    ]
    modes.append(
        {
            "id": "recording",
            **OPERATING_MODES["recording"],
            "state": recording_state,
            "reason": recording_reason,
            "configured": configuration["configured_mode"] == "recording",
            "quality": {
                "path": "recorder-bound-motu-capture",
                "sample_rate_hz": recording_session.get("capture", {}).get("sample_rate_hz")
                if isinstance(recording_session.get("capture"), dict)
                else None,
                "channels": recording_session.get("capture", {}).get("channels")
                if isinstance(recording_session.get("capture"), dict)
                else None,
                "source_bound": recording_session.get("source", {}).get("bound") is True
                if isinstance(recording_session.get("source"), dict)
                else False,
            },
            "activity": "recording" if recording_active else "idle",
        }
    )
    modes.append(
        {
            "id": "performance",
            **OPERATING_MODES["performance"],
            "state": "blocked",
            "reason": "declared-later-mode",
            "configured": configuration["configured_mode"] == "performance",
            "quality": None,
        }
    )
    mode_states = {mode["id"]: mode for mode in modes}
    transition = configuration.get("transition")
    if isinstance(transition, dict):
        overall_state = transition["state"]
    else:
        overall_state = mode_states[configuration["configured_mode"]]["state"]
    if overall_state not in OPERATING_MODE_STATES:
        raise ControlError("Betriebsmodusprojektion enthält einen unbekannten Zustand.")
    external = doctor.get("external_endpoints")
    if not isinstance(external, dict):
        external = {}
    return {
        "schema_version": 1,
        "kind": "audio_operating_mode_projection",
        "state": overall_state,
        "configured": {
            "mode": configuration["configured_mode"],
            "source": configuration.get("source", "persisted"),
            "transition": transition,
        },
        "observed": {
            "mode": observed_mode,
            "signal_state": signal_state,
            "signal_path": signal_path,
            "qobuz_current_playback": qobuz["current_qbzd_playback"],
        },
        "physical": {
            "motu_m2": motu_present,
            "authority": "doctor-current-hardware-observation",
        },
        "executable": {
            "desktop-listening": {
                "allowed": doctor_status == "ok"
                and motu_present is True
                and not qobuz["current_qbzd_playback"],
                "authority": "desktop-mixed-transition-v1",
            },
            "qobuz-reference": {
                "allowed": qobuz_ready,
                "authority": "qbzd-qconnect-doctor-readback-v1",
            },
            "recording": {
                "allowed": recording_status == "ok",
                "authority": "recorder-plan-hash-and-current-readback",
            },
            "performance": {"allowed": False, "authority": "declared-later-mode"},
        },
        "modes": modes,
        "active_signal_path": {
            "mode": observed_mode,
            "state": signal_state,
            "nodes": signal_path,
        },
        "read_only_outputs": {
            "pioneer": external.get("pioneer_vsx_830_k", {}),
            "bluetooth": external.get("transmitter_1mii_b03_pro", {}),
        },
        "truth_boundary": {
            "track_native_proven": qobuz["track_native_proven"],
            "track_sample_rate_hz": qobuz["track_sample_rate_hz"],
            "connected_or_ready_is_track_native": False,
        },
    }


def validate_doctor_report(report: dict[str, Any]) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_doctor_report"
        or report.get("read_only_contract") is not True
    ):
        raise ControlError("Audio-Doctor lieferte einen fremden Zustandsvertrag.")
    mappings = {
        key: require_mapping(report.get(key), label=f"Audio-Doctor-Feld {key}")
        for key in ("graph", "hardware", "device_truth", "external_endpoints")
    }
    hardware = mappings["hardware"]
    desired = require_mapping(
        mappings["device_truth"].get("desired"),
        label="Audio-Doctor-Feld device_truth.desired",
    )
    for device_id in ("motu_m2", "roland_fp_30x"):
        if not isinstance(hardware.get(device_id), bool) or not isinstance(
            desired.get(device_id), bool
        ):
            raise ControlError("Audio-Doctor enthält unvollständige Gerätewahrheit.")
    for key in ("warnings", "physical_unknowns", "command_health"):
        require_list(report.get(key), label=f"Audio-Doctor-Feld {key}")
    for warning in report["warnings"]:
        if (
            not isinstance(warning, dict)
            or not isinstance(warning.get("code"), str)
            or warning.get("severity") not in {"info", "medium", "high"}
            or not isinstance(warning.get("detail"), str)
        ):
            raise ControlError("Audio-Doctor enthält einen ungültigen Warnhinweis.")
    if not all(isinstance(item, str) and item for item in report["physical_unknowns"]):
        raise ControlError("Audio-Doctor enthält ungültige physische Unbekannte.")
    for command in report["command_health"]:
        if (
            not isinstance(command, dict)
            or not isinstance(command.get("command"), str)
            or not isinstance(command.get("available"), bool)
            or type(command.get("returncode")) is not int
        ):
            raise ControlError("Audio-Doctor enthält ungültigen Werkzeugzustand.")


def validate_whale_status(report: dict[str, Any]) -> None:
    required_strings = ("unit", "load_state", "active_state", "sub_state")
    if any(not isinstance(report.get(key), str) for key in required_strings):
        raise ControlError("Buckelwal-Dienst lieferte unvollständigen Status.")
    if report["unit"] != "audio-buckelwal-live-voice-v1.service":
        raise ControlError("Buckelwal-Dienst meldete eine fremde Unit.")
    voice_mode = report.get("voice_mode")
    if voice_mode is not None and voice_mode not in ALLOWED_WHALE_MODES:
        raise ControlError("Buckelwal-Dienst meldete einen unbekannten Modus.")
    active_contract = (
        report["load_state"] == "loaded"
        and report["active_state"] == "active"
        and report["sub_state"] == "running"
        and voice_mode in ALLOWED_WHALE_MODES
    )
    inactive_contract = (
        report["load_state"] in {"loaded", "not-found"}
        and report["active_state"] == "inactive"
        and report["sub_state"] in {"dead", "exited"}
    )
    if not active_contract and not inactive_contract:
        raise ControlError("Buckelwal-Dienst meldete keinen terminalen Zustand.")


def validate_dauersong_status(report: dict[str, Any]) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "dauersong_live_status"
        or report.get("unit") != "grabowski-dauersong.service"
    ):
        raise ControlError("Dauersong-Dienst meldete einen fremden Statusvertrag.")
    for key in ("load_state", "active_state", "sub_state"):
        if not isinstance(report.get(key), str):
            raise ControlError("Dauersong-Dienst lieferte unvollständigen Status.")
    if not isinstance(report.get("source_binding_ready"), bool):
        raise ControlError("Dauersong-Dienst meldete keine Quellenbindung.")
    active = report.get("active") is True
    active_contract = (
        report.get("load_state") == "loaded"
        and report.get("active_state") == "active"
        and report.get("sub_state") == "running"
    )
    inactive_contract = (
        report.get("load_state") in {"loaded", "not-found"}
        and report.get("active_state") in {"inactive", "failed"}
        and report.get("sub_state") in {"dead", "exited", "failed"}
    )
    if active != active_contract:
        raise ControlError("Dauersong-Dienst meldete widersprüchliche Aktivität.")
    if not active_contract and not inactive_contract:
        raise ControlError("Dauersong-Dienst meldete keinen terminalen Zustand.")
    stream = report.get("stream")
    if not isinstance(stream, dict):
        raise ControlError("Dauersong-Dienst meldete keinen Audiostreamzustand.")


def validate_profile_plan(
    report: dict[str, Any],
    profile_id: str,
    apply_authority: str,
) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_profile_plan"
        or report.get("profile") != profile_id
        or report.get("read_only") is not True
        or not isinstance(report.get("ready_for_laboratory_apply"), bool)
        or report.get("apply_authority") != apply_authority
    ):
        raise ControlError("Profilplaner lieferte einen fremden Planvertrag.")
    for key in (
        "readiness_blockers",
        "missing_hardware",
        "missing_physical_facts",
        "unresolved_laboratory_gates",
        "proposed_changes",
    ):
        require_list(report.get(key), label=f"Profilplan-Feld {key}")
    planned_blocker = report.get("planned_blocker")
    if planned_blocker is not None and not isinstance(planned_blocker, str):
        raise ControlError("Profilplan-Feld planned_blocker ist ungültig.")


def read_voice_recording_contract() -> dict[str, Any]:
    catalog = load_json_object(RECORDING_CATALOG)
    if (
        catalog.get("schema_version") != 1
        or catalog.get("kind") != "audio_recording_session_catalog"
    ):
        raise ControlError("Recorderkatalog enthält einen fremden Vertrag.")
    sessions = require_mapping(catalog.get("sessions"), label="Recorderkatalog sessions")
    voice = require_mapping(sessions.get("voice-recording"), label="Recorderprofil Stimme")
    capture = require_mapping(voice.get("capture"), label="Recorderprofil capture")
    source = require_mapping(voice.get("source"), label="Recorderprofil source")
    monitoring = require_mapping(voice.get("monitoring"), label="Recorderprofil monitoring")
    physical = require_mapping(
        voice.get("required_physical_facts"), label="Recorderprofil physical"
    )
    laboratory = require_list(
        voice.get("required_laboratory_gates"), label="Recorderprofil laboratory"
    )
    advisory_laboratory = require_list(
        voice.get("advisory_laboratory_gates"),
        label="Recorderprofil advisory laboratory",
    )
    expected_physical = {
        "rode_nt1a_connected": True,
        "rode_nt1a_motu_input": ["input-1", "input-2"],
        "motu_phantom_48v": "on",
        "motu_input_gain_reference": "non-empty-string",
    }
    if (
        voice.get("profile") != "voice-recording"
        or physical != expected_physical
        or laboratory != []
        or advisory_laboratory != ["voice-level-measurement"]
        or source.get("kind") != "motu-voice"
        or source.get("vendor_id") != "07fd"
        or source.get("product_id") != "0008"
        or source.get("required_sample_rate_hz") != 48_000
        or source.get("required_channels") != 2
        or source.get("requires_unmuted") is not True
        or source.get("requires_unity_volume") is not True
        or monitoring.get("mode") != "hardware-direct"
        or monitoring.get("endpoint") != "motu-m2"
        or monitoring.get("software_loopback") is not False
        or monitoring.get("level_claim") != "physical-reference-required"
        or capture.get("sample_rate_hz") != 48_000
        or capture.get("sample_format") != "s32le"
        or capture.get("channels") != 2
        or capture.get("container") != "wav"
    ):
        raise ControlError("Recorderprofil verletzt den Voice-Vertrag.")
    levels = load_json_object(REFERENCE_LEVELS)
    recording_levels = require_mapping(
        levels.get("recording"), label="Referenzpegel recording"
    )
    typical = recording_levels.get("voice_typical_average_dbfs_range")
    peak = recording_levels.get("voice_peak_dbfs_range")
    clipping = recording_levels.get("clipping_limit_dbfs")
    if (
        not isinstance(typical, list)
        or len(typical) != 2
        or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in typical)
        or not isinstance(peak, list)
        or len(peak) != 2
        or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in peak)
        or not isinstance(clipping, (int, float))
        or isinstance(clipping, bool)
    ):
        raise ControlError("Referenzpegel für Sprachaufnahme sind unvollständig.")
    return {
        "session_type": "voice-recording",
        "profile": "voice-recording",
        "source": {
            "kind": "motu-voice",
            "interface": "MOTU M2",
            "microphone": "RØDE NT1-A",
            "sample_rate_hz": capture["sample_rate_hz"],
            "sample_format": capture["sample_format"],
            "channels": capture["channels"],
        },
        "monitoring": {
            "mode": monitoring["mode"],
            "endpoint": monitoring["endpoint"],
            "software_loopback": False,
            "latency_expectation": "hardware-direct-minimal-not-software-measured",
            "level_claim": monitoring["level_claim"],
        },
        "required_physical_facts": expected_physical,
        "required_laboratory_gates": list(laboratory),
        "advisory_laboratory_gates": list(advisory_laboratory),
        "capture": {
            "container": capture["container"],
            "sample_rate_hz": capture["sample_rate_hz"],
            "sample_format": capture["sample_format"],
            "channels": capture["channels"],
            "minimum_duration_seconds": capture["minimum_duration_seconds"],
            "maximum_duration_seconds": capture["maximum_duration_seconds"],
            "overwrite": False,
        },
        "levels": {
            "typical_average_dbfs_range": typical,
            "peak_dbfs_range": peak,
            "clipping_limit_dbfs": clipping,
            "authority": "reference-targets-not-live-measurement",
        },
    }


def read_recording_contract(mode: str) -> dict[str, Any]:
    mode_spec = RECORDING_MODES.get(mode)
    if mode_spec is None:
        raise ControlError("Unbekannter Aufnahmemodus.")
    voice = read_voice_recording_contract()
    if mode == "voice":
        return voice
    catalog = load_json_object(RECORDING_CATALOG)
    sessions = require_mapping(catalog.get("sessions"), label="Recorderkatalog sessions")
    performance = require_mapping(
        sessions.get("piano-vocal-performance"), label="Recorderprofil Performance"
    )
    source = require_mapping(performance.get("source"), label="Performancequelle")
    roland_audio = require_mapping(
        source.get("roland_audio"), label="Performance-Roland-Audio"
    )
    midi = require_mapping(source.get("midi"), label="Performance-MIDI")
    performance_laboratory = require_list(
        performance.get("required_laboratory_gates"),
        label="Performance laboratory",
    )
    performance_advisory_laboratory = require_list(
        performance.get("advisory_laboratory_gates"),
        label="Performance advisory laboratory",
    )
    if (
        performance.get("profile") != "voice-recording"
        or performance.get("capture")
        != require_mapping(
            sessions.get("voice-recording"), label="Recorderprofil Stimme"
        ).get("capture")
        or performance_laboratory != ["resampling-decision"]
        or performance_advisory_laboratory != ["voice-level-measurement"]
        or source.get("kind") != "motu-voice-with-roland-audio-and-midi"
        or roland_audio.get("kind") != "usb-audio"
        or roland_audio.get("vendor_id") != "0582"
        or roland_audio.get("product_id") != "01b1"
        or roland_audio.get("required_sample_rate_hz") != 44_100
        or roland_audio.get("required_channels") != 2
        or sorted(roland_audio.get("required_sample_formats", [])) != ["s24le", "s32le"]
        or midi.get("kind") != "alsa-sequencer-midi"
        or midi.get("usb_vendor_id") != "0582"
        or midi.get("usb_product_id") != "01b1"
        or midi.get("capture") != "arecordmidi-standard-midi-file"
        or midi.get("timing")
        != {
            "basis": "SMPTE",
            "fps": 25,
            "ticks_per_frame": 40,
            "nominal_resolution_ms": 1,
        }
    ):
        raise ControlError("Recorderprofil verletzt den Performance-Vertrag.")
    return {
        **voice,
        "session_type": mode_spec["session_type"],
        "source": {
            "kind": "motu-voice-with-roland-audio-and-midi",
            "audio": voice["source"],
            "roland_audio": {
                "instrument": "Roland FP-30X",
                "sample_rate_hz": roland_audio["required_sample_rate_hz"],
                "sample_formats": roland_audio["required_sample_formats"],
                "channels": roland_audio["required_channels"],
                "capture_rate_hz": 48_000,
            },
            "midi": {
                "instrument": "Roland FP-30X",
                "format": "Standard MIDI File",
                "timing": midi["timing"],
            },
        },
        "product": "Stereo-Mix WAV + Roland MIDI",
        "required_laboratory_gates": list(performance_laboratory),
        "advisory_laboratory_gates": list(performance_advisory_laboratory),
    }


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_recording_session_id(value: Any) -> str:
    if not isinstance(value, str) or RECORDING_SESSION_ID_RE.fullmatch(value) is None:
        raise ControlError("Ungültige Recorder-Sitzungs-ID.")
    return value


def _validate_recording_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value in {".", ".."}
        or pathlib.PurePath(value).name != value
        or "/" in value
        or chr(92) in value
        or not value.lower().endswith(".wav")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ControlError("Take-Name muss ein einzelner sicherer WAV-Dateiname sein.")
    return value


def _validate_recording_duration(value: Any, mode: str = "voice") -> int:
    contract = read_recording_contract(mode)
    maximum = contract["capture"]["maximum_duration_seconds"]
    minimum = contract["capture"]["minimum_duration_seconds"]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ControlError(
            f"Aufnahmedauer muss zwischen {minimum} und {maximum} Sekunden liegen."
        )
    return value


def _reject_recording_private_paths(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = {"path", "root", "state_root", "partial", "final", "process"}
        if forbidden.intersection(value):
            raise ControlError("Recorderprojektion enthält private Zustandsfelder.")
        for nested in value.values():
            _reject_recording_private_paths(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_recording_private_paths(nested)


def validate_recording_product_probe(report: dict[str, Any]) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_product_probe"
        or report.get("read_only") is not True
        or not isinstance(report.get("status"), str)
    ):
        raise ControlError("Recorderprojektion lieferte einen fremden Zustandsvertrag.")
    active_id = report.get("active_session_id")
    if active_id is not None:
        _validate_recording_session_id(active_id)
    session = report.get("session")
    if session is not None:
        if not isinstance(session, dict):
            raise ControlError("Recorderprojektion enthält keine gültige Sitzung.")
        _validate_recording_session_id(session.get("session_id"))
        if session.get("session_type") not in RECORDING_SESSION_TYPES or not _valid_sha256(
            session.get("plan_sha256")
        ):
            raise ControlError("Recorderprojektion ist nicht an Modus und Plan gebunden.")
    _reject_recording_private_paths(report)


def _valid_recording_library_timestamp(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value or len(value) > 80:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_recording_library_metadata(value: Any, session_id: str) -> None:
    required = {
        "schema_version",
        "kind",
        "session_id",
        "category",
        "trashed",
        "updated_at",
        "trashed_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("kind") != "audio_recording_library_metadata"
        or value.get("session_id") != session_id
        or value.get("category") not in RECORDING_LIBRARY_CATEGORIES
        or not isinstance(value.get("trashed"), bool)
        or not _valid_recording_library_timestamp(value.get("updated_at"))
        or not _valid_recording_library_timestamp(value.get("trashed_at"))
        or (value["trashed"] and value.get("trashed_at") is None)
        or (not value["trashed"] and value.get("trashed_at") is not None)
    ):
        raise ControlError("Recorderbibliothek enthält ungültige Organisationsmetadaten.")


def validate_recording_library_action(
    report: dict[str, Any], operation: str, session_id: str
) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_library_action_result"
        or report.get("operation") != operation
        or report.get("session_id") != session_id
        or not isinstance(report.get("changed"), bool)
    ):
        raise ControlError("Bibliotheksaktion lieferte keinen gebundenen Ergebnisbeleg.")
    validate_recording_library_metadata(report.get("library"), session_id)
    _reject_recording_private_paths(report)


def validate_recording_library(report: dict[str, Any]) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_product_library"
        or report.get("read_only") is not True
        or not isinstance(report.get("items"), list)
        or any(
            isinstance(report.get(name), bool) or not isinstance(report.get(name), int)
            for name in ("count", "active_count", "trashed_count")
        )
        or report.get("count") != len(report["items"])
        or report.get("active_count") + report.get("trashed_count") != report.get("count")
    ):
        raise ControlError("Recorderbibliothek lieferte einen fremden Vertrag.")
    for item in report["items"]:
        if not isinstance(item, dict):
            raise ControlError("Recorderbibliothek enthält einen ungültigen Take.")
        _validate_recording_session_id(item.get("session_id"))
        if item.get("session_type") not in RECORDING_SESSION_TYPES or not _valid_sha256(
            item.get("plan_sha256")
        ):
            raise ControlError("Recorderbibliothek enthält einen ungebundenen Take.")
        validate_recording_library_metadata(item.get("library"), item["session_id"])
    _reject_recording_private_paths(report)


def project_recording_plan(report: dict[str, Any], *, mode: str = "voice") -> dict[str, Any]:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_plan"
        or not isinstance(report.get("ready"), bool)
        or not _valid_sha256(report.get("plan_sha256"))
    ):
        raise ControlError("Recorderplan lieferte einen fremden Vertrag.")
    identity = require_mapping(report.get("identity"), label="Recorderplan identity")
    mode_spec = RECORDING_MODES.get(mode)
    if (
        mode_spec is None
        or identity.get("session_type") != mode_spec["session_type"]
        or identity.get("profile") != "voice-recording"
    ):
        raise ControlError("Recorderplan ist nicht an den gewählten Modus gebunden.")
    output = require_mapping(identity.get("output"), label="Recorderplan output")
    capture = require_mapping(identity.get("capture"), label="Recorderplan capture")
    physical = require_mapping(identity.get("physical"), label="Recorderplan physical")
    facts = require_mapping(physical.get("facts", {}), label="Recorderplan physical.facts")
    laboratory = require_mapping(identity.get("laboratory"), label="Recorderplan laboratory")
    require_list(laboratory.get("resolved", []), label="Recorderplan laboratory.resolved")
    advisory_laboratory = require_mapping(
        identity.get("advisory_laboratory"), label="Recorderplan advisory laboratory"
    )
    advisory_resolved = require_list(
        advisory_laboratory.get("resolved", []),
        label="Recorderplan advisory laboratory.resolved",
    )
    source = require_mapping(identity.get("source"), label="Recorderplan source")
    source_identity = source.get("identity")
    if source_identity is not None and not isinstance(source_identity, dict):
        raise ControlError("Recorderplan enthält keine gültige Quellenbindung.")
    readiness = require_mapping(report.get("readiness"), label="Recorderplan readiness")
    blockers = require_list(readiness.get("blockers"), label="Recorderplan blockers")
    if (
        not all(isinstance(item, str) and item for item in blockers)
        or blockers != sorted(set(blockers))
    ):
        raise ControlError("Recorderplan enthält ungültige Blocker.")
    checks = require_list(readiness.get("checks"), label="Recorderplan checks")
    if len(checks) != len(RECORDING_READINESS_CHECK_IDS):
        raise ControlError("Recorderplan enthält keine vollständige Startprüfung.")
    projected_checks: list[dict[str, Any]] = []
    observed_check_ids: list[str] = []
    check_blockers: set[str] = set()
    for raw_check in checks:
        check = require_mapping(raw_check, label="Recorderplan check")
        if set(check) != {"id", "status", "blockers"}:
            raise ControlError("Recorderplan enthält eine ungültige Startprüfung.")
        check_id = check.get("id")
        status = check.get("status")
        nested_blockers = require_list(
            check.get("blockers"), label="Recorderplan check blockers"
        )
        if (
            not isinstance(check_id, str)
            or check_id not in RECORDING_READINESS_CHECK_IDS
            or status not in {"ready", "blocked"}
            or not all(isinstance(item, str) and item for item in nested_blockers)
            or nested_blockers != sorted(set(nested_blockers))
            or (status == "ready") != (len(nested_blockers) == 0)
        ):
            raise ControlError("Recorderplan enthält eine widersprüchliche Startprüfung.")
        observed_check_ids.append(check_id)
        check_blockers.update(nested_blockers)
        projected_checks.append(
            {
                "id": check_id,
                "status": status,
                "blockers": list(nested_blockers),
            }
        )
    if tuple(observed_check_ids) != RECORDING_READINESS_CHECK_IDS:
        raise ControlError("Recorderplan enthält keine kanonische Startprüfreihenfolge.")
    if check_blockers != set(blockers):
        raise ControlError("Recorderplan widerspricht sich bei den Startblockern.")
    if report["ready"] != all(check["status"] == "ready" for check in projected_checks):
        raise ControlError("Recorderplan widerspricht sich beim Bereitschaftsstatus.")
    raw_advisories = require_list(
        readiness.get("advisories"), label="Recorderplan advisories"
    )
    if len(raw_advisories) != len(RECORDING_ADVISORY_IDS):
        raise ControlError("Recorderplan enthält keine vollständigen Aufnahmehinweise.")
    projected_advisories: list[dict[str, Any]] = []
    observed_advisory_ids: list[str] = []
    for raw_advisory in raw_advisories:
        advisory = require_mapping(raw_advisory, label="Recorderplan advisory")
        if set(advisory) != {"id", "status", "notices"}:
            raise ControlError("Recorderplan enthält einen ungültigen Aufnahmehinweis.")
        advisory_id = advisory.get("id")
        status = advisory.get("status")
        notices = require_list(
            advisory.get("notices"), label="Recorderplan advisory notices"
        )
        if (
            not isinstance(advisory_id, str)
            or advisory_id not in RECORDING_ADVISORY_IDS
            or status not in {"ready", "attention"}
            or not all(isinstance(item, str) and item for item in notices)
            or notices != sorted(set(notices))
            or (status == "ready") != (len(notices) == 0)
        ):
            raise ControlError("Recorderplan enthält einen widersprüchlichen Aufnahmehinweis.")
        observed_advisory_ids.append(advisory_id)
        projected_advisories.append(
            {"id": advisory_id, "status": status, "notices": list(notices)}
        )
    if tuple(observed_advisory_ids) != RECORDING_ADVISORY_IDS:
        raise ControlError("Recorderplan enthält keine kanonische Hinweisreihenfolge.")
    audio_identity = (
        source_identity.get("audio")
        if mode == "piano-vocal" and isinstance(source_identity, dict)
        else source_identity
    )
    projected = {
        "schema_version": 1,
        "kind": "audio_control_recording_plan",
        "ready": report["ready"],
        "plan_sha256": report["plan_sha256"],
        "mode": mode,
        "session_type": mode_spec["session_type"],
        "output": {"name": output.get("name"), "mode": output.get("mode"), "overwrite": output.get("overwrite")},
        "capture": {
            "sample_rate_hz": capture.get("sample_rate_hz"),
            "sample_format": capture.get("sample_format"),
            "channels": capture.get("channels"),
            "container": capture.get("container"),
            "maximum_duration_seconds": capture.get("maximum_duration_seconds"),
            "maximum_file_bytes": capture.get("maximum_file_bytes"),
        },
        "physical": {
            "rode_nt1a_connected": facts.get("rode_nt1a_connected"),
            "rode_nt1a_motu_input": facts.get("rode_nt1a_motu_input"),
            "motu_phantom_48v": facts.get("motu_phantom_48v"),
            "motu_input_gain_reference": bool(facts.get("motu_input_gain_reference")),
        },
        "laboratory": {
            "voice_level_measurement": "voice-level-measurement" in advisory_resolved
        },
        "source": {
            "bound": isinstance(source_identity, dict),
            "identity_sha256": source.get("identity_sha256"),
            "sample_rate_hz": (audio_identity or {}).get("sample_rate_hz"),
            "sample_format": (audio_identity or {}).get("sample_format"),
            "channels": (audio_identity or {}).get("channels"),
        },
        "monitoring": identity.get("monitoring"),
        "readiness": {
            "blockers": blockers,
            "checks": projected_checks,
            "advisories": projected_advisories,
            "free_bytes": readiness.get("free_bytes"),
            "required_file_bytes": readiness.get("required_file_bytes"),
            "required_free_bytes": readiness.get("required_free_bytes"),
        },
        "authority": "backend-plan-hash-and-source-binding",
    }
    if mode == "piano-vocal":
        performance = require_mapping(
            identity.get("performance"), label="Recorderplan performance"
        )
        midi_identity = (source_identity or {}).get("midi")
        roland_audio_identity = (source_identity or {}).get("roland_audio")
        projected["performance"] = {
            "product": "Stereo-Mix WAV + Roland MIDI",
            "midi_bound": isinstance(midi_identity, dict),
            "midi_identity_sha256": (midi_identity or {}).get("fingerprint"),
            "roland_audio_bound": isinstance(roland_audio_identity, dict),
            "roland_audio_identity_sha256": (roland_audio_identity or {}).get("fingerprint"),
            "midi_timing": performance.get("timing"),
        }
    return projected


def project_voice_level_acceptance_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "audio_level_measurement_evidence"
        or evidence.get("gate") != "voice-level-measurement"
        or evidence.get("result") not in {"pass", "fail"}
    ):
        raise ControlError("Pegelabnahme lieferte einen fremden Messvertrag.")
    blockers = evidence.get("blockers")
    if (
        not isinstance(blockers, list)
        or not all(isinstance(item, str) and item for item in blockers)
        or blockers != sorted(set(blockers))
        or (evidence["result"] == "pass") != (len(blockers) == 0)
    ):
        raise ControlError("Pegelabnahme lieferte widersprüchliche Blocker.")
    analysis = evidence.get("analysis")
    peak: float | None = None
    rms: float | None = None
    duration: float | None = None
    clipping: bool | None = None
    if analysis is not None:
        if not isinstance(analysis, dict) or analysis.get("kind") != "audio_level_analysis":
            raise ControlError("Pegelabnahme lieferte keine gültige Audioanalyse.")
        for raw, label in (
            (analysis.get("maximum_peak_dbfs"), "Peak"),
            (analysis.get("maximum_rms_dbfs"), "RMS"),
            (analysis.get("duration_seconds"), "Dauer"),
        ):
            if raw is not None and (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw))
            ):
                raise ControlError(f"Pegelabnahme lieferte einen ungültigen {label}-Wert.")
        peak = (
            float(analysis["maximum_peak_dbfs"])
            if analysis.get("maximum_peak_dbfs") is not None
            else None
        )
        rms = (
            float(analysis["maximum_rms_dbfs"])
            if analysis.get("maximum_rms_dbfs") is not None
            else None
        )
        duration = (
            float(analysis["duration_seconds"])
            if analysis.get("duration_seconds") is not None
            else None
        )
        channels = analysis.get("channels_analysis")
        if channels is not None:
            if not isinstance(channels, list) or not all(
                isinstance(item, dict) for item in channels
            ):
                raise ControlError("Pegelabnahme lieferte ungültige Kanaldaten.")
            clipping = any(
                isinstance(item.get("clipped_samples"), int)
                and not isinstance(item.get("clipped_samples"), bool)
                and item.get("clipped_samples", 0) > 0
                for item in channels
            )
    return {
        "passed": evidence["result"] == "pass",
        "blockers": list(blockers),
        "peak_dbfs": peak,
        "rms_dbfs": rms,
        "duration_seconds": duration,
        "clipping": clipping,
        "target_peak_dbfs": [-12.0, -6.0],
        "capture_seconds": VOICE_LEVEL_ACCEPTANCE_SECONDS,
        "audio_retained": False,
    }


def project_rate_policy_refresh(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "audio_policy_decision"
        or evidence.get("gate") != "rate-policy-decision"
        or evidence.get("result") not in {"pass", "fail"}
    ):
        raise ControlError("Rate-Policy-Prüfung lieferte einen fremden Vertrag.")
    blockers = evidence.get("blockers")
    if (
        not isinstance(blockers, list)
        or not all(isinstance(item, str) and item for item in blockers)
        or blockers != sorted(set(blockers))
        or (evidence["result"] == "pass") != (len(blockers) == 0)
    ):
        raise ControlError("Rate-Policy-Prüfung lieferte widersprüchliche Blocker.")
    return {
        "passed": evidence["result"] == "pass",
        "blockers": list(blockers),
        "default_graph_rate_hz": 48_000,
        "audio_effects": False,
    }


def project_resampling_refresh(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "audio_policy_decision"
        or evidence.get("gate") != "resampling-decision"
        or evidence.get("result") not in {"pass", "fail"}
    ):
        raise ControlError("Roland-Sampleratenprüfung lieferte einen fremden Vertrag.")
    blockers = evidence.get("blockers")
    if (
        not isinstance(blockers, list)
        or not all(isinstance(item, str) and item for item in blockers)
        or blockers != sorted(set(blockers))
        or (evidence["result"] == "pass") != (len(blockers) == 0)
    ):
        raise ControlError("Roland-Sampleratenprüfung lieferte widersprüchliche Blocker.")
    return {
        "passed": evidence["result"] == "pass",
        "blockers": list(blockers),
        "source_rate_hz": 44_100,
        "target_rate_hz": 48_000,
        "audio_effects": False,
    }


def prepare_laboratory_state_for_voice_level(
    laboratory: Any,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Prepare current lab state and bind it to the exact read preimage."""

    state_path = laboratory.DEFAULT_STATE
    physical_path = laboratory.PHYSICAL.DEFAULT_STATE
    if state_path.is_symlink():
        raise ControlError("Laborzustand ist kein vertrauenswürdiger regulärer Zustand.")
    expected_state_sha256: str | None = None
    if state_path.exists():
        try:
            expected_state_sha256 = laboratory.sha256_file(state_path)
        except OSError as error:
            raise ControlError("Laborzustand konnte nicht stabil gelesen werden.") from error
    try:
        state = laboratory.read_state(state_path)
    except ValueError as error:
        if str(error) != "audio profile catalog changed; review existing evidence":
            raise ControlError(
                "Laborzustand ist beschädigt oder nicht sicher migrierbar."
            ) from error
    else:
        try:
            observed_after = (
                laboratory.sha256_file(state_path) if state_path.exists() else None
            )
        except OSError as error:
            raise ControlError("Laborzustand änderte sich während des Lesens.") from error
        if observed_after != expected_state_sha256:
            raise ControlError("Laborzustand änderte sich während des Lesens.")
        return state, {
            "catalog_reconciled": False,
            "preserved_gates": [],
            "invalidated_gates": [],
        }, expected_state_sha256
    if (
        state_path.is_symlink()
        or not state_path.is_file()
        or stat.S_IMODE(state_path.stat().st_mode) != 0o600
    ):
        raise ControlError(
            "Veralteter Laborzustand ist nicht sicher archivierungsfähig."
        )
    try:
        stale = laboratory.load_json(
            state_path, maximum_bytes=laboratory.MAX_STATE_BYTES
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ControlError("Veralteter Laborzustand ist nicht sicher lesbar.") from error
    if (
        stale.get("schema_version") != 1
        or stale.get("kind") != "audio_laboratory_gate_state"
        or stale.get("catalog_sha256")
        != laboratory.sha256_file(laboratory.CATALOG_PATH)
        or not isinstance(stale.get("gates"), dict)
        or stale.get("profile_catalog_sha256")
        == laboratory.operational_profile_catalog_sha256()
    ):
        raise ControlError(
            "Laborzustand weicht über den bekannten Profilkatalog-Drift hinaus ab."
        )
    try:
        observed_after = laboratory.sha256_file(state_path)
    except OSError as error:
        raise ControlError("Laborzustand änderte sich während der Migration.") from error
    if expected_state_sha256 is None or observed_after != expected_state_sha256:
        raise ControlError("Laborzustand änderte sich während der Migration.")
    stale_sha = expected_state_sha256
    archive_path = state_path.with_name(
        f"{state_path.name}.stale-{stale_sha[:16]}.json"
    )
    if archive_path.exists():
        if archive_path.is_symlink() or laboratory.sha256_file(archive_path) != stale_sha:
            raise ControlError(
                "Vorhandenes Laborarchiv stimmt nicht mit dem veralteten Zustand überein."
            )
    else:
        try:
            os.link(state_path, archive_path, follow_symlinks=False)
        except OSError as error:
            raise ControlError(
                "Veralteter Laborzustand konnte nicht sicher archiviert werden."
            ) from error
    fresh = laboratory.empty_state()
    preserved: list[str] = []
    invalidated: list[str] = []
    for gate in sorted(stale["gates"]):
        receipt = stale["gates"].get(gate)
        evidence = receipt.get("evidence") if isinstance(receipt, dict) else None
        try:
            if (
                not isinstance(receipt, dict)
                or receipt.get("status") != "passed"
                or not isinstance(evidence, dict)
                or receipt.get("evidence_sha256")
                != laboratory.canonical_sha256(evidence)
            ):
                raise ValueError("invalid stale receipt")
            laboratory.validate_evidence(gate, evidence)
            laboratory.record_gate(fresh, gate, evidence, physical_path)
        except (KeyError, OSError, ValueError):
            invalidated.append(gate)
        else:
            preserved.append(gate)
    return fresh, {
        "catalog_reconciled": True,
        "preserved_gates": preserved,
        "invalidated_gates": invalidated,
    }, stale_sha


def verify_laboratory_state_preimage(laboratory: Any, expected_sha256: str | None) -> None:
    state_path = laboratory.DEFAULT_STATE
    if state_path.is_symlink():
        raise ActionBusy("Laborzustand wurde parallel verändert; Pegelabnahme bitte erneut starten.")
    try:
        observed = laboratory.sha256_file(state_path) if state_path.exists() else None
    except OSError as error:
        raise ActionBusy(
            "Laborzustand wurde parallel verändert; Pegelabnahme bitte erneut starten."
        ) from error
    if observed != expected_sha256:
        raise ActionBusy(
            "Laborzustand wurde parallel verändert; Pegelabnahme bitte erneut starten."
        )


def validate_recording_media_binding(report: dict[str, Any], session_id: str) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_product_media_binding"
        or report.get("session_id") != session_id
        or report.get("verified_current") is not True
        or not isinstance(report.get("path"), str)
        or not _valid_sha256(report.get("sha256"))
        or isinstance(report.get("bytes"), bool)
        or not isinstance(report.get("bytes"), int)
        or report.get("bytes") <= 44
        or report.get("mode") != "0600"
        or isinstance(report.get("device"), bool)
        or not isinstance(report.get("device"), int)
        or isinstance(report.get("inode"), bool)
        or not isinstance(report.get("inode"), int)
        or report.get("channels") != 2
        or report.get("sample_rate_hz") != 48_000
        or isinstance(report.get("duration_seconds"), bool)
        or not isinstance(report.get("duration_seconds"), (int, float))
        or not math.isfinite(float(report["duration_seconds"]))
        or not 0 < float(report["duration_seconds"]) <= 14_400
    ):
        raise ControlError("Take ist nicht als aktuelles unveränderliches Medium gebunden.")


def validate_recording_midi_binding(report: dict[str, Any], session_id: str) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "audio_recording_product_midi_binding"
        or report.get("session_id") != session_id
        or report.get("verified_current") is not True
        or not isinstance(report.get("path"), str)
        or not _valid_sha256(report.get("sha256"))
        or isinstance(report.get("bytes"), bool)
        or not isinstance(report.get("bytes"), int)
        or report.get("bytes") < 1
        or report.get("mode") != "0600"
        or isinstance(report.get("device"), bool)
        or not isinstance(report.get("device"), int)
        or isinstance(report.get("inode"), bool)
        or not isinstance(report.get("inode"), int)
    ):
        raise ControlError("Roland-MIDI ist nicht als aktuelles unveränderliches Medium gebunden.")


def is_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


def parse_request_authority(host_header: str, server_port: int) -> str | None:
    if not host_header or any(character.isspace() for character in host_header):
        return None
    try:
        parsed = urllib.parse.urlsplit(f"//{host_header}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        hostname not in {"127.0.0.1", "localhost"}
        or port != server_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname


def request_host_is_local(host_header: str, server_port: int) -> bool:
    return parse_request_authority(host_header, server_port) is not None


def origin_matches_request(
    origin: str | None,
    host_header: str,
    server_port: int,
) -> bool:
    request_hostname = parse_request_authority(host_header, server_port)
    if origin is None or request_hostname is None:
        return False
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == request_hostname
        and parsed.port == server_port
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def origin_is_local(origin: str | None, server_port: int) -> bool:
    """Compatibility helper for callers that use the canonical v1 authority."""
    return origin_matches_request(
        origin,
        f"{DEFAULT_HOST}:{server_port}",
        server_port,
    )


def read_static_file(filename: str) -> bytes:
    if filename not in {entry[0] for entry in STATIC_FILES.values()}:
        raise ControlError("UI-Datei ist nicht allowlistet.")
    target = UI_ROOT / filename
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise ControlError("UI-Datei ist nicht verfügbar.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATIC_BYTES:
            raise ControlError("UI-Datei verletzt den Größen- oder Typvertrag.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(MAX_STATIC_BYTES + 1)
        if len(body) > MAX_STATIC_BYTES:
            raise ControlError("UI-Datei überschreitet die Größenbegrenzung.")
        return body
    finally:
        os.close(descriptor)


def parse_single_byte_range(value: str, total_length: int) -> tuple[int, int]:
    """Parse one RFC 9110-style bytes range for a bounded static payload."""

    if (
        total_length <= 0
        or len(value.encode("latin-1", errors="replace")) > MAX_RANGE_HEADER_BYTES
        or any(character in value for character in "\r\n\0")
        or not value.startswith("bytes=")
    ):
        raise ValueError("invalid byte range")
    specification = value[6:]
    if "," in specification or specification.count("-") != 1:
        raise ValueError("multiple or malformed byte ranges are not supported")
    start_text, end_text = specification.split("-", 1)

    def decimal(text: str) -> int:
        if not text or not text.isascii() or not text.isdigit():
            raise ValueError("invalid byte range number")
        return int(text, 10)

    if not start_text:
        suffix_length = decimal(end_text)
        if suffix_length <= 0:
            raise ValueError("empty suffix range")
        start = max(0, total_length - suffix_length)
        return start, total_length - 1

    start = decimal(start_text)
    if start >= total_length:
        raise ValueError("range starts beyond payload")
    if not end_text:
        return start, total_length - 1
    end = decimal(end_text)
    if end < start:
        raise ValueError("range end precedes start")
    return start, min(end, total_length - 1)


def bounded_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Port muss eine ganze Zahl sein.") from error
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port muss zwischen 1024 und 65535 liegen.")
    return port


def bounded_runtime(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Laufzeit muss eine ganze Zahl sein."
        ) from error
    if not 60 <= seconds <= MAX_RUNTIME_SECONDS:
        raise argparse.ArgumentTypeError(
            f"Laufzeit muss zwischen 60 und {MAX_RUNTIME_SECONDS} Sekunden liegen."
        )
    return seconds


def valid_commit_revision(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _lexical_absolute_directory(path: pathlib.Path, *, label: str) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ControlError(f"{label} muss absolut sein.")
    return pathlib.Path(os.path.normpath(str(expanded)))


def _ensure_private_directory_chain(path: pathlib.Path, *, label: str) -> pathlib.Path:
    absolute = _lexical_absolute_directory(path, label=label)
    current = pathlib.Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        created = False
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                metadata = current.lstat()
                created = True
            except OSError as error:
                raise ControlError(f"{label} kann nicht sicher angelegt werden.") from error
        except OSError as error:
            raise ControlError(f"{label} kann nicht sicher geprüft werden.") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ControlError(f"{label} enthält eine unsichere Pfadkomponente.")
        if created and metadata.st_uid != os.getuid():
            raise ControlError(f"{label} wurde mit falschem Eigentümer angelegt.")
    final = absolute.lstat()
    if final.st_uid != os.getuid():
        raise ControlError(f"{label} gehört nicht dem aktuellen Nutzer.")
    absolute.chmod(0o700)
    return absolute


def ensure_laboratory_state_root() -> pathlib.Path:
    """Prepare the exact private laboratory root used by the service sandbox."""

    observed = _lexical_absolute_directory(
        LABORATORY_STATE_ROOT, label="Laboratory-State-Root"
    )
    expected = _lexical_absolute_directory(
        STATIC_LABORATORY_STATE_ROOT, label="Laboratory-State-Root"
    )
    if observed != expected:
        raise ControlError(
            "Laboratory-State-Root weicht vom statischen systemd-Schreibvertrag ab."
        )
    return _ensure_private_directory_chain(observed, label="Laboratory-State-Root")


def prepare_runtime_state_bootstrap() -> dict[str, Any]:
    """Prepare only the exact static service write roots, without audio effects."""

    bindings = (
        (RECORDING_OUTPUT_ROOT, STATIC_RECORDING_OUTPUT_ROOT, "Recorder-Ausgabe-Root"),
        (RECORDING_STATE_ROOT, STATIC_RECORDING_STATE_ROOT, "Recorder-State-Root"),
        (
            PROFILE_TRANSITION_STATE_ROOT,
            STATIC_PROFILE_TRANSITION_STATE_ROOT,
            "Transition-State-Root",
        ),
        (
            LABORATORY_STATE_ROOT,
            STATIC_LABORATORY_STATE_ROOT,
            "Laboratory-State-Root",
        ),
    )
    normalized: list[tuple[pathlib.Path, str]] = []
    for observed, expected, label in bindings:
        observed_root = _lexical_absolute_directory(observed, label=label)
        expected_root = _lexical_absolute_directory(expected, label=label)
        if observed_root != expected_root:
            raise ControlError(
                f"{label} weicht vom statischen systemd-Schreibvertrag ab."
            )
        normalized.append((observed_root, label))

    recording_output = _ensure_private_directory_chain(
        normalized[0][0], label=normalized[0][1]
    )
    recording_state = _ensure_private_directory_chain(
        normalized[1][0], label=normalized[1][1]
    )
    transition_state = _ensure_private_directory_chain(
        normalized[2][0], label=normalized[2][1]
    )
    laboratory_state = _ensure_private_directory_chain(
        normalized[3][0], label=normalized[3][1]
    )
    operations = _ensure_private_directory_chain(
        transition_state / "operations", label="Transition-Operations-Root"
    )
    if (
        recording_output != STATIC_RECORDING_OUTPUT_ROOT
        or recording_state != STATIC_RECORDING_STATE_ROOT
        or transition_state != STATIC_PROFILE_TRANSITION_STATE_ROOT
        or laboratory_state != STATIC_LABORATORY_STATE_ROOT
        or operations != transition_state / "operations"
    ):
        raise ControlError("Runtime-State-Bootstrap ist intern widersprüchlich.")
    return {
        "schema_version": 1,
        "kind": "audio_runtime_state_bootstrap",
        "status": "ready",
        "prepared_write_roots": 4,
        "private_state": True,
        "audio_mutated": False,
    }


def _source_checkout_present() -> bool:
    git_entry = ROOT / ".git"
    try:
        metadata = git_entry.lstat()
    except (FileNotFoundError, OSError):
        return False
    return not stat.S_ISLNK(metadata.st_mode) and (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    )


def _release_validation_tree_present() -> bool:
    try:
        root_info = ROOT.lstat()
        parent_info = ROOT.parent.lstat()
    except OSError:
        return False
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
    ):
        return False
    try:
        same_parent = ROOT.parent.resolve(strict=True) == DEPLOY_RELEASE_ROOT.resolve(strict=True)
    except OSError:
        return False
    return same_parent and re.fullmatch(r"\.[0-9a-f]{40}\..+", ROOT.name) is not None


def _sha256_bound_release_file(path: pathlib.Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ControlError("Gebundene Desktop-Transition-Datei ist nicht sicher lesbar.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlError("Gebundene Desktop-Transition-Datei ist nicht regulär.")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(131_072):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def verify_profile_transition_release_binding() -> dict[str, Any]:
    """Fail closed on deployed releases until the transition closure is bound."""

    try:
        marker_info = RELEASE_MARKER.lstat()
    except FileNotFoundError:
        if _source_checkout_present():
            return {"status": "source-checkout", "bound": False, "executable": True}
        if _release_validation_tree_present():
            return {"status": "release-validation", "bound": False, "executable": True}
        raise ControlError(
            "Desktop-Transition ist bis zur Release-Bindung nicht verfügbar."
        ) from None
    except OSError as error:
        raise ControlError("Release-Bindung der Desktop-Transition ist nicht lesbar.") from error
    if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
        raise ControlError("Release-Bindung der Desktop-Transition ist unsicher.")
    marker = read_bounded_json_object(
        RELEASE_MARKER,
        label="Audio-Control-Releasebeleg",
        maximum_bytes=MAX_RELEASE_MARKER_BYTES,
    )
    if marker.get("kind") != "audio_control_release":
        raise ControlError("Release-Bindung der Desktop-Transition hat falsche Identität.")
    critical = marker.get("critical_sha256")
    if not isinstance(critical, dict):
        raise ControlError(
            "Desktop-Transition ist bis zur vollständigen Release-Bindung blockiert."
        )
    for relative in sorted(PROFILE_TRANSITION_RELEASE_BINDING_FILES):
        expected = critical.get(relative)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ControlError(
                "Desktop-Transition ist bis zur vollständigen Release-Bindung blockiert."
            )
        target = ROOT.joinpath(*pathlib.PurePosixPath(relative).parts)
        if _sha256_bound_release_file(target) != expected:
            raise ControlError("Release-Bindung der Desktop-Transition ist gedriftet.")
    return {"status": "bound", "bound": True, "executable": True}


def release_marker_revision() -> str | None:
    try:
        metadata = RELEASE_MARKER.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "unavailable"
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return "unavailable"
    try:
        release = load_json_object(RELEASE_MARKER)
    except ControlError:
        return "unavailable"
    revision = release.get("commit")
    return revision if valid_commit_revision(revision) else "unavailable"


def current_revision(runner: CommandRunner) -> str:
    bound_revision = release_marker_revision()
    if bound_revision is not None:
        return bound_revision
    try:
        result = runner.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            timeout=2,
        )
    except ControlError:
        return "unavailable"
    revision = result.stdout.strip()
    if result.returncode == 0 and valid_commit_revision(revision):
        return revision
    return "unavailable"


def read_bounded_json_object(
    path: pathlib.Path, *, label: str, maximum_bytes: int
) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise ControlError(f"{label} fehlt.") from error
    except OSError as error:
        raise ControlError(f"{label} ist nicht lesbar.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ControlError(f"{label} hat eine unzulässige Form oder Größe.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise ControlError(f"{label} überschreitet die Größenbegrenzung.")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlError(f"{label} enthält kein gültiges JSON.") from error
        if not isinstance(value, dict):
            raise ControlError(f"{label} ist kein JSON-Objekt.")
        return value
    finally:
        os.close(descriptor)


def unix_timestamp_iso(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def deployment_projection(runtime_head: str) -> dict[str, Any]:
    base = {
        "mode": "automatic",
        "status": "unavailable",
        "source_ref": "origin/main",
        "runtime_unit": UNIT_NAME,
        "timer_unit": "audio-control-deploy.timer",
        "automatic": True,
        "in_sync": False,
        "runtime_commit": runtime_head if valid_commit_revision(runtime_head) else None,
        "receipt_commit": None,
        "last_sync_at": None,
        "release_changed": None,
        "service_health": None,
    }
    bound_revision = release_marker_revision()
    if bound_revision is None:
        return {
            **base,
            "mode": "source-checkout",
            "status": "source-checkout",
            "source_ref": "working-tree",
            "automatic": False,
        }
    if not valid_commit_revision(bound_revision):
        return base
    try:
        receipt = read_bounded_json_object(
            DEPLOY_LATEST,
            label="Deploy-Beleg",
            maximum_bytes=MAX_DEPLOY_RECEIPT_BYTES,
        )
    except ControlError:
        return base
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "audio_control_deploy_receipt"
    ):
        return base
    receipt_commit = receipt.get("commit")
    if not valid_commit_revision(receipt_commit):
        return base
    changed = receipt.get("changed")
    if not isinstance(changed, bool):
        changed = None
    service = receipt.get("service")
    health = service.get("health") if isinstance(service, dict) else None
    service_health = health.get("status") if isinstance(health, dict) else None
    if not isinstance(service_health, str) or len(service_health) > 40:
        service_health = None
    last_sync_at = unix_timestamp_iso(receipt.get("deployed_at_unix"))
    in_sync = (
        valid_commit_revision(runtime_head)
        and runtime_head == bound_revision
        and receipt_commit == runtime_head
        and service_health == "serving"
    )
    return {
        **base,
        "status": "current" if in_sync else "drift",
        "in_sync": in_sync,
        "receipt_commit": receipt_commit,
        "last_sync_at": last_sync_at,
        "release_changed": changed,
        "service_health": service_health,
    }


def qobuz_recovery_projection(
    runner: CommandRunner, *, required: bool
) -> dict[str, Any]:
    units = QOBUZ_RECOVERY_UNITS
    base = {
        "authority": "systemd-user-service-readback",
        "read_only": True,
        "required": required,
        "status": "not-required" if not required else "unavailable",
        "healthy": None if not required else False,
        "active_count": None,
        "total_count": len(units),
        "services": {},
    }
    if not required:
        return base

    argv = [
        "systemctl",
        "--user",
        "show",
        *units,
        "--property=Id",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--no-pager",
    ]
    try:
        result = runner.run(argv, timeout=2)
    except ControlError:
        return base
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 4096:
        return base

    expected_fields = {"Id", "LoadState", "ActiveState", "SubState"}
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if "=" not in line:
            return base
        key, value = line.split("=", 1)
        if (
            key not in expected_fields
            or key in current
            or re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", value) is None
        ):
            return base
        current[key] = value
    if current:
        records.append(current)
    if len(records) != len(units):
        return base

    services: dict[str, dict[str, Any]] = {}
    for record in records:
        if set(record) != expected_fields:
            return base
        unit = record["Id"]
        if unit not in units or unit in services:
            return base
        active = (
            record["LoadState"] == "loaded"
            and record["ActiveState"] == "active"
            and record["SubState"] == "running"
        )
        services[unit] = {
            "unit": unit,
            "load_state": record["LoadState"],
            "active_state": record["ActiveState"],
            "sub_state": record["SubState"],
            "active": active,
        }

    if set(services) != set(units):
        return base
    active_count = sum(service["active"] is True for service in services.values())
    healthy = active_count == len(units)
    return {
        **base,
        "status": "healthy" if healthy else "attention",
        "healthy": healthy,
        "active_count": active_count,
        "services": services,
    }


def hardware_projection(doctor_status: str, doctor: dict[str, Any]) -> dict[str, Any]:
    hardware = doctor.get("hardware") if isinstance(doctor, dict) else None
    hardware = hardware if isinstance(hardware, dict) else {}
    device_truth = doctor.get("device_truth") if isinstance(doctor, dict) else None
    desired = device_truth.get("desired") if isinstance(device_truth, dict) else None
    desired = desired if isinstance(desired, dict) else {}
    device_ids = ("motu_m2", "roland_fp_30x")
    observed = {device_id: hardware.get(device_id) is True for device_id in device_ids}
    expected = {device_id: desired.get(device_id) is True for device_id in device_ids}
    observed_count = sum(observed.values())
    desired_count = sum(expected.values())
    if doctor_status != "ok":
        state = "unavailable"
    elif desired_count == 0:
        state = "not-configured"
    elif observed_count == 0:
        state = "offline"
    elif observed_count < desired_count:
        state = "partial"
    else:
        state = "online"
    return {
        "state": state,
        "observed_count": observed_count,
        "desired_count": desired_count,
        "onsite_required": state in {"offline", "partial"},
        "observed": observed,
        "desired": expected,
    }


def project_profile_readiness(
    profiles: list[dict[str, Any]],
    *,
    hardware: dict[str, Any],
    physical_unknowns: list[str],
) -> list[dict[str, Any]]:
    observed = hardware.get("observed", {})
    unknown_facts = set(physical_unknowns)
    projected: list[dict[str, Any]] = []
    for profile in profiles:
        missing_hardware = [
            item
            for item in profile.get("required_hardware", [])
            if observed.get(item) is not True
        ]
        unresolved_physical = [
            item
            for item in profile.get("required_physical_facts", [])
            if item in unknown_facts
        ]
        laboratory_gates = list(profile.get("required_laboratory_gates", []))
        if profile.get("operational_status") == "planned":
            dashboard_state = "planned"
        elif missing_hardware or unresolved_physical:
            dashboard_state = "onsite"
        elif laboratory_gates:
            dashboard_state = "laboratory"
        elif profile.get("actionable") is True:
            dashboard_state = "executable"
        else:
            dashboard_state = "plan-ready"
        projected.append(
            {
                **profile,
                "dashboard_state": dashboard_state,
                "onsite_required": bool(missing_hardware or unresolved_physical),
                "missing_hardware_count": len(missing_hardware),
                "unresolved_physical_fact_count": len(unresolved_physical),
                "laboratory_gate_count": len(laboratory_gates),
            }
        )
    return projected


def read_profiles() -> list[dict[str, Any]]:
    catalog = load_json_object(PROFILE_CATALOG)
    profiles = catalog.get("profiles")
    if not isinstance(profiles, dict):
        raise ControlError("Audioprofilkatalog enthält kein profiles-Objekt.")
    summaries: list[dict[str, Any]] = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile_id, str) or not isinstance(profile, dict):
            raise ControlError("Audioprofilkatalog enthält einen ungültigen Eintrag.")
        if not profile_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in profile_id
        ):
            raise ControlError("Audioprofilkatalog enthält eine unsichere Profil-ID.")
        purpose = profile.get("purpose", profile_id)
        operational_status = profile.get("operational_status", "available")
        apply_authority = profile.get("apply_authority", "unknown")
        if not all(
            isinstance(value, str)
            for value in (purpose, operational_status, apply_authority)
        ):
            raise ControlError("Audioprofilkatalog enthält ungültige Textfelder.")
        required_hardware = require_list(
            profile.get("required_hardware", []),
            label=f"Audioprofil {profile_id}: required_hardware",
        )
        required_physical_facts = require_list(
            profile.get("required_physical_facts", []),
            label=f"Audioprofil {profile_id}: required_physical_facts",
        )
        required_laboratory_gates = require_list(
            profile.get("required_laboratory_gates", []),
            label=f"Audioprofil {profile_id}: required_laboratory_gates",
        )
        desired = require_mapping(
            profile.get("desired", {}),
            label=f"Audioprofil {profile_id}: desired",
        )
        operational_status = profile.get("operational_status", "available")
        summaries.append(
            {
                "id": profile_id,
                "area": PROFILE_AREAS.get(profile_id, "settings"),
                "purpose": purpose,
                "operational_status": operational_status,
                "apply_authority": apply_authority,
                "actionable": False,
                "plan_available": True,
                "blocker": profile.get("planned_blocker"),
                "required_hardware": required_hardware,
                "required_physical_facts": required_physical_facts,
                "required_laboratory_gates": required_laboratory_gates,
                "desired": desired,
            }
        )
    return summaries


def read_whale_contract() -> dict[str, Any]:
    profile = load_json_object(WHALE_PROFILE)
    if (
        profile.get("schema_version") != 3
        or profile.get("kind") != "buckelwal_live_voice_profile"
        or profile.get("voice_model")
        != "monophonic-continuous-source-derived-last-note-priority"
    ):
        raise ControlError("Buckelwalprofil enthält einen fremden Schemavertrag.")
    modes = profile.get("voice_modes")
    if not isinstance(modes, dict) or set(modes) != ALLOWED_WHALE_MODES:
        raise ControlError("Buckelwalprofil enthält keine Spielmodi.")
    default_mode = profile.get("default_voice_mode")
    if default_mode != "morph":
        raise ControlError("Buckelwalprofil enthält keinen gültigen Standardmodus.")
    for mode_id, mode in modes.items():
        if not isinstance(mode, dict) or not isinstance(mode.get("backend"), str):
            raise ControlError(f"Buckelwalmodus {mode_id} ist unvollständig.")
    morph = modes["morph"]
    morph_contract = {
        "backend": "source-derived-bandlimited-wavetable-morph",
        "manifest": "assets/whale-sources/morph/manifest.json",
        "note_range": [21, 108],
        "tuning": "twelve-tone-equal-temperament-a4-440",
        "keyboard_slot_count": 0,
        "preset_count": 0,
        "control_key_count": 0,
        "voice_count": 1,
        "source_anchor_count": 7,
        "permanent_noise_layer": False,
        "long_phrase_playback": False,
        "timbre_mapping": "continuous-source-anchor-morph-across-full-keyboard",
        "hold": "causal-continuous-articulation-development-without-sample-loop",
        "legato": "phase-continuous-frequency-and-timbre-glide",
        "detached_retrigger": "new-call-envelope-with-source-derived-cycle-reset",
        "repeated_note": "phase-preserving-rearticulation",
        "pitch_bend_range_semitones": 2,
    }
    if any(morph.get(key) != value for key, value in morph_contract.items()):
        raise ControlError("Buckelwalprofil verletzt den 88-Tasten-Morph-Vertrag.")
    organic = modes["organic"]
    organic_contract = {
        "backend": "source-derived-temporal-source-filter-organic-resynthesis",
        "base_backend": morph_contract["backend"],
        "manifest": morph_contract["manifest"],
        "voice_model_manifest": "assets/whale-sources/voice-model/manifest.json",
        "voice_model_manifest_sha256": "1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a",
        "trajectory_count": 19,
        "source_family_count": 8,
        "trajectory_selection": "gesture-seeded-family-then-clip-balanced",
        "evaluation": {
            "strategy": "leave-one-source-family-out-cross-validation",
            "family_weighting": "equal",
            "temporal_alignment": "normalized-48-point-sequence",
            "independent_test_claim": False,
        },
        "note_range": morph_contract["note_range"],
        "tuning": morph_contract["tuning"],
        "keyboard_slot_count": 0,
        "preset_count": 0,
        "control_key_count": 0,
        "voice_count": 1,
        "permanent_noise_layer": False,
        "long_phrase_playback": False,
        "organic_features": [
            "source-derived-temporal-envelope",
            "source-derived-periodicity-and-signal-coupled-roughness",
            "source-derived-spectral-tilt-harmonic-profile-and-resonance-emphasis-trajectories",
            "source-derived-pulse-subharmonic-and-secondary-frequency-trajectories",
            "register-aware-deep-bass-body",
            "deterministic-temporal-articulation-states",
            "anti-theremin-bounded-pitch-drift",
        ],
        "temporal_articulation": {
            "states": ["tonal", "pulsed", "rough", "broken"],
            "segment_seconds_range": [0.70, 0.96],
            "crossfade_seconds": 0.14,
            "sequence": "gesture-seeded-eight-segment-pattern",
            "excitation": "source-signal-coupled-without-independent-noise",
        },
        "low_register": {
            "full_strength_note_range": [21, 33],
            "taper_end_note": 55,
            "fundamental_binding": "played-note-frequency",
            "second_harmonic_ratio": 0.23,
            "subharmonic_policy": "source-trajectory-scaled-signal-gated",
            "subharmonic_control_cap": 0.55,
            "subharmonic_output_gain": 0.010,
        },
        "maximum_additional_pitch_drift_cents": 20,
        "comparison": "global-temporal-and-source-family-cross-validation-against-source-bound-real-clips",
        "hold": "per-trajectory-duration-with-continuous-unit-crossfade",
        "legato": "phase-continuous-short-frequency-timbre-and-resonance-glide",
        "detached_retrigger": "gesture-seeded-source-trajectory-family-with-bounded-state-reset",
        "repeated_note": "phase-preserving-pulsed-rearticulation",
        "pitch_bend_range_semitones": 2,
    }
    if any(organic.get(key) != value for key, value in organic_contract.items()):
        raise ControlError(
            "Buckelwalprofil verletzt den organischen 88-Tasten-Vertrag."
        )
    runtime = require_mapping(
        profile.get("runtime", {}),
        label="Buckelwal-Laufzeitvertrag",
    )
    audio = require_mapping(
        profile.get("audio", {}),
        label="Buckelwal-Audiovertrag",
    )
    truth_boundary = require_mapping(
        profile.get("truth_boundary", {}),
        label="Buckelwal-Wahrheitsgrenze",
    )
    if (
        truth_boundary.get("current_backend") != morph_contract["backend"]
        or truth_boundary.get("biological_voice_model_claim") is not False
        or truth_boundary.get("keyboard_contract")
        != "all-88-keys-are-standard-chromatic-notes-with-no-presets-zones-or-control-keys"
    ):
        raise ControlError("Buckelwalprofil verletzt seine Wahrheitsgrenze.")
    return {
        "name": profile.get("name", "Buckelwal Live Voice"),
        "default_mode": default_mode,
        "keyboard": {
            "key_count": 88,
            "lowest_key": "A0",
            "highest_key": "C8",
            "midi_note_range": morph_contract["note_range"],
            "mapping": "one-monophonic-continuously-morphed-voice",
        },
        "modes": [
            {
                "id": mode_id,
                "backend": mode.get("backend", "unknown")
                if isinstance(mode, dict)
                else "unknown",
                "status": mode.get("status") if isinstance(mode, dict) else None,
            }
            for mode_id, mode in modes.items()
        ],
        "runtime": runtime,
        "audio": audio,
        "truth_boundary": truth_boundary,
    }


class AudioControl:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        action_token: str | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        telemetry: Any = BUILD_DEFAULT_TELEMETRY,
        operating_mode_state_path: pathlib.Path = OPERATING_MODE_STATE_PATH,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.action_token = action_token or secrets.token_urlsafe(32)
        self.host = host
        self.port = port
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.operating_mode_state_path = operating_mode_state_path
        self._cache_lock = threading.Lock()
        self._truth_sequence = 0
        self._snapshot_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._plan_lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_snapshot: dict[str, Any] | None = None
        self.telemetry = (
            self._build_telemetry()
            if telemetry is BUILD_DEFAULT_TELEMETRY
            else telemetry
        )

    @staticmethod
    def _build_telemetry() -> Any | None:
        """A broken telemetry core must never keep the control service down."""

        try:
            return load_live_telemetry().build_default_hub()
        except Exception:
            return None

    def start_telemetry(self) -> dict[str, Any]:
        if self.telemetry is None:
            return {"state": "unavailable"}
        try:
            report = self.telemetry.start()
            self.telemetry.submit_command("service-start", {"port": self.port})
            return report
        except Exception as error:
            # Telemetry is passive and optional; the service keeps serving.
            return {"state": "unavailable", "error": str(error)[:200]}

    def stop_telemetry(self) -> dict[str, Any]:
        if self.telemetry is None:
            return {"state": "unavailable"}
        try:
            self.telemetry.submit_command("service-stop", {"port": self.port})
        except Exception:
            pass
        try:
            return self.telemetry.stop()
        except Exception as error:
            return {"state": "unavailable", "error": str(error)[:200]}

    def telemetry_snapshot(self) -> dict[str, Any]:
        if self.telemetry is None:
            raise ControlError("Live-Telemetrie ist auf diesem System nicht verfügbar.")
        try:
            return self.telemetry.snapshot()
        except Exception as error:
            raise ControlError(
                "Live-Telemetrie lieferte keinen lesbaren Zustand."
            ) from error

    def _doctor(self) -> tuple[str, dict[str, Any], str | None]:
        try:
            result = self.runner.run(
                [sys.executable, str(DOCTOR_SCRIPT)],
                timeout=30,
            )
            report = parse_json_output(result, label="Audio-Doctor")
            if result.returncode != 0:
                return (
                    "degraded",
                    {},
                    safe_error_message(
                        report, "Audio-Doctor meldet einen unlesbaren Zustand."
                    ),
                )
            validate_doctor_report(report)
            return "ok", report, None
        except ControlError as error:
            return "unavailable", {}, str(error)

    def _whale_status(self) -> tuple[str, dict[str, Any], str | None]:
        try:
            result = self.runner.run(
                [sys.executable, str(WHALE_SCRIPT), "status"],
                timeout=8,
            )
            report = parse_json_output(result, label="Buckelwal-Dienst")
            if result.returncode != 0:
                return (
                    "unavailable",
                    {},
                    safe_error_message(
                        report, "Buckelwal-Dienstzustand ist nicht lesbar."
                    ),
                )
            validate_whale_status(report)
            active = (
                report.get("load_state") == "loaded"
                and report.get("active_state") == "active"
                and report.get("sub_state") == "running"
            )
            report = {**report, "active": active}
            return "ok", report, None
        except ControlError as error:
            return "unavailable", {}, str(error)

    def _dauersong_status(self) -> tuple[str, dict[str, Any], str | None]:
        try:
            result = self.runner.run(
                [sys.executable, str(DAUERSONG_SCRIPT), "status"],
                timeout=8,
            )
            report = parse_json_output(result, label="Dauersong-Dienst")
            if result.returncode != 0:
                return (
                    "unavailable",
                    {},
                    safe_error_message(
                        report, "Dauersong-Dienstzustand ist nicht lesbar."
                    ),
                )
            validate_dauersong_status(report)
            return "ok", report, None
        except ControlError as error:
            return "unavailable", {}, str(error)

    def _recording_probe(self) -> tuple[str, dict[str, Any], str | None]:
        try:
            result = self.runner.run(
                [
                    sys.executable,
                    str(RECORDING_PRODUCT_SCRIPT),
                    "probe",
                    "--state-root",
                    str(RECORDING_STATE_ROOT),
                ],
                timeout=8,
            )
            report = parse_json_output(result, label="Recorderzustand")
            if result.returncode != 0:
                return (
                    "unavailable",
                    {},
                    safe_error_message(
                        report, "Recorderzustand ist nicht sicher lesbar."
                    ),
                )
            validate_recording_product_probe(report)
            return "ok", report, None
        except ControlError as error:
            return "unavailable", {}, str(error)

    def recording_library(self, *, limit: int = 64) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ControlError("Bibliothekslimit ist ungültig.")
        result = self.runner.run(
            [
                sys.executable,
                str(RECORDING_PRODUCT_SCRIPT),
                "library",
                "--state-root",
                str(RECORDING_STATE_ROOT),
                "--limit",
                str(limit),
            ],
            timeout=10,
        )
        report = parse_json_output(result, label="Recorderbibliothek")
        if result.returncode != 0:
            raise ControlError(
                safe_error_message(report, "Recorderbibliothek ist nicht sicher lesbar.")
            )
        validate_recording_library(report)
        projected = json.loads(json.dumps(report))
        for item in projected["items"]:
            if (
                item.get("status") == "completed"
                and item.get("library", {}).get("trashed") is not True
            ):
                item["audio_url"] = (
                    f"/api/{API_VERSION}/recordings/{item['session_id']}/audio"
                )
                if item.get("session_type") == "piano-vocal-performance":
                    item["midi_url"] = (
                        f"/api/{API_VERSION}/recordings/{item['session_id']}/midi"
                    )
        return projected

    def _recording_plan_once(
        self, *, mode: str, safe_name: str, duration: int
    ) -> dict[str, Any]:
        mode_spec = RECORDING_MODES[mode]
        result = self.runner.run(
            [
                sys.executable,
                str(RECORDING_SCRIPT),
                "plan",
                safe_name,
                "--session-type",
                mode_spec["session_type"],
                "--maximum-seconds",
                str(duration),
                "--root",
                str(RECORDING_OUTPUT_ROOT),
                "--state-root",
                str(RECORDING_STATE_ROOT),
            ],
            timeout=30,
        )
        report = parse_json_output(result, label="Recorderplan")
        if result.returncode != 0:
            raise ControlError(
                safe_error_message(report, "Recorderplan konnte nicht erstellt werden.")
            )
        projected = project_recording_plan(report, mode=mode)
        projected["contract"] = read_recording_contract(mode)
        return projected

    def _recording_recovery_unit_active(self, unit: str) -> bool:
        result = self.runner.run(
            ["systemctl", "--user", "is-active", unit], timeout=5
        )
        state = result.stdout.strip()
        if result.returncode == 0 and state == "active":
            return True
        if result.returncode in {3, 4} and state in {
            "inactive",
            "failed",
            "unknown",
            "not-found",
        }:
            return False
        raise ControlError(
            f"Dienstzustand für {unit} ist vor der Aufnahmepfad-Reparatur unklar."
        )

    def _recording_json_list(self, argv: list[str], *, label: str) -> list[Any]:
        result = self.runner.run(argv, timeout=5)
        if result.returncode != 0:
            raise ControlError(f"{label} konnte vor der Aufnahmepfad-Reparatur nicht geprüft werden.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControlError(f"{label} lieferte keinen gültigen JSON-Zustand.") from error
        if not isinstance(payload, list):
            raise ControlError(f"{label} lieferte keinen eindeutigen Listen-Zustand.")
        return payload

    def _recording_motu_source_count(self) -> int:
        _qobuz_recovery, motu_identity = load_recording_recovery_modules()
        sources = self._recording_json_list(
            ["pactl", "--format=json", "list", "sources"],
            label="MOTU-Quellenreadback",
        )
        count = 0
        try:
            for source in sources:
                if motu_identity.source_identity(source) is not None:
                    count += 1
        except ValueError as error:
            raise ControlError("MOTU-Quellenidentität ist widersprüchlich.") from error
        return count

    def _recording_assert_motu_pcm_safe(self) -> None:
        qobuz_recovery, _motu_identity = load_recording_recovery_modules()
        try:
            physical = qobuz_recovery.resolve_unique_motu_card(
                pathlib.Path("/proc/asound"),
                sound_class_root=pathlib.Path("/sys/class/sound"),
                sys_devices_root=pathlib.Path("/sys/devices"),
            )
            if physical is None:
                raise ControlError(
                    "MOTU M2 ist auf ALSA-Ebene nicht eindeutig vorhanden; kein Audio-Neustart."
                )
            qobuz_recovery.absence_observation_pcm_safe(
                physical.card, pathlib.Path("/proc")
            )
        except qobuz_recovery.RecoveryError as error:
            raise ControlError(
                "MOTU-PCM ist nicht sicher idle; der Audio-Graph bleibt unverändert."
            ) from error

    def _recording_assert_core_restart_idle(self) -> None:
        sink_inputs = self._recording_json_list(
            ["pactl", "--format=json", "list", "sink-inputs"],
            label="Wiedergabeaktivität",
        )
        source_outputs = self._recording_json_list(
            ["pactl", "--format=json", "list", "source-outputs"],
            label="Aufnahmeaktivität",
        )
        if sink_inputs or source_outputs:
            raise ControlError(
                "Der Audio-Graph wird nicht neu gestartet, solange andere Wiedergabe oder Aufnahme aktiv ist."
            )

    def _recording_wait_for_unique_motu_source(self) -> bool:
        for attempt in range(RECORDING_SOURCE_READBACK_ATTEMPTS):
            source_count = self._recording_motu_source_count()
            if source_count == 1:
                return True
            if source_count > 1:
                raise ControlError(
                    "Nach der Reparatur sind mehrere MOTU-Aufnahmequellen sichtbar; Start bleibt gesperrt."
                )
            if attempt + 1 < RECORDING_SOURCE_READBACK_ATTEMPTS:
                time.sleep(RECORDING_SOURCE_READBACK_INTERVAL_SECONDS)
        return False

    def _converge_recording_path(self) -> None:
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion verhindert die Aufnahmepfad-Reparatur.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert die sichere Aufnahmepfad-Reparatur."
                )
            try:
                originally_active: list[str] = []
                primary_error: ControlError | None = None
                restore_errors: list[str] = []
                try:
                    try:
                        source_count = self._recording_motu_source_count()
                        if source_count != 0:
                            raise ControlError(
                                "Der Quellenfehler ist nicht als fehlender MOTU-PipeWire-Knoten belegt; kein Audio-Neustart."
                            )

                        self._recording_assert_motu_pcm_safe()
                        originally_active = [
                            unit
                            for unit in RECORDING_PATH_RECOVERY_UNITS
                            if self._recording_recovery_unit_active(unit)
                        ]
                        precheck_active = [
                            unit
                            for unit in originally_active
                            if unit in RECORDING_PRECHECK_STOP_UNITS
                        ]
                        for unit in precheck_active:
                            result = self.runner.run(
                                ["systemctl", "--user", "stop", unit], timeout=10
                            )
                            if result.returncode != 0:
                                raise ControlError(
                                    f"{unit} konnte für die Aufnahmepfad-Reparatur nicht sicher angehalten werden."
                                )

                        self._recording_assert_core_restart_idle()
                        for unit in originally_active:
                            if unit in precheck_active:
                                continue
                            result = self.runner.run(
                                ["systemctl", "--user", "stop", unit], timeout=10
                            )
                            if result.returncode != 0:
                                raise ControlError(
                                    f"{unit} konnte für die Aufnahmepfad-Reparatur nicht sicher angehalten werden."
                                )

                        # The stop loop can take tens of seconds. Re-read both
                        # non-interference gates as the final pre-effect step so
                        # a client that appeared after the first gate is not torn down.
                        self._recording_assert_core_restart_idle()
                        self._recording_assert_motu_pcm_safe()

                        result = self.runner.run(
                            [
                                "systemctl",
                                "--user",
                                "restart",
                                *RECORDING_AUDIO_CORE_UNITS,
                            ],
                            timeout=20,
                        )
                        if result.returncode != 0:
                            raise ControlError(
                                "PipeWire/WirePlumber konnten den MOTU-Aufnahmepfad nicht neu aufbauen."
                            )
                        self._recording_wait_for_unique_motu_source()
                    except ControlError as error:
                        primary_error = error
                    finally:
                        for unit in reversed(originally_active):
                            try:
                                result = self.runner.run(
                                    ["systemctl", "--user", "start", unit], timeout=10
                                )
                            except ControlError:
                                restore_errors.append(unit)
                                continue
                            if result.returncode != 0:
                                restore_errors.append(unit)
                        self.invalidate()

                    if primary_error is not None:
                        raise primary_error
                    if restore_errors:
                        raise ControlError(
                            "Aufnahmepfad wurde neu aufgebaut, aber begleitende Audiodienste konnten nicht vollständig wiederhergestellt werden: "
                            + ", ".join(restore_errors)
                        )
                finally:
                    self.invalidate()
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()

    def recording_plan(
        self,
        *,
        mode: str = "voice",
        name: Any,
        maximum_seconds: Any,
        recover_source: bool = False,
    ) -> dict[str, Any]:
        mode_spec = RECORDING_MODES.get(mode)
        if mode_spec is None:
            raise ControlError("Unbekannter Aufnahmemodus.")
        safe_name = _validate_recording_name(name)
        duration = _validate_recording_duration(maximum_seconds, mode)
        if not self._plan_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audio- oder Recorderplanung läuft bereits.")
        try:
            projected = self._recording_plan_once(
                mode=mode, safe_name=safe_name, duration=duration
            )
            blockers = projected.get("readiness", {}).get("blockers", [])
            recoverable_source_blockers = {
                "motu-source-not-unique",
                "roland-audio-source-not-unique",
                "roland-midi-source-not-unique",
            }
            if (
                recover_source
                and projected.get("ready") is not True
                and isinstance(blockers, list)
                and "motu-source-not-unique" in blockers
                and set(blockers) <= recoverable_source_blockers
            ):
                self._converge_recording_path()
                projected = self._recording_plan_once(
                    mode=mode, safe_name=safe_name, duration=duration
                )
            return projected
        finally:
            self._plan_lock.release()

    def verified_recording_media(self, session_id: Any) -> dict[str, Any]:
        safe_id = _validate_recording_session_id(session_id)
        result = self.runner.run(
            [
                sys.executable,
                str(RECORDING_PRODUCT_SCRIPT),
                "media",
                "--state-root",
                str(RECORDING_STATE_ROOT),
                "--session-id",
                safe_id,
            ],
            timeout=30,
        )
        report = parse_json_output(result, label="Take-Readback")
        if result.returncode != 0:
            raise ControlError(
                safe_error_message(report, "Take ist nicht sicher abspielbar.")
            )
        validate_recording_media_binding(report, safe_id)
        return report

    def verified_recording_midi(self, session_id: Any) -> dict[str, Any]:
        safe_id = _validate_recording_session_id(session_id)
        result = self.runner.run(
            [
                sys.executable,
                str(RECORDING_PRODUCT_SCRIPT),
                "midi",
                "--state-root",
                str(RECORDING_STATE_ROOT),
                "--session-id",
                safe_id,
            ],
            timeout=30,
        )
        report = parse_json_output(result, label="Roland-MIDI-Readback")
        if result.returncode != 0:
            raise ControlError(
                safe_error_message(report, "Roland-MIDI ist nicht sicher exportierbar.")
            )
        validate_recording_midi_binding(report, safe_id)
        return report

    def _perform_recording_library_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation", ""))
        session_id = _validate_recording_session_id(payload.get("session_id"))
        expected_fields = (
            {"operation", "session_id", "category"}
            if operation == "categorize"
            else {"operation", "session_id"}
        )
        if set(payload) != expected_fields:
            raise ControlError("Bibliotheksaktion enthält unbekannte oder fehlende Felder.")
        category: str | None = None
        if operation == "categorize":
            category = payload.get("category")
            if category not in RECORDING_LIBRARY_CATEGORIES:
                raise ControlError("Unbekannte Aufnahmekategorie.")
        command = [
            sys.executable,
            str(RECORDING_PRODUCT_SCRIPT),
            operation,
            "--state-root",
            str(RECORDING_STATE_ROOT),
            "--session-id",
            session_id,
        ]
        if category is not None:
            command.extend(["--category", category])
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert den sicheren Bibliotheks-Readback."
                )
            try:
                self.invalidate()
                try:
                    result = self.runner.run(command, timeout=15)
                    report = parse_json_output(result, label="Recorderbibliotheksaktion")
                    if result.returncode != 0:
                        raise ControlError(
                            safe_error_message(report, "Bibliotheksaktion wurde blockiert.")
                        )
                    validate_recording_library_action(report, operation, session_id)
                    snapshot = self._readback_after_mutation()
                    targeted_result = self.runner.run(
                        [
                            sys.executable,
                            str(RECORDING_PRODUCT_SCRIPT),
                            "probe",
                            "--state-root",
                            str(RECORDING_STATE_ROOT),
                            "--session-id",
                            session_id,
                        ],
                        timeout=15,
                    )
                    targeted_report = parse_json_output(
                        targeted_result, label="Recorderbibliotheks-Readback"
                    )
                    if targeted_result.returncode != 0:
                        raise ControlError(
                            safe_error_message(
                                targeted_report,
                                "Bibliotheksaktion konnte nicht gezielt zurückgelesen werden.",
                            )
                        )
                    validate_recording_product_probe(targeted_report)
                except Exception:
                    try:
                        self._readback_after_mutation()
                    except Exception:
                        pass
                    raise
                current = targeted_report.get("session")
                if (
                    not isinstance(current, dict)
                    or current.get("session_id") != session_id
                    or current.get("library") != report["library"]
                ):
                    raise ControlError(
                        "Bibliotheksaktion wurde nicht durch aktuellen Bibliothekszustand bestätigt."
                    )
                return {
                    "schema_version": 1,
                    "kind": "audio_control_recording_action_result",
                    "operation": operation,
                    "session_id": session_id,
                    "changed": report["changed"],
                    "library": report["library"],
                    "snapshot": snapshot,
                }
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()


    def _reconcile_laboratory_before_voice_capture(
        self, laboratory: Any
    ) -> dict[str, Any]:
        """Persist a safely migratable laboratory catalog before opening the mic."""

        with laboratory.state_lock(laboratory.DEFAULT_STATE):
            (
                state,
                reconciliation,
                expected_laboratory_state_sha256,
            ) = prepare_laboratory_state_for_voice_level(laboratory)
            if not reconciliation["catalog_reconciled"]:
                return reconciliation
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert die sichere Labor-Migration."
                )
            try:
                verify_laboratory_state_preimage(
                    laboratory, expected_laboratory_state_sha256
                )
                laboratory.atomic_write_private(laboratory.DEFAULT_STATE, state)
                self.invalidate()
            finally:
                self._snapshot_lock.release()
            return reconciliation


    def _refresh_recording_rate_policy_gates(
        self, laboratory: Any, rate_policy: Any, state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            rate_policy_evidence = rate_policy.rate_policy_evidence(
                "rate-policy-decision"
            )
        except (OSError, ValueError):
            rate_policy_refresh = {
                "passed": False,
                "blockers": ["rate-policy-observation-unavailable"],
                "default_graph_rate_hz": 48_000,
                "audio_effects": False,
            }
        else:
            rate_policy_refresh = project_rate_policy_refresh(rate_policy_evidence)
            if rate_policy_refresh["passed"]:
                laboratory.validate_evidence(
                    "rate-policy-decision", rate_policy_evidence
                )
                laboratory.record_gate(
                    state,
                    "rate-policy-decision",
                    rate_policy_evidence,
                    laboratory.PHYSICAL.DEFAULT_STATE,
                    replace=True,
                )
        try:
            resampling_evidence = rate_policy.rate_policy_evidence(
                "resampling-decision"
            )
        except (OSError, ValueError):
            resampling_refresh = {
                "passed": False,
                "blockers": ["resampling-observation-unavailable"],
                "source_rate_hz": 44_100,
                "target_rate_hz": 48_000,
                "audio_effects": False,
            }
        else:
            resampling_refresh = project_resampling_refresh(resampling_evidence)
            if resampling_refresh["passed"]:
                laboratory.validate_evidence(
                    "resampling-decision", resampling_evidence
                )
                laboratory.record_gate(
                    state,
                    "resampling-decision",
                    resampling_evidence,
                    laboratory.PHYSICAL.DEFAULT_STATE,
                    replace=True,
                )
        return rate_policy_refresh, resampling_refresh


    def _perform_voice_level_acceptance(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"operation"}:
            raise ControlError("Pegelabnahme benötigt genau das Feld operation.")
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            recording_status, recording_probe, recording_error = self._recording_probe()
            session = (
                recording_probe.get("session")
                if isinstance(recording_probe, dict)
                else None
            )
            if (
                recording_status != "ok"
                or recording_probe.get("status") != "idle"
                or recording_probe.get("active_session_id") is not None
                or (isinstance(session, dict) and session.get("active") is True)
            ):
                raise ControlError(
                    recording_error
                    or "Pegelabnahme ist nur bei freiem Recorder möglich."
                )
            voice_capture, laboratory, rate_policy = load_voice_level_acceptance_modules()
            try:
                reconciliation = self._reconcile_laboratory_before_voice_capture(
                    laboratory
                )
            except (OSError, ValueError) as error:
                raise ControlError(
                    "Laborzustand konnte vor der Pegelabnahme nicht sicher migriert werden."
                ) from error
            try:
                with tempfile.TemporaryDirectory(
                    prefix="audio-control-level-acceptance-"
                ) as directory:
                    private = pathlib.Path(directory)
                    private.chmod(0o700)
                    evidence = voice_capture.capture_voice_evidence(
                        VOICE_LEVEL_ACCEPTANCE_SECONDS,
                        laboratory.PHYSICAL.DEFAULT_STATE,
                        private / "voice-level-reference.wav",
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise ControlError(
                    "Pegelabnahme konnte nicht sicher ausgeführt werden."
                ) from error
            measurement = project_voice_level_acceptance_evidence(evidence)
            try:
                with laboratory.state_lock(laboratory.DEFAULT_STATE):
                    (
                        state,
                        post_capture_reconciliation,
                        expected_laboratory_state_sha256,
                    ) = prepare_laboratory_state_for_voice_level(laboratory)
                    if post_capture_reconciliation["catalog_reconciled"]:
                        reconciliation = post_capture_reconciliation
                    state_changed = post_capture_reconciliation["catalog_reconciled"]
                    if measurement["passed"]:
                        laboratory.validate_evidence(
                            "voice-level-measurement", evidence
                        )
                        laboratory.record_gate(
                            state,
                            "voice-level-measurement",
                            evidence,
                            laboratory.PHYSICAL.DEFAULT_STATE,
                            replace=True,
                        )
                        state_changed = True
                    (
                        rate_policy_refresh,
                        resampling_refresh,
                    ) = self._refresh_recording_rate_policy_gates(
                        laboratory, rate_policy, state
                    )
                    state_changed = (
                        state_changed
                        or rate_policy_refresh["passed"]
                        or resampling_refresh["passed"]
                    )
                    if state_changed:
                        if not self._snapshot_lock.acquire(blocking=False):
                            raise ActionBusy(
                                "Eine Zustandsabfrage verhindert den sicheren Pegel-/Pfad-Readback."
                            )
                        try:
                            verify_laboratory_state_preimage(
                                laboratory, expected_laboratory_state_sha256
                            )
                            laboratory.atomic_write_private(
                                laboratory.DEFAULT_STATE, state
                            )
                            self.invalidate()
                            snapshot = self._readback_after_mutation()
                        finally:
                            self._snapshot_lock.release()
                    else:
                        snapshot = self.snapshot(refresh=True)
            except (OSError, ValueError) as error:
                raise ControlError(
                    "Pegel-/Pfadbeleg konnte nicht sicher an den Laborzustand gebunden werden."
                ) from error
            return {
                "schema_version": 1,
                "kind": "audio_control_recording_action_result",
                "operation": "measure-level",
                "measurement": measurement,
                "rate_policy_refresh": rate_policy_refresh,
                "resampling_refresh": resampling_refresh,
                "laboratory_reconciliation": reconciliation,
                "snapshot": snapshot,
            }
        finally:
            self._action_lock.release()

    def perform_recording_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ControlError("Recorderaktion muss ein JSON-Objekt sein.")
        operation = payload.get("operation")
        if operation not in (
            {"plan", "prepare", "start", "stop", "recover", "measure-level"}
            | RECORDING_LIBRARY_OPERATIONS
        ):
            raise ControlError("Unbekannte Recorderaktion.")
        if operation in RECORDING_LIBRARY_OPERATIONS:
            return self._perform_recording_library_action(payload)
        if operation == "measure-level":
            return self._perform_voice_level_acceptance(payload)
        if operation in {"plan", "prepare"}:
            if set(payload) != {"operation", "mode", "name", "maximum_seconds"}:
                raise ControlError("Recorderplan enthält unbekannte oder fehlende Felder.")
            mode = payload.get("mode")
            if mode not in RECORDING_MODES:
                raise ControlError("Unbekannter Aufnahmemodus.")
            plan = self.recording_plan(
                mode=mode,
                name=payload["name"],
                maximum_seconds=payload["maximum_seconds"],
                recover_source=operation == "prepare",
            )
            return {
                "schema_version": 1,
                "kind": "audio_control_recording_action_result",
                "operation": operation,
                "mode": mode,
                "plan": plan,
            }

        session_id: str | None = None
        if operation == "start":
            if set(payload) != {
                "operation",
                "mode",
                "name",
                "maximum_seconds",
                "expected_plan_sha256",
            }:
                raise ControlError("Recorderstart enthält unbekannte oder fehlende Felder.")
            mode = payload.get("mode")
            mode_spec = RECORDING_MODES.get(mode)
            if mode_spec is None:
                raise ControlError("Unbekannter Aufnahmemodus.")
            name = _validate_recording_name(payload["name"])
            duration = _validate_recording_duration(payload["maximum_seconds"], mode)
            expected_plan_sha256 = payload["expected_plan_sha256"]
            if not _valid_sha256(expected_plan_sha256):
                raise ControlError("Recorderstart benötigt einen gültigen Plan-Hash.")
            command = [
                sys.executable,
                str(RECORDING_SCRIPT),
                "start",
                name,
                "--session-type",
                mode_spec["session_type"],
                "--maximum-seconds",
                str(duration),
                "--root",
                str(RECORDING_OUTPUT_ROOT),
                "--state-root",
                str(RECORDING_STATE_ROOT),
                "--expected-plan-sha256",
                expected_plan_sha256,
            ]
        else:
            allowed = {"operation", "session_id"}
            if set(payload) - allowed or "operation" not in payload:
                raise ControlError("Recorderabschluss enthält unbekannte Felder.")
            raw_session_id = payload.get("session_id")
            if raw_session_id is not None:
                session_id = _validate_recording_session_id(raw_session_id)
            command = [
                sys.executable,
                str(RECORDING_SCRIPT),
                operation,
                "--state-root",
                str(RECORDING_STATE_ROOT),
            ]
            if session_id is not None:
                command.extend(["--session-id", session_id])

        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert den sicheren Recorder-Readback."
                )
            try:
                self.invalidate()
                try:
                    result = self.runner.run(command, timeout=45)
                    report = parse_json_output(result, label="Recorderaktion")
                    if result.returncode != 0:
                        raise ControlError(
                            safe_error_message(report, "Recorderaktion wurde blockiert.")
                        )
                except Exception:
                    try:
                        self._readback_after_mutation()
                    except Exception:
                        pass
                    raise
                if operation == "start":
                    session_id = _validate_recording_session_id(report.get("session_id"))
                    if (
                        report.get("kind") != "audio_recording_start_receipt"
                        or report.get("status") != "running"
                        or report.get("session_type") != mode_spec["session_type"]
                        or report.get("plan_sha256") != expected_plan_sha256
                    ):
                        raise ControlError("Recorderstart lieferte keinen gebundenen Startbeleg.")
                else:
                    reported_session_id = _validate_recording_session_id(
                        report.get("session_id")
                    )
                    if (
                        report.get("schema_version") != 1
                        or report.get("kind") != "audio_recording_status"
                        or report.get("session_type") not in RECORDING_SESSION_TYPES
                        or report.get("status")
                        not in {
                            "running",
                            "completed",
                            "failed-preserved",
                            "recovery-required",
                            "identity-mismatch",
                        }
                    ):
                        raise ControlError(
                            "Recorderabschluss lieferte keinen gebundenen Statusbeleg."
                        )
                    if session_id is not None and reported_session_id != session_id:
                        raise ControlError(
                            "Recorderabschluss bezog sich auf eine andere Sitzung."
                        )
                    session_id = reported_session_id
                snapshot = self._readback_after_mutation()
                recording = snapshot.get("recording", {})
                session = recording.get("session")
                if operation == "start":
                    if (
                        recording.get("status") != "running"
                        or not isinstance(session, dict)
                        or session.get("session_id") != session_id
                        or session.get("plan_sha256") != expected_plan_sha256
                        or session.get("session_type") != mode_spec["session_type"]
                        or session.get("active") is not True
                    ):
                        raise ControlError(
                            "Recorderstart wurde nicht durch aktuellen Recorderzustand bestätigt."
                        )
                elif (
                    isinstance(session, dict)
                    and session.get("session_id") == session_id
                    and session.get("active") is True
                ):
                    raise ControlError(
                        "Recorderabschluss wurde nicht durch aktuellen Recorderzustand bestätigt."
                    )
                verification: dict[str, Any] | None = None
                if operation in {"stop", "recover"}:
                    terminal_status = report["status"]
                    if terminal_status == "completed":
                        try:
                            media = self.verified_recording_media(session_id)
                            midi: dict[str, Any] | None = None
                            if report["session_type"] == "piano-vocal-performance":
                                midi = self.verified_recording_midi(session_id)
                        except ControlError:
                            verification = {
                                "status": "unverified",
                                "reason": "current-artifact-verification-failed",
                                "media": None,
                                "midi": None,
                            }
                        else:
                            verification = {
                                "status": "verified",
                                "reason": None,
                                "media": {
                                    key: media[key]
                                    for key in (
                                        "sha256",
                                        "bytes",
                                        "channels",
                                        "sample_rate_hz",
                                        "duration_seconds",
                                        "verified_current",
                                    )
                                },
                                "midi": (
                                    {
                                        key: midi[key]
                                        for key in ("sha256", "bytes", "verified_current")
                                    }
                                    if midi is not None
                                    else None
                                ),
                            }
                    else:
                        verification = {
                            "status": "not-completed",
                            "reason": f"terminal-status:{terminal_status}",
                            "media": None,
                            "midi": None,
                        }
                response = {
                    "schema_version": 1,
                    "kind": "audio_control_recording_action_result",
                    "operation": operation,
                    "session_id": session_id,
                    "snapshot": snapshot,
                }
                if verification is not None:
                    response["verification"] = verification
                return response
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()


    def _operating_mode_configuration(self) -> dict[str, Any]:
        return read_operating_mode_configuration(self.operating_mode_state_path)

    def _store_operating_mode_transition(
        self,
        configuration: dict[str, Any],
        *,
        request_id: str,
        target_mode: str,
        state: str,
        effect_started: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        return write_operating_mode_configuration(
            self.operating_mode_state_path,
            {
                "schema_version": 1,
                "kind": "audio_operating_mode_configuration",
                "configured_mode": configuration["configured_mode"],
                "transition": {
                    "request_id": request_id,
                    "from_mode": configuration["configured_mode"],
                    "target_mode": target_mode,
                    "state": state,
                    "effect_started": effect_started,
                    "reason": reason,
                },
                "last_request": configuration.get("last_request"),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )

    def _store_operating_mode_success(
        self,
        configuration: dict[str, Any],
        *,
        request_id: str,
        target_mode: str,
        audio_mutated: bool | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        receipt = {
            "request_id": request_id,
            "target_mode": target_mode,
            "status": "ready",
            "configuration_changed": configuration["configured_mode"] != target_mode,
            "audio_mutated": audio_mutated,
        }
        stored = write_operating_mode_configuration(
            self.operating_mode_state_path,
            {
                "schema_version": 1,
                "kind": "audio_operating_mode_configuration",
                "configured_mode": target_mode,
                "transition": None,
                "last_request": receipt,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        return stored, receipt

    def _clear_operating_mode_transition(
        self, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        return write_operating_mode_configuration(
            self.operating_mode_state_path,
            {
                "schema_version": 1,
                "kind": "audio_operating_mode_configuration",
                "configured_mode": configuration["configured_mode"],
                "transition": None,
                "last_request": configuration.get("last_request"),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )

    def _apply_desktop_operating_mode(self) -> dict[str, Any]:
        """Delegate the only routing effect to desktop-mixed-transition-v1."""

        try:
            transition = load_profile_transition()
        except ControlError as error:
            raise OperatingModeError(
                "desktop_transition_precondition_blocked",
                "Desktop-Transition ist vor jeder Wirkung nicht verfügbar: "
                + str(error),
            ) from error
        try:
            plan = transition.build_plan(
                "desktop-mixed",
                transition.PLANNER.PHYSICAL.DEFAULT_STATE,
                transition.PLANNER.LABORATORY.DEFAULT_STATE,
            )
        except transition.TransitionError as error:
            raise OperatingModeError(
                "desktop_transition_precondition_blocked",
                "Desktop-Pfad wurde vor jeder Wirkung von der bestehenden "
                "Profiltransition blockiert: " + error.detail,
            ) from error
        try:
            return transition.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                transition.PLANNER.PHYSICAL.DEFAULT_STATE,
                transition.PLANNER.LABORATORY.DEFAULT_STATE,
                transition.DEFAULT_STATE_ROOT,
            )
        except transition.TransitionError as error:
            raise OperatingModeError(
                "desktop_transition_blocked",
                "Desktop-Pfad wurde von der bestehenden Profiltransition blockiert: "
                + error.detail,
            ) from error

    def _verify_operating_mode_workloads_idle(self) -> None:
        recording_status, recording, recording_error = self._recording_probe()
        whale_status, whale, whale_error = self._whale_status()
        dauersong_status, dauersong, dauersong_error = self._dauersong_status()
        if recording_status != "ok":
            raise OperatingModeError(
                "operating_mode_authority_unavailable",
                recording_error or "Recorderzustand ist vor der Modustransition nicht lesbar.",
            )
        session = recording.get("session")
        if isinstance(session, dict) and (
            session.get("active") is True or session.get("recovery_required") is True
        ):
            raise OperatingModeError(
                "operating_mode_workload_active",
                "Eine aktive oder zu bergende Aufnahme blockiert die Hörmodustransition.",
            )
        if whale_status != "ok":
            raise OperatingModeError(
                "operating_mode_authority_unavailable",
                whale_error or "Buckelwalzustand ist vor der Modustransition nicht lesbar.",
            )
        if whale.get("active") is True:
            raise OperatingModeError(
                "operating_mode_workload_active",
                "Die aktive Buckelwalstimme blockiert die Hörmodustransition.",
            )
        if dauersong_status != "ok":
            raise OperatingModeError(
                "operating_mode_authority_unavailable",
                dauersong_error or "Dauersongzustand ist vor der Modustransition nicht lesbar.",
            )
        if dauersong.get("active") is True:
            raise OperatingModeError(
                "operating_mode_workload_active",
                "Der aktive Dauersong blockiert die Hörmodustransition.",
            )

    def _operating_mode_result(
        self,
        *,
        request_id: str,
        target_mode: str,
        receipt: dict[str, Any],
        idempotent: bool,
        reconciled_after_uncertain_effect: bool,
    ) -> dict[str, Any]:
        snapshot = self._readback_after_mutation()
        mode = snapshot.get("operating_mode")
        if (
            not isinstance(mode, dict)
            or mode.get("configured", {}).get("mode") != target_mode
            or mode.get("state") != "ready"
        ):
            raise OperatingModeError(
                "operating_mode_postcondition_failed",
                "Der Zielmodus wurde nicht durch den abschließenden System-Readback bestätigt.",
            )
        return {
            "schema_version": 1,
            "kind": "audio_operating_mode_transition_result",
            "request_id": request_id,
            "target_mode": target_mode,
            "status": "ready",
            "idempotent": idempotent,
            "configuration_changed": receipt["configuration_changed"],
            "audio_mutated": receipt["audio_mutated"],
            "reconciled_after_uncertain_effect": reconciled_after_uncertain_effect,
            "operating_mode": mode,
            "snapshot": snapshot,
        }

    def perform_operating_mode_transition(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"request_id", "target_mode"}:
            raise OperatingModeError(
                "operating_mode_request_invalid",
                "Die Modustransition benötigt genau request_id und target_mode.",
            )
        request_id = _validate_operating_mode_request_id(payload.get("request_id"))
        target_mode = payload.get("target_mode")
        if target_mode not in OPERATING_MODES:
            raise OperatingModeError(
                "operating_mode_request_invalid", "Unbekannter Betriebsmodus."
            )
        if OPERATING_MODES[target_mode]["actionable"] is not True:
            raise OperatingModeError(
                "operating_mode_not_executable",
                "Dieser Betriebsmodus ist erst deklariert und hat noch keine Wirkung.",
            )
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert den sicheren Modus-Readback."
                )
            try:
                self.invalidate()
                configuration = self._operating_mode_configuration()
                transition = configuration.get("transition")
                if isinstance(transition, dict):
                    if (
                        transition["request_id"] != request_id
                        or transition["target_mode"] != target_mode
                    ):
                        raise OperatingModeError(
                            "operating_mode_recovery_required",
                            "Eine unklare frühere Modustransition muss mit derselben Request-ID abgeglichen werden.",
                        )
                    doctor_status, doctor, doctor_error = self._doctor()
                    target_ready = operating_mode_target_ready(
                        target_mode, doctor_status, doctor
                    )
                    if not target_ready:
                        if transition["effect_started"]:
                            raise OperatingModeError(
                                "operating_mode_transition_uncertain",
                                doctor_error
                                or "Der frühere Mutationsausgang bleibt unklar; es wird keine Wirkung wiederholt.",
                            )
                        # The intent was persisted before any effect could start. Clearing it
                        # is safe and lets the same request resume normal precondition checks.
                        configuration = self._clear_operating_mode_transition(
                            configuration
                        )
                    else:
                        configuration, receipt = self._store_operating_mode_success(
                            configuration,
                            request_id=request_id,
                            target_mode=target_mode,
                            audio_mutated=None if transition["effect_started"] else False,
                        )
                        return self._operating_mode_result(
                            request_id=request_id,
                            target_mode=target_mode,
                            receipt=receipt,
                            idempotent=True,
                            reconciled_after_uncertain_effect=transition["effect_started"],
                        )

                previous = configuration.get("last_request")
                if isinstance(previous, dict) and previous["request_id"] == request_id:
                    if previous["target_mode"] != target_mode:
                        raise OperatingModeError(
                            "operating_mode_request_conflict",
                            "Die Request-ID wurde bereits für einen anderen Zielmodus verwendet.",
                        )
                    doctor_status, doctor, doctor_error = self._doctor()
                    if not operating_mode_target_ready(
                        target_mode, doctor_status, doctor
                    ):
                        raise OperatingModeError(
                            "operating_mode_postcondition_failed",
                            doctor_error
                            or "Der frühere Modusbeleg ist nicht mehr aktuell; es wird keine Wirkung wiederholt.",
                        )
                    return self._operating_mode_result(
                        request_id=request_id,
                        target_mode=target_mode,
                        receipt=previous,
                        idempotent=True,
                        reconciled_after_uncertain_effect=False,
                    )

                self._verify_operating_mode_workloads_idle()
                doctor_status, doctor, doctor_error = self._doctor()
                if doctor_status != "ok":
                    raise OperatingModeError(
                        "operating_mode_authority_unavailable",
                        doctor_error or "Audio-Doctor ist vor der Modustransition nicht lesbar.",
                    )
                if doctor.get("hardware", {}).get("motu_m2") is not True:
                    raise OperatingModeError(
                        "operating_mode_physical_blocked",
                        "Das MOTU M2 ist aktuell nicht physisch beobachtet; die Modustransition bleibt blockiert.",
                    )
                qobuz = _qobuz_projection(doctor)
                if target_mode == "desktop-listening" and qobuz["current_qbzd_playback"]:
                    raise OperatingModeError(
                        "qobuz_playback_must_stop",
                        "Qobuz spielt noch direkt über das MOTU. Wiedergabe zuerst in Qobuz stoppen; die Audiozentrale stoppt keinen fremden Player.",
                    )
                target_already_ready = operating_mode_target_ready(
                    target_mode, doctor_status, doctor
                )
                if target_mode == "qobuz-reference" and not target_already_ready:
                    reason = (
                        "QConnect befindet sich im Wiederaufbau."
                        if qobuz["qconnect_state"] in {"retrying", "reconnecting"}
                        else "QBZD/QConnect bestätigt den Referenzpfad noch nicht als bereit."
                    )
                    raise OperatingModeError("qobuz_reference_not_ready", reason)

                if target_already_ready:
                    configuration, receipt = self._store_operating_mode_success(
                        configuration,
                        request_id=request_id,
                        target_mode=target_mode,
                        audio_mutated=False,
                    )
                    return self._operating_mode_result(
                        request_id=request_id,
                        target_mode=target_mode,
                        receipt=receipt,
                        idempotent=configuration["configured_mode"] == target_mode
                        and receipt["configuration_changed"] is False,
                        reconciled_after_uncertain_effect=False,
                    )

                configuration = self._store_operating_mode_transition(
                    configuration,
                    request_id=request_id,
                    target_mode=target_mode,
                    state="transitioning",
                    effect_started=False,
                    reason=None,
                )
                configuration = self._store_operating_mode_transition(
                    configuration,
                    request_id=request_id,
                    target_mode=target_mode,
                    state="transitioning",
                    effect_started=True,
                    reason=None,
                )
                effect_result: dict[str, Any] | None = None
                effect_error: ControlError | None = None
                try:
                    effect_result = self._apply_desktop_operating_mode()
                except ControlError as error:
                    effect_error = error

                after_status, after_doctor, after_error = self._doctor()
                if operating_mode_target_ready(
                    target_mode, after_status, after_doctor
                ):
                    mutated = (
                        effect_result.get("mutated")
                        if isinstance(effect_result, dict)
                        and isinstance(effect_result.get("mutated"), bool)
                        else None
                    )
                    configuration, receipt = self._store_operating_mode_success(
                        configuration,
                        request_id=request_id,
                        target_mode=target_mode,
                        audio_mutated=mutated,
                    )
                    return self._operating_mode_result(
                        request_id=request_id,
                        target_mode=target_mode,
                        receipt=receipt,
                        idempotent=False,
                        reconciled_after_uncertain_effect=effect_error is not None,
                    )

                if (
                    isinstance(effect_error, OperatingModeError)
                    and effect_error.code
                    == "desktop_transition_precondition_blocked"
                ):
                    self._clear_operating_mode_transition(configuration)
                    self.invalidate()
                    raise effect_error

                reason = "authoritative-postcondition-missing"
                self._store_operating_mode_transition(
                    configuration,
                    request_id=request_id,
                    target_mode=target_mode,
                    state="recovering",
                    effect_started=True,
                    reason=reason,
                )
                self.invalidate()
                detail = (
                    str(effect_error)
                    if effect_error is not None
                    else after_error
                    or "Desktop-Readback bestätigt den Zielzustand nicht."
                )
                raise OperatingModeError(
                    "operating_mode_transition_uncertain",
                    detail + " Es wird keine Wirkung automatisch wiederholt.",
                )
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()


    def _build_snapshot(self) -> dict[str, Any]:
        profiles = read_profiles()
        whale_contract = read_whale_contract()
        doctor_status, doctor, doctor_error = self._doctor()
        whale_status, whale, whale_error = self._whale_status()
        dauersong_status, dauersong, dauersong_error = self._dauersong_status()
        recording_status, recording_probe, recording_error = self._recording_probe()
        recording_contract = read_voice_recording_contract()
        dauersong_active = dauersong.get("active") is True
        dauersong_runtime_safe = (
            dauersong_status == "ok" and dauersong.get("runtime_safe") is True
        )
        warnings = doctor.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = []
        high_warnings = [
            warning
            for warning in warnings
            if isinstance(warning, dict) and warning.get("severity") == "high"
        ]
        physical_unknowns = doctor.get("physical_unknowns", [])
        if not isinstance(physical_unknowns, list):
            physical_unknowns = []
        hardware = hardware_projection(doctor_status, doctor)
        recording_modes = [
            {
                "id": mode_id,
                "session_type": mode_spec["session_type"],
                "label": mode_spec["label"],
                "actionable": recording_status == "ok" and mode_id == "voice",
                "blocker": (
                    "roland-midi-source-not-observed"
                    if mode_id == "piano-vocal"
                    and hardware["observed"].get("roland_fp_30x") is not True
                    else "exact-midi-gate-requires-plan"
                    if mode_id == "piano-vocal"
                    else None
                ),
                "product": (
                    "Gesang WAV + Roland MIDI"
                    if mode_id == "piano-vocal"
                    else "Gesang WAV"
                ),
            }
            for mode_id, mode_spec in RECORDING_MODES.items()
        ]
        onsite_high_warnings = [
            warning
            for warning in high_warnings
            if warning.get("code") in ONSITE_WARNING_CODES
            and hardware["observed"].get("motu_m2") is not True
        ]
        runtime_high_warnings = [
            warning for warning in high_warnings if warning not in onsite_high_warnings
        ]
        runtime_head = current_revision(self.runner)
        deployment = deployment_projection(runtime_head)
        qobuz_recovery = qobuz_recovery_projection(
            self.runner, required=deployment.get("mode") == "automatic"
        )
        qobuz_recovery_attention = (
            qobuz_recovery.get("required") is True
            and qobuz_recovery.get("healthy") is not True
        )
        runtime_unavailable = doctor_status != "ok" or whale_status != "ok"
        dauersong_attention = (
            dauersong_status != "ok"
            or dauersong.get("hardening_ready") is False
            or dauersong.get("source_binding_ready") is False
            or not dauersong_runtime_safe
        )
        runtime_attention = (
            runtime_unavailable
            or bool(runtime_high_warnings)
            or dauersong_attention
            or qobuz_recovery_attention
        )
        runtime_state = (
            "unavailable"
            if runtime_unavailable
            else "attention"
            if runtime_high_warnings or dauersong_attention or qobuz_recovery_attention
            else "healthy"
        )
        projected_profiles = project_profile_readiness(
            profiles, hardware=hardware, physical_unknowns=physical_unknowns
        )
        operating_mode = project_operating_modes(
            self._operating_mode_configuration(),
            doctor_status=doctor_status,
            doctor=doctor,
            recording_status=recording_status,
            recording=recording_probe,
        )
        profile_state_counts: dict[str, int] = {}
        for profile in projected_profiles:
            state = profile["dashboard_state"]
            profile_state_counts[state] = profile_state_counts.get(state, 0) + 1
        onsite_required = hardware["onsite_required"] or bool(physical_unknowns)
        return {
            "schema_version": 1,
            "kind": "audio_control_snapshot",
            "api_version": API_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repository": {
                "runtime_head": runtime_head,
                "spec_base_revision": SPEC_BASE_REVISION,
            },
            "deployment": deployment,
            "qobuz_recovery": qobuz_recovery,
            "service": {
                "authority": "local-backend",
                "browser_audio_authority": False,
                "bind": self.host,
                "port": self.port,
                "managed_unit": UNIT_NAME,
                "action_token": self.action_token,
                "state_cache_seconds": self.cache_seconds,
            },
            "summary": {
                "state": "attention" if runtime_attention else "stable",
                "runtime_state": runtime_state,
                "qobuz_recovery_state": qobuz_recovery["status"],
                "hardware_state": hardware["state"],
                "operational_state": (
                    "attention"
                    if runtime_attention
                    else "ready-onsite-required"
                    if onsite_required
                    else "ready"
                ),
                "warning_count": len(warnings),
                "high_warning_count": len(high_warnings),
                "runtime_high_warning_count": len(runtime_high_warnings),
                "onsite_warning_count": len(onsite_high_warnings),
                "physical_unknown_count": len(physical_unknowns),
                "operating_mode_state": operating_mode["state"],
                "configured_operating_mode": operating_mode["configured"]["mode"],
                "active_whale": bool(whale.get("active")),
                "active_dauersong": dauersong_active,
                "dauersong_runtime_safe": dauersong_runtime_safe,
                "active_recording": bool(
                    isinstance(recording_probe.get("session"), dict)
                    and recording_probe["session"].get("active") is True
                ),
                "recording_recovery_required": bool(
                    isinstance(recording_probe.get("session"), dict)
                    and recording_probe["session"].get("recovery_required") is True
                ),
                "onsite_required": onsite_required,
                "profile_state_counts": profile_state_counts,
            },
            "presence": hardware,
            "operating_mode": operating_mode,
            "doctor": {
                "status": doctor_status,
                "error": doctor_error,
                "graph": doctor.get("graph", {}),
                "hardware": doctor.get("hardware", {}),
                "device_truth": doctor.get("device_truth", {}),
                "external_endpoints": doctor.get("external_endpoints", {}),
                "warnings": warnings,
                "physical_unknowns": physical_unknowns,
                "command_health": doctor.get("command_health", []),
                "read_only_contract": doctor.get("read_only_contract"),
            },
            "whale": {
                "status": whale_status,
                "error": whale_error,
                "service": whale,
                "contract": whale_contract,
                "actions": ["start", "mode", "stop"],
            },
            "profiles": projected_profiles,
            "dauersong": {
                "status": dauersong_status,
                "actionable": (
                    dauersong_status == "ok"
                    and dauersong.get("hardening_ready") is True
                    and dauersong.get("source_binding_ready") is True
                    and dauersong_runtime_safe
                ),
                "profile": "experimental",
                "error": dauersong_error,
                "detail": (
                    "Vorhandener Dauersong v9 hinter hashgebundener Quellenprüfung, "
                    "100-%-Streamgrenze und begrenzter systemd-Laufzeit."
                ),
                "service": dauersong,
                "actions": ["start", "stop", "recover"],
            },
            "recording": {
                "status": (
                    recording_probe.get("status", "unavailable")
                    if recording_status == "ok"
                    else "unavailable"
                ),
                "actionable": recording_status == "ok",
                "error": recording_error,
                "detail": (
                    "Hardened Voice-Recorder mit Plan-Hash, MOTU-Quellenbindung, "
                    "RØDE-/48-V-Gates, Pegelhinweisen, Recovery und unveränderlichen Takes."
                ),
                "authority": "recorder-plan-hash-and-current-readback",
                "contract": recording_contract,
                "modes": recording_modes,
                "session": recording_probe.get("session"),
                "active_session_id": recording_probe.get("active_session_id"),
                "actions": [
                    "plan",
                    "prepare",
                    "measure-level",
                    "start",
                    "stop",
                    "recover",
                    "categorize",
                    "trash",
                    "restore",
                ],
            },
            "capabilities": {
                "refresh_state": True,
                "profile_plan": True,
                "telemetry_replay": True,
                "live_telemetry": self.telemetry is not None,
                "whale_learning_lesson": True,
                "whale_control": True,
                "profile_apply": False,
                "operating_mode_transition": True,
                "recording_control": recording_status == "ok",
                "dauersong_control": dauersong_status == "ok",
            },
        }

    def _truth_projection(
        self, snapshot: dict[str, Any], cached_at: float
    ) -> dict[str, Any]:
        age_ms = max(0, int((self.clock() - cached_at) * 1000))
        projected = dict(snapshot)
        truth = dict(snapshot.get("truth_stream") or {})
        truth["age_ms"] = age_ms
        truth["freshness"] = "fresh" if age_ms == 0 else "cached"
        projected["truth_stream"] = truth
        return projected

    def _cache_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        errors = [
            str(section.get("error"))
            for section_name in ("doctor", "whale", "dauersong", "recording")
            if isinstance((section := snapshot.get(section_name)), dict)
            and section.get("error")
        ]
        with self._cache_lock:
            self._truth_sequence += 1
            stored = dict(snapshot)
            stored["truth_stream"] = {
                "sequence": self._truth_sequence,
                "generated_at": snapshot.get("generated_at"),
                "freshness": "fresh",
                "age_ms": 0,
                "cache_seconds": self.cache_seconds,
                "error": " · ".join(errors) if errors else None,
                "error_count": len(errors),
                "authoritative_for": "slow-system-truth-only",
            }
            self._cached_snapshot = stored
            self._cached_at = now
        return self._truth_projection(stored, now)

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        if not self._snapshot_lock.acquire(blocking=False):
            raise ActionBusy(
                "Ein anderer autoritativer Zustands- oder Aktions-Readback läuft bereits."
            )
        try:
            with self._cache_lock:
                if (
                    not refresh
                    and self._cached_snapshot is not None
                    and self.clock() - self._cached_at < self.cache_seconds
                ):
                    return self._truth_projection(
                        self._cached_snapshot, self._cached_at
                    )
            return self._cache_snapshot(self._build_snapshot())
        finally:
            self._snapshot_lock.release()

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cached_snapshot = None
            self._cached_at = 0.0

    def _readback_after_mutation(self) -> dict[str, Any]:
        try:
            return self._cache_snapshot(self._build_snapshot())
        except Exception:
            self.invalidate()
            raise

    def profile_plan(self, profile_id: str) -> dict[str, Any]:
        profiles = {profile["id"]: profile for profile in read_profiles()}
        if profile_id not in profiles:
            raise ControlError("Unbekanntes Audioprofil.")
        if not self._plan_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Profilplanung läuft bereits.")
        try:
            result = self.runner.run(
                [sys.executable, str(PLANNER_SCRIPT), profile_id],
                timeout=45,
            )
            report = parse_json_output(result, label="Profilplaner")
            if result.returncode != 0:
                raise ControlError(
                    safe_error_message(
                        report,
                        "Profilplan konnte nicht erstellt werden.",
                    )
                )
            validate_profile_plan(
                report,
                profile_id,
                profiles[profile_id]["apply_authority"],
            )
            return report
        finally:
            self._plan_lock.release()

    def perform_dauersong_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"operation"}:
            raise ControlError("Die Dauersong-Aktion benötigt genau das Feld operation.")
        operation = payload.get("operation")
        if operation not in {"start", "stop", "recover"}:
            raise ControlError("Unbekannte Dauersong-Aktion.")

        command = [sys.executable, str(DAUERSONG_SCRIPT), str(operation)]
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert den sicheren Aktions-Readback."
                )
            try:
                self.invalidate()
                try:
                    (
                        precondition_status,
                        precondition_service,
                        precondition_error,
                    ) = self._dauersong_status()
                    if precondition_status != "ok":
                        raise ControlError(
                            precondition_error
                            or "Dauersong-Dienstzustand ist nicht sicher lesbar."
                        )
                    if operation == "start":
                        if precondition_service.get("active") is True:
                            raise ControlError("Dauersong läuft bereits.")
                        if precondition_service.get("hardening_ready") is not True:
                            raise ControlError(
                                "Der repo-verwaltete Dauersong-Härtungsvertrag ist noch nicht aktiv."
                            )
                        if precondition_service.get("source_binding_ready") is not True:
                            raise ControlError(
                                "Die gebundene Dauersong-v9-Quelle stimmt nicht mit dem geprüften Stand überein."
                            )
                    elif operation == "recover":
                        if precondition_service.get("active") is True:
                            raise ControlError(
                                "Dauersong-Recovery ist nur im inaktiven Zustand erlaubt."
                            )

                    result = self.runner.run(command, timeout=25)
                    report = parse_json_output(result, label="Dauersong-Aktion")
                    if result.returncode != 0:
                        raise ControlError(
                            safe_error_message(report, "Dauersong-Aktion wurde blockiert.")
                        )
                except Exception:
                    try:
                        self._readback_after_mutation()
                    except Exception:
                        pass
                    raise

                snapshot = self._readback_after_mutation()
                song = snapshot.get("dauersong", {})
                service = song.get("service", {})
                status_ok = song.get("status") == "ok"
                active = service.get("active") is True
                if operation == "start":
                    stream = service.get("stream")
                    maximum = stream.get("max_volume_percent") if isinstance(stream, dict) else None
                    if (
                        not status_ok
                        or not active
                        or service.get("managed_by") != "audio-control-v1"
                        or service.get("hardening_ready") is not True
                        or service.get("configured_stream_volume_percent") != 100
                        or not isinstance(stream, dict)
                        or stream.get("found") is not True
                        or type(maximum) is not int
                        or maximum > 100
                    ):
                        raise ControlError(
                            "Dauersong-Start wurde nicht mit sicher begrenztem Audiostream bestätigt."
                        )
                elif not status_ok or active:
                    raise ControlError(
                        "Dauersong-Endzustand wurde nicht als inaktiv bestätigt."
                    )
                return {
                    "schema_version": 1,
                    "kind": "audio_control_dauersong_action_result",
                    "operation": operation,
                    "result": report,
                    "snapshot": snapshot,
                }
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()

    def perform_whale_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {"operation", "mode"}
        if set(payload) - allowed_keys:
            raise ControlError("Die Aktion enthält unbekannte Felder.")
        operation = payload.get("operation")
        if operation not in {"start", "mode", "stop"}:
            raise ControlError("Unbekannte Buckelwal-Aktion.")
        contract = read_whale_contract()
        modes = {mode["id"] for mode in contract["modes"]}
        mode = payload.get("mode")
        if operation == "start":
            if mode is None:
                mode = contract["default_mode"]
            if not isinstance(mode, str) or mode not in modes:
                raise ControlError("Der gewählte Buckelwalmodus ist nicht verfügbar.")
        elif operation == "mode":
            if not isinstance(mode, str) or mode not in modes:
                raise ControlError(
                    "Ein Moduswechsel benötigt einen verfügbaren Buckelwalmodus."
                )
        elif mode is not None:
            raise ControlError("Die Stop-Aktion akzeptiert keinen Modus.")

        command = [sys.executable, str(WHALE_SCRIPT)]
        if operation == "start":
            command.extend(["start", "--voice-mode", str(mode)])
        elif operation == "mode":
            command.extend(["mode", str(mode)])
        else:
            command.append("stop")

        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("Eine andere Audioaktion läuft bereits.")
        try:
            if not self._snapshot_lock.acquire(blocking=False):
                raise ActionBusy(
                    "Eine Zustandsabfrage verhindert den sicheren Aktions-Readback."
                )
            try:
                # The snapshot lock makes invalidation, mutation and readback one
                # truth boundary without changing the single-flight lock order.
                self.invalidate()
                try:
                    if operation == "mode":
                        (
                            precondition_status,
                            precondition_service,
                            precondition_error,
                        ) = self._whale_status()
                        if precondition_status != "ok":
                            raise ControlError(
                                precondition_error
                                or "Buckelwal-Dienstzustand ist nicht lesbar."
                            )
                        if precondition_service.get("active") is not True:
                            raise ControlError(
                                "Ein Moduswechsel benötigt eine bereits aktive "
                                "Walstimme."
                            )
                    result = self.runner.run(command, timeout=25)
                    report = parse_json_output(result, label="Buckelwal-Aktion")
                    if result.returncode != 0:
                        raise ControlError(
                            safe_error_message(
                                report, "Buckelwal-Aktion wurde blockiert."
                            )
                        )
                except Exception:
                    try:
                        self._readback_after_mutation()
                    except Exception:
                        # The action cause stays primary; the empty cache forces
                        # the next ordinary snapshot to retry the failed readback.
                        pass
                    raise
                snapshot = self._readback_after_mutation()
                whale = snapshot.get("whale", {})
                service = whale.get("service", {})
                status_ok = whale.get("status") == "ok"
                active = service.get("active") is True
                if operation in {"start", "mode"} and (
                    not status_ok or not active or service.get("voice_mode") != mode
                ):
                    raise ControlError(
                        "Buckelwal-Aktion wurde nicht durch den Dienststatus bestätigt."
                    )
                if operation == "stop" and (not status_ok or active):
                    raise ControlError(
                        "Buckelwal-Stop wurde nicht durch den Dienststatus bestätigt."
                    )
                return {
                    "schema_version": 1,
                    "kind": "audio_control_action_result",
                    "operation": operation,
                    "mode": mode,
                    "result": report,
                    "snapshot": snapshot,
                }
            finally:
                self._snapshot_lock.release()
        finally:
            self._action_lock.release()


class BoundedHeaderReader:
    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.remaining = limit

    def readline(self, size: int = -1) -> bytes:
        allowed = self.remaining + 1
        if size >= 0:
            allowed = min(allowed, size)
        line = self.stream.readline(allowed)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong("HTTP header section")
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class AudioControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(
        self,
        server_address: tuple[str, int],
        controller: AudioControl,
    ) -> None:
        self.controller = controller
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(server_address, AudioControlHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(REQUEST_IO_TIMEOUT_SECONDS)
        return request, client_address

    def verify_request(self, request: Any, client_address: Any) -> bool:
        del request
        return bool(client_address) and is_loopback_host(str(client_address[0]))

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class AudioControlHandler(BaseHTTPRequestHandler):
    server: AudioControlHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "AudioControl/1"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE_BYTES + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE_BYTES:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method_name = "do_" + self.command
            if not hasattr(self, method_name):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    f"Nicht unterstützte Methode: {self.command}",
                )
                return
            method = getattr(self, method_name)
            method()
            self.wfile.flush()
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def parse_request(self) -> bool:
        stream = self.rfile
        self.rfile = BoundedHeaderReader(stream, MAX_HEADER_BYTES)
        try:
            return super().parse_request()
        finally:
            self.rfile = stream

    def handle_expect_100(self) -> bool:
        self.close_connection = True
        self._send_error_json(
            HTTPStatus.EXPECTATION_FAILED,
            "expectation_not_supported",
            "Expect: 100-continue wird nicht unterstützt.",
        )
        return False

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del explain
        self.close_connection = True
        self._send_error_json(
            HTTPStatus(code),
            "http_request_rejected",
            message or HTTPStatus(code).phrase,
        )

    def log_message(self, format_string: str, *args: object) -> None:
        # Request lines and free-form error messages can contain user-controlled
        # text.  Keep access logs content-free: method + response metadata only.
        del format_string
        status = "-"
        size = "-"
        if len(args) >= 2 and isinstance(args[1], (str, int)):
            status = str(args[1])
        if len(args) >= 3 and isinstance(args[2], (str, int)):
            size = str(args[2])
        method = self.command if isinstance(self.command, str) else "-"
        if method not in {"GET", "HEAD", "POST", "OPTIONS"}:
            method = "-"
        sys.stderr.write(
            f"{self.client_address[0]} - - [{self.log_date_time_string()}] "
            f"{method} status={status} bytes={size}\n"
        )

    def _send_headers(
        self,
        status: HTTPStatus,
        *,
        content_type: str,
        content_length: int,
        cache_control: str = "no-store",
        etag: str | None = None,
        accept_ranges: str | None = None,
        content_range: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache_control)
        self.send_header(
            "Content-Security-Policy",
            # worker-src 'self' erlaubt ausschließlich den gleichherkünftigen
            # App-Shell-Service-Worker. Alle übrigen Direktiven bleiben eng.
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; "
            "object-src 'none'; media-src 'self'; worker-src 'self'; "
            "manifest-src 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), display-capture=()",
        )
        if self.close_connection:
            self.send_header("Connection", "close")
        if etag:
            self.send_header("ETag", etag)
        if accept_ranges:
            self.send_header("Accept-Ranges", accept_ranges)
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._send_headers(
            status,
            content_type="application/json; charset=utf-8",
            content_length=len(body),
        )
        if not head_only:
            self.wfile.write(body)

    def _send_error_json(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        head_only: bool = False,
    ) -> None:
        self._send_json(
            status,
            {
                "schema_version": 1,
                "kind": "audio_control_error",
                "api_version": API_VERSION,
                "error": {
                    "code": code,
                    "message": message,
                },
            },
            head_only=head_only or self.command == "HEAD",
        )

    def _request_host_is_local(self) -> bool:
        if len(self.headers.get_all("Host", [])) != 1:
            return False
        return request_host_is_local(
            self.headers.get("Host", ""),
            self.server.server_port,
        )

    def _origin_is_local(self) -> bool:
        if len(self.headers.get_all("Origin", [])) != 1:
            return False
        return origin_matches_request(
            self.headers.get("Origin"),
            self.headers.get("Host", ""),
            self.server.server_port,
        )

    def _reject_nonlocal_host(self) -> bool:
        if self._request_host_is_local():
            return False
        self.close_connection = True
        self._send_error_json(
            HTTPStatus.MISDIRECTED_REQUEST,
            "invalid_host",
            "Nur die exakte lokale Dienstadresse ist erlaubt.",
        )
        return True

    def _serve_static(self, path: str, *, head_only: bool) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "Nicht gefunden.",
                head_only=head_only,
            )
            return
        try:
            body = read_static_file(entry[0])
        except ControlError:
            self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "static_unavailable",
                "UI-Datei ist nicht verfügbar.",
                head_only=head_only,
            )
            return
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        is_audio = entry[1] == "audio/wav"
        if self.headers.get("If-None-Match") == etag:
            self._send_headers(
                HTTPStatus.NOT_MODIFIED,
                content_type="application/octet-stream",
                content_length=0,
                cache_control="no-cache",
                etag=etag,
                accept_ranges="bytes" if is_audio else None,
            )
            return

        range_values = self.headers.get_all("Range", []) if is_audio else []
        if range_values:
            try:
                if len(range_values) != 1:
                    raise ValueError("duplicate byte range")
                start, end = parse_single_byte_range(range_values[0], len(body))
            except ValueError:
                self._send_headers(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    content_type=entry[1],
                    content_length=0,
                    cache_control="no-cache",
                    etag=etag,
                    accept_ranges="bytes",
                    content_range=f"bytes */{len(body)}",
                )
                return
            partial = body[start : end + 1]
            self._send_headers(
                HTTPStatus.PARTIAL_CONTENT,
                content_type=entry[1],
                content_length=len(partial),
                cache_control="no-cache",
                etag=etag,
                accept_ranges="bytes",
                content_range=f"bytes {start}-{end}/{len(body)}",
            )
            if not head_only:
                self.wfile.write(partial)
            return

        self._send_headers(
            HTTPStatus.OK,
            content_type=entry[1],
            content_length=len(body),
            cache_control="no-cache",
            etag=etag,
            accept_ranges="bytes" if is_audio else None,
        )
        if not head_only:
            self.wfile.write(body)

    def _serve_recording_artifact(
        self, session_id: str, *, artifact: str, head_only: bool
    ) -> None:
        if artifact not in {"audio", "midi"}:
            raise ControlError("Unbekanntes Take-Artefakt.")
        content_type = "audio/wav" if artifact == "audio" else "audio/midi"
        try:
            binding = (
                self.server.controller.verified_recording_media(session_id)
                if artifact == "audio"
                else self.server.controller.verified_recording_midi(session_id)
            )
        except ControlError as error:
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "recording_media_unavailable",
                str(error),
                head_only=head_only,
            )
            return
        descriptor: int | None = None
        response_started = False
        try:
            descriptor = os.open(
                binding["path"],
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != binding["device"]
                or metadata.st_ino != binding["inode"]
                or metadata.st_size != binding["bytes"]
            ):
                raise ControlError("Take änderte sich zwischen Verifikation und Öffnen.")
            digest = hashlib.sha256()
            observed_bytes = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                observed_bytes += len(chunk)
                digest.update(chunk)
            if observed_bytes != binding["bytes"] or digest.hexdigest() != binding["sha256"]:
                raise ControlError("Take-Inhalt änderte sich nach dem Recorder-Readback.")
            os.lseek(descriptor, 0, os.SEEK_SET)
            etag = f'"{binding["sha256"]}"'
            if self.headers.get("If-None-Match") == etag:
                self._send_headers(
                    HTTPStatus.NOT_MODIFIED,
                    content_type=content_type,
                    content_length=0,
                    cache_control="no-cache",
                    etag=etag,
                    accept_ranges="bytes",
                )
                return
            start = 0
            end = binding["bytes"] - 1
            status = HTTPStatus.OK
            range_values = self.headers.get_all("Range", [])
            if range_values:
                try:
                    if len(range_values) != 1:
                        raise ValueError("duplicate byte range")
                    start, end = parse_single_byte_range(
                        range_values[0], binding["bytes"]
                    )
                except ValueError:
                    self._send_headers(
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        content_type=content_type,
                        content_length=0,
                        cache_control="no-cache",
                        etag=etag,
                        accept_ranges="bytes",
                        content_range=f"bytes */{binding['bytes']}",
                    )
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            response_started = True
            self._send_headers(
                status,
                content_type=content_type,
                content_length=length,
                cache_control="no-cache",
                etag=etag,
                accept_ranges="bytes",
                content_range=(
                    f"bytes {start}-{end}/{binding['bytes']}"
                    if status == HTTPStatus.PARTIAL_CONTENT
                    else None
                ),
            )
            if head_only:
                return
            os.lseek(descriptor, start, os.SEEK_SET)
            remaining = length
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    raise ConnectionError("Take endete während der Ausgabe unerwartet.")
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (ControlError, OSError) as error:
            if not response_started and not self.wfile.closed:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    "recording_media_changed",
                    str(error),
                    head_only=head_only,
                )
            else:
                self.close_connection = True
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _get(self, *, head_only: bool = False) -> None:
        if self._reject_nonlocal_host():
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_request_target",
                "Ungültiges Anfrageziel.",
                head_only=head_only,
            )
            return
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_request_target",
                "Nur lokale origin-form-Anfrageziele sind erlaubt.",
                head_only=head_only,
            )
            return
        if parsed.path == f"/api/{API_VERSION}/health":
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Der Health-Endpunkt akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "schema_version": 1,
                    "kind": "audio_control_health",
                    "status": "serving",
                    "api_version": API_VERSION,
                    "authority": "local-backend",
                    "runtime_head": current_revision(self.server.controller.runner),
                },
                head_only=head_only,
            )
            return
        if parsed.path == f"/api/{API_VERSION}/telemetry":
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Der Telemetrieendpunkt akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            try:
                telemetry = self.server.controller.telemetry_snapshot()
            except ControlError as error:
                self._send_error_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "telemetry_unavailable",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, telemetry, head_only=head_only)
            return
        if parsed.path == f"/api/{API_VERSION}/replay":
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Der Replay-Endpunkt akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            try:
                replay = TELEMETRY_REPLAY.load_replay_contract()
            except TELEMETRY_REPLAY.ReplayError as error:
                self._send_error_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "replay_unavailable",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, replay, head_only=head_only)
            return
        if parsed.path == f"/api/{API_VERSION}/whale/lesson":
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Der Buckelwal-Lektionsendpunkt akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            try:
                lesson = WHALE_LESSON.load_lesson_contract()
            except WHALE_LESSON.LessonError as error:
                self._send_error_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "whale_lesson_unavailable",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, lesson, head_only=head_only)
            return
        if parsed.path == f"/api/{API_VERSION}/recordings":
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Die Recorderbibliothek akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            try:
                library = self.server.controller.recording_library()
            except ControlError as error:
                self._send_error_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "recording_library_unavailable",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, library, head_only=head_only)
            return
        recording_media = RECORDING_MEDIA_PATH_RE.fullmatch(parsed.path)
        if recording_media is not None:
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Take-Wiedergabe akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            session_id, recording_artifact = recording_media.groups()
            try:
                _validate_recording_session_id(session_id)
            except ControlError:
                self._send_error_json(
                    HTTPStatus.NOT_FOUND,
                    "unknown_recording",
                    "Unbekannter Take.",
                    head_only=head_only,
                )
                return
            self._serve_recording_artifact(
                session_id, artifact=recording_artifact, head_only=head_only
            )
            return
        if parsed.path == f"/api/{API_VERSION}/snapshot":
            if parsed.query not in {"", "refresh=1"}:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Erlaubt ist ausschließlich refresh=1.",
                    head_only=head_only,
                )
                return
            refresh = parsed.query == "refresh=1"
            try:
                snapshot = self.server.controller.snapshot(refresh=refresh)
            except ActionBusy as error:
                self._send_error_json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "snapshot_busy",
                    str(error),
                    head_only=head_only,
                )
                return
            except ControlError as error:
                self._send_error_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "snapshot_unavailable",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, snapshot, head_only=head_only)
            return
        plan_prefix = f"/api/{API_VERSION}/profiles/"
        plan_suffix = "/plan"
        if parsed.path.startswith(plan_prefix) and parsed.path.endswith(plan_suffix):
            if parsed.query:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "Der Profilplan akzeptiert keine Query.",
                    head_only=head_only,
                )
                return
            encoded_id = parsed.path[len(plan_prefix) : -len(plan_suffix)]
            profile_id = urllib.parse.unquote(encoded_id)
            if "/" in profile_id or not profile_id:
                self._send_error_json(
                    HTTPStatus.NOT_FOUND,
                    "unknown_profile",
                    "Unbekanntes Audioprofil.",
                    head_only=head_only,
                )
                return
            try:
                plan = self.server.controller.profile_plan(profile_id)
            except ActionBusy as error:
                self._send_error_json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "profile_plan_busy",
                    str(error),
                    head_only=head_only,
                )
                return
            except ControlError as error:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    "profile_plan_blocked",
                    str(error),
                    head_only=head_only,
                )
                return
            self._send_json(HTTPStatus.OK, plan, head_only=head_only)
            return
        if parsed.query:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_query",
                "Statische Pfade akzeptieren keine Query.",
                head_only=head_only,
            )
            return
        self._serve_static(parsed.path, head_only=head_only)

    def do_GET(self) -> None:
        self._get()

    def do_HEAD(self) -> None:
        self._get(head_only=True)

    def do_POST(self) -> None:
        if self._reject_nonlocal_host():
            self.close_connection = True
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            parsed = urllib.parse.SplitResult("", "", "", "", "")
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or parsed.path not in {
                f"/api/{API_VERSION}/actions/whale",
                f"/api/{API_VERSION}/actions/dauersong",
                f"/api/{API_VERSION}/actions/recording",
                f"/api/{API_VERSION}/actions/operating-mode",
            }
            or parsed.query
        ):
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "Nicht gefunden.",
            )
            return
        if not self._origin_is_local():
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "invalid_origin",
                "Der Ursprung fehlt oder stimmt nicht mit dem lokalen Host überein.",
            )
            return
        content_types = self.headers.get_all("Content-Type", [])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "invalid_content_type",
                "Content-Type application/json ist erforderlich.",
            )
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "transfer_encoding_rejected",
                "Transfer-Encoding wird nicht akzeptiert.",
            )
            return
        tokens = self.headers.get_all("X-Audio-Control-Token", [])
        token = tokens[0] if len(tokens) == 1 else ""
        if not secrets.compare_digest(token or "", self.server.controller.action_token):
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "invalid_action_token",
                "Ungültiger Aktionstoken.",
            )
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        try:
            content_length = (
                int(content_lengths[0], 10)
                if len(content_lengths) == 1
                and content_lengths[0].isascii()
                and content_lengths[0].isdigit()
                else -1
            )
        except ValueError:
            content_length = -1
        if not 0 < content_length <= MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "invalid_content_length",
                "Ungültige Anfragegröße.",
            )
            return
        try:
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise ValueError("truncated request body")
            payload = json.loads(body)
        except TimeoutError:
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "Der Anfragekörper wurde nicht rechtzeitig vollständig übertragen.",
            )
            return
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.close_connection = True
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Anfrage enthält kein vollständiges gültiges JSON.",
            )
            return
        if not isinstance(payload, dict):
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_json_type",
                "Anfrage muss ein JSON-Objekt sein.",
            )
            return
        try:
            if parsed.path == f"/api/{API_VERSION}/actions/recording":
                result = self.server.controller.perform_recording_action(payload)
            elif parsed.path == f"/api/{API_VERSION}/actions/dauersong":
                result = self.server.controller.perform_dauersong_action(payload)
            elif parsed.path == f"/api/{API_VERSION}/actions/operating-mode":
                result = self.server.controller.perform_operating_mode_transition(payload)
            else:
                result = self.server.controller.perform_whale_action(payload)
        except ActionBusy as error:
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "audio_action_busy",
                str(error),
            )
            return
        except OperatingModeError as error:
            self._send_error_json(
                HTTPStatus.CONFLICT,
                error.code,
                str(error),
            )
            return
        except ControlError as error:
            self._send_error_json(
                HTTPStatus.CONFLICT,
                "audio_action_blocked",
                str(error),
            )
            return
        self._send_json(HTTPStatus.OK, result)


def notify_systemd_ready(status: str) -> bool:
    if "\n" in status or "\r" in status:
        raise ValueError("systemd status must be one line")
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    payload = f"READY=1\nSTATUS={status}".encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        sent = notifier.sendto(payload, address)
    if sent != len(payload):
        raise ControlError("systemd readiness notification was truncated")
    return True


def systemd_status(runner: CommandRunner) -> dict[str, Any]:
    result = runner.run(
        [
            "systemctl",
            "--user",
            "show",
            UNIT_NAME,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Result",
            "--property=ExecMainStatus",
            "--property=Environment",
        ],
        timeout=8,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if result.returncode != 0 and values.get("LoadState") != "not-found":
        raise ControlError("Control-Dienstzustand ist nicht lesbar.")
    if "LoadState" not in values or "ActiveState" not in values:
        raise ControlError("systemd lieferte keinen vollständigen Dienstzustand.")
    environment: dict[str, str] = {}
    for item in values.get("Environment", "").split():
        if "=" in item:
            key, value = item.split("=", 1)
            environment[key] = value
    return {
        "unit": UNIT_NAME,
        "managed_by": environment.get("AUDIO_CONTROL_MANAGED_BY"),
        "load_state": values.get("LoadState", "unknown"),
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "result": values.get("Result", "unknown"),
        "exec_main_status": values.get("ExecMainStatus", "unknown"),
        "host": environment.get("AUDIO_CONTROL_HOST"),
        "port": int(environment["AUDIO_CONTROL_PORT"])
        if environment.get("AUDIO_CONTROL_PORT", "").isdigit()
        else None,
    }


def start_managed_service(
    runner: CommandRunner,
    *,
    host: str,
    port: int,
    runtime_seconds: int,
) -> dict[str, Any]:
    status = systemd_status(runner)
    if status.get("active_state") in {"active", "activating", "reloading"}:
        raise ControlError(f"{UNIT_NAME} läuft bereits.")
    transition_state_root = ensure_profile_transition_state_root()
    laboratory_state_root = ensure_laboratory_state_root()
    recorder_init = runner.run(
        [
            sys.executable,
            str(RECORDING_SCRIPT),
            "init",
            "--root",
            str(RECORDING_OUTPUT_ROOT),
            "--state-root",
            str(RECORDING_STATE_ROOT),
        ],
        timeout=10,
    )
    if recorder_init.returncode != 0:
        raise ControlError("Private Recorderverzeichnisse konnten nicht vorbereitet werden.")
    command = [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--service-type",
        "notify",
        "--setenv",
        f"AUDIO_CONTROL_HOST={host}",
        "--setenv",
        f"AUDIO_CONTROL_PORT={port}",
        "--setenv",
        f"AUDIO_CONTROL_MANAGED_BY={UNIT_MANAGED_BY}",
        "--unit",
        UNIT_NAME.removesuffix(".service"),
        "--property",
        "Description=Lokale Audiozentrale v1",
        "--property",
        "Restart=no",
        "--property",
        "NotifyAccess=main",
        "--property",
        "TimeoutStartSec=10s",
        "--property",
        "TimeoutStopSec=10s",
        "--property",
        "KillMode=mixed",
        "--property",
        "NoNewPrivileges=yes",
        "--property",
        "PrivateTmp=yes",
        "--property",
        "ProtectSystem=strict",
        "--property",
        "ProtectHome=read-only",
        "--property",
        "StateDirectory=audio-control-ui",
        "--property",
        "StateDirectoryMode=0700",
        "--property",
        (
            f"ReadWritePaths={RECORDING_OUTPUT_ROOT} {RECORDING_STATE_ROOT} "
            f"{transition_state_root} {laboratory_state_root}"
        ),
        "--property",
        "ProtectControlGroups=yes",
        "--property",
        "ProtectKernelTunables=yes",
        "--property",
        "LockPersonality=yes",
        "--property",
        "RestrictSUIDSGID=yes",
        "--property",
        "RestrictAddressFamilies=AF_UNIX AF_INET",
        "--property",
        "UMask=0077",
        "--property",
        "MemoryMax=134217728",
        "--property",
        "CPUQuota=50%",
        "--property",
        "TasksMax=32",
        "--property",
        "LogRateLimitIntervalSec=30s",
        "--property",
        "LogRateLimitBurst=100",
        "--property",
        f"RuntimeMaxSec={runtime_seconds}",
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    result = runner.run(command, timeout=12)
    if result.returncode != 0:
        raise ControlError("Der verwaltete Control-Dienst konnte nicht starten.")
    last_status: dict[str, Any] | None = None
    for _attempt in range(50):
        last_status = systemd_status(runner)
        if (
            last_status.get("load_state") == "loaded"
            and last_status.get("active_state") == "active"
            and last_status.get("sub_state") == "running"
            and last_status.get("host") == host
            and last_status.get("port") == port
            and last_status.get("managed_by") == UNIT_MANAGED_BY
        ):
            return {
                **last_status,
                "state": "ready",
                "url": f"http://{host}:{port}/",
                "runtime_max_seconds": runtime_seconds,
            }
        if last_status.get("active_state") in {"failed", "inactive"}:
            break
        time.sleep(0.1)
    raise ControlError("Control-Dienst meldete keine Laufbereitschaft.")


def stop_managed_service(runner: CommandRunner) -> dict[str, Any]:
    status = systemd_status(runner)
    if status.get("active_state") not in {"active", "activating", "reloading"}:
        return {"state": "inactive", "unit": UNIT_NAME}
    if status.get("managed_by") != UNIT_MANAGED_BY:
        raise ControlError(
            "Die gleichnamige aktive Unit gehört nicht zum Audio-Control-Wrapper."
        )
    result = runner.run(
        ["systemctl", "--user", "stop", UNIT_NAME],
        timeout=10,
    )
    if result.returncode != 0:
        raise ControlError("Der Control-Dienst konnte nicht beendet werden.")
    for _attempt in range(50):
        final_status = systemd_status(runner)
        if final_status.get("active_state") in {"inactive", "failed"}:
            return {
                **final_status,
                "state": "stopped",
            }
        time.sleep(0.1)
    raise ControlError("Der Control-Dienst bestätigte den Stop nicht.")


def validate_repository_contract(*, require_live_telemetry: bool = True) -> dict[str, Any]:
    missing = [
        str(path.relative_to(ROOT))
        for path in [
            PROFILE_CATALOG,
            WHALE_PROFILE,
            ROOT / "docs" / "plans" / "local-audio-control-ui-v1.md",
            ROOT / "docs" / "plans" / "audiozentrale-task-workspaces-v1.md",
            ROOT / "docs" / "operating-modes-v1.md",
            ROOT / "inventory" / "buckelwal-learning-lesson.v1.json",
            ROOT / "schemas" / "buckelwal-learning-lesson.v1.schema.json",
            WHALE_LESSON_SCRIPT,
            RECORDING_SCRIPT,
            RECORDING_PRODUCT_SCRIPT,
            RECORDING_CATALOG,
            REFERENCE_LEVELS,
        ]
        if not path.is_file()
    ]
    if missing:
        raise ControlError("Fehlende UI-Vertragsdateien: " + ", ".join(missing))
    try:
        index = read_static_file("index.html").decode("utf-8")
        javascript = read_static_file("app.js").decode("utf-8")
        styles = read_static_file("styles.css").decode("utf-8")
    except (ControlError, UnicodeDecodeError) as error:
        raise ControlError("Statische UI-Dateien verletzen den Vertrag.") from error
    required_areas = ("home", "hoeren", "aufnehmen", "spielen", "material", "system")
    absent = [
        area
        for area in required_areas
        if f'data-route="{area}"' not in index or f'id="view-{area}"' not in index
    ]
    if absent:
        raise ControlError("UI-Bereiche fehlen: " + ", ".join(absent))
    if 'audiozentrale-product-surface" content="task-workspaces-v1"' not in index:
        raise ControlError("UI ist nicht an den Task-Workspace-Vertrag gebunden.")
    if "installTaskWorkspaceLayout" not in javascript or "is-workspace-focused" not in styles:
        raise ControlError("UI enthält keinen zustandserhaltenden Workspace-Fokus.")
    if "/api/v1/snapshot" not in javascript:
        raise ControlError("UI ist nicht an die versionierte Zustands-API gebunden.")
    if "/api/v1/replay" not in javascript:
        raise ControlError("UI ist nicht an den versionierten Replay-Vertrag gebunden.")
    if "/api/v1/telemetry" not in javascript:
        raise ControlError("UI ist nicht an den Live-Telemetrievertrag gebunden.")
    if 'id="live-telemetry"' not in index:
        raise ControlError("UI enthält kein Live-Telemetriepanel.")
    lesson_javascript = read_static_file("whale-lesson.js").decode("utf-8")
    if "/api/v1/whale/lesson" not in lesson_javascript:
        raise ControlError("UI ist nicht an den Buckelwal-Lektionsvertrag gebunden.")
    if 'id="whale-learning-lesson"' not in index:
        raise ControlError("UI enthält keinen Buckelwal-Lektionsfokus.")
    action_endpoints = set(
        re.findall(r"/api/v1/actions/[a-z0-9_-]+", javascript)
    )
    required_action_endpoints = {
        "/api/v1/actions/dauersong",
        "/api/v1/actions/operating-mode",
        "/api/v1/actions/recording",
        "/api/v1/actions/whale",
    }
    missing_action_endpoints = required_action_endpoints - action_endpoints
    if missing_action_endpoints:
        raise ControlError(
            "UI-Aktionsbindungen fehlen: " + ", ".join(sorted(missing_action_endpoints))
        )
    unexpected_action_endpoints = action_endpoints - required_action_endpoints
    if unexpected_action_endpoints:
        raise ControlError(
            "Produktoberfläche enthält nicht freigegebene Audioaktionen: "
            + ", ".join(sorted(unexpected_action_endpoints))
        )
    if "/api/v1/recordings" not in javascript:
        raise ControlError("UI ist nicht an die Recorderbibliothek gebunden.")
    try:
        replay = TELEMETRY_REPLAY.load_replay_contract()
    except TELEMETRY_REPLAY.ReplayError as error:
        raise ControlError("Telemetry-Replay verletzt den Vertrag.") from error
    try:
        lesson = WHALE_LESSON.load_lesson_contract()
    except WHALE_LESSON.LessonError as error:
        raise ControlError("Buckelwal-Lektion verletzt den Vertrag.") from error
    telemetry_contract: dict[str, Any] | None = None
    try:
        telemetry_contract = load_live_telemetry().contract_report()
    except Exception as error:
        if require_live_telemetry:
            raise ControlError("Live-Telemetrie verletzt den Vertrag.") from error
    if (
        telemetry_contract is not None
        and telemetry_contract["safety"]["mode"] != "passive-observation"
    ):
        raise ControlError("Live-Telemetrie verlässt die passive Beobachtungsgrenze.")
    profiles = read_profiles()
    whale = read_whale_contract()
    recording = read_voice_recording_contract()
    return {
        "status": "ok",
        "kind": "audio_control_contract_check",
        "spec_base_revision": SPEC_BASE_REVISION,
        "areas": list(required_areas),
        "profile_count": len(profiles),
        "whale_modes": [mode["id"] for mode in whale["modes"]],
        "whale_keyboard_keys": whale["keyboard"]["key_count"],
        "recording_profile": recording["profile"],
        "recording_source": recording["source"]["kind"],
        "recording_monitoring": recording["monitoring"]["mode"],
        "replay_scenarios": [
            scenario["id"] for scenario in replay["catalog"]["scenarios"]
        ],
        "whale_lesson_id": lesson["lesson_id"],
        "whale_lesson_variants": [variant["id"] for variant in lesson["variants"]],
        "live_telemetry_streams": []
        if telemetry_contract is None
        else telemetry_contract["streams"],
        "live_telemetry_safety": "unavailable"
        if telemetry_contract is None
        else telemetry_contract["safety"]["mode"],
        "static_files": sorted({entry[0] for entry in STATIC_FILES.values()}),
    }


def serve(*, host: str, port: int, cache_seconds: float) -> None:
    if not is_loopback_host(host):
        raise ControlError("Der Control-Dienst darf nur an Loopback gebunden werden.")
    if host != DEFAULT_HOST:
        raise ControlError("Version 1 unterstützt ausschließlich 127.0.0.1.")
    validate_repository_contract(require_live_telemetry=False)
    controller = AudioControl(
        host=host,
        port=port,
        cache_seconds=cache_seconds,
    )
    with AudioControlHTTPServer((host, port), controller) as server:
        telemetry_state = controller.start_telemetry()
        notify_systemd_ready(f"Audiozentrale bereit auf {host}:{port}")
        print(
            json.dumps(
                {
                    "state": "ready",
                    "url": f"http://{host}:{port}/",
                    "api_version": API_VERSION,
                    "authority": "local-backend",
                    "readiness": "socket-bound-and-contract-validated",
                    "live_telemetry": telemetry_state.get("state", "unavailable"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            controller.stop_telemetry()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="im Vordergrund ausführen")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=bounded_port, default=DEFAULT_PORT)
    serve_parser.add_argument(
        "--cache-seconds",
        type=float,
        default=DEFAULT_CACHE_SECONDS,
    )

    start_parser = subparsers.add_parser(
        "start", help="als begrenzten systemd-Userdienst starten"
    )
    start_parser.add_argument("--host", default=DEFAULT_HOST)
    start_parser.add_argument("--port", type=bounded_port, default=DEFAULT_PORT)
    start_parser.add_argument(
        "--runtime-max-seconds",
        type=bounded_runtime,
        default=MAX_RUNTIME_SECONDS,
    )

    subparsers.add_parser("stop", help="verwalteten Userdienst beenden")
    subparsers.add_parser("status", help="verwalteten Userdienst abfragen")
    subparsers.add_parser("check", help="Repository- und UI-Vertrag prüfen")
    subparsers.add_parser(
        "prepare-runtime-state",
        help="statische private Laufzeitpfade ohne Audioeffekt vorbereiten",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        runner = CommandRunner()
        if args.command == "serve":
            if not 0 <= args.cache_seconds <= 60:
                raise ControlError("Cachezeit muss zwischen 0 und 60 Sekunden liegen.")
            serve(
                host=args.host,
                port=args.port,
                cache_seconds=args.cache_seconds,
            )
            return 0
        if args.command == "start":
            if not is_loopback_host(args.host) or args.host != DEFAULT_HOST:
                raise ControlError(
                    "Version 1 unterstützt als Bind-Adresse nur 127.0.0.1."
                )
            report = start_managed_service(
                runner,
                host=args.host,
                port=args.port,
                runtime_seconds=args.runtime_max_seconds,
            )
        elif args.command == "stop":
            report = stop_managed_service(runner)
        elif args.command == "status":
            report = systemd_status(runner)
        elif args.command == "check":
            report = validate_repository_contract()
        elif args.command == "prepare-runtime-state":
            report = prepare_runtime_state_bootstrap()
        else:
            raise AssertionError("unreachable")
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ControlError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"state": "blocked", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
