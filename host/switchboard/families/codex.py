"""Codex's family profile. Verified live against codex-cli
0.154.0-alpha.6.2 on this machine (docs/design/family-capability-matrix.md):
5 lifecycle hooks (no matcher support observed), `-c
model_reasoning_effort=<value>`, no native voice. Only "low" is confirmed
against a real config.toml; xhigh/max are clamped to "high" until a live
session confirms the top tiers are accepted (see CODEX_EFFORT_MAP).
"""

from __future__ import annotations

import os
from pathlib import Path

from switchboard.families.base import Capabilities, Detection, HookSpec, VoiceSpec, detect_on_path
from switchboard.status_table import CODEX_HOOK_EVENTS, CODEX_HOOK_STATUS, CODEX_SESSION_END_STATUS

NAME = "codex"
DISPLAY_NAME = "Codex"
EXECUTABLES = ("codex",)
KNOWN_PATHS = ("/Applications/ChatGPT.app/Contents/Resources/codex",)

CODEX_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class CodexProfile:
    name = NAME
    display_name = DISPLAY_NAME
    executables = EXECUTABLES
    known_paths = KNOWN_PATHS

    def detect(self) -> Detection:
        return detect_on_path(self.executables, self.known_paths)

    def effort_args(self, effort: str) -> list[str]:
        value = CODEX_EFFORT_MAP.get(effort, "medium")
        return ["-c", f"model_reasoning_effort={value}"]

    def hook_spec(self) -> HookSpec:
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        return HookSpec(
            config_path=codex_home / "hooks.json",
            events=CODEX_HOOK_EVENTS,
            matchers={event: None for event in CODEX_HOOK_EVENTS},
            event_status={k: v for k, v in CODEX_HOOK_STATUS.items() if k != "SessionEnd"},
            session_end_status=CODEX_SESSION_END_STATUS,
            merge_strategy="rewrite_group",
            command_suffix="; printf '{}'",
        )

    def hook_status_for(self, event: str) -> str | None:
        return CODEX_HOOK_STATUS.get(event)

    def default_voice(self) -> VoiceSpec:
        # No native voice of its own — hotkey (a system dictation app) is
        # the plan's recommended answer for Codex. Not verified live (no
        # chord configured by default) — see voice/hotkey.py's docstring.
        return VoiceSpec(provider="hotkey")

    def capabilities(self) -> Capabilities:
        return Capabilities(hooks=True, effort=True, voice=False, tier="status")
