"""Settings v2: the on-disk schema for per-slot launch config
(agents.json), a small stdlib validator (no jsonschema dependency), the
v1 -> v2 migration, and SlotSettingsService — the one place that loads,
validates, and atomically saves it. The `config` CLI subcommand
(launcher.config_command) and, from Phase 3, the web UI's /api/settings
route are both thin callers over this service.

v1 shape (still readable, never written by this module):
    {"defaults": {"cwd": ...}, "agents": [{"slot", "name", "family",
     "cwd", "command", "title", "effort"}, ...]}

v2 shape (what SlotSettingsService always reads/writes):
    {"settings_version": 2, "defaults": {"cwd", "effort"},
     "slots": [{"slot", "name", "family", "command", "args", "cwd",
     "effort", "voice": {"provider", "chord", "mode"}, "env"}, ...],
     "leds": {"idle_breathe", "thinking_cycle"}}

`family` is optional (inferred from `command`'s basename at launch time —
see families.FamilyRegistry.infer); `args` is separate from `command` so
a UI can offer a command dropdown plus a free-text args field; `title` is
dropped in v2 (regenerated from name/slot, same as launcher.build_launch
already does when an agent has no `title`); `voice`/`env` are new,
consumed starting in later phases.

`leds` is global (board-wide), not per-slot — LEDs communicate status,
not per-agent identity, so one consistent pulse feel across all three
keys is the design choice (see the "LED pulse settings" section of
docs/design/slot-settings-and-family-parity-plan.md's follow-up work).
Its two named presets each resolve (led_config_wire()) to the raw
millisecond values firmware/neokey/led_model.h actually understands,
sent to the board as a `led.config` event whenever they change — the
board never sees preset names, so the two vocabularies can evolve
independently.

See docs/design/slot-settings-and-family-parity-plan.md Phase 2.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

SETTINGS_VERSION = 2
DEFAULT_CWD = "~/Documents"
DEFAULT_EFFORT = "medium"
EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")

# Voice providers this schema accepts today. "claude_native" and "hotkey"
# are real (Phase 4); "builtin_stt" is reserved but always unavailable
# (see the plan's Phase 4.1) so it isn't accepted here yet — a document
# naming it fails validation rather than silently doing nothing.
VOICE_PROVIDERS = ("claude_native", "hotkey", "none")
VOICE_MODES = ("hold", "toggle")

# Today's board: 3 stable onboard agent keys — firmware/neokey/neokey.ino's
# AGENT_SLOTS = {1, 2, 3}. The 4th physical key (PTT_KEY_INDEX) is a
# dedicated push-to-talk button, not a 4th agent slot: it has no family or
# command of its own, it just drives voice.hold.start/stop for whichever
# agent slot is currently selected — which is why `voice` lives as a field
# *on* each agent slot above, not as a separate slot 4.
MIN_SLOT = 1
MAX_SLOT = 3

# Board-wide LED pulse presets. Values are milliseconds the firmware
# understands directly (see firmware/neokey/led_model.h): idle_breathe's
# value is a static pulse period for the idle status (0 = solid, the
# firmware's own built-in default if a board never receives led.config
# at all); thinking_cycle's value is how long the busy-status color ramp
# (white -> blue -> magenta) takes end to end, which also scales how
# fast its pulse speeds up along the way (see busyPulsePeriodMs).
LED_IDLE_BREATHE_PRESETS = {"off": 0, "slow": 7000, "medium": 4000}
LED_THINKING_CYCLE_PRESETS = {"quick": 90000, "normal": 300000, "slow": 600000}
LED_IDLE_BREATHE_DEFAULT = "off"
LED_THINKING_CYCLE_DEFAULT = "normal"


@dataclass(frozen=True)
class FieldError:
    path: str  # e.g. "slots[1].cwd"
    message: str
    severity: str = "error"  # "error" blocks save(); "warning" doesn't


class SettingsValidationError(ValueError):
    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.path}: {e.message}" for e in errors))


def _normalize_effort(effort: str | None) -> str:
    value = (effort or DEFAULT_EFFORT).strip().lower()
    return {"med": "medium"}.get(value, value)


def empty_document() -> dict:
    return {"settings_version": SETTINGS_VERSION, "defaults": {}, "slots": [], "leds": {}}


def led_config_wire(doc: dict) -> dict:
    """Resolve `doc["leds"]`'s preset names to the raw millisecond values
    firmware/neokey/led_model.h actually understands. Always returns both
    keys (falling back to the documented defaults) so a caller can send
    this straight to the board without checking for missing fields.
    """
    leds = doc.get("leds") or {}
    idle_name = leds.get("idle_breathe", LED_IDLE_BREATHE_DEFAULT)
    cycle_name = leds.get("thinking_cycle", LED_THINKING_CYCLE_DEFAULT)
    return {
        "idle_pulse_ms": LED_IDLE_BREATHE_PRESETS.get(idle_name, LED_IDLE_BREATHE_PRESETS[LED_IDLE_BREATHE_DEFAULT]),
        "busy_ramp_ms": LED_THINKING_CYCLE_PRESETS.get(cycle_name, LED_THINKING_CYCLE_PRESETS[LED_THINKING_CYCLE_DEFAULT]),
    }


def migrate_v1_to_v2(doc: dict) -> dict:
    """`agents` -> `slots`; `title` dropped (regenerated); everything else
    carried as-is. Never mutates `doc`.

    A legacy agent whose slot number is outside MIN_SLOT..MAX_SLOT is
    dropped rather than carried forward: some existing agents.json files
    predate the fix that confirmed only 3 physical keys are real agent
    slots (the 4th is a dedicated PTT button — see MAX_SLOT's comment)
    and configure a "slot 4" that firmware has never actually launched.
    Carrying it into v2 would make every future save() fail validation
    until the user noticed and removed it by hand.
    """
    defaults = dict(doc.get("defaults", {}))
    slots = []
    for agent in doc.get("agents", []):
        if not isinstance(agent, dict) or "slot" not in agent:
            continue
        number = agent["slot"]
        if isinstance(number, int) and not (MIN_SLOT <= number <= MAX_SLOT):
            continue
        slot_doc: dict = {"slot": number}
        for key in ("name", "family", "command", "cwd", "effort", "voice"):
            if agent.get(key) is not None:
                slot_doc[key] = agent[key]
        slots.append(slot_doc)
    return {"settings_version": SETTINGS_VERSION, "defaults": defaults, "slots": slots, "leds": {}}


def normalize_document(raw: dict) -> dict:
    """Whatever shape is on disk, return the v2 shape in memory. Never
    writes anything — a migration is only persisted the next time the
    caller calls save() (Phase 2.2)."""
    if raw.get("settings_version") == SETTINGS_VERSION:
        doc = dict(raw)
        doc.setdefault("defaults", {})
        doc.setdefault("slots", [])
        doc.setdefault("leds", {})
        return doc
    return migrate_v1_to_v2(raw)


def to_launch_config(doc: dict) -> dict:
    """Adapt a v2 document to the v1-ish `{"agents": [...], "defaults":
    {...}}` shape launcher.build_launch already knows how to consume, so
    the launch pipeline itself doesn't need to change for v2 to work.
    `args` is joined onto `command` (shlex-quoted) since build_launch
    still expects one shell command string. `voice` is passed through
    as-is (Phase 4: build_launch resolves it against the family default
    when absent); `env` still isn't consumed by the launch pipeline yet.
    """
    agents = []
    for slot_doc in doc.get("slots", []):
        agent: dict = {"slot": slot_doc.get("slot")}
        for key in ("name", "family", "cwd", "effort", "voice"):
            if slot_doc.get(key) is not None:
                agent[key] = slot_doc[key]
        command = slot_doc.get("command")
        if command is not None:
            args = slot_doc.get("args") or []
            agent["command"] = shlex.join([command, *args]) if args else command
        agents.append(agent)
    return {"agents": agents, "defaults": dict(doc.get("defaults", {}))}


def validate_document(doc: dict) -> list[FieldError]:
    errors: list[FieldError] = []

    if doc.get("settings_version") != SETTINGS_VERSION:
        errors.append(FieldError("settings_version", f"must be {SETTINGS_VERSION}"))

    defaults = doc.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append(FieldError("defaults", "must be an object"))
        defaults = {}
    if "effort" in defaults and _normalize_effort(defaults["effort"]) not in EFFORT_VALUES:
        errors.append(FieldError("defaults.effort", f"must be one of {EFFORT_VALUES}"))

    leds = doc.get("leds", {})
    if not isinstance(leds, dict):
        errors.append(FieldError("leds", "must be an object"))
        leds = {}
    if "idle_breathe" in leds and leds["idle_breathe"] not in LED_IDLE_BREATHE_PRESETS:
        errors.append(FieldError("leds.idle_breathe", f"must be one of {tuple(LED_IDLE_BREATHE_PRESETS)}"))
    if "thinking_cycle" in leds and leds["thinking_cycle"] not in LED_THINKING_CYCLE_PRESETS:
        errors.append(FieldError("leds.thinking_cycle", f"must be one of {tuple(LED_THINKING_CYCLE_PRESETS)}"))

    slots = doc.get("slots", [])
    if not isinstance(slots, list):
        errors.append(FieldError("slots", "must be a list"))
        return errors

    seen_slot_numbers: dict[int, int] = {}
    for index, slot_doc in enumerate(slots):
        path = f"slots[{index}]"
        if not isinstance(slot_doc, dict):
            errors.append(FieldError(path, "must be an object"))
            continue

        number = slot_doc.get("slot")
        if not isinstance(number, int) or isinstance(number, bool):
            errors.append(FieldError(f"{path}.slot", "must be an integer"))
        elif not (MIN_SLOT <= number <= MAX_SLOT):
            errors.append(FieldError(f"{path}.slot", f"must be between {MIN_SLOT} and {MAX_SLOT}"))
        else:
            seen_slot_numbers[number] = seen_slot_numbers.get(number, 0) + 1

        command = slot_doc.get("command")
        if command is None or not str(command).strip():
            errors.append(FieldError(f"{path}.command", "no command set — this slot won't launch anything", "warning"))
        elif not (_command_is_resolvable(str(command))):
            errors.append(FieldError(f"{path}.command", f"{command!r} was not found on PATH or a known install location", "warning"))

        cwd = slot_doc.get("cwd")
        if cwd is not None:
            error = _cwd_error(str(cwd))
            if error is not None:
                errors.append(FieldError(f"{path}.cwd", error))

        if "effort" in slot_doc and _normalize_effort(slot_doc["effort"]) not in EFFORT_VALUES:
            errors.append(FieldError(f"{path}.effort", f"must be one of {EFFORT_VALUES}"))

        args = slot_doc.get("args")
        if args is not None and (not isinstance(args, list) or not all(isinstance(a, str) for a in args)):
            errors.append(FieldError(f"{path}.args", "must be a list of strings"))

        env = slot_doc.get("env")
        if env is not None and (not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values())):
            errors.append(FieldError(f"{path}.env", "must be an object of string values"))

        voice = slot_doc.get("voice")
        if voice is not None:
            errors.extend(_voice_errors(f"{path}.voice", voice))

    for number, count in seen_slot_numbers.items():
        if count > 1:
            errors.append(FieldError("slots", f"slot {number} is configured {count} times"))

    return errors


def _voice_errors(path: str, voice) -> list[FieldError]:
    if not isinstance(voice, dict):
        return [FieldError(path, "must be an object")]
    errors = []
    provider = voice.get("provider")
    if provider is not None and provider not in VOICE_PROVIDERS:
        errors.append(FieldError(f"{path}.provider", f"must be one of {VOICE_PROVIDERS}"))
    mode = voice.get("mode")
    if mode is not None and mode not in VOICE_MODES:
        errors.append(FieldError(f"{path}.mode", f"must be one of {VOICE_MODES}"))
    return errors


def _cwd_error(cwd: str) -> str | None:
    resolved = Path(cwd).expanduser()
    if resolved.exists():
        return None
    documents_dir = Path.home() / "Documents"
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    if resolved == documents_dir or documents_dir in resolved.parents:
        return None  # would be created on next launch, same rule as launcher.validate_or_create_cwd
    return f"{resolved} does not exist"


def _command_is_resolvable(command: str) -> bool:
    # Deferred import: launcher.py doesn't import settings.py at module
    # scope, but avoid a hard dependency edge either direction beyond
    # what's needed to reuse its actual resolution logic (the same PATH +
    # per-family known_paths lookup a real launch would use).
    from switchboard.launcher import resolve_command

    try:
        resolve_command(command)
        return True
    except (ValueError, FileNotFoundError):
        return False


class SlotSettingsService:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return empty_document()
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Settings file is not an object: {self.path}")
        return data

    def load(self) -> dict:
        return normalize_document(self._read_raw())

    def validate(self, doc: dict) -> list[FieldError]:
        return validate_document(doc)

    def save(self, doc: dict) -> None:
        errors = [e for e in self.validate(doc) if e.severity == "error"]
        if errors:
            raise SettingsValidationError(errors)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup = self.path.with_name(self.path.name + ".bak")
            backup.write_bytes(self.path.read_bytes())
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def slot(self, number: int) -> dict | None:
        for slot_doc in self.load().get("slots", []):
            if slot_doc.get("slot") == number:
                return slot_doc
        return None

    def update_slot(self, number: int, patch: dict, *, doc: dict | None = None) -> dict:
        """Merge `patch` into slot `number` (creating it if absent) and
        save. A key mapped to None is removed rather than set. `doc` lets
        a caller supply an already-loaded (e.g. seeded-from-template)
        document instead of re-reading `self.path`.
        """
        doc = self.load() if doc is None else doc
        slots = doc.setdefault("slots", [])
        existing = next((s for s in slots if s.get("slot") == number), None)
        if existing is None:
            existing = {"slot": number}
            slots.append(existing)
        for key, value in patch.items():
            if value is None:
                existing.pop(key, None)
            else:
                existing[key] = value
        self.save(doc)
        return existing

    def version_token(self) -> str:
        """Cheap change-detection token (mtime+size) — not a hash, so it
        can miss a same-second same-size edit, but good enough to gate a
        5s poll rather than re-reading the file every tick."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return "missing"
        return f"{stat.st_mtime_ns}:{stat.st_size}"
