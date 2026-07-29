#!/usr/bin/env python3
"""Install desktop launchers and a GNOME toggle shortcut for the whale voice."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import shlex
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "whale_desktop_control.py"
APPLICATIONS = pathlib.Path.home() / ".local" / "share" / "applications"
KEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/buckelwal-toggle/"
)

ACTIONS = {
    "toggle": (
        "Buckelwal – An/Aus",
        "Walstimme ein- oder ausschalten",
        "media-playback-start",
    ),
    "morph": (
        "Buckelwal – Spielbar",
        "Durchgehende Walstimme über alle 88 Tasten",
        "audio-card",
    ),
    "realistic": (
        "Buckelwal – Sample-Vergleich",
        "Frühere echte Aufnahmephrasen vergleichen",
        "audio-input-microphone",
    ),
    "ufo": (
        "Buckelwal – UFO-Modus",
        "Früheren Synthesizer verwenden",
        "applications-multimedia",
    ),
    "off": ("Buckelwal – Aus", "Walstimme vollständig beenden", "media-playback-stop"),
    "status": (
        "Buckelwal – Status",
        "Aktuellen Walstimmenstatus anzeigen",
        "dialog-information",
    ),
}


def desktop_exec_quote(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError("desktop command arguments must be one line")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def desktop_exec_command(action: str) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown desktop action: {action}")
    return " ".join(
        desktop_exec_quote(value) for value in (sys.executable, str(CONTROL), action)
    )


def shortcut_command() -> str:
    return shlex.join([sys.executable, str(CONTROL), "toggle"])


def desktop_text(action: str) -> str:
    name, comment, icon = ACTIONS[action]
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={name}",
            f"Comment={comment}",
            f"Exec={desktop_exec_command(action)}",
            f"Icon={icon}",
            "Terminal=false",
            "Categories=AudioVideo;Audio;Utility;",
            "StartupNotify=false",
            "X-GNOME-UsesNotifications=true",
            "",
        ]
    )


def run_gsettings(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gsettings", *arguments],
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )


def parse_custom_keybindings(value: str) -> list[str]:
    text = value.strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    parsed = ast.literal_eval(text)
    if not isinstance(parsed, (list, tuple)) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise ValueError("custom-keybindings is not a string array")
    return list(parsed)


def install_shortcut(binding: str) -> dict[str, object]:
    if not shutil.which("gsettings"):
        return {"installed": False, "reason": "gsettings-not-found"}
    schema = "org.gnome.settings-daemon.plugins.media-keys"
    current = run_gsettings("get", schema, "custom-keybindings")
    if current.returncode != 0:
        return {
            "installed": False,
            "reason": current.stderr.strip() or "gsettings-read-failed",
        }
    try:
        paths = parse_custom_keybindings(current.stdout)
    except (SyntaxError, ValueError):
        return {"installed": False, "reason": "invalid-custom-keybindings-state"}
    if KEY_PATH not in paths:
        paths.append(KEY_PATH)
        result = run_gsettings("set", schema, "custom-keybindings", repr(paths))
        if result.returncode != 0:
            return {
                "installed": False,
                "reason": result.stderr.strip() or "gsettings-write-failed",
            }
    custom_schema = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    command = shortcut_command()
    settings = {
        "name": "Buckelwal an/aus",
        "command": command,
        "binding": binding,
    }
    for key, value in settings.items():
        result = run_gsettings("set", f"{custom_schema}:{KEY_PATH}", key, repr(value))
        if result.returncode != 0:
            return {
                "installed": False,
                "reason": result.stderr.strip() or f"gsettings-{key}-failed",
            }
    return {"installed": True, "binding": binding, "command": command, "path": KEY_PATH}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", default="<Super><Alt>w")
    parser.add_argument("--skip-shortcut", action="store_true")
    args = parser.parse_args()

    APPLICATIONS.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for action in ACTIONS:
        path = APPLICATIONS / f"buckelwal-{action}.desktop"
        path.write_text(desktop_text(action), encoding="utf-8")
        path.chmod(0o755)
        installed.append(str(path))
    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run(
            [updater, str(APPLICATIONS)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    shortcut = (
        {"installed": False, "reason": "skipped"}
        if args.skip_shortcut
        else install_shortcut(args.binding)
    )
    print(
        json.dumps(
            {
                "state": "installed",
                "desktop_entries": installed,
                "shortcut": shortcut,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if shortcut.get("installed") or args.skip_shortcut else 2


if __name__ == "__main__":
    raise SystemExit(main())
