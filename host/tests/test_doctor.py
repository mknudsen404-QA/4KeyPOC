import argparse
import json
import subprocess
from pathlib import Path

from switchboard import doctor
from switchboard.liveness import FakeProber
from switchboard.model import Liveness
from switchboard.registry import Registry


def fake_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_serial_port_ok(monkeypatch):
    monkeypatch.setattr(doctor, "SerialDevice", lambda port, baud: type("D", (), {"close": lambda self: None})())
    check = doctor.check_serial_port("/dev/cu.fake")
    assert check.level == "ok"


def test_check_serial_port_none_found(monkeypatch):
    monkeypatch.setattr(doctor, "find_default_port", lambda: None)
    check = doctor.check_serial_port(None)
    assert check.level == "warn"
    assert "unplugged" in check.detail


def test_check_serial_port_busy(monkeypatch):
    def raise_busy(port, baud):
        raise RuntimeError(f"{port} is already open by another process")

    monkeypatch.setattr(doctor, "SerialDevice", raise_busy)
    check = doctor.check_serial_port("/dev/cu.fake")
    assert check.level == "warn"
    assert "another bridge" in check.detail


def test_check_serial_port_ebusy_from_open_itself(monkeypatch):
    """os.open() raises EBUSY directly (not RuntimeError) when another
    process already holds TIOCEXCL — this is the common real-world path,
    confirmed manually against the live LaunchAgent bridge."""
    import errno

    def raise_ebusy(port, baud):
        raise OSError(errno.EBUSY, "Resource busy")

    monkeypatch.setattr(doctor, "SerialDevice", raise_ebusy)
    check = doctor.check_serial_port("/dev/cu.fake")
    assert check.level == "warn"
    assert "another bridge" in check.detail


def test_check_serial_port_other_oserror(monkeypatch):
    def raise_oserror(port, baud):
        raise OSError("no such device")

    monkeypatch.setattr(doctor, "SerialDevice", raise_oserror)
    check = doctor.check_serial_port("/dev/cu.fake")
    assert check.level == "fail"


class FakeLineDevice:
    """Fake SerialDevice for check_firmware_identity: yields a fixed set
    of lines instead of actually reading a port."""

    def __init__(self, lines):
        self._lines = lines

    def lines(self, duration=None):
        return iter(self._lines)

    def close(self):
        pass


def test_check_firmware_identity_no_port(monkeypatch):
    monkeypatch.setattr(doctor, "find_default_port", lambda: None)
    check = doctor.check_firmware_identity(None)
    assert check.level == "warn"
    assert "unplugged" in check.detail


def test_check_firmware_identity_switchbd_volume_mounted(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "Path", lambda p: tmp_path)
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "fail"
    assert "msc_cdc_spike" in check.detail


def test_check_firmware_identity_neokey_boot_line_ok(monkeypatch):
    monkeypatch.setattr(doctor, "Path", lambda p: Path("/nonexistent"))
    line = json.dumps({"event": "boot", "stage": "start", "firmware": "neokey", "build": "Sep 14 2026 00:00:00"})
    monkeypatch.setattr(doctor, "SerialDevice", lambda port, baud: FakeLineDevice([line]))
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "ok"
    assert "neokey" in check.detail
    assert "Sep 14 2026 00:00:00" in check.detail


def test_check_firmware_identity_wrong_firmware_boot_line_fails(monkeypatch):
    monkeypatch.setattr(doctor, "Path", lambda p: Path("/nonexistent"))
    line = json.dumps({"event": "boot", "stage": "ping", "firmware": "msc_cdc_spike", "build": "x"})
    monkeypatch.setattr(doctor, "SerialDevice", lambda port, baud: FakeLineDevice([line]))
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "fail"
    assert "msc_cdc_spike" in check.detail


def test_check_firmware_identity_no_boot_line_warns_not_fails(monkeypatch):
    monkeypatch.setattr(doctor, "Path", lambda p: Path("/nonexistent"))
    monkeypatch.setattr(doctor, "SerialDevice", lambda port, baud: FakeLineDevice([]))
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "warn"
    assert "no boot line" in check.detail


def test_check_firmware_identity_ignores_non_boot_lines(monkeypatch):
    monkeypatch.setattr(doctor, "Path", lambda p: Path("/nonexistent"))
    lines = [
        "log noise, not json",
        json.dumps({"event": "agent.select", "slot": 1}),
        json.dumps({"event": "boot", "stage": "ready", "firmware": "neokey", "attempts": 0}),
    ]
    monkeypatch.setattr(doctor, "SerialDevice", lambda port, baud: FakeLineDevice(lines))
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "ok"


def test_check_firmware_identity_port_open_failure_warns(monkeypatch):
    monkeypatch.setattr(doctor, "Path", lambda p: Path("/nonexistent"))

    def raise_oserror(port, baud):
        raise OSError("no such device")

    monkeypatch.setattr(doctor, "SerialDevice", raise_oserror)
    check = doctor.check_firmware_identity("/dev/cu.fake")
    assert check.level == "warn"


