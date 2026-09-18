"""Shared value types and the FamilyProfile protocol every CLI family
implements (see docs/design/slot-settings-and-family-parity-plan.md
Phase 1). A profile owns the parts of a family's behaviour that genuinely
vary per CLI — where its binary lives, its effort flag syntax, its hook
config file and merge strategy, its support tier. The status vocabulary
itself (which hook maps to which status, color, busy-ness) stays owned by
status_table.py; profiles that have hooks read it from there rather than
duplicating it, so there is exactly one place that data can drift out of
sync with the firmware header.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Detection:
    found: bool
    path: str | None = None


@dataclass(frozen=True)
class HookSpec:
    """Everything a generic hook installer needs to register/re-register
    this family's lifecycle hooks without knowing which family it is.
    """

    config_path: Path
    events: tuple[str, ...]
    matchers: dict[str, str | None] = field(default_factory=dict)
    event_status: dict[str, str] = field(default_factory=dict)
    session_end_status: str | None = None
    merge_strategy: str = "merge_by_marker"  # or "rewrite_group"
    command_suffix: str = ""


@dataclass(frozen=True)
class Capabilities:
    hooks: bool
    effort: bool
    voice: bool
    tier: str  # "full" | "status" | "launch_only"


@dataclass(frozen=True)
class VoiceSpec:
    """A family's default voice provider for a slot that doesn't specify
    one explicitly in agents.json (settings v2's `voice` field is
    optional per slot — see switchboard/settings.py). Matches the shape
    of that field, resolved to a provider name switchboard.voice's
    registry understands.
    """

    provider: str
    chord: str | None = None
    mode: str = "hold"


class FamilyProfile(Protocol):
    name: str
    display_name: str
    executables: tuple[str, ...]
    known_paths: tuple[str, ...]

    def detect(self) -> Detection: ...
    def effort_args(self, effort: str) -> list[str]: ...
    def hook_spec(self) -> HookSpec | None: ...
    def hook_status_for(self, event: str) -> str | None: ...
    def default_voice(self) -> VoiceSpec: ...
    def slash_commands(self) -> tuple[str, ...]: ...
    def capabilities(self) -> Capabilities: ...


def detect_on_path(executables: tuple[str, ...], known_paths: tuple[str, ...]) -> Detection:
    for name in executables:
        found = shutil.which(name)
        if found:
            return Detection(True, found)
    for candidate in known_paths:
        candidate_path = Path(candidate).expanduser()
        if candidate_path.exists():
            return Detection(True, str(candidate_path))
    return Detection(False, None)
