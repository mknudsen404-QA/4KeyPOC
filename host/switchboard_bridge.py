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
import fcntl
import glob
import http.server
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
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


@dataclass
class BridgeState:
    selected_slot: int | None = None
    selected_agent: str | None = None
    effort_by_slot: dict[int, str] | None = None
    mic_active: bool = False
    registry: dict | None = None
    registry_path: Path = DEFAULT_REGISTRY
    auto_launch: bool = False
    launch_config: dict | None = None
    no_open: bool = False
    dry_run: bool = False
    device_fd: int | None = None

    def __post_init__(self) -> None:
        if self.effort_by_slot is None:
            self.effort_by_slot = {}


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
    with path.open("w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2, sort_keys=True)
        handle.write("\n")


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
    terminal_log: str | None = None,
) -> dict:
    return {
        "slot": slot,
        "name": name,
        "family": family,
        "cwd": cwd,
        "command": command,
        "terminal_title": terminal_title,
        "terminal_tty": terminal_tty,
        "terminal_log": terminal_log,
        "status": "launched",
        "effort": normalize_effort(effort),
        "activity": "terminal launched",
        "last_launched_at": int(time.time()),
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


def agent_config_for_slot(config: dict | None, slot: int) -> dict:
    agents = (config or {}).get("agents", [])
    for agent in agents:
        if int(agent.get("slot", 0)) == slot:
            return dict(agent)
    return {
        "slot": slot,
        "name": f"Agent {slot}",
        "family": "codex",
        "cwd": str(PROJECT_ROOT),
        "command": "codex",
    }


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
        cwd=agent.get("cwd", str(PROJECT_ROOT)),
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


def update_registry_slot_status(
    registry_path: Path,
    slot: int,
    *,
    status: str | None = None,
    effort: str | None = None,
    activity: str | None = None,
) -> dict | None:
    registry = load_registry(registry_path)
    record = registry.get("slots", {}).get(str(slot))
    if not record:
        return None
    if status is not None:
        record["status"] = status
    if effort is not None:
        record["effort"] = effort
    if activity is not None:
        record["activity"] = activity
    record["last_updated_at"] = int(time.time())
    save_registry(registry, registry_path)
    return record


def agent_update_event(record: dict) -> dict:
    status = record.get("status", "empty")
    busy_seconds = 0
    if status in BUSY_STATUSES:
        last_updated = record.get("last_updated_at")
        if last_updated:
            busy_seconds = max(0, int(time.time()) - int(last_updated))
    return {
        "event": "agent.update",
        "slot": record.get("slot"),
        "name": record.get("name", f"Agent {record.get('slot', '')}"),
        "family": record.get("family", "slot"),
        "status": status,
        "effort": record.get("effort", "medium"),
        "activity": record.get("activity", ""),
        "busy_seconds": busy_seconds,
    }


def write_device_event(fd: int | None, event: dict) -> None:
    if fd is None:
        return
    line = json.dumps(event, separators=(",", ":")) + "\n"
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
    registry = state.registry or {"slots": {}}

    if name == "agent.select":
        state.selected_slot = int(event.get("slot", 0)) or None
        state.selected_agent = event.get("name")
        if state.selected_slot:
            slot_key = str(state.selected_slot)
            existing = registry.get("slots", {}).get(slot_key)
            if existing and existing.get("terminal_tty") and not find_terminal_tab(existing["terminal_tty"]):
                # The Terminal window/tab was closed by hand since launch.
                # Treat the slot as empty again so re-pressing its key can
                # relaunch a fresh session instead of describing a ghost.
                registry.get("slots", {}).pop(slot_key, None)
                save_registry(registry, state.registry_path)
            if not registry.get("slots", {}).get(slot_key) and state.auto_launch:
                message = launch_slot_from_config(
                    slot=state.selected_slot,
                    config=state.launch_config,
                    registry_path=state.registry_path,
                    dry_run=state.dry_run,
                    no_open=state.no_open,
                )
                state.registry = load_registry(state.registry_path)
                write_slot_update(state.device_fd, state.registry, state.selected_slot)
                return message
            # Slot already has a live session — re-selecting it should bring
            # its window to front, not just repaint the LED. (A freshly
            # launched slot, above, already comes to front on its own via
            # Terminal's `do script` + activate.)
            live = registry.get("slots", {}).get(slot_key)
            if live and live.get("terminal_tty"):
                focus_terminal_tab(live["terminal_tty"])
            write_slot_update(state.device_fd, registry, state.selected_slot)
            return f"Selected {registry_slot_summary(registry, state.selected_slot)}"
        return "Selected agent slot is unknown"

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
            if find_terminal_tab(record["terminal_tty"]):
                new_command = command_with_effort(record["command"], record["family"], effort)
                cwd = record.get("cwd", str(PROJECT_ROOT))
                title = record.get("terminal_title", f"Switchboard A{slot}")
                new_shell_command = terminal_command(
                    cwd, new_command, title, log_path=record.get("terminal_log"), slot=slot
                )
                pushed = restart_in_slot_terminal(record["terminal_tty"], new_shell_command)
            else:
                # Tab was closed by hand since launch; stop treating it as live.
                update_registry_slot_status(state.registry_path, slot, activity="terminal window closed")

        # Effort is its own field on the device (top-right of the screen);
        # it doesn't need to also occupy the activity line.
        update_registry_slot_status(state.registry_path, slot, effort=effort)
        state.effort_by_slot[slot] = effort
        state.registry = load_registry(state.registry_path)
        write_slot_update(state.device_fd, state.registry, slot)
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
        if not tty_name or not find_terminal_tab(tty_name):
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
    state = BridgeState(registry=load_registry(registry_path), registry_path=registry_path)
    started = time.monotonic()
    for line in lines:
        message = handle_line(line, state)
        if message:
            print(message, flush=True)
        if duration is not None and (time.monotonic() - started) >= duration:
            return 0
    return 0


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\r")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# PROVISIONAL. These patterns are intentionally few and conservative rather
# than guessed at length — they have not been calibrated against a real
# codex/claude session transcript yet. Treat status auto-detection as
# best-effort until verified live and expand this list from what's actually
# observed (see Status Auto-Detection Plan in SWITCHBOARD_DESIGN.md).
#
# No "error"/"traceback" -> blocked pattern here on purpose: matching those
# words anywhere in the last few KB of a log fires on any benign mention
# (grep output, "error handling", a since-fixed traceback scrolling by),
# and was flipping the LED red constantly during ordinary subagent runs.
# Real status changes come from actual hook events (CLAUDE_HOOK_STATUS /
# CODEX_HOOK_STATUS below); "blocked" is unreached until something more
# precise than a text-scan can set it.
ACTIVITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\(y/n\)|\[y/n\]", re.IGNORECASE), "needs_input"),
]

