"""The web server against a stand-in app: routes, who may see what, cookies, POST checks, rate limits,
security headers, static files and the WhatsApp webhook.

Each test starts a real server on 127.0.0.1 (a free port) in a thread. A request "from another computer" is
simulated with an X-Forwarded-For header, which is what a tunnel or a hosting proxy adds.

Run: python3 -m unittest tests.test_server -v
"""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import tempfile
import threading
import types
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

import wazy.server as server_module
from wazy.config import Config, ConfigError
from wazy.db import Database
from wazy.server import RateLimiter, access_code_for, make_server, start_scheduler, static_file, whole_number
from wazy.types import Reply

CODE = "class-code-2026"
ADMIN = "presenter-code-2026"
REMOTE = {"X-Forwarded-For": "203.0.113.7"}
PRIVATE = "".join(["Ana's ", "birthday"])     # built at run time, so a code line in a traceback never shows it
JSON = {"Content-Type": "application/json"}


class FakeApp:
    """Records every call and answers with fixed replies, like the real App would."""

    def __init__(self) -> None:
        self.db = Database(":memory:")
        self.calls: List[tuple] = []
        self.handle_replies = [Reply("confirm", "Got it: “Call mum”.", "en", 3)]
        self.tap_replies = [Reply("done_ack", "Nice one: “Call mum”.", "en", 3)]
        self.fail = False

    def handle(self, user_id: str, text: str, *, channel: str = "web") -> list:
        self.calls.append(("handle", user_id, text, channel))
        if self.fail:
            raise ValueError("boom with the private words " + text)
        return list(self.handle_replies)

    def tap(self, user_id: str, commitment_id: int, action: str) -> list:
        self.calls.append(("tap", user_id, commitment_id, action))
        return list(self.tap_replies)

    def state(self, user_id: str) -> dict:
        self.calls.append(("state", user_id))
        return {"messages": [], "open": [], "who": user_id}

    def admin_state(self) -> dict:
        self.calls.append(("admin_state",))
        return {"users": [], "focus_user": None}

    def reset(self) -> None:
        self.calls.append(("reset",))

    def tick(self) -> int:
        self.calls.append(("tick",))
        return 0

    def close(self) -> None:
        self.calls.append(("close",))

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


class FakeWhatsApp:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.webhooks: List[Tuple[dict, str]] = []

    def handle_webhook(self, params: dict, signature: str) -> int:
        self.webhooks.append((params, signature))
        return self.status


class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))

    def cookies(self) -> Dict[str, str]:
        """Set-Cookie headers by cookie name (the whole header line, flags included)."""
        return {line.split("=", 1)[0]: line for line in self.headers.get_all("Set-Cookie") or []}

    def cookie_value(self, name: str) -> str:
        return self.cookies()[name].split("=", 1)[1].split(";", 1)[0]


