"""The Bridge: one queue, one owner. `step()` is the only code that touches
the registry inside the process — everything else (the serial reader
thread, the liveness ticker, the hook server) only ever calls `submit()`.
"""

from __future__ import annotations

import copy
import queue
import threading
import time
import traceback
from typing import Callable

from switchboard import model
from switchboard.device import DeviceLink
from switchboard.events import BoardEvent, Event, LivenessObserved, Shutdown, SlotRegistered, parse_board_line
from switchboard.hooks_server import HOOK_HOST, HOOK_PORT, start_hook_server
from switchboard.key_injector import FakeKeyInjector, KeyInjector
from switchboard.launcher import build_launch
from switchboard.liveness import ProcessProber
from switchboard.reducer import CloseTab, Effect, Focus, Launch, Log, ReducerState, SendUpdate, VoiceKey, reduce
from switchboard.registry import Registry
from switchboard.terminal import TerminalDriver

# How long to wait for Terminal to actually finish switching to a slot's tab
# before starting a PTT hold — Focus(tty) can return before the switch has
# visibly settled, which is the second most likely cause of a hold landing
# in the wrong tab.
FOCUS_SETTLE_TIMEOUT_S = 0.3
FOCUS_SETTLE_POLL_INTERVAL_S = 0.03

# How often the settings file is allowed to be re-stat()ed for a change on
# the periodic tick (Phase 2.4). A Launch always checks regardless of this
# throttle — it's cheap (one stat) and launches are rare enough that
# freshness there matters more than avoiding the syscall.
SETTINGS_RELOAD_INTERVAL_S = 5.0


