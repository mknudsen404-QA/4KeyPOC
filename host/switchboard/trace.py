"""A structured, redacted, size-bounded trace of every event the bridge
processes — one JSON object per line, written to trace.jsonl alongside
bridge.out.log. See docs/design/diagnostic-trace-and-support-bundle-spec.md
for the full record schema and the reasoning behind it.

This is deliberately a second file rather than a replacement for
bridge.out.log: that file stays human-readable prose for a person tailing
it live, while this one is meant for `jq` and for us.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path
from typing import Callable

DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_BACKUP_COUNT = 2

# The only fields ever pulled out of a raw hook payload for the default
# (non-verbose) trace. Anything not listed here is dropped. See
# redact_hook_payload's docstring for the reasoning per field.
_SESSION_KEY = "session_id"
_PASSTHROUGH_KEYS = {
    "notification_type": "nt",
    "tool_name": "tool",
    "agent_id": "agent",
}


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:8]


def redact_hook_payload(payload: dict) -> dict:
    """The subset of a hook payload safe to persist by default: enough to
    correlate events across a session and identify which tool/notification
    fired, never prompt text, tool inputs/outputs, or filesystem paths.

    This is the only function on the trace path that reads a hook
    payload's contents. Anything new that needs to reach the trace by
    default must be added here (and to the schema table in the spec) —
    it must not be threaded through some other path that bypasses this.
    """
    out: dict = {}
    session_id = payload.get(_SESSION_KEY)
    if session_id:
        out["session"] = _session_hash(str(session_id))
    for src, dst in _PASSTHROUGH_KEYS.items():
        value = payload.get(src)
        if value is not None:
            out[dst] = value
    return out


class TraceWriter:
    """Appends one JSON line per record to `path`, rotating at
    `max_bytes`. Never raises: a write or rotation failure logs once
    (via `on_error`, if given) and the writer goes silently inert for the
    rest of the process rather than taking the bridge down with it.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        verbose: bool = False,
        clock=None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.verbose = verbose
        self._clock = clock
        self._on_error = on_error
        self._dead = False
        self._logger: logging.Logger | None = None
        self._handler: logging.handlers.RotatingFileHandler | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            # A private, non-propagating logger per writer instance (named
            # by object id) so multiple TraceWriters (tests) never share
            # or leak handlers via the global logging module state.
            logger = logging.Logger(f"switchboard.trace.{id(self)}", level=logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            self._logger = logger
            self._handler = handler
        except OSError as exc:
            self._dead = True
            self._report(f"Could not open trace file {path}: {exc}")

    def _report(self, message: str) -> None:
        if self._on_error is not None:
            self._on_error(message)

    def _now(self) -> tuple[str, float]:
        if self._clock is not None:
            mono = self._clock.monotonic()
            epoch = self._clock.now()
        else:
            mono = time.monotonic()
            epoch = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
        ms = int((epoch - int(epoch)) * 1000)
        return f"{ts}.{ms:03d}Z", round(mono, 3)

    def write(self, record: dict) -> None:
        if self._dead or self._logger is None:
            return
        line = dict(record)
        if "ts" not in line or "mono" not in line:
            ts, mono = self._now()
            line.setdefault("ts", ts)
            line.setdefault("mono", mono)
        try:
            self._logger.info(json.dumps(line, separators=(",", ":"), default=str))
        except OSError as exc:
            self._dead = True
            self._report(f"Trace write failed, disabling trace for this run: {exc}")

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()


class NullTraceWriter:
    """Same interface as TraceWriter, does nothing. The default for every
    existing caller/test so adding tracing never changes behavior for
    code that doesn't opt in."""

    verbose = False

    def write(self, record: dict) -> None:  # noqa: ARG002 - interface parity
        pass

    def close(self) -> None:
        pass


def _log_dir() -> Path:
    override = os.environ.get("SWITCHBOARD_LOG_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Logs" / "Switchboard"


def open_default(*, verbose: bool = False, on_error: Callable[[str], None] | None = None, clock=None) -> TraceWriter:
    """~/Library/Logs/Switchboard/trace.jsonl, honoring SWITCHBOARD_LOG_DIR
    (tests, and anyone running the bridge with a non-default log home)."""
    return TraceWriter(_log_dir() / "trace.jsonl", verbose=verbose, on_error=on_error, clock=clock)
