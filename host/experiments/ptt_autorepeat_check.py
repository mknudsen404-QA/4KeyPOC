"""Standalone experiment for the PTT bug (see docs/design/auto-install-plan.md,
"Push-to-talk: the current bug, the fix, and first-run onboarding").

Confirms whether Claude Code's hold-to-talk needs OS key-autorepeat to
recognize a hold, by posting synthetic keyDown/keyUp events straight to
Terminal.app (bypassing the bridge entirely) in three shapes:

  a) one keyDown, hold 2s, one keyUp            -- what the bridge does today
  b) keyDown repeated every 33ms for 2s (with the Quartz autorepeat flag
     set on every repeat after the first), then keyUp
  c) same cadence as (b) but WITHOUT the autorepeat flag

If (b) records and (a) does not, the diagnosis in the plan is confirmed.
If (c) also records, the flag doesn't matter and cadence alone is enough
(simplifies the KeyInjector fix). If none of them record, the bug is
something else and the KeyInjector plan needs to be revisited before
writing it.

Usage:
    host/.venv/bin/python3 host/experiments/ptt_autorepeat_check.py a
    host/.venv/bin/python3 host/experiments/ptt_autorepeat_check.py b
    host/.venv/bin/python3 host/experiments/ptt_autorepeat_check.py c

Before running:
  1. Have a Claude Code session open in Terminal.app, in the SAME window/tab
     that will be frontmost when you run this (this script does not focus
     any window for you -- switch to the Claude Code Terminal tab yourself
     right before running, you have ~3s from the countdown).
  2. Make sure `/voice` is on in that session (type /voice once if needed).
  3. Run this script from a DIFFERENT terminal/window (e.g. iTerm, or a
     second Terminal window) so the keyDown/keyUp events go to Terminal.app
     generally -- CGEventPostToPid targets the Terminal.app process, not a
     specific window, same as the bridge does today.
  4. The first run will trigger an Accessibility permission prompt for
     whatever Python binary you invoke this with. Grant it (System
     Settings -> Privacy & Security -> Accessibility), then re-run.

After each run, check the Claude Code session: did it show the recording
indicator / transcribe anything? Report back which of a/b/c did.
"""

from __future__ import annotations

import subprocess
import sys
import time

import Quartz

SPACE_KEYCODE = 49
REPEAT_INTERVAL_S = 0.033  # ~30ms, matches macOS default key repeat rate
HOLD_DURATION_S = 2.0


def terminal_pid() -> int | None:
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


def post(pid: int, down: bool, *, autorepeat: bool = False) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, SPACE_KEYCODE, down)
    if autorepeat:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat, 1)
    Quartz.CGEventPostToPid(pid, event)


def case_a(pid: int) -> None:
    print("case (a): single keyDown, hold 2s, single keyUp")
    post(pid, True)
    time.sleep(HOLD_DURATION_S)
    post(pid, False)


def case_b(pid: int) -> None:
    print("case (b): keyDown repeated every 33ms for 2s WITH autorepeat flag, then keyUp")
    post(pid, True, autorepeat=False)  # first press is a real press, never autorepeat
    elapsed = 0.0
    while elapsed < HOLD_DURATION_S:
        time.sleep(REPEAT_INTERVAL_S)
        elapsed += REPEAT_INTERVAL_S
        post(pid, True, autorepeat=True)
    post(pid, False)


def case_c(pid: int) -> None:
    print("case (c): keyDown repeated every 33ms for 2s WITHOUT autorepeat flag, then keyUp")
    post(pid, True, autorepeat=False)
    elapsed = 0.0
    while elapsed < HOLD_DURATION_S:
        time.sleep(REPEAT_INTERVAL_S)
        elapsed += REPEAT_INTERVAL_S
        post(pid, True, autorepeat=False)
    post(pid, False)


CASES = {"a": case_a, "b": case_b, "c": case_c}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in CASES:
        print(__doc__)
        print(f"usage: {sys.argv[0]} [a|b|c]")
        return 1
    pid = terminal_pid()
    if pid is None:
        print("Could not find Terminal.app pid. Is Terminal running?")
        return 1
    print(f"Terminal.app pid={pid}. Switch to the Claude Code tab now.")
    for n in (3, 2, 1):
        print(n)
        time.sleep(1)
    CASES[sys.argv[1]](pid)
    print("done. Check the Claude Code session for a recording indicator / transcript.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
