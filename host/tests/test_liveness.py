import subprocess

from switchboard.liveness import ProcessProber
from switchboard.model import Liveness


def make_record(slot=1, tty="/dev/ttys001", **extra):
    record = {"slot": slot, "name": "Test", "family": "shell", "status": "idle", "terminal_tty": tty}
    record.update(extra)
    return record


def fake_result(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_alive_when_agent_process_on_tty():
    prober = ProcessProber(run=lambda *a, **k: fake_result("login\nclaude\n"), exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.ALIVE


def test_dead_when_only_shell_on_tty():
    prober = ProcessProber(run=lambda *a, **k: fake_result("login\n-zsh\n"), exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.DEAD


def test_dead_when_tty_missing():
    calls = []
    prober = ProcessProber(run=lambda *a, **k: calls.append(1) or fake_result(""), exists=lambda p: False)
    assert prober.probe(make_record()) is Liveness.DEAD
    assert not calls


def test_unknown_on_timeout():
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2.0)

    prober = ProcessProber(run=raise_timeout, exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.UNKNOWN


def test_unknown_on_no_tty_record():
    prober = ProcessProber()
    assert prober.probe({"slot": 1}) is Liveness.UNKNOWN


def test_unknown_logs_timeout_reason():
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=3.0)

    logged = []
    prober = ProcessProber(run=raise_timeout, exists=lambda p: True, log=logged.append)
    prober.probe(make_record(slot=2))
    assert len(logged) == 1
    assert "slot 2" in logged[0] and "timed out" in logged[0]


def test_unknown_logs_bad_returncode_reason():
    logged = []
    prober = ProcessProber(
        run=lambda *a, **k: fake_result(returncode=2), exists=lambda p: True, log=logged.append
    )
    prober.probe(make_record(slot=3))
    assert len(logged) == 1
    assert "slot 3" in logged[0] and "exited 2" in logged[0]


def test_unknown_logs_oserror_reason():
    def raise_oserror(*a, **k):
        raise OSError("no such file")

    logged = []
    prober = ProcessProber(run=raise_oserror, exists=lambda p: True, log=logged.append)
    prober.probe(make_record(slot=4))
    assert len(logged) == 1
    assert "slot 4" in logged[0]


def test_no_log_by_default():
    """log=None (the default) must not raise even on an UNKNOWN result."""
    prober = ProcessProber(run=lambda *a, **k: fake_result(returncode=2), exists=lambda p: True)
    assert prober.probe(make_record()) is Liveness.UNKNOWN


def test_alive_and_dead_paths_never_log():
    logged = []
    alive = ProcessProber(run=lambda *a, **k: fake_result("claude\n"), exists=lambda p: True, log=logged.append)
    alive.probe(make_record())
    dead = ProcessProber(run=lambda *a, **k: fake_result("-zsh\n"), exists=lambda p: True, log=logged.append)
    dead.probe(make_record())
    assert logged == []
