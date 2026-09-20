import json
import zipfile
from datetime import timedelta
from pathlib import Path

from switchboard import doctor as doctor_module
from switchboard.hooks_install import HOOK_MARKER
from switchboard.support_bundle import build_bundle


def _read_member(zip_path: Path, name: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(f"switchboard-support/{name}").decode("utf-8")


def _members(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return {n.split("switchboard-support/", 1)[1] for n in zf.namelist()}


def _fake_doctor_checks(*, port, registry_path, agents_config_path):
    return [doctor_module.DoctorCheck("Fake check", "ok", "stubbed — see test_doctor.py for real check coverage")]


def stub_doctor(monkeypatch) -> None:
    """doctor.run_checks() probes real OS state — osascript, launchctl,
    Terminal automation permission — which is slow and environment-
    dependent; unbounded once, it hung a CI runner for the length of the
    whole job (see doctor.check_terminal_automation's fix). What these
    tests care about is what build_bundle does with doctor's output
    (renders doctor.txt/doctor.json), not doctor's checks themselves.
    """
    monkeypatch.setattr(doctor_module, "run_checks", _fake_doctor_checks)


def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    stub_doctor(monkeypatch)


def test_build_bundle_succeeds_with_nothing_present(tmp_path, monkeypatch):
    """No bridge running, no board connected, no prior logs — the exact
    state a user in trouble runs this from. Whatever this test machine
    happens to have installed, the bundle must still come out complete."""
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    out = tmp_path / "out.zip"

    result = build_bundle(
        out=out,
        registry_path=tmp_path / "registry.json",
        agents_config_path=tmp_path / "agents.json",
        log_directory=log_directory,
    )

    assert result == out
    members = _members(out)
    assert "bridge.out.log.missing" in members
    assert "agents.json.missing" in members
    assert "agent_registry.json.missing" in members
    assert "doctor.txt" in members
    assert "doctor.json" in members
    assert "versions.txt" in members
    assert "hooks.claude.json" in members
    assert "hooks.codex.json" in members
    assert "manifest.json" in members

    versions = _read_member(out, "versions.txt")
    assert any(line.startswith("claude ") for line in versions.splitlines())
    assert any(line.startswith("codex ") for line in versions.splitlines())

    manifest = json.loads(_read_member(out, "manifest.json"))
    assert manifest["bundle_version"] == 1
    # manifest.json lists every member written before it, i.e. everything
    # except itself.
    assert set(manifest["members"]) == members - {"manifest.json"}


def test_build_bundle_succeeds_when_claude_and_codex_are_not_on_path(tmp_path, monkeypatch):
    """Simulates claude/codex missing without touching PATH globally —
    doctor's own checks (launchctl, etc.) still need a real PATH to run."""
    import subprocess as real_subprocess

    from switchboard import support_bundle

    real_run = real_subprocess.run

    def fake_run(args, *a, **k):
        if args and args[0] in ("claude", "codex"):
            raise FileNotFoundError(args[0])
        return real_run(args, *a, **k)

    monkeypatch.setattr(support_bundle.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    stub_doctor(monkeypatch)

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=tmp_path / "logs",
    )

    versions = _read_member(out, "versions.txt")
    assert "claude not found" in versions
    assert "codex not found" in versions


def test_build_bundle_includes_present_files(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "bridge.out.log").write_text("hello\n")
    (log_directory / "trace.jsonl").write_text('{"kind":"start","ts":"2026-01-01T00:00:00.000Z"}\n')
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"version": 1, "slots": {}}')
    agents_path = tmp_path / "agents.json"
    agents_path.write_text('{"agents": []}')

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=registry_path, agents_config_path=agents_path, log_directory=log_directory,
    )

    assert _read_member(out, "bridge.out.log") == "hello\n"
    assert _read_member(out, "agent_registry.json") == registry_path.read_text()
    assert '"kind":"start"' in _read_member(out, "trace.jsonl")


def test_build_bundle_tail_truncates_large_bridge_out_log(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "bridge.out.log").write_text("x" * 3_000_000 + "TAIL_MARKER")

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory,
    )

    contents = _read_member(out, "bridge.out.log")
    assert contents.endswith("TAIL_MARKER")
    assert len(contents) <= 2_000_000 + len("TAIL_MARKER")


def test_build_bundle_since_drops_older_trace_lines(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    old = {"kind": "board", "event": "old", "ts": "2020-01-01T00:00:00.000Z"}
    recent = {"kind": "board", "event": "recent", "ts": "2099-01-01T00:00:00.000Z"}
    (log_directory / "trace.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory, since=timedelta(days=1),
    )

    trace = _read_member(out, "trace.jsonl")
    assert '"event":"recent"' in trace
    assert '"event":"old"' not in trace


def test_build_bundle_concatenates_rotated_trace_files_oldest_first(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "trace.jsonl.2").write_text('{"kind":"board","event":"oldest"}\n')
    (log_directory / "trace.jsonl.1").write_text('{"kind":"board","event":"middle"}\n')
    (log_directory / "trace.jsonl").write_text('{"kind":"board","event":"newest"}\n')

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory,
    )

    trace = _read_member(out, "trace.jsonl")
    assert trace.index("oldest") < trace.index("middle") < trace.index("newest")


def test_build_bundle_extracts_only_our_hook_groups(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": f"curl http://{HOOK_MARKER}UserPromptSubmit"}]},
                        {"hooks": [{"type": "command", "command": "some-other-tools-hook"}]},
                    ]
                },
                "permissions": {"secret": "not ours, must never appear"},
            }
        )
    )

    out = build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=tmp_path / "logs",
    )

    hooks = json.loads(_read_member(out, "hooks.claude.json"))
    assert list(hooks.keys()) == ["UserPromptSubmit"]
    assert len(hooks["UserPromptSubmit"]) == 1
    assert HOOK_MARKER in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    dumped = _read_member(out, "hooks.claude.json")
    assert "some-other-tools-hook" not in dumped
    assert "not ours, must never appear" not in dumped


def test_build_bundle_strips_verbose_payloads_unless_included(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    record = {"kind": "hook", "slot": "1", "payload": {"prompt": "do the secret thing"}}
    (log_directory / "trace.jsonl").write_text(json.dumps(record) + "\n")

    default_out = build_bundle(
        out=tmp_path / "default.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory,
    )
    assert "do the secret thing" not in _read_member(default_out, "trace.jsonl")

    included_out = build_bundle(
        out=tmp_path / "included.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory, include_payloads=True,
    )
    assert "do the secret thing" in _read_member(included_out, "trace.jsonl")


def test_build_bundle_warns_when_stripping_payloads(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "trace.jsonl").write_text(json.dumps({"kind": "hook", "payload": {"x": 1}}) + "\n")

    warnings = []
    build_bundle(
        out=tmp_path / "out.zip", registry_path=tmp_path / "r.json", agents_config_path=tmp_path / "a.json",
        log_directory=log_directory, warn=warnings.append,
    )
    assert len(warnings) == 1
    assert "--include-payloads" in warnings[0]
