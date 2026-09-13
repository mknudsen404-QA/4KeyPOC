#!/usr/bin/env python3
"""
Switchboard host bridge.

The bridge has two jobs:

1. Listen to JSON-lines events from the ESP32-S3 over USB serial.
2. Own the Mac-side slot registry so the console can map physical agent keys
   to launched Codex/Claude CLI sessions.

Mutating actions are still observe-only. This bridge can launch local terminal
sessions and record which slot owns them, but it does not approve plans, run
slash commands, or send text into an agent yet.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.server
import json
import os
import select
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

HOST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HOST_DIR.parent
sys.path.insert(0, str(HOST_DIR))  # so `import switchboard` works regardless of cwd

from switchboard.model import (  # noqa: E402 - needs HOST_DIR on sys.path first
    BUSY_STATUSES,
    CLAUDE_HOOK_STATUS,
    CODEX_HOOK_EVENTS,
    CODEX_HOOK_STATUS,
    STATUS_CHOICES,
    VOICE_SUPPORTED_FAMILIES,
    Liveness,
    command_with_effort,
    effort_args,
    hook_status_for as _hook_status_for,
    normalize_effort,
)
from switchboard.model import agent_update_event as _model_agent_update_event
from switchboard.model import set_status as _model_set_status
from switchboard.model import slot_record as _model_slot_record
from switchboard.registry import Registry
from switchboard.device import find_default_port, SerialDevice
from switchboard.liveness import ProcessProber
from switchboard.terminal import AppleScriptTerminal
from switchboard.launcher import (
    DEFAULT_CWD,
    KNOWN_COMMAND_PATHS,
    agent_config_for_slot,
    load_agents_config,
    resolve_agent_cwd,
    resolve_command,
    terminal_command,
    validate_or_create_cwd as _validate_or_create_cwd,
)
from switchboard.hooks_server import HOOK_HOST, HOOK_PATH_PREFIX, HOOK_PORT
from switchboard.hooks_install import (
    CLAUDE_HOOK_EVENTS,
    HOOK_MARKER,
    install_claude_hooks,
    install_codex_hooks,
    _claude_group_is_ours,
)

DEFAULT_REGISTRY = HOST_DIR / "agent_registry.json"
# agents.json is the user's own per-slot config (which CLI family/command each
# agent key launches). It is personal/local, not checked in. agents.example.json
# is the checked-in template; it's only used as a fallback until agents.json
# exists, so first-time setup still works with no configuration.
USER_AGENTS_CONFIG = HOST_DIR / "agents.json"
EXAMPLE_AGENTS_CONFIG = HOST_DIR / "agents.example.json"
DEFAULT_AGENTS_CONFIG = USER_AGENTS_CONFIG if USER_AGENTS_CONFIG.exists() else EXAMPLE_AGENTS_CONFIG
# macOS virtual keycode for the spacebar (used to drive Claude Code's /voice
# hold-to-record mode).
SPACE_KEYCODE = 49

# A live session was observed with only some Ctrl-C interrupts actually
# landing (real AI TUIs appear to swallow Ctrl-C while a turn is actively
# generating) — a failed interrupt typed the relaunch command straight into
# the live conversation. So effort changes only update the registry/screen;
# they take effect on the slot's next launch, not the running session.


class _SystemClock:
    def now(self) -> float:
        return time.time()


_clock = _SystemClock()


def _now() -> int:
    return int(_clock.now())


@dataclass
class BridgeState:
    selected_slot: int | None = None
    selected_agent: str | None = None
    effort_by_slot: dict[int, str] | None = None
    mic_active: bool = False
    registry_path: Path = DEFAULT_REGISTRY
    auto_launch: bool = False
    launch_config: dict | None = None
    no_open: bool = False
    dry_run: bool = False
    device_fd: int | None = None
    dead_probes: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.effort_by_slot is None:
            self.effort_by_slot = {}
        if self.dead_probes is None:
            self.dead_probes = {}


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    return Registry(path).load()


def save_registry(registry: dict, path: Path = DEFAULT_REGISTRY) -> None:
    Registry(path).save(registry)


_REGISTRY_LOCK = threading.RLock()


@contextlib.contextmanager
def registry_transaction(path: Path = DEFAULT_REGISTRY):
    """Exclusive load->mutate->save. Holds the in-process lock and an
    fcntl.flock on <path>.lock so the `status`/`clear`/`launch` CLI
    (separate processes) can't interleave with the running bridge.

    Goes through the module-level load_registry/save_registry (not
    Registry.transaction() directly) so tests that monkeypatch those two
    names here keep intercepting it.
    """
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



def slot_record(
    *,
    slot: int,
    name: str,
    family: str,
    cwd: str,
    command: str,
    terminal_title: str,
    effort: str = "medium",
    terminal_tty: str | None = None,
) -> dict:
    return _model_slot_record(
        slot=slot,
        name=name,
        family=family,
        cwd=cwd,
        command=command,
        terminal_title=terminal_title,
        effort=effort,
        terminal_tty=terminal_tty,
        now=_now(),
    )


def set_registry_slot(registry: dict, record: dict) -> None:
    registry.setdefault("slots", {})[str(record["slot"])] = record


def launch_slot_from_config(
    *,
    slot: int,
    config: dict | None,
    registry_path: Path,
    dry_run: bool,
    no_open: bool,
) -> str:
    agent = agent_config_for_slot(config, slot)
    child = argparse.Namespace(
        slot=slot,
        name=agent.get("name", f"Agent {slot}"),
        family=agent.get("family", "codex"),
        cwd=resolve_agent_cwd(agent, config),
        command=agent.get("command", "codex"),
        title=agent.get("title"),
        effort=agent.get("effort", "medium"),
        registry=registry_path,
        dry_run=dry_run,
        no_open=no_open,
        quiet=True,
    )
    result = launch_agent(child)
    if result:
        return f"Agent {slot} launch failed"
    mode = "would launch" if dry_run else ("registered" if no_open else "launched")
    return f"Agent {slot} was empty, bridge {mode} it"


def open_serial(port: str, baud: int) -> int:
    """Thin wrapper: the real implementation is device.SerialDevice; this
    keeps returning a bare fd for the not-yet-migrated listen()/sync_device
    below (Phase 1.4/1.5 replace those with a Bridge/DeviceLink directly).
    """
    return SerialDevice(port, baud).fd


def serial_lines(fd: int, duration: float | None = None) -> Iterable[str]:
    buffer = b""
    started = time.monotonic()
    while True:
        if duration is not None and (time.monotonic() - started) >= duration:
            return
        ready, _, _ = select.select([fd], [], [], 0.25)
        if not ready:
            continue
        chunk = os.read(fd, 1024)
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace").strip()


def file_lines(handle: TextIO) -> Iterable[str]:
    for line in handle:
        line = line.strip()
        if line:
            yield line


def registry_slot_summary(registry: dict, slot: int | str | None) -> str:
    if slot is None:
        return "No selected slot"
    record = registry.get("slots", {}).get(str(slot))
    if not record:
        return f"Slot {slot} is not registered"
    return (
        f"Slot {slot}: {record.get('name', 'unnamed')} "
        f"({record.get('family', 'agent')}) "
        f"[{record.get('status', 'unknown')}, {record.get('effort', 'medium')}] "
        f"in {record.get('cwd', 'unknown cwd')}"
    )


def _set_status(record: dict, status: str) -> None:
    """Apply a status transition, tracking busy_since (turn-scoped ramp).

    Thin wrapper: the real logic is switchboard.model.set_status (pure,
    takes `now` explicitly); this supplies the bridge's global clock.
    """
    _model_set_status(record, status, _now())


def update_registry_slot_status(
    registry_path: Path,
    slot: int,
    *,
    status: str | None = None,
    effort: str | None = None,
    activity: str | None = None,
) -> dict | None:
    with registry_transaction(registry_path) as registry:
        record = registry.get("slots", {}).get(str(slot))
        if not record:
            return None
        if status is not None:
            _set_status(record, status)
        if effort is not None:
            record["effort"] = effort
        if activity is not None:
            record["activity"] = activity
        record["last_updated_at"] = _now()
        return record


def agent_update_event(record: dict) -> dict:
    return _model_agent_update_event(record, _clock.now())


# Guards os.write(device_fd, ...) below. Without it, concurrent writers to
# the one shared serial fd — the main listener loop, the liveness ticker
# background thread, and each ThreadingHTTPServer hook-request thread (two
# CLIs can fire lifecycle hooks within milliseconds of each other) — can
# interleave their bytes mid-write. The firmware reads line-by-line, so an
# interleaved write becomes one malformed JSON line that deserializeJson
# silently drops, leaving that key's LED stuck showing whatever color it
# had before (e.g. the initial "launched" blue never advancing to "idle"
# white) until the next status change happens to get through cleanly.
_DEVICE_WRITE_LOCK = threading.Lock()


def write_device_event(fd: int | None, event: dict) -> None:
    if fd is None:
        return
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with _DEVICE_WRITE_LOCK:
        os.write(fd, line.encode("utf-8"))


def write_slot_update(fd: int | None, registry: dict, slot: int | str) -> None:
    record = registry.get("slots", {}).get(str(slot))
    if record:
        write_device_event(fd, agent_update_event(record))
    else:
        # No record means the slot was just freed (session ended, tab closed).
        # Tell the screen so it actually shows empty instead of the last thing
        # it was told — a bare `pop` from the registry alone is invisible to
        # the device.
        write_device_event(fd, agent_update_event({"slot": int(slot), "status": "empty", "activity": "press to launch"}))


def describe_event(event: dict, state: BridgeState) -> str:
    name = event.get("event")
    # Always read the registry fresh: it's also written by the HTTP hook
    # server and the liveness ticker, both on their own threads, so a
    # cached copy here would go stale the moment either of them fires and
    # get pushed to the device on the next button press, repainting a key
    # with an old status.
    registry = load_registry(state.registry_path)

    if name == "agent.select":
        state.selected_slot = int(event.get("slot", 0)) or None
        state.selected_agent = event.get("name")
        if not state.selected_slot:
            return "Selected agent slot is unknown"
        slot_key = str(state.selected_slot)
        existing = registry.get("slots", {}).get(slot_key)

        if existing:
            probe = probe_liveness(existing)
            if probe is Liveness.DEAD:
                if _dead_probe_confirmed(state, slot_key):
                    freed = reconcile_liveness(state, slot_keys=[slot_key])
                    registry = load_registry(state.registry_path)
                    existing = registry.get("slots", {}).get(slot_key)
                    if freed and not existing:
                        pass  # fall through below to (maybe) auto-launch
                else:
                    return f"Slot {state.selected_slot}: liveness unconfirmed, probing again"
            else:
                state.dead_probes.pop(slot_key, None)
                if probe is Liveness.UNKNOWN:
                    # --no-open records (no terminal_tty) are always UNKNOWN,
                    # and never auto-launched here: only hooks/clear can free
                    # them, so re-pressing the key just re-focuses/repaints.
                    if existing.get("terminal_tty"):
                        focus_terminal_tab(existing["terminal_tty"])
                    write_slot_update(state.device_fd, registry, slot_key)
                    return f"Slot {state.selected_slot}: liveness unknown, not relaunching"
                # ALIVE — bring its window to front and repaint, don't relaunch.
                if existing.get("terminal_tty"):
                    focus_terminal_tab(existing["terminal_tty"])
                write_slot_update(state.device_fd, registry, slot_key)
                return f"Selected {registry_slot_summary(registry, slot_key)}"

        if not existing and state.auto_launch:
            message = launch_slot_from_config(
                slot=state.selected_slot,
                config=state.launch_config,
                registry_path=state.registry_path,
                dry_run=state.dry_run,
                no_open=state.no_open,
            )
            registry = load_registry(state.registry_path)
            write_slot_update(state.device_fd, registry, state.selected_slot)
            return message

        write_slot_update(state.device_fd, registry, slot_key)
        return f"Selected {registry_slot_summary(registry, slot_key)}"

    if name == "agent.update.ack":
        return f"Board applied update for Agent {event.get('slot')}"

    if name == "agent.focus":
        slot = int(event.get("slot", state.selected_slot or 0))
        record = registry.get("slots", {}).get(str(slot)) if slot else None
        if record and record.get("terminal_tty"):
            focus_terminal_tab(record["terminal_tty"])
        return f"Focus requested. {registry_slot_summary(registry, slot)}"

    if name == "agent.reasoning.apply":
        slot = int(event.get("slot", state.selected_slot or 0))
        effort = normalize_effort(str(event.get("effort", "medium")))
        if not slot:
            return f"Reasoning effort for agent {slot}: {effort}"

        update_registry_slot_status(state.registry_path, slot, effort=effort)
        registry = load_registry(state.registry_path)
        write_slot_update(state.device_fd, registry, slot)
        return f"Reasoning effort for agent {slot}: {effort} (applies on next launch)"

    if name == "voice.hold.start":
        slot = int(event.get("slot", state.selected_slot or 0))
        if not slot:
            return "Voice hold start: no agent selected"
        record = registry.get("slots", {}).get(str(slot))
        if not record:
            return f"Voice hold start: agent {slot} is not launched"
        family = record.get("family")
        if family not in VOICE_SUPPORTED_FAMILIES:
            return f"Voice hold: agent {slot} is '{family}', voice is Claude-only for now"
        tty_name = record.get("terminal_tty")
        if not tty_name or probe_liveness(record) is Liveness.DEAD:
            return f"Voice hold start: agent {slot} has no open terminal tab"

        # NOTE: we deliberately do NOT auto-type "/voice" here. It's a toggle,
        # and detecting its current on/off state by tailing the terminal log
        # is unreliable — Claude Code's TUI does full-screen ANSI redraws, not
        # plain scrolling text, so a byte-tail search can miss the toggle
        # message and the bridge ends up flipping voice mode the wrong way
        # (confirmed live: it silently disabled voice mode the user had just
        # enabled by hand). So: the user turns /voice on themselves, once per
        # session, and PTT only ever sends the hold/release keypresses.
        focus_terminal_tab(tty_name)

        pid = get_terminal_pid()
        if pid is None:
            return "Voice hold start: could not find Terminal's process id"
        try:
            post_key_event(pid, SPACE_KEYCODE, key_down=True)
        except RuntimeError as exc:
            return f"Voice hold start: {exc}"
        state.mic_active = True
        return f"Voice hold started for agent {slot}"

    if name == "voice.hold.stop":
        slot = int(event.get("slot", state.selected_slot or 0))
        record = registry.get("slots", {}).get(str(slot)) if slot else None
        if not record or record.get("family") not in VOICE_SUPPORTED_FAMILIES:
            state.mic_active = False
            return f"Voice hold stop: agent {slot} is not a voice-capable slot"
        pid = get_terminal_pid()
        if pid is not None:
            try:
                post_key_event(pid, SPACE_KEYCODE, key_down=False)
            except RuntimeError as exc:
                state.mic_active = False
                return f"Voice hold stop: {exc}"
        state.mic_active = False
        return f"Voice hold stopped for agent {slot}"

    if name == "plan.approve":
        return "Approval requested. Bridge did not approve anything."

    if name == "review.request":
        return "Code review requested. Bridge did not start one."

    if name == "slash.run":
        command = event.get("command", "")
        return f"Slash command requested: {command or '(none supplied)'}"

    return f"Unhandled device event: {json.dumps(event, sort_keys=True)}"


def handle_line(line: str, state: BridgeState) -> str | None:
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return f"Ignored malformed JSON: {line}"
    if not isinstance(event, dict):
        return f"Ignored non-object JSON: {line}"
    return describe_event(event, state)


def run_listener(lines: Iterable[str], registry_path: Path, duration: float | None = None) -> int:
    state = BridgeState(registry_path=registry_path)
    started = time.monotonic()
    for line in lines:
        message = handle_line(line, state)
        if message:
            print(message, flush=True)
        if duration is not None and (time.monotonic() - started) >= duration:
            return 0
    return 0


# Indirection so tests can monkeypatch the process-liveness check.
_run = subprocess.run


def probe_liveness(record: dict) -> Liveness:
    """Thin wrapper: the real implementation is liveness.ProcessProber.
    Constructing it fresh on every call (rather than once at import time)
    means `_run`/`os.path.exists` are read at call time, so tests that
    monkeypatch them here keep working unchanged.
    """
    return ProcessProber(run=_run, exists=os.path.exists).probe(record)


def _dead_probe_confirmed(state: BridgeState, slot_key: str) -> bool:
    """Record one DEAD probe for slot_key; True once it has 2 in a row."""
    count = state.dead_probes.get(slot_key, 0) + 1
    if count >= 2:
        state.dead_probes.pop(slot_key, None)
        return True
    state.dead_probes[slot_key] = count
    return False


def reconcile_liveness(
    state: BridgeState,
    *,
    slot_keys: Iterable[str] | None = None,
    require_confirmation: bool = True,
) -> list[str]:
    """Probe registered slots (all of them, or just slot_keys); free any
    that are confirmed dead. Returns the freed slot keys.

    With require_confirmation (the default), a slot is only freed on its
    second consecutive DEAD probe (see _dead_probe_confirmed) — a single
    DEAD reading right after launch, or a transient ps hiccup, shouldn't
    free a slot that's actually fine. require_confirmation=False (used at
    bridge startup, where nothing has been running yet) frees on the first
    DEAD reading.

    Does not push device updates itself — callers do that (see
    _liveness_ticker and the agent.select handling in describe_event), so a
    caller that's about to push a full sync anyway (bridge startup) doesn't
    end up sending two updates for the same freed slot.
    """
    freed: list[str] = []
    with registry_transaction(state.registry_path) as registry:
        slots = registry.get("slots", {})
        keys = list(slot_keys) if slot_keys is not None else list(slots.keys())
        for slot_key in keys:
            record = slots.get(slot_key)
            if not record:
                state.dead_probes.pop(slot_key, None)
                continue
            probe = probe_liveness(record)
            if probe is not Liveness.DEAD:
                state.dead_probes.pop(slot_key, None)
                continue
            if require_confirmation and not _dead_probe_confirmed(state, slot_key):
                continue
            slots.pop(slot_key, None)
            state.dead_probes.pop(slot_key, None)
            freed.append(slot_key)
    return freed


def _liveness_ticker(state: BridgeState, stop_event: threading.Event, interval: float) -> None:
    """Background loop: probe every registered slot's liveness, freeing any
    confirmed-dead one so its LED goes back to empty. This is the only thing
    that ever notices a plain Terminal tab/window close (Cmd+W) — lifecycle
    hooks cover `/exit`, but a hand-closed tab fires no hook at all. Must
    never die: a raised exception here would otherwise silently stop all
    liveness tracking for the rest of the bridge's run.
    """
    while not stop_event.wait(interval):
        try:
            freed = reconcile_liveness(state)
            if freed:
                registry = load_registry(state.registry_path)
                for slot_key in freed:
                    write_slot_update(state.device_fd, registry, slot_key)
        except Exception:  # noqa: BLE001 - must survive to probe again next tick
            traceback.print_exc(file=sys.stderr)


# HOOK_HOST/HOOK_PORT/HOOK_PATH_PREFIX/HOOK_MARKER/CLAUDE_HOOK_EVENTS and the
# install_claude_hooks/install_codex_hooks/_claude_group_is_ours functions
# now live in switchboard.hooks_server / switchboard.hooks_install (imported
# above) — this is still the old in-process HTTP handler (deleted in 1.4).


def _parse_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_agent_id(body: bytes) -> str | None:
    agent_id = _parse_body(body).get("agent_id")
    return str(agent_id) if agent_id else None


class _HookRequestHandler(http.server.BaseHTTPRequestHandler):
    state: BridgeState

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep hook traffic out of the bridge's normal event log

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        event = self.path.rsplit("/", 1)[-1]
        slot_key = (self.headers.get("X-Switchboard-Slot") or "").strip()
        if slot_key:
            push_update = False
            with registry_transaction(self.state.registry_path) as registry:
                record = registry.get("slots", {}).get(slot_key)
                if record:
                    push_update = self._apply_hook_event(registry, slot_key, record, event, body)
            if push_update:
                registry = load_registry(self.state.registry_path)
                write_slot_update(self.state.device_fd, registry, slot_key)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    @staticmethod
    def _apply_hook_event(registry: dict, slot_key: str, record: dict, event: str, body: bytes) -> bool:
        # Guard against a second session (e.g. a relaunch that reused
        # SWITCHBOARD_SLOT before the old one's SessionEnd freed it) sending
        # hooks that would otherwise corrupt this slot's state. The slot's
        # owner is whichever session_id first claims it via SessionStart;
        # hooks from any other session_id are dropped rather than applied.
        payload = _parse_body(body)
        incoming = payload.get("session_id")
        owner = record.get("session_id")
        if event == "SessionStart" and incoming:
            if owner and owner != incoming:
                return False  # a second session claims an owned slot: ignore it
            record["session_id"] = incoming
        elif incoming and owner and incoming != owner:
            return False  # stale/foreign session: ignore

        # A subagent (Task-tool run) can be dispatched to run in the
        # background and keep going after the main turn's Stop event fires —
        # Stop only means "the main turn is done", not "this slot is idle".
        # Without this tracking, a slot would flash to "done"/green (or
        # whatever the next main-thread status is) while a background
        # subagent it just launched is still visibly working. SubagentStart/
        # SubagentStop bracket each subagent run (by agent_id, so duplicate
        # or out-of-order delivery can't double-count) and gate the deferred
        # "done" until every subagent from this turn has actually finished.
        if event in ("SubagentStart", "SubagentStop"):
            agent_id = _extract_agent_id(body)
            if not agent_id:
                return False  # can't track reliably without an id; ignore
            active = set(record.get("active_subagents", []))
            if event == "SubagentStart":
                if agent_id in active:
                    return False
                active.add(agent_id)
                record["active_subagents"] = sorted(active)
                if record.get("status") not in BUSY_STATUSES:
                    _set_status(record, "working")
                return True
            # SubagentStop
            if agent_id not in active:
                return False
            active.discard(agent_id)
            record["active_subagents"] = sorted(active)
            dirty = True
            if not active and record.pop("main_stopped", False):
                # The main turn already finished (Stop already fired) and
                # this was the last subagent still outstanding — deliver the
                # deferred "done" now.
                _set_status(record, "done")
            return dirty

        status = _hook_status_for(record.get("family", ""), event)
        if not status:
            return False
        if status == "empty":
            # SessionEnd/`/exit`: actually free the slot (pop it), not just
            # flip a status field — auto-launch and the slot cells only
            # treat a MISSING record as available.
            registry.get("slots", {}).pop(slot_key, None)
            return True
        dirty = False
        if status == "working" and record.pop("main_stopped", None):
            # A new turn started while an old background subagent was still
            # finishing — that subagent's eventual SubagentStop must not
            # deliver a stale "done" for a slot that's busy again.
            dirty = True
        if status == record.get("status"):
            if event == "UserPromptSubmit":
                # New turn: reset the ramp even if the slot was already busy
                # (e.g. a prompt sent mid-turn), since busy_elapsed_ms
                # measures the current turn, not cumulative busy time. This
                # must happen even when status doesn't change (busy -> busy).
                record["busy_since"] = _now()
                dirty = True
            return dirty
        if status == "done" and record.get("active_subagents"):
            # Defer: a subagent this turn dispatched is still running.
            # SubagentStop will deliver "done" once the last one finishes.
            record["main_stopped"] = True
            return True
        _set_status(record, status)
        if event == "UserPromptSubmit":
            record["busy_since"] = _now()
        record.pop("main_stopped", None)
        return True


def start_hook_server(state: BridgeState) -> http.server.ThreadingHTTPServer | None:
    try:
        bound_handler = type("_BoundHookHandler", (_HookRequestHandler,), {"state": state})
        server = http.server.ThreadingHTTPServer((HOOK_HOST, HOOK_PORT), bound_handler)
    except OSError as exc:
        print(f"Could not start hook listener on {HOOK_HOST}:{HOOK_PORT}: {exc}", file=sys.stderr)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def install_hooks(args: argparse.Namespace) -> int:
    claude_result = install_claude_hooks()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    codex_result = install_codex_hooks()
    print(f"Claude Code hooks (~/.claude/settings.json): {claude_result}")
    print(f"Codex hooks ({codex_home / 'hooks.json'}): {codex_result}")
    if claude_result == "changed":
        print("Claude Code will use the new hooks starting with its next session.")
    if codex_result == "changed":
        print("Codex may report changed hooks on next launch — trust them via /hooks in the session.")
    return 0


def run_bridge_listener(
    *,
    lines: Iterable[str],
    registry_path: Path,
    duration: float | None = None,
    auto_launch: bool = False,
    launch_config_path: Path = DEFAULT_AGENTS_CONFIG,
    dry_run: bool = False,
    no_open: bool = False,
    device_fd: int | None = None,
    liveness_interval: float = 2.0,
) -> int:
    state = BridgeState(
        registry_path=registry_path,
        auto_launch=auto_launch,
        launch_config=load_agents_config(launch_config_path),
        dry_run=dry_run,
        no_open=no_open,
        device_fd=device_fd,
    )
    # Startup: relax the 2-probe confirmation rule (nothing has been running
    # yet, so a single DEAD reading is trustworthy), then push every slot
    # 1..4's status to the board — registered slots get their real status,
    # unregistered ones get "empty" — so a bridge restart always leaves the
    # LEDs in a clean, correct state instead of whatever they last showed.
    reconcile_liveness(state, require_confirmation=False)
    registry = load_registry(state.registry_path)
    for slot in range(1, 5):
        write_slot_update(state.device_fd, registry, slot)

    stop_ticker = threading.Event()
    ticker = threading.Thread(target=_liveness_ticker, args=(state, stop_ticker, liveness_interval), daemon=True)
    ticker.start()
    hook_server = start_hook_server(state)
    started = time.monotonic()
    try:
        for line in lines:
            message = handle_line(line, state)
            if message:
                print(message, flush=True)
            if duration is not None and (time.monotonic() - started) >= duration:
                return 0
        return 0
    finally:
        stop_ticker.set()
        if hook_server is not None:
            hook_server.shutdown()


# The one AppleScriptTerminal instance the not-yet-migrated code below
# (open_terminal/focus_terminal_tab/get_terminal_pid/post_key_event; the
# Bridge in 1.4 takes a TerminalDriver directly instead) drives Terminal.app
# through.
_terminal = AppleScriptTerminal()


def open_terminal(command: str) -> str | None:
    return _terminal.open(command)


def focus_terminal_tab(tty_name: str) -> bool:
    return _terminal.focus(tty_name)


def get_terminal_pid() -> int | None:
    return _terminal.terminal_pid()


def post_key_event(pid: int, keycode: int, key_down: bool) -> None:
    _terminal.post_key(pid, keycode, key_down)


def launch_agent(args: argparse.Namespace) -> int:
    slot = int(args.slot)
    if slot < 1 or slot > 4:
        print("Slots 1 through 4 map to the v1 agent keys.", file=sys.stderr)
        return 2

    existed_before = Path(args.cwd).expanduser().resolve().exists()
    try:
        cwd = _validate_or_create_cwd(args.cwd)
    except ValueError:
        print(
            f"Working folder does not exist: {Path(args.cwd).expanduser().resolve()}. Set it with: "
            f"switchboard_bridge.py config --slot {slot} --cwd PATH",
            file=sys.stderr,
        )
        return 2
    if not existed_before:
        print(f"Created {cwd}")

    try:
        base_command = resolve_command(args.command)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        print("Install the CLI, use an absolute --command path, or use --dry-run to preview.", file=sys.stderr)
        return 2

    effort = normalize_effort(getattr(args, "effort", None))
    command = command_with_effort(base_command, args.family, effort)

    title = args.title or f"Switchboard A{slot} {args.name}"
    shell_command = terminal_command(cwd, command, title, slot=slot)
    # Store the base command (no effort flags) so a later live effort change
    # can rebuild the launch line from scratch instead of stacking flags.
    record = slot_record(
        slot=slot,
        name=args.name,
        family=args.family,
        cwd=cwd,
        command=base_command,
        terminal_title=title,
        effort=effort,
    )

    if args.dry_run:
        if not getattr(args, "quiet", False):
            print(f"Would register {registry_slot_summary({'slots': {str(slot): record}}, slot)}")
            print("Would run:")
            print(shell_command)
        return 0

    if args.no_open:
        with registry_transaction(args.registry) as registry:
            set_registry_slot(registry, record)
        registry = load_registry(args.registry)
        if not getattr(args, "quiet", False):
            print(f"Registered {registry_slot_summary(registry, slot)}")
            print("Terminal launch skipped.")
        return 0

    record["terminal_tty"] = open_terminal(shell_command)
    with registry_transaction(args.registry) as registry:
        set_registry_slot(registry, record)
    registry = load_registry(args.registry)
    if not getattr(args, "quiet", False):
        print(f"Launched and registered {registry_slot_summary(registry, slot)}")
    return 0


def launch_all(args: argparse.Namespace) -> int:
    config = load_agents_config(Path(args.config))
    agents = config.get("agents", [])
    if not isinstance(agents, list):
        print("Config must contain an agents list.", file=sys.stderr)
        return 2
    for agent in agents:
        message = launch_slot_from_config(
            slot=int(agent["slot"]),
            config=config,
            registry_path=args.registry,
            dry_run=args.dry_run,
            no_open=args.no_open,
        )
        print(message)
        if "launch failed" in message:
            return 2
    return 0


def list_slots(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    slots = registry.get("slots", {})
    if not slots:
        print("No agent slots are registered yet.")
        return 0
    for slot in sorted(slots, key=lambda s: int(s)):
        print(registry_slot_summary(registry, slot))
    return 0


def set_status(args: argparse.Namespace) -> int:
    record = update_registry_slot_status(
        args.registry,
        args.slot,
        status=args.status,
        effort=args.effort,
        activity=args.activity,
    )
    if not record:
        print(f"Slot {args.slot} is not registered yet.", file=sys.stderr)
        return 2
    print(registry_slot_summary(load_registry(args.registry), args.slot))
    return 0


def clear_slot(args: argparse.Namespace) -> int:
    with registry_transaction(args.registry) as registry:
        removed = registry.get("slots", {}).pop(str(args.slot), None)
    if removed:
        print(f"Cleared Agent {args.slot}")
    else:
        print(f"Agent {args.slot} was already empty")
    return 0


def _write_agents_config(path: Path, config: dict) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def config_command(args: argparse.Namespace) -> int:
    agents_config_path = Path(args.agents_config).expanduser()
    if agents_config_path.exists():
        config = load_agents_config(agents_config_path)
    elif EXAMPLE_AGENTS_CONFIG.exists():
        config = load_agents_config(EXAMPLE_AGENTS_CONFIG)
    else:
        config = {"agents": []}

    if args.show:
        print(json.dumps(config, indent=2, sort_keys=True))
        return 0

    if args.default_cwd is not None:
        try:
            resolved = _validate_or_create_cwd(args.default_cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        config.setdefault("defaults", {})["cwd"] = resolved
        _write_agents_config(agents_config_path, config)
        print(f"Default cwd set to {resolved}.")
        print("Takes effect on each slot's next launch.")
        return 0

    if args.slot is None:
        print("Specify --slot, --default-cwd, or --show.", file=sys.stderr)
        return 2

    agents = config.setdefault("agents", [])
    agent = next((a for a in agents if int(a.get("slot", 0)) == args.slot), None)
    if agent is None:
        agent = {"slot": args.slot}
        agents.append(agent)

    if args.cwd is not None:
        try:
            agent["cwd"] = _validate_or_create_cwd(args.cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.name is not None:
        agent["name"] = args.name
    if args.family is not None:
        agent["family"] = args.family
    if args.command is not None:
        agent["command"] = args.command
    if args.effort is not None:
        agent["effort"] = normalize_effort(args.effort)

    _write_agents_config(agents_config_path, config)
    print(
        f"Slot {args.slot}: {agent.get('name', f'Agent {args.slot}')} "
        f"({agent.get('family', 'codex')}) command={agent.get('command', 'codex')!r} "
        f"cwd={resolve_agent_cwd(agent, config)}"
    )
    print("Takes effect on this slot's next launch.")
    return 0


def sync_device(args: argparse.Namespace) -> int:
    if getattr(args, "slot", None) is None and getattr(args, "slot_pos", None) is not None:
        args.slot = args.slot_pos

    port = args.port or find_default_port()
    if not port:
        print("No USB serial port found. Plug in the board or pass --port.", file=sys.stderr)
        return 2
    registry = load_registry(args.registry)
    fd = open_serial(port, args.baud)
    try:
        slots = registry.get("slots", {})
        expected_acks = 0
        if args.slot:
            if not slots.get(str(args.slot)):
                print(f"Agent {args.slot} is not registered; nothing to sync.", file=sys.stderr)
                return 2
            write_slot_update(fd, registry, args.slot)
            expected_acks = 1
            print(f"Sent Agent {args.slot} status to board")
        else:
            for slot in sorted(slots, key=lambda s: int(s)):
                write_slot_update(fd, registry, slot)
                expected_acks += 1
            print(f"Sent {len(slots)} registered agent slot(s) to board")

        seen_acks = 0
        deadline = time.monotonic() + 1.0
        buffer = b""
        while expected_acks and seen_acks < expected_acks and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 1024)
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded.startswith("{"):
                    continue
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "agent.update.ack":
                    seen_acks += 1
                    print(f"Board applied update for Agent {event.get('slot')}")

        if expected_acks and seen_acks == 0:
            print("No board acknowledgement received; the screen may not have updated.", file=sys.stderr)
            return 1
    finally:
        os.close(fd)
    return 0


def listen(args: argparse.Namespace) -> int:
    if args.sample:
        sample = HOST_DIR / "sample_events.jsonl"
        with sample.open("r", encoding="utf-8") as handle:
            return run_bridge_listener(
                lines=file_lines(handle),
                registry_path=args.registry,
                duration=args.duration,
                auto_launch=args.auto_launch,
                launch_config_path=args.launch_config,
                dry_run=args.dry_run,
                no_open=args.no_open,
                device_fd=None,
            )

    if args.stdin:
        return run_bridge_listener(
            lines=file_lines(sys.stdin),
            registry_path=args.registry,
            duration=args.duration,
            auto_launch=args.auto_launch,
            launch_config_path=args.launch_config,
            dry_run=args.dry_run,
            no_open=args.no_open,
            device_fd=None,
        )

    while True:
        port = args.port or find_default_port()
        if not port:
            if not args.retry:
                print("No USB serial port found. Plug in the board or pass --port.", file=sys.stderr)
                return 2
            print("No USB serial port found. Waiting for Switchboard...", flush=True)
            time.sleep(args.retry_delay)
            continue

        print(f"Listening on {port} at {args.baud} baud. Press Ctrl+C to stop.", flush=True)
        try:
            fd = open_serial(port, args.baud)
        except OSError as exc:
            if not args.retry:
                print(f"Could not open {port}: {exc}", file=sys.stderr)
                return 2
            print(f"Could not open {port}: {exc}. Retrying...", flush=True)
            time.sleep(args.retry_delay)
            continue

        try:
            return run_bridge_listener(
                lines=serial_lines(fd, args.duration),
                registry_path=args.registry,
                duration=None,
                auto_launch=args.auto_launch,
                launch_config_path=args.launch_config,
                dry_run=args.dry_run,
                no_open=args.no_open,
                device_fd=fd,
            )
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except OSError as exc:
            if not args.retry:
                raise
            print(f"Serial connection lost: {exc}. Waiting for reconnect...", flush=True)
            time.sleep(args.retry_delay)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Switchboard host bridge.")
    parser.set_defaults(command_name="listen", func=listen)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--port", help="Serial port, for example /dev/cu.usbmodem2301")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--duration", type=float, help="Stop listening after this many seconds")
    parser.add_argument("--sample", action="store_true", help="Read sample events instead of serial")
    parser.add_argument("--stdin", action="store_true", help="Read events from standard input")
    parser.add_argument("--auto-launch", action="store_true", help="Launch/register an empty slot when its agent key is pressed")
    parser.add_argument("--launch-config", type=Path, default=DEFAULT_AGENTS_CONFIG, help="Agent launch config")
    parser.add_argument("--dry-run", action="store_true", help="Preview auto-launch actions without opening terminals or writing the registry")
    parser.add_argument("--no-open", action="store_true", help="Register auto-launched slots without opening Terminal")
    parser.add_argument("--retry", action="store_true", help="Keep waiting when the board is not connected yet")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between reconnect attempts")

    subcommands = parser.add_subparsers(dest="command_name")

    listen_parser = subcommands.add_parser("listen", help="Listen to board events")
    listen_parser.set_defaults(func=listen)
    listen_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    listen_parser.add_argument("--port", help="Serial port, for example /dev/cu.usbmodem2301")
    listen_parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    listen_parser.add_argument("--duration", type=float, help="Stop listening after this many seconds")
    listen_parser.add_argument("--sample", action="store_true", help="Read sample events instead of serial")
    listen_parser.add_argument("--stdin", action="store_true", help="Read events from standard input")
    listen_parser.add_argument("--auto-launch", action="store_true", help="Launch/register an empty slot when its agent key is pressed")
    listen_parser.add_argument("--launch-config", type=Path, default=DEFAULT_AGENTS_CONFIG, help="Agent launch config")
    listen_parser.add_argument("--dry-run", action="store_true", help="Preview auto-launch actions without opening terminals or writing the registry")
    listen_parser.add_argument("--no-open", action="store_true", help="Register auto-launched slots without opening Terminal")
    listen_parser.add_argument("--retry", action="store_true", help="Keep waiting when the board is not connected yet")
    listen_parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between reconnect attempts")

    launch_parser = subcommands.add_parser("launch", help="Launch/register one agent slot")
    launch_parser.set_defaults(func=launch_agent)
    launch_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    launch_parser.add_argument("--slot", type=int, required=True)
    launch_parser.add_argument("--name", required=True)
    launch_parser.add_argument(
        "--family", default="codex", choices=("codex", "claude", "shell"),
        help="'shell' is a no-cost test family (no effort flags added) for a harmless command like cat",
    )
    launch_parser.add_argument("--cwd", default=DEFAULT_CWD)
    launch_parser.add_argument("--command", default="codex")
    launch_parser.add_argument("--title")
    launch_parser.add_argument("--effort", default="medium", help="low, medium, high, xhigh, or max")
    launch_parser.add_argument("--dry-run", action="store_true")
    launch_parser.add_argument("--no-open", action="store_true")

    launch_all_parser = subcommands.add_parser("launch-all", help="Launch/register agents from a config file")
    launch_all_parser.set_defaults(func=launch_all)
    launch_all_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    launch_all_parser.add_argument("--config", default=str(HOST_DIR / "agents.example.json"))
    launch_all_parser.add_argument("--dry-run", action="store_true")
    launch_all_parser.add_argument("--no-open", action="store_true")

    slots_parser = subcommands.add_parser("slots", help="Show registered agent slots")
    slots_parser.set_defaults(func=list_slots)
    slots_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)

    status_parser = subcommands.add_parser("status", help="Update one registered agent slot")
    status_parser.set_defaults(func=set_status)
    status_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    status_parser.add_argument("--slot", type=int, required=True)
    status_parser.add_argument("--status", choices=STATUS_CHOICES)
    status_parser.add_argument("--effort")
    status_parser.add_argument("--activity")

    clear_parser = subcommands.add_parser("clear", help="Clear one registered agent slot")
    clear_parser.set_defaults(func=clear_slot)
    clear_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    clear_parser.add_argument("--slot", type=int, required=True)

    config_parser = subcommands.add_parser("config", help="Edit host/agents.json (per-slot launch config)")
    config_parser.set_defaults(func=config_command)
    config_parser.add_argument("--agents-config", type=Path, default=USER_AGENTS_CONFIG)
    config_parser.add_argument("--slot", type=int)
    config_parser.add_argument("--cwd")
    config_parser.add_argument("--name")
    config_parser.add_argument("--family", choices=("codex", "claude", "shell"))
    config_parser.add_argument("--command")
    config_parser.add_argument("--effort", help="low, medium, high, xhigh, or max")
    config_parser.add_argument("--default-cwd")
    config_parser.add_argument("--show", action="store_true")

    hooks_parser = subcommands.add_parser(
        "install-hooks",
        help="Register Switchboard lifecycle hooks with Claude Code and Codex (real status auto-detection)",
    )
    hooks_parser.set_defaults(func=install_hooks)

    sync_parser = subcommands.add_parser("sync", help="Send registered slot status to the board")
    sync_parser.set_defaults(func=sync_device)
    sync_parser.add_argument("--registry", type=Path, default=argparse.SUPPRESS)
    sync_parser.add_argument("--port", help="Serial port, for example /dev/cu.usbmodem2301")
    sync_parser.add_argument("--baud", type=int, default=115200)
    sync_parser.add_argument("slot_pos", nargs="?", type=int, help="Optional slot number to sync")
    sync_parser.add_argument("--slot", type=int, help="Sync one slot instead of all registered slots")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
