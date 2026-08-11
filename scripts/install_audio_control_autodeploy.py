#!/usr/bin/env python3
"""Install and activate the revision-bound Audiozentrale deployment path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "audio_control_deploy.py"
UNIT_ROOT = ROOT / "systemd" / "user"
UNIT_FILES = (
    "audio-control-ui-v1.service",
    "audio-control-level-observer-v1.service",
    "audio-control-deploy.service",
    "audio-control-deploy.timer",
)
DEFAULT_SOURCE_REPO = pathlib.Path.home() / "repos" / "audio"
DEFAULT_DEPLOY_ROOT = pathlib.Path.home() / ".local" / "share" / "audio-control-ui"
DEFAULT_STATE_ROOT = pathlib.Path.home() / ".local" / "state" / "audio-control-deploy"


class InstallError(RuntimeError):
    """Controlled installation failure."""


def run(argv: list[str], *, timeout: float = 120.0, check: bool = True) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise InstallError(f"Zeitüberschreitung: {argv[0]}") from error
    receipt = {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "kein Detail"
        raise InstallError(f"Befehl fehlgeschlagen ({argv[0]}): {detail}")
    return receipt


def ensure_absolute_directory(path: pathlib.Path, mode: int = 0o700) -> pathlib.Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise InstallError(f"Pfad muss absolut sein: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise InstallError(f"Installationspfad ist nicht vertrauenswürdig: {path}")
    path.chmod(mode)
    return path


def atomic_copy(source: pathlib.Path, destination: pathlib.Path, mode: int) -> str:
    if not source.is_file() or source.is_symlink():
        raise InstallError(f"Installationsquelle fehlt oder ist unsicher: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)
    )
    temporary = pathlib.Path(temporary_name)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            while chunk := input_handle.read(131_072):
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest.hexdigest()


def atomic_write(path: pathlib.Path, content: str, mode: int) -> str:
    encoded = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(encoded).hexdigest()


def validate_source_repo(path: pathlib.Path) -> pathlib.Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise InstallError("Quellrepository muss absolut sein.")
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise InstallError("Quellrepository ist nicht lesbar.")
    observed = pathlib.Path(completed.stdout.strip()).resolve()
    if observed != path.resolve():
        raise InstallError("Quellrepository stimmt nicht mit seinem Git-Wurzelpfad überein.")
    return observed


def environment_line(name: str, value: str) -> str:
    if any(character in value for character in "\n\r\0"):
        raise InstallError(f"Ungültiger Wert für {name}.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"\n'


def validate_ui_endpoint(host: str, port: int) -> None:
    if host != "127.0.0.1":
        raise InstallError("Version 1 unterstützt als UI-Bind-Adresse nur 127.0.0.1.")
    if not 1024 <= port <= 65535:
        raise InstallError("UI-Port muss zwischen 1024 und 65535 liegen.")


def normalized_absolute_path(path: pathlib.Path, *, label: str) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise InstallError(f"{label} muss absolut sein.")
    return pathlib.Path(os.path.normpath(str(expanded)))


def validate_runtime_roots(
    deploy_root: pathlib.Path, state_root: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    deploy = normalized_absolute_path(deploy_root, label="Deploy-Root")
    state = normalized_absolute_path(state_root, label="State-Root")
    if deploy != DEFAULT_DEPLOY_ROOT:
        raise InstallError(
            f"Version 1 unterstützt nur den Deploy-Root {DEFAULT_DEPLOY_ROOT}."
        )
    if state != DEFAULT_STATE_ROOT:
        raise InstallError(
            f"Version 1 unterstützt nur den State-Root {DEFAULT_STATE_ROOT}."
        )
    return deploy, state


def install(args: argparse.Namespace) -> dict[str, Any]:
    validate_ui_endpoint(args.host, args.port)
    deploy_root, state_root = validate_runtime_roots(
        args.deploy_root, args.state_root
    )
    source_repo = validate_source_repo(args.source_repo)
    deploy_root = ensure_absolute_directory(deploy_root)
    state_root = ensure_absolute_directory(state_root)
    libexec = ensure_absolute_directory(pathlib.Path.home() / ".local" / "libexec")
    config_root = ensure_absolute_directory(pathlib.Path.home() / ".config")
    unit_root = ensure_absolute_directory(
        pathlib.Path.home() / ".config" / "systemd" / "user"
    )

    installed: dict[str, str] = {}
    deploy_destination = libexec / "audio-control-deploy.py"
    installed[str(deploy_destination)] = atomic_copy(
        DEPLOY_SCRIPT, deploy_destination, 0o700
    )
    for unit_name in UNIT_FILES:
        source = UNIT_ROOT / unit_name
        destination = unit_root / unit_name
        installed[str(destination)] = atomic_copy(source, destination, 0o600)

    environment = "".join(
        [
            environment_line("AUDIO_CONTROL_SOURCE_REPO", str(source_repo)),
            environment_line("AUDIO_CONTROL_DEPLOY_ROOT", str(deploy_root)),
            environment_line("AUDIO_CONTROL_STATE_ROOT", str(state_root)),
            environment_line("AUDIO_CONTROL_REMOTE", args.remote),
            environment_line("AUDIO_CONTROL_BRANCH", args.branch),
            environment_line("AUDIO_CONTROL_UNIT", "audio-control-ui-v1.service"),
            environment_line("AUDIO_CONTROL_HOST", args.host),
            environment_line("AUDIO_CONTROL_PORT", str(args.port)),
        ]
    )
    environment_path = config_root / "audio-control-deploy.env"
    installed[str(environment_path)] = atomic_write(environment_path, environment, 0o600)

    commands: list[dict[str, Any]] = [
        run(["systemctl", "--user", "daemon-reload"], timeout=30),
    ]
    sync_command = [
        sys.executable,
        str(deploy_destination),
        "--source-repo",
        str(source_repo),
        "--deploy-root",
        str(deploy_root),
        "--state-root",
        str(state_root),
        "--remote",
        args.remote,
        "--branch",
        args.branch,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "sync",
    ]
    if args.expected_commit:
        sync_command.extend(["--expected-commit", args.expected_commit])
    commands.append(run(sync_command, timeout=300))
    commands.extend(
        [
            run(
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "audio-control-ui-v1.service",
                ],
                timeout=30,
            ),
            run(
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "--now",
                    "audio-control-deploy.timer",
                ],
                timeout=30,
            ),
        ]
    )
    return {
        "schema_version": 1,
        "kind": "audio_control_autodeploy_install",
        "source_repo": str(source_repo),
        "deploy_root": str(deploy_root),
        "state_root": str(state_root),
        "installed": installed,
        "commands": commands,
        "timer_interval_seconds": 60,
    }


def parse_path(value: str) -> pathlib.Path:
    return pathlib.Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=parse_path, default=DEFAULT_SOURCE_REPO)
    parser.add_argument(
        "--deploy-root",
        type=parse_path,
        default=DEFAULT_DEPLOY_ROOT,
        help=f"V1 ist fest auf {DEFAULT_DEPLOY_ROOT} gebunden",
    )
    parser.add_argument(
        "--state-root",
        type=parse_path,
        default=DEFAULT_STATE_ROOT,
        help=f"V1 ist fest auf {DEFAULT_STATE_ROOT} gebunden",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--expected-commit", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = install(args)
    except (InstallError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_control_autodeploy_install_error",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