class ServerTest(unittest.TestCase):
    """Starts a fresh server for every test."""
    whatsapp: Optional[FakeWhatsApp] = None

    def setUp(self) -> None:
        self.app = FakeApp()
        self.start(self.settings())

    def settings(self) -> Config:
        return Config(port=0, access_code=CODE, admin_code=ADMIN)

    def start(self, config: Config, whatsapp: Optional[FakeWhatsApp] = None) -> None:
        if hasattr(self, "server"):
            self.stop()
        self.server = make_server(self.app, config, whatsapp or self.whatsapp)
        self.server.find_tunnel = lambda: None
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def tearDown(self) -> None:
        self.stop()
        self.app.db.close()

    def request(self, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            answer = connection.getresponse()
            return Response(answer.status, answer.headers, answer.read())
        finally:
            connection.close()

    def get(self, path: str, **headers: str) -> Response:
        return self.request("GET", path, headers=_headers(headers))

    def post(self, path: str, data: object = None, raw: Optional[bytes] = None, **headers: str) -> Response:
        body = raw if raw is not None else json.dumps(data if data is not None else {}).encode("utf-8")
        return self.request("POST", path, body=body, headers={**JSON, **_headers(headers)})

    def remote_browser(self) -> Dict[str, str]:
        """Open the access link like a phone does, and return the headers that browser sends afterwards."""
        answer = self.get(f"/?k={CODE}", **_kw(REMOTE))
        self.assertEqual(answer.status, 302)
        cookie = (f"wazy_access={answer.cookie_value('wazy_access')}; "
                  f"wazy_user={answer.cookie_value('wazy_user')}")
        return {**REMOTE, "Cookie": cookie}


def _kw(headers: Dict[str, str]) -> Dict[str, str]:
    """Header names as keyword arguments: X-Forwarded-For -> X_Forwarded_For."""
    return {name.replace("-", "_"): value for name, value in headers.items()}


def _headers(keywords: Dict[str, str]) -> Dict[str, str]:
    return {name.replace("_", "-"): value for name, value in keywords.items()}


# ---------------------------------------------------------------------------------------------
# This computer
# ---------------------------------------------------------------------------------------------
class LocalRoutes(ServerTest):
    def test_chat_page(self):
        for path in ("/", "/chat"):
            with self.subTest(path=path):
                answer = self.get(path)
                self.assertEqual(answer.status, 200)
                self.assertTrue(answer.headers["Content-Type"].startswith("text/html"))
                self.assertEqual(answer.body, (server_module.STATIC / "chat.html").read_bytes())

    def test_local_chat_sets_no_user_cookie(self):
        self.assertNotIn("wazy_user", self.get("/").cookies())

    def test_presenter_view(self):
        answer = self.get("/demo")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.body, (server_module.STATIC / "demo.html").read_bytes())

    def test_state_is_for_web_local(self):
        answer = self.get("/api/state")
        self.assertEqual(answer.json(), {"messages": [], "open": [], "who": "web:local"})
        self.assertEqual(self.app.calls, [("state", "web:local")])

    def test_admin_state(self):
        self.assertEqual(self.get("/api/admin/state").json(), {"users": [], "focus_user": None})

    def test_healthz_says_only_ok(self):
        answer = self.get("/healthz")
        self.assertEqual((answer.status, answer.json()), (200, {"ok": True}))
        self.assertEqual(self.app.calls, [])

    def test_unknown_path_is_404_json(self):
        answer = self.get("/nope")
        self.assertEqual((answer.status, answer.json()), (404, {"ok": False, "error": "not_found"}))

    def test_head_sends_headers_but_no_body(self):
        answer = self.request("HEAD", "/")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.body, b"")
        self.assertGreater(int(answer.headers["Content-Length"]), 0)

    def test_reset(self):
        answer = self.post("/api/reset")
        self.assertEqual((answer.status, answer.json()), (200, {"ok": True}))
        self.assertIn("reset", self.app.names())

    def test_proxy_headers_make_a_loopback_request_remote(self):
        for name in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
            with self.subTest(header=name):
                self.assertEqual(self.get("/api/state", **{name.replace("-", "_"): "1.2.3.4"}).status, 403)
                self.assertEqual(self.get("/demo", **{name.replace("-", "_"): "1.2.3.4"}).status, 403)


