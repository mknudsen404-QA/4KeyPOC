"""Antigravity's family profile. Verified live against antigravity-cli
1.2.16 on this machine (docs/design/family-capability-matrix.md, Phase 0
Antigravity spike): a Gemini-backed agentic CLI (binary `agy`, installed
via `brew install --cask antigravity-cli`), sharing its config root with
Gemini CLI (`~/.gemini/antigravity-cli/`).

**Effort is NOT a uniform 5-tier flag here, unlike Claude/Codex.**
`--effort {low,medium,high,xhigh,max}` exists, but which values are legal
depends entirely on the selected `--model`, and an unsupported
combination is a hard launch error, not a clamp:

    $ agy --effort xhigh --print "hi"
    error: invalid model selection (--model "" --effort "xhigh"):
    gemini-3.8-flash has no "xhigh" effort (available: low, medium, high)

    $ agy --model gemini-3.1-pro --effort medium --print "hi"
    error: invalid model selection (--model "gemini-3.1-pro" --effort
    "medium"): gemini-3.1-pro has no "medium" effort (available: low, high)

With no `--model` given, Antigravity defaults to `gemini-3.8-flash`
(confirmed via the error text above) — already a Gemini model, so a bare
`agy` launch satisfies "wire it up to Gemini" without any extra flag.
`effort_args()` below clamps to that default model's actual ceiling
(low/medium/high; xhigh/max both clamp to high) so a slot's configured
effort can never turn into a launch error. A slot whose `args` sets an
explicit `--model` with a different supported range would need its own
clamp table — not attempted here; if that's needed, treat it the way the
plan's capability matrix treats a real but unverified gap, not a guess.

**Hooks wired 2026-10-04 — a genuinely different mechanism from
Claude/Codex's fire-and-forget curl hooks, not a copy of that pattern.**
`~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/hooks.md`
(verified live against the installed CLI) documents:

- Hooks live in a project-local `.agents/hooks.json` (the doc's own
  example), not a single global user config file — so there's no
  sensible no-arg "install once" entry point the way `install_claude_hooks`/
  `install_codex_hooks` have. `hooks_install.py`'s `install_antigravity_hooks`
  always takes a `cwd` and is called once per launch, from
  `launcher.build_launch()`, right before a slot's shell command is
  built — not from the `install-hooks` CLI subcommand.
- Only 5 events exist (`PreToolUse`, `PostToolUse`, `PreInvocation`,
  `PostInvocation`, `Stop`) — no `SessionStart`, `SessionEnd`, or
  `UserPromptSubmit` analog. Only 4 are actually installed:
  `PostInvocation`'s own meaning ("after tool calls finish", distinct
  from `PostToolUse`) never maps to a state transition this project
  tracks, so wiring it up would just be status-free noise on every
  model turn — left out rather than installed-but-ignored.
- Hooks run **synchronously and block the agent loop**, and each event
  has its own required JSON stdout contract (`PreToolUse` must return a
  `decision` of `allow`/`deny`/`ask`/`force_ask`; `Stop`'s `decision`
  must NOT be `"continue"`, or it blocks the agent from actually
  stopping). Getting this wrong doesn't just fail to report status, it
  can stall or mis-gate the agent's own execution loop — so unlike
  Claude/Codex's hook command (stdout thrown to `/dev/null`, see
  `hooks_install.py`'s `_hook_command`), this family's installed command
  always echoes the exact passthrough JSON each event's contract
  requires (`_ANTIGRAVITY_REQUIRED_STDOUT`) after posting to the bridge.
- `PreToolUse` isn't matcher-restricted server-side the way Claude's
  hook registration is (Claude only *subscribes* to `AskUserQuestion`;
  Antigravity's `PreToolUse` fires for every tool and the matcher is
  applied client-side in `hooks.json`). The installed hook only targets
  `ask_question` (confirmed live as the tool name used for interactive
  questions — see `builtin/skills/automation/SKILL.md`'s own usage),
  same restriction Claude applies to its own `AskUserQuestion`, for the
  same reason: every other tool's `PreToolUse` would be noise, not a
  status change.

`status_table.py`'s `STATUSES` carries `antigravity_hooks` per row the
same way it already carries `claude_hooks`/`codex_hooks` — this family
reuses the exact same status vocabulary (`working`, `needs_input`,
`done`), not a parallel one.

**What's still a real, scoped gap, not an oversight:**
- No session-exit signal at all (see "Session-end signal" below) — a
  slot never auto-frees via hook; liveness probing is the only thing
  that notices the process is gone.
- Not yet run live end-to-end against an actual `agy` session (the
  Claude/Codex hook paths were verified live before being marked
  `hooks=True`; this one hasn't been, see `capabilities()`'s comment).

Voice: no native voice/dictation surface found in `agy --help` (the
`mic-serve` subcommand serves *this* machine's microphone to a CLI on
*another* host — a remote-session feature, not local push-to-talk).
Falls back to the same system-dictation `hotkey` provider Codex uses,
which is family-agnostic by design — confirmed live end-to-end for Codex
on 2026-10-03 (F13 bound to macOS Dictation), and there is no reason it
would behave differently for a different CLI's focused Terminal tab, but
that specific combination (Antigravity slot + hotkey PTT) has not itself
been run live yet.

Session-end signal: none found — no event in the 5-event table above
means "the CLI process exited." A slot stays whatever it last reported
(same degrade path as Codex's missing SessionStart: see
`families/codex.py`'s docstring) until liveness probing notices the
process is gone.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.families.base import Capabilities, Detection, HookSpec, VoiceSpec, detect_on_path
from switchboard.status_table import ANTIGRAVITY_ASK_QUESTION_MATCHER, ANTIGRAVITY_HOOK_STATUS

NAME = "antigravity"
DISPLAY_NAME = "Antigravity"
EXECUTABLES = ("agy",)
KNOWN_PATHS: tuple[str, ...] = ()

# The 4 events actually installed — PostInvocation is deliberately left
# out, see module docstring. matchers=None means "fires for everything";
# PreToolUse is the one exception (see module docstring + status_table's
# ANTIGRAVITY_ASK_QUESTION_MATCHER).
ANTIGRAVITY_HOOK_EVENTS: tuple[str, ...] = ("PreToolUse", "PostToolUse", "PreInvocation", "Stop")

# The default model (`gemini-3.8-flash`, used when a slot's args don't
# override --model) only accepts these three — confirmed live. xhigh/max
# clamp down to high rather than erroring the launch; see module docstring.
ANTIGRAVITY_EFFORT_VALUES = {"low", "medium", "high"}
_CLAMP_TO_HIGH = {"xhigh", "max"}


class AntigravityProfile:
    name = NAME
    display_name = DISPLAY_NAME
    executables = EXECUTABLES
    known_paths = KNOWN_PATHS

    def detect(self) -> Detection:
        return detect_on_path(self.executables, self.known_paths)

    def effort_args(self, effort: str) -> list[str]:
        if effort in _CLAMP_TO_HIGH:
            effort = "high"
        value = effort if effort in ANTIGRAVITY_EFFORT_VALUES else "medium"
        return ["--effort", value]

    def hook_spec(self) -> HookSpec:
        # config_path is a placeholder: this family has no meaningful
        # global config location (see module docstring), so the real
        # path is always supplied by the caller as path_override —
        # launcher.build_launch() passes the launching slot's own cwd,
        # via hooks_install.install_antigravity_hooks(cwd). merge_strategy
        # "merge_named_hook" is what actually knows how to use that.
        return HookSpec(
            config_path=Path(".agents/hooks.json"),
            events=ANTIGRAVITY_HOOK_EVENTS,
            matchers={"PreToolUse": ANTIGRAVITY_ASK_QUESTION_MATCHER},
            event_status=dict(ANTIGRAVITY_HOOK_STATUS),
            session_end_status=None,
            merge_strategy="merge_named_hook",
        )

    def hook_status_for(self, event: str) -> str | None:
        return ANTIGRAVITY_HOOK_STATUS.get(event)

    def default_voice(self) -> VoiceSpec:
        # No native voice surface found — same family-agnostic hotkey
        # fallback as Codex, reusing the F13/Dictation chord confirmed
        # live for it. See module docstring for what's and isn't verified.
        return VoiceSpec(provider="hotkey", chord="f13", mode="toggle")

    def slash_commands(self) -> tuple[str, ...]:
        # No documented slash-command list found for Antigravity's CLI.
        return ()

    def capabilities(self) -> Capabilities:
        # tier is live, not hardcoded, same pattern as Codex: effort
        # always works (clamped above so it can't error), voice depends
        # on whether this machine actually has a usable PTT provider.
        # hooks=True is the mechanism being implemented (same meaning as
        # Claude/Codex's hardcoded hooks=True), not a live-session claim —
        # see module docstring's "still a real, scoped gap" section for
        # what hasn't been run live yet.
        from switchboard.voice import registry as voice_registry

        voice_available = voice_registry.get(self.default_voice().provider).available().available
        tier = "full" if voice_available else "status"
        return Capabilities(hooks=True, effort=True, voice=voice_available, tier=tier)
