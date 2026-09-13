# Switchboard: state bugs, design review, and testing plan

Captured: 2026-09-13. Scope: `host/switchboard_bridge.py` (1,649 lines) and
`firmware/neokey/switchboard_neokey.ino` (262 lines). The ESP-IDF display
firmware is out of scope for this pass.

## 1. What the symptoms actually are

Every symptom reported today traces to a small number of concrete causes.
Line numbers refer to the current `main` (`c5e66c6`).

### 1.1 "New session flashes blue, then goes white"

Not a race. It is the designed status mapping:

| Step | Bridge status | Firmware color |
| --- | --- | --- |
| Key pressed, `launch_agent` writes the record | `launched` (`slot_record`, bridge:187) | `0x0000FF` blue (`colorForStatus`, ino:56) |
| Claude's `SessionStart` hook fires ~1 s later | `idle` (`CLAUDE_HOOK_STATUS`, bridge:684) | `0x202020` dim white (ino:57) |

Fix: make `launched` and `idle` render the same soft white. `launched` can
keep its own semantic in the registry (useful for the "hook never arrived"
diagnosis) but the LED should not distinguish them.

### 1.2 "Busy color should go soft white → blue → magenta over the session"

The current ramp (ino:31-36) is blue 210° → 380° (wraps to ember orange 20°)
over 300 s, smoothstep-eased. Two problems:

1. It starts at saturated blue, not white, so there is no visual continuity
   from idle into working.
2. Because the end hue is 380°, the sweep **passes through pure red (360°/0°)
   at t≈0.88, i.e. ~220 s into a turn**. A long turn looks exactly like
   `blocked` for the last minute and a half. The comment on ino:24 says the
   long route was chosen to avoid colliding with green/yellow — it collides
   with red instead.

Proposed ramp: end at magenta (300°) and never cross 360°. Saturation ramps
0 → 1 across the first third so the key *warms up* out of white, then hue
slides 210° → 300°.

```text
t=0      soft white  (h=210, s=0.0, v=0.55)
t=0.33   blue        (h=210, s=1.0, v=1.0)
t=1.0    magenta     (h=300, s=1.0, v=1.0)
```

Also: `busy_seconds` is derived from `last_updated_at` (bridge:376-378),
which is bumped on *any* field change, including an effort-dial change
(`update_registry_slot_status`, bridge:362) and `needs_input → working`.
The ramp silently restarts on those. Store an explicit `busy_since` set only
on the transition *into* a busy status.

And: the firmware only advances the ramp when a message arrives. The poller
re-pushes every busy slot every 2 s purely to tick the clock (bridge:642-648).
Instead send `busy_since` once and let the firmware compute elapsed time from
`millis()`. Fewer serial writes, smoother ramp, and the ramp becomes a pure
function of `(status, elapsed_ms)` — trivially testable.

### 1.3 "A key is lit but there is no CLI behind it"

Three independent causes, all confirmed in code and two observed live today.

**(a) Liveness means "Terminal tab exists", not "agent process exists".**
`find_terminal_tab` (bridge:1127) asks AppleScript whether any tab has that
tty. A tab showing `[Process completed]` still counts as alive. So when a
session ends without `SessionEnd` being delivered (crash, Ctrl-C twice,
hook `curl` killed by SIGHUP, bridge not running at that moment, port 8877
busy), the record survives with its last status and the LED stays lit.
Re-pressing the key runs `focus_terminal_tab` (bridge:445-448) — that is the
"pressing again brings the window up" you saw.

**(b) The liveness check fails unsafe.** `find_terminal_tab` returns `False`
for *any* osascript failure — an Apple Event timeout while Terminal is busy
running `do script`, an automation-permission prompt, Terminal being hung
on a dialog. A transient error is treated as "dead": the record is popped
and, with `--auto-launch`, **a second session is launched into a slot that
already has a live one**. That is the 4th CLI you got on a 3-key board.
`bridge.out.log` shows it directly — consecutive `Agent 1 was empty, bridge
launched it` lines with no `Selected` line between them. The poller thread
does the same check every 2 s for every slot, concurrently with the main
loop, so two osascript calls hit Terminal at once.

