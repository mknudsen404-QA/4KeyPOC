import argparse
import json
import sys

import switchboard_bridge as sb


def test_cwd_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    (tmp_path / "slot-specific").mkdir()
    (tmp_path / "default-specific").mkdir()

    # slot cwd wins over defaults.cwd and ~/Documents
    agent = {"slot": 1, "cwd": str(tmp_path / "slot-specific")}
    config = {"defaults": {"cwd": str(tmp_path / "default-specific")}}
    assert sb.resolve_agent_cwd(agent, config) == str(tmp_path / "slot-specific")

    # defaults.cwd wins over ~/Documents when slot has no cwd
    agent = {"slot": 1}
    assert sb.resolve_agent_cwd(agent, config) == str(tmp_path / "default-specific")

    # ~/Documents is the final fallback
    agent = {"slot": 1}
    config = {}
    assert sb.resolve_agent_cwd(agent, config) == str(tmp_path / "Documents")


def test_missing_cwd_under_documents_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "new-project"
    assert not target.exists()
    resolved = sb._validate_or_create_cwd(str(target))
    assert resolved == str(target)
    assert target.exists()


def test_missing_cwd_elsewhere_fails_with_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "elsewhere" / "missing"
    try:
        sb._validate_or_create_cwd(str(target))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "does not exist" in str(exc)
    assert not target.exists()


def test_config_subcommand_writes_agents_json(tmp_path, monkeypatch, capsys):
    agents_config = tmp_path / "agents.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge.py",
            "config",
            "--agents-config",
            str(agents_config),
            "--slot",
            "1",
            "--name",
            "TestAgent",
            "--family",
            "claude",
            "--command",
            "claude",
            "--effort",
            "high",
        ],
    )
    parser = sb.build_parser()
    args = parser.parse_args()
    result = args.func(args)
    assert result == 0

    written = json.loads(agents_config.read_text())
    agent = next(a for a in written["agents"] if a["slot"] == 1)
    assert agent["name"] == "TestAgent"
    assert agent["family"] == "claude"
    assert agent["command"] == "claude"
    assert agent["effort"] == "high"

    captured = capsys.readouterr()
    assert "next launch" in captured.out


def test_launch_all_uses_defaults_cwd_and_effort(tmp_path, monkeypatch, registry_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {"cwd": "~/Documents"},
                "agents": [
                    {"slot": 1, "name": "A", "family": "shell", "command": "cat", "effort": "high"},
                ],
            }
        )
    )

    seen_args = []
    real_launch_agent = sb.launch_agent

    def spy_launch_agent(child):
        seen_args.append(child)
        return real_launch_agent(child)

    monkeypatch.setattr(sb, "launch_agent", spy_launch_agent)

    args = argparse.Namespace(
        registry=registry_path,
        config=str(config_path),
        dry_run=True,
        no_open=False,
    )
    result = sb.launch_all(args)
    assert result == 0
    assert len(seen_args) == 1
    assert seen_args[0].cwd == str(tmp_path / "Documents")
    assert seen_args[0].effort == "high"
