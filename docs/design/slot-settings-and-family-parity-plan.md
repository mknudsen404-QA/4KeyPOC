# Slot settings UI + CLI-family parity plan

Written 2026-09-14. Handoff document for an implementing agent. Companion
to `auto-install-plan.md` (which owns installer/Windows/permissions work);
this plan owns *what each key launches* and *how well each CLI family is
supported*. PTT is confirmed working for Claude as of today.

## Goals

1. **Slot settings a non-developer can change**: which CLI each agent key
   launches (Codex, Claude, Gemini, Kimi, …), in which folder, with which
   effort, and how push-to-talk should work for that slot — without
   editing JSON or opening a terminal.
2. **Feature parity across CLI families**, with Codex first: status LEDs,
   effort dial, PTT. Where a CLI has no native equivalent (Codex has no
   `/voice`), provide it through a family-agnostic mechanism instead of
   giving up.
3. **Adding a new CLI family is one file**, not a scavenger hunt.

## Current state (what the code says)

- Per-slot config lives in `host/agents.json` (`slot, name, family, cwd,
  command, title, effort`), edited by hand or with
  `switchboard config --slot N …`. Family defaults to `codex`.
- Family-specific behaviour is spread over four modules:
  `model.effort_args` (per-family effort flags),
  `launcher.KNOWN_COMMAND_PATHS` (where to find the binary),
  `model.VOICE_SUPPORTED_FAMILIES` (`("claude",)`), and
  `status_table.STATUSES` + `hooks_install.py` (per-family hook→status
  maps and two separate hook installers). Adding a family today touches
  all of them.
- The bridge reads `agents.json` once at startup (`Bridge.launch_config`);
  edits need a bridge restart.
- The bridge already serves loopback HTTP on `127.0.0.1:8877` for hooks
  (`hooks_server.py`, POST only).
- Codex already has 5 hooks registered (`UserPromptSubmit`,
  `PermissionRequest`, `PostToolUse`, `Stop`, `SessionEnd`). Claude has 9.
  `blocked` is unreachable for every family (no error hook anywhere).
- PTT = `KeyInjector` holding space in the focused tab (Claude's native
  hold-to-talk). Nothing family-agnostic exists for voice.

## Architecture

```
                    ┌──────────────────────────────┐
  Settings web UI ──┤                              │
  (loopback page)   │   SlotSettingsService        │──► agents.json (v2, atomic write)
  `switchboard      │   load / validate / save /   │
   config` CLI    ──┤   diff / changed-since       │──► change notice ──► Bridge reload
                    └──────────────┬───────────────┘
                                   │ reads
                    ┌──────────────▼───────────────┐
                    │   FamilyRegistry             │  one FamilyProfile per CLI:
                    │   claude / codex / gemini /  │  detect(), effort_args(), hook
                    │   kimi / generic             │  spec, default VoiceProvider,
                    └──────────────┬───────────────┘  capabilities()
                                   │ used by
              launcher · hooks_install · status_table · reducer · doctor
                                   │
                    ┌──────────────▼───────────────┐
                    │   VoiceProvider              │  claude_native | hotkey |
                    │   (per slot, default from    │  builtin_stt (later) | none
                    │    family)                   │
                    └──────────────────────────────┘
```

Principles applied:

- **Single responsibility / open-closed**: `FamilyProfile` is the only
  place a family's quirks live. New family = new profile module + tests.
  Everything else asks the registry.
- **Liskov**: every profile satisfies the same `FamilyProfile` protocol;
  `GenericProfile` is a valid profile with all capabilities `False`, so
  an unknown command still launches and shows `launched`/liveness.
- **Interface segregation**: `VoiceProvider` (`hold()`, `release()`,
  `available()`) is separate from `FamilyProfile`; a slot can pair any
  family with any provider.
- **Dependency inversion**: launcher/reducer/hooks_install depend on the
  protocols, not on `if family == "claude"`.
- **DRY**: `status_table.py` stays the single source for the status
  vocabulary; profiles contribute *their* hook→status rows to it rather
  than duplicating it. The CLI `config` and the web UI both call
  `SlotSettingsService` — no logic in either front end.
