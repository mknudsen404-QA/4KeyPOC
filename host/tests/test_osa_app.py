from switchboard.osa_app import _applescript_string, build_script


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
