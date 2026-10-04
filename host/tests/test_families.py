from switchboard.families import DEFAULT_FAMILY, FamilyRegistry, registry
from switchboard.families.antigravity import AntigravityProfile
from switchboard.families.claude import ClaudeProfile
from switchboard.families.codex import CodexProfile
from switchboard.families.generic import GenericProfile


def test_default_family_is_codex():
    assert DEFAULT_FAMILY == "codex"


def test_get_known_family_returns_its_profile():
    assert isinstance(registry.get("claude"), ClaudeProfile)
    assert isinstance(registry.get("codex"), CodexProfile)
    assert isinstance(registry.get("antigravity"), AntigravityProfile)


def test_get_unknown_family_returns_generic_profile():
    profile = registry.get("gemini")
    assert isinstance(profile, GenericProfile)
    assert profile.name == "gemini"


def test_get_none_or_empty_returns_generic_profile():
    assert isinstance(registry.get(None), GenericProfile)
    assert isinstance(registry.get(""), GenericProfile)


def test_infer_from_known_executable_basename():
    assert registry.infer("claude") == "claude"
    assert registry.infer("/usr/local/bin/claude --effort high") == "claude"
    assert registry.infer("codex") == "codex"
    assert registry.infer("agy") == "antigravity"


def test_infer_unknown_command_is_generic_not_codex():
    """The footgun this exists to fix: a slot with command "gemini" and no
    family used to silently get Codex's effort flags."""
    assert registry.infer("gemini") == "generic"


def test_infer_missing_command_falls_back_to_default_family():
    assert registry.infer(None) == DEFAULT_FAMILY
    assert registry.infer("") == DEFAULT_FAMILY


def test_claude_effort_args_known_and_unknown_values():
    profile = ClaudeProfile()
    assert profile.effort_args("xhigh") == ["--effort", "xhigh"]
    assert profile.effort_args("not-a-real-value") == ["--effort", "medium"]


def test_codex_effort_args_passes_through_all_five_values():
    """Confirmed live (Phase 5): codex accepts every value without error."""
    profile = CodexProfile()
    for value in ("low", "medium", "high", "xhigh", "max"):
        assert profile.effort_args(value) == ["-c", f"model_reasoning_effort={value}"]


def test_codex_effort_args_unknown_value_falls_back_to_medium():
    assert CodexProfile().effort_args("not-a-real-value") == ["-c", "model_reasoning_effort=medium"]


def test_antigravity_effort_args_clamps_to_the_default_models_ceiling():
    """Confirmed live: the default model (gemini-3.8-flash) only accepts
    low/medium/high — xhigh/max are a hard launch error on this CLI, not
    a no-op, so they must clamp down rather than pass through."""
    profile = AntigravityProfile()
    assert profile.effort_args("low") == ["--effort", "low"]
    assert profile.effort_args("medium") == ["--effort", "medium"]
    assert profile.effort_args("high") == ["--effort", "high"]
    assert profile.effort_args("xhigh") == ["--effort", "high"]
    assert profile.effort_args("max") == ["--effort", "high"]
    assert profile.effort_args("not-a-real-value") == ["--effort", "medium"]


def test_antigravity_hook_spec():
    """Hooks are wired (2026-10-04): a project-local merge strategy,
    4 of the 5 real events (PostInvocation deliberately left out — see
    module docstring), Stop maps to done same as every other family."""
    profile = AntigravityProfile()
    spec = profile.hook_spec()
    assert spec is not None
    assert spec.merge_strategy == "merge_named_hook"
    assert set(spec.events) == {"PreToolUse", "PostToolUse", "PreInvocation", "Stop"}
    assert "PostInvocation" not in spec.events
    assert spec.matchers["PreToolUse"] == "ask_question"
    assert spec.session_end_status is None
    assert profile.hook_status_for("Stop") == "done"
    assert profile.hook_status_for("PreToolUse") == "needs_input"
    assert profile.hook_status_for("PostInvocation") is None
    assert profile.capabilities().hooks is True


