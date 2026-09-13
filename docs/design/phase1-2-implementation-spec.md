# Switchboard Phase 1 + 2 — implementation spec (handoff)

Status: ready to implement. Companion to `state-and-testing-plan.md` (the
*why*, §3 and §6) and successor to `phase0-implementation-spec.md` (Phase 0
is merged: `79dcfd3`, 52 host tests + firmware host test green). Work
through the steps in order; each step ends with a green test run and its own
commit.

Decisions already made (do not reopen):

- Sketch fix is a **file rename**, not a directory rename (§1.0).
- The bridge keeps the registry **on disk** as the source of truth. Phase 1
  makes one thread the only in-process writer; `flock` stays for the
  out-of-process CLI (`status`, `clear`, `launch`).
- `switchboard_bridge.py` remains the entry point (LaunchAgent plist and
  `setup.sh` point at it). It becomes a two-line shim.
- Public CLI surface is unchanged (same subcommands, same flags) except for
  the additions listed in Phase 2. `install_bridge_launch_agent.py` is not
  touched.

## 0. Ground rules for the implementer

- Python 3.14, stdlib only (pyobjc stays optional). Tests:
  `host/.venv/bin/python -m pytest host/tests -q`. Every step must leave
  this green; the count only goes up.
- The bridge runs as a LaunchAgent on this Mac. After editing, restart it
  with `launchctl kickstart -k gui/$(id -u)/com.switchboard.bridge`. Never
  run `clear` or delete `host/agent_registry.json` casually — live sessions
  (possibly the one you are in) are registered there.
- Commit after each numbered step. Message format: `Phase 1.N: <summary>` /
  `Phase 2.N: <summary>`.
- Firmware compiles with
  `arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc firmware/neokey`
  (works only after §1.0). Do **not** flash the board unless the user asks.
- Preserve the "why" comments in the existing code when moving it. Losing
  the explanations of failed experiments (seesaw reset, `/voice` toggle,
  `TIOCEXCL`, interleaved serial writes) is a regression.
- When moving a function, `git mv`-style moves are impossible across
  modules; instead move it verbatim first, run tests, then refactor. Do not
  rewrite and move in the same commit.

## 1. Phase 1 — restructure

### 1.0 Sketch name fix (step 1.0)

Arduino requires `<dir>/<dir>.ino`. Today `firmware/neokey/` holds
`switchboard_neokey.ino`, so `arduino-cli compile firmware/neokey` fails
with `main file missing from sketch: .../firmware/neokey/neokey.ino`
(verified 2026-09-13). The directory name `firmware/neokey/` is referenced
in ~12 places (design docs, `run.sh`, hardware READMEs, `README_BRIDGE.md`);
the file basename in 5. Rename the file.

1. `git mv firmware/neokey/switchboard_neokey.ino firmware/neokey/neokey.ino`
2. Update every reference to `switchboard_neokey.ino`:
   - `firmware/neokey/neokey.ino` header comment (if it names itself)
   - `firmware/msc_cdc_spike/README.md:5`
   - `firmware/msc_cdc_spike/msc_cdc_spike.ino:5,9`
   - `hardware/kicad/README.md:14`
   - `docs/design/state-and-testing-plan.md` (scope line at top)
3. `codex_micro_neokey.ino` appears in `plug-and-play-installer-plan.md`
   and `4key-business-plan.md`. Those are dated narrative of what was
   flashed at the time; **leave them**, but add one line under the
   installer plan's "Spike findings" heading: *"(`codex_micro_neokey.ino`
   is the pre-rename name of `firmware/neokey/neokey.ino`.)"*
4. Add `firmware/neokey/test/compile.sh`:

   ```sh
   #!/usr/bin/env bash
   # Compile-only check (no upload). Needs: arduino-cli, esp32 core,
   # "Adafruit seesaw Library", "ArduinoJson".
   set -euo pipefail
   cd "$(dirname "${BASH_SOURCE[0]}")/.."
   arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc --warnings default .
   ```

   Make it executable; run it; it must exit 0. Mention it next to `run.sh`
   in `firmware/neokey/README.md`.
5. Acceptance: `grep -rn switchboard_neokey . --exclude-dir=.git` returns
   only lines inside `docs/design/phase*` specs (historical). `compile.sh`
   and `run.sh` both exit 0.

