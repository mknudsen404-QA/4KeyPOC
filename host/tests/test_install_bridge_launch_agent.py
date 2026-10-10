import argparse

from install_bridge_launch_agent import build_plist


def test_update_app_minimal_path_keeps_cli_install_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    args = argparse.Namespace(retry_delay=2.0, port=None, no_open=False, close_dead_tabs=False)
    paths = build_plist(args)["EnvironmentVariables"]["PATH"].split(":")
    assert str(tmp_path / ".local/bin") in paths
    assert "/opt/homebrew/bin" in paths
    assert "/usr/local/bin" in paths


def test_launch_agent_preserves_custom_path_precedence_without_duplicates(monkeypatch):
    monkeypatch.setenv("PATH", "/custom/bin:/opt/homebrew/bin:/usr/bin")
    args = argparse.Namespace(retry_delay=2.0, port=None, no_open=False, close_dead_tabs=False)
    paths = build_plist(args)["EnvironmentVariables"]["PATH"].split(":")
    assert paths[:3] == ["/custom/bin", "/opt/homebrew/bin", "/usr/bin"]
    assert len(paths) == len(set(paths))
