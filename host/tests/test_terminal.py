from switchboard.terminal import FakeTerminal


def test_terminal_fake_records_calls():
    terminal = FakeTerminal()
    tty1 = terminal.open("exec claude")
    tty2 = terminal.open("exec codex")
    assert tty1 == "/dev/ttysFAKE1"
    assert tty2 == "/dev/ttysFAKE2"
    assert terminal.opened == ["exec claude", "exec codex"]

    assert terminal.focus(tty1) is True
    assert terminal.focused == [tty1]

    assert terminal.terminal_pid() == 1234

    terminal.post_key(1234, 49, True)
    terminal.post_key(1234, 49, False)
    assert terminal.posted == [(1234, 49, True), (1234, 49, False)]

    assert terminal.close(tty1) is True
    assert terminal.closed == [tty1]


def test_terminal_fake_frontmost_tty_tracks_focus():
    terminal = FakeTerminal()
    assert terminal.frontmost_tty() is None

    terminal.focus("/dev/ttysFAKE1")
    assert terminal.frontmost_tty() == "/dev/ttysFAKE1"

    terminal.frontmost = None  # simulate focus not having settled yet
    assert terminal.frontmost_tty() is None
