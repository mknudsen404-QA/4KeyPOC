"""Family-agnostic PTT: focus the terminal, then drive a system-wide
dictation app's own shortcut so its transcript gets typed into whatever's
focused — works for every family and every tier, including generic,
since it never depends on the CLI at all. This is the plan's recommended
answer for Codex (no native voice of its own).

**Not verified live.** No known dictation app (Aqua Voice, Wispr Flow,
Superwhisper) is installed on this machine, and macOS's built-in
Dictation has never been enabled here — checked directly:

    $ defaults read com.apple.HIToolbox AppleDictationAutoEnable
    The domain/default pair of (com.apple.HIToolbox, AppleDictationAutoEnable) does not exist

Per the plan's ground rule ("unverified = capabilities() says no"),
available() returns False on this machine regardless of the chord table
below. It will flip to True once run on a machine with a real dictation
app — see AVAILABLE_APPS / _macos_dictation_enabled().

Second, narrower gap: KeyInjector (key_injector.py) only knows a single
bare keycode per hold/release — it has no modifier-key support. So only
single, unmodified keys can actually be driven yet; a chord like
"ctrl+space" or "cmd+shift+d" (both examples in the plan's own vocabulary)
is accepted by the schema but not yet drivable, and hold() says so
clearly rather than silently doing the wrong thing or pretending it
worked. Extending KeyInjector with modifier flags is the real fix,
deferred until a chord that needs it is actually being validated against
a real dictation app.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from switchboard.voice.base import Availability, VoiceContext

NAME = "hotkey"

# The only chords actually drivable today — KeyInjector has no modifier
# support, so anything beyond a single bare key (space, fn) is schema-legal
# but not yet implemented; see the module docstring.
CHORD_KEYCODES: dict[str, int] = {
    "space": 49,
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
    result = subprocess.run(
        ["defaults", "read", "-g", "AppleDictationAutoEnable"],
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() == "1"


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
        ctx.key_injector.hold(ctx.pid, keycode)
        if ctx.mode == "toggle":
            ctx.key_injector.release(ctx.pid, keycode)

    def release(self, ctx: VoiceContext) -> None:
        keycode = self._resolve_keycode(ctx, log_on_missing=False)
        if keycode is None:
            return
        if ctx.mode == "toggle":
            ctx.key_injector.hold(ctx.pid, keycode)
        ctx.key_injector.release(ctx.pid, keycode)

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
