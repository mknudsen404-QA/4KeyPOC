#!/usr/bin/env python3
"""
Install the Switchboard bridge as a macOS LaunchAgent.

This makes the bridge start at login and keep trying when the board is not
plugged in yet. It writes a user-level plist to ~/Library/LaunchAgents.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


HOST_DIR = Path(__file__).resolve().parent
BRIDGE = HOST_DIR / "switchboard_bridge.py"
LABEL = "com.switchboard.bridge"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library/Logs/Switchboard"


def build_plist(args: argparse.Namespace) -> dict:
    program_arguments = [
        sys.executable,
        str(BRIDGE),
        "listen",
        "--auto-launch",
        "--retry",
        "--retry-delay",
        str(args.retry_delay),
    ]
    if args.port:
        program_arguments.extend(["--port", args.port])
    if args.no_open:
        program_arguments.append("--no-open")

    return {
        "Label": LABEL,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(HOST_DIR.parent),
        "StandardOutPath": str(LOG_DIR / "bridge.out.log"),
        "StandardErrorPath": str(LOG_DIR / "bridge.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Switchboard bridge LaunchAgent.")
    parser.add_argument("--port", help="Pin bridge to a specific serial port")
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--no-open", action="store_true", help="Register slots without opening Terminal")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    plist = build_plist(args)
    if args.dry_run:
        print(plistlib.dumps(plist).decode("utf-8"))
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as handle:
        plistlib.dump(plist, handle)

    subprocess.run(["launchctl", "unload", str(PLIST)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"Installed and started {LABEL}")
    print(f"Plist: {PLIST}")
    print(f"Logs: {LOG_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