# ---------------------------------------------------------------------------------------------
# Another computer: the access code
# ---------------------------------------------------------------------------------------------
class RemoteAccess(ServerTest):
    def test_chat_without_code_is_a_friendly_403_page(self):
        answer = self.get("/", **_kw(REMOTE))
        self.assertEqual(answer.status, 403)
        self.assertIn(b"QR", answer.body)
        self.assertTrue(answer.headers["Content-Type"].startswith("text/html"))

    def test_api_without_code_is_403_and_the_app_is_not_asked(self):
        answer = self.get("/api/state", **_kw(REMOTE))
        self.assertEqual((answer.status, answer.json()), (403, {"ok": False, "error": "forbidden"}))
        self.assertEqual(self.app.calls, [])

    def test_wrong_code_is_refused(self):
        answer = self.get("/?k=wrong-code-123", **_kw(REMOTE))
        self.assertEqual(answer.status, 403)
        self.assertNotIn("wazy_access", answer.cookies())

    def test_code_moves_into_a_cookie_and_the_link_is_cleaned(self):
        answer = self.get(f"/?k={CODE}", **_kw(REMOTE))
        self.assertEqual(answer.status, 302)
        self.assertEqual(answer.headers["Location"], "/")
        self.assertEqual(answer.cookie_value("wazy_access"), CODE)

    def test_chat_path_keeps_its_path_after_the_redirect(self):
        self.assertEqual(self.get(f"/chat?k={CODE}", **_kw(REMOTE)).headers["Location"], "/chat")

    def test_cookies_are_httponly_lax_and_site_wide(self):
        cookies = self.get(f"/?k={CODE}", **_kw(REMOTE)).cookies()
        for name in ("wazy_access", "wazy_user"):
            with self.subTest(cookie=name):
                self.assertIn("HttpOnly", cookies[name])
                self.assertIn("SameSite=Lax", cookies[name])
                self.assertIn("Path=/", cookies[name])
                self.assertNotIn("Secure", cookies[name])

    def test_user_cookie_is_128_random_bits(self):
        value = self.get(f"/?k={CODE}", **_kw(REMOTE)).cookie_value("wazy_user")
        self.assertRegex(value, r"^[0-9a-f]{32}$")

    def test_cookie_opens_the_chat(self):
        headers = self.remote_browser()
        answer = self.get("/", **_kw(headers))
        self.assertEqual(answer.status, 200)
        self.assertNotIn("wazy_user", answer.cookies())       # it already has one

    def test_wrong_cookie_is_refused(self):
        answer = self.get("/api/state", **_kw({**REMOTE, "Cookie": "wazy_access=guess-guess"}))
        self.assertEqual(answer.status, 403)

    def test_each_browser_has_its_own_conversation(self):
        first, second = self.remote_browser(), self.remote_browser()
        self.get("/api/state", **_kw(first))
        self.get("/api/state", **_kw(second))
        ids = [call[1] for call in self.app.calls]
        self.assertEqual(len(set(ids)), 2)
        for user_id in ids:
            self.assertRegex(user_id, r"^web:[0-9a-f]{32}$")
        self.assertEqual(ids[0], "web:" + first["Cookie"].split("wazy_user=")[1])

    def test_a_browser_with_access_but_no_id_gets_one(self):
        answer = self.get("/", **_kw({**REMOTE, "Cookie": f"wazy_access={CODE}"}))
        self.assertEqual(answer.status, 200)
        self.assertRegex(answer.cookie_value("wazy_user"), r"^[0-9a-f]{32}$")

    def test_a_made_up_user_id_is_replaced(self):
        for bad in ("abc", "../../etc", "whatsapp:+34600000000", "A" * 32):
            with self.subTest(bad=bad):
                self.app.calls.clear()
                answer = self.get("/api/state", **_kw({**REMOTE, "Cookie": f"wazy_access={CODE}; wazy_user={bad}"}))
                self.assertEqual(answer.status, 200)
                self.assertNotEqual(self.app.calls[0][1], "web:" + bad)
                self.assertRegex(answer.cookie_value("wazy_user"), r"^[0-9a-f]{32}$")

    def test_already_allowed_browser_with_a_stale_code_is_just_redirected(self):
        headers = self.remote_browser()
        answer = self.get("/?k=an-old-code-999", **_kw(headers))
        self.assertEqual((answer.status, answer.headers["Location"]), (302, "/"))

    def test_healthz_needs_no_code(self):
        self.assertEqual(self.get("/healthz", **_kw(REMOTE)).json(), {"ok": True})


class SecureCookies(ServerTest):
    def test_secure_when_the_proxy_says_https(self):
        cookies = self.get(f"/?k={CODE}", **_kw({**REMOTE, "X-Forwarded-Proto": "https"})).cookies()
        self.assertIn("Secure", cookies["wazy_access"])
        self.assertIn("Secure", cookies["wazy_user"])

    def test_secure_when_the_public_address_is_https(self):
        self.start(Config(port=0, access_code=CODE, public_url="https://wazy.example.com"))
        self.assertIn("Secure", self.get(f"/?k={CODE}", **_kw(REMOTE)).cookies()["wazy_access"])


class AccessCode(ServerTest):
    def test_configured_code_wins(self):
        self.app.db.set_setting("access_code", "saved-code-1")
        self.assertEqual(access_code_for(self.app.db, "configured-1"), "configured-1")

    def test_saved_code_is_reused(self):
        self.app.db.set_setting("access_code", "saved-code-1")
        self.assertEqual(access_code_for(self.app.db, ""), "saved-code-1")

    def test_new_code_is_generated_once_and_kept(self):
        code = access_code_for(self.app.db, "")
        self.assertRegex(code, r"^[A-Za-z0-9_-]{8,}$")
        self.assertEqual(self.app.db.setting("access_code"), code)
        self.assertEqual(access_code_for(self.app.db, ""), code)

    def test_server_uses_the_generated_code(self):
        self.start(Config(port=0))
        code = self.app.db.setting("access_code")
        self.assertEqual(self.get(f"/?k={code}", **_kw(REMOTE)).status, 302)


