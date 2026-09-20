"""The Bridge: one queue, one owner. `step()` is the only code that touches
the registry inside the process — everything else (the serial reader
thread, the liveness ticker, the hook server) only ever calls `submit()`.
"""

from __future__ import annotations

import copy
import datetime
import os
import queue
import secrets
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from switchboard import model
from switchboard.device import DeviceLink
from switchboard.events import BoardEvent, Event, HookEvent, LivenessObserved, Shutdown, SlotRegistered, parse_board_line
from switchboard.hooks_server import HOOK_HOST, HOOK_PORT, UIContext, start_hook_server
from switchboard.key_injector import FakeKeyInjector, KeyInjector
from switchboard.launcher import build_launch
from switchboard.liveness import ProcessProber
from switchboard.reducer import CloseTab, Effect, Focus, Launch, Log, ReducerState, SendUpdate, VoiceKey, reduce
from switchboard.registry import Registry
from switchboard.terminal import TerminalDriver
from switchboard.trace import NullTraceWriter, redact_hook_payload

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

# How often the liveness ticker emits a `heartbeat` trace record (every
# slot's current status and how long it's been there) — the input a dwell
# based bug (a status stuck for far longer than it should be) needs to be
# diagnosable at all, since without a periodic marker there is nothing to
# compare a stuck slot's last transition against.
HEARTBEAT_INTERVAL_S = 60.0


def _default_log(message: str) -> None:
    # Plain `print` alone isn't enough here: when stdout is redirected to a
    # plain file (as the LaunchAgent does, for bridge.out.log — not a tty),
    # CPython fully block-buffers it, so log lines can sit invisible in
    # memory for arbitrarily long instead of reaching the log file when
    # they're printed. Confirmed live: a liveness-probe diagnostic line
    # never appeared on disk minutes after it must have run. flush=True
    # makes every log line land immediately regardless of buffering mode.
    #
    # The timestamp prefix is what makes "this happened N minutes after
    # that" answerable from bridge.out.log alone — the file previously had
    # none, so questions like "how long was this idle before it dropped"
    # were unanswerable without cross-referencing something else.
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"{ts} {message}", flush=True)


# BoardEvent names for which the Bridge probes liveness itself and injects
# the result into the payload before handing the event to the (pure,
# probe-free) reducer.
_PROBED_EVENTS = ("agent.select", "voice.hold.start", "voice.hold.stop")


def _slot_str(slot) -> str | None:
    return None if slot is None else str(slot)


def _tty_basename(tty: str | None) -> str | None:
    return None if tty is None else tty.rsplit("/", 1)[-1]


