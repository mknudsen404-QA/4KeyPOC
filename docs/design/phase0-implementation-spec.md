# Switchboard Phase 0 — implementation spec (handoff)

Status: ready to implement. Companion to `state-and-testing-plan.md` (the
*why*). This document is the *what*, in enough detail to execute without
re-deriving the analysis. Work through the steps in order; each step ends
with a passing test run and its own commit.

Decisions already made (do not reopen):

- Busy ramp measures the **current turn** (resets on `UserPromptSubmit`).
- **Log tailing is removed** (`script -q`, `host/logs`, `poll_slot_logs`
  pattern matching, `test-trigger`). Hooks are the only status source.
- Default working directory for launched agents is **`~/Documents`**.

## 0. Ground rules for the implementer

- Python 3.14, stdlib only for the bridge (pyobjc stays optional). Tests use
  `pytest`; add `host/requirements-dev.txt` containing `pytest>=8`. Run with
  `host/.venv/bin/pip install -r host/requirements-dev.txt` then
  `host/.venv/bin/python -m pytest host/tests -q`.
- Do not restructure into a package yet (that is Phase 1). Phase 0 edits
  `host/switchboard_bridge.py` in place. Keep every existing CLI subcommand
  working except `test-trigger`, which is deleted.
- The bridge is running as a LaunchAgent on this Mac. After editing, restart
  it with `launchctl kickstart -k gui/$(id -u)/com.switchboard.bridge`.
  Never run `clear` or delete `host/agent_registry.json` casually: live
  sessions (possibly the one you are running in) are registered there.
- Commit after each numbered step. Message format: `Phase 0.N: <summary>`.
- Firmware changes are compiled with
  `arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc firmware/neokey`
  (install `arduino-cli` via Homebrew if missing; libraries:
  `Adafruit seesaw Library`, `ArduinoJson`). Do **not** flash the board
  unless the user asks; leave the `.ino` compiled and tested on the host.

## 1. Test scaffold (step 0.1)

Create:

```text
host/requirements-dev.txt
host/pytest.ini            [pytest] testpaths = tests ; markers = hardware: needs a board
host/tests/__init__.py
host/tests/conftest.py
host/tests/fixtures/hooks/  (JSON payloads, see §1.2)
```

`host/tests/conftest.py` must provide:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so `import switchboard_bridge` works
import switchboard_bridge as sb

@pytest.fixture
def registry_path(tmp_path): return tmp_path / "registry.json"

@pytest.fixture
def fake_clock(monkeypatch):
    class FakeClock:
        def __init__(self): self.t = 1_000_000.0
        def now(self): return self.t
        def advance(self, s): self.t += s
    clock = FakeClock()
    monkeypatch.setattr(sb, "_clock", clock)
    return clock

@pytest.fixture
def fake_device():
    """Collects every line the bridge would write to the board."""
    class FakeDevice:
        def __init__(self): self.lines: list[dict] = []
    dev = FakeDevice()
    return dev
```

`fake_device` is used via `monkeypatch.setattr(sb, "write_device_event", lambda fd, ev: dev.lines.append(ev))`
in the tests that need it — write a small helper fixture `capture_device`
that does this and yields `dev.lines`.

### 1.1 Clock seam

Add to the bridge, near the top:

```python
class _SystemClock:
    def now(self) -> float: return time.time()