**(c) Once duplicated, the bridge cannot recover.** Both sessions carry
`SWITCHBOARD_SLOT=1`, so both post hooks into the same slot (flicker). When
you close *either* one, its `SessionEnd` pops the slot record — which now
describes the *other*, still-running session. Right now the registry is
`{"slots": {}}` while the original slot-1 session (ttys000, 13:49) is still
running with its tab open. Hook payloads include `session_id`; the bridge
should record it at `SessionStart` and ignore hook events whose
`session_id` doesn't match the slot's current owner.

### 1.4 Registry races (the "state changes hitting race conditions" feeling)

The registry is a JSON file with unsynchronized read-modify-write from three
kinds of threads: the serial loop (`describe_event`), the poller
(`poll_slot_logs`), and one thread per hook POST (`ThreadingHTTPServer`;
`PostToolUse` fires after every tool call, so this is *busy*).

- Lost updates: thread A loads, thread B pops a slot and saves, thread A
  saves its stale copy → the freed slot is resurrected.
- Torn reads: `save_registry` opens with `"w"` (truncate) then dumps
  (bridge:139-143). A concurrent `load_registry` can read an empty or
  partial file and raise `JSONDecodeError`. `poll_slot_logs` has no
  try/except, so that exception **kills the poller thread silently for the
  rest of the process lifetime** — after which tab-close detection never
  runs again. The main loop would crash outright (launchd restarts it).
- `_DEVICE_WRITE_LOCK` (commit `c5e66c6`) fixed interleaved *serial* writes
  but the same class of bug exists one layer up, on the registry.

## 2. Design review (SOLID / DRY / testability)

### Single Responsibility

`switchboard_bridge.py` is one module doing eight jobs: serial I/O, registry
persistence, status state machine, HTTP hook server, hook *installer*,
AppleScript/Terminal automation, Quartz key posting, and a 12-subcommand CLI.
`describe_event` (bridge:427-563) is a 136-line `if name == ...` chain that
mutates in-memory state, mutates the registry, launches terminals, focuses
windows, posts key events, and writes to the device. `_apply_hook_event` is a
state machine embedded in an HTTP handler.

### Open/Closed

Adding a status means editing `STATUS_CHOICES`, `BUSY_STATUSES`,
`CLAUDE_HOOK_STATUS`, `CODEX_HOOK_STATUS`, `colorForStatus` (C++), the
`isBusy` string compare in `pollIncomingSerial` (ino:172), and the design doc
table. The firmware and host each hard-code the busy set separately.

### Dependency Inversion (the testability blocker)

Every side effect is a module-level free function calling `subprocess.run`,
`os.write`, `time.time()`, or `Quartz` directly, so nothing can be tested
without monkeypatching module globals. There is no seam for a clock, a
terminal driver, or a device link. `time.time()` is called in six places.

### DRY

- `build_parser` declares the same 12 arguments on the root parser and again
  on `listen` (bridge:1546-1573). A parent parser removes the duplication.
- `sync_device` re-implements the line-splitting from `serial_lines`
  (bridge:1421-1437).
- `launch_all` duplicates `launch_slot_from_config` (and drops `effort`).
- `find_terminal_tab`, `focus_terminal_tab`, `restart_in_slot_terminal`, and
  `test_trigger` each contain the same AppleScript tab-search loop.
- `BridgeState.effort_by_slot` and `selected_agent` mirror registry fields
  and are never read back.
- `LIVE_EFFORT_RESTART_ENABLED = False` gates ~15 lines of dead code plus
  `restart_in_slot_terminal`; keep the lesson as a comment, delete the code.

### What is good and should be kept

- The exclusive-open on the serial port (`TIOCEXCL`) and the reasoning
  comments around it.
