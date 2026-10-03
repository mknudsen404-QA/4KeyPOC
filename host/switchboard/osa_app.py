"""Shared helper for building tiny double-clickable macOS .app bundles via
`osacompile` (built into macOS, no new dependency, no code signing), each
wrapping a single shell command with native-notification feedback instead
of a terminal. Used by install_settings_app.py and install_update_app.py.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _applescript_string(value: str) -> str:
    """Escape `value` for embedding as an AppleScript double-quoted
    string literal (backslash and double-quote are the only two
    characters that need it)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_script(shell_command: str, *, notification_title: str, success_message: str | None = None) -> str:
    """`shell_command` runs via `do shell script`. AppleScript's own
    behavior for that is to raise an error whose message is the command's
    stderr when it exits non-zero — so a failure becomes a native
    notification with the exact message the command already prints,
    without this needing to parse or guess anything. `success_message`,
    if given, also shows a notification on success; omitted means silent
    on success (e.g. "just opened a browser tab" doesn't need one).
    """
    lines = ["try", f"    do shell script {_applescript_string(shell_command)}"]
    if success_message is not None:
        lines.append(
            f"    display notification {_applescript_string(success_message)} "
            f"with title {_applescript_string(notification_title)}"
        )
    lines += [
        "on error errMsg",
        f"    display notification errMsg with title {_applescript_string(notification_title)}",
        "end try",
        "",
    ]
    return "\n".join(lines)


def compile_app(script: str, app_path: Path) -> None:
    """Always rebuilt fresh (paths baked into `script` may have moved
    since the last install, same reasoning as the LaunchAgent plist being
    regenerated rather than patched)."""
    app_path.parent.mkdir(parents=True, exist_ok=True)
    if app_path.exists():
        shutil.rmtree(app_path)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        subprocess.run(["osacompile", "-o", str(app_path), script_path], check=True, capture_output=True, text=True)
    finally:
        Path(script_path).unlink(missing_ok=True)