# ---------------------------------------------------------------------------------------------
# The presenter (admin)
# ---------------------------------------------------------------------------------------------
class Admin(ServerTest):
    def admin_browser(self) -> Dict[str, str]:
        answer = self.get(f"/demo?admin={ADMIN}", **_kw(REMOTE))
        self.assertEqual((answer.status, answer.headers["Location"]), (302, "/demo"))
        self.assertIn("HttpOnly", answer.cookies()["wazy_admin"])
        return {**REMOTE, "Cookie": f"wazy_admin={answer.cookie_value('wazy_admin')}"}

    def test_remote_presenter_view_is_refused(self):
        for path in ("/demo", "/api/admin/state", "/api/link"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, **_kw(REMOTE)).status, 403)
        self.assertEqual(self.post("/api/reset", **_kw(REMOTE)).status, 403)
        self.assertEqual(self.app.calls, [])

    def test_access_code_is_not_enough_for_the_presenter_view(self):
        headers = self.remote_browser()
        for path in ("/demo", "/api/admin/state", "/api/link"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, **_kw(headers)).status, 403)
        self.assertEqual(self.post("/api/reset", **_kw(headers)).status, 403)
        self.assertNotIn("reset", self.app.names())

    def test_admin_code_opens_everything(self):
        headers = self.admin_browser()
        self.assertEqual(self.get("/demo", **_kw(headers)).status, 200)
        self.assertEqual(self.get("/api/admin/state", **_kw(headers)).status, 200)
        self.assertEqual(self.get("/api/link", **_kw(headers)).status, 200)
        self.assertEqual(self.get("/", **_kw(headers)).status, 200)
        self.assertEqual(self.post("/api/reset", **_kw(headers)).status, 200)
        self.assertIn("reset", self.app.names())

    def test_wrong_admin_code_is_refused(self):
        answer = self.get("/demo?admin=not-the-admin-code", **_kw(REMOTE))
        self.assertEqual(answer.status, 403)
        self.assertNotIn("wazy_admin", answer.cookies())

    def test_no_admin_code_set_means_no_remote_presenter(self):
        self.start(Config(port=0, access_code=CODE))
        self.assertEqual(self.get("/demo?admin=", **_kw(REMOTE)).status, 403)
        self.assertEqual(self.get("/demo", **_kw({**REMOTE, "Cookie": "wazy_admin="})).status, 403)


class Link(ServerTest):
    def test_public_address_is_used(self):
        self.start(Config(port=0, access_code=CODE, public_url="https://wazy.example.com"))
        self.assertEqual(self.get("/api/link").json()["url"], f"https://wazy.example.com/?k={CODE}")

    def test_tunnel_address_is_used_when_there_is_no_public_address(self):
        self.server.find_tunnel = lambda: "https://abc.tunnel.example/"
        self.assertEqual(self.get("/api/link").json()["url"], f"https://abc.tunnel.example/?k={CODE}")

    def test_no_address_known(self):
        self.assertEqual(self.get("/api/link").json(), {"url": None, "whatsapp_url": None})

    def test_whatsapp_link_when_twilio_is_on(self):
        self.start(Config(port=0, access_code=CODE, public_url="https://wazy.example.com",
                          twilio_account_sid="AC" + "0" * 32, twilio_auth_token="0" * 32,
                          twilio_from="whatsapp:+14155238886"), FakeWhatsApp())
        self.assertEqual(self.get("/api/link").json()["whatsapp_url"], "https://wa.me/14155238886")


