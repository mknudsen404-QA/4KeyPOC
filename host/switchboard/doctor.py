"""`doctor` — probe the bridge's operating environment and report health.
Every check probes something real (a socket, a subprocess, a file) rather
than guessing from config alone. Each check is one DoctorCheck; the CLI
prints one `[level] name: detail` line per check and exits 1 if any
check's level is "fail".
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from switchboard.device import SerialDevice, find_default_port
from switchboard.hooks_install import install_claude_hooks, install_codex_hooks
from switchboard.hooks_server import HOOK_HOST, HOOK_PORT
from switchboard.launcher import load_agents_config, resolve_agent_cwd
from switchboard.liveness import ProcessProber
from switchboard.model import Liveness
from switchboard.registry import Registry

LAUNCH_AGENT_LABEL = "com.switchboard.bridge"

# Levels, worst to best — used only to decide the process exit code.
_FAIL_LEVELS = {"fail"}


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str = ""

    def line(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"[{self.level}] {self.name}{suffix}"


def check_serial_port(port: str | None) -> DoctorCheck:
    resolved = port or find_default_port()
    if not resolved:
        return DoctorCheck("serial port", "warn", "no USB serial port found (board unplugged?)")
    try:
        device = SerialDevice(resolved, 115200)
    except RuntimeError as exc:
        # SerialDevice raises RuntimeError when its own TIOCEXCL ioctl call
        # fails after a successful open.
        return DoctorCheck("serial port", "warn", f"another bridge is running (LaunchAgent?) — {exc}")
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            # More common in practice than the RuntimeError path above: a
            # port already held exclusively (TIOCEXCL) by another process
            # rejects the os.open() call itself with EBUSY, before
            # SerialDevice ever gets to its own ioctl call.
            return DoctorCheck(
                "serial port", "warn", f"another bridge is running (LaunchAgent?) — {resolved} is busy"
            )
        return DoctorCheck("serial port", "fail", f"{resolved}: {exc}")
    device.close()
    return DoctorCheck("serial port", "ok", resolved)


def check_hook_port(launchagent_loaded: bool) -> DoctorCheck:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        result = sock.connect_ex((HOOK_HOST, HOOK_PORT))
    if result != 0:
        return DoctorCheck("hook port", "warn", f"nothing listening on {HOOK_HOST}:{HOOK_PORT} (bridge not running?)")
    if launchagent_loaded:
        return DoctorCheck("hook port", "ok", f"a bridge is listening on {HOOK_PORT}")
    return DoctorCheck("hook port", "warn", f"something else owns {HOOK_PORT} (LaunchAgent isn't loaded)")


_HOOK_DRY_RUN_LEVELS = {
    "installed": "ok",
    "unchanged": "ok",
    "missing": "warn",
    "stale marker": "warn",
    "failed": "fail",
}


def check_claude_hooks() -> DoctorCheck:
    status = install_claude_hooks(dry_run=True)
    return DoctorCheck("Claude Code hooks", _HOOK_DRY_RUN_LEVELS.get(status, "warn"), status)


def check_codex_hooks() -> DoctorCheck:
    status = install_codex_hooks(dry_run=True)
    return DoctorCheck("Codex hooks", _HOOK_DRY_RUN_LEVELS.get(status, "warn"), status)


def check_terminal_automation() -> DoctorCheck:
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Terminal" to count windows'],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return DoctorCheck("Terminal automation", "ok", f"{result.stdout.strip()} window(s)")
    if "-1743" in result.stderr:
        return DoctorCheck(
            "Terminal automation", "fail",
            "permission denied — grant Automation access for Terminal in System Settings",
        )
    return DoctorCheck("Terminal automation", "warn", result.stderr.strip() or "osascript failed")


def check_accessibility() -> DoctorCheck:
    try:
        from Quartz import AXIsProcessTrusted  # type: ignore[import]
    except ImportError:
        return DoctorCheck("Accessibility", "warn", "pyobjc missing, voice PTT unavailable")
    if AXIsProcessTrusted():
        return DoctorCheck("Accessibility", "ok", "process is trusted")
    return DoctorCheck("Accessibility", "warn", "not trusted — grant Accessibility access for voice PTT")


def check_registry(registry_path: Path, *, prober: ProcessProber | None = None) -> DoctorCheck:
    slots = Registry(registry_path).load().get("slots", {})
    if not slots:
        return DoctorCheck("registry", "ok", "no slots registered")
    prober = prober or ProcessProber()
    not_alive = [
        f"{slot_key} ({liveness.value})"
        for slot_key, record in slots.items()
        for liveness in [prober.probe(record)]
        if liveness is not Liveness.ALIVE
    ]
    if not not_alive:
        return DoctorCheck("registry", "ok", f"{len(slots)} slot(s), all alive")
    return DoctorCheck("registry", "warn", f"not alive: {', '.join(not_alive)}")


def check_agents_config(agents_config_path: Path) -> DoctorCheck:
    config = load_agents_config(agents_config_path)
    documents_dir = Path.home() / "Documents"
    problems = []
    for agent in config.get("agents", []):
        resolved = Path(resolve_agent_cwd(agent, config))
        if resolved.exists():
            continue
        if resolved == documents_dir or documents_dir in resolved.parents:
            continue  # would be created on next launch
        problems.append(f"slot {agent.get('slot')}: {resolved} does not exist")
    if not problems:
        return DoctorCheck("agents.json", "ok", "every slot's cwd exists or is creatable")
    return DoctorCheck("agents.json", "fail", "; ".join(problems))


def check_launchagent() -> DoctorCheck:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return DoctorCheck("LaunchAgent", "ok", "loaded")
    return DoctorCheck("LaunchAgent", "warn", "not loaded — run setup.sh or install_bridge_launch_agent.py")


def run_checks(*, port: str | None, registry_path: Path, agents_config_path: Path) -> list[DoctorCheck]:
    launchagent = check_launchagent()
    return [
        check_serial_port(port),
        check_hook_port(launchagent.level == "ok"),
        check_claude_hooks(),
        check_codex_hooks(),
        check_terminal_automation(),
        check_accessibility(),
        check_registry(registry_path),
        check_agents_config(agents_config_path),
        launchagent,
    ]


def doctor_command(args) -> int:
    checks = run_checks(port=args.port, registry_path=args.registry, agents_config_path=args.agents_config)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps([{"name": c.name, "level": c.level, "detail": c.detail} for c in checks]))
    else:
        for check in checks:
            print(check.line())
    return 1 if any(c.level in _FAIL_LEVELS for c in checks) else 0
