"""Race regressions that need real threads and a real pty/hook server — the
things test_bridge.py's no-thread step()-only tests can't exercise.
"""

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

from switchboard.bridge import Bridge
from switchboard.events import Shutdown
from switchboard.liveness import FakeProber
from switchboard.model import Liveness
from switchboard.registry import Registry
from switchboard.device import FdDevice
from switchboard.terminal import FakeTerminal


@pytest.fixture(scope="session", autouse=True)
def _warm_up_pty_thread_socket():
    """Absorb a one-time startup cost before any real test's timing budget
    does. Observed only on GitHub Actions' macOS runner (never locally):
    whichever tests happen to be the first few in the session to use
    `bridge_harness` fail with zero device output even though the exact
    same operations, on the same fixture, succeed reliably for every later
    test in the same run — confirmed by moving which tests run first and
    watching the failures move with them. A generic pty+thread+bound-socket
    warm-up did NOT fix it (tried first); this one specifically also
    constructs and tears down a real http.server.ThreadingHTTPServer, since
    that (not a bare socket) is the one thing every `bridge_harness` use
    does that a plain TCP bind doesn't — a real Bridge.run() also
    constructs one via start_hook_server() before its worker loop starts
    dequeuing anything, so if *that* has a one-time slow path (some
    network/security check specific to a listening HTTP server on this
    sandbox), it would stall every submitted event until it clears.
    """
    from switchboard.hooks_server import start_hook_server

    master, slave = os.openpty()
    tty.setraw(slave)
    warmup_thread = threading.Thread(target=lambda: None)
    warmup_thread.start()
    warmup_thread.join()
    server = start_hook_server(lambda event: None, "127.0.0.1", 0)
    time.sleep(1.0)
    if server is not None:
        server.shutdown()
        server.server_close()
    os.close(master)
    os.close(slave)
    yield


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _auto_ack(master_fd, parsed_line):
    """Simulate a real board: ack every agent.update immediately. Without
    this, the bridge's pending-ack retry logic (Phase 2.3b) sees every
    update from these tests as never-acked, and a clock.advance() in a
    later test step makes it look overdue and retries it — a spurious
    extra device line these tests don't expect."""
    if parsed_line.get("event") == "agent.update":
        ack = json.dumps({"event": "agent.update.ack", "slot": parsed_line.get("slot")}).encode() + b"\n"
        os.write(master_fd, ack)


def expect_lines(fd, count, timeout=6.0):
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
                parsed = json.loads(line)
                lines.append(parsed)
                _auto_ack(fd, parsed)
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
                parsed = json.loads(line)
                lines.append(parsed)
                _auto_ack(fd, parsed)
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
def bridge_harness(tmp_path, fake_clock):
    """Spins up a real Bridge.run() against a pty pair (as the DeviceLink)
    plus a real hooks_server HTTP server on an ephemeral port. Terminal and
    liveness are fakes (no real Terminal.app/ps involved)."""
    master, slave = os.openpty()
    # Raw mode on the slave side: a pty's default line discipline echoes
    # anything written into master straight back out to master (as a real
    # terminal would echo typed input), which would otherwise show up as a
    # bogus extra "device line" on every press(). Real serial ports are
    # configured the same way in device.py's _configure_serial().
    tty.setraw(slave)
    port = free_port()

    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)
    device = FdDevice(read_fd=slave, write_fd=slave)
    terminal = FakeTerminal()
    prober = FakeProber()
    launch_config = {
        # family="claude" so lifecycle hooks map to a status; command="cat"
        # so resolve_command succeeds on any machine (CI included)
        # regardless of whether the real Claude Code CLI is installed.
        "agents": [
            {"slot": n, "name": f"Agent {n}", "family": "claude", "command": "cat", "cwd": "/tmp"}
            for n in range(1, 4)
        ]
    }

    bridge = Bridge(
        registry=registry,
        device=device,
        terminal=terminal,
        prober=prober,
        clock=fake_clock,
        launch_config=launch_config,
        auto_launch=True,
        dry_run=False,
        no_open=False,
    )
    bridge.startup_sync()  # the initial 3 "empty" lines

    thread = threading.Thread(
        target=bridge.run, kwargs=dict(liveness_interval=0.05, hook_port=port), daemon=True
    )
    thread.start()
    # Let the reader/ticker threads and the hook HTTP server actually start
    # running before the test presses keys or POSTs hooks. On a cold CI
    # runner (observed on GitHub Actions, never locally) the first couple
    # of these fixtures in a pytest session can take noticeably longer
    # than usual to get their background threads scheduled; without this,
    # the very first `press()` in the test can land before anything is
    # listening and its "launched" line never arrives.
    time.sleep(0.5)

    yield {
        "master": master,
        "port": port,
        "prober": prober,
        "terminal": terminal,
        "registry": registry,
        "registry_path": registry_path,
        "clock": fake_clock,
        "bridge": bridge,
    }

    bridge.submit(Shutdown())
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

    startup = expect_lines(master, 3)
    assert [line["status"] for line in startup] == ["empty", "empty", "empty"]

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
    terminal = bridge_harness["terminal"]

    expect_lines(master, 3)  # startup sync
    press(master, 1)
    expect_lines(master, 1)  # launched
    assert len(terminal.opened) == 1

    bridge_harness["prober"].answers["1"] = Liveness.UNKNOWN
    press(master, 1)
    drain_lines(master, timeout=0.4)
    assert len(terminal.opened) == 1