- The hook-marker approach to idempotent settings merging.
- Subagent gating of `done` (`active_subagents` / `main_stopped`) — the
  logic is right; it just needs a pure home and a table of tests.
- The `shell`/`cat` zero-cost test slot and `test-trigger`.
- Comments generally explain *why*, including failed experiments. Keep that.

## 3. Target architecture

### 3.1 One owner for state, one event queue

All inputs become typed events on a single `queue.Queue`; one worker thread
owns the registry and the device link. The serial reader, the hook server,
and the liveness ticker only *enqueue*. This removes the entire class of
registry races without adding locks to every path.

```text
serial reader ──┐
hook HTTP ──────┼──▶ Queue[Event] ──▶ Bridge.step(event)
liveness tick ──┘                        │
                                         ├─ reducer (pure): (slots, event, now) → (slots', effects)
                                         ├─ Registry.save (atomic)
                                         └─ effects: DeviceLink.send / Terminal.launch / focus
```

The reducer is a pure function of `(state, event, now)`. Every bug in §1 can
then be written as a five-line table test.

### 3.2 Module split

```text
host/switchboard/
  model.py          Status enum, SlotRecord dataclass, BUSY set, effort normalization
  events.py         typed events: KeyPressed, HookEvent(session_id, family, name, payload),
                    LivenessChanged, Tick, DeviceAck
  reducer.py        pure transitions incl. subagent gating, busy_since, session_id guard
  registry.py       Registry: load, atomic save (tmp + os.replace), fcntl.flock for the
                    out-of-process `status`/`clear` CLI writers
  clock.py          Clock protocol; SystemClock; FakeClock for tests
  device.py         DeviceLink protocol; SerialDevice (exclusive open, locked writes,
                    line reader); FakeDevice records sent lines
  terminal.py       TerminalDriver protocol; AppleScriptTerminal; process-based liveness
  launcher.py       resolve_command, effort args, terminal_command builder
  hooks_server.py   HTTP → HookEvent → queue (no state access)
  hooks_install.py  settings.json / hooks.json merge
  bridge.py         Bridge: queue loop, effects executor, liveness ticker
  cli.py            argparse; parent parser for shared args
host/switchboard_bridge.py   thin shim: `from switchboard.cli import main`
host/tests/
```

### 3.3 Liveness, done properly

- Track the agent process, not the tab. `ps -o pid=,comm= -t <tty>` lists
  `script` (or the agent binary) while the session is alive, and no
  AppleScript is involved — so no Terminal.app side-launch, no permission
  prompt, no Apple Event timeout.
- Distinguish `ALIVE / DEAD / UNKNOWN`. Only `DEAD` (confirmed, e.g. two
  consecutive checks 2 s apart) frees a slot. `UNKNOWN` (tool error) logs
  and leaves the record alone.
- Record `session_id` from the `SessionStart` payload; drop hook events with
  a mismatched `session_id`. Record the `script` PID at launch as a second
  identity key.
- Never auto-launch into a slot whose record is `UNKNOWN`; re-press once it
  resolves. (A key press on an `UNKNOWN` slot should push a distinct LED
  pattern — see §5.)

### 3.4 Firmware

- Move color math into `led_model.h` as pure functions of
  `(status, elapsed_ms, selected, now_ms)` so it can be compiled and tested
  on the host with a 30-line `main()`.
- Accept `busy_since_ms` (host wall-clock offset converted to device-relative
  on receipt) and compute the ramp locally.
- Emit `agent.update.ack` — `sync` already waits for it and currently always
  reports "no acknowledgement" on the NeoKey build.
- Replace the `strcmp` chains with a `status_from_string` → enum lookup shared
  with a generated table (see §4.4).

## 4. Testing plan

Today there are zero automated tests (the `work/*_registry.json` files are
residue of manual runs). Python 3.14 is on this Mac; `pytest` is not in the
venv yet.

