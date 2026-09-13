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
import enum
import fcntl
import glob
import http.server
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
import traceback
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

try:
    import Quartz  # type: ignore[import]

    QUARTZ_AVAILABLE = True
except ImportError:
    QUARTZ_AVAILABLE = False


HOST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HOST_DIR.parent
DEFAULT_REGISTRY = HOST_DIR / "agent_registry.json"
# agents.json is the user's own per-slot config (which CLI family/command each
# agent key launches). It is personal/local, not checked in. agents.example.json
# is the checked-in template; it's only used as a fallback until agents.json
# exists, so first-time setup still works with no configuration.
USER_AGENTS_CONFIG = HOST_DIR / "agents.json"
EXAMPLE_AGENTS_CONFIG = HOST_DIR / "agents.example.json"
DEFAULT_AGENTS_CONFIG = USER_AGENTS_CONFIG if USER_AGENTS_CONFIG.exists() else EXAMPLE_AGENTS_CONFIG
STATUS_CHOICES = ("empty", "launched", "idle", "thinking", "working", "waiting", "needs_input", "blocked", "done")
# Statuses long enough to be worth escalating the LED for — the ones that
# just mean "still going," not the ones that already stand out on their own
# (needs_input/blocked are already flagged, done/idle aren't "busy").
BUSY_STATUSES = ("working", "thinking")
# macOS virtual keycode for the spacebar (used to drive Claude Code's /voice
# hold-to-record mode). Voice hold is Claude-only for now — Codex's /voice
# support, if any, hasn't been scoped.
SPACE_KEYCODE = 49
VOICE_SUPPORTED_FAMILIES = ("claude",)
KNOWN_COMMAND_PATHS = {
    "codex": [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ],
    "claude": [
        str(Path.home() / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ],
}

# The device's effort dial sends LOW/MED/HIGH/XHIGH/MAX (lowercased by the
# bridge before storage). Neither CLI's effort vocabulary matches ours
# exactly, so normalize first, then map per family.
# Disabled: a live session was observed with only some Ctrl-C interrupts
# actually landing (real AI TUIs appear to swallow Ctrl-C while a turn is
# actively generating, only honoring it at idle) — a failed interrupt still
# types the relaunch shell command into the tab, which becomes a literal
# message inside the live conversation. Re-enable only after a reliable
# "did the interrupt actually land" check is added (e.g. reading the tab
# back via a proper terminal emulator instead of guessing after a fixed
# delay). Until then, effort changes only update the registry/screen.
LIVE_EFFORT_RESTART_ENABLED = False

EFFORT_ALIASES = {"med": "medium"}
CLAUDE_EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}
# codex's `-c model_reasoning_effort=` only has "low" confirmed against a real
# config.toml on this machine; medium/high are the standard OpenAI tiers and
# very likely valid, but xhigh/max are not confirmed for this specific key
# (a different key, multi_agent_reasoning_effort, does accept "xhigh") so they
# are clamped to "high" rather than risk passing an unrecognized value at
# launch. Revisit once a real codex session confirms the top tiers.
CODEX_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

BAUD_RATES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}

for _rate_name, _rate_value in (
    (230400, "B230400"),
    (460800, "B460800"),
    (921600, "B921600"),
):
    if hasattr(termios, _rate_value):
        BAUD_RATES[_rate_name] = getattr(termios, _rate_value)


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
    if not path.exists():
        return {"version": 1, "slots": {}}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Registry is not an object: {path}")
    data.setdefault("version", 1)
    data.setdefault("slots", {})
    return data


