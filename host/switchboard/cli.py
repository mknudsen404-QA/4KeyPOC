"""Switchboard host bridge CLI.

Subcommands build the real objects (Registry, a DeviceLink, a
TerminalDriver, ProcessProber, SystemClock) and either call into
switchboard.bridge.Bridge (`listen`) or act on the registry/agents config
directly (`launch`, `launch-all`, `slots`, `status`, `clear`, `config`,
`install-hooks`, `sync`).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from switchboard.clock import SystemClock
from switchboard.device import FdDevice, FileDevice, SerialDevice, find_default_port, sync_device
from switchboard.doctor import doctor_command
from switchboard.hooks_install import install_hooks
from switchboard.key_injector import FakeKeyInjector, RepeatingKeyInjector, macos_key_repeat_timing
from switchboard.launcher import (
    DEFAULT_AGENTS_CONFIG,
    DEFAULT_CWD,
    EXAMPLE_AGENTS_CONFIG,
    HOST_DIR,
    USER_AGENTS_CONFIG,
    agent_config_for_slot,
    build_launch,
    config_command,
    load_agents_config,
    resolve_agent_cwd,
)
from switchboard.liveness import ProcessProber
from switchboard.model import STATUS_CHOICES
from switchboard.model import set_status as _model_set_status
from switchboard.model import slot_record
from switchboard.registry import Registry
from switchboard.terminal import AppleScriptTerminal, NullTerminal

DEFAULT_REGISTRY = HOST_DIR / "agent_registry.json"

_clock = SystemClock()
_terminal = AppleScriptTerminal()


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


def update_registry_slot_status(
    registry_path: Path,
    slot: int,
    *,
    status: str | None = None,
    effort: str | None = None,
    activity: str | None = None,
) -> dict | None:
    with Registry(registry_path).transaction() as registry:
        record = registry.get("slots", {}).get(str(slot))
        if not record:
            return None
        now = int(_clock.now())
        if status is not None:
            _model_set_status(record, status, now)
        if effort is not None:
            record["effort"] = effort
        if activity is not None:
            record["activity"] = activity
        record["last_updated_at"] = now
        return record


def launch_agent(args: argparse.Namespace) -> int:
    slot = int(args.slot)
    if slot < 1 or slot > 4:
        print("Slots 1 through 4 map to the v1 agent keys.", file=sys.stderr)
        return 2

    overrides = {"name": args.name, "family": args.family, "cwd": args.cwd, "command": args.command, "title": args.title}
    try:
        plan = build_launch(slot, None, effort=args.effort, overrides=overrides)
    except ValueError as exc:
        print(
            f"{exc}. Set it with: switchboard_bridge.py config --slot {slot} --cwd PATH",
            file=sys.stderr,
        )
        return 2
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print("Install the CLI, use an absolute --command path, or use --dry-run to preview.", file=sys.stderr)
        return 2

    quiet = getattr(args, "quiet", False)
    now = int(_clock.now())
    if args.dry_run:
        record = {**plan.record_fields, "status": "launched"}
        if not quiet:
            print(f"Would register {registry_slot_summary({'slots': {str(slot): record}}, slot)}")
            print("Would run:")
            print(plan.shell_command)
        return 0

    terminal_tty = None if args.no_open else _terminal.open(plan.shell_command)
    record = slot_record(**plan.record_fields, terminal_tty=terminal_tty, now=now)
    with Registry(args.registry).transaction() as registry:
        registry.setdefault("slots", {})[str(slot)] = record
    registry = Registry(args.registry).load()
    if not quiet:
        verb = "Registered" if args.no_open else "Launched and registered"
        print(f"{verb} {registry_slot_summary(registry, slot)}")
        if args.no_open:
            print("Terminal launch skipped.")
    return 0


def _launch_slot_from_config(*, slot: int, config: dict | None, registry_path: Path, dry_run: bool, no_open: bool) -> str:
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


def launch_all(args: argparse.Namespace) -> int:
    config = load_agents_config(Path(args.config))
    agents = config.get("agents", [])
    if not isinstance(agents, list):
        print("Config must contain an agents list.", file=sys.stderr)
        return 2
    for agent in agents:
        message = _launch_slot_from_config(
            slot=int(agent["slot"]), config=config, registry_path=args.registry, dry_run=args.dry_run, no_open=args.no_open
        )
        print(message)
        if "launch failed" in message:
            return 2
    return 0


def list_slots(args: argparse.Namespace) -> int:
    registry = Registry(args.registry).load()
    slots = registry.get("slots", {})
    if not slots:
        print("No agent slots are registered yet.")
        return 0
    for slot in sorted(slots, key=lambda s: int(s)):
        print(registry_slot_summary(registry, slot))
    return 0


def set_status(args: argparse.Namespace) -> int:  # noqa: F811 - CLI action, distinct from model.set_status
    record = update_registry_slot_status(args.registry, args.slot, status=args.status, effort=args.effort, activity=args.activity)
    if not record:
        print(f"Slot {args.slot} is not registered yet.", file=sys.stderr)
        return 2
    print(registry_slot_summary(Registry(args.registry).load(), args.slot))
    return 0


def clear_slot(args: argparse.Namespace) -> int:
    with Registry(args.registry).transaction() as registry:
        removed = registry.get("slots", {}).pop(str(args.slot), None)
    if removed:
        print(f"Cleared Agent {args.slot}")
    else:
        print(f"Agent {args.slot} was already empty")
    return 0


def _make_bridge(args: argparse.Namespace, device):
    from switchboard.bridge import Bridge, _default_log

    terminal = NullTerminal() if (args.no_open or args.dry_run) else _terminal
    if isinstance(terminal, NullTerminal):
        key_injector = FakeKeyInjector()
    else:
        initial_delay, repeat_interval = macos_key_repeat_timing()
        key_injector = RepeatingKeyInjector(
            terminal.post_key, initial_delay=initial_delay, repeat_interval=repeat_interval, log=_default_log
        )
    return Bridge(
        registry=Registry(args.registry),
        device=device,
        terminal=terminal,
        key_injector=key_injector,
        # flush=True: `listen` runs indefinitely with stdout redirected to
        # a plain file under the LaunchAgent (bridge.out.log, not a tty),
        # which CPython fully block-buffers by default — bare `print`
        # alone can leave diagnostic lines invisible on disk for
        # arbitrarily long. Bridge's own default logger already does this;
        # ProcessProber's is wired separately since it's constructed here.
        prober=ProcessProber(log=_default_log),
        clock=SystemClock(),
        launch_config=load_agents_config(args.launch_config),
        auto_launch=args.auto_launch,
        dry_run=args.dry_run,
        no_open=args.no_open,
        close_dead_tabs=getattr(args, "close_dead_tabs", False),
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


def _add_listen_flags(parser: argparse.ArgumentParser) -> None:
    """The flags `listen` needs, shared with the root parser (running the
    bridge with no subcommand at all defaults to `listen`) so `--help`
    output for `listen` lists each one exactly once regardless of which
    parser it's attached to.
    """
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
    parser.add_argument(
        "--close-dead-tabs", action="store_true",
        help="Close a slot's Terminal tab when its process is confirmed dead (default: leave it)",
    )
    parser.add_argument("--retry", action="store_true", help="Keep waiting when the board is not connected yet")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between reconnect attempts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Switchboard host bridge.")
    parser.set_defaults(command_name="listen", func=listen)
    _add_listen_flags(parser)

    subcommands = parser.add_subparsers(dest="command_name")

    listen_parser = subcommands.add_parser("listen", help="Listen to board events")
    listen_parser.set_defaults(func=listen)
    _add_listen_flags(listen_parser)

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

    doctor_parser = subcommands.add_parser("doctor", help="Probe the bridge's operating environment and report health")
    doctor_parser.set_defaults(func=doctor_command)
    doctor_parser.add_argument("--port", help="Serial port to probe, for example /dev/cu.usbmodem2301")
    doctor_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    doctor_parser.add_argument("--agents-config", type=Path, default=DEFAULT_AGENTS_CONFIG)
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text lines")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
