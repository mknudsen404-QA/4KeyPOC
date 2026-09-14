"""Registering (and re-registering) Switchboard's CLI lifecycle hooks with
Claude Code and Codex. Unchanged logic from Phase 0 — just moved.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from switchboard.hooks_server import HOOK_HOST, HOOK_PATH_PREFIX, HOOK_PORT
from switchboard.model import CODEX_HOOK_EVENTS

# Substring used to identify Switchboard's own entries in ~/.claude/settings.json
# and $CODEX_HOME/hooks.json so re-installing (or another tool's installer) never
# clobbers unrelated hooks. Distinct from OpenMicro's /om-hook/ and vibesense's
# /hook/ markers, so all three can coexist.
HOOK_MARKER = f"{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}"

# Real lifecycle hook events, not screen-scraped guesses — see
# https://github.com/stephenleo/OpenMicro, which validated this approach.
# PreToolUse only fires for AskUserQuestion.
CLAUDE_HOOK_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "AskUserQuestion",
    "PostToolUse": None,
    "Notification": None,
    "Stop": None,
    "SessionEnd": None,
    # Fire for every subagent (Task-tool) run, including ones dispatched to
    # run in the background that outlive the main turn's Stop event — see
    # the "active_subagents" handling in reducer._apply_hook_event, which
    # is what these two exist to feed.
    "SubagentStart": None,
    "SubagentStop": None,
}


def _hook_command(event: str) -> str:
    # --max-time 1 and `|| true` make this fire-and-forget: it no-ops
    # harmlessly (within ~1s) whenever the bridge isn't running, so the
    # hook never needs uninstalling and never blocks the CLI on our behalf.
    return (
        f"curl -s --max-time 1 -X POST http://{HOOK_HOST}:{HOOK_PORT}{HOOK_PATH_PREFIX}{event} "
        f'-H "X-Switchboard-Slot: $SWITCHBOARD_SLOT" -d @- >/dev/null 2>&1 || true'
    )


def _codex_hook_command(event: str) -> str:
    # Codex hook commands must print a JSON object as their result.
    return _hook_command(event) + "; printf '{}'"


def _claude_group_is_ours(group: dict) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and HOOK_MARKER in h.get("command", "") for h in hooks)


def install_claude_hooks(settings_path: Path | None = None, dry_run: bool = False) -> str:
    """Returns "unchanged"/"changed"/"failed" normally. With dry_run=True,
    makes no writes and instead returns "installed" (already correct),
    "missing" (no Switchboard hooks present at all), or "stale marker"
    (some are present but don't match what we'd install now) — for
    `doctor`.
    """
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not parse {path}: {exc}", file=sys.stderr)
            return "failed"
    settings.setdefault("hooks", {})
    changed = False
    any_ours_present = False
    for event, matcher in CLAUDE_HOOK_EVENTS.items():
        groups = [g for g in settings["hooks"].get(event, []) if isinstance(g, dict)]
        foreign = [g for g in groups if not _claude_group_is_ours(g)]
        desired = {"hooks": [{"type": "command", "command": _hook_command(event)}]}
        if matcher is not None:
            desired = {"matcher": matcher, **desired}
        existing_ours = [g for g in groups if _claude_group_is_ours(g)]
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.switchboard-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        print(f"Could not write {path}: {exc}", file=sys.stderr)
        return "failed"
    return "changed"


def install_hooks(args=None) -> int:
    """The `install-hooks` CLI subcommand. Takes an (unused) argparse
    Namespace so it matches every other subcommand function's signature.
    """
    claude_result = install_claude_hooks()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    codex_result = install_codex_hooks()
    print(f"Claude Code hooks (~/.claude/settings.json): {claude_result}")
    print(f"Codex hooks ({codex_home / 'hooks.json'}): {codex_result}")
    if claude_result == "changed":
        print("Claude Code will use the new hooks starting with its next session.")
    if codex_result == "changed":
        print("Codex may report changed hooks on next launch — trust them via /hooks in the session.")
    return 0


def install_codex_hooks(hooks_path: Path | None = None, dry_run: bool = False) -> str:
    """See install_claude_hooks' docstring for the dry_run=True return values."""
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = hooks_path or (codex_home / "hooks.json")
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not parse {path}: {exc}", file=sys.stderr)
            return "failed"
    settings.setdefault("hooks", {})
    before = json.dumps(settings, sort_keys=True)
    any_ours_present = any(
        isinstance(groups, list) and any(_claude_group_is_ours(g) for g in groups)
        for groups in settings["hooks"].values()
    )

    for event in list(settings["hooks"].keys()):
        value = settings["hooks"][event]
        if not isinstance(value, list):
            continue
        foreign = [g for g in value if not _claude_group_is_ours(g)]
        if foreign:
            settings["hooks"][event] = foreign
        else:
            settings["hooks"].pop(event, None)

    for event in CODEX_HOOK_EVENTS:
        groups = settings["hooks"].get(event, [])
        if not isinstance(groups, list):
            groups = []
        groups.append({"hooks": [{"type": "command", "command": _codex_hook_command(event)}]})
        settings["hooks"][event] = groups

    after = json.dumps(settings, sort_keys=True)
    changed = after != before
    if dry_run:
        if not changed:
            return "installed"
        return "stale marker" if any_ours_present else "missing"
    if not changed:
        return "unchanged"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.switchboard-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        print(f"Could not write {path}: {exc}", file=sys.stderr)
        return "failed"
    return "changed"