_clock = _SystemClock()
def _now() -> int: return int(_clock.now())
```

Replace **every** `int(time.time())` / `time.time()` in the module with
`_now()`. (`time.monotonic()` for the listener `duration` timeouts may stay.)
Acceptance: `grep -n "time.time" host/switchboard_bridge.py` returns nothing.

### 1.2 Hook payload fixtures

Claude Code posts a JSON body on every hook. Minimum shapes to create as
fixture files (one per file, realistic values):

```json
SessionStart.json      {"session_id":"s-aaa","hook_event_name":"SessionStart","cwd":"/Users/x/Documents","source":"startup"}
UserPromptSubmit.json  {"session_id":"s-aaa","hook_event_name":"UserPromptSubmit","prompt":"hi"}
PostToolUse.json       {"session_id":"s-aaa","hook_event_name":"PostToolUse","tool_name":"Bash"}
Notification.json      {"session_id":"s-aaa","hook_event_name":"Notification","message":"Claude needs your permission"}
Stop.json              {"session_id":"s-aaa","hook_event_name":"Stop","stop_hook_active":false}
SubagentStart.json     {"session_id":"s-aaa","hook_event_name":"SubagentStart","agent_id":"ag-1"}
SubagentStop.json      {"session_id":"s-aaa","hook_event_name":"SubagentStop","agent_id":"ag-1"}
SessionEnd.json        {"session_id":"s-aaa","hook_event_name":"SessionEnd","reason":"exit"}
```

Tests load these with a helper `hook_body(name, **overrides) -> bytes`.

## 2. Registry: atomic, locked (step 0.2)

Replace `load_registry` / `save_registry` usage pattern with a transaction.

```python
_REGISTRY_LOCK = threading.RLock()

def save_registry(registry: dict, path: Path = DEFAULT_REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)

