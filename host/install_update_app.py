#!/usr/bin/env python3
"""
Build "Switchboard Update.app" — a double-clickable macOS app that pulls
the latest release and re-runs setup.sh, so updating a deployed unit
never requires a terminal. Same osacompile mechanism as
install_settings_app.py; see switchboard/osa_app.py.

Intentionally simple, matching the macOS-only, dev-checkout-based scope
of this hardening pass (docs/design/hardening-pass-plan.md Workstream B):
`git pull --ff-only` plus setup.sh re-running every install step
(idempotent — see Workstream B), not a signed/versioned release
pipeline. `--ff-only` refuses to proceed (visibly, via the same
notification path as any other failure) rather than silently merging or
rebasing if the local checkout ever has commits of its own.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from switchboard.osa_app import build_script, compile_app

HOST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HOST_DIR.parent
SETUP_SH = HOST_DIR / "setup.sh"
APP_PATH = Path.home() / "Applications" / "Switchboard Update.app"


def build_command() -> str:
    return " && ".join(
        [
            f"cd {shlex.quote(str(PROJECT_ROOT))}",
            "git pull --ff-only",
            shlex.quote(str(SETUP_SH)),
        ]
    )


def install(*, dry_run: bool = False) -> int:
    script = build_script(
        build_command(),
        notification_title="Switchboard",
        success_message="Switchboard is up to date.",
    )
    if dry_run:
        print(script)
        print(f"Would write: {APP_PATH}")
        return 0

    compile_app(script, APP_PATH)
    print(f"Installed {APP_PATH}")
    print("Double-click it any time to pull the latest version and re-run setup.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Switchboard Update.app")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return install(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
