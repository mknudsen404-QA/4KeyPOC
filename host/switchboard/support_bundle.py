"""Builds a support-bundle zip: everything needed to diagnose a Switchboard
bug off the user's machine, in one file, safe to hand over without reading
first. See docs/design/diagnostic-trace-and-support-bundle-spec.md
section 6.4 for the member list and the reasoning behind each one.

Every member is best-effort — a missing source file becomes a
`<name>.missing` entry describing why, never a failed bundle. This must
work with no bridge running and no board connected, since that's exactly
when a user runs it.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from switchboard import doctor
from switchboard.families import registry as family_registry
from switchboard.families.claude import NAME as CLAUDE_FAMILY
from switchboard.families.codex import NAME as CODEX_FAMILY
from switchboard.hooks_install import _group_is_ours
from switchboard.trace import log_dir

BUNDLE_ROOT = "switchboard-support"

# bridge.out.log can grow large between rotations; only the tail is
# useful for a recent bug, and the whole point is to bound the bundle.
_LOG_TAIL_BYTES = 2_000_000

_TRACE_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class _BundleWriter:
    """Wraps a ZipFile, prefixing every member with BUNDLE_ROOT and
    tracking what was actually written for manifest.json."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self._zf = zf
        self.members: list[str] = []

    def add_text(self, path: str, text: str) -> None:
        self._zf.writestr(f"{BUNDLE_ROOT}/{path}", text)
        self.members.append(path)

    def add_file(self, path: str, source: Path, *, tail_bytes: int | None = None) -> None:
        try:
            data = source.read_bytes()
        except OSError as exc:
            self.add_text(f"{path}.missing", f"{exc}\n")
            return
        if tail_bytes is not None and len(data) > tail_bytes:
            data = data[-tail_bytes:]
        self._zf.writestr(f"{BUNDLE_ROOT}/{path}", data)
        self.members.append(path)


def _parse_trace_ts(record: dict) -> datetime | None:
    ts = record.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, _TRACE_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _collect_trace_text(log_directory: Path) -> str:
    """Oldest to newest: the rotated backups, then the current file."""
    parts = []
    for suffix in (".2", ".1", ""):
        path = log_directory / f"trace.jsonl{suffix}"
        if path.exists():
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "".join(parts)


def _filter_and_redact_trace(text: str, *, since: timedelta | None, include_payloads: bool) -> tuple[str, bool]:
    """Applies --since (drop older lines) and, unless include_payloads,
    strips any `payload` field a verbose trace may carry (see trace.py's
    TraceWriter(verbose=True)) — a bundle is meant to be safe to hand over
    without reading first, and a verbose trace is the one thing that
    isn't. Returns (filtered text, whether anything was stripped).
    """
    cutoff = datetime.now(timezone.utc) - since if since is not None else None
    stripped = False
    out_lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)  # keep anything unparseable rather than silently drop it
            continue
        if cutoff is not None:
            ts = _parse_trace_ts(record)
            if ts is not None and ts < cutoff:
                continue
        if not include_payloads and "payload" in record:
            record.pop("payload")
            stripped = True
        out_lines.append(json.dumps(record, separators=(",", ":")))
    return "\n".join(out_lines) + ("\n" if out_lines else ""), stripped


def _run_version(*args: str, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "not found"
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else "unknown"


def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _versions_text() -> str:
    lines = [
        f"switchboard {_git_short_sha()}",
        f"python {sys.version.split()[0]}",
        f"macos {platform.mac_ver()[0] or 'unknown'}",
        f"claude {_run_version('claude', '--version')}",
        f"codex {_run_version('codex', '--version')}",
        "firmware unknown",  # no protocol field for this yet — see spec section 11
    ]
    return "\n".join(lines) + "\n"


def _extract_our_hooks(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {}
    ours: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        our_groups = [g for g in groups if _group_is_ours(g)]
        if our_groups:
            ours[event] = our_groups
    return ours


def build_bundle(
    *,
    out: Path,
    since: timedelta | None = None,
    registry_path: Path,
    agents_config_path: Path,
    port: str | None = None,
    log_directory: Path | None = None,
    include_payloads: bool = False,
    warn: Callable[[str], None] | None = None,
) -> Path:
    log_directory = log_directory or log_dir()
    warn = warn or (lambda message: print(message, file=sys.stderr))
    out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        writer = _BundleWriter(zf)

        raw_trace = _collect_trace_text(log_directory)
        trace_text, stripped = _filter_and_redact_trace(raw_trace, since=since, include_payloads=include_payloads)
        if stripped:
            warn("support-bundle: this trace was recorded in verbose mode; payload fields were stripped "
                 "(pass --include-payloads to keep them — they may contain prompt text).")
        writer.add_text("trace.jsonl", trace_text)

        writer.add_file("bridge.out.log", log_directory / "bridge.out.log", tail_bytes=_LOG_TAIL_BYTES)
        writer.add_file("bridge.err.log", log_directory / "bridge.err.log")

        checks = doctor.run_checks(port=port, registry_path=registry_path, agents_config_path=agents_config_path)
        writer.add_text("doctor.txt", "\n".join(c.line() for c in checks) + "\n")
        writer.add_text(
            "doctor.json",
            json.dumps([{"name": c.name, "level": c.level, "detail": c.detail} for c in checks], indent=2),
        )

        writer.add_file("agents.json", agents_config_path)
        writer.add_file("agent_registry.json", registry_path)
        writer.add_text("versions.txt", _versions_text())

        claude_hooks_path = family_registry.get(CLAUDE_FAMILY).hook_spec().config_path
        codex_hooks_path = family_registry.get(CODEX_FAMILY).hook_spec().config_path
        writer.add_text("hooks.claude.json", json.dumps(_extract_our_hooks(claude_hooks_path), indent=2) + "\n")
        writer.add_text("hooks.codex.json", json.dumps(_extract_our_hooks(codex_hooks_path), indent=2) + "\n")

        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.switchboard.bridge.plist"
        if plist_path.exists():
            writer.add_file("launchagent.plist", plist_path)

        manifest = {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bundle_version": 1,
            "since": str(since) if since is not None else None,
            "members": sorted(writer.members),
        }
        writer.add_text("manifest.json", json.dumps(manifest, indent=2) + "\n")

    return out
