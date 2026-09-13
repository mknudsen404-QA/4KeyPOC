import builtins
import subprocess

import pytest

from switchboard import model
from switchboard.events import BoardEvent, HookEvent, LivenessObserved, SlotRegistered
from switchboard.reducer import Effect, Focus, Launch, ReducerState, SendUpdate, VoiceKey, reduce

NOW = 1_000_000


def make_record(*, slot=1, family="claude", status="idle", session_id=None, active_subagents=None, **extra):
    record = {"slot": slot, "name": "Test", "family": family, "status": status}
    if session_id is not None:
        record["session_id"] = session_id
    if active_subagents is not None:
        record["active_subagents"] = active_subagents
    record.update(extra)
    return record


def hook_body(**overrides):
    return overrides


def do_hook(slots, event, body=None, now=NOW):
    state = ReducerState()
    return reduce(slots, state, HookEvent("1", event, body or {}), now, auto_launch=False)


# ---- Ported from Phase 0's test_hook_reducer.py -----------------------


@pytest.mark.parametrize("event,expected_status", model.CLAUDE_HOOK_STATUS.items())
def test_claude_event_table(event, expected_status):
    record = make_record(family="claude", status="__initial__")
    slots = {"1": record}
    slots, _ = do_hook(slots, event)
    if expected_status == "empty":
        assert "1" not in slots
    else:
        assert slots["1"]["status"] == expected_status


@pytest.mark.parametrize("event,expected_status", model.CODEX_HOOK_STATUS.items())
def test_codex_event_table(event, expected_status):
    record = make_record(family="codex", status="__initial__")
    slots = {"1": record}
    slots, _ = do_hook(slots, event)
    if expected_status == "empty":
        assert "1" not in slots
    else:
        assert slots["1"]["status"] == expected_status


def test_session_end_pops_slot():
    slots = {"1": make_record(status="working")}
    slots, effects = do_hook(slots, "SessionEnd")
    assert "1" not in slots
    assert effects == [SendUpdate("1")]


def test_busy_since_set_on_enter_busy():
    slots = {"1": make_record(status="idle")}
    slots, _ = do_hook(slots, "UserPromptSubmit")
    assert slots["1"]["status"] == "working"
    assert slots["1"]["busy_since"] == NOW


def test_busy_since_preserved_across_post_tool_use():
    slots = {"1": make_record(status="idle")}
    slots, _ = do_hook(slots, "UserPromptSubmit")
    busy_since = slots["1"]["busy_since"]
    slots, _ = do_hook(slots, "PostToolUse", now=NOW + 30)
    assert slots["1"]["busy_since"] == busy_since


def test_new_prompt_resets_busy_since():
    slots = {"1": make_record(status="idle")}
    slots, _ = do_hook(slots, "UserPromptSubmit")
    slots, _ = do_hook(slots, "UserPromptSubmit", now=NOW + 30)
    assert slots["1"]["busy_since"] == NOW + 30


def test_busy_since_cleared_on_stop():
    slots = {"1": make_record(status="idle")}
    slots, _ = do_hook(slots, "UserPromptSubmit")
    assert "busy_since" in slots["1"]
    slots, _ = do_hook(slots, "Stop")
    assert "busy_since" not in slots["1"]


def test_stop_deferred_while_subagent_active():
    slots = {"1": make_record(status="working")}
    slots, _ = do_hook(slots, "SubagentStart", hook_body(agent_id="ag-1"))
    slots, effects = do_hook(slots, "Stop")
    assert effects == [SendUpdate("1")]
    assert slots["1"]["status"] == "working"
    assert slots["1"].get("main_stopped") is True


def test_subagent_stop_delivers_deferred_done():
    slots = {"1": make_record(status="working")}
    slots, _ = do_hook(slots, "SubagentStart", hook_body(agent_id="ag-1"))
    slots, _ = do_hook(slots, "Stop")
    slots, effects = do_hook(slots, "SubagentStop", hook_body(agent_id="ag-1"))
    assert effects == [SendUpdate("1")]
    assert slots["1"]["status"] == "done"
    assert "main_stopped" not in slots["1"]


def test_duplicate_subagent_start_ignored():
    slots = {"1": make_record(status="working")}
    slots, effects1 = do_hook(slots, "SubagentStart", hook_body(agent_id="ag-1"))
    assert effects1 == [SendUpdate("1")]
    slots, effects2 = do_hook(slots, "SubagentStart", hook_body(agent_id="ag-1"))
    assert effects2 == []


