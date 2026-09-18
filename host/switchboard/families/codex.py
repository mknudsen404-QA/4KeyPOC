"""Codex's family profile. Verified live against codex-cli
0.154.0-alpha.6.2 on this machine (docs/design/family-capability-matrix.md,
Phase 5 update): 5 lifecycle hooks fire in practice — UserPromptSubmit,
PostToolUse, Stop, SessionEnd confirmed via a real `codex exec` session
with `--dangerously-bypass-hook-trust` (needed only because that spike's
hook commands weren't yet in `[hooks.state]`'s persisted trust table;
this bridge's own installed hooks already are, per a real
`~/.codex/config.toml` on this machine, so they fire normally in an
actual interactive session); PermissionRequest's trust entry is present
in that same table but wasn't independently re-fired this pass (`exec`
mode has no interactive approval to trigger it — see the plan's Phase
5.1). No session-start signal was observed even with every schema-known
event (PreToolUse, PreCompact, PostCompact, SessionStart, SubagentStart,
SubagentStop, Interrupt) wired to a logger and bypassed — a slot stays
"launched" until its first UserPromptSubmit, which is already what
happens (see status_table.py's CODEX_HOOK_STATUS: no SessionStart entry).

All five effort values (`low`/`medium`/`high`/`xhigh`/`max`) are accepted
by `-c model_reasoning_effort=<value>` without error — confirmed live
(each one echoed back in the CLI's own startup banner, e.g. "reasoning
effort: xhigh"). The earlier clamp of xhigh/max to high is removed.
Whether an override set with `-c` persists for the whole session or only
the first turn wasn't independently tested (would need a scripted
multi-turn interactive session); it's a process-wide config override
applied once at startup, the same mechanism as `--model`, so there's no
structural reason to expect it to reset mid-session.
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

# All five accepted live — see the module docstring.
CODEX_EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}


class CodexProfile:
    name = NAME
    display_name = DISPLAY_NAME
    executables = EXECUTABLES
    known_paths = KNOWN_PATHS

    def detect(self) -> Detection:
        return detect_on_path(self.executables, self.known_paths)

    def effort_args(self, effort: str) -> list[str]:
        value = effort if effort in CODEX_EFFORT_VALUES else "medium"
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

    def slash_commands(self) -> tuple[str, ...]:
        # No documented slash-command list found for Codex's TUI (Phase 0
        # matrix); Phase 5.4 is explicitly out of scope for wiring any of
        # this up, so this stays empty rather than guessing.
        return ()

    def capabilities(self) -> Capabilities:
        # tier is live, not hardcoded: Codex is "status" (hooks + effort,
        # no voice) on a machine with no dictation app, and genuinely
        # "full" the moment one is installed and hotkey.available()
        # flips to True — see voice/hotkey.py.
        from switchboard.voice import registry as voice_registry

        voice_available = voice_registry.get(self.default_voice().provider).available().available
        tier = "full" if voice_available else "status"
        return Capabilities(hooks=True, effort=True, voice=voice_available, tier=tier)
