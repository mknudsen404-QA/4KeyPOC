"""Claude Code's native `/voice` hold-to-record: hold Space in the focused
terminal via the repeating KeyInjector (see key_injector.py's module
docstring for why a single keyDown/keyUp doesn't register as a hold).
Confirmed working live (2026-09-14, this plan's header) — the one
provider every other provider was generalized away from.
"""

from __future__ import annotations

from switchboard.voice.base import Availability, VoiceContext

NAME = "claude_native"

# macOS virtual keycode for the spacebar.
SPACE_KEYCODE = 49


class ClaudeNativeProvider:
    name = NAME

    def available(self) -> Availability:
        return Availability(True, "hold Space in the focused terminal (Claude Code's own /voice)")

    def hold(self, ctx: VoiceContext) -> None:
        ctx.key_injector.hold(ctx.pid, SPACE_KEYCODE)

    def release(self, ctx: VoiceContext) -> None:
        ctx.key_injector.release(ctx.pid, SPACE_KEYCODE)
