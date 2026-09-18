import json

import pytest

from switchboard import settings
from switchboard.settings import SettingsValidationError, SlotSettingsService


def test_empty_document_shape():
    doc = settings.empty_document()
    assert doc == {"settings_version": 2, "defaults": {}, "slots": [], "leds": {}}


def test_migrate_v1_to_v2_renames_agents_to_slots_and_drops_title():
    v1 = {
        "defaults": {"cwd": "~/Documents"},
        "agents": [
            {"slot": 1, "name": "Maestro", "family": "codex", "cwd": "~/Documents", "command": "codex",
             "title": "Switchboard Agent 1 — Maestro"},
            {"slot": 3, "name": "Claude", "family": "claude", "command": "claude", "effort": "high"},
        ],
    }
    v2 = settings.migrate_v1_to_v2(v1)
    assert v2["settings_version"] == 2
    assert v2["defaults"] == {"cwd": "~/Documents"}
    slot1 = next(s for s in v2["slots"] if s["slot"] == 1)
    assert "title" not in slot1
    assert slot1["name"] == "Maestro"
    assert slot1["family"] == "codex"
    assert slot1["cwd"] == "~/Documents"
    assert slot1["command"] == "codex"
    slot3 = next(s for s in v2["slots"] if s["slot"] == 3)
    assert slot3["effort"] == "high"


def test_migrate_does_not_mutate_input():
    v1 = {"agents": [{"slot": 1, "name": "A", "command": "cat"}]}
    settings.migrate_v1_to_v2(v1)
    assert v1 == {"agents": [{"slot": 1, "name": "A", "command": "cat"}]}


def test_migrate_drops_legacy_out_of_range_slot():
    """Some existing agents.json files predate the fix confirming only 3
    physical keys are real agent slots (the 4th is push-to-talk) and
    configure a dead "slot 4" agent that firmware never launches. Carrying
    it into v2 would make every future save() fail validation until the
    user noticed and removed it by hand, so it's dropped during migration
    instead."""
    v1 = {"agents": [
        {"slot": 1, "name": "Maestro", "command": "codex"},
        {"slot": 4, "name": "Forge", "command": "codex"},
    ]}
    v2 = settings.migrate_v1_to_v2(v1)
    assert [s["slot"] for s in v2["slots"]] == [1]


def test_normalize_document_passes_through_existing_v2():
    v2 = {"settings_version": 2, "defaults": {}, "slots": [{"slot": 1, "command": "cat"}], "leds": {}}
    assert settings.normalize_document(v2) == v2


def test_normalize_document_adds_missing_leds_key():
    """An older v2 document saved before this feature existed has no
    `leds` key at all — normalize_document backfills it rather than
    leaving callers to guess."""
    v2 = {"settings_version": 2, "defaults": {}, "slots": []}
    assert settings.normalize_document(v2)["leds"] == {}


def test_normalize_document_migrates_v1():
    v1 = {"agents": [{"slot": 1, "command": "cat"}]}
    normalized = settings.normalize_document(v1)
    assert normalized["settings_version"] == 2
    assert normalized["slots"] == [{"slot": 1, "command": "cat"}]


def test_to_launch_config_joins_args_onto_command():
    v2 = {
        "settings_version": 2,
        "defaults": {"cwd": "~/Documents"},
        "slots": [{"slot": 1, "name": "A", "family": "codex", "command": "codex", "args": ["--foo", "bar baz"]}],
    }
    launch_config = settings.to_launch_config(v2)
    agent = launch_config["agents"][0]
    assert agent["command"] == "codex --foo 'bar baz'"
    assert launch_config["defaults"] == {"cwd": "~/Documents"}


def test_to_launch_config_no_args_leaves_command_bare():
    v2 = {"settings_version": 2, "defaults": {}, "slots": [{"slot": 1, "command": "claude"}]}
    assert settings.to_launch_config(v2)["agents"][0]["command"] == "claude"


# --- led_config_wire ---------------------------------------------------------


def test_led_config_wire_defaults_when_leds_missing():
    doc = settings.empty_document()
    assert settings.led_config_wire(doc) == {"idle_pulse_ms": 0, "busy_ramp_ms": 300000}


def test_led_config_wire_resolves_named_presets():
    doc = settings.empty_document()
    doc["leds"] = {"idle_breathe": "slow", "thinking_cycle": "quick"}
    assert settings.led_config_wire(doc) == {"idle_pulse_ms": 7000, "busy_ramp_ms": 90000}


def test_led_config_wire_unknown_preset_falls_back_to_default():
    doc = settings.empty_document()
    doc["leds"] = {"idle_breathe": "extremely-fast", "thinking_cycle": "glacial"}
    assert settings.led_config_wire(doc) == settings.led_config_wire(settings.empty_document())


# --- SlotSettingsService round trip -----------------------------------------


def test_load_missing_file_returns_empty_v2_document(tmp_path):
    service = SlotSettingsService(tmp_path / "agents.json")
    assert service.load() == settings.empty_document()


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    service = SlotSettingsService(tmp_path / "agents.json")
    doc = settings.empty_document()
    doc["slots"].append({"slot": 1, "name": "A", "command": "cat", "cwd": str(tmp_path / "Documents")})
    service.save(doc)
    assert service.load() == doc


def test_save_writes_a_backup_of_the_previous_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    path = tmp_path / "agents.json"
    service = SlotSettingsService(path)
    first = settings.empty_document()
    service.save(first)
    second = settings.empty_document()
    second["defaults"]["cwd"] = "~/Documents"
    service.save(second)
    backup = json.loads((tmp_path / "agents.json.bak").read_text())
    assert backup == first
    assert json.loads(path.read_text()) == second