### 1.1 Package skeleton + pure modules (step 1.1)

Create `host/switchboard/` and move the **pure** code first — nothing that
touches I/O. `switchboard_bridge.py` imports these back (`from
switchboard.model import *`-style explicit names) so all existing tests keep
passing untouched.

```text
host/switchboard/
  __init__.py       __version__ = "1.0"; nothing else
  model.py          STATUS_CHOICES, BUSY_STATUSES, EFFORT_* tables, normalize_effort,
                    effort_args, command_with_effort, slot_record, agent_update_event,
                    _set_status → set_status (public), CLAUDE_HOOK_STATUS, CODEX_HOOK_STATUS,
                    CODEX_HOOK_EVENTS, hook_status_for
  events.py         see below
  clock.py          Clock protocol, SystemClock, FakeClock, now() helper is REMOVED —
                    every caller receives a clock (see §1.3)
```

`events.py`:

```python
@dataclass(frozen=True)
class BoardEvent:            # a JSON line from the device, already parsed
    name: str                # "agent.select", "agent.focus", "agent.reasoning.apply",
                             # "voice.hold.start", "voice.hold.stop", "agent.update.ack", ...
    payload: dict

@dataclass(frozen=True)
class HookEvent:             # one POST from a CLI lifecycle hook
    slot_key: str
    name: str                # "SessionStart", "PostToolUse", ...
    payload: dict            # parsed JSON body ({} if unparseable)

@dataclass(frozen=True)
class LivenessObserved:      # produced by the ticker AFTER probing; the reducer never probes
    results: dict[str, Liveness]     # slot_key -> ALIVE/DEAD/UNKNOWN
    confirm: bool = True             # False at startup (single DEAD frees)

@dataclass(frozen=True)
class SlotRegistered:        # a launch completed (or an external `launch` CLI ran); record is final
    record: dict

@dataclass(frozen=True)
class Shutdown: ...

Event = BoardEvent | HookEvent | LivenessObserved | SlotRegistered | Shutdown

def parse_board_line(line: str) -> BoardEvent | str | None:
    """None for non-JSON noise, a str diagnostic for malformed/non-object JSON, else BoardEvent."""
```

`Liveness` enum moves to `model.py` (it is data, used by events and reducer).

Tests: `host/tests/test_events.py` — `parse_board_line` for: blank line,
firmware log text, `{"event":"agent.select","slot":1}`, `[1,2]`, `{bad`,
invalid UTF-8 already decoded with `errors="replace"`. Move the effort/
launcher-arg tests that currently live in `test_config.py` to
`test_model.py` if any target `effort_args`; otherwise add
`test_effort_args_clamps_codex_xhigh_to_high` and
`test_command_with_effort_shell_family_unchanged`.

### 1.2 Reducer (step 1.2)

`host/switchboard/reducer.py` — a pure function. No imports of `os`,
`subprocess`, `time`, `json` file I/O, or anything from `terminal`/`device`.

```python
@dataclass
class Effect: ...
@dataclass(frozen=True) class SendUpdate(Effect): slot_key: str          # push agent.update (record or empty)
@dataclass(frozen=True) class Launch(Effect): slot: int                  # open a terminal for this slot
@dataclass(frozen=True) class Focus(Effect): tty: str
@dataclass(frozen=True) class VoiceKey(Effect): down: bool
@dataclass(frozen=True) class Log(Effect): message: str

@dataclass
class ReducerState:          # in-memory, NOT persisted; owned by Bridge
    selected_slot: int | None = None
    mic_active: bool = False
    dead_probes: dict[str, int] = field(default_factory=dict)

def reduce(slots: dict[str, dict], state: ReducerState, event: Event, now: int,
           *, auto_launch: bool) -> tuple[dict[str, dict], list[Effect]]:
```

`slots` is `registry["slots"]`. The function may mutate and return the same
dict (simplest) — but it must never touch anything else. Every branch of
today's `describe_event` and `_HookRequestHandler._apply_hook_event`
becomes a branch here, and the string it used to `return` becomes a
`Log(...)` effect. Rules to carry over exactly (they are all tested today
in `test_hook_reducer.py` / `test_liveness.py`; port those tests to call
`reduce` directly):

