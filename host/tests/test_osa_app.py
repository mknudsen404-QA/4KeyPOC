import sys

import pytest

from switchboard.osa_app import _applescript_string, build_script, compile_app


def test_applescript_string_escapes_quotes_and_backslashes():
    # The one thing worth getting exactly right here: a path or command
    # containing a literal double-quote or backslash must not break out
    # of the AppleScript string literal it's embedded in.
    assert _applescript_string('say "hi"') == '"say \\"hi\\""'
    assert _applescript_string(r"C:\path") == '"C:\\\\path"'
    assert _applescript_string("plain") == '"plain"'


def test_build_script_wraps_command_in_try_on_error():
    script = build_script("echo hi", notification_title="Switchboard")
    assert 'do shell script "echo hi"' in script
    assert "on error errMsg" in script
    assert 'display notification errMsg with title "Switchboard"' in script
    # No success_message given: must not claim a success notification.
    assert script.count("display notification") == 1


def test_build_script_adds_success_notification_when_requested():
    script = build_script("echo hi", notification_title="Switchboard", success_message="All good")
    assert 'display notification "All good" with title "Switchboard"' in script
    assert script.count("display notification") == 2


def test_build_script_escapes_a_command_containing_quotes():
    script = build_script('echo "hello"', notification_title="Switchboard")
    assert 'do shell script "echo \\"hello\\""' in script


@pytest.mark.skipif(sys.platform != "darwin", reason="compile_app shells out to osacompile, macOS-only")
def test_compile_app_replaces_the_blank_default_icon(tmp_path):
    # osacompile's own default icon is a real, non-empty file (the blank
    # Script Editor applet icon) — this is the one concrete thing worth
    # locking in: that passing icon_path actually overwrites it, not
    # just that compile_app doesn't crash.
    icon_src = tmp_path / "fake.icns"
    icon_src.write_bytes(b"not a real icns, just needs to be distinguishable bytes")
    app_path = tmp_path / "Test.app"

    compile_app(build_script("true", notification_title="Test"), app_path, icon_path=icon_src)

    installed_icon = app_path / "Contents" / "Resources" / "applet.icns"
    assert installed_icon.read_bytes() == icon_src.read_bytes()


@pytest.mark.skipif(sys.platform != "darwin", reason="compile_app shells out to osacompile, macOS-only")
def test_compile_app_without_icon_path_keeps_the_default(tmp_path):
    app_path = tmp_path / "Test.app"
    compile_app(build_script("true", notification_title="Test"), app_path)
    installed_icon = app_path / "Contents" / "Resources" / "applet.icns"
    assert installed_icon.exists()
    assert installed_icon.read_bytes() != b"not a real icns, just needs to be distinguishable bytes"
