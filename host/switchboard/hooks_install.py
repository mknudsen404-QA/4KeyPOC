"""Registering (and re-registering) Switchboard's CLI lifecycle hooks.
Each family's event list, matchers, and JSON-merge strategy come from its
FamilyProfile (switchboard/families/) via HookSpec — this module only
knows two generic ways to write those events into a hooks-JSON file
("merge_by_marker": keep foreign groups, replace/add ours per event, used
by Claude Code; "rewrite_group": strip all our old groups everywhere then
re-append one per event, used by Codex), not which family it's talking to.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from switchboard.families import registry as family_registry
from switchboard.families.base import HookSpec
from switchboard.families.claude import NAME as CLAUDE_FAMILY
from switchboard.families.codex import NAME as CODEX_FAMILY
from switchboard.hooks_server import HOOK_HOST, HOOK_PATH_PREFIX, HOOK_PORT

# Substring used to identify Switchboard's own entries in ~/.claude/settings.json
# and $CODEX_HOME/hooks.json so re-installing (or another tool's installer) never
# clobbers unrelated hooks. Distinct from OpenMicro's /om-hook/ and vibesense's
# /hook/ markers, so all three can coexist.
HOOK_MARKER = f"{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}"


def _hook_command(event: str, spec: HookSpec) -> str:
    # --max-time 1 and `|| true` make this fire-and-forget: it no-ops
    # harmlessly (within ~1s) whenever the bridge isn't running, so the
    # hook never needs uninstalling and never blocks the CLI on our behalf.
    base = (
        f"curl -s --max-time 1 -X POST http://{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}{event} "
        f'-H "X-Switchboard-Slot: $SWITCHBOARD_SLOT" -d @- >/dev/null 2>&1 || true'
    )
    return base + spec.command_suffix


def _group_is_ours(group: dict) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and HOOK_MARKER in h.get("command", "") for h in hooks)


def _write_json(path: Path, settings: dict) -> str | None:
    """Returns an error string on failure, else None."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.switchboard-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        return str(exc)
    return None


def _load_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        return json.loads(path.read_text()), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, str(exc)


def _install_merge_by_marker(spec: HookSpec, path_override: Path | None, dry_run: bool) -> str:
    """Returns "unchanged"/"changed"/"failed" normally. With dry_run=True,
    makes no writes and instead returns "installed" (already correct),
    "missing" (no Switchboard hooks present at all), or "stale marker"
    (some are present but don't match what we'd install now) — for
    `doctor`. Keeps any group not written by us (a "foreign" group) as-is,
    per event.
    """
    path = path_override or spec.config_path
    settings, error = _load_json(path)
    if error is not None:
        print(f"Could not parse {path}: {error}", file=sys.stderr)
        return "failed"
    settings = settings or {}
    settings.setdefault("hooks", {})
    changed = False
    any_ours_present = False
    for event, matcher in spec.matchers.items():
        groups = [g for g in settings["hooks"].get(event, []) if isinstance(g, dict)]
        foreign = [g for g in groups if not _group_is_ours(g)]
        desired = {"hooks": [{"type": "command", "command": _hook_command(event, spec)}]}
        if matcher is not None:
            desired = {"matcher": matcher, **desired}
        existing_ours = [g for g in groups if _group_is_ours(g)]
        if existing_ours:
            any_ours_present = True
        if len(existing_ours) == 1 and existing_ours[0] == desired:
            continue
        settings["hooks"][event] = foreign + [desired]
        changed = True
    if dry_run:
        if not changed:
            return "installed"
        return "stale marker" if any_ours_present else "missing"
    if not changed:
        return "unchanged"
    error = _write_json(path, settings)
    if error is not None:
        print(f"Could not write {path}: {error}", file=sys.stderr)
        return "failed"
    return "changed"


def _install_rewrite_group(spec: HookSpec, path_override: Path | None, dry_run: bool) -> str:
    """See _install_merge_by_marker's docstring for the dry_run=True return
    values. Strips every group we previously wrote (anywhere in the file,
    even an event no longer in `spec.events`) and re-appends exactly one
    fresh group per event — the shape Codex's hooks.json needs, since it
    doesn't support merging per-event the way Claude's settings.json does.
    """
    path = path_override or spec.config_path
    settings, error = _load_json(path)
    if error is not None:
        print(f"Could not parse {path}: {error}", file=sys.stderr)
        return "failed"
    settings = settings or {}
    settings.setdefault("hooks", {})
    before = json.dumps(settings, sort_keys=True)
    any_ours_present = any(
        isinstance(groups, list) and any(_group_is_ours(g) for g in groups) for groups in settings["hooks"].values()
    )

    for event in list(settings["hooks"].keys()):
        value = settings["hooks"][event]
        if not isinstance(value, list):
            continue
        foreign = [g for g in value if not _group_is_ours(g)]
        if foreign:
            settings["hooks"][event] = foreign
        else:
            settings["hooks"].pop(event, None)

    for event in spec.events:
        groups = settings["hooks"].get(event, [])
        if not isinstance(groups, list):
            groups = []
        groups.append({"hooks": [{"type": "command", "command": _hook_command(event, spec)}]})
        settings["hooks"][event] = groups

    after = json.dumps(settings, sort_keys=True)
    changed = after != before
    if dry_run:
        if not changed:
            return "installed"
        return "stale marker" if any_ours_present else "missing"
    if not changed:
        return "unchanged"
    error = _write_json(path, settings)
    if error is not None:
        print(f"Could not write {path}: {error}", file=sys.stderr)
        return "failed"
    return "changed"


_INSTALLERS = {
    "merge_by_marker": _install_merge_by_marker,
    "rewrite_group": _install_rewrite_group,
}


def _install_family_hooks(family_name: str, path_override: Path | None, dry_run: bool) -> str:
    spec = family_registry.get(family_name).hook_spec()
    if spec is None:
        return "unchanged"
    return _INSTALLERS[spec.merge_strategy](spec, path_override, dry_run)


def install_claude_hooks(settings_path: Path | None = None, dry_run: bool = False) -> str:
    return _install_family_hooks(CLAUDE_FAMILY, settings_path, dry_run)


def install_codex_hooks(hooks_path: Path | None = None, dry_run: bool = False) -> str:
    return _install_family_hooks(CODEX_FAMILY, hooks_path, dry_run)


def install_hooks(args=None) -> int:
    """The `install-hooks` CLI subcommand. Takes an (unused) argparse
    Namespace so it matches every other subcommand function's signature.
    """
    claude_result = install_claude_hooks()
    codex_result = install_codex_hooks()
    claude_path = family_registry.get(CLAUDE_FAMILY).hook_spec().config_path
    codex_path = family_registry.get(CODEX_FAMILY).hook_spec().config_path
    print(f"Claude Code hooks ({claude_path}): {claude_result}")
    print(f"Codex hooks ({codex_path}): {codex_result}")
    if claude_result == "changed":
        print("Claude Code will use the new hooks starting with its next session.")
    if codex_result == "changed":
        print("Codex may report changed hooks on next launch — trust them via /hooks in the session.")
    return 0