- **Atomic/consistent settings**: validate the whole document, write
  temp + `os.replace`, keep `agents.json.bak`, reject on schema error
  with a field-level message. One `settings_version` field; migrations
  are explicit functions with tests.

## Ground rules for the implementer

- No new runtime dependencies for the web UI (stdlib `http.server`,
  hand-written HTML/JS). The page is served on loopback only.
- Never send a keystroke or text into an agent session except through
  the existing `Focus` + `KeyInjector`/`VoiceProvider` path; no new
  "type this string" shortcuts.
- Every family claim (hooks available, flag syntax, voice) must be
  verified against a real session and recorded in the profile's
  docstring with the CLI version. Unverified = `capabilities()` says no.
- Tests for every phase; `pytest -m "not hardware"` green; golden traces
  for Codex added alongside the Claude ones.
- Commits prefixed `Slots N.x:`.

## Phase 0 — Family capability spike (half a day, produces a matrix, no code changes)

For each of Codex, Gemini CLI, Kimi CLI (and re-confirm Claude), record
in `docs/design/family-capability-matrix.md`:

| Capability | Claude | Codex | Gemini | Kimi |
|---|---|---|---|---|
| Binary name / install method / version tested | | | | |
| Lifecycle hooks (which events, config file, matcher support) | 9 events | 5 events | ? | ? |
| Per-session env passthrough (for `SWITCHBOARD_SLOT`) | yes | yes | ? | ? |
| Effort/reasoning flag and accepted values | `--effort` | `-c model_reasoning_effort=` (xhigh/max unconfirmed) | ? | ? |
| Native voice / dictation | `/voice` hold-space | none known | ? | ? |
| Slash-command surface usable from a key (approve, review, …) | | | | |
| Session-end signal | `SessionEnd` | `SessionEnd` | ? | ? |

Do it by reading each CLI's current `--help` and docs, then a 10-minute
live session per family with the hook server logging raw payloads
(`Log` lines already exist). Where a family has hooks with a different
event vocabulary, note the mapping to our statuses. The matrix decides
which tier each family lands in (below). Kimi and Gemini are installed
on no machine today — install them into a scratch dir for the spike.

Support tiers (used by the UI to set expectations honestly):

- **Full**: hooks + effort + voice (native or via provider).
- **Status**: hooks + effort, voice via provider only.
- **Launch-only**: no hooks; LED shows `launched`, then liveness
  (`unknown`/`empty`) — nothing else. Any unrecognised command lands here.

## Phase 1 — `FamilyRegistry` refactor (no behaviour change)

Files: new `host/switchboard/families/{__init__,base,claude,codex,generic}.py`;
edit `model.py`, `launcher.py`, `status_table.py`, `hooks_install.py`,
`reducer.py`, `doctor.py`; tests `test_families.py`.

1.1 `FamilyProfile` protocol:

```python
class FamilyProfile(Protocol):
    name: str                      # "claude"
    display_name: str              # "Claude Code"
    executables: tuple[str, ...]   # ("claude",) — basename(s) that imply this family
    known_paths: tuple[str, ...]   # fallback absolute paths (from launcher today)
    def detect(self) -> Detection: ...            # found path + version, or not found
    def effort_args(self, effort: str) -> list[str]: ...
    def hook_spec(self) -> HookSpec | None: ...   # config file, event→status rows, matchers, command template
    def default_voice(self) -> VoiceSpec: ...     # e.g. VoiceSpec("claude_native") / VoiceSpec("hotkey")
    def capabilities(self) -> Capabilities: ...   # hooks/effort/voice booleans + tier
```

1.2 Move `effort_args` bodies, `KNOWN_COMMAND_PATHS`, and the
per-family hook maps into `claude.py` / `codex.py`. `status_table.STATUSES`
keeps the status rows (name/busy/color/pulse); the `claude_hooks` /
`codex_hooks` columns become derived from the registry
(`registry.hook_rows()`), so the generated firmware header is unchanged
(assert with the existing currency test).

