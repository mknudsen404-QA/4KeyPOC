import json

from switchboard import hooks_install
from switchboard.families.antigravity import AntigravityProfile


def test_antigravity_hook_command_echoes_required_contract():
    """Unlike Claude/Codex's hook command (stdout thrown to /dev/null),
    Antigravity actually reads this stdout and blocks on it — PreToolUse
    must get a `decision`, Stop must not, or the agent can never stop."""
    pre = hooks_install._antigravity_hook_command("PreToolUse")
    assert pre.rstrip().endswith("""echo '{"decision":"allow"}'""")
    stop = hooks_install._antigravity_hook_command("Stop")
    assert stop.rstrip().endswith("echo '{}'")
    assert "decision" not in stop.rsplit("echo", 1)[-1]


def test_install_antigravity_hooks_writes_expected_shape(tmp_path):
    result = hooks_install.install_antigravity_hooks(tmp_path)
    assert result == "changed"
    hooks_path = tmp_path / ".agents" / "hooks.json"
    data = json.loads(hooks_path.read_text())
    entry = data["switchboard"]

    # Grouped events (PreToolUse/PostToolUse) need a matcher + hooks wrapper.
    assert entry["PreToolUse"] == [
        {
            "matcher": "ask_question",
            "hooks": [{"type": "command", "command": hooks_install._antigravity_hook_command("PreToolUse")}],
        }
    ]
    assert entry["PostToolUse"][0]["matcher"] == "*"

    # Flat events (PreInvocation/Stop) are bare handler objects, no matcher.
    assert entry["PreInvocation"] == [{"type": "command", "command": hooks_install._antigravity_hook_command("PreInvocation")}]
    assert entry["Stop"] == [{"type": "command", "command": hooks_install._antigravity_hook_command("Stop")}]

    # PostInvocation was deliberately left unimplemented (see module docstring).
    assert "PostInvocation" not in entry


def test_install_antigravity_hooks_is_idempotent(tmp_path):
    first = hooks_install.install_antigravity_hooks(tmp_path)
    second = hooks_install.install_antigravity_hooks(tmp_path)
    assert first == "changed"
    assert second == "unchanged"


def test_install_antigravity_hooks_preserves_foreign_named_hooks(tmp_path):
    hooks_path = tmp_path / ".agents" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    foreign = {"lint-checker": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]}}
    hooks_path.write_text(json.dumps(foreign))

    result = hooks_install.install_antigravity_hooks(tmp_path)
    assert result == "changed"

    data = json.loads(hooks_path.read_text())
    assert data["lint-checker"] == foreign["lint-checker"]
    assert "switchboard" in data


def test_install_antigravity_hooks_dry_run_reports_missing_then_installed(tmp_path):
    assert hooks_install.install_antigravity_hooks(tmp_path, dry_run=True) == "missing"
    hooks_install.install_antigravity_hooks(tmp_path)
    assert hooks_install.install_antigravity_hooks(tmp_path, dry_run=True) == "installed"


def test_install_antigravity_hooks_dry_run_reports_stale_marker(tmp_path):
    hooks_path = tmp_path / ".agents" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({"switchboard": {"Stop": [{"type": "command", "command": "stale"}]}}))
    assert hooks_install.install_antigravity_hooks(tmp_path, dry_run=True) == "stale marker"


def test_hook_spec_merge_strategy_dispatches_to_antigravity_installer():
    """Confirms the generic _install_family_hooks dispatch table actually
    reaches the new installer for this family's merge_strategy string."""
    assert hooks_install._INSTALLERS[AntigravityProfile().hook_spec().merge_strategy] is hooks_install._install_merge_named_hook