def save_registry(registry: dict, path: Path = DEFAULT_REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


_REGISTRY_LOCK = threading.RLock()


@contextlib.contextmanager
def registry_transaction(path: Path = DEFAULT_REGISTRY):
    """Exclusive load->mutate->save. Holds the in-process lock and an
    fcntl.flock on <path>.lock so the `status`/`clear`/`config` CLI (separate
    processes) can't interleave with the running bridge.
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


def normalize_effort(effort: str | None) -> str:
    value = (effort or "medium").strip().lower()
    return EFFORT_ALIASES.get(value, value)


def effort_args(family: str, effort: str | None) -> list[str]:
    normalized = normalize_effort(effort)
    if family == "claude":
        value = normalized if normalized in CLAUDE_EFFORT_VALUES else "medium"
        return ["--effort", value]
    if family == "codex":
        value = CODEX_EFFORT_MAP.get(normalized, "medium")
        return ["-c", f"model_reasoning_effort={value}"]
    return []


def command_with_effort(base_command: str, family: str, effort: str | None) -> str:
    extra_args = effort_args(family, effort)
    if not extra_args:
        return base_command
    return f"{base_command} {shlex.join(extra_args)}"


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
    return {
        "slot": slot,
        "name": name,
        "family": family,
        "cwd": cwd,
        "command": command,
        "terminal_title": terminal_title,
        "terminal_tty": terminal_tty,
        "status": "launched",
        "effort": normalize_effort(effort),
        "activity": "terminal launched",
        "last_launched_at": _now(),
    }


def set_registry_slot(registry: dict, record: dict) -> None:
    registry.setdefault("slots", {})[str(record["slot"])] = record


def load_agents_config(path: Path = DEFAULT_AGENTS_CONFIG) -> dict:
    if not path.exists():
        return {"agents": []}
    with path.expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Agent config is not an object: {path}")
    data.setdefault("agents", [])
    return data


DEFAULT_CWD = "~/Documents"


def agent_config_for_slot(config: dict | None, slot: int) -> dict:
    agents = (config or {}).get("agents", [])
    for agent in agents:
        if int(agent.get("slot", 0)) == slot:
            return dict(agent)
    return {
        "slot": slot,
        "name": f"Agent {slot}",
        "family": "codex",
        "command": "codex",
    }


def resolve_agent_cwd(agent: dict, config: dict | None) -> str:
    """Resolution order: slot cwd -> defaults.cwd -> ~/Documents."""
    cwd = agent.get("cwd") or (config or {}).get("defaults", {}).get("cwd") or DEFAULT_CWD
    return str(Path(cwd).expanduser().resolve())


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


def find_default_port() -> str | None:
    candidates: list[str] = []
    for pattern in (
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.SLAB_USBtoUART*",
    ):
        candidates.extend(glob.glob(pattern))
    return sorted(candidates)[0] if candidates else None


def configure_serial(fd: int, baud: int) -> None:
    if baud not in BAUD_RATES:
        supported = ", ".join(str(rate) for rate in sorted(BAUD_RATES))
        raise ValueError(f"Unsupported baud rate {baud}. Supported: {supported}")

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = BAUD_RATES[baud]
    attrs[5] = BAUD_RATES[baud]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    tty.setraw(fd)


def open_serial(port: str, baud: int) -> int:
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        # Exclusive access: without this, a second process (a stray `cat`,
        # another bridge instance, a leftover diagnostic session) can open
        # the same tty at the same time with no error from either side —
        # the OS just splits incoming bytes unpredictably between readers.
        # That happened for real: a diagnostic `cat` outlived its intended
        # lifetime and silently starved the bridge of every board event for
        # a good chunk of a debugging session before anyone noticed. Fail
        # loudly instead.
        fcntl.ioctl(fd, termios.TIOCEXCL)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(
            f"{port} is already open by another process (found while claiming exclusive "
            f"access). Close whatever else has it — `lsof {port}` will show you what — "
            "before starting the bridge."
        ) from exc
    configure_serial(fd, baud)
    return fd


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

    Every status assignment goes through this instead of writing
    record["status"] directly, so busy_since starts the moment a slot
    becomes busy and is cleared the moment it stops being busy.
    """
    was_busy = record.get("status") in BUSY_STATUSES
    record["status"] = status
    record["last_updated_at"] = _now()
    now_busy = status in BUSY_STATUSES
    if now_busy and not was_busy:
        record["busy_since"] = _now()
    elif not now_busy:
        record.pop("busy_since", None)


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
    status = record.get("status", "empty")
    busy_elapsed_ms = 0
    if status in BUSY_STATUSES:
        busy_since = record.get("busy_since")
        if busy_since:
            busy_elapsed_ms = max(0, round((_clock.now() - int(busy_since)) * 1000))
    return {
        "event": "agent.update",
        "slot": record.get("slot"),
        "name": record.get("name", f"Agent {record.get('slot', '')}"),
        "family": record.get("family", "slot"),
        "status": status,
        "effort": record.get("effort", "medium"),
        "activity": record.get("activity", ""),
        "busy_elapsed_ms": busy_elapsed_ms,
    }


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

        record = registry.get("slots", {}).get(str(slot))
        pushed = False
        if LIVE_EFFORT_RESTART_ENABLED and record and record.get("terminal_tty"):
            if probe_liveness(record) is Liveness.ALIVE:
                new_command = command_with_effort(record["command"], record["family"], effort)
                cwd = record.get("cwd", str(PROJECT_ROOT))
                title = record.get("terminal_title", f"Switchboard A{slot}")
                new_shell_command = terminal_command(cwd, new_command, title, slot=slot)
                pushed = restart_in_slot_terminal(record["terminal_tty"], new_shell_command)
            else:
                # Tab was closed by hand since launch; stop treating it as live.
                update_registry_slot_status(state.registry_path, slot, activity="terminal window closed")

        # Effort is its own field on the device (top-right of the screen);
        # it doesn't need to also occupy the activity line.
        update_registry_slot_status(state.registry_path, slot, effort=effort)
        state.effort_by_slot[slot] = effort
        registry = load_registry(state.registry_path)
        write_slot_update(state.device_fd, registry, slot)
        if pushed:
            return f"Reasoning effort for agent {slot}: {effort} (restarted live session)"
        return f"Reasoning effort for agent {slot}: {effort} (no live terminal to update)"

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


class Liveness(enum.Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


_SHELL_COMMS = {"login", "-zsh", "zsh", "-bash", "bash", "sh", "-sh", "fish", "-fish"}

# Indirection so tests can monkeypatch the process-liveness check.
_run = subprocess.run


def probe_liveness(record: dict) -> Liveness:
    """Is the process behind this slot's terminal still running?

    UNKNOWN covers both "we can't tell" (a ps failure/timeout) and
    "there's nothing to check" (a --no-open record has no terminal_tty at
    all) — in both cases the bridge can't safely conclude DEAD, so only
    hooks or an explicit `clear` can free the slot.
    """
    tty_path = record.get("terminal_tty")
    if not tty_path:
        return Liveness.UNKNOWN
    if not os.path.exists(tty_path):
        return Liveness.DEAD  # tab closed: the pty node is gone
    try:
        result = _run(
            ["ps", "-o", "comm=", "-t", os.path.basename(tty_path)],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Liveness.UNKNOWN
    if result.returncode not in (0, 1):
        return Liveness.UNKNOWN
    comms = {line.strip().rsplit("/", 1)[-1] for line in result.stdout.splitlines() if line.strip()}
    return Liveness.ALIVE if (comms - _SHELL_COMMS) else Liveness.DEAD


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


HOOK_HOST = "127.0.0.1"
HOOK_PORT = 8877
HOOK_PATH_PREFIX = "/switchboard-hook/"
# Substring used to identify Switchboard's own entries in ~/.claude/settings.json
# and $CODEX_HOME/hooks.json so re-installing (or another tool's installer) never
# clobbers unrelated hooks. Distinct from OpenMicro's /om-hook/ and vibesense's
# /hook/ markers, so all three can coexist.
HOOK_MARKER = f"{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}"

# Real lifecycle hook events, not screen-scraped guesses — see
# https://github.com/stephenleo/OpenMicro, which validated this approach.
# PreToolUse only fires for AskUserQuestion (see CLAUDE_HOOK_EVENTS' matcher).
CLAUDE_HOOK_STATUS = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "needs_input",
    "PostToolUse": "working",
    "Notification": "needs_input",
    "Stop": "done",
    "SessionEnd": "empty",
}
CODEX_HOOK_STATUS = {
    "UserPromptSubmit": "working",
    "PermissionRequest": "needs_input",
    "PostToolUse": "working",
    "Stop": "done",
    # Unverified: SessionEnd appears alongside SessionStart/SubagentStart/
    # SubagentStop in the codex binary's own strings, so it plausibly exists
    # as a real hook event, but this hasn't been confirmed by actually
    # observing it fire on `/exit`. If slots still don't free on Codex exit
    # after install-hooks, this is the first thing to check.
    "SessionEnd": "empty",
}

CLAUDE_HOOK_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "AskUserQuestion",
    "PostToolUse": None,
    "Notification": None,
    "Stop": None,
    "SessionEnd": None,
    # Fire for every subagent (Task-tool) run, including ones dispatched to
    # run in the background that outlive the main turn's Stop event — see
    # the "active_subagents" handling in _HookRequestHandler.do_POST, which
    # is what these two exist to feed.
    "SubagentStart": None,
    "SubagentStop": None,
}
CODEX_HOOK_EVENTS = ("UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd")


def _hook_status_for(family: str, event: str) -> str | None:
    if family == "claude":
        return CLAUDE_HOOK_STATUS.get(event)
    if family == "codex":
        return CODEX_HOOK_STATUS.get(event)
    return None


def _hook_command(event: str) -> str:
    # --max-time 1 and `|| true` make this fire-and-forget: it no-ops
    # harmlessly (within ~1s) whenever the bridge isn't running, so the
    # hook never needs uninstalling and never blocks the CLI on our behalf.
    return (
        f"curl -s --max-time 1 -X POST http://{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}{event} "
        f'-H "X-Switchboard-Slot: $SWITCHBOARD_SLOT" -d @- >/dev/null 2>&1 || true'
    )


def _codex_hook_command(event: str) -> str:
    # Codex hook commands must print a JSON object as their result.
    return _hook_command(event) + "; printf '{}'"


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


def _claude_group_is_ours(group: dict) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and HOOK_MARKER in h.get("command", "") for h in hooks)


