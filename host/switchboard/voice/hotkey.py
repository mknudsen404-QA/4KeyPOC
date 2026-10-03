"""Family-agnostic PTT: focus the terminal, then drive a system-wide
dictation app's own shortcut so its transcript gets typed into whatever's
focused — works for every family and every tier, including generic,
since it never depends on the CLI at all. This is the plan's recommended
answer for Codex (no native voice of its own).

**Confirmed working live end-to-end on macOS 26.5 (2026-10-03).**

Availability: the original check (`defaults read -g
AppleDictationAutoEnable`) was stale — that key doesn't exist on current
macOS even with Dictation on. The real signal is
`com.apple.assistant.support`'s "Offline Dictation Status" dict: each
locale the user has enabled Dictation for gets an entry with `Installed =
1` once its on-device model is downloaded. See
_macos_dictation_enabled().

Chord: Dictation's own Shortcut setting (System Settings -> Keyboard ->
Dictation -> Shortcut) defaults to a double-press gesture ("press Control
twice"), which this provider's single press-and-release can't drive. Its
"Customize..." option accepts an arbitrary single key though, including
one with no physical key on the keyboard (F13 was recorded by posting a
synthetic keydown/keyup into the shortcut recorder — see
`key_injector.post_key_global`). F13 was chosen specifically because it
has no typing meaning anywhere else, so binding Dictation to it can't
collide with anything. `families/codex.py`'s `default_voice()` sets
`chord="f13", mode="toggle"` accordingly (`mode="toggle"` because
Dictation's shortcut starts/stops it, it isn't a true hold).

Delivery mechanism: a global OS shortcut like this one is only triggered
by posting into the system-wide HID event stream (`CGEventPost`), not by
posting to one process's queue (`CGEventPostToPid`, which is what
claude_native correctly uses to type into Claude's own terminal). This
provider uses `ctx.global_key_injector`, not `ctx.key_injector` — see
`voice/base.py`'s `VoiceContext` docstring and
`key_injector.post_key_global`.

Narrower remaining gap: KeyInjector (key_injector.py) only knows a single
bare keycode per hold/release — it has no modifier-key support. So only
single, unmodified keys can actually be driven; a chord like "ctrl+space"
or "cmd+shift+d" (both examples in the plan's own vocabulary) is
schema-legal but not yet drivable, and hold() says so clearly rather than
silently doing the wrong thing or pretending it worked. Extending
KeyInjector with modifier flags is the real fix, deferred until a chord
that needs it is actually being validated.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from switchboard.voice.base import Availability, VoiceContext

NAME = "hotkey"

# The only chords actually drivable today — KeyInjector has no modifier
# support, so anything beyond a single bare key (space, fn) is schema-legal
# but not yet implemented; see the module docstring.
CHORD_KEYCODES: dict[str, int] = {
    "space": 49,
    "f13": 0x69,  # kVK_F13 — confirmed live 2026-10-03 as Codex's default chord
}

# Dictation apps this provider knows the *name* of, for an availability
# check — none installed or verified on this machine, so this list is
# unverified against a real one; update once one is.
KNOWN_DICTATION_APPS = (
    "/Applications/Aqua Voice.app",
    "/Applications/Wispr Flow.app",
    "/Applications/Superwhisper.app",
)


def _macos_dictation_enabled() -> bool:
    """True if built-in Dictation has an installed (downloaded) offline
    model for any locale — the current, live signal on macOS 26.5. See
    the module docstring for why the older `AppleDictationAutoEnable`
    global-domain key no longer reflects this."""
    export = subprocess.run(
        ["defaults", "export", "com.apple.assistant.support", "-"],
        check=False, capture_output=True,
    )
    if export.returncode != 0 or not export.stdout:
        return False
    convert = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", "-"],
        input=export.stdout, check=False, capture_output=True,
    )
    if convert.returncode != 0:
        return False
    try:
        data = json.loads(convert.stdout)
    except json.JSONDecodeError:
        return False
    locales = data.get("Offline Dictation Status", {})
    return any(isinstance(entry, dict) and entry.get("Installed") for entry in locales.values())


class HotkeyProvider:
    name = NAME

    def available(self) -> Availability:
        for app in KNOWN_DICTATION_APPS:
            if Path(app).exists():
                return Availability(True, f"found {app}")
        if _macos_dictation_enabled():
            return Availability(True, "macOS built-in Dictation is enabled")
        return Availability(False, "no known dictation app found and macOS Dictation is not enabled")

    def hold(self, ctx: VoiceContext) -> None:
        """`mode: "hold"` holds the chord for the whole PTT duration
        (release() lets go). `mode: "toggle"` (macOS built-in Dictation,
        which has no hold-to-talk of its own) taps the chord once here to
        turn dictation on, and taps it again in release() to turn it off."""
        keycode = self._resolve_keycode(ctx)
        if keycode is None:
            return
        ctx.global_key_injector.hold(ctx.pid, keycode)
        if ctx.mode == "toggle":
            ctx.global_key_injector.release(ctx.pid, keycode)

    def release(self, ctx: VoiceContext) -> None:
        keycode = self._resolve_keycode(ctx, log_on_missing=False)
        if keycode is None:
            return
        if ctx.mode == "toggle":
            ctx.global_key_injector.hold(ctx.pid, keycode)
        ctx.global_key_injector.release(ctx.pid, keycode)

    def _resolve_keycode(self, ctx: VoiceContext, *, log_on_missing: bool = True) -> int | None:
        chord = ctx.chord
        if not chord:
            if log_on_missing:
                ctx.log("Voice hold: hotkey provider has no chord configured for this slot")
            return None
        keycode = CHORD_KEYCODES.get(chord)
        if keycode is None:
            if log_on_missing:
                ctx.log(
                    f"Voice hold: chord {chord!r} is schema-legal but not yet drivable "
                    f"(KeyInjector has no modifier support) — supported today: {sorted(CHORD_KEYCODES)}"
                )
            return None
        return keycode
