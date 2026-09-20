# Switchboard: diagnostic trace and support bundle

Captured: 2026-09-19. Scope: `host/switchboard/` (the Python bridge) and
the settings web UI it serves. Firmware is out of scope except where the
bridge already receives data from it.

This document is a handoff spec. Section 1 explains why; sections 3–8 are
the implementation contract; section 9 is the test plan; section 10 is the
acceptance checklist. An implementer should be able to work from sections
3 onward without re-deriving any of the reasoning.

## 1. Why

Three field bugs were reported this week and none can be diagnosed from
the current log:

| Bug | Symptom | What the log would need to show |
| --- | --- | --- |
| A | Claude CLI blocked on `/login`/OAuth shows **working** instead of needs_input | Which hook (if any) fired while the CLI was blocked, and its `notification_type` |
| B | Codex slot frees itself after ~15 min idle; the CLI is left running but untracked | Whether the free came from a `SessionEnd` hook, a confirmed-DEAD liveness probe, or something else — and what `ps` returned |
| C | Codex **needs_input** persists most of a turn during a tool chain | Timing between `PermissionRequest`, the next `PostToolUse`, and the status transitions the reducer actually applied |

The existing log (`~/Library/Logs/Switchboard/bridge.out.log`) cannot
answer any of these because:

1. **No timestamps.** `_default_log` (`bridge.py:42`) prints bare text.
   "15 minutes" cannot be measured.
2. **No state transitions.** The reducer knows the before/after status of
   every slot on every event, but only ad-hoc `Log(...)` effects reach the
   file.
3. **Drowned in noise.** In the one-week log inspected on 2026-09-19,
   179,386 of 179,734 lines were `No USB serial port found. Waiting for
   Switchboard...` (one line every ~3 s while the board is unplugged).
   The ~350 useful lines were unfindable without grep.
4. **Unbounded.** Nothing rotates `bridge.out.log`. It was 9.5 MB after
   one week of mostly-idle use.
5. **No export path.** Getting the log off a user's machine is "find the
   file in Finder and AirDrop it".

## 2. Design summary

- Add a second log, **`trace.jsonl`**: one JSON object per line, one line
  per event the bridge processes, each carrying a timestamp, the event,
  and every slot status transition it caused. Written from exactly one
  place (`Bridge.step`).
- Add a **60 s heartbeat** record with every slot's status and dwell time,
  so idle gaps are measurable.
- Add **liveness evidence**: every DEAD/UNKNOWN probe result records the
  `ps` comm list that produced it.
- **Suppress repeat noise** in `bridge.out.log` (log on transition, then a
  periodic count).
- **Rotate both files** at a fixed byte budget (15 MB total per file).
- **Redact at capture time**: the trace stores hook *metadata*
  (event name, notification type, tool name, session-id hash), never
  prompt text, tool inputs, or file paths from hook payloads. A user can
  hand the file over without reading it first.
- Add **`switchboard support-bundle`**: a CLI subcommand that zips the
  logs, `doctor` output, config, registry, versions, and installed hook
  stanzas into one file on the Desktop.
- Add a **"Download diagnostics"** button to the settings web UI that
  returns the same zip via `GET /api/support-bundle`.

`bridge.out.log` stays as the human-readable log and keeps its current
format (plus timestamps). `trace.jsonl` is for tooling and for us.

## 3. Files and where things live

