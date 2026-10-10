#!/usr/bin/env python3
"""
Build "Switchboard Settings.app" — a plain, double-clickable macOS app
(no Dock presence required, no code signing, no new dependency) that
opens the settings web UI, for a user who has no reason to know
`switchboard_bridge.py settings` is a command that exists.

Wraps the existing `settings` CLI subcommand, which already does the
real work (confirm a bridge is actually listening, resolve the per-run
token, open the page) — this just gives that command an icon. See
switchboard/osa_app.py for how the .app itself is built.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from switchboard.osa_app import build_script, compile_app

HOST_DIR = Path(__file__).resolve().parent
BRIDGE = HOST_DIR / "switchboard_bridge.py"
PYTHON = HOST_DIR / ".venv" / "bin" / "python3"
APP_PATH = Path.home() / "Applications" / "Switchboard Settings.app"
ICON_PATH = HOST_DIR / "switchboard" / "icons" / "settings.icns"


def build_command() -> str:
    python = str(PYTHON) if PYTHON.exists() else "python3"
    return shlex.join([python, str(BRIDGE), "settings"])


def install(*, dry_run: bool = False) -> int:
    script = build_script(build_command(), notification_title="Switchboard")
    if dry_run:
        print(script)
        print(f"Would write: {APP_PATH}")
        return 0

    compile_app(script, APP_PATH, icon_path=ICON_PATH if ICON_PATH.exists() else None)
    print(f"Installed {APP_PATH}")
    print("Double-click it any time to open the settings page (or drag it to the Dock).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Switchboard Settings.app")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return install(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
