"""HTTP server for CLI lifecycle hooks. This has NO access to the registry
or the device — it only turns a POST into a HookEvent and calls `submit`
(the Bridge's thread-safe queue.put).
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from typing import Callable

from switchboard.events import HookEvent

HOOK_HOST = "127.0.0.1"
HOOK_PORT = int(os.environ.get("SWITCHBOARD_HOOK_PORT", "8877"))
HOOK_PATH_PREFIX = "/switchboard-hook/"


def _parse_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _make_handler(submit: Callable[[HookEvent], None]) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # keep hook traffic out of the bridge's normal event log

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            event_name = self.path.rsplit("/", 1)[-1]
            slot_key = (self.headers.get("X-Switchboard-Slot") or "").strip()
            if slot_key:
                submit(HookEvent(slot_key=slot_key, name=event_name, payload=_parse_body(body)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    return _Handler


def start_hook_server(
    submit: Callable[[HookEvent], None], host: str = HOOK_HOST, port: int = HOOK_PORT
) -> http.server.ThreadingHTTPServer | None:
    try:
        server = http.server.ThreadingHTTPServer((host, port), _make_handler(submit))
    except OSError as exc:
        print(f"Could not start hook listener on {host}:{port}: {exc}", file=sys.stderr)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