- `session_id` guard, subagent gating, `busy_since` on enter-busy and on
  every `UserPromptSubmit`, `main_stopped` clearing, `SessionEnd` pops.
- `agent.select`: the probe result is **not** available to the reducer.
  The Bridge resolves it first and enqueues the select as
  `BoardEvent("agent.select", {"slot": 1, "liveness": "alive"})` — i.e.
  the worker probes the existing record (if any) and injects the result
  into the payload before reducing. The reducer then applies the Phase 0
  rules: ALIVE → `Focus` + `SendUpdate`; UNKNOWN → `Focus` (if tty) +
  `SendUpdate` + `Log("liveness unknown, not relaunching")`; DEAD →
  bump `dead_probes`, free only when confirmed, then fall through to
  `Launch` if `auto_launch`; no record + `auto_launch` → `Launch`.
- `LivenessObserved`: for each `(slot_key, result)`: ALIVE/UNKNOWN reset
  `dead_probes[slot_key]`; DEAD increments and frees on the 2nd (or 1st if
  `confirm=False`); every freed slot yields `SendUpdate`.
- `SlotRegistered(record)`: `slots[str(record["slot"])] = record`,
  `SendUpdate`.
- `agent.reasoning.apply`: set `effort` only (never `status`/`busy_since`),
  `SendUpdate`. **Delete** `LIVE_EFFORT_RESTART_ENABLED`, the dead branch,
  and `restart_in_slot_terminal`. Keep a 3-line comment stating the lesson
  (restarting a live CLI to change effort lost the session; effort applies
  on next launch).
- `voice.hold.start/stop`: exactly today's checks (family in
  `VOICE_SUPPORTED_FAMILIES`, tty present, liveness — the Bridge injects
  `"liveness"` into the payload for these too), emitting `Focus` +
  `VoiceKey(True)` / `VoiceKey(False)` and updating `state.mic_active`.
  Keep the `/voice`-toggle comment verbatim.
- `agent.update.ack` → `Log`. (Phase 2 makes it do more.)
- `plan.approve`, `review.request`, `slash.run`, unknown → `Log`.

Drop `BridgeState.selected_agent` and `effort_by_slot` (never read).

Tests — `host/tests/test_reducer.py` (this **replaces**
`test_hook_reducer.py`; delete the old file in the same commit):

- Every row from the Phase 0 `test_hook_reducer.py` table, rewritten as
  `reduce(slots, state, HookEvent("1", name, payload), now, auto_launch=False)`.
- Every row from `test_liveness.py` that is about *freeing rules* (two
  consecutive DEAD, UNKNOWN resets, startup `confirm=False`), rewritten as
  `LivenessObserved`. The rows about `probe_liveness` parsing `ps` output
  stay in `test_liveness.py` (that is I/O-adjacent and lives in
  `liveness.py`, §1.3).
- New: `test_reduce_never_performs_io` — monkeypatch `subprocess.run`,
  `os.write`, `builtins.open` to raise, run every event kind once, assert
  no raise.
- New: `test_select_on_unknown_emits_no_launch`,
  `test_select_on_dead_first_press_does_not_free`,
  `test_select_on_dead_second_press_frees_and_launches`,
  `test_select_with_auto_launch_off_only_sends_update`.
- New: `test_effects_order_focus_before_send_update` (LED and window should
  react in the order the user perceives them).

### 1.3 I/O seams (step 1.3)

Move the side-effecting code behind small protocol classes. Each has one
real implementation and one fake; the fakes live in the package (not in
tests) so `--dry-run` / `--no-open` / `--stdin` modes can use them too.

