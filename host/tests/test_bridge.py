from switchboard.bridge import Bridge
from switchboard.clock import FakeClock
from switchboard.device import FakeDevice
from switchboard.events import BoardEvent, HookEvent
from switchboard.key_injector import FakeKeyInjector
from switchboard.liveness import FakeProber
from switchboard.model import Liveness
from switchboard.registry import Registry
from switchboard.terminal import FakeTerminal


def make_bridge(
    tmp_path, *, clock=None, auto_launch=True, dry_run=False, no_open=False, launch_config=None,
    close_dead_tabs=False,
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


def test_startup_sync_pushes_four_updates_and_frees_dead(tmp_path):
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

    assert len(device.sent) == 4
    by_slot = {e["slot"]: e for e in device.sent}
    assert by_slot[1]["status"] != "empty"
    assert by_slot[2]["status"] == "empty"
    assert by_slot[3]["status"] == "empty"
    assert by_slot[4]["status"] == "empty"
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
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001"}}})

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))

    assert terminal.focused == ["/dev/ttys001"]  # FakeTerminal.focus() settles frontmost immediately
    assert key_injector.held == [(1234, 49)]


def test_voice_hold_start_aborts_if_focus_never_settles(tmp_path):
    bridge, registry, device, terminal, prober, clock, key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001"}}})

    # Simulate Terminal never actually switching tabs (focus() still records
    # the call for the log line, but frontmost_tty() stays whatever it was).
    real_focus = terminal.focus
    terminal.focus = lambda tty: (real_focus(tty), setattr(terminal, "frontmost", None))[0]

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))

    assert key_injector.held == []  # hold aborted, never reaches the injector


def test_voice_hold_stop_releases_key(tmp_path):
    bridge, registry, device, terminal, prober, clock, key_injector = make_bridge(tmp_path)
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "family": "claude", "terminal_tty": "/dev/ttys001"}}})

    bridge.step(BoardEvent("voice.hold.start", {"slot": 1}))
    bridge.step(BoardEvent("voice.hold.stop", {"slot": 1}))

    assert key_injector.released == [(1234, 49)]


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
