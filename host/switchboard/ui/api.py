"""Pure(ish) request/response building for the settings web UI's JSON
API — no HTTP, no sockets, so this is unit-testable without spinning a
server (see tests/test_ui_api.py). hooks_server.py's do_GET/do_PUT are a
thin routing layer over these functions.

See docs/design/slot-settings-and-family-parity-plan.md Phase 3.1.
"""

from __future__ import annotations

from switchboard.families import registry as family_registry
from switchboard.registry import Registry
from switchboard.settings import EFFORT_VALUES, VOICE_MODES, VOICE_PROVIDERS, SettingsValidationError, SlotSettingsService, validate_document
from switchboard.voice import registry as voice_registry

# Human-facing labels for the schema's voice provider vocabulary
# (settings.VOICE_PROVIDERS). Availability itself comes from each
# provider's own available() (switchboard.voice, Phase 4) — a real,
# live-checked answer, not a static guess.
_VOICE_PROVIDER_LABELS = {
    "claude_native": "Claude native (hold Space)",
    "hotkey": "System dictation hotkey",
    "none": "None",
}


def field_error_dict(errors) -> list[dict]:
    return [{"path": e.path, "message": e.message, "severity": e.severity} for e in errors]


def settings_get(service: SlotSettingsService) -> dict:
    doc = service.load()
    warnings = [e for e in validate_document(doc) if e.severity == "warning"]
    return {
        "document": doc,
        "version_token": service.version_token(),
        "warnings": field_error_dict(warnings),
    }


def settings_put(service: SlotSettingsService, body: dict) -> tuple[int, dict]:
    """Full-document save with optimistic concurrency. Returns (http_status,
    response_body): 200 on success, 409 if the on-disk version moved since
    the caller's version_token, 422 with field errors otherwise.
    """
    if not isinstance(body, dict) or "document" not in body:
        return 400, {"error": "expected an object with a \"document\" field"}
    doc = body["document"]
    if not isinstance(doc, dict):
        return 400, {"error": "\"document\" must be an object"}

    expected_token = body.get("version_token")
    current_token = service.version_token()
    if expected_token is not None and expected_token != current_token:
        return 409, {
            "error": "settings changed on disk since you loaded them",
            "current": settings_get(service),
        }

    try:
        service.save(doc)
    except SettingsValidationError as exc:
        return 422, {"errors": field_error_dict(exc.errors)}
    return 200, settings_get(service)


def families_get() -> dict:
    families = []
    for profile in family_registry.profiles():
        detection = profile.detect()
        caps = profile.capabilities()
        default_voice = profile.default_voice()
        families.append({
            "name": profile.name,
            "display_name": profile.display_name,
            "detected": detection.found,
            "path": detection.path,
            "tier": caps.tier,
            "hooks": caps.hooks,
            "effort": caps.effort,
            "voice": caps.voice,
            "effort_values": list(EFFORT_VALUES) if caps.effort else [],
            "default_voice_provider": default_voice.provider,
        })
    return {"families": families, "generic_tier": "launch_only"}


def status_get(registry: Registry) -> dict:
    slots = registry.load().get("slots", {})
    records = []
    for key in sorted(slots, key=lambda k: int(k)):
        record = slots[key]
        records.append({
            "slot": int(key),
            "name": record.get("name"),
            "family": record.get("family"),
            "status": record.get("status", "empty"),
            "liveness": record.get("liveness"),
        })
    return {"slots": records}


def voice_providers_get() -> dict:
    providers = []
    for name in VOICE_PROVIDERS:
        availability = voice_registry.get(name).available()
        providers.append({
            "name": name,
            "display_name": _VOICE_PROVIDER_LABELS.get(name, name),
            "modes": list(VOICE_MODES),
            "available": availability.available,
            "detail": availability.detail,
        })
    return {"providers": providers}
