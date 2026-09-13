import json
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so `import switchboard_bridge` works
import switchboard_bridge as sb

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "hooks"


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "registry.json"


@pytest.fixture
def fake_clock(monkeypatch):
    class FakeClock:
        def __init__(self):
            self.t = 1_000_000.0

        def now(self):
            return self.t

        def advance(self, s):
            self.t += s

    clock = FakeClock()
    monkeypatch.setattr(sb, "_clock", clock)
    return clock


@pytest.fixture
def fake_device():
    """Collects every line the bridge would write to the board."""

    class FakeDevice:
        def __init__(self):
            self.lines: list[dict] = []

    dev = FakeDevice()
    return dev


@pytest.fixture
def capture_device(monkeypatch, fake_device):
    monkeypatch.setattr(sb, "write_device_event", lambda fd, ev: fake_device.lines.append(ev))
    return fake_device.lines


def hook_body(name: str, **overrides) -> bytes:
    data = json.loads((FIXTURES_DIR / f"{name}.json").read_text())
    data.update(overrides)
    return json.dumps(data).encode("utf-8")
