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
                 "--launch-config", "--dry-run", "--no-open", "--close-dead-tabs", "--retry", "--retry-delay",
                 "--trace-verbose"):
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
    assert written["settings_version"] == 2
    agent = next(a for a in written["slots"] if a["slot"] == 1)
    assert agent["name"] == "TestAgent"
    assert agent["family"] == "claude"
    assert agent["command"] == "claude"
    assert agent["effort"] == "high"

    captured = capsys.readouterr()
    assert "next launch" in captured.out


def test_config_subcommand_cwd_flag_writes_v2_document(tmp_path, monkeypatch):
    """CLI parity: every `config` flag ends up going through
    SlotSettingsService — this covers --cwd specifically, since the flag
    above already covers --name/--family/--command/--effort."""
    agents_config = tmp_path / "agents.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = tmp_path / "Documents" / "project"
    project_dir.mkdir(parents=True)

    monkeypatch.setattr(
        sys, "argv",
        ["bridge.py", "config", "--agents-config", str(agents_config), "--slot", "2", "--cwd", str(project_dir)],
    )
    parser = cli.build_parser()
    args = parser.parse_args()
    assert args.func(args) == 0

    written = json.loads(agents_config.read_text())
    agent = next(a for a in written["slots"] if a["slot"] == 2)
    assert agent["cwd"] == str(project_dir)


def test_config_subcommand_default_cwd_flag_writes_v2_document(tmp_path, monkeypatch):
    agents_config = tmp_path / "agents.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()

    monkeypatch.setattr(
        sys, "argv",
        ["bridge.py", "config", "--agents-config", str(agents_config), "--default-cwd", "~/Documents"],
    )
    parser = cli.build_parser()
    args = parser.parse_args()
    assert args.func(args) == 0

    written = json.loads(agents_config.read_text())
    assert written["settings_version"] == 2
    assert written["defaults"]["cwd"] == str(tmp_path / "Documents")


def test_config_subcommand_show_flag_prints_v2_document(tmp_path, monkeypatch, capsys):
    agents_config = tmp_path / "agents.json"
    agents_config.write_text(json.dumps({"agents": [{"slot": 1, "name": "A", "command": "cat"}]}))
    monkeypatch.setenv("HOME", str(tmp_path))

    monkeypatch.setattr(sys, "argv", ["bridge.py", "config", "--agents-config", str(agents_config), "--show"])
    parser = cli.build_parser()
    args = parser.parse_args()
    assert args.func(args) == 0

    shown = json.loads(capsys.readouterr().out)
    assert shown["settings_version"] == 2
    assert shown["slots"] == [{"slot": 1, "name": "A", "command": "cat"}]
    # --show never writes: the on-disk file is still v1 until a real change is saved.
    assert "agents" in json.loads(agents_config.read_text())


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


# --- diagnostics: noise suppression and log rotation ------------------------


def test_repeater_prints_first_occurrence(capsys):
    repeater = cli._Repeater(every=100)
    repeater.print("waiting")
    assert capsys.readouterr().out.strip() == "waiting"


def test_repeater_collapses_identical_repeats(capsys):
    repeater = cli._Repeater(every=10)
    for _ in range(25):
        repeater.print("waiting")
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines == ["waiting", "waiting (still happening, x10)", "waiting (still happening, x20)"]


def test_repeater_summarizes_before_a_different_message(capsys):
    repeater = cli._Repeater(every=100)
    for _ in range(5):
        repeater.print("waiting")
    repeater.print("connected")
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines == ["waiting", "(previous message repeated 5 times)", "connected"]


def test_repeater_no_summary_after_single_occurrence(capsys):
    repeater = cli._Repeater(every=100)
    repeater.print("waiting")
    repeater.print("connected")
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines == ["waiting", "connected"]


def test_rotate_bridge_out_log_leaves_small_file_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path))
    log_path = tmp_path / "bridge.out.log"
    log_path.write_text("small")
    cli._rotate_bridge_out_log(max_bytes=1000)
    assert log_path.read_text() == "small"
    assert not (tmp_path / "bridge.out.log.1").exists()


def test_rotate_bridge_out_log_renames_when_over_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path))
    log_path = tmp_path / "bridge.out.log"
    log_path.write_text("x" * 2000)
    cli._rotate_bridge_out_log(max_bytes=1000)
    assert not log_path.exists()
    assert (tmp_path / "bridge.out.log.1").read_text() == "x" * 2000


def test_rotate_bridge_out_log_overwrites_old_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path))
    (tmp_path / "bridge.out.log.1").write_text("stale")
    log_path = tmp_path / "bridge.out.log"
    log_path.write_text("x" * 2000)
    cli._rotate_bridge_out_log(max_bytes=1000)
    assert (tmp_path / "bridge.out.log.1").read_text() == "x" * 2000


def test_support_bundle_parse_duration_accepts_units():
    from datetime import timedelta

    assert cli._parse_duration("30m") == timedelta(minutes=30)
    assert cli._parse_duration("2h") == timedelta(hours=2)
    assert cli._parse_duration("3d") == timedelta(days=3)
    assert cli._parse_duration("45s") == timedelta(seconds=45)


def test_support_bundle_parse_duration_rejects_garbage():
    import pytest

    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_duration("not-a-duration")


def test_support_bundle_command_writes_to_requested_out_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path / "logs"))
    out_path = tmp_path / "custom.zip"
    args = cli.build_parser().parse_args(
        [
            "support-bundle", "--out", str(out_path),
            "--registry", str(tmp_path / "registry.json"),
            "--agents-config", str(tmp_path / "agents.json"),
        ]
    )
    result = cli.support_bundle_command(args)
    assert result == 0
    assert out_path.exists()
    assert capsys.readouterr().out.strip() == str(out_path)


def test_support_bundle_command_defaults_out_to_desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "Desktop").mkdir()
    args = cli.build_parser().parse_args(
        ["support-bundle", "--registry", str(tmp_path / "registry.json"), "--agents-config", str(tmp_path / "agents.json")]
    )
    cli.support_bundle_command(args)
    produced = list((tmp_path / "Desktop").glob("switchboard-support-*.zip"))
    assert len(produced) == 1


def test_rotate_bridge_out_log_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path))
    cli._rotate_bridge_out_log()  # must not raise when nothing has ever been logged yet


def test_listen_announces_a_stuck_bad_port_once_not_per_retry(tmp_path, monkeypatch, capsys):
    """Regression: `--port` pointing at something that never opens used to
    reprint "Listening on ..." on every retry-delay tick forever, same
    spam shape as the "No USB serial port found" line this stage fixed."""
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(tmp_path))

    def raising_serial_device(port, baud):
        raise OSError("no such device")

    monkeypatch.setattr(cli, "SerialDevice", raising_serial_device)

    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise StopIteration  # escape listen()'s infinite retry loop

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    args = argparse.Namespace(
        sample=False, stdin=False, port="/dev/nonexistent", retry=True, retry_delay=0.01, baud=115200,
        trace_verbose=False,
    )
    try:
        cli.listen(args)
    except StopIteration:
        pass

    out = capsys.readouterr().out
    assert out.count("Listening on /dev/nonexistent") == 1
    # "Could not open" repeats identically every retry, so the repeater
    # collapses it too (same shape as "No USB serial port found").
    assert out.count("Could not open") == 1