```text
host/switchboard/
  clock.py        (already created in 1.1)
  registry.py     Registry(path): load(), save(), transaction() — the Phase 0 code, as methods.
                  Keep DEFAULT_REGISTRY resolution in cli.py, not here.
  device.py       DeviceLink protocol: send(event: dict) -> None; lines() -> Iterator[str]; close()
                  SerialDevice(port, baud): Phase 0 open_serial/configure_serial/serial_lines +
                    the _DEVICE_WRITE_LOCK (keep its comment); TIOCEXCL stays.
                  FdDevice(read_fd, write_fd | None): used by the pty integration test and by
                    --stdin (write_fd=None → send() is a no-op).
                  FileDevice(handle): --sample; send() no-op.
                  FakeDevice(): sent: list[dict]; feed(line: str); lines() yields fed lines,
                    blocks on an internal queue until close().
  liveness.py     Liveness prober: class ProcessProber(run=subprocess.run, exists=os.path.exists)
                    with probe(record) -> Liveness (the Phase 0 function, `_SHELL_COMMS` kept).
                  FakeProber(default=Liveness.ALIVE): answers: dict[str, Liveness]; probe() reads
                    answers.get(slot_key, default); records calls.
  terminal.py     TerminalDriver protocol: open(shell_command) -> str | None (tty);
                    focus(tty) -> bool; terminal_pid() -> int | None; post_key(pid, keycode, down)
                  AppleScriptTerminal: today's open_terminal/focus_terminal_tab/get_terminal_pid/
                    post_key_event, with ONE shared `_find_tab_script(tty)` AppleScript fragment
                    (dedupe: today the tab-search loop is copy-pasted).
                  NullTerminal: every method logs and returns None/False — used for --no-open,
                    --dry-run, and when Quartz/osascript is unavailable.
                  FakeTerminal: records calls; open() returns f"/dev/ttysFAKE{n}".
  launcher.py     resolve_command, terminal_command, KNOWN_COMMAND_PATHS, load_agents_config,
                  agent_config_for_slot, resolve_agent_cwd, DEFAULT_CWD, validate_or_create_cwd,
                  and a single `build_launch(slot, config, *, effort=None, overrides=None)
                  -> LaunchPlan(record_fields, shell_command)` used by BOTH the CLI `launch`
                  and the bridge's Launch effect. `launch_all` and `launch_slot_from_config`
                  collapse into this.
  hooks_server.py start_hook_server(submit: Callable[[HookEvent], None], host, port) — the
                  handler parses the body, builds HookEvent, calls submit(), returns 200. It has
                  NO access to the registry or the device.
  hooks_install.py install_claude_hooks, install_codex_hooks, _claude_group_is_ours,
                  HOOK_MARKER, hook command builders. Unchanged logic.
```

Tests: port `test_liveness.py` probe rows to `ProcessProber(run=stub)`;
port `test_registry.py` to `Registry(path)`; add
`test_terminal_fake_records_calls` (trivial) and
`test_launcher_build_launch_golden` — the `shell_command` for
`(slot=2, family=claude, cwd=~/Documents/foo, effort=high)` is compared to
a golden string checked into the test.

### 1.4 Bridge: one queue, one owner (step 1.4)

`host/switchboard/bridge.py`:

```python
class Bridge:
    def __init__(self, *, registry: Registry, device: DeviceLink, terminal: TerminalDriver,
                 prober: LivenessProber, clock: Clock, launch_config: dict | None,
                 auto_launch: bool, dry_run: bool, no_open: bool,
                 log: Callable[[str], None] = print) -> None
    def submit(self, event: Event) -> None            # thread-safe; any thread
    def step(self, event: Event) -> None              # process ONE event on the calling thread
    def startup_sync(self) -> None                    # reconcile (confirm=False) + push slots 1..4
    def run(self, *, liveness_interval: float = 2.0, duration: float | None = None) -> int
```

`step()` is the whole Phase 1 win. It is the only code that touches the
registry inside the process:

```python
def step(self, event):
    event = self._resolve_probes(event)            # agent.select / voice.* get "liveness" injected
    with self.registry.transaction() as reg:
        slots = reg.setdefault("slots", {})
        new_slots, effects = reduce(slots, self.state, event, int(self.clock.now()), auto_launch=...)
        reg["slots"] = new_slots
        snapshot = copy.deepcopy(new_slots)         # for effects, after the lock is released
    for effect in effects:
        self._apply(effect, snapshot)
```

`_apply`:

- `SendUpdate(k)` → `device.send(agent_update_event(snapshot[k]))` or the
  empty-record update (today's `write_slot_update` logic, using
  `self.clock` for `busy_elapsed_ms`).
- `Launch(slot)` → `plan = build_launch(...)`; if `dry_run` → `Log` only;
  `tty = None if no_open else terminal.open(plan.shell_command)`; then
  `self.step(SlotRegistered(record))` (re-entrant call is fine: it is the
  same thread and the transaction is released).
