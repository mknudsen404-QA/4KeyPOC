"""The on-disk slot registry: the source of truth for what's launched
where. Registry(path) wraps load/save/transaction as methods — Phase 0's
free functions, unchanged, just given a home. DEFAULT_REGISTRY resolution
stays in cli.py, not here.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from pathlib import Path

_REGISTRY_LOCK = threading.RLock()


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "slots": {}}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Registry is not an object: {self.path}")
        data.setdefault("version", 1)
        data.setdefault("slots", {})
        return data

    def save(self, registry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(registry, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def transaction(self):
        """Exclusive load->mutate->save. Holds the in-process lock and an
        fcntl.flock on <path>.lock so the `status`/`clear`/`launch` CLI
        (separate processes) can't interleave with the running bridge.
        """
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _REGISTRY_LOCK, open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                registry = self.load()
                yield registry
                self.save(registry)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
