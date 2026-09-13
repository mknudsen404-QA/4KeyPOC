"""Golden-trace regressions: each *.in.jsonl under tests/golden/ replays a
sequence of board/hook/liveness events through a real Bridge.step() (no
threads, no sleeps, no real clock) and compares the exact, ordered list of
device.send() calls to the matching *.out.jsonl.

Input line grammar (one JSON object per line):

    {"registry": {...}}                                    # optional first line: pre-seed the registry
    {"t": 0,  "board": {"event": "agent.select", "slot": 1}}
    {"t": 1,  "hook": {"slot": 1, "event": "SessionStart", "body": {"session_id": "s-aaa"}}}
    {"t": 44, "liveness": {"1": "unknown"}}                # a LivenessObserved tick, as the ticker would submit
    {"t": 50, "startup_sync": true}                         # calls bridge.startup_sync() instead of a step()

`t` is absolute seconds; the runner sets FakeClock to 1_000_000 + t before
each line. FakeProber's answers are updated from the most recent
`liveness` line, so an `agent.select` right after agrees with the trace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard.bridge import Bridge
from switchboard.clock import FakeClock
from switchboard.device import FakeDevice
from switchboard.events import BoardEvent, HookEvent, LivenessObserved
from switchboard.liveness import FakeProber
from switchboard.model import Liveness
from switchboard.registry import Registry
from switchboard.terminal import FakeTerminal

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def run_trace(lines: list[dict], registry_path: Path) -> list[dict]:
    registry = Registry(registry_path)
    device = FakeDevice()
    terminal = FakeTerminal()
    prober = FakeProber()
    clock = FakeClock()
    launch_config = {
        # family="claude" so lifecycle hooks map to a status (hook_status_for
        # only knows claude/codex); command="cat" so resolve_command succeeds
        # on any machine regardless of whether the real Claude Code CLI is
        # installed — the family label, not the actual binary, drives hooks.
        "agents": [
            {"slot": n, "name": f"Agent {n}", "family": "claude", "command": "cat", "cwd": "/tmp"}
            for n in range(1, 5)
        ]
    }
    bridge = Bridge(
        registry=registry, device=device, terminal=terminal, prober=prober, clock=clock,
        launch_config=launch_config, auto_launch=True, dry_run=False, no_open=False, log=lambda msg: None,
    )

    for entry in lines:
        if "registry" in entry:
            registry.save(entry["registry"])
            continue
        clock.t = 1_000_000 + entry["t"]
        if "board" in entry:
            board = dict(entry["board"])
            name = board.pop("event")
            bridge.step(BoardEvent(name, board))
        elif "hook" in entry:
            hook = entry["hook"]
            bridge.step(HookEvent(str(hook["slot"]), hook["event"], hook.get("body", {})))
        elif "liveness" in entry:
            results = {k: Liveness(v) for k, v in entry["liveness"].items()}
            prober.answers.update(results)
            bridge.step(LivenessObserved(results))
        elif entry.get("startup_sync"):
            bridge.startup_sync()
        else:
            raise ValueError(f"Unrecognized golden trace line: {entry}")

    return device.sent


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "registry.json"


@pytest.mark.parametrize("in_path", sorted(GOLDEN_DIR.glob("*.in.jsonl")), ids=lambda p: p.stem)
def test_golden_trace(in_path, registry_path, request):
    out_path = in_path.with_name(in_path.name.replace(".in.jsonl", ".out.jsonl"))
    lines = _read_jsonl(in_path)
    actual = run_trace(lines, registry_path)

    if request.config.getoption("--update-golden"):
        _write_jsonl(out_path, actual)
        pytest.skip(f"rewrote {out_path.name}")

    expected = _read_jsonl(out_path) if out_path.exists() else []
    assert actual == expected, (
        f"{in_path.name} produced different device output than {out_path.name}. "
        f"Run with --update-golden to review and update it."
    )
