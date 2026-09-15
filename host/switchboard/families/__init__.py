"""FamilyRegistry: the one place that knows which CLI families
Switchboard supports and how to find a profile for a family name or a
command's basename. Everything else (model.py's effort_args/hook_status_for,
launcher.py's KNOWN_COMMAND_PATHS and family inference, doctor.py's
families check) asks `registry`, never `if family == "claude"`.

See docs/design/slot-settings-and-family-parity-plan.md Phase 1.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.families.base import Capabilities, Detection, FamilyProfile, HookSpec
from switchboard.families.claude import ClaudeProfile
from switchboard.families.codex import CodexProfile
from switchboard.families.generic import GenericProfile

# Historical default: a slot with no family/command configured at all
# still launches Codex, same as before this refactor.
DEFAULT_FAMILY = CodexProfile.name


class FamilyRegistry:
    def __init__(self, profiles: tuple[FamilyProfile, ...]) -> None:
        self._by_name: dict[str, FamilyProfile] = {p.name: p for p in profiles}
        self._by_executable: dict[str, FamilyProfile] = {}
        for profile in profiles:
            for executable in profile.executables:
                self._by_executable[executable] = profile

    def profiles(self) -> tuple[FamilyProfile, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str | None) -> FamilyProfile:
        """A known family by name, else a GenericProfile stood up for
        whatever string was given. Never raises — an unrecognised family
        still launches (Liskov: GenericProfile is a valid profile with
        every capability False).
        """
        if not name:
            return GenericProfile(name or "")
        return self._by_name.get(name, GenericProfile(name))

    def infer(self, command: str | None) -> str:
        """The family name for a slot whose config omits `family`,
        inferred from its command's basename (Phase 1.4). Falls back to
        "generic" for anything unrecognised — deliberately *not* the old
        default of "codex", which was a footgun: a slot configured with
        command: "gemini" and no family used to silently get Codex's
        effort flags.
        """
        if not command:
            return DEFAULT_FAMILY
        parts = command.split()
        if not parts:
            return DEFAULT_FAMILY
        basename = Path(parts[0]).name
        profile = self._by_executable.get(basename)
        return profile.name if profile else "generic"

    def known_paths_map(self) -> dict[str, list[str]]:
        return {name: list(profile.known_paths) for name, profile in self._by_name.items() if profile.known_paths}


registry = FamilyRegistry((ClaudeProfile(), CodexProfile()))

__all__ = [
    "registry",
    "DEFAULT_FAMILY",
    "FamilyRegistry",
    "FamilyProfile",
    "Capabilities",
    "Detection",
    "HookSpec",
    "GenericProfile",
]
