#!/usr/bin/env python3
"""Atomic, revision-bound deployment for the local Audiozentrale."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable

DEFAULT_SOURCE_REPO = pathlib.Path.home() / "repos" / "audio"
DEFAULT_DEPLOY_ROOT = pathlib.Path.home() / ".local" / "share" / "audio-control-ui"
DEFAULT_STATE_ROOT = pathlib.Path.home() / ".local" / "state" / "audio-control-deploy"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_UNIT = "audio-control-ui-v1.service"
REMOTE_BRIDGE_UNIT = "audio-remote-bridge-v1.service"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
UI_RUNTIME_ENV = DEFAULT_STATE_ROOT / "runtime.env"
UI_MANAGED_BY = "audio-control-autodeploy-v1"
RUNTIME_FILES = {
    "scripts/audio_control_deploy.py": (
        pathlib.Path.home() / ".local" / "libexec" / "audio-control-deploy.py",
        0o700,
    ),
    "systemd/user/audio-control-ui-v1.service": (
        pathlib.Path.home() / ".config" / "systemd" / "user" / "audio-control-ui-v1.service",
        0o600,
    ),
    "systemd/user/audio-remote-bridge-v1.service": (
        pathlib.Path.home() / ".config" / "systemd" / "user" / "audio-remote-bridge-v1.service",
        0o600,
    ),
    "systemd/user/audio-control-deploy.service": (
        pathlib.Path.home() / ".config" / "systemd" / "user" / "audio-control-deploy.service",
        0o600,
    ),
    "systemd/user/audio-control-deploy.timer": (
        pathlib.Path.home() / ".config" / "systemd" / "user" / "audio-control-deploy.timer",
        0o600,
    ),
}
PWA_CRITICAL_RELEASE_FILES = (
    "inventory/audiozentrale-ipad-pwa.v1.json",
    "schemas/audiozentrale-ipad-pwa.v1.schema.json",
    "ui/sw.js",
    "ui/manifest.webmanifest",
    "ui/icon-180.png",
    "ui/icon-192.png",
    "ui/icon-512.png",
)
PWA_RELEASE_SENTINEL = "tests/test_audio_ipad_pwa.py"
REMOTE_BRIDGE_CRITICAL_RELEASE_FILES = (
    "scripts/audio_remote_bridge.py",
    "scripts/audio_remote_bridge_tailscale.py",
    "inventory/audiozentrale-remote-bridge.v1.json",
    "schemas/audiozentrale-remote-bridge.v1.schema.json",
    "systemd/user/audio-remote-bridge-v1.service",
)
REMOTE_BRIDGE_RELEASE_SENTINEL = "tests/test_audio_remote_bridge.py"

BASE_CRITICAL_RELEASE_FILES = (
    "scripts/audio_control.py",
    "scripts/audio_remote_bridge.py",
    "scripts/audio_remote_bridge_tailscale.py",
    "inventory/audiozentrale-remote-bridge.v1.json",
    "schemas/audiozentrale-remote-bridge.v1.schema.json",
    "systemd/user/audio-remote-bridge-v1.service",
    "inventory/audiozentrale-ipad-pwa.v1.json",
    "schemas/audiozentrale-ipad-pwa.v1.schema.json",
    "scripts/audio_telemetry_replay.py",
    "inventory/audiozentrale-telemetry-replay.v1.json",
    "schemas/audiozentrale-telemetry-replay.v1.schema.json",
    "scripts/whale_learning_lesson.py",
    "inventory/buckelwal-learning-lesson.v1.json",
    "schemas/buckelwal-learning-lesson.v1.schema.json",
    "assets/whale-sources/processed/manifest.json",
    "assets/whale-sources/processed/humpback-song-cc0-01.wav",
    "assets/whale-sources/morph/manifest.json",
    "ui/whale-lesson.js",
    "ui/whale-learning-reference.wav",
    "ui/whale-learning-morph.wav",
    "ui/whale-learning-envelope.wav",
    "ui/whale-learning-periodicity.wav",
    "ui/whale-learning-articulation.wav",
    "ui/index.html",
    "ui/app.js",
    "ui/styles.css",
    "ui/sw.js",
    "ui/manifest.webmanifest",
    "ui/icon-180.png",
    "ui/icon-192.png",
    "ui/icon-512.png",
)
STATIC_ENDPOINTS = (
    ("/", "ui/index.html", ("text/html",)),
    ("/app.js", "ui/app.js", ("application/javascript", "text/javascript")),
    ("/styles.css", "ui/styles.css", ("text/css",)),
    ("/sw.js", "ui/sw.js", ("application/javascript", "text/javascript")),
    ("/manifest.webmanifest", "ui/manifest.webmanifest", ("application/manifest+json",)),
    ("/icon-180.png", "ui/icon-180.png", ("image/png",)),
    ("/icon-192.png", "ui/icon-192.png", ("image/png",)),
    ("/icon-512.png", "ui/icon-512.png", ("image/png",)),
    ("/whale-lesson.js", "ui/whale-lesson.js", ("application/javascript", "text/javascript")),
    ("/whale-learning-reference.wav", "ui/whale-learning-reference.wav", ("audio/wav",)),
)
DEFAULT_RELEASE_RETENTION = 3
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_OUTPUT_BYTES = 1_048_576
MAX_STATIC_BYTES = 1_048_576


class DeployError(RuntimeError):
    """Controlled deployment failure."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    def receipt(self, *, redact_indexes: set[int] | None = None) -> dict[str, Any]:
        redacted = redact_indexes or set()
        return {
            "argv": [
                "<redacted>" if index in redacted else value
                for index, value in enumerate(self.argv)
            ],
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout_sha256": hashlib.sha256(self.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(self.stderr.encode()).hexdigest(),
        }


