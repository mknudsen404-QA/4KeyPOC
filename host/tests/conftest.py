import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so `import switchboard_bridge` works


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="Rewrite golden *.out.jsonl files instead of comparing")


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "registry.json"


@pytest.fixture
def fake_clock():
    from switchboard.clock import FakeClock

    return FakeClock()
