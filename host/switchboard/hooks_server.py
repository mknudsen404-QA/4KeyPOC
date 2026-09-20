"""HTTP server for two unrelated things that happen to share one loopback
port: CLI lifecycle hook POSTs (turned into a HookEvent and handed to
`submit`, the Bridge's thread-safe queue.put) and, when a UIContext is
given, the settings web UI (Phase 3) — a static page plus a small JSON
API over switchboard.ui.api. Neither has direct access to the registry
or the device beyond what UIContext exposes.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from switchboard.events import HookEvent
from switchboard.registry import Registry
from switchboard.trace import NullTraceWriter, redact_hook_payload

HOOK_HOST = "127.0.0.1"
HOOK_PORT = int(os.environ.get("SWITCHBOARD_HOOK_PORT", "8877"))
HOOK_PATH_PREFIX = "/switchboard-hook/"

UI_DIR = Path(__file__).resolve().parent / "ui"
_STATIC_FILES = {
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


@dataclass(frozen=True)
class UIContext:
    """Everything the settings UI's API routes need, bundled so
    hooks_server.py doesn't import switchboard.settings/switchboard.ui.api
    at module scope (keeps the hook-POST-only path's import graph small)."""

    settings_path: Path
    registry: Registry
    token: str

    def settings_service(self):
        from switchboard.settings import SlotSettingsService

        return SlotSettingsService(self.settings_path)


def _parse_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_allowed_host(host_header: str, port: int) -> bool:
    return host_header in (f"127.0.0.1:{port}", f"localhost:{port}")


def _is_allowed_origin(origin_header: str, port: int) -> bool:
    return origin_header in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def _api_get_routes():
    # Imported lazily (see UIContext.settings_service's comment) and built
    # once per request rather than at module scope, so a test that never
    # touches the UI never pays for importing switchboard.ui.api.
    from switchboard.ui import api

    return {
        "/api/settings": lambda ctx: (200, api.settings_get(ctx.settings_service())),
        "/api/families": lambda ctx: (200, api.families_get()),
        "/api/status": lambda ctx: (200, api.status_get(ctx.registry)),
        "/api/voice-providers": lambda ctx: (200, api.voice_providers_get()),
    }


def _make_handler(
    submit: Callable[[HookEvent], None], ui_context: UIContext | None, port: int, trace=None
) -> type[http.server.BaseHTTPRequestHandler]:
    trace = trace if trace is not None else NullTraceWriter()
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # keep hook/UI traffic out of the bridge's normal event log

        def _security_ok(self) -> bool:
            # Loopback bind already stops the outside world; this stops a
            # malicious page the user has open in the SAME browser from
            # driving requests here via DNS rebinding (bad Host) or a
            # plain cross-site fetch/POST (bad Origin, when present).
            if not _is_allowed_host(self.headers.get("Host", ""), port):
                self.send_error(403, "Host not allowed")
                return False
            origin = self.headers.get("Origin")
            if origin and not _is_allowed_origin(origin, port):
                self.send_error(403, "Origin not allowed")
                return False
            return True

        def _send_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _serve_index(self) -> None:
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            token = ui_context.token if ui_context is not None else ""
            html = html.replace("{{TOKEN}}", token)
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _serve_static(self, filename: str, content_type: str) -> None:
            payload = (UI_DIR / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._security_ok():
                return
            if ui_context is None:
                # No settings_path -> no UI wired up (see Bridge.run()):
                # 404 everything UI-related rather than serving a page
                # whose API calls could never work.
                self.send_error(404)
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path in _STATIC_FILES:
                filename, content_type = _STATIC_FILES[path]
                return self._serve_static(filename, content_type)
            if path == "/api/support-bundle":
                return self._serve_support_bundle()
            route = _api_get_routes().get(path)
            if route is not None:
                status, body = route(ui_context)
                return self._send_json(status, body)
            self.send_error(404)

        def _serve_support_bundle(self) -> None:
            # Same token gate as PUT /api/settings — a page in another
            # tab must not be able to pull the bundle just because it can
            # reach this loopback port.
            if (self.headers.get("X-Switchboard-Token") or "") != ui_context.token:
                self.send_error(403, "Bad or missing X-Switchboard-Token")
                return
            from switchboard.ui import api

            try:
                status, body, filename = api.support_bundle_get(ui_context)
            except OSError as exc:
                return self._send_json(500, {"error": str(exc)})
            if status != 200:
                return self._send_json(status, {"error": body.decode("utf-8", errors="replace")})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            if not self._security_ok():
                return
            path = self.path.split("?", 1)[0]
            if ui_context is None or path != "/api/settings":
                self.send_error(404)
                return
            if (self.headers.get("X-Switchboard-Token") or "") != ui_context.token:
                self.send_error(403, "Bad or missing X-Switchboard-Token")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            from switchboard.ui import api

            status, response = api.settings_put(ui_context.settings_service(), _parse_body(body))
            self._send_json(status, response)

        def do_POST(self):
            if not self._security_ok():
                return
            rx_mono = time.monotonic()
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            event_name = self.path.rsplit("/", 1)[-1]
            slot_key = (self.headers.get("X-Switchboard-Slot") or "").strip()
            if slot_key:
                payload = _parse_body(body)
                # Recorded here, not just after Bridge.step processes it:
                # the installed hook command is `curl --max-time 1 || true`
                # (hooks_install.py), so a slow bridge silently drops the
                # hook entirely — a hook_rx with no matching `hook` record
                # shortly after is exactly that fingerprint.
                receipt = {"kind": "hook_rx", "slot": slot_key, "event": event_name, "bytes": len(body)}
                receipt.update(redact_hook_payload(payload) if not trace.verbose else payload)
                trace.write(receipt)
                submit(HookEvent(slot_key=slot_key, name=event_name, payload=payload, rx_mono=rx_mono))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    return _Handler


def start_hook_server(
    submit: Callable[[HookEvent], None],
    host: str = HOOK_HOST,
    port: int = HOOK_PORT,
    *,
    ui_context: UIContext | None = None,
    trace=None,
) -> http.server.ThreadingHTTPServer | None:
    try:
        server = http.server.ThreadingHTTPServer((host, port), _make_handler(submit, ui_context, port, trace))
    except OSError as exc:
        print(f"Could not start hook listener on {host}:{port}: {exc}", file=sys.stderr)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