def install_claude_hooks(settings_path: Path | None = None) -> str:
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not parse {path}: {exc}", file=sys.stderr)
            return "failed"
    settings.setdefault("hooks", {})
    changed = False
    for event, matcher in CLAUDE_HOOK_EVENTS.items():
        groups = [g for g in settings["hooks"].get(event, []) if isinstance(g, dict)]
        foreign = [g for g in groups if not _claude_group_is_ours(g)]
        desired = {"hooks": [{"type": "command", "command": _hook_command(event)}]}
        if matcher is not None:
            desired = {"matcher": matcher, **desired}
        existing_ours = [g for g in groups if _claude_group_is_ours(g)]
        if len(existing_ours) == 1 and existing_ours[0] == desired:
            continue
        settings["hooks"][event] = foreign + [desired]
        changed = True
    if not changed:
        return "unchanged"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.switchboard-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        print(f"Could not write {path}: {exc}", file=sys.stderr)
        return "failed"
    return "changed"


def install_codex_hooks(hooks_path: Path | None = None) -> str:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = hooks_path or (codex_home / "hooks.json")
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not parse {path}: {exc}", file=sys.stderr)
            return "failed"
    settings.setdefault("hooks", {})
    before = json.dumps(settings, sort_keys=True)

    for event in list(settings["hooks"].keys()):
        value = settings["hooks"][event]
        if not isinstance(value, list):
            continue
        foreign = [g for g in value if not _claude_group_is_ours(g)]
        if foreign:
            settings["hooks"][event] = foreign
        else:
            settings["hooks"].pop(event, None)

    for event in CODEX_HOOK_EVENTS:
        groups = settings["hooks"].get(event, [])
        if not isinstance(groups, list):
            groups = []
        groups.append({"hooks": [{"type": "command", "command": _codex_hook_command(event)}]})
        settings["hooks"][event] = groups

    after = json.dumps(settings, sort_keys=True)
    if after == before:
        return "unchanged"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.switchboard-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        print(f"Could not write {path}: {exc}", file=sys.stderr)
        return "failed"
    return "changed"


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


