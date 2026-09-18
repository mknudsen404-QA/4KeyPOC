"""The explicit "no voice provider for this slot" choice. hold()/release()
are no-ops — the reducer never even emits a VoiceKey effect for a slot
resolved to this provider (see reducer._handle_voice_start), so in
practice these methods only run if something calls them directly (tests,
or a future caller that skips the reducer's guard) — they stay safe
either way rather than raising.
"""

from __future__ import annotations

from switchboard.voice.base import Availability, VoiceContext

NAME = "none"


class NoneProvider:
    name = NAME

    def available(self) -> Availability:
        return Availability(True, "always available — deliberately does nothing")

    def hold(self, ctx: VoiceContext) -> None:
        ctx.log("Voice hold: no provider configured for this slot")

    def release(self, ctx: VoiceContext) -> None:
        pass
