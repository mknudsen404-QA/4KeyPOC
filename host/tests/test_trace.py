import json

from switchboard.clock import FakeClock
from switchboard.trace import NullTraceWriter, TraceWriter, redact_hook_payload


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_write_adds_timestamp_and_monotonic(tmp_path):
    clock = FakeClock(start=1_700_000_000.5)
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=clock)
    writer.write({"kind": "board", "event": "agent.select"})
    lines = read_lines(tmp_path / "trace.jsonl")
    assert len(lines) == 1
    assert lines[0]["kind"] == "board"
    assert lines[0]["ts"].endswith("Z")
    assert lines[0]["mono"] == 1_700_000_000.5


def test_write_preserves_explicit_ts_and_mono(tmp_path):
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=FakeClock())
    writer.write({"kind": "x", "ts": "fixed", "mono": 1.0})
    lines = read_lines(tmp_path / "trace.jsonl")
    assert lines[0]["ts"] == "fixed"
    assert lines[0]["mono"] == 1.0


def test_one_line_per_write_valid_json(tmp_path):
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=FakeClock())
    for i in range(5):
        writer.write({"kind": "heartbeat", "i": i})
    lines = read_lines(tmp_path / "trace.jsonl")
    assert [line["i"] for line in lines] == [0, 1, 2, 3, 4]


def test_rotation_keeps_backup_count(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, max_bytes=200, backup_count=2, clock=FakeClock())
    for i in range(200):
        writer.write({"kind": "heartbeat", "i": i, "pad": "x" * 20})
    assert path.exists()
    assert (tmp_path / "trace.jsonl.1").exists()
    assert (tmp_path / "trace.jsonl.2").exists()
    assert not (tmp_path / "trace.jsonl.3").exists()


def test_open_failure_disables_writer_and_reports_once(tmp_path):
    # Point the path at something that can't be a directory (a file in
    # place of the parent dir) to force an OSError on open.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    errors = []
    writer = TraceWriter(blocker / "trace.jsonl", clock=FakeClock(), on_error=errors.append)
    writer.write({"kind": "board"})
    writer.write({"kind": "board"})
    assert len(errors) == 1
    assert "trace" in errors[0].lower()


def test_write_failure_after_open_disables_writer(tmp_path, monkeypatch):
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=FakeClock())
    errors = []
    writer._on_error = errors.append

    def boom(_msg):
        raise OSError("disk full")

    monkeypatch.setattr(writer._logger, "info", boom)
    writer.write({"kind": "board"})
    assert len(errors) == 1
    # Now dead: further writes are silent no-ops, not more errors.
    writer.write({"kind": "board"})
    assert len(errors) == 1


def test_null_trace_writer_is_a_no_op(tmp_path):
    writer = NullTraceWriter()
    writer.write({"kind": "board"})  # must not raise
    writer.close()


def test_redact_hook_payload_keeps_only_allowed_fields():
    payload = {
        "session_id": "abc-123",
        "notification_type": "permission_prompt",
        "tool_name": "shell",
        "agent_id": "sub-1",
        "prompt": "do the thing with my SSN 123-45-6789",
        "tool_input": {"command": "rm -rf /"},
        "tool_response": {"output": "secret"},
        "cwd": "/Users/matt/private-project",
        "transcript_path": "/Users/matt/.claude/transcripts/x.jsonl",
        "permission_mode": "acceptEdits",
    }
    out = redact_hook_payload(payload)
    assert set(out.keys()) == {"session", "nt", "tool", "agent"}
    assert out["nt"] == "permission_prompt"
    assert out["tool"] == "shell"
    assert out["agent"] == "sub-1"
    assert len(out["session"]) == 8
    dumped = json.dumps(out)
    assert "do the thing" not in dumped
    assert "rm -rf" not in dumped
    assert "private-project" not in dumped


def test_redact_hook_payload_session_hash_is_stable_and_not_reversible():
    a = redact_hook_payload({"session_id": "same-session"})
    b = redact_hook_payload({"session_id": "same-session"})
    c = redact_hook_payload({"session_id": "different-session"})
    assert a["session"] == b["session"]
    assert a["session"] != c["session"]
    assert "same-session" not in a["session"]


def test_redact_hook_payload_missing_fields_are_omitted():
    assert redact_hook_payload({}) == {}
    assert redact_hook_payload({"tool_name": "shell"}) == {"tool": "shell"}


def test_default_writer_is_not_verbose(tmp_path):
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=FakeClock())
    assert writer.verbose is False


def test_verbose_writer_flag_is_set(tmp_path):
    writer = TraceWriter(tmp_path / "trace.jsonl", clock=FakeClock(), verbose=True)
    assert writer.verbose is True