def terminal_command(cwd: str, command: str, title: str, slot: int | None = None) -> str:
    lines = [
        f"printf '\\033]0;%s\\007' {shlex.quote(title)}",
        f"cd {shlex.quote(cwd)}",
    ]
    if slot is not None:
        # Lets this session's lifecycle hooks (see install-hooks) tag their
        # POST with which slot they came from — same trick OpenMicro uses
        # with OPENMICRO_INSTANCE_ID.
        lines.append(f"export SWITCHBOARD_SLOT={int(slot)}")
    lines.append(f"exec {command}")
    return "\n".join(lines)


def resolve_command(command: str) -> str:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("Command is empty")

    executable = parts[0]
    if "/" in executable:
        if Path(executable).expanduser().exists():
            parts[0] = str(Path(executable).expanduser())
            return shlex.join(parts)
        raise FileNotFoundError(f"Command not found: {executable}")

    found = shutil.which(executable)
    if found:
        parts[0] = found
        return shlex.join(parts)

    for candidate in KNOWN_COMMAND_PATHS.get(executable, []):
        candidate_path = Path(candidate).expanduser()
        if candidate_path.exists():
            parts[0] = str(candidate_path)
            return shlex.join(parts)

    raise FileNotFoundError(f"Command not found on PATH: {executable}")


