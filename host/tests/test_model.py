from switchboard import model


def test_effort_args_clamps_codex_xhigh_to_high():
    assert model.effort_args("codex", "xhigh") == ["-c", "model_reasoning_effort=high"]


def test_effort_args_clamps_codex_max_to_high():
    assert model.effort_args("codex", "max") == ["-c", "model_reasoning_effort=high"]


def test_effort_args_claude_passes_through_known_values():
    assert model.effort_args("claude", "xhigh") == ["--effort", "xhigh"]


def test_effort_args_unknown_family_returns_empty():
    assert model.effort_args("shell", "high") == []


def test_command_with_effort_shell_family_unchanged():
    assert model.command_with_effort("cat", "shell", "high") == "cat"


def test_command_with_effort_appends_flags():
    assert model.command_with_effort("claude", "claude", "high") == "claude --effort high"


def test_normalize_effort_aliases_med_to_medium():
    assert model.normalize_effort("med") == "medium"


def test_normalize_effort_defaults_to_medium():
    assert model.normalize_effort(None) == "medium"


def test_slot_record_shape():
    record = model.slot_record(
        slot=2, name="A", family="claude", cwd="/tmp/x", command="claude",
        terminal_title="title", now=1_000_000,
    )
    assert record["slot"] == 2
    assert record["status"] == "launched"
    assert record["last_launched_at"] == 1_000_000
    assert record["effort"] == "medium"


def test_set_status_tracks_busy_since():
    record = {"status": "idle"}
    model.set_status(record, "working", 1_000_000)
    assert record["busy_since"] == 1_000_000
    model.set_status(record, "working", 1_000_010)  # still busy: unchanged
    assert record["busy_since"] == 1_000_000
    model.set_status(record, "done", 1_000_020)
    assert "busy_since" not in record


def test_agent_update_event_busy_elapsed_ms():
    record = {"slot": 1, "status": "working", "busy_since": 1_000_000}
    event = model.agent_update_event(record, 1_000_012.345)
    assert event["busy_elapsed_ms"] == 12345


def test_agent_update_event_not_busy_is_zero():
    record = {"slot": 1, "status": "idle"}
    event = model.agent_update_event(record, 1_000_000.0)
    assert event["busy_elapsed_ms"] == 0


def test_hook_status_for_claude_and_codex():
    assert model.hook_status_for("claude", "Stop") == "done"
    assert model.hook_status_for("codex", "PermissionRequest") == "needs_input"
    assert model.hook_status_for("shell", "Stop") is None
