import sys
import threading
import time

from switchboard.registry import Registry
from switchboard.cli import HOST_DIR  # only for the cross-process shim check


def test_save_is_atomic_under_concurrent_reads(registry_path):
    registry = Registry(registry_path)
    registry.save({"version": 1, "slots": {}, "counter": 0})

    errors = []

    def writer():
        try:
            for _ in range(200):
                with registry.transaction() as reg:
                    reg["counter"] = reg.get("counter", 0) + 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                registry.load()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)] + [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert registry.load()["counter"] == 1600


def test_transaction_serializes_across_processes(registry_path):
    registry = Registry(registry_path)
    registry.save({"version": 1, "slots": {}})
    bridge_path = str((HOST_DIR / "switchboard_bridge.py"))

    result_holder = {}

    def hold_transaction():
        with registry.transaction() as reg:
            reg["held"] = True
            time.sleep(0.5)

    holder_thread = threading.Thread(target=hold_transaction)
    holder_thread.start()
    time.sleep(0.1)  # let the holder actually acquire the lock first

    import subprocess

    start = time.monotonic()
    result_holder["proc"] = subprocess.run(
        [sys.executable, bridge_path, "--registry", str(registry_path), "status", "--slot", "1", "--status", "done"],
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - start
    holder_thread.join()

    assert elapsed >= 0.4


def test_no_tmp_files_left_behind(registry_path):
    registry = Registry(registry_path)
    for _ in range(10):
        with registry.transaction() as reg:
            reg["x"] = reg.get("x", 0) + 1
    leftovers = list(registry_path.parent.glob(f"{registry_path.name}.*.tmp"))
    assert leftovers == []


def test_load_missing_file_returns_empty_registry(registry_path):
    registry = Registry(registry_path)
    assert registry.load() == {"version": 1, "slots": {}}


def test_load_rejects_non_object_json(registry_path, tmp_path):
    registry_path.write_text("[1, 2, 3]")
    registry = Registry(registry_path)
    try:
        registry.load()
        assert False, "expected ValueError"
    except ValueError:
        pass