def open_terminal(command: str) -> str | None:
    """Open a new Terminal tab running command, return its tty path.

    The tty is a stable per-tab identifier (Terminal tabs have no usable `id`
    property) that later lets the bridge find this exact tab again to push a
    live effort change into it, instead of guessing at "front window".
    """
    script = """
on run argv
  set bridgeCommand to item 1 of argv
  tell application "Terminal"
    set newTab to do script bridgeCommand
    activate
    set ttyName to tty of newTab
  end tell
  return ttyName
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, command],
        check=True,
        capture_output=True,
        text=True,
    )
    tty_name = result.stdout.strip()
    return tty_name or None


def focus_terminal_tab(tty_name: str) -> bool:
    """Bring the window owning tty_name to front and select that exact tab.

    Used before driving a slot's session (voice hold, future agent.focus)
    so the right tab actually has keyboard focus, not just "some Terminal
    window."
    """
    script = """
on run argv
  set targetTty to item 1 of argv
  tell application "Terminal"
    set targetWindow to missing value
    set targetTab to missing value
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          set targetWindow to w
          set targetTab to t
        end if
      end repeat
    end repeat
    if targetWindow is missing value then
      return "missing"
    end if
    set selected tab of targetWindow to targetTab
    set index of targetWindow to 1
    activate
  end tell
  return "ok"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, tty_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "ok"


def get_terminal_pid() -> int | None:
    """PID of the Terminal.app process, for posting synthetic key events to it."""
    result = subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to return unix id of (first process whose name is "Terminal")'],
        check=False,
        capture_output=True,
        text=True,
    )
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def post_key_event(pid: int, keycode: int, key_down: bool) -> None:
    """Post a synthetic keyDown/keyUp to a specific process (not just 'frontmost').

    This is what makes real press-and-hold possible — AppleScript's `keystroke`
    only ever sends an atomic press+release, which can't represent "held."
    Requires the bridge process to have Accessibility permission (System
    Settings -> Privacy & Security -> Accessibility).
    """
    if not QUARTZ_AVAILABLE:
        raise RuntimeError(
            "Quartz is not installed. Run the bridge with host/.venv/bin/python3, "
            "or `pip install pyobjc-framework-Quartz` for whatever Python runs it."
        )
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, key_down)
    Quartz.CGEventPostToPid(pid, event)


def restart_in_slot_terminal(tty_name: str, shell_command: str) -> bool:
    """Interrupt whatever is running in tty_name's tab, then run shell_command.

    Sends a real Ctrl-C (ASCII ETX) into the tab first so an interactive TUI
    (codex/claude) drops back to a shell prompt, then types the new command.
    Returns False if the tab is gone (window/tab closed since launch).
    """
    script = """
on run argv
  set targetTty to item 1 of argv
  set payload to item 2 of argv
  tell application "Terminal"
    set targetTab to missing value
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          set targetTab to t
        end if
      end repeat
    end repeat
    if targetTab is missing value then
      return "missing"
    end if
    do script (ASCII character 3) in targetTab
    delay 0.4
    do script payload in targetTab
    activate
  end tell
  return "ok"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, tty_name, shell_command],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "ok"


def launch_agent(args: argparse.Namespace) -> int:
    slot = int(args.slot)
    if slot < 1 or slot > 4:
        print("Slots 1 through 4 map to the v1 agent keys.", file=sys.stderr)
        return 2

    cwd = str(Path(args.cwd).expanduser().resolve())
    if not Path(cwd).exists():
        documents_dir = Path.home() / "Documents"
        if documents_dir in Path(cwd).parents or Path(cwd) == documents_dir:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            print(f"Created {cwd}")
        else:
            print(
                f"Working folder does not exist: {cwd}. Set it with: "
                f"switchboard_bridge.py config --slot {slot} --cwd PATH",
                file=sys.stderr,
            )
            return 2

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


def _validate_or_create_cwd(cwd_str: str) -> str:
    """Resolve cwd_str, creating it if missing and it's under ~/Documents.

    Raises ValueError (with a user-facing message) if it's missing and
    outside ~/Documents — creating an arbitrary path elsewhere on the
    filesystem on the user's behalf is not something to do silently.
    """
    resolved = Path(cwd_str).expanduser().resolve()
    if not resolved.exists():
        documents_dir = Path.home() / "Documents"
        if resolved == documents_dir or documents_dir in resolved.parents:
            resolved.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"Working folder does not exist: {resolved}")
    return str(resolved)


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
