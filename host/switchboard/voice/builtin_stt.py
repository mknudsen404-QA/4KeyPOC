"""Deferred (plan Phase 4.1): the bridge recording the mic itself and
transcribing locally (whisper.cpp / mlx-whisper) or via an API, then
typing the text in. A real implementation is a big dependency and
privacy surface — only worth it if `hotkey` proves too fiddly for
coworkers. This stub exists so the schema/registry have a stable name to
point at when that day comes, without anything claiming it works today.
"""

from __future__ import annotations

from switchboard.voice.base import Availability, VoiceContext

NAME = "builtin_stt"


class BuiltinSttProvider:
    name = NAME

    def available(self) -> Availability:
        return Availability(False, "not implemented — deferred, see the plan's Phase 4.1")

    def hold(self, ctx: VoiceContext) -> None:
        ctx.log("Voice hold: builtin_stt is not implemented yet")

    def release(self, ctx: VoiceContext) -> None:
        pass
