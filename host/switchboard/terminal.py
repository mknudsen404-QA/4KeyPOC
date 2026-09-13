"""Driving Terminal.app via AppleScript, plus the no-op/fake variants used
for --no-open, --dry-run, and unit tests. `osascript` only ever appears in
this module.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Protocol

try:
    import Quartz  # type: ignore[import]

    QUARTZ_AVAILABLE = True
except ImportError:
    QUARTZ_AVAILABLE = False


class TerminalDriver(Protocol):
    def open(self, shell_command: str) -> str | None: ...

    def focus(self, tty: str) -> bool: ...

    def terminal_pid(self) -> int | None: ...

    def post_key(self, pid: int, keycode: int, down: bool) -> None: ...


def _find_tab_script(body: str) -> str:
    """AppleScript fragment shared by every "find the tab owning this tty"
    operation (today: focus and close) — Phase 0 copy-pasted this loop."""
    return f"""
on run argv
  set targetTty to item 1 of argv
  tell application "Terminal"
    set targetWindow to missing value
    set targetTab to missing value
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          set targetWindow to w
          set targetTab to t
        end if
      end repeat
    end repeat
    if targetWindow is missing value then
      return "missing"
    end if
    {body}
  end tell
  return "ok"
end run
"""


class AppleScriptTerminal:
    def open(self, shell_command: str) -> str | None:
        """Open a new Terminal tab running shell_command, return its tty path.

        The tty is a stable per-tab identifier (Terminal tabs have no usable
        `id` property) that later lets the bridge find this exact tab again
        to push a live effort change into it, instead of guessing at "front
        window".
        """
        script = """
on run argv
  set bridgeCommand to item 1 of argv
  tell application "Terminal"
    set newTab to do script bridgeCommand
    activate
    set ttyName to tty of newTab
  end tell
  return ttyName
end run
"""
        result = subprocess.run(
            ["osascript", "-e", script, shell_command], check=True, capture_output=True, text=True
        )
        return result.stdout.strip() or None

    def focus(self, tty: str) -> bool:
        """Bring the window owning tty to front and select that exact tab.

        Used before driving a slot's session (voice hold, agent.focus) so
        the right tab actually has keyboard focus, not just "some Terminal
        window."
        """
        script = _find_tab_script(
            "set selected tab of targetWindow to targetTab\n"
            "    set index of targetWindow to 1\n"
            "    activate"
        )
        result = subprocess.run(["osascript", "-e", script, tty], check=False, capture_output=True, text=True)
        return result.stdout.strip() == "ok"

    def terminal_pid(self) -> int | None:
        """PID of the Terminal.app process, for posting synthetic key events to it."""
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to return unix id of '
                '(first process whose name is "Terminal")',
            ],
            check=False, capture_output=True, text=True,
        )
        text = result.stdout.strip()
        return int(text) if text.isdigit() else None

    def post_key(self, pid: int, keycode: int, down: bool) -> None:
        """Post a synthetic keyDown/keyUp to a specific process (not just
        'frontmost'). This is what makes real press-and-hold possible —
        AppleScript's `keystroke` only ever sends an atomic press+release,
        which can't represent "held." Requires the bridge process to have
        Accessibility permission (System Settings -> Privacy & Security ->
        Accessibility).
        """
        if not QUARTZ_AVAILABLE:
            raise RuntimeError(
                "Quartz is not installed. Run the bridge with host/.venv/bin/python3, "
                "or `pip install pyobjc-framework-Quartz` for whatever Python runs it."
            )
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventPostToPid(pid, event)

    def close(self, tty: str) -> bool:
        """Close the tab owning tty (Phase 2.4's --close-dead-tabs)."""
        script = _find_tab_script("close targetTab")
        result = subprocess.run(["osascript", "-e", script, tty], check=False, capture_output=True, text=True)
        return result.stdout.strip() == "ok"


class NullTerminal:
    """Used for --no-open, --dry-run, and when Quartz/osascript is
    unavailable: every method logs and returns None/False."""

    def __init__(self, log: Callable[[str], None] = print) -> None:
        self._log = log

    def open(self, shell_command: str) -> str | None:
        self._log("NullTerminal: open() is a no-op")
        return None

    def focus(self, tty: str) -> bool:
        self._log("NullTerminal: focus() is a no-op")
        return False

    def terminal_pid(self) -> int | None:
        self._log("NullTerminal: terminal_pid() is a no-op")
        return None

    def post_key(self, pid: int, keycode: int, down: bool) -> None:
        self._log("NullTerminal: post_key() is a no-op")

    def close(self, tty: str) -> bool:
        self._log("NullTerminal: close() is a no-op")
        return False


class FakeTerminal:
    """Records calls; open() returns f"/dev/ttysFAKE{n}"."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.focused: list[str] = []
        self.posted: list[tuple[int | None, int, bool]] = []
        self.closed: list[str] = []
        self._n = 0

    def open(self, shell_command: str) -> str | None:
        self._n += 1
        tty = f"/dev/ttysFAKE{self._n}"
        self.opened.append(shell_command)
        return tty

    def focus(self, tty: str) -> bool:
        self.focused.append(tty)
        return True

    def terminal_pid(self) -> int | None:
        return 1234

    def post_key(self, pid: int, keycode: int, down: bool) -> None:
        self.posted.append((pid, keycode, down))

    def close(self, tty: str) -> bool:
        self.closed.append(tty)
        return True