### 4.1 Unit (pytest, no hardware, < 2 s total)

| Area | Tests |
| --- | --- |
| `reducer` | table-driven: every `(family, hook event)` → status; `Stop` with active subagents defers; `SubagentStop` delivers deferred done; duplicate `SubagentStart` ignored; new `UserPromptSubmit` clears `main_stopped`; mismatched `session_id` ignored; `busy_since` set only on idle→busy; effort change does not touch `busy_since` |
| `reducer` liveness | `DEAD` frees + emits empty update; `UNKNOWN` is a no-op; `ALIVE` after `UNKNOWN` restores; key press on `UNKNOWN` never launches |
| `registry` | atomic save leaves no torn file under concurrent readers (spawn 20 threads, 500 iterations, every load parses); `flock` serializes an external `status` CLI write |
| `launcher` | `effort_args` for claude/codex/shell incl. `xhigh`/`max` clamping; `terminal_command` output is a golden string; `resolve_command` fallback order |
| `hooks_install` | idempotent (second run `unchanged`); preserves foreign hook groups; replaces only marker-tagged groups |
| `events` | line parsing: non-JSON, non-object, unknown event, malformed UTF-8 |
| firmware model | `hsvToRgb` reference vectors; ramp never yields hue in the red band (330°–30°) for any `elapsed`; `t=0` is white; `t=1` is magenta; `applySelection` never zeros a non-zero color |

### 4.2 Integration (no hardware, seconds)

- **Fake device via pty pair.** `os.openpty()` gives the bridge a real fd to
  read/write; the test writes board events into the other end and asserts
  exact `agent.update` lines back, with a `FakeClock`.
- **Real hook server on an ephemeral port**, driven by `urllib` POSTs with
  captured real payloads (record one real Claude session's hook bodies into
  `tests/fixtures/hooks/*.json`). Asserts the device line sequence.
- **Golden traces.** A `.jsonl` of interleaved key presses, hook events, and
  clock ticks → expected `.jsonl` of device output. New bugs become new
  trace files. This is the determinism anchor: same input, same output,
  every time, with no sleeps.
- **Race regression.** 3 slots × 50 hook POSTs from 8 threads while the
  liveness ticker fires; assert no slot ever resurrects after `SessionEnd`
  and the registry parses on every read.
- **Duplicate-launch regression.** Liveness returns `UNKNOWN` for one tick;
  assert zero launches.

### 4.3 Smoke (opt-in, `pytest -m hardware`, and a `doctor` command)