| Path | Change |
| --- | --- |
| `host/switchboard/trace.py` | **New.** `TraceWriter`, record builders, redaction, rotation. |
| `host/switchboard/support_bundle.py` | **New.** `build_bundle(...) -> Path` and the pieces it collects. |
| `host/switchboard/bridge.py` | Tap `step()`; heartbeat in `tick_liveness`; timestamped `_default_log`; pass `trace` to prober. |
| `host/switchboard/liveness.py` | `ProcessProber.probe` reports comm list to the trace on DEAD/UNKNOWN. |
| `host/switchboard/hooks_server.py` | Record hook receipt (before queueing) with server-side timing; add `GET /api/support-bundle`. |
| `host/switchboard/cli.py` | `listen`: repeat-suppression for the two noisy messages; new `support-bundle` subcommand; `--trace-verbose` flag. |
| `host/switchboard/ui/api.py` | `support_bundle_get(ctx) -> (status, bytes, headers)`. |
| `host/switchboard/ui/index.html`, `app.js` | One button that fetches the zip and triggers a download. |
| `host/install_bridge_launch_agent.py` | No change to plist. `LOG_DIR` is reused. |
| `host/tests/test_trace.py`, `test_support_bundle.py` | **New.** |
| `host/tests/test_bridge.py`, `test_liveness.py`, `test_cli.py`, `test_ui_api.py` | Extend. |
| `host/README_BRIDGE.md` | New section "Diagnostics" (trace file, `support-bundle`, redaction promise). |

On-disk location for all logs: `~/Library/Logs/Switchboard/` — already the
LaunchAgent's `StandardOutPath` directory
(`install_bridge_launch_agent.py:23`). Override with `SWITCHBOARD_LOG_DIR`
for tests and foreground runs.

```
~/Library/Logs/Switchboard/
  bridge.out.log       # LaunchAgent stdout (human log) — unchanged path
  bridge.err.log       # LaunchAgent stderr — unchanged
  trace.jsonl          # new
  trace.jsonl.1        # rotated
  trace.jsonl.2
```

`bridge.out.log` is written by launchd's stdout redirection, not by the
bridge, so the bridge cannot rotate it. Handle that in
`install_bridge_launch_agent.py` by adding a `newsyslog` entry — see §8.

## 4. `trace.py`

### 4.1 Public API

```python
class TraceWriter:
    def __init__(self, path: Path, *, max_bytes: int = 5_000_000, backup_count: int = 2,
                 verbose: bool = False, clock=None) -> None: ...
    def write(self, record: dict) -> None
    def close(self) -> None

class NullTraceWriter:
    """Same interface, does nothing. The default everywhere so existing
    callers and tests are unaffected."""

def open_default(verbose: bool = False) -> TraceWriter
    """~/Library/Logs/Switchboard/trace.jsonl, honouring SWITCHBOARD_LOG_DIR."""
```

`write` must:
- add `"ts"` (ISO-8601 UTC with milliseconds, e.g. `2026-09-19T20:06:11.412Z`)
  and `"mono"` (`clock.monotonic()`, float, 3 dp) if the record lacks them;
- serialise with `json.dumps(record, separators=(",", ":"), default=str)`;
- append one line and **flush** (same reason as `_default_log`'s
  `flush=True` — under launchd nothing else will flush it);
- rotate when the file would exceed `max_bytes` (rename `.1`→`.2`,
  `trace.jsonl`→`.1`, reopen). Use `logging.handlers.RotatingFileHandler`
  with a bare `Formatter("%(message)s")` rather than reimplementing this;
- never raise. Catch `OSError` on open/write, log once via the bridge's
  human log, and degrade to no-op for the rest of the process.

Thread safety: `write` is called from the worker thread (`step`), the
ticker thread (heartbeat, probes), and hook-server threads (receipt
records). `RotatingFileHandler` already locks; do not add another lock.

### 4.2 Record schema

Every record has `ts`, `mono`, `kind`. All other keys are per-kind. Keys
are short because volume matters; values are never free text from a
hook payload.