def test_new_prompt_clears_main_stopped():
    slots = {"1": make_record(status="working")}
    slots, _ = do_hook(slots, "SubagentStart", hook_body(agent_id="ag-1"))
    slots, _ = do_hook(slots, "Stop")
    assert slots["1"].get("main_stopped") is True
    slots, _ = do_hook(slots, "UserPromptSubmit")
    assert "main_stopped" not in slots["1"]


def test_session_id_adopted_on_session_start():
    slots = {"1": make_record(status="launched")}
    slots, _ = do_hook(slots, "SessionStart", hook_body(session_id="s-aaa"))
    assert slots["1"]["session_id"] == "s-aaa"
    assert slots["1"]["status"] == "idle"


def test_foreign_session_id_ignored():
    slots = {"1": make_record(status="idle", session_id="s-aaa")}
    slots, effects = do_hook(slots, "UserPromptSubmit", hook_body(session_id="s-bbb"))
    assert effects == []
    assert slots["1"]["status"] == "idle"


def test_second_session_start_does_not_steal_slot():
    slots = {"1": make_record(status="idle", session_id="s-aaa")}
    slots, effects = do_hook(slots, "SessionStart", hook_body(session_id="s-bbb"))
    assert effects == []
    assert slots["1"]["session_id"] == "s-aaa"


def test_legacy_record_without_session_id_accepts_events():
    slots = {"1": make_record(status="idle")}  # no session_id key at all
    slots, effects = do_hook(slots, "UserPromptSubmit", hook_body(session_id="s-aaa"))
    assert effects == [SendUpdate("1")]
    assert slots["1"]["status"] == "working"


def test_foreign_session_ignored_golden_scenario():
    # §1.3c
    slots = {"1": make_record(status="working", session_id="s-real")}
    slots, effects = do_hook(slots, "Stop", hook_body(session_id="s-fake"))
    assert effects == []
    assert slots["1"]["status"] == "working"


# ---- New: liveness-freeing rules (ported from Phase 0's test_liveness.py) --


def test_liveness_observed_frees_after_two_consecutive_dead():
    slots = {"1": {"slot": 1, "status": "idle"}}
    state = ReducerState()
    slots, effects = reduce(slots, state, LivenessObserved({"1": model.Liveness.DEAD}), NOW, auto_launch=False)
    assert effects == []
    assert "1" in slots

    slots, effects = reduce(slots, state, LivenessObserved({"1": model.Liveness.DEAD}), NOW, auto_launch=False)
    assert effects == [SendUpdate("1")]
    assert "1" not in slots


def test_liveness_observed_unknown_resets_dead_count():
    slots = {"1": {"slot": 1, "status": "idle"}}
    state = ReducerState()
    reduce(slots, state, LivenessObserved({"1": model.Liveness.DEAD}), NOW, auto_launch=False)
    reduce(slots, state, LivenessObserved({"1": model.Liveness.UNKNOWN}), NOW, auto_launch=False)
    slots, effects = reduce(slots, state, LivenessObserved({"1": model.Liveness.DEAD}), NOW, auto_launch=False)
    assert effects == []  # count reset to 1, not the confirming 2nd
    assert "1" in slots


def test_liveness_observed_startup_frees_on_first_dead():
    slots = {"1": {"slot": 1, "status": "idle"}}
    state = ReducerState()
    slots, effects = reduce(
        slots, state, LivenessObserved({"1": model.Liveness.DEAD}, confirm=False), NOW, auto_launch=False
    )
    assert effects == [SendUpdate("1")]
    assert "1" not in slots


# ---- New: reducer never performs I/O -----------------------------------


