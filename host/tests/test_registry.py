import sys
import threading
import time

import switchboard_bridge as sb


def test_save_is_atomic_under_concurrent_reads(registry_path):
    sb.save_registry({"version": 1, "slots": {}, "counter": 0}, registry_path)

    errors = []

    def writer():
        try:
            for _ in range(200):
                with sb.registry_transaction(registry_path) as registry:
                    registry["counter"] = registry.get("counter", 0) + 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                sb.load_registry(registry_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)] + [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sb.load_registry(registry_path)["counter"] == 1600


def test_transaction_serializes_across_processes(registry_path):
    sb.save_registry({"version": 1, "slots": {}}, registry_path)
    bridge_path = str((sb.HOST_DIR / "switchboard_bridge.py"))

    result_holder = {}

    def hold_transaction():
        with sb.registry_transaction(registry_path) as registry:
            registry["held"] = True
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
    for _ in range(10):
        with sb.registry_transaction(registry_path) as registry:
            registry["x"] = registry.get("x", 0) + 1
    leftovers = list(registry_path.parent.glob(f"{registry_path.name}.*.tmp"))
    assert leftovers == []
