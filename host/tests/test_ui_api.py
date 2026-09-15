"""Phase 3: the settings web UI's JSON API. Two layers:

- switchboard.ui.api: pure request/response building, tested directly
  against a real SlotSettingsService/Registry on tmp_path (no HTTP).
- switchboard.hooks_server: the HTTP routing + security layer, tested
  with a real ThreadingHTTPServer on an ephemeral port (a Playwright-free
  smoke test) — this is what proves the token gate and Host/Origin checks
  actually work over the wire, not just in isolated unit tests.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.request import Request

import pytest

from switchboard import settings
from switchboard.hooks_server import UIContext, _is_allowed_host, _is_allowed_origin, start_hook_server
from switchboard.registry import Registry
from switchboard.settings import SlotSettingsService
from switchboard.ui import api


# --- switchboard.ui.api (no HTTP) -------------------------------------------


def _service(tmp_path, monkeypatch, initial=None):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir(exist_ok=True)
    service = SlotSettingsService(tmp_path / "agents.json")
    if initial is not None:
        service.save(initial)
    return service


def test_settings_get_shape(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    result = api.settings_get(service)
    assert result["document"] == settings.empty_document()
    assert result["version_token"] == service.version_token()
    assert result["warnings"] == []


def test_settings_get_surfaces_warnings_not_errors(tmp_path, monkeypatch):
    doc = settings.empty_document()
    doc["slots"] = [{"slot": 1, "command": "definitely-not-a-real-binary-xyz"}]
    service = _service(tmp_path, monkeypatch, initial=doc)
    result = api.settings_get(service)
    assert any(w["path"] == "slots[0].command" and w["severity"] == "warning" for w in result["warnings"])


def test_settings_put_success(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    loaded = api.settings_get(service)
    doc = loaded["document"]
    doc["slots"] = [{"slot": 1, "name": "A", "command": "cat"}]
    status, body = api.settings_put(service, {"document": doc, "version_token": loaded["version_token"]})
    assert status == 200
    assert body["document"]["slots"] == [{"slot": 1, "name": "A", "command": "cat"}]
    assert service.load()["slots"] == [{"slot": 1, "name": "A", "command": "cat"}]


def test_settings_put_conflict_on_stale_token(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    loaded = api.settings_get(service)
    service.save(settings.empty_document())  # someone else's save, moves the token
    status, body = api.settings_put(service, {"document": loaded["document"], "version_token": loaded["version_token"]})
    assert status == 409
    assert "current" in body


def test_settings_put_rejects_invalid_document(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    doc = settings.empty_document()
    doc["slots"] = [{"slot": 9, "command": "cat"}]  # out of range
    status, body = api.settings_put(service, {"document": doc})
    assert status == 422
    assert any(e["path"] == "slots[0].slot" for e in body["errors"])


def test_settings_put_missing_document_field_is_400(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    status, body = api.settings_put(service, {})
    assert status == 400


def test_families_get_lists_claude_and_codex():
    result = api.families_get()
    names = {f["name"] for f in result["families"]}
    assert {"claude", "codex"} <= names
    for entry in result["families"]:
        assert "tier" in entry and "detected" in entry


def test_status_get_reads_registry(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    registry.save({"version": 1, "slots": {"1": {"slot": 1, "name": "A", "status": "working"}}})
    result = api.status_get(registry)
    assert result["slots"] == [{"slot": 1, "name": "A", "family": None, "status": "working", "liveness": None}]


def test_status_get_empty_registry(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    assert api.voice_providers_get()["providers"]  # sanity: non-empty regardless
    assert api.status_get(registry) == {"slots": []}


def test_voice_providers_get_matches_schema_vocabulary():
    """Every provider the schema (settings.VOICE_PROVIDERS) accepts must
    be reported here — and vice versa — so the UI's dropdown and the
    validator never disagree about what's a legal value."""
    reported = {p["name"] for p in api.voice_providers_get()["providers"]}
    assert reported == set(settings.VOICE_PROVIDERS)


def test_voice_providers_get_hotkey_reported_unavailable():
    """hotkey is schema-legal but not implemented yet (Phase 4) — must be
    honest about that rather than claiming it works."""
    providers = {p["name"]: p for p in api.voice_providers_get()["providers"]}
    assert providers["hotkey"]["available"] is False
    assert providers["claude_native"]["available"] is True