- `Focus(tty)` → `terminal.focus(tty)`.
- `VoiceKey(down)` → `terminal.post_key(terminal.terminal_pid(), SPACE_KEYCODE, down)`.
- `Log(msg)` → `self.log(msg)`.

`run()` starts three producers and loops on the queue in the calling thread:

- **Serial reader thread**: `for line in device.lines(): submit(parse…)`;
  on `OSError` submits `Shutdown` with the exception attached so `cli.listen`
  can do its `--retry` dance exactly as today.
- **Ticker thread**: every `liveness_interval`, loads the registry
  (read-only), probes each slot **on the ticker thread** (so a slow `ps`
  never blocks the worker), submits `LivenessObserved(results)`. Wrapped
  in `try/except Exception` + traceback, must never die (keep the comment).
- **Hook server**: `start_hook_server(self.submit, …)`.

The worker loop: `while True: event = queue.get(timeout=0.25)`; on
`Shutdown` break; on any other exception in `step`, print traceback and
continue (the worker must be as unkillable as the ticker). `duration`
uses `clock.monotonic()`.

Then delete from `switchboard_bridge.py`: `describe_event`, `handle_line`,
`run_listener`, `run_bridge_listener`, `_liveness_ticker`,
`reconcile_liveness`, `_dead_probe_confirmed`, `BridgeState`,
`_HookRequestHandler._apply_hook_event`, `write_slot_update`,
`write_device_event`, `sync_device`'s hand-rolled line splitter (use
`SerialDevice.lines()` with a deadline).

Tests — `host/tests/test_bridge.py` (unit, no threads: construct `Bridge`
with `FakeDevice`, `FakeTerminal`, `FakeProber`, `FakeClock`, `Registry(tmp)`
and call `step` directly):

- `test_step_select_empty_slot_launches_and_registers`: 1 `terminal.open`
  call, record in registry with `terminal_tty == "/dev/ttysFAKE1"`, device
  got `launched`.
- `test_step_select_unknown_never_launches`.
- `test_step_hook_sequence_matches_phase0_golden`: SessionStart → idle,
  UserPromptSubmit → working (elapsed 0), +40 s PostToolUse → no line,
  Stop → done, SessionEnd → empty.
- `test_effects_run_after_lock_released`: make `FakeDevice.send` try to
  open a `registry.transaction()` (non-blocking `LOCK_EX | LOCK_NB`); it
  must succeed.
- `test_worker_survives_step_exception`: a `FakeDevice.send` that raises
  once; `run()` in a thread with `duration=0.5`; the next event still
  produces a line.
- `test_startup_sync_pushes_four_updates_and_frees_dead`.

### 1.5 CLI + shim (step 1.5)

`host/switchboard/cli.py`:

- `build_parser()` with a **parent parser** for the shared listen flags
  (`--registry --port --baud --duration --sample --stdin --auto-launch
  --launch-config --dry-run --no-open --retry --retry-delay`) attached to
  both the root and `listen`. `--help` output for `listen` must list every
  flag exactly once.
- Subcommands map to functions that build the real objects:
  `listen` → `SerialDevice`/`FdDevice(stdin)`/`FileDevice(sample)`,
  `AppleScriptTerminal` (or `NullTerminal` for `--no-open`/`--dry-run`),
  `ProcessProber`, `SystemClock`, then `Bridge(...).run(...)`.
  `launch`, `launch-all`, `slots`, `status`, `clear`, `config`,
  `install-hooks`, `sync` — bodies moved, using `Registry`, `launcher`,
  `hooks_install`, `SerialDevice`.
- `host/switchboard_bridge.py` becomes:

  ```python
  #!/usr/bin/env python3
  """Entry point shim; the implementation lives in the switchboard package."""
  import sys, pathlib
  sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
  from switchboard.cli import main
  if __name__ == "__main__":
      raise SystemExit(main())
  ```

Tests — `host/tests/test_cli.py`: `test_listen_help_lists_each_flag_once`,
`test_root_and_listen_defaults_identical` (parse `[]` vs `["listen"]`,
compare namespaces minus `func`), `test_status_subcommand_roundtrip`
(`status --slot 1 --status done` against a tmp registry via
`subprocess.run` on the shim — this also proves the shim works).
`conftest.py` no longer imports `switchboard_bridge`; fixtures build
package objects.

