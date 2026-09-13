"""The Bridge: one queue, one owner. `step()` is the only code that touches
the registry inside the process — everything else (the serial reader
thread, the liveness ticker, the hook server) only ever calls `submit()`.
"""

from __future__ import annotations

import copy
import queue
import threading
import traceback
from typing import Callable

from switchboard import model
from switchboard.device import DeviceLink
from switchboard.events import BoardEvent, Event, LivenessObserved, Shutdown, SlotRegistered, parse_board_line
from switchboard.hooks_server import HOOK_HOST, HOOK_PORT, start_hook_server
from switchboard.launcher import build_launch
from switchboard.liveness import ProcessProber
from switchboard.reducer import Effect, Focus, Launch, Log, ReducerState, SendUpdate, VoiceKey, reduce
from switchboard.registry import Registry
from switchboard.terminal import TerminalDriver

# macOS virtual keycode for the spacebar (used to drive Claude Code's /voice
# hold-to-record mode).
SPACE_KEYCODE = 49

# BoardEvent names for which the Bridge probes liveness itself and injects
# the result into the payload before handing the event to the (pure,
# probe-free) reducer.
_PROBED_EVENTS = ("agent.select", "voice.hold.start", "voice.hold.stop")


class Bridge:
    def __init__(
        self,
        *,
        registry: Registry,
        device: DeviceLink,
        terminal: TerminalDriver,
        prober: ProcessProber,
        clock,
        launch_config: dict | None,
        auto_launch: bool,
        dry_run: bool,
        no_open: bool,
        log: Callable[[str], None] = print,
    ) -> None:
        self.registry = registry
        self.device = device
        self.terminal = terminal
        self.prober = prober
        self.clock = clock
        self.launch_config = launch_config
        self.auto_launch = auto_launch
        self.dry_run = dry_run
        self.no_open = no_open
        self.log = log
        self.state = ReducerState()
        self._queue: "queue.Queue[Event]" = queue.Queue()
        # Set by run() when the serial reader thread's device.lines() raises
        # (a real disconnect) rather than run() simply hitting `duration` —
        # callers that retry on disconnect (cli.py's listen --retry) check
        # this after run() returns, since run() itself always returns 0.
        self.last_shutdown_error: Exception | None = None

    def submit(self, event: Event) -> None:
        """Thread-safe; callable from any thread."""
        self._queue.put(event)

    def step(self, event: Event) -> None:
        """Process ONE event on the calling thread. The only place that
        touches the registry inside this process."""
        event = self._resolve_probes(event)
        with self.registry.transaction() as reg:
            slots = reg.setdefault("slots", {})
            new_slots, effects = reduce(
                slots, self.state, event, int(self.clock.now()), auto_launch=self.auto_launch
            )
            reg["slots"] = new_slots
            snapshot = copy.deepcopy(new_slots)  # for effects, after the lock is released
        for effect in effects:
            self._apply(effect, snapshot)

    def _resolve_probes(self, event: Event) -> Event:
        if not isinstance(event, BoardEvent) or event.name not in _PROBED_EVENTS:
            return event
        slot = event.payload.get("slot")
        if slot is None:
            return event
        with self.registry.transaction() as reg:
            record = reg.get("slots", {}).get(str(slot))
        if record is None:
            return event  # nothing to probe; reducer treats this as "no record"
        liveness = self.prober.probe(record)
        return BoardEvent(event.name, {**event.payload, "liveness": liveness.value})

    def startup_sync(self) -> None:
        """Reconcile (a single DEAD reading frees, since nothing has been
        running yet) then push every slot 1..4's status to the board — a
        bridge restart always leaves the LEDs in a clean, correct state
        instead of whatever they last showed.

        Applies the freeing via `reduce` directly (inside one transaction)
        rather than through `step`, discarding its SendUpdate effects: the
        unconditional 1..4 loop below already covers every freed slot, so
        going through step()/_apply() too would send two updates for it.
        """
        with self.registry.transaction() as reg:
            slots = reg.setdefault("slots", {})
            results = {slot_key: self.prober.probe(record) for slot_key, record in slots.items()}
            if results:
                new_slots, _effects = reduce(
                    slots, self.state, LivenessObserved(results, confirm=False),
                    int(self.clock.now()), auto_launch=self.auto_launch,
                )
                reg["slots"] = new_slots
            snapshot = copy.deepcopy(reg.get("slots", {}))
        for slot in range(1, 5):
            self._apply(SendUpdate(str(slot)), snapshot)

    def _apply(self, effect: Effect, snapshot: dict[str, dict]) -> None:
        if isinstance(effect, SendUpdate):
            record = snapshot.get(effect.slot_key)
            if record is None:
                record = {"slot": int(effect.slot_key), "status": "empty", "activity": "press to launch"}
            self.device.send(model.agent_update_event(record, self.clock.now()))
        elif isinstance(effect, Launch):
            self._apply_launch(effect.slot)
        elif isinstance(effect, Focus):
            self.terminal.focus(effect.tty)
        elif isinstance(effect, VoiceKey):
            self.terminal.post_key(self.terminal.terminal_pid(), SPACE_KEYCODE, effect.down)
        elif isinstance(effect, Log):
            self.log(effect.message)

    def _apply_launch(self, slot: int) -> None:
        plan = build_launch(slot, self.launch_config)
        if self.dry_run:
            self.log(f"Would launch slot {slot}: {plan.shell_command}")
            return
        tty = None if self.no_open else self.terminal.open(plan.shell_command)
        record = model.slot_record(**plan.record_fields, terminal_tty=tty, now=int(self.clock.now()))
        self.step(SlotRegistered(record))  # re-entrant: same thread, transaction already released

    def run(
        self,
        *,
        liveness_interval: float = 2.0,
        duration: float | None = None,
        hook_host: str = HOOK_HOST,
        hook_port: int = HOOK_PORT,
    ) -> int:
        stop = threading.Event()

        def read_serial() -> None:
            try:
                for line in self.device.lines():
                    parsed = parse_board_line(line)
                    if parsed is None:
                        continue
                    if isinstance(parsed, str):
                        self.log(parsed)
                        continue
                    self.submit(parsed)
                self.log("reader thread: device.lines() ended without an exception")
            except OSError as exc:
                self.submit(Shutdown(error=exc))

        def tick_liveness() -> None:
            while not stop.wait(liveness_interval):
                try:
                    with self.registry.transaction() as reg:
                        slots = reg.get("slots", {})
                        results = {k: self.prober.probe(v) for k, v in slots.items()}
                    if results:
                        self.submit(LivenessObserved(results))
                except Exception:  # noqa: BLE001 - must survive to probe again next tick
                    traceback.print_exc()

        reader_thread = threading.Thread(target=read_serial, daemon=True)
        reader_thread.start()
        ticker_thread = threading.Thread(target=tick_liveness, daemon=True)
        ticker_thread.start()
        hook_server = start_hook_server(self.submit, hook_host, hook_port)

        started = self.clock.monotonic()
        try:
            while True:
                if duration is not None and (self.clock.monotonic() - started) >= duration:
                    return 0
                try:
                    event = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                if isinstance(event, Shutdown):
                    self.last_shutdown_error = event.error
                    self.log(f"worker: got Shutdown(error={event.error!r}), stopping run()")
                    return 0
                try:
                    self.step(event)
                except Exception:  # noqa: BLE001 - the worker must be as unkillable as the ticker
                    traceback.print_exc()
        finally:
            stop.set()
            if hook_server is not None:
                # shutdown() blocks until serve_forever()'s loop notices and
                # exits — bound the wait so a slow/stuck HTTP server can
                # never keep run() (and whatever's join()ing its thread)
                # from returning. Always release the socket either way.
                shutdown_thread = threading.Thread(target=hook_server.shutdown, daemon=True)
                shutdown_thread.start()
                shutdown_thread.join(timeout=2.0)
                hook_server.server_close()
