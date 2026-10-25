"""HTTP server for the Wazy MVP: the chat (for the phone), the presenter view and a small JSON API.

Who can reach what:
  * This computer (127.0.0.1, not through a tunnel): everything.
  * Anyone else (a phone through ngrok): only the chat, and only with the access code,
    which arrives once in the QR link (?k=...) and is then kept in an HttpOnly cookie.
  * The presenter view, the tunnel address and "reset" are for this computer only.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import Config, ConfigError
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 4096
SENDS_PER_MINUTE = 30
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")


def tunnel_url() -> Optional[str]:
    """The public https address of a running ngrok tunnel, read from ngrok's local API."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as response:
            for tunnel in json.load(response).get("tunnels", []):
                if str(tunnel.get("public_url", "")).startswith("https://"):
                    return tunnel["public_url"]
    except Exception:
        pass
    return None


def make_handler(store: Store, access_code: str):
    send_times: list = []
    send_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Wazy"
        sys_version = ""

        def log_message(self, *args) -> None:  # keep the terminal quiet; message text is never logged
            pass

        # ----- who is asking -----
        def is_local(self) -> bool:
            return (self.client_address[0] in ("127.0.0.1", "::1")
                    and not self.headers.get("X-Forwarded-For"))

        def has_code(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            if any(part.strip() == f"wazy={access_code}" for part in cookie.split(";")):
                return True
            return parse_qs(urlparse(self.path).query).get("k", [""])[0] == access_code

        def allowed(self) -> bool:
            return self.is_local() or self.has_code()

        # ----- replies -----
        def reply(self, status: int, body, content_type: str = "application/json", headers: Optional[dict] = None) -> None:
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            if content_type == "text/html":
                self.send_header("Content-Security-Policy", CSP)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def page(self, name: str) -> None:
            self.reply(200, (STATIC / name).read_bytes(), "text/html")

        def forbidden(self) -> None:
            self.reply(403, "<!doctype html><meta name=viewport content='width=device-width'>"
                            "<body style='font-family:sans-serif;padding:24px'><h2>Wazy test server</h2>"
                            "<p>Scan the QR code on the presenter's screen to open the chat.</p>", "text/html")

        # ----- GET -----
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if not self.allowed():
                return self.forbidden()
            if "k" in parse_qs(url.query) and self.has_code():
                # Move the code from the address bar into a cookie, then show the clean address.
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self.reply(302, b"", "text/plain", {
                    "Set-Cookie": f"wazy={access_code}; Path=/; Max-Age=1209600; HttpOnly; SameSite=Lax{secure}",
                    "Location": url.path or "/"})
            if url.path in ("/", "/chat"):
                return self.page("chat.html")
            if url.path == "/api/state":
                return self.reply(200, store.state())
            if url.path == "/demo":
                return self.page("demo.html") if self.is_local() else self.forbidden()
            if url.path == "/api/link":
                if not self.is_local():
                    return self.forbidden()
                public = tunnel_url()
                return self.reply(200, {"url": f"{public}/?k={access_code}" if public else None})
            self.reply(404, {"error": "not found"})

        do_HEAD = do_GET

        # ----- POST -----
        def do_POST(self) -> None:
            if not self.allowed():
                return self.reply(403, {"error": "forbidden"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.reply(415, {"error": "send JSON"})          # blocks cross-site form posts
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.reply(403, {"error": "cross-site request"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.reply(400, {"error": "bad Content-Length"})
            if length < 0 or length > MAX_BODY:
                return self.reply(413, {"error": "too large"})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.reply(400, {"error": "bad JSON"})
            if not isinstance(data, dict):
                return self.reply(400, {"error": "send a JSON object"})
            path = urlparse(self.path).path

            if path == "/api/send":
                with send_lock:
                    now = time.time()
                    send_times[:] = [t for t in send_times if now - t < 60]
                    if len(send_times) >= SENDS_PER_MINUTE:
                        return self.reply(429, {"error": "slow down"})
                    send_times.append(now)
                store.handle_text(str(data.get("text", "")))
                return self.reply(200, {"ok": True})
            if path == "/api/answer":
                try:
                    commitment_id = int(data.get("commitment_id"))
                except (TypeError, ValueError):
                    return self.reply(400, {"error": "commitment_id must be a number"})
                if data.get("action") not in ("done", "later"):
                    return self.reply(400, {"error": "action must be done or later"})
                ok = store.answer_nudge(commitment_id, data["action"])
                return self.reply(200 if ok else 409, {"ok": ok})
            if path == "/api/reset" and self.is_local():
                store.reset()
                return self.reply(200, {"ok": True})
            self.reply(404, {"error": "not found"})

    return Handler


def run_scheduler(store: Store, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store.run_due()
        except Exception as error:  # never let one bad row stop the nudges
            print(f"[wazy] scheduler error: {error}", flush=True)
        stop.wait(1.0)


def main() -> None:
    try:
        config = Config()
    except ConfigError as error:
        raise SystemExit(f"[wazy] {error.code}")
    store = Store(config.db_path, config.anthropic_api_key, config.ai_model, config.later_minutes,
                  clock=config.clock())
    access_code = store.access_code(config.access_code)
    stop = threading.Event()
    threading.Thread(target=run_scheduler, args=(store, stop), daemon=True).start()
    try:
        server = ThreadingHTTPServer((config.host, config.port), make_handler(store, access_code))
    except OSError as error:
        raise SystemExit(f"[wazy] Cannot use port {config.port} ({error.strerror}). "
                         "Is Wazy already running? Set WAZY_PORT to use another port.")
    later = f"after {config.later_minutes} min" if config.later_minutes else "next morning at 10:00"
    print(f"""
  Wazy MVP is running
  -------------------
  Presenter view : http://127.0.0.1:{config.port}/demo
  Chat           : http://127.0.0.1:{config.port}/
  Reader         : {config.reader_name}
  Time zone      : {config.timezone_name}
  "Later"        : one more nudge {later}
  Data           : {config.db_path}
  Phone access   : run `ngrok http {config.port}` (or ./run.sh --phone), then scan the QR code on /demo

  Press Ctrl+C to stop.
""", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Wazy stopped.")
    finally:
        stop.set()
        server.server_close()
