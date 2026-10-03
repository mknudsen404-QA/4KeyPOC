# Family capability matrix (Phase 0 spike)

Written 2026-09-14. Companion to `slot-settings-and-family-parity-plan.md`
Phase 0. Produced by reading each CLI's `--help`, bundled docs, and (for
Claude/Codex) this machine's real config/hooks files — Gemini CLI was
installed into a scratch npm prefix and its bundled docs read directly;
Kimi Code CLI's docs were read from moonshotai.github.io/kimi-code since
neither Gemini nor Kimi has a real session on this machine yet (no live
10-minute session with the hook server was run for either — see "Not yet
verified" below). Per the plan's ground rule, anything not verified
against a real session counts as unconfirmed and `capabilities()` must
say no for it until it is.

## Matrix

| Capability | Claude | Codex | Gemini CLI | Kimi Code CLI |
|---|---|---|---|---|
| Binary / install / version tested | `claude` 2.1.271, `~/.local/bin/claude` | `codex-cli` 0.154.0-alpha.6.2, `/Applications/ChatGPT.app/Contents/Resources/codex` | `@google/gemini-cli` 0.59.0 (npm, scratch install, not installed for real use) | `kimi-code` — official install script or npm; not installed on this machine (docs-only) |
| Lifecycle hooks: events | 9: `SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop, SessionEnd, SubagentStart, SubagentStop` (confirmed live, see `status_table.HOOK_MATCHERS`) | **12 schema-known** (extracted from the installed binary's embedded JSON schema, `strings` on `codex`): `PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, SessionStart, SessionEnd, UserPromptSubmit, SubagentStart, SubagentStop, Stop, Interrupt` — more than Phase 0 assumed. **Confirmed actually firing** in a real `codex exec` session (Phase 5, `--dangerously-bypass-hook-trust`): `UserPromptSubmit, PostToolUse, Stop, SessionEnd` (exact payload shapes recorded in `families/codex.py`'s module docstring and `tests/golden/codex_launch_turn_stop.*.jsonl`). `PermissionRequest` has a persisted trust entry on this machine (proof it fired at some point in real interactive use) but wasn't independently re-fired this pass — `exec` mode has no interactive approval to trigger it. **Confirmed NOT firing** even with every schema-known event wired to a logger and trust bypassed: `PreToolUse, PreCompact, PostCompact, SessionStart, SubagentStart, SubagentStop, Interrupt` — genuinely absent in this scenario, not a trust artifact. Real gotcha found along the way: hooks in `~/.codex/hooks.json` are gated by a persisted per-hook trust hash (`[hooks.state]` in `config.toml`); an interactive session establishes trust once, but `codex exec` silently no-ops any hook that isn't already trusted, with zero error output. | 11 documented: `SessionStart, SessionEnd, BeforeAgent, AfterAgent, BeforeModel, AfterModel, BeforeToolSelection, BeforeTool, AfterTool, PreCompress, Notification` — **not run live** | 20 documented: `UserPromptSubmit, UserPromptQueued, PreToolUse, PostToolUse, PostToolUseFailure, Stop, TurnStarted, PermissionRequest, PermissionResult, SessionStart, SessionEnd, SessionHeartbeat, SubagentStart, SubagentStop, TaskStarted, StopFailure, Interrupt, PreCompact, PostCompact, Notification` — **not run live** |
| Hook config file / shape | `~/.claude/settings.json`, `hooks.<Event> = [{matcher, hooks:[{type:"command",command}]}]` — same shape Switchboard already merges by marker | `$CODEX_HOME/hooks.json` (`~/.codex/hooks.json`), same shape as Claude but no matcher support seen; Switchboard rewrites the whole group per event | `.gemini/settings.json` (project) / `~/.gemini/settings.json` (user) / `/etc/gemini-cli/settings.json` (system) — **identical JSON shape to Claude's**: `hooks.<Event> = [{matcher, hooks:[{name?, type:"command", command, timeout?}]}]`. Gemini even ships `gemini hooks migrate` to import a Claude Code `settings.json` directly. | `~/.kimi-code/config.toml`, a flat `[[hooks]]` TOML array — one table per rule (`event`, `matcher`, `command`, `timeout`), **not** grouped by event like Claude/Codex/Gemini. A new merge strategy (e.g. "TOML array, keyed by our command substring") is needed; the existing "rewrite by event" (Codex) and "merge by marker" (Claude) strategies don't fit as-is. |
| Matcher support | Yes — exact string per event, `"|"`-joined for the `Notification` type filter | Not observed (Switchboard doesn't send one) | Yes — regex for `BeforeTool`/`AfterTool`, exact string for lifecycle events, `"*"`/`""` = wildcard | Yes — regex, optional per rule |
| Per-session env passthrough (`SWITCHBOARD_SLOT`) | Yes (confirmed live — `X-Switchboard-Slot` header already flows end to end) | Yes (confirmed live) | **Unconfirmed** — the docs say hooks "are executed with a sanitized environment" and only guarantee `GEMINI_PROJECT_DIR`, `GEMINI_PLANS_DIR`, `GEMINI_SESSION_ID`, `GEMINI_CWD`, and a `CLAUDE_PROJECT_DIR` alias. `SWITCHBOARD_SLOT` may not survive; `GEMINI_SESSION_ID` could substitute for slot identification if a live test shows the env var doesn't. | **Unconfirmed** — docs don't mention environment passthrough at all; hook commands are plain shell commands so parent-env inheritance is likely but not documented or tested. |
| Effort/reasoning flag and accepted values | `--effort {low,medium,high,xhigh,max}` (confirmed live) | `-c model_reasoning_effort=<value>`; **all five values confirmed live** (Phase 5: each one echoed back in the CLI's own startup banner, e.g. `codex exec -c model_reasoning_effort=xhigh` prints "reasoning effort: xhigh", no error for any of low/medium/high/xhigh/max). The earlier xhigh/max→high clamp is removed (`families/codex.py`'s `CODEX_EFFORT_VALUES`). Whether the override persists the whole session or only the first turn wasn't independently tested. | **No CLI flag exists.** Effort-like control is `thinkingConfig.thinkingBudget` (an integer token count, not a tier) under `modelConfigs` in `settings.json` — a config-file edit, not a launch arg. No `-c key=value` equivalent was found in `--help` or the CLI reference. | **No CLI flag exists** either (`-c` is taken by `--continue`, not config-override). Effort is `[thinking].effort` in `config.toml`, vocabulary `low/medium/high/xhigh/max` (documented default `max`, falls back to the model's default if unsupported) — same 5-tier vocabulary Switchboard already uses, but only reachable by editing the CLI's own config file today, not a per-launch flag. |
| Native voice / dictation | `/voice`, hold-`Space` PTT (confirmed working today per this plan's header) | None known | **Experimental, off by default**: `experimental.voiceMode` setting; hold-`Space` PTT (`app.voiceModePTT` keybinding) once enabled; transcription backend `experimental.voice.backend` defaults to `"gemini-live"` (cloud) with a local Whisper option (`experimental.voice.whisperModel`). Same shape as Claude's native voice (hold-space in the focused terminal) — a real find, not something the plan's authors expected ("no native voice known" is wrong for Gemini once `voiceMode` is turned on). Not verified live. | None found in the docs read. |
| Slash-command surface usable from a key | `/voice` (used today), plan/approve slash commands exist but unused by Switchboard | Unknown — Codex's TUI has no documented slash-command list found | `/hooks panel`, `/hooks enable-all`/`disable-all`, presumably `/voice` once enabled | `/model`, `/yolo`, `/auto` documented; no hook-management slash command found |
| Session-end signal | `SessionEnd` (confirmed live) | `SessionEnd` (confirmed live) | `SessionEnd` — "Fires when the CLI exits or a session is cleared" | `SessionEnd` — "Session close/archive" |

## Support tier (per the plan's definitions)

- **Claude**: **Full** — hooks + effort + native voice. Already shipped.
- **Codex**: **Status** on this machine — hooks + all 5 effort tiers (no
  clamp, Phase 5), voice via `hotkey` (Phase 4), which isn't available
  here (no dictation app). `CodexProfile.capabilities()` computes this
  live rather than hardcoding it: `doctor`/the settings UI report
  **Full** automatically the moment a dictation app is installed —
  verified by monkeypatching `available()` in `test_families.py`, not
  yet by an actual install.
- **Gemini CLI**: **Status**, provisionally — hooks exist (11 events,
  Claude-compatible config shape, even a Claude→Gemini hook migrator) but
  effort has no launch flag (config-file only, and not tier-based) and env
  passthrough for `SWITCHBOARD_SLOT` is unconfirmed. If a live test shows
  `SWITCHBOARD_SLOT` does *not* survive Gemini's "sanitized environment,"
  Gemini hooks can still work by keying off `GEMINI_SESSION_ID` instead
  (the bridge would need to learn a session_id → slot mapping at launch
  time rather than reading a header). Voice is a genuine upside surprise:
  `experimental.voiceMode` gives Gemini the same hold-space native voice
  Claude has, once enabled and verified — worth reflecting in
  `default_voice()` as `claude_native`-equivalent (call it `hold_space_native`)
  rather than routing Gemini through the generic `hotkey` provider, *after*
  a live verification.
- **Kimi Code CLI**: **Launch-only**, provisionally, despite having the
  richest hook vocabulary of the four (20 events, including an explicit
  `PermissionRequest`/`PermissionResult` pair that maps cleanly to
  `needs_input`/`done`). The blocker is entirely mechanical: hooks live in
  a flat TOML array, not grouped JSON, so `hooks_install.py`'s two existing
  merge strategies (Claude's "merge by marker", Codex's "rewrite whole
  group") don't apply without a third `HookSpec.merge_strategy` for TOML
  arrays. Effort has the same 5-tier vocabulary as Switchboard's dial but
  no launch flag — config-file only. Until both a TOML merge strategy is
  written and a live session confirms `hook_event_name`/`session_id`
  really arrive the way the docs say, Kimi should be treated as
  launch-only (hooks disabled) rather than guessing at the TOML shape
  against a real install.

## Phase 4 update (2026-09-18): voice provider availability, checked live

The `VoiceProvider` abstraction (`switchboard/voice/`) is built and wired up.
Its `hotkey` provider — the plan's recommended answer for Codex and every
other non-Claude family — is **not available on this machine**, checked
directly rather than assumed:

- No known dictation app installed: `/Applications/Aqua Voice.app`,
  `/Applications/Wispr Flow.app`, `/Applications/Superwhisper.app` — none
  present.
- macOS built-in Dictation has never been enabled: `defaults read -g
  AppleDictationAutoEnable` returns "does not exist".

`doctor` and the settings UI's `/api/voice-providers` both report this
honestly. `claude_native` remains the only provider confirmed working live.
Installing a dictation app (or enabling built-in Dictation) and re-running
Phase 4.4's validation — a Codex slot with `provider: hotkey` actually
recording and landing text in the Codex prompt on a hold — is the next real
gap, not a code gap.

## Phase 4.4 validation (2026-10-03): hotkey confirmed live for Codex

Built-in macOS Dictation was enabled (System Settings -> Keyboard ->
Dictation), which immediately fixed `_macos_dictation_enabled()`'s
detection — except the detection signal itself was stale (see
`voice/hotkey.py`'s module docstring: the old `AppleDictationAutoEnable`
global-domain key no longer exists on macOS 26; replaced with reading
`com.apple.assistant.support`'s per-locale `Installed` flag).

Dictation's default shortcut ("press Control twice") is a double-tap
gesture this provider's single press-and-release can't drive. Its
"Customize..." option accepts an arbitrary single key, including one
synthesized rather than physically present on the keyboard: F13 was
recorded by posting a synthetic keydown/keyup into the shortcut recorder
while it was focused (`key_injector.post_key_global`), with no modifier
and no typing meaning elsewhere, so it can't collide with anything.
`families/codex.py`'s `default_voice()` now returns `chord="f13",
mode="toggle"`.

A second, more structural bug surfaced along the way: the shared
`key_injector` (bound to `CGEventPostToPid`, targeting one process) is
correct for `claude_native` typing into Claude's own focused terminal,
but silently can't trigger a system-wide OS shortcut — `CGEventPostToPid`
bypasses the global-hotkey dispatch entirely. Fixed by giving
`VoiceContext` a second injector, `global_key_injector`, bound to
`CGEventPost(kCGHIDEventTap, ...)` (system-wide), which `hotkey.py` now
uses instead. See `key_injector.post_key_global`'s docstring and
`voice/base.py`'s `VoiceContext` docstring for the distinction.

End-to-end confirmed live: launched a real Codex slot, pressed the
board's PTT key, got the macOS microphone/Terminal-automation permission
prompts, granted them, and dictated text directly into the Codex prompt.
Codex's support tier is genuinely **Full** now, not just reachable —
`doctor`/the settings UI already computed this live from
`hotkey.available()` rather than hardcoding it, so no separate change was
needed there.

Also fixed in passing: `doctor.check_accessibility()` imported
`AXIsProcessTrusted` from the wrong module (`Quartz` instead of
`ApplicationServices`), so it always reported "pyobjc missing" even when
Accessibility was already granted — unrelated to hotkey specifically, but
it was masking the real Accessibility state while debugging this.

## Not yet verified (needs a real 10-minute session with the hook server logging raw payloads, per the plan)

1. Gemini CLI: install for real (not the scratch npm prefix used here),
   authenticate, run a session with `.gemini/settings.json` hooks pointed
   at `http://127.0.0.1:8877/switchboard-hook/<Event>` for all 11 events,
   and confirm (a) whether `SWITCHBOARD_SLOT` survives the "sanitized
   environment", (b) the exact JSON payload shape per event (field names
   likely differ from Claude/Codex's), (c) whether `experimental.voiceMode`
   actually works hands-off in a terminal tab the way Claude's `/voice`
   does.
2. Kimi Code CLI: install for real, write a `[[hooks]]` TOML block for all
   20 events pointed at the same loopback endpoint, and confirm (a) the
   stdin payload's exact field names beyond the four documented base
   fields, (b) whether `SWITCHBOARD_SLOT` (or any custom env var) reaches
   the hook command's environment, (c) whether `[thinking].effort` can be
   overridden per-launch some way not covered by `--help` (e.g. a
   `KIMI_MODEL_THINKING_*` env var family — only `KIMI_MODEL_THINKING_KEEP`
   is documented, not an effort override).
3. Codex: whether a `-c model_reasoning_effort=` override persists for
   the whole session or only the first turn (xhigh/max acceptance itself
   is now confirmed — Phase 5). Would need a scripted multi-turn
   interactive session, not just `codex exec`'s single shot.
4. Codex: install a real dictation app (or enable macOS Dictation) and
   redo Phase 4.4's validation — a Codex slot with `provider: hotkey`
   actually recording and landing text in the prompt.

## Sources

- Claude Code: this machine's `~/.claude/settings.json`-equivalent
  behaviour, already encoded in `switchboard/status_table.py` and
  `switchboard/model.py`.
- Codex: `codex --help`, `codex-cli` 0.154.0-alpha.6.2 on this machine,
  `~/.codex/config.toml`, `~/.codex/hooks.json` (Switchboard's own
  entries, already installed). Phase 5: `strings -a` on the installed
  `codex` binary (it embeds its own hook-event JSON schema as literal
  text — a `HookEventName` enum and `hooks.state`/`trusted_hash` config
  keys), then a real `codex exec --dangerously-bypass-hook-trust` session
  against a temporary `~/.codex/hooks.json` pointed at a throwaway local
  HTTP logger (real hooks.json backed up first, restored immediately
  after, verified byte-identical via `diff`) to see which schema-known
  events actually fire.
- Gemini CLI: `@google/gemini-cli@0.59.0` installed to a scratch npm
  prefix; `gemini --help`; bundled docs at
  `node_modules/@google/gemini-cli/bundle/docs/{hooks/index.md,
  hooks/writing-hooks.md, cli/generation-settings.md, cli/settings.md,
  reference/keyboard-shortcuts.md}`.
- Kimi Code CLI: `github.com/MoonshotAI/kimi-code`,
  `moonshotai.github.io/kimi-code/en/customization/hooks.html`,
  `moonshotai.github.io/kimi-code/en/configuration/config-files.html`,
  `moonshotai.github.io/kimi-code/en/reference/kimi-command.html`.
- Antigravity CLI: see the dedicated section below for sources (this spike
  was run live on this machine, not from docs alone).

## Antigravity CLI (Phase 0 spike, run live — 2026-10-03)

Installed via `brew install --cask antigravity-cli` (binary `agy`,
version 1.2.16 at spike time). Distinct from the `antigravity` and
`antigravity-ide` casks, which are the GUI app and the VS Code-fork IDE —
`antigravity-cli` is the one with an actual terminal/CLI surface, so it's
the one that fits Switchboard's launch-into-a-Terminal-tab model. Shares
its config root with Gemini CLI (`~/.gemini/antigravity-cli/`), and is
itself Gemini-backed by default (see Effort below).

| Capability | Antigravity CLI |
|---|---|
| Binary / install / version tested | `agy` 1.2.16 (`antigravity-cli` cask), `/opt/homebrew/bin/agy` |
| Lifecycle hooks: events | 5, genuinely different shape from Claude/Codex/Gemini/Kimi: `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`. **No `SessionStart`, `SessionEnd`, or `UserPromptSubmit` analog at all** — confirmed from the bundled spec, not inferred. |
| Hook config file / shape | **Project-local** `.agents/hooks.json` (the doc's own example path), not a single global user config file — a real mechanical difference from every other family's "one file, merge by marker/rewrite group" approach. JSON object keyed by *named hook* (not grouped by event like Claude/Codex/Gemini), each hook object holding its own per-event handler lists. |
| Hook execution model | **Synchronous, blocks the agent loop** (documented explicitly: "no async execution"). Each event has its own required JSON stdout contract (e.g. `PreToolUse` must return a `decision` of `allow`/`deny`/`ask`/`force_ask`; `Stop` must omit `"decision": "continue"` to let the agent actually stop) — unlike Claude/Codex/Gemini's fire-and-forget `curl ... \|\| true`, a malformed response here can stall or mis-gate the agent's own execution loop, not just fail to report status. |
| Per-session env passthrough (`SWITCHBOARD_SLOT`) | **Unconfirmed** — not attempted this pass; hooks.json's `command` field says only that `~` expands and cwd is the hooks.json directory, nothing about environment sanitization either way. |
| Effort/reasoning flag and accepted values | `--effort {low,medium,high,xhigh,max}` exists but is **not a uniform 5-tier flag** — legality depends entirely on the selected `--model`, and a mismatch is a hard launch error, not a clamp. Confirmed live: the default model (`gemini-3.8-flash`, used when `--model` is omitted) only accepts `low`/`medium`/`high`; `gemini-3.1-pro` only accepts `low`/`high` (no `medium`, `xhigh`, or `max` at all). `families/antigravity.py`'s `effort_args()` clamps `xhigh`/`max` down to `high` so a configured slot can never turn into a launch error — see its docstring for the exact error text that proved this. |
| Native voice / dictation | **None found** in `agy --help` or its bundled docs. `mic-serve` serves *this* machine's microphone to a CLI on *another* host (a remote-session feature) — not local push-to-talk. Falls back to the same system-dictation `hotkey` provider Codex uses (F13 bound to macOS Dictation, confirmed live for Codex 2026-10-03) — **confirmed live for Antigravity too**, same session: a real `agy` slot launched, PTT held via the board's dedicated mic key, macOS Dictation transcribed directly into the Antigravity prompt. |
| Slash-command surface usable from a key | Unknown — no documented slash-command list found. |
| Session-end signal | **None** — the 5-event table has nothing that means "the CLI process exited," only `Stop` ("execution loop terminates," i.e. end of a turn). Same degrade path as Codex's missing `SessionStart`: a slot just sits at its last reported status until liveness probing notices the process is gone. |

### Support tier: Launch + effort + voice, no live status (`"status"`)

`AntigravityProfile.capabilities()` (like Codex's) computes this live
rather than hardcoding it: `hooks=False` always (nothing wired yet —
real gap, see above, not an oversight), `effort=True` always (clamped so
it can never error), `voice` tracks `hotkey.available()` same as Codex.
Confirmed live end-to-end this session: launched a real `agy` slot
(`gemini-3.8-flash` by default — "wired up to Gemini" with zero extra
flags, since that's Antigravity's own default model), selected it with
the board's agent key, held the dedicated PTT key, and macOS Dictation
transcribed text straight into the Gemini-backed session. What's
*correctly* not claimed: no key-light/status auto-update, since nothing
drives it — the slot stays at `launched` until a future pass builds the
project-local-`hooks.json` + blocking-JSON-contract wrapper the hooks
doc requires (tracked in `families/antigravity.py`'s module docstring,
same scoped-gap precedent as Kimi's TOML merge strategy).

A second, more structural bug surfaced and was fixed while verifying
this: the shared `key_injector` (bound to `CGEventPostToPid`, targeting
one process) is correct for `claude_native` typing into Claude's own
focused terminal, but can't trigger a system-wide OS shortcut like
Dictation's configured key — `CGEventPostToPid` bypasses the
global-hotkey dispatch entirely. `VoiceContext` now carries a second
injector, `global_key_injector`, bound to `CGEventPost(kCGHIDEventTap,
...)`, which `hotkey.py` uses instead. This fix is what made both the
Codex and Antigravity live PTT verifications above actually work — see
`key_injector.post_key_global`'s docstring.