TAIL_READ_BYTES = 4000


def classify_activity(tail_text: str) -> str | None:
    clean = strip_ansi(tail_text)
    for pattern, status in ACTIVITY_PATTERNS:
        if pattern.search(clean):
            return status
    return None


def read_log_tail(log_path: str, max_bytes: int = TAIL_READ_BYTES) -> str:
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def poll_slot_logs(state: BridgeState, stop_event: threading.Event, interval: float = 2.0) -> None:
    """Background loop: tail each launched slot's log, push a status change
    to the device when the classifier's guess differs from what's stored.
    Also proactively frees any slot whose Terminal tab was closed by hand —
    lifecycle hooks cover `/exit`, but a plain window/tab close (Cmd+W) fires
    no hook at all, so this is the only thing that ever notices that case.
    Runs alongside the serial-reading loop in its own thread; every read of
    state.registry_path goes through load_registry/save_registry so this
    never holds the file open across a sleep.
    """
    while not stop_event.wait(interval):
        registry = load_registry(state.registry_path)
        slots = registry.get("slots", {})
        changed_slots: list[str] = []
        freed_slots: list[str] = []
        for slot_key, record in list(slots.items()):
            tty_name = record.get("terminal_tty")
            if tty_name and not find_terminal_tab(tty_name):
                freed_slots.append(slot_key)
                continue
            log_path = record.get("terminal_log")
            if not log_path or not Path(log_path).exists():
                continue
            detected = classify_activity(read_log_tail(log_path))
            if detected and detected != record.get("status"):
                record["status"] = detected
                record["last_updated_at"] = int(time.time())
                changed_slots.append(slot_key)
        for slot_key in freed_slots:
            slots.pop(slot_key, None)
        if changed_slots or freed_slots:
            save_registry(registry, state.registry_path)
            state.registry = registry
            for slot_key in changed_slots + freed_slots:
                write_slot_update(state.device_fd, registry, slot_key)

        # Re-push busy slots even when status itself hasn't changed, purely
        # so busy_seconds keeps climbing on the device — that's what lets the
        # LED escalate (e.g. start pulsing) the longer a turn runs, not just
        # react to status transitions.
        already_pushed = set(changed_slots) | set(freed_slots)
        for slot_key, record in slots.items():
            if slot_key not in already_pushed and record.get("status") in BUSY_STATUSES:
                write_slot_update(state.device_fd, registry, slot_key)


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


