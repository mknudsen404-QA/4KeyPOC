"""VoiceRegistry: looks up a VoiceProvider by name, the same shape as
switchboard.families.FamilyRegistry. Everything that used to hardcode a
single family as the only one allowed to use voice (reducer.py, bridge.py)
now asks this instead, keyed off the slot's resolved `voice` field. See
docs/design/slot-settings-and-family-parity-plan.md Phase 4.
"""

from __future__ import annotations

from switchboard.voice.base import VoiceProvider
from switchboard.voice.builtin_stt import BuiltinSttProvider
from switchboard.voice.claude_native import ClaudeNativeProvider
from switchboard.voice.hotkey import HotkeyProvider
from switchboard.voice.none import NoneProvider

DEFAULT_PROVIDER = NoneProvider.name


class VoiceRegistry:
    def __init__(self, providers: tuple[VoiceProvider, ...]) -> None:
        self._by_name: dict[str, VoiceProvider] = {p.name: p for p in providers}

    def providers(self) -> tuple[VoiceProvider, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str | None) -> VoiceProvider:
        """Falls back to a NoneProvider for an unknown/missing name (or a
        registry instance that simply doesn't carry one) — never raises,
        so a typo or unimplemented provider name degrades to "does
        nothing" rather than crashing a hold/release call."""
        fallback = self._by_name.get(DEFAULT_PROVIDER) or NoneProvider()
        if not name:
            return fallback
        return self._by_name.get(name, fallback)


registry = VoiceRegistry((ClaudeNativeProvider(), HotkeyProvider(), NoneProvider(), BuiltinSttProvider()))

__all__ = ["registry", "DEFAULT_PROVIDER", "VoiceRegistry", "VoiceProvider"]
