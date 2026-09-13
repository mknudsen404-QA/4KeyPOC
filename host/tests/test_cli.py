import argparse
import json
import subprocess
import sys

from switchboard import cli


def test_listen_help_lists_each_flag_once(capsys):
    parser = cli.build_parser()
    try:
        parser.parse_args(["listen", "--help"])
    except SystemExit:
        pass
    help_text = capsys.readouterr().out
    # Every flag legitimately appears once in the "usage:" synopsis and
    # once in the "options:" detail listing below it; a flag defined twice
    # (the bug this guards against) would show a second detail entry, so
    # check only the options section, past the synopsis.
    import re

    options_text = help_text.split("options:", 1)[1]
    for flag in ("--registry", "--port", "--baud", "--duration", "--sample", "--stdin", "--auto-launch",
                 "--launch-config", "--dry-run", "--no-open", "--retry", "--retry-delay"):
        occurrences = len(re.findall(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", options_text))
        assert occurrences == 1, f"{flag} appears {occurrences} times in `listen --help`'s options"


def test_root_and_listen_defaults_identical():
    parser = cli.build_parser()
    root_args = vars(parser.parse_args([]))
    listen_args = vars(parser.parse_args(["listen"]))
    for namespace in (root_args, listen_args):
        namespace.pop("func", None)
        # The subparsers action's own default (None) always wins over
        # set_defaults(command_name="listen") when no subcommand is given —
        # an argparse quirk unrelated to whether the actual flags match.
        namespace.pop("command_name", None)
    assert root_args == listen_args


def test_status_subcommand_roundtrip(tmp_path):
    registry_path = tmp_path / "registry.json"
    from switchboard.registry import Registry
    from switchboard.model import slot_record

    Registry(registry_path).save(
        {"version": 1, "slots": {"1": slot_record(
            slot=1, name="A", family="shell", cwd="/tmp", command="cat",
            terminal_title="t", now=1_000_000,
        )}}
    )
    bridge_path = str(cli.HOST_DIR / "switchboard_bridge.py")
    result = subprocess.run(
        [sys.executable, bridge_path, "--registry", str(registry_path), "status", "--slot", "1", "--status", "done"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    updated = Registry(registry_path).load()["slots"]["1"]
    assert updated["status"] == "done"


def test_root_registry_flag_survives_into_subcommand(tmp_path):
    """A regression guard for the classic argparse subparser-default-
    stomps-root-value bug: subcommand parsers redeclare --registry (so it
    can also be given after the subcommand) with default=SUPPRESS, so when
    it's only given at the root level it isn't clobbered."""
    parser = cli.build_parser()
    registry_path = tmp_path / "registry.json"
    args = parser.parse_args(["--registry", str(registry_path), "slots"])
    assert args.registry == registry_path


def test_config_subcommand_writes_agents_json(tmp_path, monkeypatch, capsys):
    agents_config = tmp_path / "agents.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()

    monkeypatch.setattr(
        sys, "argv",
        ["bridge.py", "config", "--agents-config", str(agents_config), "--slot", "1", "--name", "TestAgent",
         "--family", "claude", "--command", "claude", "--effort", "high"],
    )
    parser = cli.build_parser()
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
                "agents": [{"slot": 1, "name": "A", "family": "shell", "command": "cat", "effort": "high"}],
            }
        )
    )

    seen_args = []
    real_launch_agent = cli.launch_agent

    def spy_launch_agent(child):
        seen_args.append(child)
        return real_launch_agent(child)

    monkeypatch.setattr(cli, "launch_agent", spy_launch_agent)

    args = argparse.Namespace(registry=registry_path, config=str(config_path), dry_run=True, no_open=False)
    result = cli.launch_all(args)
    assert result == 0
    assert len(seen_args) == 1
    assert seen_args[0].cwd == str(tmp_path / "Documents")
    assert seen_args[0].effort == "high"
