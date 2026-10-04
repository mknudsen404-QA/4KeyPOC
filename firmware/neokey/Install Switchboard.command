#!/usr/bin/env bash
# Switchboard bridge bootstrap installer.
#
# This is the file that ships on the ESP32-S3's virtual USB drive. It is
# deliberately thin: it just checks whether the bridge is installed, asks
# permission, clones the (public) repo, and hands off to the real
# host/setup.sh. All actual install logic lives in setup.sh so that
# updating install steps later never requires re-flashing every board.
set -euo pipefail

REPO_URL="https://github.com/mknudsen404-QA/4KeyPOC.git"
INSTALL_DIR="$HOME/4KeyPOC"
PLIST="$HOME/Library/LaunchAgents/com.switchboard.bridge.plist"
LOG_DIR="$HOME/Library/Logs/Switchboard"

notify() {
  # $1 = message, $2 = "note" or "critical" (icon style)
  osascript -e "display dialog \"$1\" with title \"Switchboard Installer\" buttons {\"OK\"} default button \"OK\" with icon $2" >/dev/null 2>&1 || true
}

confirm() {
  osascript -e "display dialog \"$1\" with title \"Switchboard Installer\" buttons {\"Cancel\", \"Install\"} default button \"Install\" cancel button \"Cancel\" with icon note" >/dev/null 2>&1
}

if [ -f "$PLIST" ]; then
  notify "Switchboard bridge is already installed on this Mac. You're all set — no action needed." note
  exit 0
fi

if ! confirm "This will install the Switchboard bridge: it clones the public 4KeyPOC repo to $INSTALL_DIR and runs its setup script. Continue?"; then
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  notify "git isn't installed yet. macOS should now prompt you to install the Command Line Tools — once that finishes, double-click this installer again." caution
  git --version >/dev/null 2>&1 || true
  exit 1
fi

mkdir -p "$LOG_DIR"
INSTALL_LOG="$LOG_DIR/installer.log"

run_install() {
  echo "=== Switchboard installer run: $(date) ==="

  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Repo already present at $INSTALL_DIR, pulling latest instead of re-cloning."
    git -C "$INSTALL_DIR" pull --ff-only
  else
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi

  cd "$INSTALL_DIR/host"
  ./setup.sh
}

# set -e is intentionally suspended for this one call: a failure here needs
# to fall through to the "show a failure dialog" branch below, not abort
# the whole script on the spot.
set +e
run_install >>"$INSTALL_LOG" 2>&1
INSTALL_STATUS=$?
set -e

if [ "$INSTALL_STATUS" -eq 0 ]; then
  notify "Switchboard bridge installed successfully. Plug in your NeoKey board any time — it'll just work from now on." note
else
  notify "Setup hit a problem. Check the log for details: $INSTALL_LOG" caution
  exit 1
fi
