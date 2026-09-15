"""The fallback profile for any command Switchboard doesn't recognise.
Every capability is False and hook_spec() is None, but it's still a
complete, valid FamilyProfile (Liskov) — an unrecognised command still
launches and shows `launched`/liveness, it just doesn't get effort flags,
hooks, or voice.
"""

from __future__ import annotations

from switchboard.families.base import Capabilities, Detection, HookSpec, detect_on_path


class GenericProfile:
    display_name: str
    executables: tuple[str, ...]
    known_paths: tuple[str, ...] = ()

    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name.title() if name else "Unknown"
        self.executables = (name,) if name else ()

    def detect(self) -> Detection:
        return detect_on_path(self.executables, self.known_paths)

    def effort_args(self, effort: str) -> list[str]:
        return []

    def hook_spec(self) -> HookSpec | None:
        return None

    def hook_status_for(self, event: str) -> str | None:
        return None

    def capabilities(self) -> Capabilities:
        return Capabilities(hooks=False, effort=False, voice=False, tier="launch_only")
