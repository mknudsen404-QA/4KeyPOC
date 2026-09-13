import json
import os
import select
import socket
import threading
import time
import tty
import urllib.request
from urllib.request import Request

import pytest

import switchboard_bridge as sb


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def expect_lines(fd, count, timeout=3.0):
    """Block until `count` JSON lines have been read from fd, or fail."""
    buffer = b""
    lines = []
    deadline = time.monotonic() + timeout
    while len(lines) < count and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                lines.append(json.loads(line))
    assert len(lines) >= count, f"expected {count} lines within {timeout}s, got {len(lines)}: {lines}"
    return lines[:count]


def drain_lines(fd, timeout=0.4):
    """Collect whatever JSON lines arrive within timeout (no count required)."""
    buffer = b""
    lines = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                lines.append(json.loads(line))
    return lines


def expect_no_line(fd, timeout=0.4):
    lines = drain_lines(fd, timeout)
    assert lines == [], f"expected no device lines, got: {lines}"


def press(master_fd, slot):
    os.write(master_fd, json.dumps({"event": "agent.select", "slot": slot}).encode() + b"\n")


def hook(port, event, slot, **body):
    req = Request(
        f"http://127.0.0.1:{port}/switchboard-hook/{event}",
        data=json.dumps(body).encode(),
        headers={"X-Switchboard-Slot": str(slot), "Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=2).read()


@pytest.fixture
def bridge_harness(monkeypatch, registry_path, fake_clock):
    """Spins up a real run_bridge_listener against a pty pair + a real hook
    HTTP server on an ephemeral port. launch_slot_from_config is faked to
    register a slot directly (no real Terminal/AppleScript); probe_liveness
    is a controllable stub; focus_terminal_tab is a no-op.
    """
    master, slave = os.openpty()
    # Raw mode on the slave side: a pty's default line discipline echoes
    # anything written into master straight back out to master (as a real
    # terminal would echo typed input), which would otherwise show up as a
    # bogus extra "device line" on every press(). Real serial ports are
    # configured the same way in configure_serial().
    tty.setraw(slave)
    port = free_port()
    monkeypatch.setattr(sb, "HOOK_PORT", port)
    monkeypatch.setattr(sb, "focus_terminal_tab", lambda tty: True)

    launch_calls = []

    def fake_launch_slot_from_config(*, slot, config, registry_path, dry_run, no_open):
        launch_calls.append(slot)
        with sb.registry_transaction(registry_path) as registry:
            registry.setdefault("slots", {})[str(slot)] = sb.slot_record(
                slot=slot,
                name=f"Agent {slot}",
                family="claude",
                cwd="/tmp",
                command="claude",
                terminal_title=f"Switchboard A{slot}",
            )
            registry["slots"][str(slot)]["terminal_tty"] = "/dev/ttysFAKE"
        return f"Agent {slot} was empty, bridge launched it"

    monkeypatch.setattr(sb, "launch_slot_from_config", fake_launch_slot_from_config)

    probe_state = {"value": sb.Liveness.ALIVE}
    monkeypatch.setattr(sb, "probe_liveness", lambda record: probe_state["value"])

    thread = threading.Thread(
        target=sb.run_bridge_listener,
        kwargs=dict(
            lines=sb.serial_lines(slave, duration=5),
            registry_path=registry_path,
            auto_launch=True,
            device_fd=slave,
            liveness_interval=0.05,
        ),
        daemon=True,
    )
    thread.start()

    yield {
        "master": master,
        "slave": slave,
        "port": port,
        "probe_state": probe_state,
        "launch_calls": launch_calls,
        "registry_path": registry_path,
        "clock": fake_clock,
    }

    thread.join(timeout=6)
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def test_golden_launch_turn_stop(bridge_harness):
    master = bridge_harness["master"]
    port = bridge_harness["port"]
    clock = bridge_harness["clock"]

    startup = expect_lines(master, 4)
    assert [line["status"] for line in startup] == ["empty", "empty", "empty", "empty"]

    press(master, 1)
    launched = expect_lines(master, 1)[0]
    assert launched["slot"] == 1
    assert launched["status"] == "launched"

    hook(port, "SessionStart", 1, session_id="s-test")
    idle = expect_lines(master, 1)[0]
    assert idle["status"] == "idle"

    hook(port, "UserPromptSubmit", 1, session_id="s-test", prompt="hi")
    working = expect_lines(master, 1)[0]
    assert working["status"] == "working"
    assert working["busy_elapsed_ms"] == 0

    clock.advance(40)
    hook(port, "PostToolUse", 1, session_id="s-test", tool_name="Bash")
    expect_no_line(master)

    hook(port, "Stop", 1, session_id="s-test")
    done = expect_lines(master, 1)[0]
    assert done["status"] == "done"

    hook(port, "SessionEnd", 1, session_id="s-test", reason="exit")
    empty = expect_lines(master, 1)[0]
    assert empty["status"] == "empty"


def test_no_duplicate_launch_when_liveness_unknown(bridge_harness):
    master = bridge_harness["master"]

    expect_lines(master, 4)  # startup sync
    press(master, 1)
    expect_lines(master, 1)  # launched
    assert bridge_harness["launch_calls"] == [1]

    bridge_harness["probe_state"]["value"] = sb.Liveness.UNKNOWN
    press(master, 1)
    lines = drain_lines(master, timeout=0.4)
    assert bridge_harness["launch_calls"] == [1]


def test_dead_session_freed_after_two_ticks_and_led_cleared(bridge_harness):
    master = bridge_harness["master"]
    registry_path = bridge_harness["registry_path"]

    expect_lines(master, 4)  # startup sync

    with sb.registry_transaction(registry_path) as registry:
        registry.setdefault("slots", {})["1"] = sb.slot_record(
            slot=1, name="Agent 1", family="claude", cwd="/tmp", command="claude",
            terminal_title="Switchboard A1",
        )
        registry["slots"]["1"]["terminal_tty"] = "/dev/ttysFAKE"

    bridge_harness["probe_state"]["value"] = sb.Liveness.DEAD
    time.sleep(0.3)  # several 0.05s ticks: first DEAD, second confirms + frees

    lines = drain_lines(master, timeout=0.3)
    empties = [line for line in lines if line.get("slot") == 1 and line["status"] == "empty"]
    assert len(empties) == 1
    assert "1" not in sb.load_registry(registry_path).get("slots", {})


def test_foreign_session_hooks_ignored(bridge_harness):
    master = bridge_harness["master"]
    port = bridge_harness["port"]

    expect_lines(master, 4)  # startup sync
    press(master, 1)
    expect_lines(master, 1)  # launched

    hook(port, "SessionStart", 1, session_id="s-aaa")
    idle = expect_lines(master, 1)[0]
    assert idle["status"] == "idle"

    hook(port, "UserPromptSubmit", 1, session_id="s-bbb", prompt="hi")
    expect_no_line(master)


def test_registry_race_no_resurrection(bridge_harness):
    master = bridge_harness["master"]
    port = bridge_harness["port"]
    registry_path = bridge_harness["registry_path"]

    expect_lines(master, 4)  # startup sync
    press(master, 1)
    expect_lines(master, 1)  # launched

    errors = []

    def hammer():
        try:
            for _ in range(50):
                hook(port, "PostToolUse", 1, tool_name="Bash")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def end_session():
        time.sleep(0.05)
        try:
            hook(port, "SessionEnd", 1, reason="exit")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    threads.append(threading.Thread(target=end_session))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    time.sleep(0.2)  # let any trailing hook POST finish applying
    registry = sb.load_registry(registry_path)  # must parse without raising
    assert "1" not in registry.get("slots", {})
