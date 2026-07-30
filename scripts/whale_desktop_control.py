#!/usr/bin/env python3
"""Desktop-friendly controller for Buckelwal Live Voice."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE = ROOT / "scripts" / "whale_live.py"


def run_live(*arguments: str) -> tuple[int, dict[str, object]]:
    result = subprocess.run(
        [sys.executable, str(LIVE), *arguments],
        check=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    text = result.stdout.strip() or result.stderr.strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"state": "unknown", "detail": text}
    return result.returncode, payload


def notify(title: str, body: str) -> None:
    command = shutil.which("notify-send")
    if not command:
        return
    subprocess.run(
        [command, "--app-name=Buckelwal", title, body],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


def active(status: dict[str, object]) -> bool:
    return status.get("active_state") in {"active", "activating", "reloading"}


def describe(payload: dict[str, object]) -> str:
    state = payload.get("state") or payload.get("active_state") or "unbekannt"
    mode = payload.get("voice_mode")
    if mode == "morph":
        mode = "spielbar"
    elif mode == "organic":
        mode = "organisch"
    elif mode == "realistic":
        mode = "Sample"
    elif mode == "ufo":
        mode = "UFO"
    return f"Status: {state}" + (f" · Modus: {mode}" if mode else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("toggle", "on", "off", "morph", "organic", "realistic", "ufo", "status")
    )
    args = parser.parse_args()

    status_code, status = run_live("status")
    if args.action == "status":
        notify("Buckelwal-Status", describe(status))
        print(json.dumps(status, indent=2, sort_keys=True))
        return status_code

    if args.action == "off":
        code, payload = run_live("stop")
        notify("Buckelwal ausgeschaltet", describe(payload))
    elif args.action == "toggle":
        if active(status):
            code, payload = run_live("stop")
        else:
            code, payload = run_live("start", "--voice-mode", "morph")
        notify("Buckelwal umgeschaltet", describe(payload))
    elif args.action == "on":
        if active(status):
            code, payload = 0, status
        else:
            code, payload = run_live("start", "--voice-mode", "morph")
        notify("Buckelwal eingeschaltet", describe(payload))
    else:
        mode = args.action
        code, payload = run_live("mode", mode)
        titles = {
            "morph": "Buckelwal spielbar",
            "organic": "Buckelwal organisch",
            "realistic": "Buckelwal Sample-Vergleich",
            "ufo": "Buckelwal UFO-Vergleich",
        }
        title = titles[mode]
        notify(title, describe(payload))

    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
