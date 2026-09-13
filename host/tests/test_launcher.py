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
    }


def test_build_launch_overrides_win_over_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(launcher, "resolve_command", lambda cmd: f"/usr/bin/{cmd}")

    plan = launcher.build_launch(3, None, overrides={"name": "Custom", "family": "shell", "command": "cat"})
    assert plan.record_fields["name"] == "Custom"
    assert plan.record_fields["family"] == "shell"
    assert plan.record_fields["command"] == "/usr/bin/cat"
