"""Pure data and business rules: status vocabulary, effort mapping, hook
status tables, and the record shapes the rest of the bridge passes around.

Nothing here touches a clock, a file, a socket, or a subprocess — every
function that needs "now" takes it as an explicit argument (see clock.py).
This is what lets the reducer (reducer.py) and its tests stay pure too.
"""

from __future__ import annotations

import enum
import shlex

STATUS_CHOICES = ("empty", "launched", "idle", "thinking", "working", "waiting", "needs_input", "blocked", "done")
# Statuses long enough to be worth escalating the LED for — the ones that
# just mean "still going," not the ones that already stand out on their own
# (needs_input/blocked are already flagged, done/idle aren't "busy").
BUSY_STATUSES = ("working", "thinking")

# The device's effort dial sends LOW/MED/HIGH/XHIGH/MAX (lowercased by the
# bridge before storage). Neither CLI's effort vocabulary matches ours
# exactly, so normalize first, then map per family.
EFFORT_ALIASES = {"med": "medium"}
CLAUDE_EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}
# codex's `-c model_reasoning_effort=` only has "low" confirmed against a real
# config.toml on this machine; medium/high are the standard OpenAI tiers and
# very likely valid, but xhigh/max are not confirmed for this specific key
# (a different key, multi_agent_reasoning_effort, does accept "xhigh") so they
# are clamped to "high" rather than risk passing an unrecognized value at
# launch. Revisit once a real codex session confirms the top tiers.
CODEX_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


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


# Voice hold is Claude-only for now — Codex's /voice support, if any,
# hasn't been scoped.
VOICE_SUPPORTED_FAMILIES = ("claude",)


class Liveness(enum.Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def slot_record(
    *,
    slot: int,
    name: str,
    family: str,
    cwd: str,
    command: str,
    terminal_title: str,
    now: int,
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
        "last_launched_at": now,
    }


def set_status(record: dict, status: str, now: int) -> None:
    """Apply a status transition, tracking busy_since (turn-scoped ramp).

    Every status assignment goes through this instead of writing
    record["status"] directly, so busy_since starts the moment a slot
    becomes busy and is cleared the moment it stops being busy.
    """
    was_busy = record.get("status") in BUSY_STATUSES
    record["status"] = status
    record["last_updated_at"] = now
    now_busy = status in BUSY_STATUSES
    if now_busy and not was_busy:
        record["busy_since"] = now
    elif not now_busy:
        record.pop("busy_since", None)


def agent_update_event(record: dict, now: float, *, liveness: str | None = None) -> dict:
    status = record.get("status", "empty")
    busy_elapsed_ms = 0
    if status in BUSY_STATUSES:
        busy_since = record.get("busy_since")
        if busy_since:
            busy_elapsed_ms = max(0, round((now - int(busy_since)) * 1000))
    event = {
        "event": "agent.update",
        "slot": record.get("slot"),
        "name": record.get("name", f"Agent {record.get('slot', '')}"),
        "family": record.get("family", "slot"),
        "status": status,
        "effort": record.get("effort", "medium"),
        "activity": record.get("activity", ""),
        "busy_elapsed_ms": busy_elapsed_ms,
    }
    if liveness is not None:
        event["liveness"] = liveness
    return event


# Real lifecycle hook events, not screen-scraped guesses — see
# https://github.com/stephenleo/OpenMicro, which validated this approach.
# PreToolUse only fires for AskUserQuestion (see hooks_install's matcher table).
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
CODEX_HOOK_EVENTS = ("UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd")


def hook_status_for(family: str, event: str) -> str | None:
    if family == "claude":
        return CLAUDE_HOOK_STATUS.get(event)
    if family == "codex":
        return CODEX_HOOK_STATUS.get(event)
    return None
