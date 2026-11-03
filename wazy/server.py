"""The web server: the chat page, the presenter view, a small JSON API and the WhatsApp webhook.

Who can reach what:
  * This computer (a loopback address and no proxy or tunnel headers): everything.
  * Another browser: the chat only, after opening the access link once (/?k=CODE). The code then moves
    into an HttpOnly cookie. Each browser also gets its own random id cookie, so every tester has a
    separate conversation: user_id = "web:" + that id. This computer is always "web:local".
  * The presenter view (/demo), its data, the QR link and reset: this computer, or a browser that
    opened /demo?admin=CODE once (WAZY_ADMIN_CODE).
  * Twilio: POST /whatsapp/webhook. whatsapp.py checks the signature and decides the answer.
  * Anyone: GET /healthz, which says {"ok": true} and nothing else.

Every JSON POST must be JSON (415), come from this site if the browser says where it comes from (403),
be at most 4096 bytes (413) and hold a JSON object (400). Each person may POST 30 times a minute (429).
The terminal stays quiet: no request lines (a link can hold the access code) and never message text.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlparse

from .config import Config, ConfigError, load_config

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 4096                 # bytes in a JSON POST
MAX_WEBHOOK_BODY = 32768        # Twilio's form posts carry many fields and up to 1600 characters of text
MAX_SKIP = 65536                # a refused body up to this size is read and dropped, so the client sees our answer
POSTS_PER_MINUTE = 30

ACCESS_COOKIE = "wazy_access"
USER_COOKIE = "wazy_user"
ADMIN_COOKIE = "wazy_admin"
ACCESS_COOKIE_AGE = 30 * 24 * 3600      # a month
USER_COOKIE_AGE = 365 * 24 * 3600       # a year: the cookie is the person's conversation
ADMIN_COOKIE_AGE = 24 * 3600            # a day

LOCAL_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP")
USER_TOKEN = re.compile(r"^[0-9a-f]{32}$")                     # 128 random bits as hex
STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9]+)+$")   # no folders, no "..", no "%"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                ".json": "application/json; charset=utf-8", ".webmanifest": "application/manifest+json",
                ".woff2": "font/woff2"}
PAGES = {"/": "chat.html", "/chat": "chat.html", "/demo": "demo.html"}

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
EMPTY_TWIML = b'<?xml version="1.0" encoding="UTF-8"?><Response/>'
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                    ("X-Frame-Options", "SAMEORIGIN"), ("Cache-Control", "no-store"))
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'; "
       "base-uri 'none'; form-action 'self'")
FORBIDDEN_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                  "<title>Wazy</title><body style='font-family:sans-serif;padding:24px'><h2>Wazy test server</h2>"
                  "<p>Scan the QR code on the presenter's screen to open the chat.</p>"
                  "<p>Escanea el código QR de la pantalla del presentador para abrir el chat.</p>").encode("utf-8")


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def same(given: str, expected: str) -> bool:
    """Compare a secret in constant time, so the answer time does not leak how much of it was right."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def reply_json(reply: Any) -> dict:
    """One Wazy reply as the chat page receives it."""
    return {"kind": reply.kind, "text": reply.text, "lang": reply.lang,
            "commitment_id": reply.commitment_id, "buttons": bool(reply.buttons)}


def whole_number(value: Any) -> Optional[int]:
    """12 or "12" -> 12. Anything else (true, 1.5, "x", -3) -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.match(r"^[0-9]{1,18}$", value):
        return int(value)
    return None


def static_file(name: str) -> Optional[Path]:
    """A file directly inside wazy/static with a known type, or None. No folders, no '..', no tricks."""
    if not STATIC_NAME.match(name):
        return None
    folder = STATIC.resolve()
    path = (folder / name).resolve()            # resolve() follows links, so a link pointing out is caught below
    if path.parent != folder or path.suffix not in STATIC_TYPES or not path.is_file():
        return None
    return path


def log_error(where: str, error: BaseException) -> None:
    """Say where something broke, with the error type and code lines but not its message (it may hold text)."""
    print(f"[wazy] {where} failed: {type(error).__name__}", file=sys.stderr, flush=True)
    traceback.print_tb(error.__traceback__, file=sys.stderr)


def access_code_for(db: Any, configured: str) -> str:
    """The code remote browsers need: WAZY_ACCESS_CODE, else the one saved earlier, else a new one (saved)."""
    if configured:
        return configured
    saved = db.setting("access_code")
    if saved:
        return saved
    code = secrets.token_urlsafe(9)             # 12 characters, safe in a link and a cookie
    db.set_setting("access_code", code)
    return code


def tunnel_url() -> Optional[str]:
    """The public https address of a tunnel started by ./run.sh --phone, read from its local status API."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as response:
            for tunnel in json.load(response).get("tunnels", []):
                if str(tunnel.get("public_url", "")).startswith("https://"):
                    return str(tunnel["public_url"])
    except Exception:
        pass
    return None


