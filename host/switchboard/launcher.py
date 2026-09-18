"""Turning "launch slot N" into a resolved command, a working directory,
and the shell script Terminal.app will run — used by both the CLI `launch`
command and the bridge's Launch effect.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from switchboard.families import DEFAULT_FAMILY
from switchboard.families import registry as family_registry
from switchboard.model import command_with_effort, normalize_effort

HOST_DIR = Path(__file__).resolve().parent.parent
# agents.json is the user's own per-slot config (which CLI family/command each
# agent key launches). It is personal/local, not checked in. agents.example.json
# is the checked-in template; it's only used as a fallback until agents.json
# exists, so first-time setup still works with no configuration.
USER_AGENTS_CONFIG = HOST_DIR / "agents.json"
EXAMPLE_AGENTS_CONFIG = HOST_DIR / "agents.example.json"
DEFAULT_AGENTS_CONFIG = USER_AGENTS_CONFIG if USER_AGENTS_CONFIG.exists() else EXAMPLE_AGENTS_CONFIG

DEFAULT_CWD = "~/Documents"

# Per-family fallback binary paths (used when the command isn't on PATH),
# sourced from each family's own profile — see switchboard/families/.
KNOWN_COMMAND_PATHS = family_registry.known_paths_map()


def load_agents_config(path: Path) -> dict:
    """Returns the v1-shaped `{"agents": [...], "defaults": {...}}` dict
    every launch function here expects, regardless of whether `path` is
    an old v1 file or a v2 settings document (settings_version: 2,
    `slots` instead of `agents`) — see switchboard/settings.py. This is
    what lets the whole launch pipeline stay unchanged as agents.json
    migrates to v2.
    """
    if not path.exists():
        return {"agents": []}
    with path.expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Agent config is not an object: {path}")
    if data.get("settings_version") == 2:
        from switchboard.settings import to_launch_config

        return to_launch_config(data)
    data.setdefault("agents", [])
    return data


def agent_config_for_slot(config: dict | None, slot: int) -> dict:
    agents = (config or {}).get("agents", [])
    for agent in agents:
        if int(agent.get("slot", 0)) == slot:
            return dict(agent)
    return {
        "slot": slot,
        "name": f"Agent {slot}",
        "family": DEFAULT_FAMILY,
        "command": DEFAULT_FAMILY,
    }


def resolve_agent_cwd(agent: dict, config: dict | None) -> str:
    """Resolution order: slot cwd -> defaults.cwd -> ~/Documents."""
    cwd = agent.get("cwd") or (config or {}).get("defaults", {}).get("cwd") or DEFAULT_CWD
    return str(Path(cwd).expanduser().resolve())


def validate_or_create_cwd(cwd_str: str) -> str:
    """Resolve cwd_str, creating it if missing and it's under ~/Documents.

    Raises ValueError (with a user-facing message) if it's missing and
    outside ~/Documents — creating an arbitrary path elsewhere on the
    filesystem on the user's behalf is not something to do silently.
    """
    resolved = Path(cwd_str).expanduser().resolve()
    if not resolved.exists():
        documents_dir = Path.home() / "Documents"
        if resolved == documents_dir or documents_dir in resolved.parents:
            resolved.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"Working folder does not exist: {resolved}")
    return str(resolved)


def resolve_command(command: str) -> str:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("Command is empty")

    executable = parts[0]
    if "/" in executable:
        if Path(executable).expanduser().exists():
            parts[0] = str(Path(executable).expanduser())
            return shlex.join(parts)
        raise FileNotFoundError(f"Command not found: {executable}")

    found = shutil.which(executable)
    if found:
        parts[0] = found
        return shlex.join(parts)

    for candidate in KNOWN_COMMAND_PATHS.get(executable, []):
        candidate_path = Path(candidate).expanduser()
        if candidate_path.exists():
            parts[0] = str(candidate_path)
            return shlex.join(parts)

    raise FileNotFoundError(f"Command not found on PATH: {executable}")


def terminal_command(cwd: str, command: str, title: str, slot: int | None = None) -> str:
    lines = [
        f"printf '\\033]0;%s\\007' {shlex.quote(title)}",
        f"cd {shlex.quote(cwd)}",
    ]
    if slot is not None:
        # Lets this session's lifecycle hooks (see hooks_install.py) tag
        # their POST with which slot they came from — same trick OpenMicro
        # uses with OPENMICRO_INSTANCE_ID.
        lines.append(f"export SWITCHBOARD_SLOT={int(slot)}")
    lines.append(f"exec {command}")
    return "\n".join(lines)


def config_command(args) -> int:
    """The `config` CLI subcommand: a thin wrapper over
    SlotSettingsService (settings.py) — it turns argparse flags into a
    patch dict and lets the service do the load/validate/save. Always
    reads and writes settings v2 (`{"settings_version": 2, "slots": [...]}`
    ); an existing v1 agents.json is migrated to v2 in memory the first
    time this runs and only written back once a change is actually saved.
    """
    from switchboard.settings import SettingsValidationError, SlotSettingsService

    agents_config_path = Path(args.agents_config).expanduser()
    service = SlotSettingsService(agents_config_path)
    if agents_config_path.exists():
        doc = service.load()
    elif EXAMPLE_AGENTS_CONFIG.exists():
        doc = SlotSettingsService(EXAMPLE_AGENTS_CONFIG).load()
    else:
        doc = service.load()  # empty v2 document

    if args.show:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    if args.default_cwd is not None:
        try:
            resolved = validate_or_create_cwd(args.default_cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        doc.setdefault("defaults", {})["cwd"] = resolved
        service.save(doc)
        print(f"Default cwd set to {resolved}.")
        print("Takes effect on each slot's next launch.")
        return 0

    if args.slot is None:
        print("Specify --slot, --default-cwd, or --show.", file=sys.stderr)
        return 2

    patch: dict = {}
    if args.cwd is not None:
        try:
            patch["cwd"] = validate_or_create_cwd(args.cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.name is not None:
        patch["name"] = args.name
    if args.family is not None:
        patch["family"] = args.family
    if args.command is not None:
        patch["command"] = args.command
    if args.effort is not None:
        patch["effort"] = normalize_effort(args.effort)

    try:
        slot_doc = service.update_slot(args.slot, patch, doc=doc)
    except SettingsValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        f"Slot {args.slot}: {slot_doc.get('name', f'Agent {args.slot}')} "
        f"({slot_doc.get('family', DEFAULT_FAMILY)}) command={slot_doc.get('command', DEFAULT_FAMILY)!r} "
        f"cwd={resolve_agent_cwd(slot_doc, doc)}"
    )
    print("Takes effect on this slot's next launch.")
    return 0


@dataclass
class LaunchPlan:
    record_fields: dict  # kwargs for model.slot_record (minus `now`)
    shell_command: str


def build_launch(
    slot: int, config: dict | None, *, effort: str | None = None, overrides: dict | None = None
) -> LaunchPlan:
    """Resolve everything about launching `slot`: which command, in which
    directory, with which effort flags, and the exact shell script Terminal
    will run. `overrides` (from explicit CLI flags) win over the per-slot
    agent config; `effort` (also a CLI flag) wins over the config's effort.
    """
    agent = agent_config_for_slot(config, slot)
    if overrides:
        agent = {**agent, **{k: v for k, v in overrides.items() if v is not None}}

    name = agent.get("name", f"Agent {slot}")
    command_field = agent.get("command", DEFAULT_FAMILY)
    if "family" in agent:
        family = agent["family"]
    else:
        # No explicit family: infer it from the command's basename rather
        # than silently defaulting to Codex's family — a slot configured
        # with command: gemini and no family used to get Codex's effort flags.
        family = family_registry.infer(command_field)
        if family != DEFAULT_FAMILY:
            print(
                f"Slot {slot}: no 'family' set for command {command_field!r}; inferred '{family}'. "
                "Set 'family' explicitly in agents.json to silence this.",
                file=sys.stderr,
            )
    cwd = validate_or_create_cwd(resolve_agent_cwd(agent, config))
    effort_value = normalize_effort(effort if effort is not None else agent.get("effort"))
    base_command = resolve_command(command_field)
    command = command_with_effort(base_command, family, effort_value)
    title = agent.get("title") or f"Switchboard A{slot} {name}"
    shell_command = terminal_command(cwd, command, title, slot=slot)

    if "voice" in agent:
        voice = dict(agent["voice"])
    else:
        default = family_registry.get(family).default_voice()
        voice = {"provider": default.provider, "chord": default.chord, "mode": default.mode}

    record_fields = {
        "slot": slot,
        "name": name,
        "family": family,
        "cwd": cwd,
        "command": base_command,
        "terminal_title": title,
        "effort": effort_value,
        "voice": voice,
    }
    return LaunchPlan(record_fields=record_fields, shell_command=shell_command)