### 1.6 Golden traces + regressions (step 1.6)

`host/tests/golden/*.in.jsonl` / `*.out.jsonl`, run by
`host/tests/test_golden.py` which parametrizes over every `*.in.jsonl`.

Input line grammar (one JSON object per line):

```json
{"t": 0,  "board": {"event": "agent.select", "slot": 1}}
{"t": 1,  "hook": {"slot": 1, "event": "SessionStart", "body": {"session_id": "s-aaa"}}}
{"t": 3,  "hook": {"slot": 1, "event": "UserPromptSubmit", "body": {"session_id": "s-aaa"}}}
{"t": 43, "hook": {"slot": 1, "event": "PostToolUse", "body": {"session_id": "s-aaa"}}}
{"t": 44, "liveness": {"1": "unknown"}}
{"t": 46, "liveness": {"1": "dead"}}
{"t": 48, "liveness": {"1": "dead"}}
```

`t` is absolute seconds; the runner sets `FakeClock` to `1_000_000 + t`
before each event. `FakeProber` answers are set from the most recent
`liveness` line so `agent.select` probes agree with the trace. The runner
calls `bridge.step` (no threads, no sleeps) and compares
`FakeDevice.sent` to the `.out.jsonl` (exact, ordered). A `--update-golden`
pytest option rewrites `.out.jsonl` for review.

Traces to check in (each one is a bug from `state-and-testing-plan.md` §1):

1. `launch_turn_stop.in.jsonl` — §1.1: `launched` then `idle`, both soft
   white on the board; `busy_elapsed_ms` exact.
2. `new_prompt_resets_ramp.in.jsonl` — §1.2: two `UserPromptSubmit` 100 s
   apart; second update has `busy_elapsed_ms: 0`.
3. `effort_change_keeps_ramp.in.jsonl` — §1.2: `agent.reasoning.apply` mid
   turn; the update's `busy_elapsed_ms` continues counting.
4. `unknown_never_launches.in.jsonl` — §1.3b: select, `liveness unknown`,
   select again → exactly one `launched` line.
5. `dead_two_ticks.in.jsonl` — §1.3a: DEAD, DEAD → one `empty`; DEAD,
   UNKNOWN, DEAD → none.
6. `foreign_session_ignored.in.jsonl` — §1.3c.
7. `subagent_defers_done.in.jsonl` — existing gating.
8. `startup_reconcile.in.jsonl` — a pre-seeded registry (the runner
   accepts `{"registry": {...}}` as the first line) with one dead slot;
   `startup_sync` → 4 lines, dead slot `empty`.

Race regressions (threads, but no hardware) — keep the Phase 0
`test_bridge_integration.py` working by porting it to `FdDevice` over the
pty pair and the real `hooks_server` on an ephemeral port; keep scenarios
1–5. Add:

- `test_hook_flood_never_resurrects_slot`: 8 threads × 50 `PostToolUse`
  POSTs; `SessionEnd` from thread 9 at random time; after `join`, slot
  absent, registry parses, and **no `agent.update` for slot 1 appears after
  the `empty` line** (the queue makes this a strict ordering guarantee —
  assert it).

### 1.7 CI (step 1.7)

`.github/workflows/ci.yml`, `macos-latest`:

```yaml
- uses: actions/setup-python@v5   { python-version: "3.14" }
- run: python -m pip install -r host/requirements-dev.txt
- run: python -m pytest host/tests -q -m "not hardware"
- run: brew install arduino-cli
- uses: actions/cache@v4          # ~/Library/Arduino15 keyed on the core + lib versions
- run: arduino-cli core install esp32:esp32
- run: arduino-cli lib install "Adafruit seesaw Library" ArduinoJson
- run: firmware/neokey/test/run.sh
- run: firmware/neokey/test/compile.sh
```

Pin the esp32 core version to what is installed locally (`arduino-cli core
list` — write the version into the workflow). The `sync`/`launch`
subprocess tests must not require Terminal.app: they run with
`--dry-run`/`--no-open` or against the shim's non-GUI paths only.

