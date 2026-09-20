"""Is the process behind a launched slot's terminal still running?"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from switchboard.model import Liveness
from switchboard.trace import NullTraceWriter

_SHELL_COMMS = {"login", "-zsh", "zsh", "-bash", "bash", "sh", "-sh", "fish", "-fish"}


class ProcessProber:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        exists: Callable[[str], bool] = os.path.exists,
        log: Callable[[str], None] | None = None,
        timeout: float = 3.0,
        trace=None,
    ) -> None:
        self._run = run
        self._exists = exists
        # None (the default in production, via Bridge) means "don't log" —
        # tests construct ProcessProber() bare constantly and shouldn't pay
        # for that. cli.py wires the real Bridge's log function through so
        # a slot flipping to UNKNOWN is diagnosable after the fact, instead
        # of a silent, unexplained amber pulse (confirmed live: a session
        # with several MCP server child processes went UNKNOWN for two
        # ticks right after finishing a turn, with no way to tell why).
        self._log = log
        self._timeout = timeout
        self._trace = trace if trace is not None else NullTraceWriter()

    def probe(self, record: dict) -> Liveness:
        """UNKNOWN covers both "we can't tell" (a ps failure/timeout) and
        "there's nothing to check" (a --no-open record has no terminal_tty
        at all) — in both cases the bridge can't safely conclude DEAD, so
        only hooks or an explicit `clear` can free the slot.

        Every DEAD or UNKNOWN result is also written to the trace with
        whatever evidence produced it (the `ps` comm list, or the reason
        a probe couldn't run) — this is the evidence a "slot froze/free
        when it shouldn't have" bug needs: whether the CLI process
        genuinely exited (only shell comms left) or the probe itself is
        unreliable (a timeout, a bad `ps` exit) and shouldn't be trusted.
        """
        slot = record.get("slot")
        tty_path = record.get("terminal_tty")
        if not tty_path:
            self._trace.write({"kind": "probe", "slot": slot, "tty": None, "result": "unknown", "reason": "no terminal_tty"})
            return Liveness.UNKNOWN
        if not self._exists(tty_path):
            self._trace.write({"kind": "probe", "slot": slot, "tty": tty_path, "result": "dead", "reason": "tty node gone"})
            return Liveness.DEAD  # tab closed: the pty node is gone
        try:
            result = self._run(
                ["ps", "-o", "comm=", "-t", os.path.basename(tty_path)],
                capture_output=True, text=True, timeout=self._timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            self._log_unknown(slot, tty_path, f"ps timed out after {self._timeout}s")
            self._trace.write(
                {"kind": "probe", "slot": slot, "tty": tty_path, "result": "unknown", "reason": f"ps timed out after {self._timeout}s"}
            )
            return Liveness.UNKNOWN
        except OSError as exc:
            self._log_unknown(slot, tty_path, f"ps raised {exc!r}")
            self._trace.write({"kind": "probe", "slot": slot, "tty": tty_path, "result": "unknown", "reason": f"ps raised {exc!r}"})
            return Liveness.UNKNOWN
        if result.returncode not in (0, 1):
            reason = f"ps exited {result.returncode} (stderr: {result.stderr.strip() or '<empty>'})"
            self._log_unknown(slot, tty_path, reason)
            self._trace.write({"kind": "probe", "slot": slot, "tty": tty_path, "result": "unknown", "reason": reason})
            return Liveness.UNKNOWN
        comms = {line.strip().rsplit("/", 1)[-1] for line in result.stdout.splitlines() if line.strip()}
        if comms - _SHELL_COMMS:
            return Liveness.ALIVE
        self._trace.write({"kind": "probe", "slot": slot, "tty": tty_path, "result": "dead", "comms": sorted(comms)})
        return Liveness.DEAD

    def _log_unknown(self, slot, tty_path: str, reason: str) -> None:
        if self._log is not None:
            self._log(f"slot {slot}: liveness probe on {tty_path} returned unknown — {reason}")


class FakeProber:
    def __init__(self, default: Liveness = Liveness.ALIVE) -> None:
        self.default = default
        self.answers: dict[str, Liveness] = {}
        self.calls: list[str] = []

    def probe(self, record: dict) -> Liveness:
        slot_key = str(record.get("slot"))
        self.calls.append(slot_key)
        return self.answers.get(slot_key, self.default)