1.3 `hooks_install.py` becomes one generic installer driven by
`HookSpec` (file path, JSON shape, marker, matcher support); the two
current functions become thin per-family adapters over it. The Codex
"rewrite all groups" strategy vs the Claude "merge by marker" strategy is
a `HookSpec.merge_strategy` field, not two code paths.

1.4 `family` in `agents.json` becomes optional: if absent, infer from
`command`'s basename via `executables`; unknown → `generic`. The current
default of `codex` for a missing family is a footgun (a slot configured
with `command: "gemini"` would get Codex effort flags) — the inference
fixes it; keep a one-release warning log when inference changes an old
config's meaning.

1.5 `doctor` gains a `families` check: for each family referenced by a
slot, `detect()` result and tier.

Acceptance: all existing tests pass unchanged; `grep -rn '"claude"\|"codex"'
host/switchboard/*.py` matches only inside `families/` and tests;
firmware header currency test passes without regeneration.

## Phase 2 — Settings model v2 + service + hot reload

Files: `host/switchboard/settings.py` (new), `host/agents.example.json`,
`launcher.py` (`config_command` becomes a thin caller), `bridge.py`,
tests `test_settings.py`.

2.1 Schema v2 (documented in `host/settings.schema.json`, validated by a
small stdlib validator — no jsonschema dependency):

```json
{
  "settings_version": 2,
  "defaults": { "cwd": "~/Documents", "effort": "medium" },
  "slots": [
    {
      "slot": 1,
      "name": "Maestro",
      "command": "codex",
      "family": "codex",
      "args": [],
      "cwd": "~/Documents/project",
      "effort": "high",
      "voice": { "provider": "hotkey", "chord": "fn", "mode": "toggle" },
      "env": {}
    }
  ]
}
```

`family` optional (inferred), `args` separate from `command` so the UI
can show a command dropdown plus a free-text args field, `voice` optional
(family default), `env` for per-slot API keys/models later.

2.2 Migration `v1 → v2` (`agents` → `slots`, `title` dropped in favour of
the generated title, everything else carried). Runs on load, writes back
only when the user saves. Test with the current `agents.example.json`.

2.3 `SlotSettingsService`: `load()`, `validate(doc) -> list[FieldError]`,
`save(doc)` (validate, `.bak`, temp + `os.replace`), `slot(n)`,
`update_slot(n, patch)`, `version_token()` (mtime+size for cheap change
detection). The CLI `config` subcommand and the web API are both ~20
lines over this.

2.4 Bridge hot reload: `Bridge` asks the service for `version_token()`
before each `Launch` and on a 5 s tick; on change it reloads and logs
"settings reloaded". Running sessions are untouched — settings apply on
the slot's next launch, and the UI says so.

2.5 Validation rules worth tests: slot numbers unique and within the
board's key count (1–3 today; PTT is key 4); `command` resolvable
(warning, not error — the binary may be installed later); `cwd` exists
or is under `~/Documents` (existing rule); effort in the vocabulary;
voice provider known and available on this OS.

Acceptance: round-trip tests, migration tests, CLI parity tests (every
`config` flag maps to a service call), bridge reload test with a fake
clock.

## Phase 3 — Settings web UI (loopback, no dependencies)

Files: `host/switchboard/ui/{server.py,index.html,app.js,styles.css}`,
`hooks_server.py` (add GET routing + `/api/*`), `cli.py`
(`switchboard settings` opens the browser), tests `test_ui_api.py`.

3.1 Routes on the existing `127.0.0.1:8877` server:

- `GET /` → the page. `GET /api/settings` → v2 document + validation
  warnings. `PUT /api/settings` → full-document save (returns field
  errors as `422`). `GET /api/families` → registry: name, display name,
  detected path/version, tier, default voice, effort vocabulary.
  `GET /api/status` → current slot records (name/status/liveness) so the
  page can show what's live. `GET /api/voice-providers` → providers and
  availability on this OS.
