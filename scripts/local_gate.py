#!/usr/bin/env python3
"""Token gate in front of the local ComfyUI container.

ComfyUI has no authentication of its own, so the tunnel that lets the VPS
render on this machine does not point at it directly. It points here: this
process checks ``Authorization: Bearer $LOCAL_COMFY_TOKEN`` and forwards only
the routes the render path uses to the container on loopback.

    python scripts/local_gate.py          # 127.0.0.1:18189 -> 127.0.0.1:18188

Configuration (``.env`` or the environment):
    LOCAL_COMFY_TOKEN     shared secret; the gate refuses to start without it
    LOCAL_GATE_PORT       where the gate listens (default 18189, loopback only)
    LOCAL_GATE_UPSTREAM   the container (default http://127.0.0.1:18188)
"""

from __future__ import annotations

import hmac
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def _setting(name: str, default: str) -> str:
    import os
    return os.environ.get(name) or config.ENV.get(name) or default


TOKEN = _setting("LOCAL_COMFY_TOKEN", "")
PORT = int(_setting("LOCAL_GATE_PORT", "18189"))
UPSTREAM = _setting("LOCAL_GATE_UPSTREAM", "http://127.0.0.1:18188").rstrip("/")

# Exactly what scripts/fleet.py and scripts/local_comfy.py call. Everything
# else ComfyUI serves (the editor, the manager, the websocket, /userdata) stays
# unreachable from the tunnel.
ALLOWED = {
    "GET": re.compile(r"^/(system_stats|object_info(/[\w.-]+)?|history/[\w-]+|view)$"),
    "POST": re.compile(r"^/(prompt|interrupt|upload/image)$"),
}
# Hop-by-hop, plus the credential itself: ComfyUI has no use for it.
DROP = {"host", "authorization", "connection", "keep-alive", "transfer-encoding",
        "te", "trailer", "upgrade", "proxy-authorization", "content-length"}
MAX_BODY = 64 * 1024 * 1024


class Gate(BaseHTTPRequestHandler):
    server_version = "comfy-local-gate"
    sys_version = ""

    def _deny(self, code: int, msg: str) -> None:
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode()):
            return self._deny(401, "unauthorized")
        path = self.path.split("?", 1)[0]
        rule = ALLOWED.get(self.command)
        if rule is None or not rule.match(path):
            return self._deny(404, "not exposed")

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._deny(413, "body too large")
        data = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
        req = urllib.request.Request(UPSTREAM + self.path, data=data,
                                     headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            resp = exc  # ComfyUI's 400 body names the node at fault: pass it on
        except (urllib.error.URLError, OSError) as exc:
            return self._deny(502, f"local ComfyUI unreachable: {exc}")
        with resp:
            body = resp.read()
            self.send_response(resp.status if hasattr(resp, "status") else resp.code)
            for k, v in resp.headers.items():
                if k.lower() not in DROP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    do_GET = _forward
    do_POST = _forward

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"gate {self.address_string()} {fmt % args}\n")


def main() -> int:
    if len(TOKEN) < 32:
        sys.exit("LOCAL_COMFY_TOKEN is missing or shorter than 32 characters; "
                 "the gate will not expose ComfyUI without one.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Gate)
    print(f"local gate 127.0.0.1:{PORT} -> {UPSTREAM}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
