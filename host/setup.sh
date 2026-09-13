#!/usr/bin/env bash
# One-command setup for the Switchboard bridge on a new Mac.
#
# Idempotent: safe to re-run. Skips anything that already exists (venv,
# agents.json) rather than clobbering it.
set -euo pipefail

# --- output helpers -----------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'
else
  BOLD=""; DIM=""; RESET=""; GREEN=""; YELLOW=""; RED=""; CYAN=""
fi

step()  { printf '%s%s==>%s %s\n' "$BOLD" "$CYAN" "$RESET" "$1"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
skip()  { printf '  %s-%s %s\n' "$DIM" "$RESET" "$1${DIM} (already set up, skipping)${RESET}"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()   { printf '%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }

trap 'die "Setup failed. Nothing after this point was applied — safe to fix the issue above and re-run ./setup.sh."' ERR

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$HOST_DIR")"

printf '%s%sSwitchboard Bridge Setup%s\n' "$BOLD" "$CYAN" "$RESET"
printf '%s%s%s\n\n' "$DIM" "$PROJECT_ROOT" "$RESET"

# --- prerequisites -------------------------------------------------------
step "Checking prerequisites"
[ "$(uname -s)" = "Darwin" ] || die "This bridge is macOS-only (uses AppleScript, Quartz, and launchctl)."
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install it (e.g. via Homebrew) and re-run."
ok "macOS detected"
ok "python3 found: $(command -v python3)"

# --- virtualenv ------------------------------------------------------------
step "Setting up the Python virtualenv"
if [ ! -d "$HOST_DIR/.venv" ]; then
  python3 -m venv "$HOST_DIR/.venv"
  ok "Created host/.venv"
else
  skip "Virtualenv"
fi

step "Installing dependencies"
"$HOST_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOST_DIR/.venv/bin/pip" install --quiet pyobjc-framework-Quartz pyobjc-framework-ApplicationServices
ok "pyobjc-framework-Quartz (needed for real push-to-talk hold/release)"

# --- agents.json -----------------------------------------------------------
step "Configuring agent slots"
if [ ! -f "$HOST_DIR/agents.json" ]; then
  cp "$HOST_DIR/agents.example.json" "$HOST_DIR/agents.json"
  ok "Created host/agents.json (agents launch in ~/Documents by default)"
  warn "Edit host/agents.json, or use 'switchboard_bridge.py config', for a different CLI/path mix per slot"
else
  skip "agents.json"
fi

# --- hooks -------------------------------------------------------------
step "Wiring up Claude Code / Codex lifecycle hooks"
"$HOST_DIR/.venv/bin/python3" "$HOST_DIR/switchboard_bridge.py" install-hooks | sed 's/^/  /'
ok "Hooks installed (idempotent — merges into your existing settings)"

# --- launch agent --------------------------------------------------------
step "Installing the login LaunchAgent"
"$HOST_DIR/.venv/bin/python3" "$HOST_DIR/install_bridge_launch_agent.py" | sed 's/^/  /'
ok "Bridge will now start automatically at login"

# --- summary ---------------------------------------------------------------
printf '\n%s%sSetup complete%s\n' "$BOLD" "$GREEN" "$RESET"
cat <<EOF

  The bridge is running now and will start automatically every time you
  log in — no manual command needed going forward.

  Next steps:
    1. Plug in the ESP32-S3 + NeoKey board.
    2. Press an agent key to confirm it launches/selects a session.
    3. The first time you use push-to-talk, macOS will prompt for
       Accessibility permission — grant it, then restart the bridge to
       pick it up:
         launchctl unload ~/Library/LaunchAgents/com.switchboard.bridge.plist
         launchctl load ~/Library/LaunchAgents/com.switchboard.bridge.plist
       (or just re-run this script)

  Logs:   ~/Library/Logs/Switchboard/bridge.{out,err}.log
  Config: host/agents.json
EOF