- Security for a loopback page: bind `127.0.0.1` only (already);
  reject requests whose `Host` isn't `127.0.0.1:8877`/`localhost:8877`
  and whose `Origin` (when present) isn't the same — this blocks
  DNS-rebinding and cross-site POSTs from random web pages; a per-run
  token generated at bridge start, embedded in the page and required as
  `X-Switchboard-Token` on mutating calls; `switchboard settings` opens
  `http://127.0.0.1:8877/?t=<token>`. No auth beyond that: it's the
  user's own machine and their own process.

3.2 The page (one screen, phone-width friendly, works in any browser):

```
Switchboard settings                              bridge: running · board: connected
┌─ Slot 1 ── key A ───────────────────────────────────────────────────┐
│ Name  [Maestro      ]   CLI [Codex ▾] ✓ found /Applications/…/codex  │
│ Folder [~/Documents/project        ] [Browse…]                       │
│ Effort [medium ▾]    Extra args [                    ]               │
│ Push-to-talk [Hotkey dictation ▾]  chord [fn] mode [toggle ▾]        │
│ Status now: working (2m)    Tier: Status (hooks + effort; voice via hotkey) │
└──────────────────────────────────────────────────────────────────────┘
… slots 2, 3 …
Defaults: folder [~/Documents] effort [medium]
[Save]  Changes apply the next time a slot launches.
```

- CLI dropdown = registry families with a detected/not-found badge, plus
  "Custom command…" which reveals the free-text command field and lands
  in the generic tier.
- "Browse…" is best-effort: on macOS shell out to `osascript choose
  folder`, Windows `System.Windows.Forms.FolderBrowserDialog` via
  PowerShell, otherwise hide the button. Handled by the `Dialog`
  adapter from the install plan if it has landed; otherwise a local
  helper.
- Voice section only shows fields relevant to the chosen provider.
- Save is whole-document with optimistic concurrency: the page sends the
  `version_token` it loaded; a mismatch (CLI edited meanwhile) returns
  `409` and the page offers reload.

3.3 Entry points: `switchboard settings` (opens browser), the success
dialog of the installer (from `auto-install-plan.md`) links to it, and
the board README.

Acceptance: API tests with the fake service; a Playwright-free smoke test
that fetches `/` and asserts the token gate; manual check on macOS in
Safari/Chrome; the page must degrade to read-only with a clear banner
when the bridge is not running (there is nothing to serve it then — the
CLI path prints "start the bridge first").

## Phase 4 — `VoiceProvider` abstraction (family-agnostic PTT)

Files: `host/switchboard/voice/{__init__,base,claude_native,hotkey,none}.py`,
`reducer.py` (`_handle_voice_start/stop` consult the slot's provider),
`bridge.py`, `families/*` (defaults), tests `test_voice.py`.

4.1 Protocol: `available() -> Availability`, `hold(ctx)`, `release(ctx)`
where `ctx` carries the slot record and the `KeyInjector`. Providers:

- `claude_native` — today's behaviour (focus tab, hold space via the
  repeating `KeyInjector`). Default for `claude`.