def test_dead_session_freed_after_two_ticks_and_led_cleared(bridge_harness):
    master = bridge_harness["master"]
    registry = bridge_harness["registry"]

    expect_lines(master, 3)  # startup sync

    with registry.transaction() as reg:
        from switchboard.model import slot_record

        reg.setdefault("slots", {})["1"] = slot_record(
            slot=1, name="Agent 1", family="claude", cwd="/tmp", command="claude",
            terminal_title="Switchboard A1", terminal_tty="/dev/ttysFAKE", now=1_000_000,
        )

    bridge_harness["prober"].answers["1"] = Liveness.DEAD
    time.sleep(0.3)  # several 0.05s ticks: first DEAD, second confirms + frees

    lines = drain_lines(master, timeout=0.3)
    empties = [line for line in lines if line.get("slot") == 1 and line["status"] == "empty"]
    assert len(empties) == 1
    assert "1" not in registry.load().get("slots", {})


def test_foreign_session_hooks_ignored(bridge_harness):
    master = bridge_harness["master"]
    port = bridge_harness["port"]

    expect_lines(master, 3)  # startup sync
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
    registry = bridge_harness["registry"]

    expect_lines(master, 3)  # startup sync
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
    parsed = registry.load()  # must parse without raising
    assert "1" not in parsed.get("slots", {})


def test_hook_flood_never_resurrects_slot(bridge_harness):
    """8 threads x 50 PostToolUse POSTs racing a SessionEnd from a 9th
    thread; after everything settles, the slot must stay absent and no
    agent.update for it may appear after the final "empty" line — the
    single-worker queue makes this a strict ordering guarantee."""
    master = bridge_harness["master"]
    port = bridge_harness["port"]
    registry = bridge_harness["registry"]

    expect_lines(master, 3)  # startup sync
    press(master, 1)
    expect_lines(master, 1)  # launched
    hook(port, "SessionStart", 1, session_id="s-flood")

    errors = []

    def hammer():
        try:
            for _ in range(50):
                hook(port, "PostToolUse", 1, session_id="s-flood", tool_name="Bash")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def end_session():
        time.sleep(0.02)
        try:
            hook(port, "SessionEnd", 1, session_id="s-flood", reason="exit")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    threads.append(threading.Thread(target=end_session))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    lines = drain_lines(master, timeout=0.5)
    assert "1" not in registry.load().get("slots", {})

    empty_indices = [i for i, line in enumerate(lines) if line.get("slot") == 1 and line["status"] == "empty"]
    assert empty_indices, "expected at least one 'empty' line for slot 1"
    last_empty = empty_indices[-1]
    assert all(
        not (line.get("slot") == 1 and line["status"] != "empty") for line in lines[last_empty + 1 :]
    ), f"a non-empty update for slot 1 appeared after its 'empty' line: {lines[last_empty:]}"