def test_load_migrates_a_v1_file_without_writing_it(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"agents": [{"slot": 1, "name": "A", "command": "cat"}]}))
    service = SlotSettingsService(path)
    doc = service.load()
    assert doc["settings_version"] == 2
    assert doc["slots"] == [{"slot": 1, "name": "A", "command": "cat"}]
    # Not written back — the file on disk is still v1 until save() is called.
    assert "agents" in json.loads(path.read_text())


def test_slot_returns_none_when_not_configured(tmp_path):
    service = SlotSettingsService(tmp_path / "agents.json")
    assert service.slot(1) is None


def test_update_slot_creates_and_patches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    service = SlotSettingsService(tmp_path / "agents.json")
    service.update_slot(1, {"name": "A", "command": "cat"})
    updated = service.update_slot(1, {"effort": "high"})
    assert updated == {"slot": 1, "name": "A", "command": "cat", "effort": "high"}
    assert service.slot(1) == updated


def test_update_slot_none_value_removes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    service = SlotSettingsService(tmp_path / "agents.json")
    service.update_slot(1, {"name": "A", "command": "cat"})
    updated = service.update_slot(1, {"name": None})
    assert "name" not in updated


def test_update_slot_rejects_invalid_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    service = SlotSettingsService(tmp_path / "agents.json")
    with pytest.raises(SettingsValidationError):
        service.update_slot(9, {"command": "cat"})  # slot 9 is out of range


def test_version_token_changes_after_save(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    service = SlotSettingsService(tmp_path / "agents.json")
    before = service.version_token()
    service.save(settings.empty_document())
    after = service.version_token()
    assert before != after


def test_version_token_missing_file_is_stable():
    service = SlotSettingsService("/nonexistent/agents.json")
    assert service.version_token() == "missing" == service.version_token()


# --- validation --------------------------------------------------------------


def _errors_for(slot_doc, **doc_overrides):
    doc = settings.empty_document()
    doc.update(doc_overrides)
    doc["slots"] = [slot_doc]
    return settings.validate_document(doc)


def test_valid_minimal_document_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    errors = _errors_for({"slot": 1, "command": "cat", "cwd": str(tmp_path / "Documents")})
    assert not any(e.severity == "error" for e in errors)


def test_slot_number_out_of_range_is_an_error():
    errors = _errors_for({"slot": 0, "command": "cat"})
    assert any(e.path == "slots[0].slot" and e.severity == "error" for e in errors)
    errors = _errors_for({"slot": 5, "command": "cat"})
    assert any(e.path == "slots[0].slot" and e.severity == "error" for e in errors)


def test_duplicate_slot_numbers_are_an_error():
    doc = settings.empty_document()
    doc["slots"] = [{"slot": 1, "command": "cat"}, {"slot": 1, "command": "echo"}]
    errors = settings.validate_document(doc)
    assert any("configured 2 times" in e.message for e in errors)


def test_missing_command_is_a_warning_not_an_error():
    errors = _errors_for({"slot": 1})
    matching = [e for e in errors if e.path == "slots[0].command"]
    assert matching and matching[0].severity == "warning"


def test_unresolvable_command_is_a_warning_not_an_error():
    errors = _errors_for({"slot": 1, "command": "definitely-not-a-real-binary-xyz"})
    matching = [e for e in errors if e.path == "slots[0].command"]
    assert matching and matching[0].severity == "warning"


def test_cwd_missing_outside_documents_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    errors = _errors_for({"slot": 1, "command": "cat", "cwd": "/nonexistent/elsewhere"})
    assert any(e.path == "slots[0].cwd" and e.severity == "error" for e in errors)


def test_cwd_missing_under_documents_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    errors = _errors_for({"slot": 1, "command": "cat", "cwd": str(tmp_path / "Documents" / "new-project")})
    assert not any(e.path == "slots[0].cwd" for e in errors)


def test_unknown_effort_is_an_error():
    errors = _errors_for({"slot": 1, "command": "cat", "effort": "extreme"})
    assert any(e.path == "slots[0].effort" and e.severity == "error" for e in errors)


def test_unknown_voice_provider_is_an_error():
    errors = _errors_for({"slot": 1, "command": "cat", "voice": {"provider": "carrier-pigeon"}})
    assert any(e.path == "slots[0].voice.provider" and e.severity == "error" for e in errors)


def test_known_voice_provider_is_fine():
    errors = _errors_for({"slot": 1, "command": "cat", "voice": {"provider": "hotkey", "mode": "toggle"}})
    assert not any(e.path.startswith("slots[0].voice") for e in errors)


def test_args_must_be_list_of_strings():
    errors = _errors_for({"slot": 1, "command": "cat", "args": "not-a-list"})
    assert any(e.path == "slots[0].args" and e.severity == "error" for e in errors)


def test_env_must_be_object_of_strings():
    errors = _errors_for({"slot": 1, "command": "cat", "env": {"KEY": 5}})
    assert any(e.path == "slots[0].env" and e.severity == "error" for e in errors)


def test_unknown_idle_breathe_preset_is_an_error():
    doc = settings.empty_document()
    doc["leds"] = {"idle_breathe": "supersonic"}
    errors = settings.validate_document(doc)
    assert any(e.path == "leds.idle_breathe" and e.severity == "error" for e in errors)


def test_unknown_thinking_cycle_preset_is_an_error():
    doc = settings.empty_document()
    doc["leds"] = {"thinking_cycle": "supersonic"}
    errors = settings.validate_document(doc)
    assert any(e.path == "leds.thinking_cycle" and e.severity == "error" for e in errors)


def test_known_led_presets_are_fine():
    doc = settings.empty_document()
    doc["leds"] = {"idle_breathe": "slow", "thinking_cycle": "quick"}
    errors = settings.validate_document(doc)
    assert not any(e.path.startswith("leds") for e in errors)
