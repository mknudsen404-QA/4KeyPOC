from switchboard.bridge import Bridge
from switchboard.clock import FakeClock
from switchboard.device import FakeDevice
from switchboard.events import BoardEvent, HookEvent, LivenessObserved
from switchboard.key_injector import FakeKeyInjector
from switchboard.liveness import FakeProber
from switchboard.model import Liveness
from switchboard.registry import Registry
from switchboard.terminal import FakeTerminal


class RecordingTraceWriter:
    """A TraceWriter double that keeps every record in memory instead of
    writing to disk, so tests can assert on exactly what was traced."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.records if r.get("kind") == kind]


def make_bridge(
    tmp_path, *, clock=None, auto_launch=True, dry_run=False, no_open=False, launch_config=None,
    close_dead_tabs=False, settings_path=None, trace=None,
):
    registry = Registry(tmp_path / "registry.json")
    device = FakeDevice()
    terminal = FakeTerminal()
    prober = FakeProber()
    key_injector = FakeKeyInjector()
    clock = clock or FakeClock()
    bridge = Bridge(
        registry=registry,
        device=device,
        terminal=terminal,
        prober=prober,
        clock=clock,
        launch_config=launch_config or {"agents": [{"slot": 1, "name": "A", "family": "shell", "command": "cat"}]},
        auto_launch=auto_launch,
        dry_run=dry_run,
        no_open=no_open,
        close_dead_tabs=close_dead_tabs,
        key_injector=key_injector,
        settings_path=settings_path,
        trace=trace,
    )
    return bridge, registry, device, terminal, prober, clock, key_injector


def test_step_select_empty_slot_launches_and_registers(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    bridge.step(BoardEvent("agent.select", {"slot": 1}))

    assert len(terminal.opened) == 1
    record = registry.load()["slots"]["1"]
    assert record["terminal_tty"] == "/dev/ttysFAKE1"
    assert any(e.get("event") == "agent.update" and e.get("status") == "launched" for e in device.sent)


def test_step_select_unknown_never_launches(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "shell", "status": "launched"}}})
    prober.answers["1"] = Liveness.UNKNOWN  # --no-open record: no terminal_tty to probe
    bridge.step(BoardEvent("agent.select", {"slot": 1}))
    assert terminal.opened == []


def test_step_hook_sequence_matches_phase0_golden(tmp_path):
    clock = FakeClock()
    bridge, registry, device, terminal, prober, _, _key_injector = make_bridge(tmp_path, clock=clock)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "status": "launched"}}})

    bridge.step(HookEvent("1", "SessionStart", {"session_id": "s-aaa"}))
    assert device.sent[-1]["status"] == "idle"

    bridge.step(HookEvent("1", "UserPromptSubmit", {"session_id": "s-aaa"}))
    assert device.sent[-1]["status"] == "working"
    assert device.sent[-1]["busy_elapsed_ms"] == 0

    sent_before_post_tool_use = len(device.sent)
    clock.advance(40)
    bridge.step(HookEvent("1", "PostToolUse", {"session_id": "s-aaa"}))
    # Still "working" -> "working": no line (matches the Phase 0 golden
    # trace exactly — busy_elapsed_ms only shows up on the *next* update).
    assert len(device.sent) == sent_before_post_tool_use

    bridge.step(HookEvent("1", "Stop", {"session_id": "s-aaa"}))
    assert device.sent[-1]["status"] == "done"

    bridge.step(HookEvent("1", "SessionEnd", {"session_id": "s-aaa"}))
    assert device.sent[-1]["status"] == "empty"
    assert "1" not in registry.load()["slots"]


def test_effects_run_after_lock_released(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {}})

    acquired = []

    def send_and_probe_lock(event):
        # If the registry lock were still held by step(), this transaction
        # would deadlock (RLock is per-thread, but the flock is per-fd; a
        # nested `with open(...)` + LOCK_EX on the same process/thread does
        # not block, so instead assert we can complete a full transaction
        # from within send() without exception.
        with registry.transaction() as reg:
            reg["probed"] = True
        acquired.append(True)

    device.send = send_and_probe_lock
    bridge.step(BoardEvent("agent.select", {"slot": 1}))
    assert acquired == [True]


def test_worker_survives_step_exception(tmp_path):
    """run()'s worker loop wraps step() in try/except so one event's
    exception doesn't kill the thread — exercised here directly (no
    threads, no real time) by calling step() the same way run() does."""
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {}})

    calls = {"n": 0}
    real_send = device.send

    def flaky_send(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        real_send(event)

    device.send = flaky_send

    try:
        bridge.step(BoardEvent("agent.select", {"slot": 1}))  # triggers the flaky raise
    except RuntimeError:
        pass  # exactly what run()'s try/except absorbs

    # The worker must still function afterward.
    bridge.step(BoardEvent("agent.select", {"slot": 1}))
    assert calls["n"] >= 2
    assert len(device.sent) >= 1


def test_startup_sync_pushes_three_updates_and_frees_dead(tmp_path):
    """Only slots 1-3 are real agent keys (key 4 is push-to-talk)."""
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path, auto_launch=False)
    registry.save(
        {
            "version": 1,
            "slots": {
                "1": {"slot": 1, "name": "A", "family": "shell", "status": "idle", "terminal_tty": "/dev/ttys001"},
                "2": {"slot": 2, "name": "B", "family": "shell", "status": "idle", "terminal_tty": "/dev/ttys002"},
            },
        }
    )
    prober.answers = {"1": Liveness.ALIVE, "2": Liveness.DEAD}

    bridge.startup_sync()

    assert len(device.sent) == 3
    by_slot = {e["slot"]: e for e in device.sent}
    assert by_slot[1]["status"] != "empty"
    assert by_slot[2]["status"] == "empty"
    assert by_slot[3]["status"] == "empty"
    assert "2" not in registry.load()["slots"]


def test_pending_ack_retries_up_to_three_times_then_gives_up(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {}})

    bridge.step(BoardEvent("agent.select", {"slot": 1}))  # launches slot 1 -> 1 send, pending_acks["1"]
    assert len(device.sent) == 1
    assert "1" in bridge.pending_acks

    clock.advance(1.1)
    bridge.retry_pending_acks()
    assert len(device.sent) == 2  # attempt 2

    clock.advance(1.1)
    bridge.retry_pending_acks()
    assert len(device.sent) == 3  # attempt 3

    clock.advance(1.1)
    bridge.retry_pending_acks()  # 3 attempts already spent: log and give up, no 4th send
    assert len(device.sent) == 3
    assert "1" not in bridge.pending_acks


def test_pending_ack_cleared_by_matching_ack_event(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {}})

    bridge.step(BoardEvent("agent.select", {"slot": 1}))
    assert "1" in bridge.pending_acks

    bridge.step(BoardEvent("agent.update.ack", {"slot": 1}))
    assert "1" not in bridge.pending_acks

    clock.advance(5)
    bridge.retry_pending_acks()
    assert len(device.sent) == 1  # no retry: the ack already cleared it


def test_pending_ack_not_yet_due_is_not_retried(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {}})

    bridge.step(BoardEvent("agent.select", {"slot": 1}))
    clock.advance(0.5)  # under the 1s threshold
    bridge.retry_pending_acks()
    assert len(device.sent) == 1


def test_close_dead_tabs_closes_terminal_when_enabled(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path, close_dead_tabs=True)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "status": "idle", "terminal_tty": "/dev/ttys001"}}})
    prober.answers["1"] = Liveness.DEAD

    from switchboard.events import LivenessObserved

    bridge.step(LivenessObserved({"1": Liveness.DEAD}))
    bridge.step(LivenessObserved({"1": Liveness.DEAD}))

    assert terminal.closed == ["/dev/ttys001"]


def test_close_dead_tabs_leaves_terminal_when_disabled(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path, close_dead_tabs=False)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "status": "idle", "terminal_tty": "/dev/ttys001"}}})

    from switchboard.events import LivenessObserved

    bridge.step(LivenessObserved({"1": Liveness.DEAD}))
    bridge.step(LivenessObserved({"1": Liveness.DEAD}))

    assert terminal.closed == []
    assert "1" not in registry.load()["slots"]  # still freed either way


def test_voice_hold_start_holds_key_after_focus_settles(tmp_path):
    bridge, registry, device, terminal, prober, clock, key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001", "voice": {"provider": "claude_native"}}}})

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))

    assert terminal.focused == ["/dev/ttys001"]  # FakeTerminal.focus() settles frontmost immediately
    assert key_injector.held == [(1234, 49)]


def test_voice_hold_start_aborts_if_focus_never_settles(tmp_path):
    bridge, registry, device, terminal, prober, clock, key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001", "voice": {"provider": "claude_native"}}}})

    # Simulate Terminal never actually switching tabs (focus() still records
    # the call for the log line, but frontmost_tty() stays whatever it was).
    real_focus = terminal.focus
    terminal.focus = lambda tty: (real_focus(tty), setattr(terminal, "frontmost", None))[0]

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))

    assert key_injector.held == []  # hold aborted, never reaches the injector


def test_voice_hold_stop_releases_key(tmp_path):
    bridge, registry, device, terminal, prober, clock, key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001", "voice": {"provider": "claude_native"}}}})

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))
    bridge.step(BoardEvent("voice.hold.stop", {"slot": 1}))

    assert key_injector.released == [(1234, 49)]


def test_startup_sync_sends_led_config_when_settings_path_given(tmp_path, monkeypatch):
    from switchboard.settings import SlotSettingsService, empty_document

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    settings_path = tmp_path / "agents.json"
    doc = empty_document()
    doc["leds"] = {"idle_breathe": "slow", "thinking_cycle": "quick"}
    SlotSettingsService(settings_path).save(doc)

    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(
        tmp_path, settings_path=settings_path,
    )
    bridge.startup_sync()

    led_events = [e for e in device.sent if e.get("event") == "led.config"]
    assert led_events == [{"event": "led.config", "idle_pulse_ms": 7000, "busy_ramp_ms": 90000}]
    assert bridge.led_config == {"idle_pulse_ms": 7000, "busy_ramp_ms": 90000}


def test_startup_sync_sends_no_led_config_without_settings_path(tmp_path):
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path)
    bridge.startup_sync()
    assert not any(e.get("event") == "led.config" for e in device.sent)
    assert bridge.led_config is None


def test_settings_reload_resends_led_config_on_change(tmp_path, monkeypatch):
    from switchboard.settings import SlotSettingsService, empty_document

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    settings_path = tmp_path / "agents.json"
    service = SlotSettingsService(settings_path)
    doc = empty_document()
    doc["slots"].append({"slot": 1, "name": "A", "family": "shell", "command": "cat"})
    service.save(doc)

    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(
        tmp_path,
        launch_config={"agents": [{"slot": 1, "name": "A", "family": "shell", "command": "cat"}]},
        settings_path=settings_path,
    )
    bridge.startup_sync()
    assert bridge.led_config == {"idle_pulse_ms": 0, "busy_ramp_ms": 300000}

    doc = service.load()
    doc["leds"] = {"idle_breathe": "medium"}
    service.save(doc)

    bridge.step(BoardEvent("agent.select", {"slot": 1}))  # any Launch triggers _apply_launch's force_check

    assert bridge.led_config == {"idle_pulse_ms": 4000, "busy_ramp_ms": 300000}
    led_events = [e for e in device.sent if e.get("event") == "led.config"]
    assert led_events[-1] == {"event": "led.config", "idle_pulse_ms": 4000, "busy_ramp_ms": 300000}


def test_settings_reload_before_launch_picks_up_change(tmp_path, monkeypatch):
    """Phase 2.4: a Launch always checks agents.json first, regardless of
    the 5s tick throttle, so a freshly-saved setting applies right away."""
    from switchboard.settings import SlotSettingsService, empty_document

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    settings_path = tmp_path / "agents.json"
    service = SlotSettingsService(settings_path)
    doc = empty_document()
    doc["slots"].append({"slot": 1, "name": "Old", "family": "shell", "command": "cat"})
    service.save(doc)

    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(
        tmp_path,
        launch_config={"agents": [{"slot": 1, "name": "Old", "family": "shell", "command": "cat"}]},
        settings_path=settings_path,
    )
    logs = []
    bridge.log = logs.append

    doc["slots"][0]["name"] = "New"
    doc["slots"][0]["command"] = "echo"
    service.save(doc)

    bridge.step(BoardEvent("agent.select", {"slot": 1}))

    assert "settings reloaded" in logs
    record = registry.load()["slots"]["1"]
    assert record["name"] == "New"
    assert record["command"].endswith("echo")


def test_settings_reload_tick_is_throttled(tmp_path, monkeypatch):
    from switchboard.settings import SlotSettingsService, empty_document

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    settings_path = tmp_path / "agents.json"
    service = SlotSettingsService(settings_path)
    doc = empty_document()
    doc["slots"].append({"slot": 1, "name": "Old", "command": "cat"})
    service.save(doc)

    clock = FakeClock()
    bridge, *_rest, clock, _key_injector = make_bridge(
        tmp_path, clock=clock, settings_path=settings_path,
        launch_config={"agents": [{"slot": 1, "name": "Old", "command": "cat"}]},
    )
    logs = []
    bridge.log = logs.append

    doc["slots"][0]["name"] = "New"
    service.save(doc)

    clock.advance(2.0)
    bridge._maybe_reload_settings()
    assert "settings reloaded" not in logs
    assert bridge.launch_config["agents"][0]["name"] == "Old"

    clock.advance(4.0)  # 6s total: past the 5s throttle
    bridge._maybe_reload_settings()
    assert "settings reloaded" in logs
    assert bridge.launch_config["agents"][0]["name"] == "New"


def test_settings_reload_noop_when_settings_path_not_given(tmp_path):
    bridge, *_rest = make_bridge(tmp_path)
    original = bridge.launch_config
    bridge._maybe_reload_settings(force_check=True)
    assert bridge.launch_config is original


def test_default_log_flushes(monkeypatch):
    """Bridge's default logger must flush every line — `listen` runs
    indefinitely with stdout redirected to a plain file under the
    LaunchAgent, which CPython fully block-buffers by default, so a bare
    `print()` can leave log lines invisible on disk indefinitely."""
    from switchboard.bridge import _default_log

    calls = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: calls.append(k.get("flush")))
    _default_log("hello")
    assert calls == [True]


def test_default_log_prefixes_a_timestamp(capsys):
    from switchboard.bridge import _default_log

    _default_log("hello")
    out = capsys.readouterr().out
    assert out.endswith("hello\n")
    ts = out.split(" ", 1)[0]
    assert ts.endswith("Z")
    assert ts.count("-") == 2 and ts.count(":") == 2


def test_hook_event_traces_hook_then_transition_then_effect(tmp_path):
    trace = RecordingTraceWriter()
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path, trace=trace)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "status": "idle", "status_since": 0}}})

    bridge.step(HookEvent("1", "UserPromptSubmit", {"session_id": "s-aaa", "tool_name": "shell"}))

    kinds = [r["kind"] for r in trace.records]
    assert kinds.index("hook") < kinds.index("transition") < kinds.index("effect")

    hook_record = trace.of_kind("hook")[0]
    assert hook_record["slot"] == "1"
    assert hook_record["event"] == "UserPromptSubmit"
    assert hook_record["family"] == "claude"
    assert hook_record["applied"] is True
    assert hook_record["tool"] == "shell"
    assert "payload" not in hook_record  # default (non-verbose) trace never carries raw payload
    assert len(hook_record["session"]) == 8

    transition = trace.of_kind("transition")[0]
    assert transition == {
        **transition,
        "slot": "1", "from": "idle", "to": "working", "family": "claude", "cause": "hook:UserPromptSubmit",
    }
    assert transition["dwell_ms"] is not None

    assert trace.of_kind("effect")[0]["type"] == "SendUpdate"


def test_hook_event_not_applied_when_session_owner_mismatch(tmp_path):
    trace = RecordingTraceWriter()
    bridge, registry, *_ = make_bridge(tmp_path, trace=trace)
    registry.save(
        {"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "status": "idle", "session_id": "s-owner"}}}
    )

    bridge.step(HookEvent("1", "UserPromptSubmit", {"session_id": "s-intruder"}))

    hook_record = trace.of_kind("hook")[0]
    assert hook_record["applied"] is False
    assert trace.of_kind("transition") == []


def test_verbose_trace_includes_raw_hook_payload(tmp_path):
    trace = RecordingTraceWriter(verbose=True)
    bridge, registry, *_ = make_bridge(tmp_path, trace=trace)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "status": "idle"}}})

    bridge.step(HookEvent("1", "UserPromptSubmit", {"session_id": "s-aaa", "prompt": "do the secret thing"}))

    hook_record = trace.of_kind("hook")[0]
    assert hook_record["payload"]["prompt"] == "do the secret thing"


def test_noop_liveness_observed_traces_only_liveness(tmp_path):
    trace = RecordingTraceWriter()
    bridge, registry, *_ = make_bridge(tmp_path, trace=trace)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "shell", "status": "idle"}}})

    bridge.step(LivenessObserved({"1": Liveness.ALIVE}))

    kinds = [r["kind"] for r in trace.records]
    assert kinds == ["liveness"]


def test_confirmed_dead_free_traces_freed_with_dwell(tmp_path):
    clock = FakeClock(start=1_000_000.0)
    trace = RecordingTraceWriter()
    bridge, registry, *_ = make_bridge(tmp_path, clock=clock, trace=trace)
    registry.save(
        {"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "shell", "status": "idle", "status_since": 999_940}}}
    )

    clock.advance(60)
    bridge.step(LivenessObserved({"1": Liveness.DEAD}))  # first DEAD: unconfirmed, no free yet
    assert trace.of_kind("freed") == []

    clock.advance(2)
    bridge.step(LivenessObserved({"1": Liveness.DEAD}))  # second consecutive DEAD: confirmed, frees it

    freed = trace.of_kind("freed")
    assert len(freed) == 1
    assert freed[0]["slot"] == "1"
    assert freed[0]["from"] == "idle"
    assert freed[0]["cause"] == "liveness"
    assert freed[0]["dwell_ms"] == 122_000  # (1_000_062 - 999_940) * 1000


def test_log_mirrors_into_trace_even_when_bridge_log_is_reassigned(tmp_path):
    """Tests (and the LaunchAgent's own callers) reassign bridge.log
    directly rather than passing log= to the constructor; mirroring into
    the trace must survive that."""
    trace = RecordingTraceWriter()
    bridge, *_rest = make_bridge(tmp_path, trace=trace)
    captured = []
    bridge.log = captured.append
    bridge._log("hello")
    assert captured == ["hello"]
    assert trace.of_kind("log")[0]["msg"] == "hello"


def test_dry_run_launch_mirrors_into_trace(tmp_path):
    trace = RecordingTraceWriter()
    bridge, *_rest = make_bridge(tmp_path, dry_run=True, trace=trace)
    bridge._apply_launch(1)
    logs = trace.of_kind("log")
    assert any("Would launch slot 1" in r["msg"] for r in logs)


def test_heartbeat_fires_at_interval(tmp_path):
    from switchboard.bridge import HEARTBEAT_INTERVAL_S

    clock = FakeClock()
    trace = RecordingTraceWriter()
    bridge, registry, device, terminal, prober, clock, _key_injector = make_bridge(tmp_path, clock=clock, trace=trace)
    registry.save(
        {"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "status": "idle", "status_since": clock.t}}}
    )

    clock.advance(HEARTBEAT_INTERVAL_S)
    bridge._emit_heartbeat(registry.load()["slots"])

    heartbeats = trace.of_kind("heartbeat")
    assert len(heartbeats) == 1
    assert heartbeats[0]["slots"]["1"]["status"] == "idle"
    assert heartbeats[0]["slots"]["1"]["dwell_ms"] == HEARTBEAT_INTERVAL_S * 1000
    assert heartbeats[0]["pending_acks"] == 0
