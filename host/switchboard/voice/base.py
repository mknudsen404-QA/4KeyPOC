"""Shared value types and the VoiceProvider protocol every push-to-talk
provider implements (see docs/design/slot-settings-and-family-parity-plan.md
Phase 4). A provider owns exactly the parts of PTT that vary — what
keystroke(s) it drives and whether it's usable on this OS/install — never
the surrounding mechanics (which tty has focus, waiting for the focus
switch to settle): those stay in bridge.py, the same for every provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from switchboard.key_injector import KeyInjector


@dataclass(frozen=True)
class Availability:
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class VoiceContext:
    """Everything hold()/release() need, without depending on Bridge or
    Terminal directly. `pid` is the terminal process to post keys to
    (already focused and settled by the time this is built — bridge.py's
    job, not the provider's)."""

    pid: int
    key_injector: KeyInjector
    chord: str | None = None
    mode: str = "hold"
    log: Callable[[str], None] = print


class VoiceProvider(Protocol):
    name: str

    def available(self) -> Availability: ...
    def hold(self, ctx: VoiceContext) -> None: ...
    def release(self, ctx: VoiceContext) -> None: ...
