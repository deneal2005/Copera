#!/usr/bin/env python3
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from smarth_xerox import config
from smarth_xerox.http_app import Application, HttpError, json_response
from smarth_xerox.retention import cleanup_expired

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/admin": ("index.html", "text/html; charset=utf-8"),
    "/track": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml; charset=utf-8"),
    "/static/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
}
application: Application | None = None


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SmartXerox"
    sys_version = ""

    def log_message(self, format_string: str, *args) -> None:
        logging.getLogger("http").info("%s - %s", self.client_address[0], format_string % args)

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        asset = ASSETS.get(route)
        if asset:
            name, content_type = asset
            path = STATIC / name
            if not path.is_file():
                response = json_response(503, {"error": {"code": "ui_unavailable", "message": "Application files are missing."}})
            else:
                response = type("StaticResponse", (), {
                    "status": 200, "body": path.read_bytes(), "content_type": content_type,
                    "headers": {"Cache-Control": "no-cache"},
                })()
        else:
            response = self._dispatch("GET")
        self._send(response)

    def do_POST(self) -> None:
        self._send(self._dispatch("POST"))

    def do_PUT(self) -> None:
        self._send(self._dispatch("PUT"))

    def do_PATCH(self) -> None:
        self._send(self._dispatch("PATCH"))

    def do_DELETE(self) -> None:
        self._send(self._dispatch("DELETE"))

    def _dispatch(self, method: str):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return json_response(400, {"error": {"code": "invalid_length", "message": "Request length is invalid."}})
        if size < 0 or size > config.MAX_REQUEST_BYTES:
            return json_response(413, {"error": {"code": "request_too_large", "message": "Request is larger than the server limit."}})
        body = self.rfile.read(size) if size else b""
        if application is None:
            return json_response(503, {"error": {"code": "not_ready", "message": "The application is starting."}})
        return application.handle(method, self.path, self.headers, body, self.client_address[0])

    def _send(self, response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                         "script-src 'self' https://checkout.razorpay.com; connect-src 'self' https://api.razorpay.com; "
                         "frame-src blob: https://api.razorpay.com https://checkout.razorpay.com; object-src 'none'; "
                         "base-uri 'self'; frame-ancestors 'none'; form-action 'self' https://api.razorpay.com")
        self.send_header("Cache-Control", "no-store")
        if config.SECURE_COOKIES:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for key, value in response.headers.items():
            if isinstance(value, list):
                for item in value:
                    self.send_header(key, item)
            else:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)


def main() -> None:
    global application
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = Application()
    cleanup_expired()
    stop_cleanup = threading.Event()

    def cleanup_loop() -> None:
        while not stop_cleanup.wait(24 * 60 * 60):
            try:
                cleanup_expired()
            except Exception:
                logging.getLogger("smarth_xerox.retention").exception("Scheduled document cleanup failed")

    threading.Thread(target=cleanup_loop, daemon=True, name="smx-retention").start()
    server = ThreadingHTTPServer((config.HOST, config.PORT), RequestHandler)
    server.daemon_threads = True
    print(f"Smart Xerox Management listening on http://{config.HOST}:{config.PORT}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        stop_cleanup.set()
        server.server_close()


if __name__ == "__main__":
    main()