class RateLimiter:
    """At most `limit` events per `window` seconds for each key: a sliding window of timestamps."""

    def __init__(self, limit: int = POSTS_PER_MINUTE, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self.times: Dict[str, List[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self.lock:
            now = self.clock()
            recent = [t for t in self.times.get(key, []) if now - t < self.window]
            allowed = len(recent) < self.limit
            if allowed:
                recent.append(now)
            self.times[key] = recent
            if len(self.times) > 10000:          # forget people who have gone quiet
                self.times = {k: v for k, v in self.times.items() if v and now - v[-1] < self.window}
            return allowed


# ---------------------------------------------------------------------------------------------
# The server object: holds what every request needs
# ---------------------------------------------------------------------------------------------
class WazyServer(ThreadingHTTPServer):
    daemon_threads = True       # a slow phone never stops the server from shutting down

    def __init__(self, address: tuple, app: Any, config: Config, whatsapp: Any, access_code: str) -> None:
        super().__init__(address, Handler)
        self.app = app
        self.config = config
        self.whatsapp = whatsapp
        self.access_code = access_code
        self.limiter = RateLimiter()
        self.find_tunnel: Callable[[], Optional[str]] = tunnel_url

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A broken connection (the phone went away) is normal; anything else is logged without text."""
        error = sys.exc_info()[1]
        if error is not None and not isinstance(error, OSError):
            log_error("connection", error)


def make_server(app: Any, config: Config, whatsapp: Any = None) -> WazyServer:
    """Build (and bind) the server. Call serve_forever() on it to start answering."""
    code = access_code_for(app.db, config.access_code)
    return WazyServer((config.host, config.port), app, config, whatsapp, code)


# ---------------------------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server: WazyServer
    server_version = "Wazy"
    sys_version = ""
    timeout = 30                # seconds a client may take to send its request

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet: request lines can hold the access code, and message text is never logged."""

    # ----- who is asking -----
    def is_local(self) -> bool:
        """This computer, directly: a loopback address and no proxy or tunnel headers."""
        if self.client_address[0] not in LOCAL_ADDRESSES:
            return False
        return not any(name in self.headers for name in PROXY_HEADERS)

    def cookies(self) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                found.setdefault(name, value.strip())
        return found

    def is_admin(self) -> bool:
        if self.is_local():
            return True
        code = self.server.config.admin_code
        return bool(code) and same(self.cookies().get(ADMIN_COOKIE, ""), code)

    def has_access(self) -> bool:
        if self.is_local() or self.is_admin():
            return True
        return same(self.cookies().get(ACCESS_COOKIE, ""), self.server.access_code)

    def user_id(self) -> str:
        """'web:local' here; elsewhere 'web:' + the browser's id cookie, made now if it has none."""
        if self.is_local():
            return "web:local"
        if self.user is None:
            token = self.cookies().get(USER_COOKIE, "")
            if not USER_TOKEN.match(token):
                token = secrets.token_hex(16)
                self.new_cookies.append(self.cookie(USER_COOKIE, token, USER_COOKIE_AGE))
            self.user = "web:" + token
        return self.user

    def is_https(self) -> bool:
        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return forwarded == "https" or self.server.config.public_url.startswith("https://")

    def cookie(self, name: str, value: str, max_age: int) -> str:
        secure = "; Secure" if self.is_https() else ""
        return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    # ----- answering -----
    def send(self, status: int, body: bytes, content_type: str, headers: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for cookie in self.new_cookies:
            self.send_header("Set-Cookie", cookie)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data: Any) -> None:
        self.send(status, json_bytes(data), JSON_TYPE)

    def refuse(self, status: int, error: str) -> None:
        self.send_json(status, {"ok": False, "error": error})

    def send_replies(self, replies: list) -> None:
        self.send_json(200, {"ok": True, "replies": [reply_json(r) for r in replies]})

    def send_page(self, name: str) -> None:
        self.send(200, (STATIC / name).read_bytes(), HTML_TYPE)

    def send_forbidden_page(self) -> None:
        self.send(403, FORBIDDEN_PAGE, HTML_TYPE)

    def redirect(self, location: str) -> None:
        self.send(302, b"", "text/plain; charset=utf-8", (("Location", location),))

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Errors the base class finds itself (a bad request line, a method we do not offer): same headers."""
        self.new_cookies = []
        self.close_connection = True
        self.refuse(code, "bad_request" if code < 500 else "not_supported")

    # ----- GET -----
    def do_GET(self) -> None:
        self.new_cookies: List[str] = []
        self.user: Optional[str] = None
        url = urlparse(self.path)
        self.query = parse_qs(url.query)
        routes = {"/": self.get_chat, "/chat": self.get_chat, "/demo": self.get_demo,
                  "/healthz": self.get_healthz, "/api/state": self.get_state,
                  "/api/admin/state": self.get_admin_state, "/api/link": self.get_link}
        try:
            if url.path in routes:
                routes[url.path]()
            elif url.path.startswith("/static/"):
                self.get_static(url.path[len("/static/"):])
            else:
                self.refuse(404, "not_found")
        except Exception as error:
            log_error(f"GET {url.path}", error)
            self.refuse(500, "server_error")

    do_HEAD = do_GET

    def get_chat(self) -> None:
        """The chat. A remote browser opens /?k=CODE once; the code moves into a cookie and the link is cleaned."""
        path = urlparse(self.path).path
        given = self.query.get("k", [""])[0]
        if given and same(given, self.server.access_code):
            self.new_cookies.append(self.cookie(ACCESS_COOKIE, given, ACCESS_COOKIE_AGE))
            self.user_id()                      # give this browser its own id now
            return self.redirect(path)
        if not self.has_access():
            return self.send_forbidden_page()
        if given:
            return self.redirect(path)          # already allowed: just take the wrong code out of the address
        self.user_id()
        self.send_page(PAGES[path])

    def get_demo(self) -> None:
        """The presenter view. A remote browser opens /demo?admin=CODE once; the code moves into a cookie."""
        given = self.query.get("admin", [""])[0]
        code = self.server.config.admin_code
        if given and code and same(given, code):
            self.new_cookies.append(self.cookie(ADMIN_COOKIE, given, ADMIN_COOKIE_AGE))
            return self.redirect("/demo")
        if not self.is_admin():
            return self.send_forbidden_page()
        if given:
            return self.redirect("/demo")
        self.send_page(PAGES["/demo"])

    def get_healthz(self) -> None:
        """For uptime checks: no login, no data."""
        self.send_json(200, {"ok": True})

    def get_state(self) -> None:
        """What this person's chat shows (only their own data)."""
        if not self.has_access():
            return self.refuse(403, "forbidden")
        self.send_json(200, self.server.app.state(self.user_id()))

    def get_admin_state(self) -> None:
        """Everything, for the presenter view."""
        if not self.is_admin():
            return self.refuse(403, "forbidden")
        self.send_json(200, self.server.app.admin_state())

    def get_link(self) -> None:
        """The chat link with the access code, for the QR code on the presenter view (None if not reachable)."""
        if not self.is_admin():
            return self.refuse(403, "forbidden")
        config = self.server.config
        base = config.public_url or self.server.find_tunnel()
        url = f"{base.rstrip('/')}/?k={quote(self.server.access_code, safe='')}" if base else None
        whatsapp = "https://wa.me/" + config.twilio_from.split("+")[-1] if config.twilio_enabled else None
        self.send_json(200, {"url": url, "whatsapp_url": whatsapp})

    def get_static(self, name: str) -> None:
        """Files directly inside wazy/static (styles, scripts, images). Pages have their own routes."""
        if not self.has_access():
            return self.refuse(403, "forbidden")
        path = static_file(name)
        if path is None:
            return self.refuse(404, "not_found")
        self.send(200, path.read_bytes(), STATIC_TYPES[path.suffix])

    # ----- POST -----
    def do_POST(self) -> None:
        self.new_cookies = []
        self.user = None
        path = urlparse(self.path).path
        routes = {"/api/send": self.post_send, "/api/answer": self.post_answer, "/api/reset": self.post_reset}
        try:
            if path == "/whatsapp/webhook":
                return self.post_webhook()
            if path not in routes:
                return self.refuse(404, "not_found")
            allowed = self.is_admin() if path == "/api/reset" else self.has_access()
            if not allowed:
                return self.refuse(403, "forbidden")
            data = self.checked_json()
            if data is not None:
                routes[path](data)
        except Exception as error:
            log_error(f"POST {path}", error)
            self.refuse(500, "server_error")

    def body_length(self) -> Optional[int]:
        """Content-Length as a number (0 if missing), or None if it is not a number."""
        value = self.headers.get("Content-Length", "0").strip()
        return int(value) if re.match(r"^[0-9]{1,12}$", value) else None

    def read_body(self, limit: int) -> Optional[bytes]:
        """The request body, or None after answering 400 (bad length) or 413 (too large)."""
        length = self.body_length()
        if length is None:
            self.refuse(400, "bad_request")
            return None
        if length > limit:
            self.rfile.read(min(length, MAX_SKIP))
            self.refuse(413, "too_large")
            return None
        return self.rfile.read(length)

    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower()

    def same_origin(self) -> bool:
        """Browsers say where a POST comes from (Origin). If it is there, it must be this site."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        allowed = set()
        host = self.headers.get("Host", "").strip().lower()
        if host:
            allowed.update({"http://" + host, "https://" + host})
        public = urlparse(self.server.config.public_url)
        if public.netloc:
            allowed.add("https://" + public.netloc.lower())
        return origin.strip().lower() in allowed

    def checked_json(self) -> Optional[dict]:
        """The checks every JSON POST passes, in order. Returns the JSON object, or None after answering."""
        body = self.read_body(MAX_BODY)         # read first, so the client always sees our answer
        if body is None:
            return None
        if self.content_type() != "application/json":
            return self.refuse(415, "json_only")
        if not self.same_origin():
            return self.refuse(403, "forbidden")
        if not self.server.limiter.allow(self.user_id()):
            return self.refuse(429, "rate_limited")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return self.refuse(400, "bad_json")
        if not isinstance(data, dict):
            return self.refuse(400, "bad_json")
        return data

    def post_send(self, data: dict) -> None:
        """{"text": "..."}: the person's message. Answers with Wazy's replies."""
        text = data.get("text")
        if not isinstance(text, str):
            return self.refuse(400, "bad_request")
        self.send_replies(self.server.app.handle(self.user_id(), text, channel="web"))

    def post_answer(self, data: dict) -> None:
        """{"commitment_id": 12, "action": "done"|"later"}: a tap on a nudge. 409 if it is not open or not theirs."""
        commitment_id = whole_number(data.get("commitment_id"))
        action = data.get("action")
        if commitment_id is None or action not in ("done", "later"):
            return self.refuse(400, "bad_request")
        replies = self.server.app.tap(self.user_id(), commitment_id, action)
        if not replies:
            return self.refuse(409, "not_open")
        self.send_replies(replies)

    def post_reset(self, data: dict) -> None:
        """Presenter only: wipe everything except settings, for a clean demo."""
        self.server.app.reset()
        self.send_json(200, {"ok": True})

    def post_webhook(self) -> None:
        """Twilio's form post. whatsapp.py checks the signature and decides the status; we answer empty TwiML."""
        whatsapp = self.server.whatsapp
        if whatsapp is None:
            return self.refuse(404, "not_found")
        body = self.read_body(MAX_WEBHOOK_BODY)
        if body is None:
            return None
        if self.content_type() != "application/x-www-form-urlencoded":
            return self.refuse(415, "form_only")
        try:
            params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            return self.refuse(400, "bad_request")
        status = whatsapp.handle_webhook(params, self.headers.get("X-Twilio-Signature", ""))
        self.send(status, EMPTY_TWIML, "text/xml; charset=utf-8")


# ---------------------------------------------------------------------------------------------
# Starting everything: python -m wazy
# ---------------------------------------------------------------------------------------------
def start_scheduler(app: Any, stop: threading.Event, every: float = 1.0) -> threading.Thread:
    """Call app.tick() every second in the background until stop is set. One bad tick never stops it."""
    def loop() -> None:
        while not stop.is_set():
            try:
                app.tick()
            except Exception as error:
                log_error("scheduler", error)
            stop.wait(every)

    thread = threading.Thread(target=loop, name="wazy-scheduler", daemon=True)
    thread.start()
    return thread


def build(config: Config) -> tuple:
    """The App, plus the AI reader and WhatsApp when their settings are there. Returns (app, whatsapp)."""
    from .app import App               # imported here so this file can be tested with a stand-in app
    ai = None
    if config.anthropic_api_key:
        from .ai import AIReader
        ai = AIReader(config.anthropic_api_key, config.ai_model)
    whatsapp = None
    if config.twilio_enabled:
        from .whatsapp import WhatsApp
        whatsapp = WhatsApp(account_sid=config.twilio_account_sid, auth_token=config.twilio_auth_token,
                            from_number=config.twilio_from, public_url=config.public_url,
                            content_sid=config.twilio_content_sid)
    app = App(config.db_path, default_tz=config.tz, later_minutes=config.later_minutes, ai=ai,
              on_message=whatsapp.on_message if whatsapp else None)
    if whatsapp:
        whatsapp.attach(app)
    return app, whatsapp


def startup_message(config: Config, server: WazyServer) -> str:
    """What the terminal shows once the server is up. Never message text, never the admin code."""
    port = server.server_address[1]
    here = config.host if config.host not in ("0.0.0.0", "::", "localhost") else "127.0.0.1"
    local = f"http://{here}:{port}"
    code = quote(server.access_code, safe="")
    access = f"{config.public_url}/?k={code}" if config.public_url else f"{local}/?k={code}"
    later = f"after {config.later_minutes} min" if config.later_minutes else "the next morning"
    phones = ("open the access link" if config.public_url
              else "run ./run.sh --phone, then scan the QR code on the presenter view")
    admin = "  Remote presenter: /demo?admin=<your WAZY_ADMIN_CODE>\n" if config.admin_code else ""
    return (f"\n  Wazy is running\n  ---------------\n"
            f"  Chat           : {local}/\n"
            f"  Presenter view : {local}/demo\n"
            f"  Access link    : {access}\n"
            f"  Reader         : {'rules + Claude Haiku' if config.anthropic_api_key else 'rules'}\n"
            f"  WhatsApp       : {'on' if config.twilio_enabled else 'off'}\n"
            f"  Time zone      : {config.tz}\n"
            f"  \"Later\"        : one more nudge {later}\n"
            f"  Data           : {config.db_path}\n"
            f"  Phones         : {phones}\n{admin}"
            f"\n  Press Ctrl+C to stop.\n")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"[wazy] Not starting: {error}", file=sys.stderr)
        raise SystemExit(2)
    app, whatsapp = build(config)
    try:
        server = make_server(app, config, whatsapp)
    except OSError as error:
        app.close()
        print(f"[wazy] Cannot use port {config.port} ({error.strerror}). Is Wazy already running? "
              "Set WAZY_PORT to use another port.", file=sys.stderr)
        raise SystemExit(1)
    stop = threading.Event()
    scheduler = start_scheduler(app, stop)
    if whatsapp:
        whatsapp.start()
    print(startup_message(config, server), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Wazy stopped.")
    finally:
        stop.set()
        scheduler.join(timeout=5)
        if whatsapp:
            whatsapp.stop()
        server.server_close()
        app.close()
