import pytest

import switchboard_bridge as sb
from tests.conftest import hook_body

apply_hook_event = sb._HookRequestHandler._apply_hook_event


def make_record(*, family="claude", status="idle", session_id=None, active_subagents=None, **extra):
    record = {
        "slot": 1,
        "name": "Test",
        "family": family,
        "status": status,
    }
    if session_id is not None:
        record["session_id"] = session_id
    if active_subagents is not None:
        record["active_subagents"] = active_subagents
    record.update(extra)
    return record


def apply(registry, record, event, body=b"{}"):
    return apply_hook_event(registry, "1", record, event, body)


@pytest.mark.parametrize("event,expected_status", sb.CLAUDE_HOOK_STATUS.items())
def test_claude_event_table(event, expected_status, fake_clock):
    record = make_record(family="claude", status="__initial__")
    registry = {"slots": {"1": record}}
    apply(registry, record, event)
    if expected_status == "empty":
        assert "1" not in registry["slots"]
    else:
        assert record["status"] == expected_status


@pytest.mark.parametrize("event,expected_status", sb.CODEX_HOOK_STATUS.items())
def test_codex_event_table(event, expected_status, fake_clock):
    record = make_record(family="codex", status="__initial__")
    registry = {"slots": {"1": record}}
    apply(registry, record, event)
    if expected_status == "empty":
        assert "1" not in registry["slots"]
    else:
        assert record["status"] == expected_status


def test_session_end_pops_slot(fake_clock):
    record = make_record(status="working")
    registry = {"slots": {"1": record}}
    dirty = apply(registry, record, "SessionEnd")
    assert dirty is True
    assert "1" not in registry["slots"]


def test_busy_since_set_on_enter_busy(fake_clock):
    record = make_record(status="idle")
    registry = {"slots": {"1": record}}
    apply(registry, record, "UserPromptSubmit")
    assert record["status"] == "working"
    assert record["busy_since"] == int(fake_clock.now())


def test_busy_since_preserved_across_post_tool_use(fake_clock):
    record = make_record(status="idle")
    registry = {"slots": {"1": record}}
    apply(registry, record, "UserPromptSubmit")
    busy_since = record["busy_since"]
    fake_clock.advance(30)
    apply(registry, record, "PostToolUse")
    assert record["busy_since"] == busy_since


def test_new_prompt_resets_busy_since(fake_clock):
    record = make_record(status="idle")
    registry = {"slots": {"1": record}}
    apply(registry, record, "UserPromptSubmit")
    fake_clock.advance(30)
    new_now = int(fake_clock.now())
    apply(registry, record, "UserPromptSubmit")
    assert record["busy_since"] == new_now


def test_busy_since_cleared_on_stop(fake_clock):
    record = make_record(status="idle")
    registry = {"slots": {"1": record}}
    apply(registry, record, "UserPromptSubmit")
    assert "busy_since" in record
    apply(registry, record, "Stop")
    assert "busy_since" not in record


def test_effort_change_does_not_touch_busy_since(registry_path, fake_clock):
    record = make_record(status="idle")
    sb.save_registry({"version": 1, "slots": {"1": record}}, registry_path)
    with sb.registry_transaction(registry_path) as registry:
        registry["slots"]["1"] = make_record(status="working")
        registry["slots"]["1"]["busy_since"] = int(fake_clock.now())
    sb.update_registry_slot_status(registry_path, 1, effort="high")
    updated = sb.load_registry(registry_path)["slots"]["1"]
    assert updated["status"] == "working"
    assert updated["busy_since"] == int(fake_clock.now())
    assert updated["effort"] == "high"


def test_stop_deferred_while_subagent_active(fake_clock):
    record = make_record(status="working")
    registry = {"slots": {"1": record}}
    apply(registry, record, "SubagentStart", hook_body("SubagentStart", agent_id="ag-1"))
    dirty = apply(registry, record, "Stop")
    assert dirty is True
    assert record["status"] == "working"
    assert record.get("main_stopped") is True


def test_subagent_stop_delivers_deferred_done(fake_clock):
    record = make_record(status="working")
    registry = {"slots": {"1": record}}
    apply(registry, record, "SubagentStart", hook_body("SubagentStart", agent_id="ag-1"))
    apply(registry, record, "Stop")
    dirty = apply(registry, record, "SubagentStop", hook_body("SubagentStop", agent_id="ag-1"))
    assert dirty is True
    assert record["status"] == "done"
    assert "main_stopped" not in record


def test_duplicate_subagent_start_ignored(fake_clock):
    record = make_record(status="working")
    registry = {"slots": {"1": record}}
    assert apply(registry, record, "SubagentStart", hook_body("SubagentStart", agent_id="ag-1")) is True
    assert apply(registry, record, "SubagentStart", hook_body("SubagentStart", agent_id="ag-1")) is False


def test_new_prompt_clears_main_stopped(fake_clock):
    record = make_record(status="working")
    registry = {"slots": {"1": record}}
    apply(registry, record, "SubagentStart", hook_body("SubagentStart", agent_id="ag-1"))
    apply(registry, record, "Stop")
    assert record.get("main_stopped") is True
    apply(registry, record, "UserPromptSubmit")
    assert "main_stopped" not in record


def test_session_id_adopted_on_session_start(fake_clock):
    record = make_record(status="launched")
    registry = {"slots": {"1": record}}
    apply(registry, record, "SessionStart", hook_body("SessionStart", session_id="s-aaa"))
    assert record["session_id"] == "s-aaa"
    assert record["status"] == "idle"


def test_foreign_session_id_ignored(fake_clock):
    record = make_record(status="idle", session_id="s-aaa")
    registry = {"slots": {"1": record}}
    dirty = apply(registry, record, "UserPromptSubmit", hook_body("UserPromptSubmit", session_id="s-bbb"))
    assert dirty is False
    assert record["status"] == "idle"


def test_second_session_start_does_not_steal_slot(fake_clock):
    record = make_record(status="idle", session_id="s-aaa")
    registry = {"slots": {"1": record}}
    dirty = apply(registry, record, "SessionStart", hook_body("SessionStart", session_id="s-bbb"))
    assert dirty is False
    assert record["session_id"] == "s-aaa"


def test_legacy_record_without_session_id_accepts_events(fake_clock):
    record = make_record(status="idle")  # no session_id key at all
    registry = {"slots": {"1": record}}
    dirty = apply(registry, record, "UserPromptSubmit", hook_body("UserPromptSubmit", session_id="s-aaa"))
    assert dirty is True
    assert record["status"] == "working"


def test_agent_update_event_busy_elapsed_ms(fake_clock):
    record = make_record(status="idle")
    registry = {"slots": {"1": record}}
    apply(registry, record, "UserPromptSubmit")
    fake_clock.advance(12.345)
    event = sb.agent_update_event(record)
    assert event["busy_elapsed_ms"] == 12345


def test_agent_update_event_not_busy_is_zero(fake_clock):
    record = make_record(status="idle")
    event = sb.agent_update_event(record)
    assert event["busy_elapsed_ms"] == 0
