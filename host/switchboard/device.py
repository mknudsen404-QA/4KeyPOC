"""DeviceLink implementations: turning bytes on a wire into JSON lines and
back. Each has send(event: dict) -> None, lines() -> Iterator[str], and
close() -> None. Real ones (SerialDevice) live beside test/dry-run ones
(FdDevice, FileDevice, FakeDevice) so --stdin/--sample/--dry-run and the
test suite all use the same interface as the real bridge.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import queue
import select
import termios
import threading
import time
import tty
from typing import Iterator, Protocol, TextIO


class DeviceLink(Protocol):
    def send(self, event: dict) -> None: ...

    def lines(self) -> Iterator[str]: ...

    def close(self) -> None: ...


def find_default_port() -> str | None:
    candidates: list[str] = []
    for pattern in (
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.SLAB_USBtoUART*",
    ):
        candidates.extend(glob.glob(pattern))
    return sorted(candidates)[0] if candidates else None


BAUD_RATES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}
for _rate_name, _rate_value in (
    (230400, "B230400"),
    (460800, "B460800"),
    (921600, "B921600"),
):
    if hasattr(termios, _rate_value):
        BAUD_RATES[_rate_name] = getattr(termios, _rate_value)


def _configure_serial(fd: int, baud: int) -> None:
    if baud not in BAUD_RATES:
        supported = ", ".join(str(rate) for rate in sorted(BAUD_RATES))
        raise ValueError(f"Unsupported baud rate {baud}. Supported: {supported}")
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = BAUD_RATES[baud]
    attrs[5] = BAUD_RATES[baud]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    tty.setraw(fd)


# Guards os.write(fd, ...) below. Without it, concurrent writers to the one
# shared serial fd — the main listener loop, the liveness ticker background
# thread, and each ThreadingHTTPServer hook-request thread (two CLIs can
# fire lifecycle hooks within milliseconds of each other) — can interleave
# their bytes mid-write. The firmware reads line-by-line, so an interleaved
# write becomes one malformed JSON line that deserializeJson silently
# drops, leaving that key's LED stuck showing whatever color it had before
# (e.g. the initial "launched" blue never advancing to "idle" white) until
# the next status change happens to get through cleanly.
_DEVICE_WRITE_LOCK = threading.Lock()


def _send_line(fd: int, event: dict) -> None:
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with _DEVICE_WRITE_LOCK:
        os.write(fd, line.encode("utf-8"))


class SerialDevice:
    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.baud = baud
        self.fd = self._open()

    def _open(self) -> int:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # Exclusive access: without this, a second process (a stray
            # `cat`, another bridge instance, a leftover diagnostic
            # session) can open the same tty at the same time with no
            # error from either side — the OS just splits incoming bytes
            # unpredictably between readers. That happened for real: a
            # diagnostic `cat` outlived its intended lifetime and silently
            # starved the bridge of every board event for a good chunk of
            # a debugging session before anyone noticed. Fail loudly instead.
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"{self.port} is already open by another process (found while claiming "
                f"exclusive access). Close whatever else has it — `lsof {self.port}` will "
                "show you what — before starting the bridge."
            ) from exc
        _configure_serial(fd, self.baud)
        return fd

    def send(self, event: dict) -> None:
        _send_line(self.fd, event)

    def lines(self, duration: float | None = None) -> Iterator[str]:
        buffer = b""
        started = time.monotonic()
        while True:
            if duration is not None and (time.monotonic() - started) >= duration:
                return
            ready, _, _ = select.select([self.fd], [], [], 0.25)
            if not ready:
                continue
            chunk = os.read(self.fd, 1024)
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class FdDevice:
    """Used by the pty integration test and by --stdin (write_fd=None ->
    send() is a no-op)."""

    def __init__(self, read_fd: int, write_fd: int | None = None) -> None:
        self.read_fd = read_fd
        self.write_fd = write_fd

    def send(self, event: dict) -> None:
        if self.write_fd is None:
            return
        _send_line(self.write_fd, event)

    def lines(self) -> Iterator[str]:
        buffer = b""
        while True:
            ready, _, _ = select.select([self.read_fd], [], [], 0.25)
            if not ready:
                continue
            try:
                chunk = os.read(self.read_fd, 1024)
            except OSError:
                return
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        pass


class FileDevice:
    """--sample: read events from a file; send() is a no-op."""

    def __init__(self, handle: TextIO) -> None:
        self.handle = handle

    def send(self, event: dict) -> None:
        pass

    def lines(self) -> Iterator[str]:
        for line in self.handle:
            line = line.strip()
            if line:
                yield line

    def close(self) -> None:
        self.handle.close()


class FakeDevice:
    """Records everything sent; lines() yields whatever is fed to it and
    blocks (like a real device would) until close()."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._queue: "queue.Queue[str | None]" = queue.Queue()

    def send(self, event: dict) -> None:
        self.sent.append(event)

    def feed(self, line: str) -> None:
        self._queue.put(line)

    def lines(self) -> Iterator[str]:
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item

    def close(self) -> None:
        self._queue.put(None)