def _cause_of(event: Event) -> str:
    """The trace's `cause` field for a transition/freed record: which
    input caused this reduce() call, for correlating a status change back
    to the event log line that explains it."""
    if isinstance(event, BoardEvent):
        return f"board:{event.name}"
    if isinstance(event, HookEvent):
        return f"hook:{event.name}"
    if isinstance(event, LivenessObserved):
        return "liveness"
    if isinstance(event, SlotRegistered):
        return "registered"
    return "unknown"


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
        trace=None,
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
        # The board-wide LED pulse config last sent to the board (Phase
        # "LED pulse settings") — None until startup_sync()'s unconditional
        # first send. Kept mainly for tests/inspection; re-sending an
        # unchanged value on every settings reload is harmless.
        self.led_config: dict | None = None
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
        # NullTraceWriter() by default (not a required argument): every
        # existing caller/test that doesn't pass trace= keeps behaving
        # exactly as before — see switchboard/trace.py.
        self.trace = trace if trace is not None else NullTraceWriter()
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

    def _log(self, message: str) -> None:
        """The single place a plain text log line is produced, so it can
        also reach trace.jsonl as a `log` record. Reassigning `self.log`
        directly (tests do this to capture messages) still works: this
        method calls `self.log(...)` by attribute lookup, so whatever is
        currently assigned there still receives every message."""
        self.log(message)
        self.trace.write({"kind": "log", "msg": message})

    def step(self, event: Event) -> None:
        """Process ONE event on the calling thread. The only place that
        touches the registry inside this process."""
        event = self._resolve_probes(event)
        self._trace_event_in(event)
        if isinstance(event, BoardEvent) and event.name == "agent.update.ack":
            slot = event.payload.get("slot")
            if slot is not None:
                pending = self.pending_acks.pop(str(slot), None)
                if pending is not None:
                    sent_at, _event_dict, _attempts = pending
                    self.trace.write(
                        {"kind": "ack", "slot": str(slot), "rtt_ms": round((self.clock.monotonic() - sent_at) * 1000, 1)}
                    )
        with self.registry.transaction() as reg:
            slots = reg.setdefault("slots", {})
            before = {
                slot_key: (record.get("status"), record.get("status_since"), record.get("family"))
                for slot_key, record in slots.items()
            }
            # Deep-copied now, before `reduce` can mutate it in place, so
            # it can be diffed against the post-reduce snapshot below to
            # tell whether this hook actually changed anything (a session-
            # ownership guard or a non-blocking Notification type both
            # legitimately no-op it — see reducer._apply_hook_event).
            hook_before_record = copy.deepcopy(slots.get(event.slot_key)) if isinstance(event, HookEvent) else None
            now = int(self.clock.now())
            new_slots, effects = reduce(slots, self.state, event, now, auto_launch=self.auto_launch)
            reg["slots"] = new_slots
            snapshot = copy.deepcopy(new_slots)  # for effects, after the lock is released
        if isinstance(event, SlotRegistered):
            slot_key = str(event.record["slot"])
            self.trace.write(
                {
                    "kind": "registered",
                    "slot": slot_key,
                    "family": event.record.get("family"),
                    "tty": _tty_basename(event.record.get("terminal_tty")),
                }
            )
        if isinstance(event, HookEvent):
            after_record = snapshot.get(event.slot_key)
            record = redact_hook_payload(event.payload) if self.trace.verbose is False else dict(event.payload)
            record.update(
                {
                    "kind": "hook",
                    "slot": event.slot_key,
                    "event": event.name,
                    "family": (after_record or hook_before_record or {}).get("family"),
                    "applied": after_record != hook_before_record,
                }
            )
            if self.trace.verbose:
                record["payload"] = event.payload
            rx_mono = getattr(event, "rx_mono", None)
            if rx_mono is not None:
                record["queue_ms"] = round((self.clock.monotonic() - rx_mono) * 1000, 1)
            self.trace.write(record)
        self._trace_transitions(before, snapshot, cause=_cause_of(event), now=now)
        for effect in effects:
            self._apply(effect, snapshot)

    def _trace_event_in(self, event: Event) -> None:
        """Emit the trace record for an event arriving at step(), before
        it's reduced — everything here is known from the event alone."""
        if isinstance(event, BoardEvent):
            record = {"kind": "board", "event": event.name, "slot": _slot_str(event.payload.get("slot"))}
            if "liveness" in event.payload:
                record["liveness"] = event.payload["liveness"]
            self.trace.write(record)
        elif isinstance(event, LivenessObserved):
            self.trace.write(
                {
                    "kind": "liveness",
                    "results": {k: v.value for k, v in event.results.items()},
                    "confirm": event.confirm,
                }
            )
        # HookEvent and SlotRegistered are traced after reduce (§4.2: they
        # need post-reduce info — `applied`/`family` for a hook, the final
        # record for a registration) rather than here.

    def _trace_transitions(self, before: dict, snapshot: dict, *, cause: str, now: int) -> None:
        """Diff every slot's status before/after one reduce() call and
        emit one `transition` (or `freed`, for a slot that no longer
        exists) record per slot whose status actually changed. Silent
        when nothing changed — most LivenessObserved steps are no-ops and
        must stay free."""

        def dwell_ms(status_since) -> int | None:
            if status_since is None:
                return None
            return max(0, (now - int(status_since)) * 1000)

        for slot_key, (from_status, status_since, family) in before.items():
            if slot_key not in snapshot:
                self.trace.write(
                    {
                        "kind": "freed",
                        "slot": slot_key,
                        "from": from_status,
                        "family": family,
                        "cause": cause,
                        "dwell_ms": dwell_ms(status_since),
                    }
                )
        for slot_key, record in snapshot.items():
            prior = before.get(slot_key)
            if prior is None:
                continue  # newly registered slot: covered by the `registered` record
            from_status, status_since, _family = prior
            to_status = record.get("status")
            if to_status != from_status:
                self.trace.write(
                    {
                        "kind": "transition",
                        "slot": slot_key,
                        "from": from_status,
                        "to": to_status,
                        "family": record.get("family"),
                        "cause": cause,
                        "dwell_ms": dwell_ms(status_since),
                    }
                )

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
        running yet) then push every agent slot's status to the board — a
        bridge restart always leaves the LEDs in a clean, correct state
        instead of whatever they last showed. Only slots 1-3 are real
        agent keys (firmware/neokey/neokey.ino's AGENT_SLOTS); the 4th
        physical key is push-to-talk, not a slot, so it isn't part of
        this loop.

        Applies the freeing via `reduce` directly (inside one transaction)
        rather than through `step`, discarding its SendUpdate effects: the
        unconditional loop below already covers every freed slot, so
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
        if self.settings_path is not None:
            from switchboard.settings import SlotSettingsService

            # Unconditional (not the token-change check _maybe_reload_settings
            # uses): the board needs this on every fresh connection, not just
            # when the file has changed since some earlier bridge run.
            self._send_led_config(SlotSettingsService(self.settings_path).load())
        for slot in range(1, 4):
            self._apply(SendUpdate(str(slot)), snapshot)

    def _apply(self, effect: Effect, snapshot: dict[str, dict]) -> None:
        if isinstance(effect, SendUpdate):
            record = snapshot.get(effect.slot_key)
            if record is None:
                record = {"slot": int(effect.slot_key), "status": "empty", "activity": "press to launch"}
            event_dict = model.agent_update_event(record, self.clock.now(), liveness=effect.liveness)
            self.device.send(event_dict)
            self.pending_acks[effect.slot_key] = (self.clock.monotonic(), event_dict, 1)
            self.trace.write({"kind": "effect", "type": "SendUpdate", "slot": effect.slot_key})
        elif isinstance(effect, Launch):
            self.trace.write({"kind": "effect", "type": "Launch", "slot": str(effect.slot)})
            self._apply_launch(effect.slot)
        elif isinstance(effect, Focus):
            self.trace.write({"kind": "effect", "type": "Focus"})
            self.terminal.focus(effect.tty)
        elif isinstance(effect, VoiceKey):
            self.trace.write({"kind": "effect", "type": "VoiceKey"})
            self._apply_voice_key(effect)
        elif isinstance(effect, CloseTab):
            self.trace.write({"kind": "effect", "type": "CloseTab"})
            if self.close_dead_tabs:
                self.terminal.close(effect.tty)
        elif isinstance(effect, Log):
            self._log(effect.message)

    def _apply_voice_key(self, effect: VoiceKey) -> None:
        from switchboard.voice import registry as voice_registry
        from switchboard.voice.base import VoiceContext

        provider = voice_registry.get(effect.provider)
        pid = self.terminal.terminal_pid()
        ctx = VoiceContext(pid=pid, key_injector=self.key_injector, chord=effect.chord, mode=effect.mode, log=self._log)
        if not effect.down:
            provider.release(ctx)
            return
        if effect.tty is not None and not self._wait_for_focus_settled(effect.tty):
            self._log(f"voice hold aborted: focus did not settle on {effect.tty} within {FOCUS_SETTLE_TIMEOUT_S}s")
            return
        provider.hold(ctx)

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

        doc = SlotSettingsService(self.settings_path).load()
        self.launch_config = to_launch_config(doc)
        self._send_led_config(doc)
        self._log("settings reloaded")
        self.trace.write({"kind": "settings"})

    def _send_led_config(self, doc: dict) -> None:
        """Send the board-wide LED pulse config (idle-breathe period, busy
        color-ramp duration — see settings.led_config_wire) as a
        `led.config` event. Firmware/neokey/neokey.ino stores whatever it
        last received and keeps its own compiled-in defaults (solid idle,
        the original 5-minute ramp) until the first one arrives, so an
        older board that doesn't understand this event yet just ignores
        the unrecognised line and keeps working exactly as before.
        """
        from switchboard.settings import led_config_wire

        wire = led_config_wire(doc)
        self.device.send({"event": "led.config", **wire})
        self.led_config = wire

    def ui_token_path(self) -> Path:
        """Where `run()` writes the settings UI's per-run token (Phase 3),
        so a separate `switchboard settings` CLI invocation can discover
        the currently-running bridge's token and build the right URL.
        Colocated with the registry (already this process's own personal,
        gitignored state) rather than a new well-known directory.
        """
        return self.registry.path.with_name(self.registry.path.name + ".ui_token")

    def _apply_launch(self, slot: int) -> None:
        self._maybe_reload_settings(force_check=True)
        plan = build_launch(slot, self.launch_config)
        if self.dry_run:
            self._log(f"Would launch slot {slot}: {plan.shell_command}")
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
                self._log(f"slot {slot_key}: board did not ack after 3 attempts")
                self.trace.write({"kind": "ack_giveup", "slot": slot_key})
                self.pending_acks.pop(slot_key, None)
                continue
            self.device.send(event_dict)
            self.pending_acks[slot_key] = (now, event_dict, attempts + 1)

    def _emit_heartbeat(self, slots: dict[str, dict]) -> None:
        """Every slot's current status and how long it's been there — the
        only way a stuck status (bugs like "needs_input lingers for most
        of a turn" or "slot silently frees after 15 idle minutes") is
        measurable after the fact, since without a periodic marker there
        is nothing to compare a slot's last transition against.
        """
        now = int(self.clock.now())
        slot_info = {}
        for slot_key, record in slots.items():
            status_since = record.get("status_since")
            dwell_ms = None if status_since is None else max(0, (now - int(status_since)) * 1000)
            slot_info[slot_key] = {"status": record.get("status"), "dwell_ms": dwell_ms, "family": record.get("family")}
        self.trace.write(
            {
                "kind": "heartbeat",
                "port": getattr(self.device, "port", None),
                "slots": slot_info,
                "pending_acks": len(self.pending_acks),
                "queue_depth": self._queue.qsize(),
            }
        )

    def run(
        self,
        *,
        liveness_interval: float = 2.0,
        duration: float | None = None,
        hook_host: str = HOOK_HOST,
        hook_port: int = HOOK_PORT,
    ) -> int:
        stop = threading.Event()
        self.trace.write(
            {
                "kind": "start",
                "port": getattr(self.device, "port", None),
                "pid": os.getpid(),
                "verbose": self.trace.verbose,
            }
        )

        def read_serial() -> None:
            try:
                for line in self.device.lines():
                    parsed = parse_board_line(line)
                    if parsed is None:
                        continue
                    if isinstance(parsed, str):
                        self._log(parsed)
                        continue
                    self.submit(parsed)
                self._log("reader thread: device.lines() ended without an exception")
            except OSError as exc:
                self.submit(Shutdown(error=exc))

        def tick_liveness() -> None:
            last_heartbeat = self.clock.monotonic()
            while not stop.wait(liveness_interval):
                try:
                    self._maybe_reload_settings()
                    with self.registry.transaction() as reg:
                        slots = reg.get("slots", {})
                        results = {k: self.prober.probe(v) for k, v in slots.items()}
                    if results:
                        self.submit(LivenessObserved(results))
                    self.retry_pending_acks()
                    now_mono = self.clock.monotonic()
                    if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                        last_heartbeat = now_mono
                        self._emit_heartbeat(slots)
                except Exception as exc:  # noqa: BLE001 - must survive to probe again next tick
                    traceback.print_exc()
                    self.trace.write({"kind": "error", "where": "tick_liveness", "type": type(exc).__name__, "msg": str(exc)})

        reader_thread = threading.Thread(target=read_serial, daemon=True)
        reader_thread.start()
        ticker_thread = threading.Thread(target=tick_liveness, daemon=True)
        ticker_thread.start()

        # The settings UI (Phase 3) is only wired up when this Bridge has a
        # settings_path (Phase 2.4's hot-reload opt-in) — every existing
        # caller/test without one keeps the hook-POST-only server it had
        # before, with ui_context=None.
        ui_context: UIContext | None = None
        ui_token_path: Path | None = None
        if self.settings_path is not None:
            token = secrets.token_urlsafe(24)
            ui_context = UIContext(settings_path=self.settings_path, registry=self.registry, token=token)
            ui_token_path = self.ui_token_path()
            try:
                ui_token_path.parent.mkdir(parents=True, exist_ok=True)
                ui_token_path.write_text(token)
                ui_token_path.chmod(0o600)
            except OSError as exc:
                self._log(f"Could not write UI token file {ui_token_path}: {exc}")
        hook_server = start_hook_server(self.submit, hook_host, hook_port, ui_context=ui_context)

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
                    self._log(f"worker: got Shutdown(error={event.error!r}), stopping run()")
                    return 0
                try:
                    self.step(event)
                except Exception as exc:  # noqa: BLE001 - the worker must be as unkillable as the ticker
                    traceback.print_exc()
                    self.trace.write({"kind": "error", "where": "worker", "type": type(exc).__name__, "msg": str(exc)})
        finally:
            stop.set()
            if ui_token_path is not None:
                try:
                    ui_token_path.unlink()
                except OSError:
                    pass
            if hook_server is not None:
                # shutdown() blocks until serve_forever()'s loop notices and
                # exits — bound the wait so a slow/stuck HTTP server can
                # never keep run() (and whatever's join()ing its thread)
                # from returning. Always release the socket either way.
                shutdown_thread = threading.Thread(target=hook_server.shutdown, daemon=True)
                shutdown_thread.start()
                shutdown_thread.join(timeout=2.0)
                hook_server.server_close()