def test_check_hook_port_listening_and_loaded(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0

    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **k: FakeSocket())
    check = doctor.check_hook_port(launchagent_loaded=True)
    assert check.level == "ok"


def test_check_hook_port_listening_but_not_our_launchagent(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0

    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **k: FakeSocket())
    check = doctor.check_hook_port(launchagent_loaded=False)
    assert check.level == "warn"
    assert "something else" in check.detail


def test_check_hook_port_nothing_listening(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 61  # ECONNREFUSED

    monkeypatch.setattr(doctor.socket, "socket", lambda *a, **k: FakeSocket())
    check = doctor.check_hook_port(launchagent_loaded=False)
    assert check.level == "warn"


def test_check_claude_hooks_maps_dry_run_statuses(monkeypatch):
    for status, expected_level in doctor._HOOK_DRY_RUN_LEVELS.items():
        monkeypatch.setattr(doctor, "install_claude_hooks", lambda dry_run: status)
        assert doctor.check_claude_hooks().level == expected_level


def test_check_codex_hooks_maps_dry_run_statuses(monkeypatch):
    for status, expected_level in doctor._HOOK_DRY_RUN_LEVELS.items():
        monkeypatch.setattr(doctor, "install_codex_hooks", lambda dry_run: status)
        assert doctor.check_codex_hooks().level == expected_level


def test_check_terminal_automation_ok(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: fake_result(0, "2\n"))
    check = doctor.check_terminal_automation()
    assert check.level == "ok"


def test_check_terminal_automation_permission_denied(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: fake_result(1, "", "...-1743..."))
    check = doctor.check_terminal_automation()
    assert check.level == "fail"


def test_check_terminal_automation_other_failure(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: fake_result(1, "", "some other error"))
    check = doctor.check_terminal_automation()
    assert check.level == "warn"


def test_check_accessibility_pyobjc_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "Quartz":
            raise ImportError("no Quartz")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    check = doctor.check_accessibility()
    assert check.level == "warn"
    assert "pyobjc" in check.detail


def test_check_registry_all_alive(registry_path):
    from switchboard.model import slot_record

    Registry(registry_path).save(
        {"version": 1, "slots": {"1": slot_record(
            slot=1, name="A", family="shell", cwd="/tmp", command="cat", terminal_title="t", now=1,
        )}}
    )
    prober = FakeProber(default=Liveness.ALIVE)
    check = doctor.check_registry(registry_path, prober=prober)
    assert check.level == "ok"


def test_check_registry_some_not_alive(registry_path):
    from switchboard.model import slot_record

    Registry(registry_path).save(
        {"version": 1, "slots": {"1": slot_record(
            slot=1, name="A", family="shell", cwd="/tmp", command="cat", terminal_title="t", now=1,
        )}}
    )
    prober = FakeProber(default=Liveness.DEAD)
    check = doctor.check_registry(registry_path, prober=prober)
    assert check.level == "warn"
    assert "1" in check.detail


def test_check_registry_empty(registry_path):
    Registry(registry_path).save({"version": 1, "slots": {}})
    check = doctor.check_registry(registry_path, prober=FakeProber())
    assert check.level == "ok"


def test_check_agents_config_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"agents": [{"slot": 1, "cwd": "~/Documents"}]}))
    check = doctor.check_agents_config(config_path)
    assert check.level == "ok"


def test_check_agents_config_missing_cwd_outside_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"agents": [{"slot": 1, "cwd": "/nonexistent/path/xyz"}]}))
    check = doctor.check_agents_config(config_path)
    assert check.level == "fail"


def test_check_launchagent_loaded(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: fake_result(0))
    assert doctor.check_launchagent().level == "ok"


def test_check_launchagent_not_loaded(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: fake_result(1))
    assert doctor.check_launchagent().level == "warn"


def test_doctor_command_exit_code_matrix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        doctor, "run_checks",
        lambda **k: [doctor.DoctorCheck("a", "ok"), doctor.DoctorCheck("b", "warn")],
    )
    args = argparse.Namespace(port=None, registry=tmp_path / "r.json", agents_config=tmp_path / "a.json", json=False)
    assert doctor.doctor_command(args) == 0

    monkeypatch.setattr(
        doctor, "run_checks",
        lambda **k: [doctor.DoctorCheck("a", "ok"), doctor.DoctorCheck("b", "fail")],
    )
    assert doctor.doctor_command(args) == 1


def test_doctor_command_json_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda **k: [doctor.DoctorCheck("a", "ok", "detail")])
    args = argparse.Namespace(port=None, registry=tmp_path / "r.json", agents_config=tmp_path / "a.json", json=True)
    doctor.doctor_command(args)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == [{"name": "a", "level": "ok", "detail": "detail"}]
