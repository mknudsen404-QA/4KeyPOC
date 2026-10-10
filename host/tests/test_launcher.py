import json
import shlex

import pytest

from switchboard import launcher


def test_cwd_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    (tmp_path / "slot-specific").mkdir()
    (tmp_path / "default-specific").mkdir()

    agent = {"slot": 1, "cwd": str(tmp_path / "slot-specific")}
    config = {"defaults": {"cwd": str(tmp_path / "default-specific")}}
    assert launcher.resolve_agent_cwd(agent, config) == str(tmp_path / "slot-specific")

    agent = {"slot": 1}
    assert launcher.resolve_agent_cwd(agent, config) == str(tmp_path / "default-specific")

    agent = {"slot": 1}
    config = {}
    assert launcher.resolve_agent_cwd(agent, config) == str(tmp_path / "Documents")


def test_missing_cwd_under_documents_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "new-project"
    assert not target.exists()
    resolved = launcher.validate_or_create_cwd(str(target))
    assert resolved == str(target)
    assert target.exists()


def test_missing_cwd_elsewhere_fails_with_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "elsewhere" / "missing"
    with pytest.raises(ValueError, match="does not exist"):
        launcher.validate_or_create_cwd(str(target))
    assert not target.exists()


def test_resolve_command_finds_known_path(monkeypatch, tmp_path):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\n")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(launcher, "KNOWN_COMMAND_PATHS", {"claude": [str(fake_claude)]})
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.resolve_command("claude") == str(fake_claude)


def test_resolve_command_missing_raises():
    with pytest.raises(FileNotFoundError):
        launcher.resolve_command("definitely-not-a-real-binary-xyz")


@pytest.mark.parametrize("family, executable", [("codex", "codex"), ("antigravity", "agy")])
def test_detection_and_launch_find_install_outside_login_agent_path(monkeypatch, tmp_path, family, executable):
    from switchboard.families import registry

    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    binary = tmp_path / "installed bin" / executable
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    profile = registry.get(family)
    monkeypatch.setattr(profile, "known_paths", (str(binary),))
    monkeypatch.setattr(launcher, "KNOWN_COMMAND_PATHS", registry.known_paths_map())
    assert profile.detect().path == str(binary)
    assert shlex.split(launcher.resolve_command(f'{executable} --model "test model"')) == [
        str(binary), "--model", "test model",
    ]


def test_launcher_build_launch_golden(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "foo").mkdir(parents=True)
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: "/usr/bin/claude")

    config = {
        "agents": [
            {"slot": 2, "name": "Foo", "family": "claude", "command": "claude", "cwd": "~/Documents/foo"},
        ]
    }
    plan = launcher.build_launch(2, config, effort="high")

    cwd = str(tmp_path / "Documents" / "foo")
    expected = (
        f"printf '\\033]0;%s\\007' {shlex.quote('Switchboard A2 Foo')}\n"
        f"cd {shlex.quote(cwd)}\n"
        "export SWITCHBOARD_SLOT=2\n"
        "exec /usr/bin/claude --effort high"
    )
    assert plan.shell_command == expected
    assert plan.record_fields == {
        "slot": 2,
        "name": "Foo",
        "family": "claude",
        "cwd": cwd,
        "command": "/usr/bin/claude",
        "terminal_title": "Switchboard A2 Foo",
        "effort": "high",
        "voice": {"provider": "claude_native", "chord": None, "mode": "hold"},
    }


def test_build_launch_does_not_write_hooks_json_for_claude(monkeypatch, tmp_path):
    """Claude/Codex's hooks are installed once, globally (install-hooks /
    setup.sh) — launching a slot must never touch a project-local file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "foo").mkdir(parents=True)
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: "/usr/bin/claude")

    config = {"agents": [{"slot": 2, "family": "claude", "command": "claude", "cwd": "~/Documents/foo"}]}
    launcher.build_launch(2, config)
    assert not (tmp_path / "Documents" / "foo" / ".agents").exists()


def test_build_launch_installs_project_local_hooks_for_antigravity(monkeypatch, tmp_path):
    """Antigravity's hooks.json is project-local (see
    families/antigravity.py) — launching a slot must (re)install it into
    that slot's own cwd, since there's no global config to install once."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "foo").mkdir(parents=True)
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: "/usr/bin/agy")

    config = {"agents": [{"slot": 2, "family": "antigravity", "command": "agy", "cwd": "~/Documents/foo"}]}
    launcher.build_launch(2, config)

    hooks_path = tmp_path / "Documents" / "foo" / ".agents" / "hooks.json"
    assert hooks_path.exists()
    assert "switchboard" in json.loads(hooks_path.read_text())


def test_build_launch_overrides_win_over_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: f"/usr/bin/{cmd}")

    plan = launcher.build_launch(3, None, overrides={"name": "Custom", "family": "shell", "command": "cat"})
    assert plan.record_fields["name"] == "Custom"
    assert plan.record_fields["family"] == "shell"
    assert plan.record_fields["command"] == "/usr/bin/cat"


def test_build_launch_infers_family_from_command_when_omitted(monkeypatch, tmp_path):
    """A slot configured with a command but no family used to silently
    default to "codex" (and get Codex's effort flags). It should now infer
    from the command's basename instead — "generic" for anything unknown."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: f"/usr/bin/{cmd}")

    config = {"agents": [{"slot": 1, "name": "Foo", "command": "gemini"}]}
    plan = launcher.build_launch(1, config)
    assert plan.record_fields["family"] == "generic"
    assert plan.record_fields["command"] == "/usr/bin/gemini"


def test_build_launch_infers_claude_family_from_known_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: f"/usr/bin/{cmd}")

    config = {"agents": [{"slot": 1, "name": "Foo", "command": "claude"}]}
    plan = launcher.build_launch(1, config)
    assert plan.record_fields["family"] == "claude"