def _default_log(message: str) -> None:
    # Plain `print` alone isn't enough here: when stdout is redirected to a
    # plain file (as the LaunchAgent does, for bridge.out.log — not a tty),
    # CPython fully block-buffers it, so log lines can sit invisible in
    # memory for arbitrarily long instead of reaching the log file when
    # they're printed. Confirmed live: a liveness-probe diagnostic line
    # never appeared on disk minutes after it must have run. flush=True
    # makes every log line land immediately regardless of buffering mode.
    print(message, flush=True)


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
        close_dead_tabs: bool = False,
        key_injector: KeyInjector | None = None,
        log: Callable[[str], None] = _default_log,
        settings_path=None,
    ) -> None:
        self.registry = registry
        self.device = device
        self.terminal = terminal
        self.prober = prober
        self.key_injector = key_injector or FakeKeyInjector()
        self.clock = clock
        self.launch_config = launch_config
        # settings_path is optional (Phase 2.4): when given, launch_config
        # is refreshed from switchboard.settings.SlotSettingsService before
        # each Launch and on a throttled tick — see _maybe_reload_settings.
        # Every existing caller that only passes a static launch_config
        # dict (every test, and any caller that doesn't opt in) keeps that
        # dict forever, unchanged.
        self.settings_path = settings_path
        self._settings_token = self._settings_version_token() if settings_path is not None else None
        # Baselined at construction time (not 0.0) so the throttle actually
        # throttles from startup instead of every bridge treating its first
        # tick as "5s overdue" just because clock.monotonic() isn't epoch 0.
        self._last_settings_check = clock.monotonic()
        self.auto_launch = auto_launch
        self.dry_run = dry_run
        self.no_open = no_open
        self.close_dead_tabs = close_dead_tabs
        self.log = log
        self.state = ReducerState()
        self._queue: "queue.Queue[Event]" = queue.Queue()
        # Set by run() when the serial reader thread's device.lines() raises
        # (a real disconnect) rather than run() simply hitting `duration` —
        # callers that retry on disconnect (cli.py's listen --retry) check
        # this after run() returns, since run() itself always returns 0.
        self.last_shutdown_error: Exception | None = None
        # slot_key -> (sent_at_monotonic, event_dict, attempts). Populated
        # by every SendUpdate; cleared by a matching agent.update.ack.
        # Retried (up to 3 attempts total) by the liveness ticker for any
        # entry that's gone unacked for over a second — the board may have
        # missed the original send (an interleaved-write hiccup, e.g.).
        self.pending_acks: dict[str, tuple[float, dict, int]] = {}

    def submit(self, event: Event) -> None:
        """Thread-safe; callable from any thread."""
        self._queue.put(event)

    def step(self, event: Event) -> None:
        """Process ONE event on the calling thread. The only place that
        touches the registry inside this process."""
        event = self._resolve_probes(event)
        if isinstance(event, BoardEvent) and event.name == "agent.update.ack":
            slot = event.payload.get("slot")
            if slot is not None:
                self.pending_acks.pop(str(slot), None)
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
            event_dict = model.agent_update_event(record, self.clock.now(), liveness=effect.liveness)
            self.device.send(event_dict)
            self.pending_acks[effect.slot_key] = (self.clock.monotonic(), event_dict, 1)
        elif isinstance(effect, Launch):
            self._apply_launch(effect.slot)
        elif isinstance(effect, Focus):
            self.terminal.focus(effect.tty)
        elif isinstance(effect, VoiceKey):
            self._apply_voice_key(effect)
        elif isinstance(effect, CloseTab):
            if self.close_dead_tabs:
                self.terminal.close(effect.tty)
        elif isinstance(effect, Log):
            self.log(effect.message)

    def _apply_voice_key(self, effect: VoiceKey) -> None:
        pid = self.terminal.terminal_pid()
        if not effect.down:
            self.key_injector.release(pid, SPACE_KEYCODE)
            return
        if effect.tty is not None and not self._wait_for_focus_settled(effect.tty):
            self.log(f"voice hold aborted: focus did not settle on {effect.tty} within {FOCUS_SETTLE_TIMEOUT_S}s")
            return
        self.key_injector.hold(pid, SPACE_KEYCODE)

    def _wait_for_focus_settled(self, tty: str) -> bool:
        deadline = time.monotonic() + FOCUS_SETTLE_TIMEOUT_S
        while True:
            if self.terminal.frontmost_tty() == tty:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(FOCUS_SETTLE_POLL_INTERVAL_S)

    def _settings_version_token(self) -> str | None:
        if self.settings_path is None:
            return None
        from switchboard.settings import SlotSettingsService

        return SlotSettingsService(self.settings_path).version_token()

    def _maybe_reload_settings(self, *, force_check: bool = False) -> None:
        """Phase 2.4: pick up agents.json changes without a bridge
        restart. A no-op unless `settings_path` was given. Only ever
        replaces `self.launch_config` — a slot already launched keeps
        running with whatever it was launched with; the new config
        applies on that slot's *next* launch.
        """
        if self.settings_path is None:
            return
        now = self.clock.monotonic()
        if not force_check and (now - self._last_settings_check) < SETTINGS_RELOAD_INTERVAL_S:
            return
        self._last_settings_check = now
        token = self._settings_version_token()
        if token == self._settings_token:
            return
        self._settings_token = token
        from switchboard.settings import SlotSettingsService, to_launch_config

        self.launch_config = to_launch_config(SlotSettingsService(self.settings_path).load())
        self.log("settings reloaded")

    def _apply_launch(self, slot: int) -> None:
        self._maybe_reload_settings(force_check=True)
        plan = build_launch(slot, self.launch_config)
        if self.dry_run:
            self.log(f"Would launch slot {slot}: {plan.shell_command}")
            return
        tty = None if self.no_open else self.terminal.open(plan.shell_command)
        record = model.slot_record(**plan.record_fields, terminal_tty=tty, now=int(self.clock.now()))
        self.step(SlotRegistered(record))  # re-entrant: same thread, transaction already released

    def retry_pending_acks(self) -> None:
        """Resend any SendUpdate the board hasn't acked in over a second,
        up to 3 attempts total; log and give up after that. Called from
        the liveness ticker (real use) or directly (tests, no threads).
        """
        now = self.clock.monotonic()
        for slot_key, (sent_at, event_dict, attempts) in list(self.pending_acks.items()):
            if now - sent_at < 1.0:
                continue
            if attempts >= 3:
                self.log(f"slot {slot_key}: board did not ack after 3 attempts")
                self.pending_acks.pop(slot_key, None)
                continue
            self.device.send(event_dict)
            self.pending_acks[slot_key] = (now, event_dict, attempts + 1)

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
                    self._maybe_reload_settings()
                    with self.registry.transaction() as reg:
                        slots = reg.get("slots", {})
                        results = {k: self.prober.probe(v) for k, v in slots.items()}
                    if results:
                        self.submit(LivenessObserved(results))
                    self.retry_pending_acks()
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