| `kind` | When | Fields |
| --- | --- | --- |
| `start` | Bridge constructed | `version` (git short SHA or "unknown"), `port`, `pid`, `python`, `verbose` |
| `board` | `BoardEvent` stepped | `event` (name), `slot` (str or null), `liveness` (if injected by `_resolve_probes`) |
| `hook_rx` | `hooks_server.do_POST` received, before `submit` | `slot`, `event`, `session` (see §5), `bytes` (body length), `nt` (`notification_type`), `tool` (`tool_name`), `agent` (`agent_id`) — each only if present |
| `hook` | `HookEvent` stepped | `slot`, `event`, `family`, `session`, `nt`, `tool`, `agent`, `applied` (bool: did `_apply_hook_event` return True), `queue_ms` (mono at step − mono at hook_rx, via a private field on the event; see §6.3) |
| `liveness` | `LivenessObserved` stepped | `results` `{slot: "alive"\|"dead"\|"unknown"}`, `confirm` |
| `probe` | `ProcessProber.probe` returned DEAD or UNKNOWN | `slot`, `tty`, `result`, `comms` (list of str from `ps`, shell names included), `reason` (UNKNOWN only) |
| `registered` | `SlotRegistered` stepped | `slot`, `family`, `tty` (basename only) |
| `transition` | Emitted by `step()` for **each slot whose `status` differed** before vs after `reduce` | `slot`, `from`, `to`, `family`, `cause` (`"<kind>:<event>"`, e.g. `hook:PermissionRequest`, `liveness`, `board:agent.select`), `dwell_ms` (time the slot spent in `from`, from `record["status_since"]` — see §6.2) |
| `freed` | A slot record was popped by `reduce` | `slot`, `from` (last status), `cause`, `dwell_ms` |
| `effect` | Each non-`Log` effect applied | `type` (`SendUpdate`/`Launch`/`Focus`/`VoiceKey`/`CloseTab`), `slot` |
| `ack` | `agent.update.ack` cleared a pending ack | `slot`, `rtt_ms` |
| `ack_giveup` | `retry_pending_acks` gave up | `slot` |
| `heartbeat` | Every 60 s from the ticker | `port`, `slots` `{slot: {status, dwell_ms, family}}`, `pending_acks` (count), `queue_depth` |
| `serial` | Serial state changes | `state` (`connected`\|`lost`\|`searching`), `port`, `error` (str, if any) |
| `settings` | `_maybe_reload_settings` reloaded | (none) |
| `log` | Every `self.log(...)` call | `msg` — this mirrors the human log into the trace so one file has everything |
| `error` | Worker or ticker caught an exception | `where`, `type`, `msg` (exception str, not traceback) |

`slot` is always the registry key (string) or `null`.

With `verbose=True` (opt-in, §7), `hook_rx` and `hook` additionally carry
`payload`: the full parsed JSON body.

### 4.3 Redaction (`trace.redact_hook_payload(payload: dict) -> dict`)

Returns the subset of a hook payload that may be stored by default:

- `session_id` → stored as `session`: first 8 hex chars of
  `sha256(session_id)`. Enough to correlate within a bundle; not
  reversible.
- `notification_type` → `nt`, `tool_name` → `tool`, `agent_id` → `agent`,
  `hook_event_name` → ignored (we already have `event`).
- Everything else is dropped: `prompt`, `message`, `tool_input`,
  `tool_response`, `cwd`, `transcript_path`, `permission_mode`, and any
  key not listed above.

`redact_hook_payload` is the **only** function that reads a hook payload
on the trace path. Anything new must be added there and to this table.
`tool` is included because bug C depends on it and tool names are not
user content.

## 5. `bridge.py` changes

### 5.1 Constructor