def _extract_agent_id(body: bytes) -> str | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    agent_id = payload.get("agent_id")
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
            registry = load_registry(self.state.registry_path)
            record = registry.get("slots", {}).get(slot_key)
            if record:
                dirty = self._apply_hook_event(registry, slot_key, record, event, body)
                if dirty:
                    save_registry(registry, self.state.registry_path)
                    write_slot_update(self.state.device_fd, registry, slot_key)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    @staticmethod
    def _apply_hook_event(registry: dict, slot_key: str, record: dict, event: str, body: bytes) -> bool:
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
                    record["status"] = "working"
                    record["last_updated_at"] = int(time.time())
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
                record["status"] = "done"
                record["last_updated_at"] = int(time.time())
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
            return dirty
        if status == "done" and record.get("active_subagents"):
            # Defer: a subagent this turn dispatched is still running.
            # SubagentStop will deliver "done" once the last one finishes.
            record["main_stopped"] = True
            return True
        record["status"] = status
        record["last_updated_at"] = int(time.time())
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
) -> int:
    state = BridgeState(
        registry=load_registry(registry_path),
        registry_path=registry_path,
        auto_launch=auto_launch,
        launch_config=load_agents_config(launch_config_path),
        dry_run=dry_run,
        no_open=no_open,
        device_fd=device_fd,
    )
    stop_poll = threading.Event()
    poller = threading.Thread(target=poll_slot_logs, args=(state, stop_poll), daemon=True)
    poller.start()
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
        stop_poll.set()
        if hook_server is not None:
            hook_server.shutdown()


LOG_DIR = HOST_DIR / "logs"


def terminal_log_path(slot: int) -> Path:
    return LOG_DIR / f"slot-{slot}.log"


def terminal_command(
    cwd: str, command: str, title: str, log_path: str | None = None, slot: int | None = None
) -> str:
    lines = [
        f"printf '\\033]0;%s\\007' {shlex.quote(title)}",
        f"cd {shlex.quote(cwd)}",
    ]
    if slot is not None:
        # Lets this session's lifecycle hooks (see install-hooks) tag their
        # POST with which slot they came from — same trick OpenMicro uses
        # with OPENMICRO_INSTANCE_ID.
        lines.append(f"export SWITCHBOARD_SLOT={int(slot)}")
    if log_path:
        # `script -q` tees the session's raw pty output (ANSI codes and all)
        # to log_path so the bridge can tail it for status auto-detection,
        # without changing how the session itself runs or looks in Terminal.
        # Truncate first so a relaunch/restart doesn't append onto stale text.
        lines.append(f": > {shlex.quote(log_path)}")
        lines.append(f"exec script -q {shlex.quote(log_path)} {command}")
    else:
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


def find_terminal_tab(tty_name: str) -> bool:
    """Return True if a Terminal tab with this tty is still open."""
    script = """
on run argv
  set targetTty to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          return "found"
        end if
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, tty_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "found"


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
        print(f"Working folder does not exist: {cwd}", file=sys.stderr)
        return 2

    try:
        base_command = resolve_command(args.command)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        print("Install the CLI, use an absolute --command path, or use --dry-run to preview.", file=sys.stderr)
        return 2

    effort = normalize_effort(getattr(args, "effort", None))
    command = command_with_effort(base_command, args.family, effort)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(terminal_log_path(slot))

    title = args.title or f"Switchboard A{slot} {args.name}"
    shell_command = terminal_command(cwd, command, title, log_path=log_path, slot=slot)
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
        terminal_log=log_path,
    )

    if args.dry_run:
        if not getattr(args, "quiet", False):
            print(f"Would register {registry_slot_summary({'slots': {str(slot): record}}, slot)}")
            print("Would run:")
            print(shell_command)
        return 0

    if args.no_open:
        registry = load_registry(args.registry)
        set_registry_slot(registry, record)
        save_registry(registry, args.registry)
        if not getattr(args, "quiet", False):
            print(f"Registered {registry_slot_summary(registry, slot)}")
            print("Terminal launch skipped.")
        return 0

    record["terminal_tty"] = open_terminal(shell_command)
    registry = load_registry(args.registry)
    set_registry_slot(registry, record)
    save_registry(registry, args.registry)
    if not getattr(args, "quiet", False):
        print(f"Launched and registered {registry_slot_summary(registry, slot)}")
    return 0


def launch_all(args: argparse.Namespace) -> int:
    with Path(args.config).expanduser().open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    agents = config.get("agents", [])
    if not isinstance(agents, list):
        print("Config must contain an agents list.", file=sys.stderr)
        return 2
    for agent in agents:
        child = argparse.Namespace(
            slot=agent["slot"],
            name=agent["name"],
            family=agent.get("family", "codex"),
            cwd=agent.get("cwd", str(PROJECT_ROOT)),
            command=agent.get("command", args.command),
            title=agent.get("title"),
            registry=args.registry,
            dry_run=args.dry_run,
            no_open=args.no_open,
        )
        result = launch_agent(child)
        if result:
            return result
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
    registry = load_registry(args.registry)
    removed = registry.get("slots", {}).pop(str(args.slot), None)
    save_registry(registry, args.registry)
    if removed:
        print(f"Cleared Agent {args.slot}")
    else:
        print(f"Agent {args.slot} was already empty")
    return 0


def test_trigger(args: argparse.Namespace) -> int:
    """Type text into a slot's live terminal tab, for testing status
    auto-detection without spending any real agent/API usage. Intended for a
    'shell'-family test slot (e.g. `launch --family shell --command cat`) —
    cat echoes back whatever it's given, which is what feeds the tee'd log
    the poller reads. Using this against a real codex/claude slot would type
    into that agent's actual input, so it's refused unless --force is passed.
    """
    registry = load_registry(args.registry)
    record = registry.get("slots", {}).get(str(args.slot))
    if not record:
        print(f"Slot {args.slot} is empty — launch a test agent into it first.", file=sys.stderr)
        return 2
    if record.get("family") != "shell" and not args.force:
        print(
            f"Slot {args.slot} is family '{record.get('family')}', not a 'shell' test slot. "
            "This would type into a real agent's input. Pass --force if that's really what you want.",
            file=sys.stderr,
        )
        return 2
    tty_name = record.get("terminal_tty")
    if not tty_name or not find_terminal_tab(tty_name):
        print(f"Slot {args.slot} has no open terminal tab to inject into.", file=sys.stderr)
        return 2

    script = """
