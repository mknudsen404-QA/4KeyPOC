"""Turning "launch slot N" into a resolved command, a working directory,
and the shell script Terminal.app will run — used by both the CLI `launch`
command and the bridge's Launch effect.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

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

KNOWN_COMMAND_PATHS = {
    "codex": [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ],
    "claude": [
        str(Path.home() / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ],
}


def load_agents_config(path: Path) -> dict:
    if not path.exists():
        return {"agents": []}
    with path.expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Agent config is not an object: {path}")
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
        "family": "codex",
        "command": "codex",
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


def _write_agents_config(path: Path, config: dict) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def config_command(args) -> int:
    """The `config` CLI subcommand: edit host/agents.json (per-slot launch
    config). Takes an argparse.Namespace; kept here (not cli.py) because
    it's really just structured editing of the file load_agents_config
    reads, with the same fallback-to-example-config resolution.
    """
    agents_config_path = Path(args.agents_config).expanduser()
    if agents_config_path.exists():
        config = load_agents_config(agents_config_path)
    elif EXAMPLE_AGENTS_CONFIG.exists():
        config = load_agents_config(EXAMPLE_AGENTS_CONFIG)
    else:
        config = {"agents": []}

    if args.show:
        print(json.dumps(config, indent=2, sort_keys=True))
        return 0

    if args.default_cwd is not None:
        try:
            resolved = validate_or_create_cwd(args.default_cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        config.setdefault("defaults", {})["cwd"] = resolved
        _write_agents_config(agents_config_path, config)
        print(f"Default cwd set to {resolved}.")
        print("Takes effect on each slot's next launch.")
        return 0

    if args.slot is None:
        print("Specify --slot, --default-cwd, or --show.", file=sys.stderr)
        return 2

    agents = config.setdefault("agents", [])
    agent = next((a for a in agents if int(a.get("slot", 0)) == args.slot), None)
    if agent is None:
        agent = {"slot": args.slot}
        agents.append(agent)

    if args.cwd is not None:
        try:
            agent["cwd"] = validate_or_create_cwd(args.cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.name is not None:
        agent["name"] = args.name
    if args.family is not None:
        agent["family"] = args.family
    if args.command is not None:
        agent["command"] = args.command
    if args.effort is not None:
        agent["effort"] = normalize_effort(args.effort)

    _write_agents_config(agents_config_path, config)
    print(
        f"Slot {args.slot}: {agent.get('name', f'Agent {args.slot}')} "
        f"({agent.get('family', 'codex')}) command={agent.get('command', 'codex')!r} "
        f"cwd={resolve_agent_cwd(agent, config)}"
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
    family = agent.get("family", "codex")
    cwd = validate_or_create_cwd(resolve_agent_cwd(agent, config))
    effort_value = normalize_effort(effort if effort is not None else agent.get("effort"))
    base_command = resolve_command(agent.get("command", "codex"))
    command = command_with_effort(base_command, family, effort_value)
    title = agent.get("title") or f"Switchboard A{slot} {name}"
    shell_command = terminal_command(cwd, command, title, slot=slot)

    record_fields = {
        "slot": slot,
        "name": name,
        "family": family,
        "cwd": cwd,
        "command": base_command,
        "terminal_title": title,
        "effort": effort_value,
    }
    return LaunchPlan(record_fields=record_fields, shell_command=shell_command)
