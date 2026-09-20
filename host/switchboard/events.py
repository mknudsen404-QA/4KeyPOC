"""Event shapes flowing through the Bridge's single queue, plus the parser
that turns a raw board line into one. Everything here is a frozen dataclass
(or a plain str/None from the parser) — no I/O, no clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from switchboard.model import Liveness


@dataclass(frozen=True)
class BoardEvent:
    """A JSON line from the device, already parsed."""

    name: str  # "agent.select", "agent.focus", "agent.reasoning.apply",
    # "voice.hold.start", "voice.hold.stop", "agent.update.ack", ...
    payload: dict


@dataclass(frozen=True)
class HookEvent:
    """One POST from a CLI lifecycle hook."""

    slot_key: str
    name: str  # "SessionStart", "PostToolUse", ...
    payload: dict  # parsed JSON body ({} if unparseable)
    # time.monotonic() when hooks_server received the POST, before it was
    # queued — lets the trace measure worker backlog (Bridge.step's
    # `queue_ms`). None for anything that doesn't come through the HTTP
    # server (tests constructing HookEvent directly, sample_events.jsonl).
    rx_mono: float | None = None


@dataclass(frozen=True)
class LivenessObserved:
    """Produced by the ticker AFTER probing; the reducer never probes."""

    results: dict[str, Liveness]  # slot_key -> ALIVE/DEAD/UNKNOWN
    confirm: bool = True  # False at startup (single DEAD frees)


@dataclass(frozen=True)
class SlotRegistered:
    """A launch completed (or an external `launch` CLI ran); record is final."""

    record: dict


@dataclass(frozen=True)
class Shutdown:
    error: Exception | None = None


Event = BoardEvent | HookEvent | LivenessObserved | SlotRegistered | Shutdown


def parse_board_line(line: str) -> BoardEvent | str | None:
    """None for non-JSON noise, a str diagnostic for malformed/non-object
    JSON, else a BoardEvent."""
    line = line.strip()
    if not line:
        return None
    if not (line.startswith("{") or line.startswith("[")):
        return None  # firmware log text, not an event
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return f"Ignored malformed JSON: {line}"
    if not isinstance(data, dict):
        return f"Ignored non-object JSON: {line}"
    name = data.get("event")
    if not name:
        return f"Ignored JSON with no event field: {line}"
    payload = {k: v for k, v in data.items() if k != "event"}
    return BoardEvent(name=name, payload=payload)
