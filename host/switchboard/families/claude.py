"""Claude Code's family profile. Verified live against claude 2.1.271 on
this machine (docs/design/family-capability-matrix.md): 9 lifecycle
hooks, `--effort {low,medium,high,xhigh,max}`, native `/voice` hold-Space
PTT. The hook event/status/matcher vocabulary lives in status_table.py
(the firmware header's source of truth) and is read from there, not
duplicated here.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.families.base import Capabilities, Detection, HookSpec, VoiceSpec, detect_on_path
from switchboard.status_table import CLAUDE_HOOK_STATUS, CLAUDE_SESSION_END_STATUS, HOOK_MATCHERS

NAME = "claude"
DISPLAY_NAME = "Claude Code"
EXECUTABLES = ("claude",)
KNOWN_PATHS = (
    str(Path.home() / ".local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# All five values are accepted live by `--effort`.
EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}


class ClaudeProfile:
    name = NAME
    display_name = DISPLAY_NAME
    executables = EXECUTABLES
    known_paths = KNOWN_PATHS

    def detect(self) -> Detection:
        return detect_on_path(self.executables, self.known_paths)

    def effort_args(self, effort: str) -> list[str]:
        value = effort if effort in EFFORT_VALUES else "medium"
        return ["--effort", value]

    def hook_spec(self) -> HookSpec:
        return HookSpec(
            config_path=Path.home() / ".claude" / "settings.json",
            events=tuple(HOOK_MATCHERS.keys()),
            matchers=dict(HOOK_MATCHERS),
            event_status={k: v for k, v in CLAUDE_HOOK_STATUS.items() if k != "SessionEnd"},
            session_end_status=CLAUDE_SESSION_END_STATUS,
            merge_strategy="merge_by_marker",
        )

    def hook_status_for(self, event: str) -> str | None:
        return CLAUDE_HOOK_STATUS.get(event)

    def default_voice(self) -> VoiceSpec:
        return VoiceSpec(provider="claude_native")

    def capabilities(self) -> Capabilities:
        return Capabilities(hooks=True, effort=True, voice=True, tier="full")