- `hotkey` — focus tab, then drive a system-wide dictation app's
  shortcut. `mode: "hold"` (chord held for the duration — e.g. Aqua
  Voice's push-to-talk, Wispr Flow, Superwhisper hold mode) or
  `mode: "toggle"` (tap once on hold-start, once on hold-stop — macOS
  built-in Dictation, which is toggle-only). `chord` is a small
  vocabulary (`fn`, `ctrl+space`, `cmd+shift+d`, …) mapped to keycodes
  per OS in one table. The dictation app types the transcript into the
  focused terminal, so it works for *every* family and every tier —
  including generic. This is the recommended Codex answer.
- `none` — PTT key blinks the amber hint (from `auto-install-plan.md`
  option C) with "no voice provider for this slot".
- `builtin_stt` — **deferred**: bridge records the mic and transcribes
  locally (whisper.cpp / mlx-whisper) or via an API, then types the text
  in. Big dependency and privacy surface; only worth it if `hotkey`
  proves too fiddly for coworkers. Leave a stub that reports
  unavailable.

4.2 Availability checks feed the settings UI and `doctor`: for `hotkey`
on macOS, whether a known dictation app is installed
(`/Applications/Aqua Voice.app` etc.) or built-in Dictation is enabled
(`defaults read com.apple.HIToolbox AppleDictationAutoEnable`, verify
the key during the spike); on Windows, Win+H (built-in voice typing,
toggle mode) is always present — a good default there.

4.3 `VOICE_SUPPORTED_FAMILIES` is deleted; the reducer asks the slot's
provider. Non-`claude` slots stop logging "voice is Claude-only".

4.4 Validation: with Aqua Voice (or macOS Dictation) installed on the
mini, a Codex slot with `provider: hotkey` records and lands text in the
Codex prompt on a 3 s hold. Record which apps' hold/toggle modes were
verified in the provider docstring.

Acceptance: provider tests with `FakeKeyInjector`; reducer tests per
provider; manual validation as above.

## Phase 5 — Codex parity

Files: `families/codex.py`, `hooks_install.py` (via HookSpec),
`status_table.py` rows if new statuses are needed, golden traces
`host/tests/golden/codex_*.jsonl`, `README_BRIDGE.md`.

From the Phase 0 matrix, close each gap explicitly:

5.1 **Status**: confirm which of `SessionStart`, `Notification`,
`SubagentStart/Stop` (or Codex's equivalents) exist; map any that do.
If Codex has no session-start signal, keep `launched` until the first
`UserPromptSubmit` — that is already what happens; document it. Add a
golden trace of a real Codex session (launch → prompt → tool → permission
→ stop → exit) so the reducer's Codex path is pinned the way Claude's is.

5.2 **Effort**: confirm the accepted `model_reasoning_effort` values
against the installed Codex version and remove the `xhigh/max → high`
clamp if they are accepted; otherwise keep the clamp and surface it in
the UI ("Codex caps at high"). Also confirm whether `-c` overrides
persist for the session or only for the first turn.

5.3 **Voice**: `hotkey` provider default for Codex (Phase 4). No Codex
native voice exists to wire.

5.4 **Key intents** (`plan.approve`, `review.request`, `slash.run`) are
still log-only for every family; out of scope here but the `FamilyProfile`
should expose `slash_commands()` so the future work has a home.

Acceptance: matrix rows for Codex all "verified"; golden trace test;
`doctor` reports Codex tier = Status (or Full if a dictation app is
present).

## Phase 6 — Gemini and Kimi profiles

Only after Phase 0 says what they support. Each is a `families/<name>.py`
with: executables, known paths, detect(), effort args (if any), hook
spec (if any), default voice (`hotkey`), capabilities. Expected outcome
based on today's knowledge (verify — do not assume): Gemini CLI lands in
Status or Launch-only depending on its hook support; Kimi CLI likely
Launch-only. Either way both launch, show `launched`/liveness, and get
PTT via `hotkey` with zero family-specific voice code — which is the
point of Phase 4.

Acceptance: profile tests; the settings UI lists both with honest tier
badges; README section "Which CLI each agent key launches" rewritten
around the registry and the tiers.

## Order and parallelism

0 → 1 → 2 → (3 ∥ 4) → 5 → 6. Phase 4 is the highest-value item for
Codex users and only depends on Phase 1; if time is short, do 0, 1, 4, 5
and ship the UI after.

## Decisions (made 2026-09-14 by the owner)

1. Settings UI: web page served by the bridge's loopback server. No
   menu-bar app.
2. Voice for non-Claude families: `hotkey` provider driving a system
   dictation app. `builtin_stt` stays a stub reporting unavailable.
3. Validate macOS built-in Dictation (toggle mode) first on the mini,
   then Aqua Voice (hold mode).
4. Settings v2 renames `agents` → `slots` with automatic migration; the
   hand-written `title` field is dropped and regenerated.

## Out of scope

Key-intent actions (approve/review/slash) for any family; built-in STT
implementation; per-slot model selection (the `env`/`args` fields leave
room for it); board-side settings (the firmware stays config-free).
