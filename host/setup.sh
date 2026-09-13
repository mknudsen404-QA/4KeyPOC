#!/usr/bin/env bash
# One-command setup for the Switchboard bridge on a new Mac.
#
# Idempotent: safe to re-run. Skips anything that already exists (venv,
# agents.json) rather than clobbering it.
set -euo pipefail

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$HOST_DIR")"

echo "== Switchboard bridge setup =="
echo "Project root: $PROJECT_ROOT"
echo

if [ ! -d "$HOST_DIR/.venv" ]; then
  echo "-> Creating virtualenv (host/.venv)..."
  python3 -m venv "$HOST_DIR/.venv"
else
  echo "-> Virtualenv already exists, skipping."
fi

echo "-> Installing dependencies (pyobjc-framework-Quartz, needed for real push-to-talk hold/release)..."
"$HOST_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOST_DIR/.venv/bin/pip" install --quiet pyobjc-framework-Quartz pyobjc-framework-ApplicationServices

if [ ! -f "$HOST_DIR/agents.json" ]; then
  echo "-> Creating agents.json from the template, pointed at this machine's project path..."
  python3 - "$HOST_DIR" <<'PYEOF'
import json
import sys
from pathlib import Path

host_dir = Path(sys.argv[1])
project_root = host_dir.parent
example = json.loads((host_dir / "agents.example.json").read_text())
for agent in example.get("agents", []):
    agent["cwd"] = str(project_root)
(host_dir / "agents.json").write_text(json.dumps(example, indent=2) + "\n")
PYEOF
  echo "   host/agents.json created — edit it if you want a different CLI/path mix per slot."
else
  echo "-> agents.json already exists, leaving it alone."
fi

echo "-> Installing Claude Code / Codex lifecycle hooks (idempotent, merges into existing settings)..."
"$HOST_DIR/.venv/bin/python3" "$HOST_DIR/switchboard_bridge.py" install-hooks

echo "-> Installing the bridge as a login LaunchAgent..."
"$HOST_DIR/.venv/bin/python3" "$HOST_DIR/install_bridge_launch_agent.py"

echo
echo "== Done =="
echo "The bridge now runs automatically at login and waits for the board."
echo "Plug in the ESP32-S3 + NeoKey board and press an agent key to test."
echo
echo "The first time you use push-to-talk, macOS will prompt for Accessibility"
echo "permission for python3 — grant it, then try PTT again (a process that's"
echo "already running when permission is granted needs to be restarted to pick"
echo "it up: launchctl unload ~/Library/LaunchAgents/com.switchboard.bridge.plist"
echo "then launchctl load the same path, or just re-run this script)."