def test_generic_profile_has_every_capability_false():
    profile = GenericProfile("shell")
    assert profile.effort_args("high") == []
    assert profile.hook_spec() is None
    assert profile.hook_status_for("Stop") is None
    caps = profile.capabilities()
    assert caps.hooks is False
    assert caps.effort is False
    assert caps.voice is False
    assert caps.tier == "launch_only"


def test_claude_hook_spec_matches_status_table():
    from switchboard.status_table import CLAUDE_HOOK_STATUS, CLAUDE_SESSION_END_STATUS, HOOK_MATCHERS

    spec = ClaudeProfile().hook_spec()
    assert spec.matchers == HOOK_MATCHERS
    assert spec.session_end_status == CLAUDE_SESSION_END_STATUS
    assert spec.merge_strategy == "merge_by_marker"
    for event, status in CLAUDE_HOOK_STATUS.items():
        assert ClaudeProfile().hook_status_for(event) == status


def test_codex_hook_spec_matches_status_table():
    from switchboard.status_table import CODEX_HOOK_EVENTS, CODEX_HOOK_STATUS, CODEX_SESSION_END_STATUS

    spec = CodexProfile().hook_spec()
    assert spec.events == CODEX_HOOK_EVENTS
    assert spec.session_end_status == CODEX_SESSION_END_STATUS
    assert spec.merge_strategy == "rewrite_group"
    for event, status in CODEX_HOOK_STATUS.items():
        assert CodexProfile().hook_status_for(event) == status


def test_capabilities_tiers(monkeypatch):
    """Codex's tier is live, not hardcoded (Phase 5): it tracks whether
    its default voice provider (hotkey) is actually available, rather
    than a fixed "status" forever."""
    from switchboard.voice.base import Availability

    import switchboard.voice as voice_module

    monkeypatch.setattr(voice_module.registry, "get", lambda name: type("P", (), {"available": lambda self: Availability(False)})())
    assert ClaudeProfile().capabilities().tier == "full"
    assert CodexProfile().capabilities().tier == "status"
    assert GenericProfile("kimi").capabilities().tier == "launch_only"
    # Antigravity's tier now tracks voice availability the same way
    # Codex's does (hooks are wired as of 2026-10-04) — "status" only
    # because this monkeypatch reports no voice provider available.
    assert AntigravityProfile().capabilities().tier == "status"

    monkeypatch.setattr(voice_module.registry, "get", lambda name: type("P", (), {"available": lambda self: Availability(True)})())
    assert CodexProfile().capabilities().tier == "full"
    assert CodexProfile().capabilities().voice is True
    assert AntigravityProfile().capabilities().tier == "full"
    assert AntigravityProfile().capabilities().voice is True


def test_slash_commands_gives_key_intents_a_home():
    """Phase 5.4: log-only for now, but every profile exposes the method."""
    assert ClaudeProfile().slash_commands() == ("voice",)
    assert CodexProfile().slash_commands() == ()
    assert GenericProfile("kimi").slash_commands() == ()


def test_known_paths_map_only_lists_families_with_paths():
    paths = registry.known_paths_map()
    assert set(paths) == {"claude", "codex"}
    assert paths["codex"] == list(CodexProfile().known_paths)


def test_registry_never_raises_on_arbitrary_name():
    for name in (None, "", "shell", "gemini", "kimi", "antigravity", "a slot with spaces"):
        profile = registry.get(name)
        profile.detect()
        profile.effort_args("high")
        profile.hook_status_for("Stop")
        profile.default_voice()
        profile.slash_commands()
        profile.capabilities()


def test_custom_registry_is_isolated_from_the_module_singleton():
    custom = FamilyRegistry((ClaudeProfile(),))
    assert isinstance(custom.get("claude"), ClaudeProfile)
    assert isinstance(custom.get("codex"), GenericProfile)  # not registered in this instance
