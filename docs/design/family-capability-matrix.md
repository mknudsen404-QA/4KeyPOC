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
| Lifecycle hooks: events | 9: `SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop, SessionEnd, SubagentStart, SubagentStop` (confirmed live, see `status_table.HOOK_MATCHERS`) | 5: `UserPromptSubmit, PermissionRequest, PostToolUse, Stop, SessionEnd` (confirmed live, `~/.codex/hooks.json` already has our entries) | 11 documented: `SessionStart, SessionEnd, BeforeAgent, AfterAgent, BeforeModel, AfterModel, BeforeToolSelection, BeforeTool, AfterTool, PreCompress, Notification` — **not run live** | 20 documented: `UserPromptSubmit, UserPromptQueued, PreToolUse, PostToolUse, PostToolUseFailure, Stop, TurnStarted, PermissionRequest, PermissionResult, SessionStart, SessionEnd, SessionHeartbeat, SubagentStart, SubagentStop, TaskStarted, StopFailure, Interrupt, PreCompact, PostCompact, Notification` — **not run live** |
| Hook config file / shape | `~/.claude/settings.json`, `hooks.<Event> = [{matcher, hooks:[{type:"command",command}]}]` — same shape Switchboard already merges by marker | `$CODEX_HOME/hooks.json` (`~/.codex/hooks.json`), same shape as Claude but no matcher support seen; Switchboard rewrites the whole group per event | `.gemini/settings.json` (project) / `~/.gemini/settings.json` (user) / `/etc/gemini-cli/settings.json` (system) — **identical JSON shape to Claude's**: `hooks.<Event> = [{matcher, hooks:[{name?, type:"command", command, timeout?}]}]`. Gemini even ships `gemini hooks migrate` to import a Claude Code `settings.json` directly. | `~/.kimi-code/config.toml`, a flat `[[hooks]]` TOML array — one table per rule (`event`, `matcher`, `command`, `timeout`), **not** grouped by event like Claude/Codex/Gemini. A new merge strategy (e.g. "TOML array, keyed by our command substring") is needed; the existing "rewrite by event" (Codex) and "merge by marker" (Claude) strategies don't fit as-is. |
| Matcher support | Yes — exact string per event, `"|"`-joined for the `Notification` type filter | Not observed (Switchboard doesn't send one) | Yes — regex for `BeforeTool`/`AfterTool`, exact string for lifecycle events, `"*"`/`""` = wildcard | Yes — regex, optional per rule |
| Per-session env passthrough (`SWITCHBOARD_SLOT`) | Yes (confirmed live — `X-Switchboard-Slot` header already flows end to end) | Yes (confirmed live) | **Unconfirmed** — the docs say hooks "are executed with a sanitized environment" and only guarantee `GEMINI_PROJECT_DIR`, `GEMINI_PLANS_DIR`, `GEMINI_SESSION_ID`, `GEMINI_CWD`, and a `CLAUDE_PROJECT_DIR` alias. `SWITCHBOARD_SLOT` may not survive; `GEMINI_SESSION_ID` could substitute for slot identification if a live test shows the env var doesn't. | **Unconfirmed** — docs don't mention environment passthrough at all; hook commands are plain shell commands so parent-env inheritance is likely but not documented or tested. |
| Effort/reasoning flag and accepted values | `--effort {low,medium,high,xhigh,max}` (confirmed live) | `-c model_reasoning_effort=<value>`; only `low` confirmed against a real `config.toml` on this machine (current value: `medium`, also plausible). `xhigh`/`max` still unconfirmed for this key (see `model.CODEX_EFFORT_MAP`'s comment) — clamped to `high`. | **No CLI flag exists.** Effort-like control is `thinkingConfig.thinkingBudget` (an integer token count, not a tier) under `modelConfigs` in `settings.json` — a config-file edit, not a launch arg. No `-c key=value` equivalent was found in `--help` or the CLI reference. | **No CLI flag exists** either (`-c` is taken by `--continue`, not config-override). Effort is `[thinking].effort` in `config.toml`, vocabulary `low/medium/high/xhigh/max` (documented default `max`, falls back to the model's default if unsupported) — same 5-tier vocabulary Switchboard already uses, but only reachable by editing the CLI's own config file today, not a per-launch flag. |
| Native voice / dictation | `/voice`, hold-`Space` PTT (confirmed working today per this plan's header) | None known | **Experimental, off by default**: `experimental.voiceMode` setting; hold-`Space` PTT (`app.voiceModePTT` keybinding) once enabled; transcription backend `experimental.voice.backend` defaults to `"gemini-live"` (cloud) with a local Whisper option (`experimental.voice.whisperModel`). Same shape as Claude's native voice (hold-space in the focused terminal) — a real find, not something the plan's authors expected ("no native voice known" is wrong for Gemini once `voiceMode` is turned on). Not verified live. | None found in the docs read. |
| Slash-command surface usable from a key | `/voice` (used today), plan/approve slash commands exist but unused by Switchboard | Unknown — Codex's TUI has no documented slash-command list found | `/hooks panel`, `/hooks enable-all`/`disable-all`, presumably `/voice` once enabled | `/model`, `/yolo`, `/auto` documented; no hook-management slash command found |
| Session-end signal | `SessionEnd` (confirmed live) | `SessionEnd` (confirmed live) | `SessionEnd` — "Fires when the CLI exits or a session is cleared" | `SessionEnd` — "Session close/archive" |

## Support tier (per the plan's definitions)

- **Claude**: **Full** — hooks + effort + native voice. Already shipped.
- **Codex**: **Status** — hooks + effort (with the xhigh/max clamp), voice
  only via a future `hotkey` provider (Phase 4). Matches the plan's
  expectation.
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
3. Codex: confirm `xhigh`/`max` against `model_reasoning_effort` with a
   real session (today's config.toml only proves `medium` is accepted);
   confirm whether `-c` overrides persist for the whole session or just
   the first turn (Phase 5.2 in the plan).

## Sources

- Claude Code: this machine's `~/.claude/settings.json`-equivalent
  behaviour, already encoded in `switchboard/status_table.py` and
  `switchboard/model.py`.
- Codex: `codex --help`, `codex-cli` 0.154.0-alpha.6.2 on this machine,
  `~/.codex/config.toml`, `~/.codex/hooks.json` (Switchboard's own
  entries, already installed).
- Gemini CLI: `@google/gemini-cli@0.59.0` installed to a scratch npm
  prefix; `gemini --help`; bundled docs at
  `node_modules/@google/gemini-cli/bundle/docs/{hooks/index.md,
  hooks/writing-hooks.md, cli/generation-settings.md, cli/settings.md,
  reference/keyboard-shortcuts.md}`.
- Kimi Code CLI: `github.com/MoonshotAI/kimi-code`,
  `moonshotai.github.io/kimi-code/en/customization/hooks.html`,
  `moonshotai.github.io/kimi-code/en/configuration/config-files.html`,
  `moonshotai.github.io/kimi-code/en/reference/kimi-command.html`.
