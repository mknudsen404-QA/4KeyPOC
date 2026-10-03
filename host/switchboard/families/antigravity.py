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

**No hooks wired yet — a real, scoped gap, not an oversight.**
`~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/hooks.md`
documents a genuinely different mechanism from Claude/Codex/Gemini's
fire-and-forget curl hooks:

- Hooks live in a project-local `.agents/hooks.json` (the doc's own
  example), not a single global user config file `hooks_install.py`
  could merge into once — would need writing/merging a file per slot's
  working directory instead.
- Only 5 events exist (`PreToolUse`, `PostToolUse`, `PreInvocation`,
  `PostInvocation`, `Stop`) — no `SessionStart`, `SessionEnd`, or
  `UserPromptSubmit` analog. `Stop` (loop terminates) is the closest
  thing to "turn done," but there's nothing that means "process exited."
- Hooks run **synchronously and block the agent loop**, and each event
  has its own required JSON stdout contract (e.g. `PreToolUse` must
  return a `decision` of `allow`/`deny`/`ask`/`force_ask`; `Stop` must
  omit `decision` or omit `"continue"` to let the agent actually stop).
  Getting this wrong doesn't just fail to report status, it can stall or
  mis-gate the agent's own execution loop. Switchboard's existing hook
  commands are a one-line `curl ... || true` that the CLI never inspects
  the output of — that pattern does not fit here.
- This needs a genuinely new `HookSpec.merge_strategy` (a
  project-local-file writer, not a global-config merger) plus a small
  wrapper script that posts status non-blockingly and still prints the
  correct passthrough JSON per event — real, scoped follow-up work, not
  a quick field add. Tracked here rather than guessed at.

`hook_spec()` returns `None` until that's built, same honest pattern as
`GenericProfile` (no hooks claimed) — see docs/design/family-capability-matrix.md's
Kimi entry for the precedent (richest hook vocabulary of its day, still
correctly scoped to launch-only until its own mechanical gap — there, a
TOML merge strategy — was actually closed).

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

from switchboard.families.base import Capabilities, Detection, HookSpec, VoiceSpec, detect_on_path

NAME = "antigravity"
DISPLAY_NAME = "Antigravity"
EXECUTABLES = ("agy",)
KNOWN_PATHS: tuple[str, ...] = ()

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

    def hook_spec(self) -> HookSpec | None:
        # Not implemented yet — see module docstring for exactly why this
        # isn't a quick field add (project-local file, blocking per-event
        # JSON contract, no SessionStart/SessionEnd analog).
        return None

    def hook_status_for(self, event: str) -> str | None:
        return None

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
        # on whether this machine actually has a usable PTT provider, and
        # hooks are a real, tracked gap rather than silently claimed.
        from switchboard.voice import registry as voice_registry

        voice_available = voice_registry.get(self.default_voice().provider).available().available
        return Capabilities(hooks=False, effort=True, voice=voice_available, tier="status")