# --- hooks_server security helpers (no HTTP) --------------------------------


def test_is_allowed_host():
    assert _is_allowed_host("127.0.0.1:8877", 8877)
    assert _is_allowed_host("localhost:8877", 8877)
    assert not _is_allowed_host("127.0.0.1:9999", 8877)
    assert not _is_allowed_host("evil.example:8877", 8877)
    assert not _is_allowed_host("", 8877)


def test_is_allowed_origin():
    assert _is_allowed_origin("http://127.0.0.1:8877", 8877)
    assert _is_allowed_origin("http://localhost:8877", 8877)
    assert not _is_allowed_origin("http://evil.example", 8877)
    assert not _is_allowed_origin("https://127.0.0.1:8877", 8877)  # wrong scheme


# --- real HTTP server smoke tests -------------------------------------------


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ui_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir(exist_ok=True)
    registry = Registry(tmp_path / "registry.json")
    ctx = UIContext(settings_path=tmp_path / "agents.json", registry=registry, token="test-token-123")
    port = free_port()
    server = start_hook_server(lambda event: None, "127.0.0.1", port, ui_context=ctx)
    assert server is not None
    time.sleep(0.2)
    yield {"port": port, "token": ctx.token, "registry": registry}
    server.shutdown()
    server.server_close()


def _get(port, path, headers=None):
    req = Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    resp = urllib.request.urlopen(req, timeout=2)
    return resp.status, resp.read()


def _put(port, path, body, headers=None):
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="PUT",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=2)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_index_page_embeds_real_token(ui_server):
    status, body = _get(ui_server["port"], "/")
    assert status == 200
    assert ui_server["token"].encode() in body
    assert b"{{TOKEN}}" not in body


def test_static_assets_are_served(ui_server):
    status, body = _get(ui_server["port"], "/app.js")
    assert status == 200
    assert b"SWITCHBOARD_TOKEN" in body
    status, _ = _get(ui_server["port"], "/styles.css")
    assert status == 200


def test_api_get_routes_return_json(ui_server):
    for path in ("/api/settings", "/api/families", "/api/status", "/api/voice-providers"):
        status, body = _get(ui_server["port"], path)
        assert status == 200, path
        json.loads(body)  # doesn't raise


def test_unknown_path_is_404(ui_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(ui_server["port"], "/nope")
    assert excinfo.value.code == 404


def test_put_settings_without_token_is_403(ui_server):
    status, _ = _put(ui_server["port"], "/api/settings", {"document": {"settings_version": 2, "defaults": {}, "slots": []}})
    assert status == 403


def test_put_settings_with_wrong_token_is_403(ui_server):
    status, _ = _put(
        ui_server["port"], "/api/settings",
        {"document": {"settings_version": 2, "defaults": {}, "slots": []}},
        headers={"X-Switchboard-Token": "not-the-token"},
    )
    assert status == 403


def test_put_settings_with_correct_token_saves(ui_server):
    _, body = _get(ui_server["port"], "/api/settings")
    loaded = json.loads(body)
    doc = loaded["document"]
    doc["slots"] = [{"slot": 1, "name": "A", "command": "cat"}]
    status, response_body = _put(
        ui_server["port"], "/api/settings",
        {"document": doc, "version_token": loaded["version_token"]},
        headers={"X-Switchboard-Token": ui_server["token"]},
    )
    assert status == 200
    saved = json.loads(response_body)
    assert saved["document"]["slots"] == [{"slot": 1, "name": "A", "command": "cat"}]


def test_bad_host_header_is_403(ui_server):
    """DNS-rebinding guard: a request whose Host header doesn't name this
    loopback server is rejected even though it physically arrived here."""
    sock = socket.create_connection(("127.0.0.1", ui_server["port"]), timeout=2)
    try:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n")
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()
    assert b" 403 " in response.splitlines()[0]


def test_cross_origin_request_is_403(ui_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(ui_server["port"], "/api/settings", headers={"Origin": "http://evil.example"})
    assert excinfo.value.code == 403


def test_get_without_ui_context_404s_api_routes():
    """A hook-POST-only server (no ui_context, as bridge.py uses when it
    has no settings_path) still serves nothing for the UI's routes rather
    than crashing."""
    port = free_port()
    server = start_hook_server(lambda event: None, "127.0.0.1", port)
    assert server is not None
    time.sleep(0.2)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/api/settings")
        assert excinfo.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/")
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