- `switchboard_bridge.py doctor`: port found and exclusively openable, hook
  server bound on 8877, hooks installed in both CLIs, Terminal automation
  and Accessibility permissions granted (probe, don't guess), registry
  consistent (each slot's process alive), `agents.json` cwd paths exist.
- Board round-trip: send `agent.update` for each status, expect
  `agent.update.ack` within 1 s.
- `--stdin` injection of the sample event file against a real board with
  `--no-open` (no terminals, no API spend).
- Firmware compile check with `arduino-cli compile --fqbn esp32:esp32:esp32s3`
  in CI, so a bad `.ino` cannot be merged.

### 4.4 Shared status table

Generate `CLAUDE_HOOK_STATUS`, the firmware `Status` enum, and the color
table from one `status_table.json` (or a Python module that emits the C
header). One source of truth; the unit tests check the generated header is
current.

### 4.5 CI

GitHub Actions, `macos-latest`: `pip install -e host[test] && pytest -m "not
hardware"`, plus `arduino-cli` compile of `firmware/neokey`. `idf.py build`
for the display firmware can come later; it needs the toolchain cached.

## 5. User-facing improvements

1. **Colors** (your spec): soft white on launch and idle; busy ramps white →
   blue → magenta; never red unless `blocked`. `needs_input` becomes a yellow
   *fast pulse* (design doc says flash; firmware is solid today) so it is
   distinguishable from `waiting`. `done` green holds until the key is
   pressed or the next prompt, then returns to soft white.
2. **Selection visibility.** Non-selected keys are dimmed to 1/4 (ino:96);
   at 0x202020 that is `0x080808`, effectively off at brightness 40. Use a
   brightness floor, or show selection as a brief white blink on press.
3. **Unknown state on the LED.** When liveness is `UNKNOWN` or the last
   `agent.update` had no ack, show a slow amber pulse instead of holding a
   stale color. Stale-but-confident is what caused today's confusion.
4. **Default working directory.** Today every slot defaults to the repo
   (`agent_config_for_slot`, bridge:224; `agents.example.json`; `setup.sh`
   rewrites `cwd` to the repo). Proposed:
   - `agents.json` gets a top-level `"defaults": {"cwd": "~/Documents/Switchboard"}`;
     per-slot `cwd` overrides it; `~` expands.
   - `setup.sh` creates that folder and points the template at it instead
     of the repo.
   - `switchboard_bridge.py config --slot 2 --cwd ~/Projects/foo` edits
     `agents.json` without hand-editing JSON.
   - On launch, if the cwd is missing: create it if it is under `~/Documents`,
     otherwise refuse with a clear message and a distinct LED blink.
   - Later: a `projects` list in `agents.json` and a key-hold gesture to cycle
     which project the slot opens in; the screen shows the folder name.
5. **Ghost-tab cleanup.** When a slot is freed and its tab still exists with
   no agent process, close the tab (opt-in flag) so Terminal doesn't fill
   with `[Process completed]` windows.
6. **Log rotation.** `host/logs/slot-1.log` is 213 KB after one session and
   `read_log_tail` scans it every 2 s. Truncate on launch (already done) and
   cap at a few MB, or drop the log tailing entirely now that hooks are the
   primary signal — the only remaining pattern is `(y/n)`, which Claude
   Code's hooks already cover via `Notification`.
7. **Bridge self-recovery.** On startup, reconcile the registry against
   live processes (free dead slots, push a full sync to the board) so a
   restart is always a clean state rather than inheriting a stale file.

## 6. Phasing

**Phase 0 — stop the bleeding (small diffs, this week)**
- Firmware: `launched` → soft white; new ramp (white → blue → magenta, cap at
  300°); `busy_since` local timing; ack event.
- Bridge: process-based liveness with `ALIVE/DEAD/UNKNOWN`; never free or
  launch on `UNKNOWN`; `session_id` guard; atomic registry save + one
  process-wide registry lock; try/except + log in the poller; `busy_since`.
- Tests: `pytest` scaffold; characterization tests for the reducer and the
  firmware color math *before* changing them, then the new expectations.
- Registry reconcile on bridge start.

**Phase 1 — restructure (one to two weeks)**
- Package split (§3.2), event queue + single owner, `Clock`/`DeviceLink`/
  `TerminalDriver` seams, fake implementations.
- Golden-trace integration tests, race regression, duplicate-launch
  regression. CI.
- Delete `LIVE_EFFORT_RESTART_ENABLED` dead code; dedupe AppleScript; parent
  parser.

**Phase 2 — product polish**
- `doctor`; default-cwd config and `config` command; done-fade;
  `needs_input` pulse; unknown-state amber; ghost-tab cleanup; log cap.
- Shared status table generating the C header.

## 7. Decisions (resolved 2026-09-13)

- Ramp length measures the current **turn**; it resets on every
  `UserPromptSubmit`. 300 s stays the initial ramp length.
- **Log tailing is deleted** (`script -q`, `host/logs`, pattern matching,
  `test-trigger`). Hooks are the only status source.
- Default agent cwd is **`~/Documents`** (every machine has one).

Implementation handoff: `docs/design/phase0-implementation-spec.md` — **Phase
0 is implemented** (all 10 steps landed, 52 host tests + a host-side
firmware test passing). All three decisions above are live in
`host/switchboard_bridge.py` and `firmware/neokey/`, not just planned.