# ---------------------------------------------------------------------------------------------
# Sending messages and tapping Done / Later
# ---------------------------------------------------------------------------------------------
class Api(ServerTest):
    def test_send_returns_the_replies(self):
        answer = self.post("/api/send", {"text": "call mum at 5"})
        self.assertEqual(answer.status, 200)
        expected = {"kind": "confirm", "text": "Got it: “Call mum”.", "lang": "en", "commitment_id": 3,
                    "buttons": False}
        self.assertEqual(answer.json(), {"ok": True, "replies": [expected]})
        self.assertEqual(self.app.calls, [("handle", "web:local", "call mum at 5", "web")])

    def test_send_keeps_unicode(self):
        self.post("/api/send", {"text": "recuérdame llamar a mamá 📞"})
        self.assertEqual(self.app.calls[0][2], "recuérdame llamar a mamá 📞")

    def test_nudge_buttons_are_passed_on(self):
        self.app.handle_replies = [Reply("nudge", "Time for “X”", "es", 9, True)]
        self.assertTrue(self.post("/api/send", {"text": "x"}).json()["replies"][0]["buttons"])

    def test_send_needs_text_as_a_string(self):
        for data in ({}, {"text": 5}, {"text": None}, {"text": ["a"]}):
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/send", data).json(), {"ok": False, "error": "bad_request"})
        self.assertEqual(self.app.calls, [])

    def test_remote_send_uses_that_browsers_id(self):
        headers = self.remote_browser()
        self.assertEqual(self.post("/api/send", {"text": "hi"}, **_kw(headers)).status, 200)
        self.assertEqual(self.app.calls[0][1], "web:" + headers["Cookie"].split("wazy_user=")[1])

    def test_remote_send_without_code_is_refused(self):
        self.assertEqual(self.post("/api/send", {"text": "hi"}, **_kw(REMOTE)).json(),
                         {"ok": False, "error": "forbidden"})
        self.assertEqual(self.app.calls, [])

    def test_answer_taps_the_nudge(self):
        answer = self.post("/api/answer", {"commitment_id": 3, "action": "done"})
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.json()["replies"][0]["kind"], "done_ack")
        self.assertEqual(self.app.calls, [("tap", "web:local", 3, "done")])

    def test_answer_accepts_a_number_written_as_text(self):
        self.post("/api/answer", {"commitment_id": "12", "action": "later"})
        self.assertEqual(self.app.calls, [("tap", "web:local", 12, "later")])

    def test_tap_on_a_nudge_that_is_not_open_is_409(self):
        self.app.tap_replies = []
        answer = self.post("/api/answer", {"commitment_id": 3, "action": "done"})
        self.assertEqual((answer.status, answer.json()), (409, {"ok": False, "error": "not_open"}))

    def test_answer_checks_its_fields(self):
        for data in ({"commitment_id": 3}, {"commitment_id": 3, "action": "stop"}, {"action": "done"},
                     {"commitment_id": "x", "action": "done"}, {"commitment_id": True, "action": "done"},
                     {"commitment_id": 1.5, "action": "done"}, {"commitment_id": -2, "action": "done"},
                     {"commitment_id": None, "action": "done"}):
            with self.subTest(data=data):
                answer = self.post("/api/answer", data)
                self.assertEqual((answer.status, answer.json()["error"]), (400, "bad_request"))
        self.assertEqual(self.app.calls, [])

    def test_unknown_post_path_is_404(self):
        self.assertEqual(self.post("/api/nope").status, 404)

    def test_app_error_is_500_and_the_text_is_not_logged(self):
        self.app.fail = True
        with mock.patch("sys.stderr", new=io.StringIO()) as log:
            answer = self.post("/api/send", {"text": "my secret dentist visit"})
        self.assertEqual((answer.status, answer.json()), (500, {"ok": False, "error": "server_error"}))
        self.assertIn("ValueError", log.getvalue())
        self.assertNotIn("dentist", log.getvalue())


