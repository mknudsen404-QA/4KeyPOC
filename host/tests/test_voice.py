from switchboard.key_injector import FakeKeyInjector
from switchboard.voice import DEFAULT_PROVIDER, VoiceRegistry, registry
from switchboard.voice.base import VoiceContext
from switchboard.voice.builtin_stt import BuiltinSttProvider
from switchboard.voice.claude_native import ClaudeNativeProvider, SPACE_KEYCODE
from switchboard.voice.hotkey import HotkeyProvider
from switchboard.voice.none import NoneProvider


def ctx(chord=None, mode="hold", pid=1234):
    logs = []
    return VoiceContext(pid=pid, key_injector=FakeKeyInjector(), chord=chord, mode=mode, log=logs.append), logs


# --- registry ----------------------------------------------------------------


def test_default_provider_is_none():
    assert DEFAULT_PROVIDER == "none"


def test_registry_get_known_providers():
    assert isinstance(registry.get("claude_native"), ClaudeNativeProvider)
    assert isinstance(registry.get("hotkey"), HotkeyProvider)
    assert isinstance(registry.get("none"), NoneProvider)
    assert isinstance(registry.get("builtin_stt"), BuiltinSttProvider)


def test_registry_get_unknown_or_missing_falls_back_to_none():
    assert isinstance(registry.get("some-typo"), NoneProvider)
    assert isinstance(registry.get(None), NoneProvider)
    assert isinstance(registry.get(""), NoneProvider)


def test_registry_never_raises():
    for name in (None, "", "claude_native", "hotkey", "none", "builtin_stt", "bogus"):
        provider = registry.get(name)
        provider.available()
        c, _ = ctx()
        provider.hold(c)
        provider.release(c)


def test_custom_registry_is_isolated_from_module_singleton():
    custom = VoiceRegistry((ClaudeNativeProvider(),))
    assert isinstance(custom.get("claude_native"), ClaudeNativeProvider)
    assert isinstance(custom.get("hotkey"), NoneProvider)  # not registered in this instance


# --- claude_native -------------------------------------------------------------


def test_claude_native_hold_and_release_drive_space():
    provider = ClaudeNativeProvider()
    c, _ = ctx()
    provider.hold(c)
    assert c.key_injector.held == [(1234, SPACE_KEYCODE)]
    provider.release(c)
    assert c.key_injector.released == [(1234, SPACE_KEYCODE)]


def test_claude_native_always_available():
    assert ClaudeNativeProvider().available().available is True


# --- none ----------------------------------------------------------------------


def test_none_provider_hold_logs_and_touches_no_key(caplog=None):
    provider = NoneProvider()
    c, logs = ctx()
    provider.hold(c)
    assert c.key_injector.held == []
    assert logs and "no provider configured" in logs[0]


def test_none_provider_release_is_a_pure_noop():
    provider = NoneProvider()
    c, logs = ctx()
    provider.release(c)
    assert c.key_injector.released == []
    assert logs == []


def test_none_always_available():
    assert NoneProvider().available().available is True


# --- builtin_stt (deferred stub) ------------------------------------------------


def test_builtin_stt_is_always_unavailable():
    assert BuiltinSttProvider().available().available is False


def test_builtin_stt_hold_logs_not_implemented():
    provider = BuiltinSttProvider()
    c, logs = ctx()
    provider.hold(c)
    assert c.key_injector.held == []
    assert logs and "not implemented" in logs[0]


# --- hotkey ----------------------------------------------------------------------


def test_hotkey_hold_mode_holds_the_configured_chord():
    provider = HotkeyProvider()
    c, _ = ctx(chord="space", mode="hold")
    provider.hold(c)
    assert c.key_injector.held == [(1234, 49)]
    assert c.key_injector.released == []
    provider.release(c)
    assert c.key_injector.released == [(1234, 49)]


def test_hotkey_toggle_mode_taps_twice_not_holds():
    """toggle mode (macOS built-in Dictation) has no hold-to-talk: one
    tap on hold-start turns it on, another tap on hold-stop turns it off."""
    provider = HotkeyProvider()
    c, _ = ctx(chord="space", mode="toggle")
    provider.hold(c)
    assert c.key_injector.held == [(1234, 49)]
    assert c.key_injector.released == [(1234, 49)]  # tapped, not held

    c.key_injector.held.clear()
    c.key_injector.released.clear()
    provider.release(c)
    assert c.key_injector.held == [(1234, 49)]
    assert c.key_injector.released == [(1234, 49)]


def test_hotkey_missing_chord_logs_and_touches_no_key():
    provider = HotkeyProvider()
    c, logs = ctx(chord=None)
    provider.hold(c)
    assert c.key_injector.held == []
    assert logs and "no chord configured" in logs[0]


def test_hotkey_unsupported_chord_logs_and_touches_no_key():
    """The plan's own example chords (ctrl+space, cmd+shift+d, fn) need
    modifier support KeyInjector doesn't have yet — must say so clearly
    rather than silently doing nothing or doing the wrong thing."""
    provider = HotkeyProvider()
    c, logs = ctx(chord="ctrl+space")
    provider.hold(c)
    assert c.key_injector.held == []
    assert "not yet drivable" in logs[0]


def test_hotkey_release_without_chord_does_not_log_twice():
    """release() after a failed hold() (no chord) shouldn't add a second,
    redundant warning — hold() already explained the problem."""
    provider = HotkeyProvider()
    c, logs = ctx(chord=None)
    provider.hold(c)
    provider.release(c)
    assert len(logs) == 1


def test_hotkey_unavailable_when_no_app_found_and_dictation_disabled(monkeypatch):
    import switchboard.voice.hotkey as hotkey_module

    monkeypatch.setattr(hotkey_module.Path, "exists", lambda self: False)
    monkeypatch.setattr(hotkey_module, "_macos_dictation_enabled", lambda: False)
    assert HotkeyProvider().available().available is False


def test_hotkey_available_when_a_known_app_is_found(monkeypatch):
    import switchboard.voice.hotkey as hotkey_module

    monkeypatch.setattr(hotkey_module.Path, "exists", lambda self: True)
    availability = HotkeyProvider().available()
    assert availability.available is True


def test_hotkey_available_when_macos_dictation_is_enabled(monkeypatch):
    import switchboard.voice.hotkey as hotkey_module

    monkeypatch.setattr(hotkey_module.Path, "exists", lambda self: False)
    monkeypatch.setattr(hotkey_module, "_macos_dictation_enabled", lambda: True)
    assert HotkeyProvider().available().available is True
