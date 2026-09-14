"""The single source of truth for Switchboard's status vocabulary: which
hook events produce which status for each CLI family, what "busy" means,
and what color/pulse the firmware should render for each status.

STATUS_CHOICES, BUSY_STATUSES, CLAUDE_HOOK_STATUS, CODEX_HOOK_STATUS (all
previously hand-maintained in model.py) are derived from STATUSES below.
To add a status: add one row here, then regenerate the firmware header:

    python3 -m switchboard.status_table --emit-header > firmware/neokey/status_table.h

`test_status_table_header_is_current` fails (and names this exact command)
if the checked-in header is out of sync with this file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusDef:
    name: str
    busy: bool
    claude_hooks: tuple[str, ...]
    codex_hooks: tuple[str, ...]
    # color/pulse_ms are None for busy statuses: they use the busy ramp
    # (busyColor()/busyPulsePeriodMs() in led_model.h) instead of a static
    # value.
    color: int | None
    pulse_ms: int | None


# name          busy   claude_hooks                          codex_hooks                            color      pulse_ms
STATUSES: tuple[StatusDef, ...] = (
    StatusDef("empty", False, (), (), 0x000000, 0),
    StatusDef("launched", False, (), (), 0x8C8C8C, 0),
    StatusDef("idle", False, ("SessionStart",), (), 0x8C8C8C, 0),
    StatusDef("thinking", True, (), (), None, None),
    StatusDef("working", True, ("UserPromptSubmit", "PostToolUse"), ("UserPromptSubmit", "PostToolUse"), None, None),
    StatusDef("waiting", False, (), (), 0xFFFF00, 0),
    StatusDef("needs_input", False, ("PreToolUse", "Notification"), ("PermissionRequest",), 0xFFFF00, 500),
    StatusDef("blocked", False, (), (), 0xFF0000, 0),
    StatusDef("done", False, ("Stop",), ("Stop",), 0x00FF00, 0),
    # Not a settable status (excluded from STATUS_CHOICES below) — it's the
    # liveness-uncertain override (see reducer.py's unknown_probes) and the
    # firmware's statusFromString() fallback for an unrecognized string.
    StatusDef("unknown", False, (), (), 0xFF8C00, 3000),
)

# SessionEnd always frees the slot regardless of status — not a "this hook
# produces that status" row, so it isn't part of STATUSES.
CLAUDE_SESSION_END_STATUS = "empty"
CODEX_SESSION_END_STATUS = "empty"

# Claude Code's Notification hook fires for several `notification_type`s;
# only the ones where Claude is actually blocked on the user mean
# needs_input. `idle_prompt` ("Claude is waiting for your input", ~60s
# after a turn ends) and `auth_success` are not — the slot must stay
# "done"/green after a turn, not flip to yellow a minute later. This tuple
# drives both the hook matcher below (so the other types never leave Claude
# Code) and the reducer's guard (for installs whose hooks predate the
# matcher).
NEEDS_INPUT_NOTIFICATION_TYPES: tuple[str, ...] = ("permission_prompt", "elicitation_dialog")

# Which hooks Claude Code fires for, and the matcher restricting when (None
# = every invocation of that hook). PreToolUse only fires for
# AskUserQuestion; Notification only for the blocking notification types;
# SubagentStart/SubagentStop aren't status-producing hooks (the reducer's
# active_subagents gating handles them directly) but still need registering.
HOOK_MATCHERS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "AskUserQuestion",
    "PostToolUse": None,
    "Notification": "|".join(NEEDS_INPUT_NOTIFICATION_TYPES),
    "Stop": None,
    "SessionEnd": None,
    "SubagentStart": None,
    "SubagentStop": None,
}

CODEX_HOOK_EVENTS: tuple[str, ...] = ("UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd")

STATUS_CHOICES: tuple[str, ...] = tuple(s.name for s in STATUSES if s.name != "unknown")
BUSY_STATUSES: tuple[str, ...] = tuple(s.name for s in STATUSES if s.busy)

CLAUDE_HOOK_STATUS: dict[str, str] = {hook: s.name for s in STATUSES for hook in s.claude_hooks}
CLAUDE_HOOK_STATUS["SessionEnd"] = CLAUDE_SESSION_END_STATUS

CODEX_HOOK_STATUS: dict[str, str] = {hook: s.name for s in STATUSES for hook in s.codex_hooks}
CODEX_HOOK_STATUS["SessionEnd"] = CODEX_SESSION_END_STATUS


def _pascal_case(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def emit_header() -> str:
    """The firmware's Status enum, statusFromString, statusIsBusy,
    colorForStatus, and the static per-status pulse table — everything a
    generator can mechanically produce from STATUSES. led_model.h
    #includes this and no longer hand-maintains any of it; the busy-ramp
    special cases (busyColor/busyPulsePeriodMs) stay hand-written there,
    since they need `elapsedMs`, which isn't a per-status constant.
    """
    enum_members = "\n".join(f"  {_pascal_case(s.name)}," for s in STATUSES)

    from_string_checks = "\n".join(
        f'  if (strcmp(s, "{s.name}") == 0) return Status::{_pascal_case(s.name)};'
        for s in STATUSES
        if s.name != "unknown"  # unknown is the fallback for anything unrecognized, not matched by name
    )

    busy_names = " || ".join(f"s == Status::{_pascal_case(s.name)}" for s in STATUSES if s.busy)

    color_cases = "\n".join(
        f"    case Status::{_pascal_case(s.name)}: return "
        + (f"0x{s.color:06X};" if s.color is not None else "SOFT_WHITE;  // busy ramp overrides this — see busyColor()")
        for s in STATUSES
    )

    pulse_cases = "\n".join(
        f"    case Status::{_pascal_case(s.name)}: return {s.pulse_ms}UL;"
        for s in STATUSES
        if s.pulse_ms
    )

    return f"""\
#ifndef SWITCHBOARD_STATUS_TABLE_H
#define SWITCHBOARD_STATUS_TABLE_H

// GENERATED by host/switchboard/status_table.py --emit-header. Do not
// hand-edit — edit STATUSES in that file and regenerate:
//   python3 -m switchboard.status_table --emit-header > firmware/neokey/status_table.h

#include <cstdint>
#include <cstring>

enum class Status : uint8_t {{
{enum_members}
}};

inline Status statusFromString(const char *s) {{
{from_string_checks}
  return Status::Unknown;
}}

inline bool statusIsBusy(Status s) {{
  return {busy_names};
}}

constexpr uint32_t SOFT_WHITE = 0x8C8C8C;  // launched + idle, and the busy ramp's starting color

inline uint32_t colorForStatus(Status s) {{
  switch (s) {{
{color_cases}
    default: return 0;
  }}
}}

// Static (non-ramp) per-status pulse period in ms; 0 means "solid, no
// pulse". Busy statuses (Thinking/Working) aren't listed here — they use
// busyPulsePeriodMs()'s ramp instead (see led_model.h's pulsePeriodFor()).
inline unsigned long staticPulseMsFor(Status s) {{
  switch (s) {{
{pulse_cases}
    default: return 0UL;
  }}
}}

#endif  // SWITCHBOARD_STATUS_TABLE_H
"""


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-header", action="store_true", help="Print the generated firmware header to stdout")
    args = parser.parse_args()
    if args.emit_header:
        print(emit_header(), end="")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