### 1.8 Docs (step 1.8)

- `host/README_BRIDGE.md`: new "Architecture" section (the §3.1 diagram
  from the plan, plus the module table), "Adding a status" checklist (still
  multi-file in Phase 1 — Phase 2.5 collapses it), how to run golden traces
  and update them.
- `docs/design/state-and-testing-plan.md` §6: mark Phase 1 done with the
  commit hash.

### Phase 1 definition of done

- `pytest host/tests -q` green; ≥ 80 tests; no test imports
  `switchboard_bridge` except `test_cli.py`'s subprocess check.
- `host/switchboard_bridge.py` ≤ 15 lines. No module in `host/switchboard/`
  exceeds 400 lines.
- `grep -rn "time.time\|LIVE_EFFORT_RESTART\|restart_in_slot_terminal\|find_terminal_tab" host/switchboard host/switchboard_bridge.py` → nothing.
- `grep -c osascript host/switchboard/terminal.py` == the only file containing it.
- CI green on a pushed branch.
- Manual on this Mac: restart the LaunchAgent; the Phase 0 manual checklist
  (spec §10) still passes; additionally `launchctl kickstart -k` twice in
  quick succession leaves the LEDs correct (startup sync).

## 2. Phase 2 — product polish

All of these are additive on the Phase 1 package. Order matters only where
noted.

### 2.1 `doctor` (step 2.1)

`switchboard_bridge.py doctor [--port P]` — `host/switchboard/doctor.py`.
Each check prints one line `[ok]`/`[warn]`/`[fail] <name>: <detail>`; exit
1 if any `fail`. Probe, don't guess:

| check | how |
| --- | --- |
| serial port | `find_default_port()`; try `SerialDevice.open()`; report `EBUSY` as "another bridge is running (LaunchAgent?)" |
| hook port | `socket.connect_ex(("127.0.0.1", HOOK_PORT))` — 0 means a bridge is listening (`ok` if the LaunchAgent is loaded, else `warn: something else owns 8877`) |
| hooks installed | `install_claude_hooks(dry_run=True)` / codex → reports `installed`/`missing`/`stale marker` (add a `dry_run` kwarg that returns the status string without writing) |
| Terminal automation | `osascript -e 'tell application "Terminal" to count windows'`; `-1743` = permission denied |
| Accessibility | `AXIsProcessTrusted()` via pyobjc if importable, else `warn: pyobjc missing, voice PTT unavailable` |
| registry | every slot's `ProcessProber.probe` is `ALIVE`; list the rest |
| agents.json | every resolved `cwd` exists (or is creatable under `~/Documents`) |
| LaunchAgent | `launchctl print gui/$UID/com.switchboard.bridge` succeeds |

Tests: `test_doctor.py` with every probe stubbed; assert the exit code
matrix and that `--json` (add it) emits `[{"name","level","detail"}]`.

### 2.2 `done` returns to soft white on key press (step 2.2)

Plan §5.1: green holds until the key is pressed or the next prompt. Reducer
change: `agent.select` on a slot whose status is `done` (ALIVE) sets
status `idle` (via `set_status`, which clears `busy_since`) before
`SendUpdate`. Golden trace `done_clears_on_press.in.jsonl`. Firmware
unchanged.

### 2.3 Unknown-state amber + ack tracking (step 2.3)

Two sources of "the LED may be lying":

**(a) Liveness UNKNOWN.** `agent.update` gains `"liveness": "alive" |
"unknown"` (omit for empty). The reducer keeps a per-slot in-memory
`unknown_probes` counter in `ReducerState`; ≥ 2 consecutive UNKNOWN →
`SendUpdate` with `liveness: "unknown"`; the next ALIVE → `SendUpdate` with
`alive`. Firmware: `pollIncomingSerial` reads `liveness`; `renderKey` takes
a `bool uncertain` and, when true, renders `colorForStatus(Status::Unknown)`
with the 3000 ms pulse regardless of status. `led_model_test.cpp` gains
`renderKey(Working, 0, false, 0, /*uncertain=*/true)` is amber.