Add `trace: TraceWriter | NullTraceWriter = NullTraceWriter()` keyword.
Store as `self.trace`. Emit the `start` record in `run()` (not `__init__`,
so tests constructing a Bridge don't write).

### 5.2 `_default_log`

```python
def _default_log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z {message}", flush=True)
```

Existing tests that assert on log text must match on `endswith` or strip
the prefix. `test_bridge.py` uses a captured `log` list in most places;
check for exact-equality assertions against `_default_log` output
specifically (there should be none — it prints, it doesn't return).

### 5.3 `step()` — the tap

```python
def step(self, event: Event) -> None:
    event = self._resolve_probes(event)
    self._trace_event_in(event)                       # board / hook / liveness / registered
    if isinstance(event, BoardEvent) and event.name == "agent.update.ack":
        ...existing...; self.trace.write({"kind": "ack", "slot": ..., "rtt_ms": ...})
    with self.registry.transaction() as reg:
        slots = reg.setdefault("slots", {})
        before = {k: (v.get("status"), v.get("status_since")) for k, v in slots.items()}
        new_slots, effects = reduce(...)
        reg["slots"] = new_slots
        snapshot = copy.deepcopy(new_slots)
    self._trace_transitions(before, snapshot, cause=_cause_of(event))
    for effect in effects:
        self._apply(effect, snapshot)                 # _apply emits `effect` records
```

`_trace_transitions` emits one `transition` per slot whose status changed
and one `freed` per slot present in `before` and absent in `snapshot`.
Do not emit anything when nothing changed — most `LivenessObserved` steps
are no-ops and must stay free.

`_cause_of(event)`: `board:<name>`, `hook:<name>`, `liveness`,
`registered`.

### 5.4 `status_since`

`transition.dwell_ms` needs to know when the slot entered its previous
status. `model.set_status(record, status, now)` is the single place status
is assigned (it already maintains `busy_since`). Add
`record["status_since"] = now` there, and in `slot_record` (initial
`launched`). `now` is `int(clock.now())` seconds, so `dwell_ms` is
`(now - status_since) * 1000` — second resolution is fine for dwell.

The registry is persisted JSON; adding a key is backward-compatible.
Records without `status_since` (pre-upgrade) yield `dwell_ms: null`.

### 5.5 Heartbeat

In `tick_liveness`, keep a `last_heartbeat = clock.monotonic()`; every
`HEARTBEAT_INTERVAL_S = 60.0` emit the `heartbeat` record using the
`slots` dict already read for probing. `queue_depth` is
`self._queue.qsize()`.

### 5.6 `_apply`

Emit `effect` for every branch except `Log`. `Log` → `self.log`, and
`self.log` is wrapped once in `__init__`:

```python
user_log = log
def log(message: str) -> None:
    user_log(message)
    self.trace.write({"kind": "log", "msg": message})
self.log = log
```

### 5.7 `run()` exception paths

Both `except Exception: traceback.print_exc()` sites (worker and ticker)
also emit an `error` record. Keep `print_exc()`.

### 5.8 Prober

`_make_bridge` in `cli.py` constructs `ProcessProber(log=_default_log)`.
Add `trace=` there too (§6.1).

## 6. Other module changes

### 6.1 `liveness.py`

`ProcessProber.__init__` gains `trace=NullTraceWriter()`. In `probe`, on
every DEAD or UNKNOWN return, emit a `probe` record with the parsed
`comms` list (before `_SHELL_COMMS` subtraction) and, for UNKNOWN, the
`reason` string already passed to `_log_unknown`. ALIVE emits nothing.

This is the evidence for bug B. If the slot frees because `ps` shows only
`zsh`, the CLI genuinely exited and the bug is upstream. If it shows
`codex` and the reducer still freed it, the bug is ours.

### 6.2 `hooks_server.py`

In `do_POST`, before `submit(...)`:

```python
rx_mono = time.monotonic()
trace.write({"kind": "hook_rx", "slot": slot_key, "event": event_name, "bytes": len(body),
             **redact_hook_payload(payload)})
submit(HookEvent(slot_key=slot_key, name=event_name, payload=payload, rx_mono=rx_mono))
```

`HookEvent` gains `rx_mono: float | None = None` (frozen dataclass, default
keeps all existing constructors valid). `_make_handler` / `start_hook_server`
gain a `trace=` parameter, passed from `Bridge.run()`.

Why record receipt separately from processing: the installed hook command
is `curl -s --max-time 1 ... || true` (`hooks_install.py:35`). If the
bridge's HTTP thread is slow, curl gives up after 1 s and the hook is
**silently lost**. A `hook_rx` with no matching `hook` within a few
hundred ms, or a gap where `hook_rx` should be, is the fingerprint of
that. `hook.queue_ms` measures worker backlog.

### 6.3 `cli.py`

**`listen` noise suppression.** Replace the two unconditional prints with
a small helper:

```python
class _Repeater:
    """Print a message on first occurrence and on every Nth repeat; print
    'x N' summary when a different message breaks the run."""
```

Apply to `"No USB serial port found. Waiting for Switchboard..."` and
`"Could not open {port}: ... Retrying..."`. Behaviour: first occurrence
prints; subsequent identical messages are counted; every 100th repeat
(≈5 min at the 3 s default) prints `... (still waiting, ×100)`; the next
*different* print is preceded by `(previous message repeated N times)`.
Also emit `serial` trace records on each state change only.

**`--trace-verbose`** flag on `listen` (and the root parser via
`_add_listen_flags`): sets `TraceWriter(verbose=True)`. Also honoured
from `SWITCHBOARD_TRACE_VERBOSE=1` for the LaunchAgent case where flags
are awkward.

**`support-bundle` subcommand:**

```
switchboard support-bundle [--out PATH] [--since DURATION] [--registry PATH]
                           [--agents-config PATH] [--port PORT]
```

- `--out` default: `~/Desktop/switchboard-support-<YYYYMMDD-HHMM>.zip`.
- `--since` (e.g. `2h`, `3d`): drop trace lines with `ts` older than
  now − duration. Default: everything on disk.
- Prints the resulting path on stdout, nothing else. Exit 0.
- Must work with **no bridge running** and **no board connected** — this
  is what a user runs when things are broken.

### 6.4 `support_bundle.py`

```python
def build_bundle(*, out: Path, since: timedelta | None, registry_path: Path,
                 agents_config_path: Path, port: str | None, log_dir: Path) -> Path
```

Zip members (all under a top-level `switchboard-support/` directory):

| Member | Source | Notes |
| --- | --- | --- |
| `trace.jsonl` | `log_dir/trace.jsonl*` concatenated oldest→newest | `--since` filter applied |
| `bridge.out.log` | `log_dir/bridge.out.log` | last 2 MB only (tail), since the head is usually the noise era |
| `bridge.err.log` | as is | |
| `doctor.txt` | `doctor.run_checks(...)` rendered as the CLI does | pass `port` through |
| `doctor.json` | same, JSON | |
| `agents.json` | `agents_config_path` | contains cwd paths — see redaction note |
| `agent_registry.json` | `registry_path` | contains cwd, terminal_title, tty |
| `versions.txt` | see below | |
| `hooks.claude.json` | the Switchboard-marked entries from `~/.claude/settings.json` `hooks` | reuse `hooks_install`'s marker logic to select only ours |
| `hooks.codex.json` | the Switchboard group from `$CODEX_HOME/hooks.json` | same |
| `launchagent.plist` | `~/Library/LaunchAgents/com.switchboard.bridge.plist` if present | |
| `manifest.json` | `{created, bundle_version: 1, since, members: [...]}` | |

`versions.txt` lines: `switchboard <git short SHA or "unknown">`,
`python <sys.version.split()[0]>`, `macos <platform.mac_ver()[0]>`,
`claude <output of claude --version, 3 s timeout, "not found" on error>`,
`codex <same>`, `firmware <last value the board reported, from
registry meta if we store one; else "unknown">`.

Redaction note: `agents.json` and the registry contain working-directory
paths and terminal titles. These are not hook content and are needed to
reproduce launch problems; include them. Do **not** include
`~/.claude/settings.json` or `~/.codex/config.toml` wholesale — only the
hook stanzas — since those files can contain API keys and MCP server
configs.

Every member is best-effort: a missing source file becomes a
`<name>.missing` member containing the error, never a failed bundle.

### 6.5 `ui/api.py` and the settings page

`support_bundle_get(ctx) -> tuple[int, bytes, str]` (status, zip bytes,
filename). Builds into a temp file via `build_bundle`, reads it, deletes
it. `hooks_server.do_GET` routes `/api/support-bundle` to it and responds
with `Content-Type: application/zip` and
`Content-Disposition: attachment; filename="..."`. Token check: same
`X-Switchboard-Token` rule as `PUT /api/settings` — a page in another tab
must not be able to pull the bundle. Since this is a GET the page must
`fetch` with the header and turn the blob into a download; a bare
`<a href>` will not carry the header.

`index.html`: one button, "Download diagnostics", in a new "Support"
section at the bottom. `app.js`: on click, `fetch('/api/support-bundle',
{headers: {'X-Switchboard-Token': TOKEN}})`, then `URL.createObjectURL`
→ temporary `<a download>` → click. Show the filename on success, the
error text on failure. No new dependencies.

## 7. Verbose mode

Off by default. When on (`--trace-verbose` or
`SWITCHBOARD_TRACE_VERBOSE=1`), `hook_rx`/`hook` records include the full
`payload`. The `start` record's `verbose: true` flag is how a reviewer
knows a bundle contains prompt text. `support-bundle` refuses to include
a verbose trace unless `--include-payloads` is passed, and prints a
one-line warning when it does.

Rationale: on the developer's own machine, verbose is the fastest way to
nail bugs A and C. On anyone else's machine the default must be safe to
share without reading.

## 8. Rotation of `bridge.out.log`

The bridge does not own that file descriptor — launchd does. Two options;
implement the first:

1. **`install_bridge_launch_agent.py` writes a newsyslog config** to
   `/etc/newsyslog.d/switchboard.conf` — requires sudo, which the
   installer does not currently need. Rejected.
2. **The bridge rotates it itself on startup only.** At `listen` start, if
   `log_dir/bridge.out.log` exceeds 5 MB, rename it to `bridge.out.log.1`
   (dropping any previous `.1`). launchd keeps its fd on the renamed inode
   until the next restart, so the *current* run keeps appending to `.1`
   and the fresh file starts on the next launch. Imperfect but sudo-free
   and bounds the file at ~2× the limit. **Do this one.**

With the noise suppression in §6.3 the file grows slowly enough that this
is rarely triggered anyway.

## 9. Tests

All new tests use `tmp_path` and a `FakeClock`; nothing touches
`~/Library`.

`test_trace.py`
- `write` adds `ts`/`mono`, one line per call, valid JSON, flushed.
- Rotation at `max_bytes` produces `.1`/`.2` and drops `.3`.
- `OSError` on open → subsequent writes are no-ops and the human log
  received exactly one message.
- `redact_hook_payload`: given a payload with every known Claude and Codex
  field, output contains only `session`/`nt`/`tool`/`agent`; `session` is
  8 hex chars and stable for the same input.
- Verbose writer includes `payload`; default does not.

`test_bridge.py` (extend)
- Stepping `HookEvent(UserPromptSubmit)` on an idle slot emits
  `hook` then `transition{from: idle, to: working, cause: hook:UserPromptSubmit}`
  then `effect{SendUpdate}` — in that order.
- Stepping a no-op `LivenessObserved` emits `liveness` and nothing else.
- Confirmed-dead free emits `freed` with `cause: liveness` and the
  `dwell_ms` computed from `status_since`.
- `log()` mirrors into a `log` record.
- Heartbeat fires at 60 s of fake time with the right `slots` shape.
- `_default_log` output starts with an ISO timestamp.

`test_liveness.py` (extend)
- DEAD with `comms == {"zsh"}` emits `probe{result: dead, comms: ["zsh"]}`.
- UNKNOWN on `ps` timeout emits `probe{result: unknown, reason: ...}`.
- ALIVE emits nothing.

`test_cli.py` (extend)
- `_Repeater`: 250 identical messages → prints at 1, 100, 200, plus a
  summary line when a different message arrives.
- `support-bundle --out tmp` with a fabricated `log_dir`, registry, and
  agents config produces a zip with every member in §6.4; missing sources
  become `.missing` members; `--since 1h` drops old trace lines.
- `support-bundle` succeeds when `claude`/`codex` are not on PATH.

`test_ui_api.py` (extend)
- `GET /api/support-bundle` without token → 403; with token → 200,
  `application/zip`, body starts with `PK`.

`test_golden.py`: if the golden harness replays events through `Bridge`,
add one golden trace for the existing sample event file so schema drift
is caught. Skip if the harness doesn't fit; don't rebuild it for this.

## 10. Acceptance

Each of these is a `jq` one-liner against a bundle's `trace.jsonl`. They
are the questions the three reported bugs need answered, and the spec is
done when each returns the right shape on a real capture.

**Bug A — Claude auth shows working.** Reproduce by launching a slot with
an expired login. Then:

```sh
jq -c 'select(.kind=="hook_rx" or .kind=="hook") | {ts,event,nt,slot}' trace.jsonl
```

must show every hook Claude sent while blocked, with its
`notification_type`. If a `Notification` arrives with an `nt` not in
`NEEDS_INPUT_NOTIFICATION_TYPES`, that is the fix (add it in
`status_table.py`). If nothing arrives, the fix is a dwell-based
`working → waiting` fallback and the heartbeat's `dwell_ms` is the input.

**Bug B — Codex slot frees after idle.** Leave a Codex slot idle 20 min.
Then:

```sh
jq -c 'select(.kind=="freed" or .kind=="probe" or .kind=="heartbeat") | {ts,kind,slot,cause,comms,result}' trace.jsonl
```

must show, in order, heartbeats with the slot's growing `dwell_ms`, then
either `probe{result:dead, comms:[...]}` ×2 followed by `freed{cause:
liveness}` (the CLI exited; investigate Codex), or a
`hook{event:SessionEnd}` followed by `freed{cause: hook:SessionEnd}`
(Codex ended the session; investigate why), or `freed` with a `probe`
showing `codex` still in `comms` (our bug).

**Bug C — Codex needs_input lingers.** Run a Codex turn that triggers an
approval followed by more tool calls. Then:

```sh
jq -c 'select(.slot=="2" and (.kind=="hook_rx" or .kind=="transition")) | {ts,kind,event,from,to,tool,queue_ms}' trace.jsonl
```

must show `PermissionRequest` → `transition{to: needs_input}`, then the
`PostToolUse` that should have cleared it and its `transition{to:
working}` with timestamps. If `PostToolUse` `hook_rx` is absent, Codex
did not send it (or curl timed out — check `queue_ms` on neighbours). If
present but no transition followed, the reducer dropped it (check
`applied` and `session` mismatch).

**Hygiene**
- After 24 h with the board unplugged, `bridge.out.log` gains fewer than
  200 lines.
- `trace.jsonl*` never exceeds 15 MB total.
- `support-bundle` completes in under 10 s with no bridge running.
- A default-mode bundle contains no string from any prompt typed into a
  CLI during capture (grep for a sentinel prompt).

## 11. Out of scope

- Fixing bugs A/B/C themselves. This spec produces the evidence; the
  fixes are separate changes.
- Remote upload of bundles. The user shares the zip manually.
- Firmware-side logging. If the board later reports a version string in
  its handshake, `versions.txt` should pick it up, but do not add a
  protocol change for it here.
- Structured logging for `doctor` — it already has `--json`.

## 12. Suggested order

1. `trace.py` + tests. Pure, no integration risk.
2. `bridge.py` tap, `status_since`, heartbeat, timestamped `_default_log`,
   `log` mirroring. Run the whole suite; expect a handful of log-text
   assertions to need the timestamp prefix tolerated.
3. `liveness.py` and `hooks_server.py` records.
4. `cli.py` noise suppression and `bridge.out.log` startup rotation.
5. `support_bundle.py` + `support-bundle` subcommand.
6. UI button.
7. `README_BRIDGE.md` "Diagnostics" section.

Steps 1–3 alone are enough to capture all three bugs; if time is short,
ship those first and export by AirDrop.