class PostChecks(ServerTest):
    def test_json_only(self):
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""):
            with self.subTest(content_type=kind):
                answer = self.post("/api/send", {"text": "hi"}, Content_Type=kind)
                self.assertEqual((answer.status, answer.json()["error"]), (415, "json_only"))
        self.assertEqual(self.app.calls, [])

    def test_json_with_a_charset_is_fine(self):
        self.assertEqual(self.post("/api/send", {"text": "hi"}, Content_Type="application/json; charset=utf-8").status,
                         200)

    def test_other_sites_are_refused(self):
        for origin in ("https://evil.example", "null", f"http://127.0.0.1:{self.port}.evil.example"):
            with self.subTest(origin=origin):
                answer = self.post("/api/send", {"text": "hi"}, Origin=origin)
                self.assertEqual((answer.status, answer.json()["error"]), (403, "forbidden"))
        self.assertEqual(self.app.calls, [])

    def test_same_site_origin_is_fine(self):
        self.assertEqual(self.post("/api/send", {"text": "hi"}, Origin=f"http://127.0.0.1:{self.port}").status, 200)

    def test_public_address_origin_is_fine_behind_a_proxy(self):
        self.start(Config(port=0, access_code=CODE, public_url="https://wazy.example.com"))
        answer = self.post("/api/send", {"text": "hi"}, Origin="https://wazy.example.com")
        self.assertEqual(answer.status, 200)

    def test_body_over_4096_bytes_is_413(self):
        answer = self.post("/api/send", {"text": "x" * 5000})
        self.assertEqual((answer.status, answer.json()["error"]), (413, "too_large"))
        self.assertEqual(self.app.calls, [])

    def test_body_of_exactly_4096_bytes_is_fine(self):
        body = json.dumps({"text": ""}).encode()
        body = json.dumps({"text": "x" * (4096 - len(body))}).encode()
        self.assertEqual(len(body), 4096)
        self.assertEqual(self.post("/api/send", raw=body).status, 200)

    def test_bad_json_is_400(self):
        for raw in (b"{not json", b"", b"\xff\xfe", b"[" * 3000):
            with self.subTest(raw=raw[:10]):
                answer = self.post("/api/send", raw=raw)
                self.assertEqual((answer.status, answer.json()["error"]), (400, "bad_json"))

    def test_json_that_is_not_an_object_is_400(self):
        for data in ([1, 2], "text", 5, None):
            with self.subTest(data=data):
                answer = self.post("/api/send", raw=json.dumps(data).encode())
                self.assertEqual((answer.status, answer.json()["error"]), (400, "bad_json"))

    def test_bad_content_length_is_400(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", "/api/send")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "-5")
        connection.endheaders()
        answer = connection.getresponse()
        self.assertEqual(answer.status, 400)
        connection.close()

    def test_other_methods_are_refused_with_the_same_headers(self):
        answer = self.request("PUT", "/api/send", body=b"{}")
        self.assertEqual(answer.status, 501)
        self.assertEqual(answer.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("OPTIONS", "/api/send").status, 501)


class RateLimits(ServerTest):
    def test_31st_post_in_a_minute_is_429(self):
        for _ in range(30):
            self.assertEqual(self.post("/api/send", {"text": "hi"}).status, 200)
        answer = self.post("/api/send", {"text": "hi"})
        self.assertEqual((answer.status, answer.json()), (429, {"ok": False, "error": "rate_limited"}))
        self.assertEqual(self.app.names().count("handle"), 30)

    def test_limit_is_per_person(self):
        first, second = self.remote_browser(), self.remote_browser()
        for _ in range(30):
            self.post("/api/send", {"text": "hi"}, **_kw(first))
        self.assertEqual(self.post("/api/send", {"text": "hi"}, **_kw(first)).status, 429)
        self.assertEqual(self.post("/api/send", {"text": "hi"}, **_kw(second)).status, 200)

    def test_taps_count_too(self):
        for _ in range(30):
            self.post("/api/answer", {"commitment_id": 1, "action": "done"})
        self.assertEqual(self.post("/api/send", {"text": "hi"}).status, 429)

    def test_window_slides(self):
        now = [1000.0]
        limiter = RateLimiter(limit=2, window=60, clock=lambda: now[0])
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        now[0] += 60
        self.assertTrue(limiter.allow("a"))


class Headers(ServerTest):
    def assert_security_headers(self, answer: Response) -> None:
        self.assertEqual(answer.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(answer.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(answer.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(answer.headers["Cache-Control"], "no-store")

    def test_every_kind_of_answer_has_them(self):
        answers = [self.get("/"), self.get("/api/state"), self.get("/nope"), self.get("/healthz"),
                   self.get("/", **_kw(REMOTE)), self.get(f"/?k={CODE}", **_kw(REMOTE)),
                   self.post("/api/send", {"text": "hi"}), self.post("/api/send", raw=b"x", Content_Type="text/plain"),
                   self.post("/api/send", raw=b"[")]
        for answer in answers:
            with self.subTest(status=answer.status):
                self.assert_security_headers(answer)

    def test_csp_only_on_pages(self):
        csp = self.get("/").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIsNotNone(self.get("/demo").headers["Content-Security-Policy"])
        self.assertIsNone(self.get("/api/state").headers["Content-Security-Policy"])

    def test_json_is_utf8(self):
        self.assertEqual(self.get("/healthz").headers["Content-Type"], "application/json; charset=utf-8")

    def test_server_does_not_say_its_python_version(self):
        self.assertNotIn("Python", self.get("/healthz").headers["Server"])

    def test_requests_are_not_logged(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as log:
            self.get(f"/?k={CODE}", **_kw(REMOTE))
            self.post("/api/send", {"text": "a very private message"})
            self.get("/nope")
        self.assertEqual(log.getvalue(), "")


# ---------------------------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------------------------
class StaticFiles(ServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.static = root / "static"
        self.static.mkdir()
        (self.static / "style.css").write_text("body{}", encoding="utf-8")
        (self.static / "page.html").write_text("<p>hi</p>", encoding="utf-8")
        (self.static / "notes.txt").write_text("nope", encoding="utf-8")
        (root / "secret.css").write_text("SECRET", encoding="utf-8")
        (self.static / "sub").mkdir()
        (self.static / "sub" / "x.css").write_text("x", encoding="utf-8")
        try:
            os.symlink(root / "secret.css", self.static / "link.css")
        except OSError:  # pragma: no cover - systems without links
            pass
        patcher = mock.patch.object(server_module, "STATIC", self.static)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_static_file_is_served_with_its_type(self):
        answer = self.get("/static/style.css")
        self.assertEqual((answer.status, answer.body), (200, b"body{}"))
        self.assertEqual(answer.headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(answer.headers["X-Content-Type-Options"], "nosniff")

    def test_path_tricks_are_404(self):
        for path in ("/static/../secret.css", "/static/..%2fsecret.css", "/static/%2e%2e/secret.css",
                     "/static/sub/x.css", "/static/.env", "/static//etc/passwd", "/static/link.css",
                     "/static/page.html", "/static/notes.txt", "/static/", "/static/style.css%00.png",
                     "/static/..", "/static/sub", "/static/missing.css", "/static/%5c..%5csecret.css",
                     "/../wazy/config.py", "/static/style.css/"):
            with self.subTest(path=path):
                answer = self.get(path)
                self.assertIn(answer.status, (404,))
                self.assertNotIn(b"SECRET", answer.body)

    def test_static_file_names(self):
        self.assertIsNotNone(static_file("style.css"))
        for name in ("", ".", "..", "../secret.css", "sub/x.css", "style..css", "-x.css", "style.css\x00"):
            with self.subTest(name=name):
                self.assertIsNone(static_file(name))

    def test_remote_without_code_is_refused(self):
        self.assertEqual(self.get("/static/style.css", **_kw(REMOTE)).status, 403)


# ---------------------------------------------------------------------------------------------
# WhatsApp webhook
# ---------------------------------------------------------------------------------------------
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class Webhook(ServerTest):
    def setUp(self) -> None:
        self.whatsapp = FakeWhatsApp()
        super().setUp()

    def webhook(self, body: bytes, **headers: str) -> Response:
        return self.request("POST", "/whatsapp/webhook", body=body, headers={**FORM, **_headers(headers)})

    def test_params_and_signature_are_handed_to_whatsapp(self):
        body = b"From=whatsapp%3A%2B34600000000&Body=Llamar+a+mam%C3%A1+ma%C3%B1ana&ButtonPayload=&MessageSid=SM1"
        answer = self.webhook(body, X_Twilio_Signature="abc=", **_kw(REMOTE))
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.body, b'<?xml version="1.0" encoding="UTF-8"?><Response/>')
        self.assertTrue(answer.headers["Content-Type"].startswith("text/xml"))
        params, signature = self.whatsapp.webhooks[0]
        self.assertEqual(params, {"From": "whatsapp:+34600000000", "Body": "Llamar a mamá mañana",
                                  "ButtonPayload": "", "MessageSid": "SM1"})
        self.assertEqual(signature, "abc=")

    def test_bad_signature_status_comes_from_whatsapp(self):
        self.whatsapp.status = 403
        answer = self.webhook(b"Body=hi", X_Twilio_Signature="wrong")
        self.assertEqual(answer.status, 403)
        self.assertIn(b"<Response/>", answer.body)

    def test_missing_signature_is_passed_as_empty(self):
        self.webhook(b"Body=hi")
        self.assertEqual(self.whatsapp.webhooks[0][1], "")

    def test_webhook_needs_no_access_code_or_origin(self):
        answer = self.webhook(b"Body=hi", Origin="https://somewhere.example", **_kw(REMOTE))
        self.assertEqual(answer.status, 200)

    def test_webhook_takes_form_data_only(self):
        answer = self.request("POST", "/whatsapp/webhook", body=b"{}", headers=JSON)
        self.assertEqual(answer.status, 415)
        self.assertEqual(self.whatsapp.webhooks, [])

    def test_webhook_body_has_a_size_limit(self):
        answer = self.webhook(b"Body=" + b"x" * 40000)
        self.assertEqual(answer.status, 413)
        self.assertEqual(self.whatsapp.webhooks, [])

    def test_a_long_whatsapp_message_fits(self):
        body = ("Body=" + "%C3%B1" * 1600).encode()
        self.assertEqual(self.webhook(body).status, 200)

    def test_webhook_is_not_rate_limited_by_the_web_limit(self):
        for _ in range(35):
            self.webhook(b"Body=hi")
        self.assertEqual(self.webhook(b"Body=hi").status, 200)

    def test_no_whatsapp_means_404(self):
        self.start(self.settings(), None)
        self.server.whatsapp = None
        self.assertEqual(self.webhook(b"Body=hi").status, 404)


# ---------------------------------------------------------------------------------------------
# Starting up
# ---------------------------------------------------------------------------------------------
class Helpers(unittest.TestCase):
    def test_whole_number(self):
        self.assertEqual([whole_number(v) for v in (12, "12", 0, True, 1.5, "x", -1, "-1", None, "1e3")],
                         [12, 12, 0, None, None, None, None, None, None, None])

    def test_scheduler_ticks_until_stopped_and_survives_errors(self):
        app = FakeApp()
        ticks = threading.Event()
        count = [0]

        def tick() -> int:
            count[0] += 1
            if count[0] == 1:
                raise RuntimeError(PRIVATE)
            if count[0] >= 3:
                ticks.set()
            return 0

        app.tick = tick  # type: ignore[assignment]
        stop = threading.Event()
        with mock.patch("sys.stderr", new=io.StringIO()) as log:
            thread = start_scheduler(app, stop, every=0.01)
            self.assertTrue(ticks.wait(5))
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIn("RuntimeError", log.getvalue())
        self.assertNotIn("Ana's birthday", log.getvalue())

    def test_main_module_runs_the_server(self):
        import wazy.__main__ as entry
        self.assertIs(entry.main, server_module.main)


class Main(unittest.TestCase):
    def test_bad_setting_prints_a_clear_error_and_exits_2(self):
        error = ConfigError("WAZY_PORT must be a whole number from 1 to 65535 (got 'abc').")
        with mock.patch.object(server_module, "load_config", side_effect=error), \
                mock.patch("sys.stderr", new=io.StringIO()) as log:
            with self.assertRaises(SystemExit) as caught:
                server_module.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("WAZY_PORT", log.getvalue())

    def run_main(self, config: Config) -> Tuple[dict, str]:
        """Run main() with stand-ins for App, AIReader and WhatsApp; the server stops at once (Ctrl+C)."""
        built: dict = {"whatsapp": []}

        class App(FakeApp):
            def __init__(self, db_path: str, **options: object) -> None:
                super().__init__()
                built["app"] = self
                built["app_args"] = (db_path, options)

        class AIReader:
            def __init__(self, key: str, model: str) -> None:
                built["ai"] = (key, model)

        class WhatsApp(FakeWhatsApp):
            def __init__(self, **options: object) -> None:
                super().__init__()
                self.events: List[str] = []
                built["whatsapp"].append((self, options))

            def on_message(self, user_id: str, message_id: int, reply: Reply) -> None:
                pass

            def attach(self, app: object) -> None:
                self.events.append("attach")

            def start(self) -> None:
                self.events.append("start")

            def stop(self) -> None:
                self.events.append("stop")

        class Server:
            server_address = ("127.0.0.1", 8765)
            access_code = CODE

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                built["closed"] = True

        modules = {"wazy.app": types.SimpleNamespace(App=App), "wazy.ai": types.SimpleNamespace(AIReader=AIReader),
                   "wazy.whatsapp": types.SimpleNamespace(WhatsApp=WhatsApp)}
        with mock.patch.dict("sys.modules", modules), \
                mock.patch.object(server_module, "load_config", return_value=config), \
                mock.patch.object(server_module, "make_server", return_value=Server()), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            server_module.main()
        return built, out.getvalue()

    def test_plain_start_builds_the_app_without_ai_or_whatsapp(self):
        built, out = self.run_main(Config(db_path=":memory:", later_minutes=2, tz="UTC"))
        db_path, options = built["app_args"]
        self.assertEqual(db_path, ":memory:")
        self.assertEqual(options, {"default_tz": "UTC", "later_minutes": 2, "ai": None, "on_message": None})
        self.assertNotIn("ai", built)
        self.assertEqual(built["whatsapp"], [])
        self.assertIn("http://127.0.0.1:8765/", out)
        self.assertIn(f"?k={CODE}", out)
        self.assertTrue(built["closed"])
        self.assertIn("close", built["app"].names())
        self.assertIn("tick", built["app"].names())

    def test_ai_and_whatsapp_are_built_and_wired_when_set(self):
        config = Config(db_path=":memory:", anthropic_api_key="sk-ant-x", ai_model="m1",
                        twilio_account_sid="AC" + "0" * 32, twilio_auth_token="1" * 32,
                        twilio_from="whatsapp:+14155238886", twilio_content_sid="HX" + "2" * 32,
                        public_url="https://wazy.example.com", admin_code="admin-code-123")
        built, out = self.run_main(config)
        self.assertEqual(built["ai"], ("sk-ant-x", "m1"))
        whatsapp, options = built["whatsapp"][0]
        self.assertEqual(options, {"account_sid": "AC" + "0" * 32, "auth_token": "1" * 32,
                                   "from_number": "whatsapp:+14155238886", "public_url": "https://wazy.example.com",
                                   "content_sid": "HX" + "2" * 32})
        self.assertEqual(whatsapp.events, ["attach", "start", "stop"])
        self.assertEqual(built["app_args"][1]["on_message"], whatsapp.on_message)
        self.assertIsNotNone(built["app_args"][1]["ai"])
        self.assertIn(f"https://wazy.example.com/?k={CODE}", out)
        self.assertNotIn("admin-code-123", out)
        self.assertNotIn("sk-ant-x", out)
        self.assertNotIn("1" * 32, out)


if __name__ == "__main__":
    unittest.main()