on run argv
  set targetTty to item 1 of argv
  set payload to item 2 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          do script payload in t
        end if
      end repeat
    end repeat
  end tell
end run
"""
    subprocess.run(["osascript", "-e", script, tty_name, args.text], check=True)
    print(f"Injected into slot {args.slot}: {args.text!r}")
    print("Give the poller a couple seconds (default interval 2s), then check:")
    print(f"  python3 host/switchboard_bridge.py slots")
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
    listen_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
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
    launch_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    launch_parser.add_argument("--slot", type=int, required=True)
    launch_parser.add_argument("--name", required=True)
    launch_parser.add_argument(
        "--family", default="codex", choices=("codex", "claude", "shell"),
        help="'shell' is a no-cost test family (no effort flags added) for a harmless command like cat",
    )
    launch_parser.add_argument("--cwd", default=str(PROJECT_ROOT))
    launch_parser.add_argument("--command", default="codex")
    launch_parser.add_argument("--title")
    launch_parser.add_argument("--effort", default="medium", help="low, medium, high, xhigh, or max")
    launch_parser.add_argument("--dry-run", action="store_true")
    launch_parser.add_argument("--no-open", action="store_true")

    launch_all_parser = subcommands.add_parser("launch-all", help="Launch/register agents from a config file")
    launch_all_parser.set_defaults(func=launch_all)
    launch_all_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    launch_all_parser.add_argument("--config", default=str(HOST_DIR / "agents.example.json"))
    launch_all_parser.add_argument("--command", default="codex")
    launch_all_parser.add_argument("--dry-run", action="store_true")
    launch_all_parser.add_argument("--no-open", action="store_true")

    slots_parser = subcommands.add_parser("slots", help="Show registered agent slots")
    slots_parser.set_defaults(func=list_slots)
    slots_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)

    status_parser = subcommands.add_parser("status", help="Update one registered agent slot")
    status_parser.set_defaults(func=set_status)
    status_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    status_parser.add_argument("--slot", type=int, required=True)
    status_parser.add_argument("--status", choices=STATUS_CHOICES)
    status_parser.add_argument("--effort")
    status_parser.add_argument("--activity")

    clear_parser = subcommands.add_parser("clear", help="Clear one registered agent slot")
    clear_parser.set_defaults(func=clear_slot)
    clear_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    clear_parser.add_argument("--slot", type=int, required=True)

    hooks_parser = subcommands.add_parser(
        "install-hooks",
        help="Register Switchboard lifecycle hooks with Claude Code and Codex (real status auto-detection)",
    )
    hooks_parser.set_defaults(func=install_hooks)

    trigger_parser = subcommands.add_parser(
        "test-trigger", help="Type text into a shell-family test slot's terminal (no API cost)"
    )
    trigger_parser.set_defaults(func=test_trigger)
    trigger_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    trigger_parser.add_argument("--slot", type=int, required=True)
    trigger_parser.add_argument("text")
    trigger_parser.add_argument("--force", action="store_true", help="Allow injecting into a non-shell (real agent) slot")

    sync_parser = subcommands.add_parser("sync", help="Send registered slot status to the board")
    sync_parser.set_defaults(func=sync_device)
    sync_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
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