@contextlib.contextmanager
def registry_transaction(path: Path = DEFAULT_REGISTRY):
    """Exclusive load→mutate→save. Holds the in-process lock and an
    fcntl.flock on <path>.lock so the `status`/`clear`/`config` CLI
    (separate processes) can't interleave with the running bridge."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _REGISTRY_LOCK, open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            registry = load_registry(path)
            yield registry
            save_registry(registry, path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
```

Rules:

- Every code path that mutates the registry uses `with registry_transaction(path) as registry:`
  and mutates the yielded dict. Call sites to convert: `update_registry_slot_status`,
  `describe_event` (`agent.select` pop, launch), `launch_agent`,
  `_HookRequestHandler.do_POST`, `clear_slot`, the poller (rewritten in §4),
  `set_status`.
- Read-only paths (`list_slots`, `registry_slot_summary`, `sync_device`) may
  keep `load_registry`. `load_registry` must tolerate a missing file only;
  a corrupt file raises (that is now impossible to produce via `save_registry`).
- Device writes (`write_slot_update`) happen **after** the transaction
  exits, using the dict you still hold — never inside the `with` block, so
  a slow serial write does not hold the file lock.

Tests — `host/tests/test_registry.py`:

- `test_save_is_atomic_under_concurrent_reads`: 8 writer threads each doing
  200 `registry_transaction` increments of `registry["counter"]`, 4 reader
  threads calling `load_registry` in a loop; assert no exception in any
  thread and final counter == 1600.
- `test_transaction_serializes_across_processes`: spawn
  `subprocess.run([sys.executable, bridge, "--registry", p, "status", "--slot", "1", "--status", "done"])`
  while holding a transaction in the test; assert the child blocks until
  the test releases (use a 0.5 s hold and measure child wall time ≥ 0.4 s).
- `test_no_tmp_files_left_behind`.

## 3. Status model additions (step 0.3)

### 3.1 `busy_since` (turn-scoped)

In `_apply_hook_event` and `update_registry_slot_status`, apply this helper on
every status assignment instead of writing `record["status"]` directly:

```python
def _set_status(record: dict, status: str) -> None:
    was_busy = record.get("status") in BUSY_STATUSES
    record["status"] = status
    record["last_updated_at"] = _now()
    now_busy = status in BUSY_STATUSES
    if now_busy and not was_busy:
        record["busy_since"] = _now()
    elif not now_busy:
        record.pop("busy_since", None)
```

Additionally: `UserPromptSubmit` always resets `busy_since = _now()` even if
the slot was already busy (new turn = new ramp). Implement as: in
`_apply_hook_event`, if `event == "UserPromptSubmit"`, set
`record["busy_since"] = _now()` after `_set_status`.

Effort changes (`update_registry_slot_status(effort=...)`) must **not**
touch `busy_since` or `status`.

### 3.2 Device event shape

`agent_update_event` emits:

```json
{"event":"agent.update","slot":1,"name":"Maestro","family":"claude",
 "status":"working","effort":"medium","activity":"",
 "busy_elapsed_ms":12345}
```

`busy_elapsed_ms = max(0, (_now() - busy_since) * 1000)` when status is
busy, else `0`. Drop `busy_seconds`. (Firmware §6 accepts both during the
transition.)

### 3.3 `session_id` guard

In `_apply_hook_event(registry, slot_key, record, event, body)`:

```python
payload = _parse_body(body)              # dict or {}
incoming = payload.get("session_id")
owner = record.get("session_id")
if event == "SessionStart" and incoming:
    if owner and owner != incoming:
        return False                     # a second session claims an owned slot: ignore it
    record["session_id"] = incoming
elif incoming and owner and incoming != owner:
    return False                         # stale/foreign session: ignore
```

`_extract_agent_id` becomes `_parse_body(body).get("agent_id")`.

Note the first branch: if a duplicate session somehow gets launched with the
same `SWITCHBOARD_SLOT`, its hooks are dropped rather than corrupting the
slot. The duplicate is *not* the slot's owner until the owner's
`SessionEnd` frees the slot.

### 3.4 Remove the 2 s re-push

Delete the "Re-push busy slots even when status itself hasn't changed" loop.
The firmware now advances the ramp locally (§6).

Tests — `host/tests/test_hook_reducer.py` (call `_apply_hook_event`
directly on an in-memory record with `fake_clock`):

| test | assertion |
| --- | --- |
| `test_claude_event_table` | parametrized over `CLAUDE_HOOK_STATUS`: each event yields its status |
| `test_codex_event_table` | same for `CODEX_HOOK_STATUS` |
| `test_session_end_pops_slot` | record removed from `registry["slots"]` |
| `test_busy_since_set_on_enter_busy` | idle → `UserPromptSubmit` sets `busy_since == clock.now` |
| `test_busy_since_preserved_across_post_tool_use` | advance 30 s, `PostToolUse`: `busy_since` unchanged |
| `test_new_prompt_resets_busy_since` | working, advance 30 s, `UserPromptSubmit` → `busy_since` == new now |
| `test_busy_since_cleared_on_stop` | `Stop` → key absent |
| `test_effort_change_does_not_touch_busy_since` | via `update_registry_slot_status(effort="high")` |
| `test_stop_deferred_while_subagent_active` | `SubagentStart`, `Stop` → status still working, `main_stopped` True |
| `test_subagent_stop_delivers_deferred_done` | then `SubagentStop` → done |
| `test_duplicate_subagent_start_ignored` | returns False second time |
| `test_new_prompt_clears_main_stopped` | |
| `test_session_id_adopted_on_session_start` | |
| `test_foreign_session_id_ignored` | owner `s-aaa`, body `s-bbb` `UserPromptSubmit` → False, status unchanged |
| `test_second_session_start_does_not_steal_slot` | |
| `test_legacy_record_without_session_id_accepts_events` | |
| `test_agent_update_event_busy_elapsed_ms` | advance 12.345 s → `12345` |
| `test_agent_update_event_not_busy_is_zero` | |

## 4. Liveness by process, three-valued (step 0.4)

### 4.1 Launch without `script`

`terminal_command()` no longer wraps in `script -q`; it becomes:

```text
printf '\033]0;%s\007' '<title>'
cd '<cwd>'
export SWITCHBOARD_SLOT=<n>
exec <command>
```

Remove `log_path` parameter, `LOG_DIR`, `terminal_log_path`, `terminal_log`
from `slot_record`, `ANSI_RE`, `strip_ansi`, `ACTIVITY_PATTERNS`,
`TAIL_READ_BYTES`, `classify_activity`, `read_log_tail`, `test_trigger`, and
the `test-trigger` subparser. Remove `host/logs/` mention from `.gitignore`
and the "Testing status auto-detection without spending tokens" section from
`host/README_BRIDGE.md`. Keep the `shell` family (still useful: `launch
--family shell --command cat` is a free liveness test).

### 4.2 Probe

```python
class Liveness(enum.Enum):
    ALIVE = "alive"; DEAD = "dead"; UNKNOWN = "unknown"

_SHELL_COMMS = {"login", "-zsh", "zsh", "-bash", "bash", "sh", "-sh", "fish", "-fish"}

def probe_liveness(record: dict) -> Liveness:
    tty_path = record.get("terminal_tty")
    if not tty_path:
        return Liveness.UNKNOWN            # --no-open records: only hooks/clear can free them
    if not os.path.exists(tty_path):
        return Liveness.DEAD               # tab closed: the pty node is gone
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-t", os.path.basename(tty_path)],
            capture_output=True, text=True, timeout=2.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return Liveness.UNKNOWN
    if result.returncode not in (0, 1):
        return Liveness.UNKNOWN
    comms = {line.strip().rsplit("/", 1)[-1] for line in result.stdout.splitlines() if line.strip()}
    return Liveness.ALIVE if (comms - _SHELL_COMMS) else Liveness.DEAD
```

Make `subprocess.run` here go through a module-level `_run = subprocess.run`
so tests can monkeypatch `sb._run`.

### 4.3 Confirmation before freeing

Keep an in-memory `dict[str, int]` of consecutive DEAD probes per slot on
`BridgeState` (`dead_probes`). A slot is freed only when it has been
`DEAD` on **two consecutive** probes at least 1 s apart. Any `ALIVE` or
`UNKNOWN` resets the count to 0.

```python
def reconcile_liveness(state: BridgeState) -> list[str]:
    """Probe every registered slot; free confirmed-dead ones. Returns freed slot keys."""
```

Called: (a) by the background ticker every 2 s (this replaces
`poll_slot_logs`; wrap the whole body in `try/except Exception` that logs
to stderr with the traceback and continues — the thread must never die),
(b) once at bridge startup **before** the listener loop (see §4.5),
(c) on `agent.select` for that one slot (see §4.4).

After freeing, `write_slot_update(fd, registry, slot)` for each freed slot
(sends `status: empty`).

### 4.4 `agent.select` rules

Replace the `find_terminal_tab` block in `describe_event` with:

```text
probe = probe_liveness(existing) if existing else None
if existing and probe is DEAD:            # single DEAD is enough here? NO — same 2-probe rule
    treat as still registered; bump dead_probes; if now confirmed → free, then fall through to launch
if existing and probe is UNKNOWN:
    focus_terminal_tab(...) if tty else nothing
    return f"Slot {n}: liveness unknown, not relaunching"   # never auto-launch on UNKNOWN
if existing (ALIVE):
    focus + write_slot_update + return "Selected ..."
if not existing and auto_launch:
    launch
```

`find_terminal_tab` is deleted. `focus_terminal_tab` stays (AppleScript is
fine for focus; a failure there is harmless).

### 4.5 Startup reconcile + full sync

In `run_bridge_listener`, before starting threads:

1. `reconcile_liveness(state)` with the 2-probe rule relaxed to a single
   DEAD (nothing has been running; a dead pty at startup is dead).
2. For slots 1..4: `write_slot_update(device_fd, registry, slot)` — registered
   slots get their status, unregistered get `empty`. This is what makes a
   bridge restart a clean state on the LEDs.

### 4.6 Tests — `host/tests/test_liveness.py`

Monkeypatch `sb._run` and `os.path.exists` (via `monkeypatch.setattr(sb.os.path, "exists", ...)`).

| test | setup → expected |
| --- | --- |
| `test_alive_when_agent_process_on_tty` | ps stdout `login\nclaude\n` → ALIVE |
| `test_dead_when_only_shell_on_tty` | `login\n-zsh\n` → DEAD |
| `test_dead_when_tty_missing` | exists False → DEAD (ps not called) |
| `test_unknown_on_timeout` | `_run` raises TimeoutExpired → UNKNOWN |
| `test_unknown_on_no_tty_record` | |
| `test_free_requires_two_consecutive_dead` | first `reconcile_liveness` → nothing freed; second → freed |
| `test_unknown_resets_dead_count` | DEAD, UNKNOWN, DEAD → not freed |
| `test_select_never_launches_on_unknown` | `describe_event({"event":"agent.select","slot":1})` with `auto_launch=True`, probe UNKNOWN → `launch_slot_from_config` not called (monkeypatch it to raise) |
| `test_select_on_alive_focuses_and_pushes_update` | |
| `test_startup_reconcile_frees_dead_and_syncs_all_four` | captured device lines: exactly 4 `agent.update`, dead slot is `empty` |
| `test_ticker_survives_exception` | make `load_registry` raise once; assert ticker calls again |

## 5. Default working directory (step 0.5)

- `agents.json` schema gains an optional top-level `"defaults": {"cwd": "~/Documents"}`.
- Resolution order for a slot's cwd: slot `cwd` → `defaults.cwd` → `~/Documents`.
  Always `Path(...).expanduser().resolve()`.
- `agent_config_for_slot` fallback `cwd` becomes `~/Documents` (not `PROJECT_ROOT`).
- `launch_agent`: if the cwd does not exist and it is inside `Path.home()/"Documents"`,
  create it (`mkdir -p`) and print `Created <cwd>`; otherwise fail with exit 2 and
  message `Working folder does not exist: <cwd>. Set it with: switchboard_bridge.py config --slot N --cwd PATH`.
- `agents.example.json`: every slot's `cwd` becomes `"~/Documents"`, and add
  `"defaults": {"cwd": "~/Documents"}`.
- `setup.sh`: stop rewriting `cwd` to the project root; copy the example
  verbatim.
- New subcommand:

```text
switchboard_bridge.py config --slot N [--cwd PATH] [--name NAME] [--family codex|claude|shell] [--command CMD] [--effort E]
switchboard_bridge.py config --default-cwd PATH
switchboard_bridge.py config --show
```

  Edits `host/agents.json` (creating it from the example if missing),
  validates `--cwd` exists (or is under `~/Documents`, in which case create
  it), writes with the same tmp+replace pattern, prints the resulting slot
  line. Takes effect on the slot's *next* launch; say so in the output.

- `launch_all` must go through `launch_slot_from_config` (delete its
  duplicated body) so `effort` and `defaults.cwd` apply there too.
- README_BRIDGE.md: update the "Which CLI each agent key launches" section
  and the `launch` example (drop the hardcoded `/Users/matthewknudsen/...`).

Tests — `host/tests/test_config.py`:

- `test_cwd_resolution_order` (slot > defaults > `~/Documents`).
- `test_missing_cwd_under_documents_is_created` (monkeypatch `Path.home` to `tmp_path`).
- `test_missing_cwd_elsewhere_fails_with_hint`.
- `test_config_subcommand_writes_agents_json` (run `main()` with argv via `monkeypatch.setattr(sys, "argv", ...)`, isolated `HOST_DIR`/paths through a `--agents-config` flag you add to `config`).
- `test_launch_all_uses_defaults_cwd_and_effort` with `--dry-run`.

## 6. Firmware (step 0.6)

### 6.1 Split pure logic into `firmware/neokey/led_model.h`

Header-only, no Arduino includes. Everything that is a pure function moves
here: `hsvToRgb`, `busyRampProgress`, `applySelection`, `breathFactor`,
`scaleColor`, plus the new functions below. Guard `min`/`PI` with local
definitions so it compiles with plain `c++`.

```cpp
enum class Status : uint8_t { Empty, Launched, Idle, Thinking, Working, Waiting, NeedsInput, Blocked, Done, Unknown };
Status statusFromString(const char *s);          // unknown strings -> Unknown
bool statusIsBusy(Status s);                      // Thinking || Working

constexpr uint32_t SOFT_WHITE = 0x8C8C8C;         // launched + idle
constexpr uint32_t BUSY_RAMP_MS = 300000;          // 5 min turn ramp
constexpr float BUSY_START_HUE = 210.0f, BUSY_END_HUE = 300.0f;   // blue -> magenta, never past 300
constexpr float WHITE_PHASE = 1.0f / 3.0f;         // first third: white -> blue (saturation ramp)

uint32_t colorForStatus(Status s);                // static colors; busy statuses return SOFT_WHITE (caller uses busyColor)
uint32_t busyColor(uint32_t elapsedMs);           // the ramp
unsigned long busyPulsePeriodMs(uint32_t elapsedMs);   // 4000 -> 900 as before
unsigned long pulsePeriodFor(Status s, uint32_t elapsedMs); // NeedsInput -> 500; busy -> busyPulsePeriodMs; else 0 (solid)
uint32_t renderKey(Status s, uint32_t elapsedMs, bool selected, unsigned long nowMs);  // the one function loop() calls per key
```

`busyColor` definition:

```cpp
float t = busyRampProgress(elapsedMs)      // smoothstep of min(elapsed, BUSY_RAMP_MS)/BUSY_RAMP_MS
if (t < WHITE_PHASE) { s = t / WHITE_PHASE; h = BUSY_START_HUE; v = 0.55f + 0.45f * s; }
else { u = (t - WHITE_PHASE) / (1 - WHITE_PHASE); s = 1; h = BUSY_START_HUE + (BUSY_END_HUE - BUSY_START_HUE) * u; v = 1; }
return hsvToRgb(h, s, v);
```

`colorForStatus`: Launched/Idle → `SOFT_WHITE`; Done `0x00FF00`;
Waiting/NeedsInput `0xFFFF00`; Blocked `0xFF0000`; Unknown `0xFF8C00`
(amber, slow pulse 3000 ms — the "bridge isn't sure" state, used when the
board hasn't heard from the bridge about a slot for > 15 s after a
`launched`; optional in Phase 0, required in Phase 2); Empty `0`.

`renderKey`: pick base color (`busyColor` if busy else `colorForStatus`),
apply selection dim with a floor — non-selected keys are scaled to 40%, not
25%, and never below `0x101010` per channel if the base channel was
non-zero — then apply `breathFactor` if `pulsePeriodFor` is non-zero.

### 6.2 Sketch changes (`switchboard_neokey.ino`)

- Per-slot state becomes `Status slotStatus[]`, `unsigned long slotBusyStartMs[]`.
- `pollIncomingSerial`: on `agent.update`, read `busy_elapsed_ms` (fall
  back to `busy_seconds * 1000` if present, for one release), set
  `slotBusyStartMs[i] = millis() - busy_elapsed_ms`. Then **send the ack**:
  `{"event":"agent.update.ack","slot":N}` via `sendEvent`.
- `redrawAgentKeys`: `elapsed = statusIsBusy(s) ? millis() - slotBusyStartMs[i] : 0`; `setPixelColor(i, renderKey(...))`.
- Bump `StaticJsonDocument` sizes to 384 (the update line grew).

### 6.3 Host-side firmware test — `firmware/neokey/test/led_model_test.cpp`

Plain C++17, no framework; `assert`-based; built by
`firmware/neokey/test/run.sh` (`c++ -std=c++17 -I.. led_model_test.cpp -o /tmp/led_model_test && /tmp/led_model_test`).
Arduino ignores the `test/` subfolder. Cases:

- `hsvToRgb` reference: (0,1,1)→FF0000, (120,1,1)→00FF00, (240,1,1)→0000FF, (210,0,0.55)→8C8C8C ±1.
- `busyColor(0)` == SOFT_WHITE ±1 per channel.
- `busyColor(BUSY_RAMP_MS)` == hsv(300,1,1) = FF00FF.
- For `elapsed` in 0..BUSY_RAMP_MS step 1000: derive hue back from the RGB
  (or expose `busyHue(elapsed)`) and assert hue ∈ [210, 300]. Never in red band.
- `busyColor` is monotonic in hue and never decreases in saturation.
- `statusFromString("working")` busy; `"garbage"` → Unknown.
- `renderKey(Idle, 0, selected=false, 0)` != 0 (dim floor).
- `pulsePeriodFor(NeedsInput, 0) == 500`, `pulsePeriodFor(Done, 0) == 0`.

Add a line to the top-level README pointing at `run.sh`.

## 7. Bridge ↔ firmware protocol summary after Phase 0

Board → bridge (unchanged): `agent.select`, `agent.focus`, `agent.reasoning.apply`,
`voice.hold.start`, `voice.hold.stop`, plus **new** `agent.update.ack`.

Bridge → board: `agent.update` with `busy_elapsed_ms` replacing
`busy_seconds`. Sent only on: status/effort/name change, slot freed, key
select, bridge startup full sync, `sync` command. Never on a timer.

## 8. Integration test (step 0.7) — `host/tests/test_bridge_integration.py`

Uses a real pty pair as the device and the real hook HTTP server on an
ephemeral port (make `HOOK_PORT` overridable via `SWITCHBOARD_HOOK_PORT` env
var; `start_hook_server` reads it). `launch_slot_from_config` is
monkeypatched to register a record with `terminal_tty="/dev/ttysFAKE"` and
`probe_liveness` is monkeypatched to a controllable stub. `focus_terminal_tab`
is monkeypatched to a no-op.

```python
master, slave = os.openpty()
thread = Thread(target=sb.run_bridge_listener, kwargs=dict(lines=sb.serial_lines(slave, duration=5), registry_path=..., auto_launch=True, device_fd=slave, ...))
def press(slot): os.write(master, json.dumps({"event":"agent.select","slot":slot}).encode()+b"\n")
def hook(event, slot, **body): urllib.request.urlopen(Request(f"http://127.0.0.1:{port}/switchboard-hook/{event}", data=json.dumps(body).encode(), headers={"X-Switchboard-Slot": str(slot)}))
def device_lines(): read from master, split on \n, json-decode
```

Scenarios (each asserts the exact ordered list of `agent.update` dicts read
from `master`, with a fake clock so `busy_elapsed_ms` is exact):

1. `test_golden_launch_turn_stop`: startup sync (4 empties) → press 1 →
   `launched` → SessionStart → `idle` → UserPromptSubmit → `working,
   busy_elapsed_ms 0` → clock +40 s, PostToolUse → **no line** → Stop →
   `done` → SessionEnd → `empty`.
2. `test_no_duplicate_launch_when_liveness_unknown`: press 1, launch; probe
   stub → UNKNOWN; press 1 again; assert launch called once.
3. `test_dead_session_freed_after_two_ticks_and_led_cleared`: probe → DEAD;
   wait two ticker intervals (ticker interval must be injectable —
   add `liveness_interval` kwarg to `run_bridge_listener`, default 2.0, test
   uses 0.05); assert `empty` pushed exactly once.
4. `test_foreign_session_hooks_ignored`: SessionStart `s-aaa`; hooks from
   `s-bbb` produce no lines.
5. `test_registry_race_no_resurrection`: 8 threads × 50 `PostToolUse` POSTs
   for slot 1 while a `SessionEnd` lands mid-way; at the end slot 1 is
   absent and the registry file parses.

## 9. Docs to update (step 0.8)

- `host/README_BRIDGE.md`: remove log-tail/test-trigger sections; add
  `config`; document `busy_elapsed_ms`, `agent.update.ack`, `~/Documents`
  default, and how to run tests.
- `SWITCHBOARD_DESIGN.md` "Agent Status Colors": replace the table with the
  Phase 0 palette (soft white idle/launched, white→blue→magenta busy,
  yellow fast pulse needs_input, red blocked, green done, amber unknown).
- `docs/design/state-and-testing-plan.md` §7: mark decisions resolved.

## 10. Definition of done

- `host/.venv/bin/python -m pytest host/tests -q` green; ≥ 45 tests.
- `firmware/neokey/test/run.sh` exits 0.
- `arduino-cli compile` of `firmware/neokey` succeeds.
- `grep -n "time.time\|script -q\|find_terminal_tab\|busy_seconds" host/switchboard_bridge.py` → no matches
  (except the one-release firmware fallback, which is in the `.ino`, not the bridge).
- Manual check on this Mac (user runs it): restart the LaunchAgent, press a
  key → soft white immediately, stays soft white after SessionStart; type a
  prompt → key warms from white to blue over ~100 s, magenta by ~5 min;
  `/exit` → key off within 1 s; close a tab with Cmd+W → key off within ~4 s;
  press the key of a live session twice quickly → still exactly one session.

## 11. Explicitly out of scope (Phase 1/2)

Package split, event queue/single state owner, `doctor`, ghost-tab
cleanup, done-fade, shared status table generator, CI workflow. Do not start
these in Phase 0 branches.
