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
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, TextIO

HOST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HOST_DIR.parent
sys.path.insert(0, str(HOST_DIR))  # so `import switchboard` works regardless of cwd

from switchboard.model import (  # noqa: E402 - needs HOST_DIR on sys.path first
    STATUS_CHOICES,
    Liveness,
    command_with_effort,
    normalize_effort,
)
from switchboard.model import agent_update_event as _model_agent_update_event
from switchboard.model import set_status as _model_set_status
from switchboard.model import slot_record as _model_slot_record
from switchboard.registry import Registry
from switchboard.device import find_default_port, FdDevice, FileDevice, SerialDevice
from switchboard.liveness import ProcessProber
from switchboard.clock import SystemClock
from switchboard.terminal import AppleScriptTerminal, NullTerminal
from switchboard.launcher import (
    DEFAULT_CWD,
    agent_config_for_slot,
    load_agents_config,
    resolve_agent_cwd,
    resolve_command,
    terminal_command,
    validate_or_create_cwd as _validate_or_create_cwd,
)
from switchboard.hooks_install import install_claude_hooks, install_codex_hooks

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


# Indirection so tests can monkeypatch the process-liveness check.
_run = subprocess.run


def probe_liveness(record: dict) -> Liveness:
    """Thin wrapper: the real implementation is liveness.ProcessProber.
    Constructing it fresh on every call (rather than once at import time)
    means `_run`/`os.path.exists` are read at call time, so tests that
    monkeypatch them here keep working unchanged.
    """
    return ProcessProber(run=_run, exists=os.path.exists).probe(record)


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
    slots = registry.get("slots", {})
    if args.slot and not slots.get(str(args.slot)):
        print(f"Agent {args.slot} is not registered; nothing to sync.", file=sys.stderr)
        return 2
    targets = [str(args.slot)] if args.slot else sorted(slots, key=lambda s: int(s))

    device = SerialDevice(port, args.baud)
    try:
        for slot_key in targets:
            device.send(_model_agent_update_event(slots[slot_key], _clock.now()))
        if args.slot:
            print(f"Sent Agent {args.slot} status to board")
        else:
            print(f"Sent {len(targets)} registered agent slot(s) to board")

        expected_acks = len(targets)
        seen_acks = 0
        for line in device.lines(duration=1.0):
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "agent.update.ack":
                seen_acks += 1
                print(f"Board applied update for Agent {event.get('slot')}")
                if seen_acks >= expected_acks:
                    break

        if expected_acks and seen_acks == 0:
            print("No board acknowledgement received; the screen may not have updated.", file=sys.stderr)
            return 1
    finally:
        device.close()
    return 0


def _make_bridge(args: argparse.Namespace, device) -> "Bridge":
    from switchboard.bridge import Bridge

    terminal = NullTerminal() if (args.no_open or args.dry_run) else _terminal
    return Bridge(
        registry=Registry(args.registry),
        device=device,
        terminal=terminal,
        prober=ProcessProber(),
        clock=SystemClock(),
        launch_config=load_agents_config(args.launch_config),
        auto_launch=args.auto_launch,
        dry_run=args.dry_run,
        no_open=args.no_open,
    )


def listen(args: argparse.Namespace) -> int:
    if args.sample:
        sample = HOST_DIR / "sample_events.jsonl"
        with sample.open("r", encoding="utf-8") as handle:
            bridge = _make_bridge(args, FileDevice(handle))
            bridge.startup_sync()
            return bridge.run(duration=args.duration)

    if args.stdin:
        bridge = _make_bridge(args, FdDevice(sys.stdin.fileno(), write_fd=None))
        bridge.startup_sync()
        return bridge.run(duration=args.duration)

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
            device = SerialDevice(port, args.baud)
        except (OSError, RuntimeError) as exc:
            if not args.retry:
                print(f"Could not open {port}: {exc}", file=sys.stderr)
                return 2
            print(f"Could not open {port}: {exc}. Retrying...", flush=True)
            time.sleep(args.retry_delay)
            continue

        bridge = _make_bridge(args, device)
        bridge.startup_sync()
        try:
            result = bridge.run(duration=args.duration)
            if bridge.last_shutdown_error is None:
                return result
            if not args.retry:
                raise bridge.last_shutdown_error
            print(f"Serial connection lost: {bridge.last_shutdown_error}. Waiting for reconnect...", flush=True)
            time.sleep(args.retry_delay)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        finally:
            device.close()


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