**(b) No ack.** Bridge side only. `Bridge` keeps `pending_acks: dict[str,
(sent_at_monotonic, event, attempts)]`; `agent.update.ack` clears the
entry; the ticker's `LivenessObserved` step also retries any entry older
than 1 s up to 3 attempts, then `Log("slot N: board did not ack after 3
attempts")`. Test: `FakeDevice` with `drop_next=2` → the third send
succeeds; assert 3 sends and one ack.

### 2.4 Ghost-tab cleanup (step 2.4)

`--close-dead-tabs` on `listen` (default off; LaunchAgent installer gains
the same flag). When the reducer frees a slot via `LivenessObserved`
(confirmed DEAD) and the record had a `terminal_tty`, emit
`CloseTab(tty)`. `AppleScriptTerminal.close(tty)`: find the tab by tty,
`close` it (Terminal's own "close without asking" respects the user's
profile setting — do not force). `FakeTerminal.close` records. Never emit
`CloseTab` for `SessionEnd` (the user typed `/exit`; leave the shell).

### 2.5 Shared status table (step 2.5)

`host/switchboard/status_table.py` is the single source of truth:

```python
STATUSES = [
  # name          busy   claude_hooks                        codex_hooks              color      pulse_ms
  ("empty",       False, [],                                 [],                      0x000000,  0),
  ("launched",    False, [],                                 [],                      0x8C8C8C,  0),
  ("idle",        False, ["SessionStart"],                   [],                      0x8C8C8C,  0),
  ("thinking",    True,  [],                                 [],                      None,      None),  # ramp
  ("working",     True,  ["UserPromptSubmit","PostToolUse"], ["UserPromptSubmit","PostToolUse"], None, None),
  ("waiting",     False, [],                                 [],                      0xFFFF00,  0),
  ("needs_input", False, ["PreToolUse","Notification"],      ["PermissionRequest"],   0xFFFF00,  500),
  ("blocked",     False, [],                                 [],                      0xFF0000,  0),
  ("done",        False, ["Stop"],                           ["Stop"],                0x00FF00,  0),
  ("unknown",     False, [],                                 [],                      0xFF8C00,  3000),
]
```

(Copy the real mappings from `model.py` — the table above is the shape,
not the authority; `SessionEnd → empty` and the `PreToolUse` matcher
`AskUserQuestion` from `CLAUDE_HOOK_EVENTS` must match what is live, so the
table also needs a per-hook `matcher` column or a separate `HOOK_MATCHERS`
dict kept next to it.)
`STATUS_CHOICES`, `BUSY_STATUSES`, `CLAUDE_HOOK_STATUS`, `CODEX_HOOK_STATUS`
are derived from it. `python -m switchboard.status_table --emit-header >
firmware/neokey/status_table.h` generates the `Status` enum,
`statusFromString`, `statusIsBusy`, `colorForStatus`, and the static pulse
table; `led_model.h` includes it and deletes its hand-written copies.
Test `test_status_table_header_is_current` regenerates into a string and
compares with the checked-in file (fail message says which command to
run). Update the README "Adding a status" checklist to: edit one table,
run one command.

### 2.6 Docs + design table (step 2.6)

- `SWITCHBOARD_DESIGN.md` "Agent Status Colors": add the `liveness`
  field and the amber rule; note that the table is generated from
  `status_table.py`.
- `host/README_BRIDGE.md`: `doctor`, `--close-dead-tabs`, ack retry.
- `state-and-testing-plan.md` §6: Phase 2 done; §5.6 (log rotation) marked
  resolved-by-removal in Phase 0.

### Phase 2 definition of done

- `pytest` green, ≥ 100 tests; `run.sh` and `compile.sh` exit 0; CI green.
- `doctor` on this Mac reports all `ok` with the LaunchAgent running, and
  reports the serial port as busy with a useful message when it is.
- Manual: unplug-free test of amber — `launch --family shell --command cat
  --no-open --slot 3` gives a record with no tty → UNKNOWN → key 3 pulses
  amber within ~4 s; `clear --slot 3` turns it off.
- Manual: Cmd+W a live tab with `--close-dead-tabs` off → key off within
  ~4 s and the `[Process completed]` tab remains; with it on → the tab is
  gone too.

## 3. Explicitly out of scope

Display (ESP-IDF) firmware, the plug-and-play installer, projects list /
key-hold gesture (plan §5.4 "Later"), any change to hook installation
semantics, pyobjc becoming required.
