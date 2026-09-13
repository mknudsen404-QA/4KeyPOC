import subprocess

import switchboard_bridge as sb
from switchboard.liveness import ProcessProber
from switchboard.model import Liveness


def make_record(slot=1, tty="/dev/ttys001", **extra):
    record = {"slot": slot, "name": "Test", "family": "shell", "status": "idle", "terminal_tty": tty}
    record.update(extra)
    return record


def fake_result(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_alive_when_agent_process_on_tty():
    prober = ProcessProber(run=lambda *a, **k: fake_result("login\nclaude\n"), exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.ALIVE


def test_dead_when_only_shell_on_tty():
    prober = ProcessProber(run=lambda *a, **k: fake_result("login\n-zsh\n"), exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.DEAD


def test_dead_when_tty_missing():
    calls = []
    prober = ProcessProber(run=lambda *a, **k: calls.append(1) or fake_result(""), exists=lambda p: False)
    assert prober.probe(make_record()) is Liveness.DEAD
    assert not calls


def test_unknown_on_timeout():
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2.0)

    prober = ProcessProber(run=raise_timeout, exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.UNKNOWN


def test_unknown_on_no_tty_record():
    prober = ProcessProber()
    assert prober.probe({"slot": 1}) is Liveness.UNKNOWN


def test_free_requires_two_consecutive_dead(monkeypatch, registry_path):
    sb.save_registry({"version": 1, "slots": {"1": make_record()}}, registry_path)
    monkeypatch.setattr(sb.os.path, "exists", lambda p: True)
    monkeypatch.setattr(sb, "_run", lambda *a, **k: fake_result("login\n-zsh\n"))
    state = sb.BridgeState(registry_path=registry_path)

    freed = sb.reconcile_liveness(state)
    assert freed == []
    assert "1" in sb.load_registry(registry_path)["slots"]

    freed = sb.reconcile_liveness(state)
    assert freed == ["1"]
    assert "1" not in sb.load_registry(registry_path)["slots"]


def test_unknown_resets_dead_count(monkeypatch, registry_path):
    sb.save_registry({"version": 1, "slots": {"1": make_record()}}, registry_path)
    state = sb.BridgeState(registry_path=registry_path)
    monkeypatch.setattr(sb.os.path, "exists", lambda p: True)

    monkeypatch.setattr(sb, "_run", lambda *a, **k: fake_result("login\n-zsh\n"))  # DEAD
    assert sb.reconcile_liveness(state) == []

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2.0)

    monkeypatch.setattr(sb, "_run", raise_timeout)  # UNKNOWN, resets the count
    assert sb.reconcile_liveness(state) == []

    monkeypatch.setattr(sb, "_run", lambda *a, **k: fake_result("login\n-zsh\n"))  # DEAD again (count=1)
    assert sb.reconcile_liveness(state) == []


def test_select_never_launches_on_unknown(monkeypatch, registry_path, capture_device):
    record = {"slot": 1, "name": "Test", "family": "shell", "status": "launched"}  # no terminal_tty -> UNKNOWN
    sb.save_registry({"version": 1, "slots": {"1": record}}, registry_path)

    def raise_if_called(*a, **k):
        raise AssertionError("launch_slot_from_config should not be called on UNKNOWN liveness")

    monkeypatch.setattr(sb, "launch_slot_from_config", raise_if_called)
    state = sb.BridgeState(registry_path=registry_path, auto_launch=True)
    message = sb.describe_event({"event": "agent.select", "slot": 1}, state)
    assert "unknown" in message.lower()


def test_select_on_alive_focuses_and_pushes_update(monkeypatch, registry_path, capture_device):
    record = make_record(status="idle")
    sb.save_registry({"version": 1, "slots": {"1": record}}, registry_path)
    monkeypatch.setattr(sb.os.path, "exists", lambda p: True)
    monkeypatch.setattr(sb, "_run", lambda *a, **k: fake_result("login\nclaude\n"))
    focused = []
    monkeypatch.setattr(sb, "focus_terminal_tab", lambda tty: focused.append(tty) or True)
    state = sb.BridgeState(registry_path=registry_path)

    message = sb.describe_event({"event": "agent.select", "slot": 1}, state)

    assert focused == ["/dev/ttys001"]
    assert "Selected" in message
    assert len(capture_device) == 1
    assert capture_device[0]["slot"] == 1


def test_startup_reconcile_frees_dead_and_syncs_all_four(monkeypatch, registry_path, capture_device):
    slots = {
        "1": make_record(slot=1, tty="/dev/ttys001"),
        "2": make_record(slot=2, tty="/dev/ttys002"),
    }
    sb.save_registry({"version": 1, "slots": slots}, registry_path)

    real_exists = sb.os.path.exists

    def fake_exists(p):
        # Only fake out the two tty paths under test — os.path.exists is the
        # real stdlib function (shared, not a per-module copy), and pathlib's
        # Path.exists() calls through it too, so anything else must fall
        # through to the real check or load_registry starts seeing its own
        # registry file as missing.
        if p in ("/dev/ttys001", "/dev/ttys002"):
            return p == "/dev/ttys001"
        return real_exists(p)

    monkeypatch.setattr(sb.os.path, "exists", fake_exists)
    monkeypatch.setattr(sb, "_run", lambda *a, **k: fake_result("login\nclaude\n"))

    state = sb.BridgeState(registry_path=registry_path)
    sb.reconcile_liveness(state, require_confirmation=False)
    registry = sb.load_registry(registry_path)
    for slot in range(1, 5):
        sb.write_slot_update(state.device_fd, registry, slot)

    assert len(capture_device) == 4
    by_slot = {event["slot"]: event for event in capture_device}
    assert by_slot[2]["status"] == "empty"
    assert by_slot[1]["status"] != "empty"
    assert by_slot[3]["status"] == "empty"
    assert by_slot[4]["status"] == "empty"


def test_ticker_survives_exception(monkeypatch, registry_path):
    calls = []
    real_load_registry = sb.load_registry
    state = sb.BridgeState(registry_path=registry_path)

    def flaky_load_registry(path=sb.DEFAULT_REGISTRY):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real_load_registry(path)

    monkeypatch.setattr(sb, "load_registry", flaky_load_registry)

    import threading

    stop_event = threading.Event()

    def run_two_ticks():
        sb._liveness_ticker(state, stop_event, 0.01)

    thread = threading.Thread(target=run_two_ticks)
    thread.start()
    import time

    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=1)

    assert len(calls) >= 2