def test_reduce_never_performs_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("reduce() must never touch I/O")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("os.write", boom)
    monkeypatch.setattr(builtins, "open", boom)

    events = [
        BoardEvent("agent.select", {"slot": 1}),
        BoardEvent("agent.select", {"slot": 1, "liveness": "alive"}),
        BoardEvent("agent.reasoning.apply", {"slot": 1, "effort": "high"}),
        BoardEvent("voice.hold.start", {"slot": 1}),
        BoardEvent("voice.hold.stop", {"slot": 1}),
        BoardEvent("agent.update.ack", {"slot": 1}),
        BoardEvent("agent.focus", {"slot": 1}),
        BoardEvent("plan.approve", {}),
        BoardEvent("review.request", {}),
        BoardEvent("slash.run", {"command": "/foo"}),
        BoardEvent("mystery.event", {}),
        HookEvent("1", "UserPromptSubmit", {}),
        LivenessObserved({"1": model.Liveness.DEAD}),
        SlotRegistered({"slot": 1, "name": "A", "status": "launched"}),
    ]
    slots = {"1": make_record(status="idle", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    for event in events:
        slots, effects = reduce(slots, state, event, NOW, auto_launch=True)
        for effect in effects:
            assert isinstance(effect, Effect)


# ---- New: select/liveness interaction -----------------------------------


def test_select_on_unknown_emits_no_launch():
    slots = {"1": make_record(status="launched")}  # no terminal_tty
    state = ReducerState()
    slots, effects = reduce(
        slots, state, BoardEvent("agent.select", {"slot": 1, "liveness": "unknown"}), NOW, auto_launch=True
    )
    assert not any(isinstance(e, Launch) for e in effects)
    assert "1" in slots


def test_select_on_dead_first_press_does_not_free():
    slots = {"1": make_record(status="idle", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    slots, effects = reduce(
        slots, state, BoardEvent("agent.select", {"slot": 1, "liveness": "dead"}), NOW, auto_launch=True
    )
    assert "1" in slots
    assert not any(isinstance(e, SendUpdate) for e in effects)
    assert not any(isinstance(e, Launch) for e in effects)


def test_select_on_dead_second_press_frees_and_launches():
    slots = {"1": make_record(status="idle", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    reduce(slots, state, BoardEvent("agent.select", {"slot": 1, "liveness": "dead"}), NOW, auto_launch=True)
    slots, effects = reduce(
        slots, state, BoardEvent("agent.select", {"slot": 1, "liveness": "dead"}), NOW, auto_launch=True
    )
    assert "1" not in slots
    assert SendUpdate("1") in effects
    assert Launch(1) in effects


def test_select_with_auto_launch_off_only_sends_update():
    state = ReducerState()
    slots: dict[str, dict] = {}
    slots, effects = reduce(slots, state, BoardEvent("agent.select", {"slot": 1}), NOW, auto_launch=False)
    assert effects == [SendUpdate("1")]


def test_select_empty_slot_with_auto_launch_launches():
    state = ReducerState()
    slots: dict[str, dict] = {}
    slots, effects = reduce(slots, state, BoardEvent("agent.select", {"slot": 1}), NOW, auto_launch=True)
    assert effects == [Launch(1)]


def test_effects_order_focus_before_send_update():
    slots = {"1": make_record(status="idle", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    slots, effects = reduce(
        slots, state, BoardEvent("agent.select", {"slot": 1, "liveness": "alive"}), NOW, auto_launch=False
    )
    kinds = [type(e) for e in effects if isinstance(e, (Focus, SendUpdate))]
    assert kinds == [Focus, SendUpdate]


def test_reasoning_apply_sets_effort_only():
    slots = {"1": make_record(status="working", busy_since=NOW - 10)}
    state = ReducerState()
    slots, effects = reduce(
        slots, state, BoardEvent("agent.reasoning.apply", {"slot": 1, "effort": "high"}), NOW, auto_launch=False
    )
    assert slots["1"]["effort"] == "high"
    assert slots["1"]["status"] == "working"
    assert slots["1"]["busy_since"] == NOW - 10
    assert SendUpdate("1") in effects


def test_voice_hold_start_requires_claude_family():
    slots = {"1": make_record(family="codex", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    slots, effects = reduce(slots, state, BoardEvent("voice.hold.start", {"slot": 1}), NOW, auto_launch=False)
    assert not any(isinstance(e, VoiceKey) for e in effects)
    assert state.mic_active is False


def test_voice_hold_start_emits_focus_and_key_down():
    slots = {"1": make_record(family="claude", terminal_tty="/dev/ttys001")}
    state = ReducerState()
    slots, effects = reduce(slots, state, BoardEvent("voice.hold.start", {"slot": 1}), NOW, auto_launch=False)
    assert Focus("/dev/ttys001") in effects
    assert VoiceKey(True) in effects
    assert state.mic_active is True


def test_voice_hold_stop_emits_key_up():
    slots = {"1": make_record(family="claude", terminal_tty="/dev/ttys001")}
    state = ReducerState(mic_active=True)
    slots, effects = reduce(slots, state, BoardEvent("voice.hold.stop", {"slot": 1}), NOW, auto_launch=False)
    assert VoiceKey(False) in effects
    assert state.mic_active is False


def test_slot_registered_stores_record_and_sends_update():
    slots: dict[str, dict] = {}
    state = ReducerState()
    record = {"slot": 2, "name": "A", "status": "launched"}
    slots, effects = reduce(slots, state, SlotRegistered(record), NOW, auto_launch=False)
    assert slots["2"] == record
    assert effects == [SendUpdate("2")]