def run_command(
    argv: Iterable[str],
    *,
    cwd: pathlib.Path | None = None,
    timeout: float = 60.0,
    check: bool = True,
) -> CommandResult:
    command = tuple(str(item) for item in argv)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DeployError(f"Zeitüberschreitung: {command[0]}") from error
    stdout = completed.stdout[-MAX_OUTPUT_BYTES:]
    stderr = completed.stderr[-MAX_OUTPUT_BYTES:]
    result = CommandResult(
        argv=command,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "kein Detail"
        raise DeployError(f"Befehl fehlgeschlagen ({command[0]}): {detail}")
    return result


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise DeployError(f"Pfad muss absolut sein: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise DeployError(f"Verzeichnis ist nicht vertrauenswürdig: {path}")
    path.chmod(0o700)
    return path


def ensure_source_repo(path: pathlib.Path) -> pathlib.Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise DeployError("Quellrepository muss absolut angegeben werden.")
    result = run_command(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        timeout=15,
    )
    observed = pathlib.Path(result.stdout.strip()).resolve()
    if observed != path.resolve():
        raise DeployError("Quellrepository stimmt nicht mit seinem Git-Wurzelpfad überein.")
    return path.resolve()


def validate_remote_url(value: str) -> str:
    if not value or any(character in value for character in "\n\r\0"):
        raise DeployError("Remote-URL ist leer oder enthält Steuerzeichen.")
    if value.startswith("git@") and ":" in value[4:]:
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"ssh", "https"} or not parsed.hostname:
        raise DeployError("Nur SSH- und HTTPS-Remotes sind für das Deployment erlaubt.")
    if parsed.username or parsed.password:
        raise DeployError("Remote-URLs mit eingebetteten Zugangsdaten sind verboten.")
    return value


def prepare_deployment_repository(
    source_repo: pathlib.Path,
    state_root: pathlib.Path,
    *,
    remote: str,
) -> tuple[pathlib.Path, list[dict[str, Any]]]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        raise DeployError("Remote-Name ist ungültig.")
    receipts: list[dict[str, Any]] = []
    remote_result = run_command(
        ["git", "-C", str(source_repo), "remote", "get-url", remote],
        timeout=15,
    )
    receipts.append(remote_result.receipt())
    remote_url = validate_remote_url(remote_result.stdout.strip())

    repository = state_root / "repository.git"
    if repository.exists():
        if repository.is_symlink() or not repository.is_dir():
            raise DeployError("Deployment-Repository ist nicht vertrauenswürdig.")
        bare = run_command(
            ["git", "--git-dir", str(repository), "rev-parse", "--is-bare-repository"],
            timeout=15,
        )
        receipts.append(bare.receipt())
        if bare.stdout.strip() != "true":
            raise DeployError("Deployment-Repository ist nicht bare.")
    else:
        initialized = run_command(
            ["git", "init", "--bare", "--quiet", str(repository)],
            timeout=30,
        )
        receipts.append(initialized.receipt())

    existing = run_command(
        ["git", "--git-dir", str(repository), "remote", "get-url", "source"],
        timeout=15,
        check=False,
    )
    receipts.append(existing.receipt())
    if existing.returncode == 0:
        if existing.stdout.strip() != remote_url:
            updated = run_command(
                [
                    "git",
                    "--git-dir",
                    str(repository),
                    "remote",
                    "set-url",
                    "source",
                    remote_url,
                ],
                timeout=15,
            )
            receipts.append(updated.receipt(redact_indexes={6}))
    else:
        added = run_command(
            [
                "git",
                "--git-dir",
                str(repository),
                "remote",
                "add",
                "source",
                remote_url,
            ],
            timeout=15,
        )
        receipts.append(added.receipt(redact_indexes={6}))
    return repository, receipts


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(131_072):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace_bytes(path: pathlib.Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise DeployError(f"Runtimeziel ist nicht vertrauenswürdig: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def validate_ui_endpoint(host: str, port: int) -> None:
    if host != DEFAULT_HOST:
        raise DeployError("Version 1 unterstützt als UI-Bind-Adresse nur 127.0.0.1.")
    if not 1024 <= port <= 65535:
        raise DeployError("UI-Port muss zwischen 1024 und 65535 liegen.")


def runtime_environment_payload(host: str, port: int) -> bytes:
    validate_ui_endpoint(host, port)
    return (
        f'AUDIO_CONTROL_HOST="{host}"\n'
        f'AUDIO_CONTROL_PORT="{port}"\n'
        f'AUDIO_CONTROL_MANAGED_BY="{UI_MANAGED_BY}"\n'
    ).encode("utf-8")


def reconcile_runtime_environment(
    path: pathlib.Path,
    *,
    host: str,
    port: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = runtime_environment_payload(host, port)
    expected_sha = hashlib.sha256(payload).hexdigest()
    previous_payload: bytes | None = None
    previous_mode: int | None = None
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise DeployError(f"UI-Laufzeitkonfiguration ist nicht vertrauenswürdig: {path}")
        previous_payload = path.read_bytes()
        previous_mode = stat.S_IMODE(path.stat().st_mode)
        if (
            hashlib.sha256(previous_payload).hexdigest() == expected_sha
            and previous_mode == 0o600
        ):
            return {
                "path": str(path),
                "changed": False,
                "sha256": expected_sha,
                "mode": "0o600",
                "host": host,
                "port": port,
            }, None
    backup = {
        "path": str(path),
        "payload": previous_payload,
        "mode": previous_mode,
    }
    atomic_replace_bytes(path, payload, 0o600)
    return {
        "path": str(path),
        "changed": True,
        "sha256": expected_sha,
        "mode": "0o600",
        "host": host,
        "port": port,
    }, backup


def restore_runtime_environment(backup: dict[str, Any] | None) -> None:
    if backup is None:
        return
    destination = pathlib.Path(backup["path"])
    payload = backup["payload"]
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        return
    atomic_replace_bytes(destination, payload, int(backup["mode"] or 0o600))


def install_release_runtime(
    release: pathlib.Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    present = [relative for relative in RUNTIME_FILES if (release / relative).is_file()]
    if not present:
        return [], []
    if set(present) != set(RUNTIME_FILES):
        missing = sorted(set(RUNTIME_FILES) - set(present))
        raise DeployError(
            "Release enthält einen unvollständigen Deploymechanismus: "
            + ", ".join(missing)
        )

    source_payloads: dict[str, bytes] = {}
    candidates: list[dict[str, Any]] = []
    for relative, (destination, mode) in RUNTIME_FILES.items():
        source = release / relative
        if source.is_symlink() or not source.is_file():
            raise DeployError(f"Runtimequelle ist nicht vertrauenswürdig: {relative}")
        payload = source.read_bytes()
        source_payloads[relative] = payload
        source_sha = hashlib.sha256(payload).hexdigest()
        previous_payload: bytes | None = None
        previous_mode: int | None = None
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise DeployError(f"Runtimeziel ist nicht vertrauenswürdig: {destination}")
            previous_payload = destination.read_bytes()
            previous_mode = stat.S_IMODE(destination.stat().st_mode)
            if (
                hashlib.sha256(previous_payload).hexdigest() == source_sha
                and previous_mode == mode
            ):
                continue
        candidates.append(
            {
                "relative": relative,
                "destination": destination,
                "mode": mode,
                "payload": payload,
                "sha256": source_sha,
                "previous_payload": previous_payload,
                "previous_mode": previous_mode,
            }
        )

    if not candidates:
        return [], []

    deploy_relative = "scripts/audio_control_deploy.py"
    deploy_source = release / deploy_relative
    try:
        compile(
            source_payloads[deploy_relative].decode("utf-8"),
            str(deploy_source),
            "exec",
        )
    except (SyntaxError, UnicodeDecodeError) as error:
        raise DeployError("Deploymechanismus des Releases ist nicht ausführbar.") from error

    with tempfile.TemporaryDirectory(prefix="audio-control-units-") as directory:
        unit_root = pathlib.Path(directory)
        unit_snapshots: list[pathlib.Path] = []
        for relative, payload in source_payloads.items():
            if not relative.endswith((".service", ".timer")):
                continue
            snapshot = unit_root / pathlib.Path(relative).name
            snapshot.write_bytes(payload)
            snapshot.chmod(0o600)
            unit_snapshots.append(snapshot)
        run_command(
            [
                "systemd-analyze",
                "--user",
                "verify",
                *map(str, unit_snapshots),
            ],
            timeout=30,
        )

    updates: list[dict[str, Any]] = []
    backups: list[dict[str, Any]] = []
    try:
        for candidate in candidates:
            destination = candidate["destination"]
            backups.append(
                {
                    "path": str(destination),
                    "payload": candidate["previous_payload"],
                    "mode": candidate["previous_mode"],
                }
            )
            atomic_replace_bytes(
                destination, candidate["payload"], int(candidate["mode"])
            )
            updates.append(
                {
                    "source": candidate["relative"],
                    "destination": str(destination),
                    "sha256": candidate["sha256"],
                    "mode": oct(int(candidate["mode"])),
                }
            )
    except Exception:
        restore_release_runtime(backups)
        raise
    return updates, backups


def restore_release_runtime(backups: list[dict[str, Any]]) -> None:
    for backup in reversed(backups):
        destination = pathlib.Path(backup["path"])
        payload = backup["payload"]
        if payload is None:
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
            continue
        atomic_replace_bytes(destination, payload, int(backup["mode"] or 0o600))


def validate_member_name(name: str) -> tuple[str, ...]:
    candidate = pathlib.PurePosixPath(name)
    if candidate.is_absolute() or not candidate.parts:
        raise DeployError(f"Ungültiger Archivpfad: {name}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise DeployError(f"Unsicherer Archivpfad: {name}")
    return candidate.parts


def copy_exact(source: BinaryIO, destination: pathlib.Path, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, mode & 0o777)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            shutil.copyfileobj(source, handle, length=131_072)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        raise


def extract_commit(repository: pathlib.Path, commit: str, destination: pathlib.Path) -> None:
    process = subprocess.Popen(
        ["git", "--git-dir", str(repository), "archive", "--format=tar", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                parts = validate_member_name(member.name)
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=member.mode & 0o777)
                    continue
                if not member.isreg():
                    raise DeployError(
                        f"Nicht-regulärer Archiveintrag ist im Deployment verboten: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                source = archive.extractfile(member)
                if source is None:
                    raise DeployError(f"Archiveintrag ist nicht lesbar: {member.name}")
                with source:
                    copy_exact(source, target, member.mode)
        stderr = process.stderr.read(MAX_OUTPUT_BYTES).decode(errors="replace")
        returncode = process.wait(timeout=30)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise
    if returncode != 0:
        raise DeployError(f"git archive fehlgeschlagen: {stderr.strip()}")


def validate_release(release: pathlib.Path) -> list[dict[str, Any]]:
    required = [
        release / "scripts" / "audio_control.py",
        release / "scripts" / "audio_remote_bridge.py",
        release / "scripts" / "audio_remote_bridge_tailscale.py",
        release / "inventory" / "audiozentrale-remote-bridge.v1.json",
        release / "schemas" / "audiozentrale-remote-bridge.v1.schema.json",
        release / "systemd" / "user" / "audio-remote-bridge-v1.service",
        release / "ui" / "index.html",
        release / "ui" / "app.js",
        release / "ui" / "styles.css",
        release / "ui" / "sw.js",
        release / "ui" / "manifest.webmanifest",
        release / "ui" / "icon-180.png",
        release / "ui" / "icon-192.png",
        release / "ui" / "icon-512.png",
        release / "inventory" / "audiozentrale-ipad-pwa.v1.json",
        release / "schemas" / "audiozentrale-ipad-pwa.v1.schema.json",
        release / "tests" / "test_audio_control.py",
        release / "tests" / "test_audio_ipad_pwa.py",
        release / "tests" / "test_audio_remote_bridge.py",
    ]
    missing = [str(path.relative_to(release)) for path in required if not path.is_file()]
    if missing:
        raise DeployError("Release ist unvollständig: " + ", ".join(missing))
    checks: list[CommandResult] = [
        run_command(
            [sys.executable, "scripts/audio_control.py", "check"],
            cwd=release,
            timeout=60,
        ),
        run_command(
            [sys.executable, "-m", "unittest", "tests/test_audio_control.py"],
            cwd=release,
            timeout=180,
        ),
        run_command(
            [sys.executable, "-m", "unittest", "tests/test_audio_ipad_pwa.py"],
            cwd=release,
            timeout=180,
        ),
        run_command(
            [sys.executable, "scripts/audio_remote_bridge.py", "check"],
            cwd=release,
            timeout=30,
        ),
        run_command(
            [sys.executable, "-m", "unittest", "tests/test_audio_remote_bridge.py"],
            cwd=release,
            timeout=180,
        ),
        run_command(
            [sys.executable, "-m", "compileall", "-q", "scripts", "tests"],
            cwd=release,
            timeout=120,
        ),
    ]
    node = shutil.which("node")
    if node:
        checks.append(run_command([node, "--check", "ui/app.js"], cwd=release, timeout=30))
        checks.append(run_command([node, "--check", "ui/sw.js"], cwd=release, timeout=30))
    return [check.receipt() for check in checks]


def release_marker(release: pathlib.Path) -> dict[str, Any]:
    marker = release / ".audio-control-release.json"
    if not marker.is_file() or marker.is_symlink():
        raise DeployError(f"Releasebeleg fehlt: {release}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeployError(f"Releasebeleg ist ungültig: {release}") from error
    if not isinstance(payload, dict):
        raise DeployError(f"Releasebeleg hat falsche Form: {release}")
    return payload


def critical_release_paths(release: pathlib.Path) -> tuple[str, ...]:
    paths = list(BASE_CRITICAL_RELEASE_FILES)
    # Pre-PWA releases predate the sentinel and must remain marker-upgradeable.
    # Once the sentinel exists, every PWA critical file is mandatory.
    if not (release / PWA_RELEASE_SENTINEL).is_file():
        pwa_paths = set(PWA_CRITICAL_RELEASE_FILES)
        paths = [relative for relative in paths if relative not in pwa_paths]
    if not (release / REMOTE_BRIDGE_RELEASE_SENTINEL).is_file():
        remote_bridge_paths = set(REMOTE_BRIDGE_CRITICAL_RELEASE_FILES)
        paths = [relative for relative in paths if relative not in remote_bridge_paths]
    paths.extend(relative for relative in RUNTIME_FILES if (release / relative).exists())
    return tuple(dict.fromkeys(paths))


def release_hashes(release: pathlib.Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in critical_release_paths(release):
        target = release.joinpath(*validate_member_name(relative))
        if target.is_symlink() or not target.is_file():
            raise DeployError(f"Kritische Releasedatei fehlt oder ist unsicher: {relative}")
        hashes[relative] = sha256_path(target)
    return dict(sorted(hashes.items()))


def release_marker_payload(
    *,
    commit: str,
    created_at_unix: int,
    critical_sha256: dict[str, str],
    upgraded_from_marker_sha256: str | None = None,
) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "schema_version": 2,
        "kind": "audio_control_release",
        "commit": commit,
        "created_at_unix": created_at_unix,
        "critical_sha256": dict(sorted(critical_sha256.items())),
        "index_sha256": critical_sha256["ui/index.html"],
        "app_sha256": critical_sha256["ui/app.js"],
        "styles_sha256": critical_sha256["ui/styles.css"],
    }
    if upgraded_from_marker_sha256 is not None:
        marker["upgraded_at_unix"] = int(time.time())
        marker["upgraded_from_marker_sha256"] = upgraded_from_marker_sha256
    return marker


def verify_recorded_release_marker(
    release: pathlib.Path,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    marker = release_marker(release)
    if marker.get("kind") != "audio_control_release":
        raise DeployError(f"Releasebeleg hat einen unbekannten Typ: {release}")
    commit = marker.get("commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise DeployError(f"Releasebeleg enthält keinen vollständigen Commit: {release}")
    if expected_commit is not None and commit != expected_commit:
        raise DeployError(f"Releasebeleg und erwarteter Commit widersprechen sich: {release}")

    critical = marker.get("critical_sha256")
    if isinstance(critical, dict):
        for relative, expected_hash in critical.items():
            if (
                not isinstance(relative, str)
                or not isinstance(expected_hash, str)
                or not SHA256_RE.fullmatch(expected_hash)
            ):
                raise DeployError(f"Releasebeleg hat ungültige Hashwerte: {release}")
            target = release.joinpath(*validate_member_name(relative))
            if target.is_symlink() or not target.is_file():
                raise DeployError(f"Gebundene Releasedatei fehlt oder ist unsicher: {relative}")
            if sha256_path(target) != expected_hash:
                raise DeployError(f"Releasedatei weicht vom Beleg ab: {relative}")
        return marker

    legacy = {
        "ui/index.html": marker.get("index_sha256"),
        "ui/app.js": marker.get("app_sha256"),
        "ui/styles.css": marker.get("styles_sha256"),
    }
    for relative, expected_hash in legacy.items():
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise DeployError(f"Legacy-Releasebeleg ist unvollständig: {release}")
        target = release / relative
        if target.is_symlink() or not target.is_file() or sha256_path(target) != expected_hash:
            raise DeployError(f"Legacy-Releasedatei weicht vom Beleg ab: {relative}")
    return marker


def verify_release_marker(
    release: pathlib.Path,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    marker = verify_recorded_release_marker(release, expected_commit=expected_commit)
    critical = marker.get("critical_sha256")
    if isinstance(critical, dict):
        required = set(critical_release_paths(release))
        missing = sorted(required - set(critical))
        unexpected = sorted(set(critical) - required)
        if missing:
            raise DeployError("Releasebeleg ist unvollständig: " + ", ".join(missing))
        if unexpected:
            raise DeployError(
                "Releasebeleg enthält unerwartete kritische Pfade: "
                + ", ".join(unexpected)
            )
    return marker


def current_release_target(
    deploy_root: pathlib.Path,
) -> tuple[str, pathlib.Path] | None:
    current = deploy_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise DeployError("Deploymentzeiger ist kein symbolischer Link.")
    target = pathlib.PurePosixPath(os.readlink(current))
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
        raise DeployError("Deploymentzeiger verweist nicht auf einen gebundenen Release.")
    commit = target.parts[1]
    if not COMMIT_RE.fullmatch(commit):
        raise DeployError("Deploymentzeiger enthält keinen vollständigen Commit.")
    release = deploy_root / "releases" / commit
    if release.is_symlink() or not release.is_dir():
        raise DeployError("Deploymentzeiger verweist nicht auf ein vertrauenswürdiges Release.")
    return commit, release


def read_regular_file_snapshot(path: pathlib.Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DeployError(f"Kritische Releasedatei ist nicht sicher lesbar: {path}") from error
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise DeployError(f"Kritische Releasedatei ist nicht regulär: {path}")
        return handle.read()


def git_blob_oid(payload: bytes, object_format: str) -> str:
    if object_format != "sha1":
        raise DeployError(f"Nicht unterstütztes Git-Objektformat: {object_format}")
    framed = f"blob {len(payload)}\0".encode("ascii") + payload
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest()


def upgrade_current_release_marker(
    repository: pathlib.Path,
    deploy_root: pathlib.Path,
) -> dict[str, Any]:
    current = current_release_target(deploy_root)
    if current is None:
        return {"changed": False, "reason": "no-current-release"}
    commit, release = current
    marker = verify_recorded_release_marker(release, expected_commit=commit)
    required_paths = critical_release_paths(release)
    critical = marker.get("critical_sha256")
    if isinstance(critical, dict) and set(required_paths) == set(critical):
        verify_release_marker(release, expected_commit=commit)
        return {"changed": False, "commit": commit, "reason": "marker-current"}

    created_at = marker.get("created_at_unix")
    if not isinstance(created_at, int) or created_at < 0:
        raise DeployError("Releasebeleg enthält keinen gültigen Erzeugungszeitpunkt.")
    marker_path = release / ".audio-control-release.json"
    previous_payload = read_regular_file_snapshot(marker_path)
    previous_sha256 = hashlib.sha256(previous_payload).hexdigest()
    previous_mode = stat.S_IMODE(marker_path.stat().st_mode)

    format_result = run_command(
        ["git", "--git-dir", str(repository), "rev-parse", "--show-object-format"],
        timeout=15,
    )
    object_format = format_result.stdout.strip()
    if object_format != "sha1":
        raise DeployError("Deployment-Repository verwendet nicht das erwartete SHA-1-Format.")

    bound_hashes: dict[str, str] = {}
    bindings: list[dict[str, Any]] = []
    git_receipts = [format_result.receipt()]
    for relative in required_paths:
        target = release.joinpath(*validate_member_name(relative))
        payload = read_regular_file_snapshot(target)
        expected_result = run_command(
            [
                "git",
                "--git-dir",
                str(repository),
                "rev-parse",
                "--verify",
                f"{commit}:{relative}",
            ],
            timeout=15,
        )
        git_receipts.append(expected_result.receipt())
        expected_oid = expected_result.stdout.strip()
        observed_oid = git_blob_oid(payload, object_format)
        if not OBJECT_ID_RE.fullmatch(expected_oid) or expected_oid != observed_oid:
            raise DeployError(
                f"Releasedatei ist nicht an den erwarteten Git-Blob gebunden: {relative}"
            )
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        bound_hashes[relative] = payload_sha256
        bindings.append(
            {
                "path": relative,
                "git_blob_oid": observed_oid,
                "sha256": payload_sha256,
            }
        )

    upgraded = release_marker_payload(
        commit=commit,
        created_at_unix=created_at,
        critical_sha256=bound_hashes,
        upgraded_from_marker_sha256=previous_sha256,
    )
    try:
        atomic_write_json(marker_path, upgraded)
        verified = verify_release_marker(release, expected_commit=commit)
    except Exception:
        atomic_replace_bytes(marker_path, previous_payload, previous_mode)
        raise
    return {
        "changed": True,
        "commit": commit,
        "previous_marker_sha256": previous_sha256,
        "marker_sha256": sha256_path(marker_path),
        "critical_file_count": len(bindings),
        "bindings": bindings,
        "git": git_receipts,
        "schema_version": verified.get("schema_version"),
    }

def prune_releases(
    deploy_root: pathlib.Path,
    *,
    current_commit: str,
    keep: int = DEFAULT_RELEASE_RETENTION,
) -> dict[str, Any]:
    releases = deploy_root / "releases"
    candidates: list[tuple[int, str, pathlib.Path]] = []
    warnings: list[str] = []
    if not releases.is_dir() or releases.is_symlink():
        return {"keep": keep, "removed": [], "warnings": ["release-root-unavailable"]}
    for child in releases.iterdir():
        if child.is_symlink() or not child.is_dir() or not COMMIT_RE.fullmatch(child.name):
            warnings.append(f"übersprungen:{child.name}")
            continue
        try:
            marker = verify_release_marker(child, expected_commit=child.name)
            created_at = marker.get("created_at_unix")
            if not isinstance(created_at, int):
                created_at = int(child.stat().st_mtime)
            candidates.append((created_at, child.name, child))
        except (DeployError, OSError) as error:
            warnings.append(f"ungültig:{child.name}:{error}")
    candidates.sort(reverse=True)
    protected = {current_commit}
    for _created_at, commit, _path in candidates:
        if len(protected) >= max(1, keep):
            break
        protected.add(commit)
    removed: list[str] = []
    for _created_at, commit, path in candidates:
        if commit in protected:
            continue
        try:
            shutil.rmtree(path)
            removed.append(commit)
        except OSError as error:
            warnings.append(f"cleanup-fehlgeschlagen:{commit}:{error}")
    return {"keep": max(1, keep), "removed": removed, "warnings": warnings}


def read_current_commit(deploy_root: pathlib.Path) -> str | None:
    current = current_release_target(deploy_root)
    if current is None:
        return None
    commit, release = current
    verify_release_marker(release, expected_commit=commit)
    return commit


def switch_current(deploy_root: pathlib.Path, commit: str) -> None:
    current = deploy_root / "current"
    temporary = deploy_root / f".current-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    os.symlink(str(pathlib.Path("releases") / commit), temporary)
    os.replace(temporary, current)


def remove_current(deploy_root: pathlib.Path) -> None:
    current = deploy_root / "current"
    if current.is_symlink():
        current.unlink()


def service_command(action: str, unit: str, *, check: bool = True) -> CommandResult:
    return run_command(
        ["systemctl", "--user", action, unit],
        timeout=30,
        check=check,
    )


def activate_service(unit: str) -> list[dict[str, Any]]:
    results = [
        run_command(["systemctl", "--user", "daemon-reload"], timeout=30),
        service_command("stop", unit),
        service_command("reset-failed", unit, check=False),
        service_command("start", unit),
    ]
    return [result.receipt() for result in results]


def stop_service(unit: str) -> None:
    service_command("stop", unit, check=False)


def release_supports_remote_bridge(release: pathlib.Path) -> bool:
    sentinel = release / REMOTE_BRIDGE_RELEASE_SENTINEL
    return sentinel.is_file() and not sentinel.is_symlink()


def read_service_activity(unit: str) -> dict[str, Any]:
    result = run_command(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
        ],
        timeout=15,
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    load_state = values.get("LoadState", "")
    if load_state == "not-found":
        return {
            "unit": unit,
            "active": False,
            "active_state": "not-found",
            "load_state": load_state,
            "readback": result.receipt(),
        }
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "kein Detail"
        raise DeployError(f"Dienstzustand von {unit} ist nicht lesbar: {detail}")
    if load_state != "loaded":
        raise DeployError(f"Dienst {unit} ist nicht geladen: {load_state!r}.")
    active_state = values.get("ActiveState", "")
    if active_state not in {"active", "inactive", "failed"}:
        raise DeployError(
            f"Dienst {unit} befindet sich in einem nicht stabilen Zustand: {active_state!r}"
        )
    return {
        "unit": unit,
        "active": active_state == "active",
        "active_state": active_state,
        "load_state": load_state,
        "readback": result.receipt(),
    }


def verify_remote_bridge(
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    attempts: int = 40,
) -> dict[str, Any]:
    last_error = "noch keine Antwort"
    for _attempt in range(attempts):
        connection = http.client.HTTPConnection(host, port, timeout=3)
        try:
            connection.request(
                "GET",
                "/bridge/v1/health",
                headers={"Host": f"{host}:{port}"},
            )
            response = connection.getresponse()
            body = response.read(MAX_STATIC_BYTES + 1)
            if len(body) > MAX_STATIC_BYTES:
                last_error = "Bridge-Healthantwort ist zu groß"
                continue
            marker = response.getheader("X-Audio-Remote-Bridge", "")
            if response.status != 200:
                last_error = f"Bridge-Healthstatus {response.status}"
                continue
            if marker != "read-only-v1":
                last_error = f"Bridge-Marker ist {marker!r}"
                continue
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                last_error = "Bridge-Healthantwort hat falsche Form"
                continue
            if payload.get("kind") != "audio_remote_bridge_health":
                last_error = "Bridge-Healthantwort hat falsche Identität"
                continue
            if payload.get("status") != "serving" or payload.get("projection") != "read-only":
                last_error = "Bridge meldet keinen read-only Serving-Zustand"
                continue
            if payload.get("effect_authority") is not False:
                last_error = "Bridge meldet unerwartete Effekt-Autorität"
                continue
            backend = payload.get("backend")
            if not isinstance(backend, dict) or backend.get("remote_exposure") is not False:
                last_error = "Bridge meldet unerwartete Backend-Exposition"
                continue
            return {
                "url": f"http://{host}:{port}/bridge/v1/health",
                "marker": marker,
                "health": payload,
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = str(error)
        finally:
            connection.close()
        time.sleep(0.25)
    raise DeployError(f"Remote-Bridge ist nicht gesund: {last_error}")


def restart_remote_bridge() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    restart = service_command("restart", REMOTE_BRIDGE_UNIT)
    activity = read_service_activity(REMOTE_BRIDGE_UNIT)
    if not activity["active"]:
        raise DeployError("Remote-Bridge ist nach Restart nicht aktiv.")
    health = verify_remote_bridge()
    return [restart.receipt()], activity, health


def fetch_bytes(host: str, port: int, path: str) -> tuple[int, bytes, str]:
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", path, headers={"Host": f"{host}:{port}"})
        response = connection.getresponse()
        body = response.read(MAX_STATIC_BYTES + 1)
        if len(body) > MAX_STATIC_BYTES:
            raise DeployError(f"HTTP-Antwort ist zu groß: {path}")
        return response.status, body, response.getheader("Content-Type", "")
    finally:
        connection.close()


def verify_service(
    release: pathlib.Path,
    *,
    host: str,
    port: int,
    attempts: int = 40,
) -> dict[str, Any]:
    marker = verify_release_marker(release)
    expected_commit = marker["commit"]
    expected_static = {
        endpoint: {
            "sha256": sha256_path(release / relative),
            "content_types": content_types,
        }
        for endpoint, relative, content_types in STATIC_ENDPOINTS
    }
    last_error = "noch keine Antwort"
    for _attempt in range(attempts):
        try:
            observed: dict[str, str] = {}
            mismatch: str | None = None
            for endpoint, contract in expected_static.items():
                status, body, content_type = fetch_bytes(host, port, endpoint)
                actual = hashlib.sha256(body).hexdigest()
                observed[endpoint] = actual
                if status != 200:
                    mismatch = f"{endpoint}: HTTP {status}"
                    break
                if actual != contract["sha256"]:
                    mismatch = f"{endpoint}: Hash {actual}"
                    break
                if not any(
                    content_type.startswith(expected_type)
                    for expected_type in contract["content_types"]
                ):
                    mismatch = f"{endpoint}: Typ {content_type}"
                    break
            if mismatch is not None:
                last_error = mismatch
            else:
                health_status, health_body, _health_type = fetch_bytes(
                    host, port, "/api/v1/health"
                )
                if health_status != 200:
                    last_error = f"Healthstatus {health_status}"
                else:
                    health = json.loads(health_body.decode("utf-8"))
                    if not isinstance(health, dict):
                        last_error = "Healthantwort hat falsche Form"
                    elif health.get("status") != "serving":
                        last_error = f"Healthstatus ist {health.get('status')!r}"
                    elif health.get("authority") != "local-backend":
                        last_error = "Healthantwort stammt nicht vom lokalen Backend"
                    elif health.get("runtime_head") != expected_commit:
                        last_error = (
                            "Healthrevision ist "
                            f"{health.get('runtime_head')!r} statt {expected_commit}"
                        )
                    else:
                        return {
                            "url": f"http://{host}:{port}/",
                            "static_sha256": observed,
                            "health": health,
                        }
        except (OSError, DeployError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise DeployError(f"Dienst bestätigt den erwarteten Release nicht: {last_error}")

def prepare_release(
    repository: pathlib.Path,
    deploy_root: pathlib.Path,
    commit: str,
) -> tuple[pathlib.Path, list[dict[str, Any]], bool]:
    releases = deploy_root / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = releases / commit
    if release.exists():
        if release.is_symlink() or not release.is_dir():
            raise DeployError(f"Releasepfad ist nicht vertrauenswürdig: {release}")
        verify_release_marker(release, expected_commit=commit)
        return release, [], False

    temporary = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{commit}.", dir=str(releases))
    )
    try:
        extract_commit(repository, commit, temporary)
        checks = validate_release(temporary)
        critical_sha256 = release_hashes(temporary)
        marker = release_marker_payload(
            commit=commit,
            created_at_unix=int(time.time()),
            critical_sha256=critical_sha256,
        )
        atomic_write_json(temporary / ".audio-control-release.json", marker)
        os.replace(temporary, release)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return release, checks, True


def resolve_target(
    repository: pathlib.Path,
    *,
    branch: str,
) -> tuple[str, list[dict[str, Any]]]:
    branch_check = run_command(
        ["git", "check-ref-format", "--branch", branch],
        timeout=15,
    )
    fetch = run_command(
        [
            "git",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.ssh.allow=always",
            "-c",
            "protocol.https.allow=always",
            "--git-dir",
            str(repository),
            "fetch",
            "--quiet",
            "--no-tags",
            "--prune",
            "--no-write-fetch-head",
            "source",
            f"+refs/heads/{branch}:refs/audio-deploy/target",
        ],
        timeout=90,
    )
    revision = run_command(
        [
            "git",
            "--git-dir",
            str(repository),
            "rev-parse",
            "--verify",
            "refs/audio-deploy/target^{commit}",
        ],
        timeout=15,
    )
    commit = revision.stdout.strip()
    if not COMMIT_RE.fullmatch(commit):
        raise DeployError("Remoteziel lieferte keinen vollständigen Commit.")
    return commit, [
        branch_check.receipt(),
        fetch.receipt(),
        revision.receipt(),
    ]

def sync(args: argparse.Namespace) -> dict[str, Any]:
    args.deploy_root, args.state_root = validate_runtime_roots(
        args.deploy_root, args.state_root
    )
    validate_ui_endpoint(args.host, args.port)
    source_repo = ensure_source_repo(args.source_repo)
    deploy_root = ensure_private_directory(args.deploy_root)
    state_root = ensure_private_directory(args.state_root)
    lock_path = state_root / "deploy.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "r+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        repository, repository_receipts = prepare_deployment_repository(
            source_repo,
            state_root,
            remote=args.remote,
        )
        commit, target_receipts = resolve_target(
            repository,
            branch=args.branch,
        )
        git_receipts = repository_receipts + target_receipts
        if args.expected_commit and commit != args.expected_commit:
            raise DeployError(
                f"Remoteziel {commit} entspricht nicht dem erwarteten Commit "
                f"{args.expected_commit}."
            )
        marker_upgrade = upgrade_current_release_marker(repository, deploy_root)
        previous = read_current_commit(deploy_root)
        bridge_before: dict[str, Any] = {
            "unit": REMOTE_BRIDGE_UNIT,
            "supported": False,
            "active": False,
            "active_state": "not-applicable",
        }
        if previous is not None:
            previous_release = deploy_root / "releases" / previous
            if release_supports_remote_bridge(previous_release):
                bridge_before = {
                    "supported": True,
                    **read_service_activity(REMOTE_BRIDGE_UNIT),
                }
        release, validation_receipts, created = prepare_release(
            repository, deploy_root, commit
        )
        if bridge_before["active"] and not release_supports_remote_bridge(release):
            raise DeployError(
                "Aktiver Remote-Bridge blockiert den Wechsel auf einen Release ohne Bridgevertrag."
            )
        changed = previous != commit
        if changed:
            switch_current(deploy_root, commit)
        runtime_updates: list[dict[str, Any]] = []
        runtime_backups: list[dict[str, Any]] = []
        runtime_activation: list[dict[str, Any]] = []
        runtime_environment: dict[str, Any] = {}
        runtime_environment_backup: dict[str, Any] | None = None
        service_receipts: list[dict[str, Any]] = []
        service_activation_attempted = False
        bridge_activation_receipts: list[dict[str, Any]] = []
        bridge_activity_after: dict[str, Any] | None = None
        bridge_health: dict[str, Any] | None = None
        bridge_activation_attempted = False
        bridge_restart_required = False
        runtime_unit_changed = False
        timer_updated = False
        retention: dict[str, Any] = {"keep": DEFAULT_RELEASE_RETENTION, "removed": [], "warnings": []}
        try:
            runtime_environment, runtime_environment_backup = reconcile_runtime_environment(
                UI_RUNTIME_ENV,
                host=args.host,
                port=args.port,
            )
            runtime_updates, runtime_backups = install_release_runtime(release)
            updated_sources = {update["source"] for update in runtime_updates}
            runtime_unit_changed = any(
                source.endswith((".service", ".timer"))
                for source in updated_sources
            )
            ui_unit_updated = (
                "systemd/user/audio-control-ui-v1.service" in updated_sources
            )
            bridge_unit_updated = (
                "systemd/user/audio-remote-bridge-v1.service" in updated_sources
            )
            bridge_restart_required = bool(bridge_before["active"]) and (
                changed or bridge_unit_updated
            )
            timer_updated = "systemd/user/audio-control-deploy.timer" in updated_sources
            restart_required = (
                changed
                or bool(runtime_environment.get("changed"))
                or ui_unit_updated
            )
            if restart_required:
                service_activation_attempted = True
                service_receipts = activate_service(args.unit)
                service = verify_service(
                    release,
                    host=args.host,
                    port=args.port,
                )
            else:
                if runtime_unit_changed:
                    runtime_activation.append(
                        run_command(
                            ["systemctl", "--user", "daemon-reload"],
                            timeout=30,
                        ).receipt()
                    )
                try:
                    service = verify_service(
                        release,
                        host=args.host,
                        port=args.port,
                        attempts=2,
                    )
                except DeployError:
                    service_activation_attempted = True
                    service_receipts = activate_service(args.unit)
                    service = verify_service(
                        release,
                        host=args.host,
                        port=args.port,
                    )
            if timer_updated:
                runtime_activation.append(
                    run_command(
                        ["systemctl", "--user", "restart", "audio-control-deploy.timer"],
                        timeout=30,
                    ).receipt()
                )
            if bridge_restart_required:
                bridge_activation_attempted = True
                (
                    bridge_activation_receipts,
                    bridge_activity_after,
                    bridge_health,
                ) = restart_remote_bridge()
            retention = prune_releases(deploy_root, current_commit=commit)
        except Exception:
            restore_runtime_environment(runtime_environment_backup)
            if runtime_backups:
                restore_release_runtime(runtime_backups)
                if runtime_unit_changed:
                    with contextlib.suppress(Exception):
                        run_command(
                            ["systemctl", "--user", "daemon-reload"],
                            timeout=30,
                        )
                if timer_updated:
                    with contextlib.suppress(Exception):
                        run_command(
                            [
                                "systemctl",
                                "--user",
                                "restart",
                                "audio-control-deploy.timer",
                            ],
                            timeout=30,
                        )
            if changed:
                if previous:
                    switch_current(deploy_root, previous)
                    with contextlib.suppress(Exception):
                        activate_service(args.unit)
                else:
                    remove_current(deploy_root)
                    stop_service(args.unit)
            elif runtime_environment_backup is not None or service_activation_attempted:
                with contextlib.suppress(Exception):
                    activate_service(args.unit)
            if bridge_before["active"] and bridge_activation_attempted:
                with contextlib.suppress(Exception):
                    activate_service(REMOTE_BRIDGE_UNIT)
            raise
        deployed_at = int(time.time())
        receipt = {
            "schema_version": 1,
            "kind": "audio_control_deploy_receipt",
            "source_repo": str(source_repo),
            "deployment_repository": str(repository),
            "remote_ref": f"{args.remote}/{args.branch}",
            "commit": commit,
            "previous_commit": previous,
            "changed": changed,
            "release_created": created,
            "deployed_at_unix": deployed_at,
            "unit": args.unit,
            "git": git_receipts,
            "validation": validation_receipts,
            "release_marker_upgrade": marker_upgrade,
            "runtime_updates": runtime_updates,
            "runtime_environment": runtime_environment,
            "runtime_activation": runtime_activation,
            "service_commands": service_receipts,
            "remote_bridge": {
                "before": bridge_before,
                "restart_required": bridge_restart_required,
                "activation": bridge_activation_receipts,
                "after": bridge_activity_after,
                "health": bridge_health,
            },
            "release_retention": retention,
            "service": service,
        }
        receipt_dir = state_root / "receipts"
        atomic_write_json(receipt_dir / f"{deployed_at}-{commit}.json", receipt)
        atomic_write_json(state_root / "latest.json", receipt)
        return receipt


def status(args: argparse.Namespace) -> dict[str, Any]:
    args.deploy_root, args.state_root = validate_runtime_roots(
        args.deploy_root, args.state_root
    )
    deploy_root = ensure_private_directory(args.deploy_root)
    state_root = ensure_private_directory(args.state_root)
    current = read_current_commit(deploy_root)
    service = run_command(
        [
            "systemctl",
            "--user",
            "show",
            args.unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=UnitFileState",
            "--property=FragmentPath",
        ],
        timeout=15,
        check=False,
    )
    latest_path = state_root / "latest.json"
    latest: dict[str, Any] | None = None
    if latest_path.is_file() and not latest_path.is_symlink():
        with latest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            latest = payload
    values: dict[str, str] = {}
    for line in service.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "schema_version": 1,
        "kind": "audio_control_deploy_status",
        "current_commit": current,
        "unit": args.unit,
        "service": values,
        "latest_receipt": latest,
    }


def parse_path(value: str) -> pathlib.Path:
    return pathlib.Path(value).expanduser()


def normalized_absolute_path(path: pathlib.Path, *, label: str) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise DeployError(f"{label} muss absolut sein.")
    return pathlib.Path(os.path.normpath(str(expanded)))


def validate_runtime_roots(
    deploy_root: pathlib.Path, state_root: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    deploy = normalized_absolute_path(deploy_root, label="Deploy-Root")
    state = normalized_absolute_path(state_root, label="State-Root")
    if deploy != DEFAULT_DEPLOY_ROOT:
        raise DeployError(
            f"Version 1 unterstützt nur den Deploy-Root {DEFAULT_DEPLOY_ROOT}."
        )
    if state != DEFAULT_STATE_ROOT:
        raise DeployError(
            f"Version 1 unterstützt nur den State-Root {DEFAULT_STATE_ROOT}."
        )
    return deploy, state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-repo",
        type=parse_path,
        default=parse_path(os.environ.get("AUDIO_CONTROL_SOURCE_REPO", str(DEFAULT_SOURCE_REPO))),
    )
    parser.add_argument(
        "--deploy-root",
        type=parse_path,
        default=parse_path(os.environ.get("AUDIO_CONTROL_DEPLOY_ROOT", str(DEFAULT_DEPLOY_ROOT))),
        help=f"V1 ist fest auf {DEFAULT_DEPLOY_ROOT} gebunden",
    )
    parser.add_argument(
        "--state-root",
        type=parse_path,
        default=parse_path(os.environ.get("AUDIO_CONTROL_STATE_ROOT", str(DEFAULT_STATE_ROOT))),
        help=f"V1 ist fest auf {DEFAULT_STATE_ROOT} gebunden",
    )
    parser.add_argument(
        "--remote", default=os.environ.get("AUDIO_CONTROL_REMOTE", DEFAULT_REMOTE)
    )
    parser.add_argument(
        "--branch", default=os.environ.get("AUDIO_CONTROL_BRANCH", DEFAULT_BRANCH)
    )
    parser.add_argument("--unit", default=os.environ.get("AUDIO_CONTROL_UNIT", DEFAULT_UNIT))
    parser.add_argument("--host", default=os.environ.get("AUDIO_CONTROL_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AUDIO_CONTROL_PORT", str(DEFAULT_PORT))),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync", help="origin/main atomar ausrollen")
    sync_parser.add_argument("--expected-commit", default="")
    subparsers.add_parser("status", help="Deploymentzustand lesen")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.deploy_root, args.state_root = validate_runtime_roots(
            args.deploy_root, args.state_root
        )
        if args.command == "sync":
            report = sync(args)
        elif args.command == "status":
            report = status(args)
        else:
            parser.error(f"Unbekannter Befehl: {args.command}")
            return 2
    except (DeployError, OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_control_deploy_error",
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
