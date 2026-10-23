#!/usr/bin/env python3
"""Live screenshot endpoint for the SnowLuma noVNC desktop.

Runs on the Docker host and grabs the SnowLuma container's X display
with ``xwd`` + ImageMagick, so callers (e.g. the AstrBot plugin) do not
need Docker access.  Bound to the docker0 gateway by default so it is
reachable from containers but not from the public internet.
"""

import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN_FILE = "/etc/qqwatch-screen.token"
CONTAINER = os.environ.get("QQWATCH_CONTAINER", "snowluma")
DISPLAY = os.environ.get("QQWATCH_DISPLAY", ":1")
BIND = os.environ.get("QQWATCH_BIND", "192.168.0.1")
PORT = int(os.environ.get("QQWATCH_PORT", "8765"))
WIDTH = os.environ.get("QQWATCH_WIDTH", "1100")
QUALITY = os.environ.get("QQWATCH_QUALITY", "80")


def load_token() -> str:
    with open(TOKEN_FILE, encoding="utf-8") as fh:
        return fh.read().strip()


def capture_jpeg() -> bytes:
    """Grab the X root window and convert it to a compact JPEG."""
    grab = subprocess.Popen(
        ["docker", "exec", "-i", CONTAINER, "sh", "-c",
         f"DISPLAY={DISPLAY} xwd -root -silent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        conv = subprocess.run(
            ["convert", "xwd:-", "-resize", f"{WIDTH}x", "-quality", QUALITY, "jpg:-"],
            stdin=grab.stdout,
            capture_output=True,
            timeout=25,
        )
    finally:
        if grab.stdout:
            grab.stdout.close()
        try:
            grab.wait(timeout=10)
        except subprocess.TimeoutExpired:
            grab.kill()
    if conv.returncode != 0 or not conv.stdout:
        raise RuntimeError(conv.stderr.decode(errors="replace")[:200] or "convert failed")
    return conv.stdout


class Handler(BaseHTTPRequestHandler):
    server_version = "qqwatch-screen/1.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        token = (parse_qs(parsed.query).get("token") or [""])[0]
        if parsed.path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if token != load_token():
            self._send(403, b"forbidden", "text/plain")
            return
        if parsed.path == "/shot":
            try:
                self._send(200, capture_jpeg(), "image/jpeg")
            except Exception as exc:  # noqa: BLE001 - report to caller
                self._send(500, f"screenshot failed: {exc}".encode(), "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("QQWATCH_VERBOSE"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
