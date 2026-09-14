from pathlib import Path

from switchboard import status_table

HEADER_PATH = Path(__file__).resolve().parents[2] / "firmware" / "neokey" / "status_table.h"


def test_status_table_header_is_current():
    generated = status_table.emit_header()
    checked_in = HEADER_PATH.read_text()
    assert generated == checked_in, (
        "firmware/neokey/status_table.h is out of sync with switchboard/status_table.py. "
        "Regenerate it with:\n"
        "  python3 -m switchboard.status_table --emit-header > firmware/neokey/status_table.h"
    )


def test_status_choices_excludes_unknown():
    assert "unknown" not in status_table.STATUS_CHOICES


def test_busy_statuses_are_working_and_thinking():
    assert set(status_table.BUSY_STATUSES) == {"working", "thinking"}


def test_claude_hook_status_matches_status_defs():
    assert status_table.CLAUDE_HOOK_STATUS["SessionStart"] == "idle"
    assert status_table.CLAUDE_HOOK_STATUS["UserPromptSubmit"] == "working"
    assert status_table.CLAUDE_HOOK_STATUS["PreToolUse"] == "needs_input"
    assert status_table.CLAUDE_HOOK_STATUS["Stop"] == "done"
    assert status_table.CLAUDE_HOOK_STATUS["SessionEnd"] == "empty"


def test_codex_hook_status_matches_status_defs():
    assert status_table.CODEX_HOOK_STATUS["PermissionRequest"] == "needs_input"
    assert status_table.CODEX_HOOK_STATUS["Stop"] == "done"
    assert status_table.CODEX_HOOK_STATUS["SessionEnd"] == "empty"


def test_pascal_case_naming():
    assert status_table._pascal_case("needs_input") == "NeedsInput"
    assert status_table._pascal_case("done") == "Done"


def test_notification_matcher_is_derived_from_needs_input_types():
    """The hook matcher and the reducer guard must never drift apart:
    both come from NEEDS_INPUT_NOTIFICATION_TYPES."""
    from switchboard.status_table import HOOK_MATCHERS, NEEDS_INPUT_NOTIFICATION_TYPES

    assert HOOK_MATCHERS["Notification"] == "|".join(NEEDS_INPUT_NOTIFICATION_TYPES)
    assert "permission_prompt" in NEEDS_INPUT_NOTIFICATION_TYPES
    assert "idle_prompt" not in NEEDS_INPUT_NOTIFICATION_TYPES
