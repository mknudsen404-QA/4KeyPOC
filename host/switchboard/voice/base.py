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
    job, not the provider's).

    Two injectors, because "type a key into the focused app" and
    "trigger a system-wide OS shortcut" are genuinely different
    operations (see key_injector.post_key_global's docstring):
    `key_injector` posts to `pid` specifically (claude_native's hold-Space
    inside Claude's own terminal); `global_key_injector` posts into the
    system-wide HID event stream, ignoring `pid` (hotkey's chord, which
    has to reach macOS's global shortcut dispatch, e.g. a Dictation
    shortcut, not just one app's input queue)."""

    pid: int
    key_injector: KeyInjector
    global_key_injector: KeyInjector
    chord: str | None = None
    mode: str = "hold"
    log: Callable[[str], None] = print


class VoiceProvider(Protocol):
    name: str

    def available(self) -> Availability: ...
    def hold(self, ctx: VoiceContext) -> None: ...
    def release(self, ctx: VoiceContext) -> None: ...
