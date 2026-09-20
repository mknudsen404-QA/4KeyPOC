"""Pure data and business rules: status vocabulary, effort mapping, hook
status tables, and the record shapes the rest of the bridge passes around.

Nothing here touches a clock, a file, a socket, or a subprocess — every
function that needs "now" takes it as an explicit argument (see clock.py).
This is what lets the reducer (reducer.py) and its tests stay pure too.
"""

from __future__ import annotations

import enum
import shlex

from switchboard.families import registry as family_registry
from switchboard.status_table import BUSY_STATUSES, CLAUDE_HOOK_STATUS, CODEX_HOOK_EVENTS, CODEX_HOOK_STATUS, STATUS_CHOICES

# STATUS_CHOICES/BUSY_STATUSES/CLAUDE_HOOK_STATUS/CODEX_HOOK_STATUS/
# CODEX_HOOK_EVENTS live in status_table.py now (the single source of
# truth the firmware header is also generated from) and are just
# re-exported here so every existing `from switchboard.model import ...`
# call site keeps working unchanged.

# The device's effort dial sends LOW/MED/HIGH/XHIGH/MAX (lowercased by the
# bridge before storage). Neither CLI's effort vocabulary matches ours
# exactly, so normalize first, then ask the family's profile to map it.
EFFORT_ALIASES = {"med": "medium"}


def normalize_effort(effort: str | None) -> str:
    value = (effort or "medium").strip().lower()
    return EFFORT_ALIASES.get(value, value)


def effort_args(family: str, effort: str | None) -> list[str]:
    normalized = normalize_effort(effort)
    return family_registry.get(family).effort_args(normalized)


def command_with_effort(base_command: str, family: str, effort: str | None) -> str:
    extra_args = effort_args(family, effort)
    if not extra_args:
        return base_command
    return f"{base_command} {shlex.join(extra_args)}"


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
    voice: dict | None = None,
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
        "status_since": now,
        "effort": normalize_effort(effort),
        "activity": "terminal launched",
        "last_launched_at": now,
        # Resolved once at launch time (launcher.build_launch): the
        # slot's explicit `voice` config, or its family's default_voice()
        # — see switchboard/voice/ (Phase 4). {} means no provider, same
        # as an explicit {"provider": "none"}.
        "voice": voice or {},
    }


def set_status(record: dict, status: str, now: int) -> None:
    """Apply a status transition, tracking busy_since (turn-scoped ramp).

    Every status assignment goes through this instead of writing
    record["status"] directly, so busy_since starts the moment a slot
    becomes busy and is cleared the moment it stops being busy.
    """
    was_busy = record.get("status") in BUSY_STATUSES
    if record.get("status") != status:
        record["status_since"] = now
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


def hook_status_for(family: str, event: str) -> str | None:
    return family_registry.get(family).hook_status_for(event)
