"""The pure heart of the bridge: given the current slots, in-memory reducer
state, and one event, decide the new slots and the effects (I/O) that
should happen as a result. No imports of os, subprocess, time, json file
I/O, or anything from terminal/device — everything it needs (the current
time, a probed liveness result) arrives as part of the event or `now`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from switchboard.events import BoardEvent, Event, HookEvent, LivenessObserved, Shutdown, SlotRegistered
from switchboard.model import (
    BUSY_STATUSES,
    VOICE_SUPPORTED_FAMILIES,
    Liveness,
    hook_status_for,
    set_status,
)


@dataclass(frozen=True)
class Effect:
    pass


@dataclass(frozen=True)
class SendUpdate(Effect):
    slot_key: str  # push agent.update (record or empty) for this slot
    liveness: str | None = None  # "alive" | "unknown", set only on a confirmed liveness transition


@dataclass(frozen=True)
class Launch(Effect):
    slot: int  # open a terminal for this slot


@dataclass(frozen=True)
class Focus(Effect):
    tty: str


@dataclass(frozen=True)
class VoiceKey(Effect):
    down: bool


@dataclass(frozen=True)
class CloseTab(Effect):
    tty: str  # close the Terminal tab at this tty (--close-dead-tabs only)


@dataclass(frozen=True)
class Log(Effect):
    message: str


@dataclass
class ReducerState:
    """In-memory, NOT persisted; owned by Bridge."""

    selected_slot: int | None = None
    mic_active: bool = False
    dead_probes: dict[str, int] = field(default_factory=dict)
    unknown_probes: dict[str, int] = field(default_factory=dict)


def reduce(
    slots: dict[str, dict], state: ReducerState, event: Event, now: int, *, auto_launch: bool
) -> tuple[dict[str, dict], list[Effect]]:
    if isinstance(event, BoardEvent):
        effects = _reduce_board_event(slots, state, event, now, auto_launch=auto_launch)
    elif isinstance(event, HookEvent):
        effects = _reduce_hook_event(slots, event, now)
    elif isinstance(event, LivenessObserved):
        effects = _reduce_liveness_observed(slots, state, event)
    elif isinstance(event, SlotRegistered):
        slots[str(event.record["slot"])] = event.record
        effects = [SendUpdate(str(event.record["slot"]))]
    elif isinstance(event, Shutdown):
        effects = []
    else:
        effects = [Log(f"Unhandled event: {event!r}")]
    return slots, effects


def _reduce_board_event(
    slots: dict[str, dict], state: ReducerState, event: BoardEvent, now: int, *, auto_launch: bool
) -> list[Effect]:
    payload = event.payload
    name = event.name

    if name == "agent.select":
        return _handle_select(slots, state, payload, now, auto_launch=auto_launch)
    if name == "agent.update.ack":
        return [Log(f"Board applied update for Agent {payload.get('slot')}")]
    if name == "boot":
        firmware = payload.get("firmware")
        if firmware and firmware != "neokey":
            # The spike (or anything else) identifying itself is exactly
            # the failure mode docs/design/wrong-firmware-recovery-plan.md
            # exists for — a wrong-firmware board otherwise looks identical
            # to a dead one. Log loudly but keep running: this is
            # informational, not a reason to stop the bridge.
            return [
                Log(
                    f"Board boot: WRONG FIRMWARE — board is running {firmware!r}, "
                    "not neokey. Reflash firmware/neokey "
                    "(see docs/design/wrong-firmware-recovery-plan.md)."
                )
            ]
        return [
            Log(f"Board boot: {payload.get('stage')} firmware={firmware} build={payload.get('build')}")
        ]
    if name == "error":
        return [Log(f"Board error: {payload.get('reason')} (attempt {payload.get('attempt')}) — power-cycle the board")]
    if name == "agent.focus":
        slot = int(payload.get("slot", state.selected_slot or 0))
        record = slots.get(str(slot)) if slot else None
        effects: list[Effect] = []
        if record and record.get("terminal_tty"):
            effects.append(Focus(record["terminal_tty"]))
        effects.append(Log(f"Focus requested for agent {slot}"))
        return effects
    if name == "agent.reasoning.apply":
        return _handle_reasoning_apply(slots, state, payload, now)
    if name == "voice.hold.start":
        return _handle_voice_start(slots, state, payload)
    if name == "voice.hold.stop":
        return _handle_voice_stop(slots, state, payload)
    if name == "plan.approve":
        return [Log("Approval requested. Bridge did not approve anything.")]
    if name == "review.request":
        return [Log("Code review requested. Bridge did not start one.")]
    if name == "slash.run":
        command = payload.get("command", "")
        return [Log(f"Slash command requested: {command or '(none supplied)'}")]
    return [Log(f"Unhandled device event: {name}")]


def _handle_select(
    slots: dict[str, dict], state: ReducerState, payload: dict, now: int, *, auto_launch: bool
) -> list[Effect]:
    slot = int(payload.get("slot", 0)) or None
    state.selected_slot = slot
    if not slot:
        return [Log("Selected agent slot is unknown")]
    slot_key = str(slot)
    existing = slots.get(slot_key)
    liveness = payload.get("liveness")

    if existing:
        if liveness == "dead":
            count = state.dead_probes.get(slot_key, 0) + 1
            if count >= 2:
                # Confirmed dead: free it, then fall through to the
                # no-record branch below (which may auto-launch).
                state.dead_probes.pop(slot_key, None)
                slots.pop(slot_key, None)
                effects: list[Effect] = [SendUpdate(slot_key)]
                if auto_launch:
                    effects.append(Launch(slot))
                return effects
            state.dead_probes[slot_key] = count
            return [Log(f"Slot {slot}: liveness unconfirmed, probing again")]

        # ALIVE or UNKNOWN: not dead, so reset the dead-probe count.
        state.dead_probes.pop(slot_key, None)
        if liveness != "unknown" and existing.get("status") == "done":
            # Plan §5.1: green (done) holds until the key is pressed or the
            # next prompt starts a new turn — the press that brought the
            # user back to this slot clears it now.
            set_status(existing, "idle", now)
        tty = existing.get("terminal_tty")
        effects = [Focus(tty)] if tty else []
        effects.append(SendUpdate(slot_key))
        if liveness == "unknown":
            # --no-open records (no terminal_tty) are always UNKNOWN, and
            # never auto-launched here: only hooks/clear can free them, so
            # re-pressing the key just re-focuses/repaints.
            effects.append(Log(f"Slot {slot}: liveness unknown, not relaunching"))
        else:
            effects.append(Log(f"Selected agent {slot}"))
        return effects

    if auto_launch:
        return [Launch(slot)]
    return [SendUpdate(slot_key)]


def _handle_reasoning_apply(slots: dict[str, dict], state: ReducerState, payload: dict, now: int) -> list[Effect]:
    slot = int(payload.get("slot", state.selected_slot or 0))
    effort = str(payload.get("effort", "medium")).strip().lower()
    if not slot:
        return [Log(f"Reasoning effort for agent {slot}: {effort}")]

    # Effort applies on next launch, not to a live session. An earlier
    # version restarted the live CLI in its terminal tab to apply the new
    # effort immediately, but a live session was observed with only some
    # Ctrl-C interrupts actually landing — a failed interrupt typed the
    # relaunch command straight into the live conversation instead.
    record = slots.get(str(slot))
    if record is not None:
        record["effort"] = effort
    return [SendUpdate(str(slot)), Log(f"Reasoning effort for agent {slot}: {effort} (applies on next launch)")]


def _handle_voice_start(slots: dict[str, dict], state: ReducerState, payload: dict) -> list[Effect]:
    slot = int(payload.get("slot", state.selected_slot or 0))
    if not slot:
        return [Log("Voice hold start: no agent selected")]
    record = slots.get(str(slot))
    if not record:
        return [Log(f"Voice hold start: agent {slot} is not launched")]
    family = record.get("family")
    if family not in VOICE_SUPPORTED_FAMILIES:
        return [Log(f"Voice hold: agent {slot} is '{family}', voice is Claude-only for now")]
    tty = record.get("terminal_tty")
    if not tty or payload.get("liveness") == "dead":
        return [Log(f"Voice hold start: agent {slot} has no open terminal tab")]
    state.mic_active = True
    return [Focus(tty), VoiceKey(True), Log(f"Voice hold started for agent {slot}")]


def _handle_voice_stop(slots: dict[str, dict], state: ReducerState, payload: dict) -> list[Effect]:
    slot = int(payload.get("slot", state.selected_slot or 0))
    record = slots.get(str(slot)) if slot else None
    if not record or record.get("family") not in VOICE_SUPPORTED_FAMILIES:
        state.mic_active = False
        return [Log(f"Voice hold stop: agent {slot} is not a voice-capable slot")]
    state.mic_active = False
    return [VoiceKey(False), Log(f"Voice hold stopped for agent {slot}")]


def _reduce_liveness_observed(slots: dict[str, dict], state: ReducerState, event: LivenessObserved) -> list[Effect]:
    """Free any slot confirmed dead: two consecutive DEAD readings by
    default, or a single one at startup (confirm=False) since nothing has
    been running yet and a first DEAD reading is trustworthy.

    Also tracks UNKNOWN runs per slot: the LED may be lying (still showing
    whatever status it last got, even though liveness can no longer be
    confirmed), so two consecutive UNKNOWN readings send one update
    flagged `liveness: "unknown"` (the firmware pulses amber regardless of
    status), and the next confirmed ALIVE reading sends one flagged
    `liveness: "alive"` to clear it. Only a single SendUpdate fires at each
    transition, not on every tick the slot stays UNKNOWN.
    """
    threshold = 2 if event.confirm else 1
    effects: list[Effect] = []
    for slot_key, result in event.results.items():
        if result is Liveness.DEAD:
            state.unknown_probes.pop(slot_key, None)
            count = state.dead_probes.get(slot_key, 0) + 1
            if count < threshold:
                state.dead_probes[slot_key] = count
                continue
            state.dead_probes.pop(slot_key, None)
            freed = slots.pop(slot_key, None)
            if freed is not None:
                effects.append(SendUpdate(slot_key))
                # Only a confirmed-dead process leaves behind a ghost tab
                # worth closing — SessionEnd (the user typed /exit) frees
                # the slot too, but the shell is still there on purpose and
                # is handled separately, never via this path.
                tty = freed.get("terminal_tty")
                if tty:
                    effects.append(CloseTab(tty))
            continue

        state.dead_probes.pop(slot_key, None)
        if result is Liveness.UNKNOWN:
            count = state.unknown_probes.get(slot_key, 0) + 1
            state.unknown_probes[slot_key] = count
            if count == 2 and slot_key in slots:
                effects.append(SendUpdate(slot_key, liveness="unknown"))
            continue

        # ALIVE: clear an unknown run, if any, and tell the board it's
        # trustworthy again.
        was_unknown = state.unknown_probes.pop(slot_key, None) is not None
        if was_unknown and slot_key in slots:
            effects.append(SendUpdate(slot_key, liveness="alive"))
    return effects


def _reduce_hook_event(slots: dict[str, dict], event: HookEvent, now: int) -> list[Effect]:
    record = slots.get(event.slot_key)
    if not record:
        return []
    effects: list[Effect] = []
    if event.name == "Notification":
        # DIAGNOSTIC, not yet behavioral: Claude Code's Notification hook
        # fires for more than "needs your permission" (confirmed live —
        # an idle/suggested-next-prompt state also fires it), but every
        # Notification currently maps straight to needs_input regardless
        # of what it actually says — the "Notification-text matching" gap
        # SWITCHBOARD_DESIGN.md already flags as unimplemented. Log the
        # raw message so a real mapping can be calibrated from evidence
        # instead of guessed at.
        message = event.payload.get("message", "<no message field>")
        effects.append(Log(f"slot {event.slot_key}: Notification fired: {message!r}"))
    dirty = _apply_hook_event(slots, event.slot_key, record, event.name, event.payload, now)
    if dirty:
        effects.append(SendUpdate(event.slot_key))
    return effects


def _apply_hook_event(slots: dict[str, dict], slot_key: str, record: dict, event: str, payload: dict, now: int) -> bool:
    """Mutates `record` in place (or pops it from `slots`, for an "empty"
    status). Returns whether anything changed.

    This carries over Phase 0's rules exactly — see test_reducer.py for
    the full event table.
    """
    # Guard against a second session (e.g. a relaunch that reused
    # SWITCHBOARD_SLOT before the old one's SessionEnd freed it) sending
    # hooks that would otherwise corrupt this slot's state. The slot's
    # owner is whichever session_id first claims it via SessionStart;
    # hooks from any other session_id are dropped rather than applied.
    incoming = payload.get("session_id")
    owner = record.get("session_id")
    if event == "SessionStart" and incoming:
        if owner and owner != incoming:
            return False  # a second session claims an owned slot: ignore it
        record["session_id"] = incoming
    elif incoming and owner and incoming != owner:
        return False  # stale/foreign session: ignore

    # A subagent (Task-tool run) can be dispatched to run in the background
    # and keep going after the main turn's Stop event fires — Stop only
    # means "the main turn is done", not "this slot is idle". Without this
    # tracking, a slot would flash to "done"/green (or whatever the next
    # main-thread status is) while a background subagent it just launched
    # is still visibly working. SubagentStart/SubagentStop bracket each
    # subagent run (by agent_id, so duplicate or out-of-order delivery
    # can't double-count) and gate the deferred "done" until every subagent
    # from this turn has actually finished.
    if event in ("SubagentStart", "SubagentStop"):
        agent_id = payload.get("agent_id")
        agent_id = str(agent_id) if agent_id else None
        if not agent_id:
            return False  # can't track reliably without an id; ignore
        active = set(record.get("active_subagents", []))
        if event == "SubagentStart":
            if agent_id in active:
                return False
            active.add(agent_id)
            record["active_subagents"] = sorted(active)
            if record.get("status") not in BUSY_STATUSES:
                set_status(record, "working", now)
            return True
        # SubagentStop
        if agent_id not in active:
            return False
        active.discard(agent_id)
        record["active_subagents"] = sorted(active)
        dirty = True
        if not active and record.pop("main_stopped", False):
            # The main turn already finished (Stop already fired) and this
            # was the last subagent still outstanding — deliver the
            # deferred "done" now.
            set_status(record, "done", now)
        return dirty

    status = hook_status_for(record.get("family", ""), event)
    if not status:
        return False
    if status == "empty":
        # SessionEnd/`/exit`: actually free the slot (pop it), not just
        # flip a status field — auto-launch and the slot cells only treat
        # a MISSING record as available.
        slots.pop(slot_key, None)
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
            # (e.g. a prompt sent mid-turn), since busy_elapsed_ms measures
            # the current turn, not cumulative busy time. This must happen
            # even when status doesn't change (busy -> busy).
            record["busy_since"] = now
            dirty = True
        return dirty
    if status == "done" and record.get("active_subagents"):
        # Defer: a subagent this turn dispatched is still running.
        # SubagentStop will deliver "done" once the last one finishes.
        record["main_stopped"] = True
        return True
    set_status(record, status, now)
    if event == "UserPromptSubmit":
        record["busy_since"] = now
    record.pop("main_stopped", None)
    return True
