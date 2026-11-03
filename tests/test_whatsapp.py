"""Tests for wazy/whatsapp.py: Twilio signature, webhook, dedupe, outbox, delivery with retries, sender thread.

Nothing here touches the network: a fake opener stands in for urllib.request.urlopen, records every
request and answers from a script (a success, an HTTP error, a network error, ...). The App is a small
fake with a real in-memory Database, so the outbox and inbound_seen tables are the real ones.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import json
import logging
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from wazy import whatsapp
from wazy.db import Database, to_db
from wazy.types import Reply
from wazy.whatsapp import (
    API_URL, HTTP_TIMEOUT, MAX_BODY, ZONES_BY_PREFIX, WhatsApp, mask, parse_button, signature,
    valid_signature, zone_for_number,
)

MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=MADRID)            # a Tuesday morning
TOKEN = "f" * 32
ACCOUNT = "AC" + "0" * 32
FROM = "whatsapp:+14155238886"
PUBLIC = "https://wazy.example.com"
WEBHOOK = PUBLIC + "/whatsapp/webhook"
USER = "whatsapp:+34600000123"
TEMPLATE = "HX" + "1" * 32

# Twilio's published example (docs "Webhooks security" and the twilio-php/python validator tests).
EXAMPLE_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
EXAMPLE_PARAMS = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675309", "Digits": "1234",
                  "From": "+14158675309", "To": "+18005551212"}
EXAMPLE_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="

QUIET = logging.NullHandler()


def setUpModule() -> None:
    """Keep the expected failure warnings out of the test output (assertLogs still sees them)."""
    logger = logging.getLogger("wazy.whatsapp")
    logger.addHandler(QUIET)
    logger.propagate = False


def tearDownModule() -> None:
    logger = logging.getLogger("wazy.whatsapp")
    logger.removeHandler(QUIET)
    logger.propagate = True


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------
class FakeApp:
    """Just what whatsapp.py uses: .db, .clock, .handle, .tap. Records every call."""

    def __init__(self, default_tz: str = "Europe/Madrid") -> None:
        self.now = NOW
        self.db = Database(":memory:", default_tz=default_tz)
        self.handled = []
        self.taps = []
        self.tap_answer = [Reply("done_ack", "Nice one", "en", 12)]
        self.on_handle = None              # optional: a function run inside handle (e.g. store a reply)

    def clock(self) -> datetime:
        return self.now

    def handle(self, user_id, text, *, channel="web", tz=None):
        self.handled.append({"user_id": user_id, "text": text, "channel": channel, "tz": tz})
        if self.on_handle:
            self.on_handle(user_id, text)
        return []

    def tap(self, user_id, commitment_id, action):
        self.taps.append((user_id, commitment_id, action))
        return list(self.tap_answer)


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Stands in for urllib.request.urlopen. Script items: "ok", an exception to raise, or a FakeResponse."""

    def __init__(self, *script, delay: float = 0.0) -> None:
        self.script = list(script)
        self.delay = delay
        self.requests = []
        self.timeouts = []
        self.responses = []
        self.lock = threading.Lock()

    def __call__(self, request, timeout=None):
        with self.lock:
            self.requests.append(request)
            self.timeouts.append(timeout)
            outcome = self.script.pop(0) if self.script else "ok"
            number = len(self.requests)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "ok":
            outcome = FakeResponse(201, json.dumps({"sid": "SM%032d" % number, "status": "queued"}).encode())
        self.responses.append(outcome)
        return outcome

    def forms(self) -> list:
        return [dict(urllib.parse.parse_qsl(r.data.decode("utf-8"))) for r in self.requests]


def http_error(code: int) -> urllib.error.HTTPError:
    """An HTTP error whose message and body contain message text (which must never be stored)."""
    body = io.BytesIO(b'{"message": "Call mum secret", "code": 21211}')
    return urllib.error.HTTPError(API_URL, code, "Call mum secret", {}, body)


def inbound(body: str = "Call mum at 5", sid: str = "SM" + "a" * 32, sender: str = USER, **extra) -> dict:
    """The form fields Twilio posts for one incoming WhatsApp message."""
    params = {"MessageSid": sid, "SmsMessageSid": sid, "AccountSid": ACCOUNT, "From": sender, "To": FROM,
              "Body": body, "NumMedia": "0", "ProfileName": "Marta", "WaId": sender.split("+")[-1]}
    params.update(extra)
    return params


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FakeApp()
        self.opener = FakeOpener()
        self.wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC,
                           opener=self.opener)
        self.wa.attach(self.app)

    def tearDown(self) -> None:
        self.wa.stop()
        self.app.db.close()

    def post(self, params: dict, token: str = TOKEN, url: str = WEBHOOK) -> int:
        return self.wa.handle_webhook(params, signature(url, params, token))

    def queue(self, text: str = "Got it", kind: str = "confirm", lang: str = "en", user: str = USER,
              commitment_id=None) -> int:
        """Store one Wazy message the way conversation.py does, then tell whatsapp.py about it."""
        self.app.db.user(user, self.app.now)
        message_id = self.app.db.add_message(user, "bot", text, kind, lang, commitment_id, self.app.now)
        self.wa.on_message(user, message_id, Reply(kind, text, lang, commitment_id, kind == "nudge"))
        return message_id

    def add_commitment(self, title: str, user: str = USER) -> int:
        self.app.db.user(user, self.app.now)
        cursor = self.app.db.execute(
            "INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, created_at) "
            "VALUES (?, ?, ?, ?, 'nudged', ?)", (user, title, title, to_db(self.app.now), to_db(self.app.now)))
        return int(cursor.lastrowid)

    def outbox(self) -> list:
        return self.app.db.query("SELECT * FROM outbox ORDER BY id")

    def advance(self, seconds: float) -> None:
        self.app.now = self.app.now + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------------------------
class SignatureTests(unittest.TestCase):
    def test_twilio_published_example(self):
        self.assertEqual(signature(EXAMPLE_URL, EXAMPLE_PARAMS, "12345"), EXAMPLE_SIGNATURE)

    def test_published_example_is_valid(self):
        self.assertTrue(valid_signature(EXAMPLE_URL, EXAMPLE_PARAMS, "12345", EXAMPLE_SIGNATURE))

    def test_round_trip_whatsapp_params(self):
        params = inbound("recuérdame llamar a mamá mañana a las 9 😊")
        self.assertTrue(valid_signature(WEBHOOK, params, TOKEN, signature(WEBHOOK, params, TOKEN)))

    def test_signature_is_base64_of_sha1(self):
        raw = base64.b64decode(signature(WEBHOOK, inbound(), TOKEN))
        self.assertEqual(len(raw), 20)

    def test_matches_hand_built_string_with_case_sensitive_sort(self):
        params = {"b": "1", "A": "2", "a": "3"}           # Unix order: "A" < "a" < "b"
        text = WEBHOOK + "A2" + "a3" + "b1"
        expected = base64.b64encode(hmac.new(TOKEN.encode(), text.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(signature(WEBHOOK, params, TOKEN), expected)

    def test_order_of_params_does_not_matter(self):
        params = inbound()
        backwards = dict(reversed(list(params.items())))
        self.assertEqual(signature(WEBHOOK, params, TOKEN), signature(WEBHOOK, backwards, TOKEN))

    def test_list_values_from_parse_qs_give_same_signature(self):
        params = inbound()
        as_lists = {name: [value] for name, value in params.items()}
        self.assertEqual(signature(WEBHOOK, params, TOKEN), signature(WEBHOOK, as_lists, TOKEN))

    def test_repeated_field_values_are_sorted(self):
        text = WEBHOOK + "X1X2"
        expected = base64.b64encode(hmac.new(TOKEN.encode(), text.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(signature(WEBHOOK, {"X": ["2", "1"]}, TOKEN), expected)

    def test_tampered_param_value_is_rejected(self):
        params = inbound("Call mum at 5")
        given = signature(WEBHOOK, params, TOKEN)
        params["Body"] = "Call mum at 6"
        self.assertFalse(valid_signature(WEBHOOK, params, TOKEN, given))

    def test_added_or_removed_param_is_rejected(self):
        params = inbound()
        given = signature(WEBHOOK, params, TOKEN)
        self.assertFalse(valid_signature(WEBHOOK, dict(params, ButtonPayload="done:1"), TOKEN, given))
        smaller = dict(params)
        del smaller["ProfileName"]
        self.assertFalse(valid_signature(WEBHOOK, smaller, TOKEN, given))

    def test_tampered_url_is_rejected(self):
        params = inbound()
        given = signature(WEBHOOK, params, TOKEN)
        for url in (WEBHOOK + "/", WEBHOOK.replace("https", "http"), WEBHOOK + "?x=1", PUBLIC):
            with self.subTest(url=url):
                self.assertFalse(valid_signature(url, params, TOKEN, given))

    def test_wrong_token_is_rejected(self):
        params = inbound()
        self.assertFalse(valid_signature(WEBHOOK, params, TOKEN, signature(WEBHOOK, params, "e" * 32)))

    def test_missing_signature_is_rejected(self):
        for given in ("", None):
            with self.subTest(given=given):
                self.assertFalse(valid_signature(WEBHOOK, inbound(), TOKEN, given))

    def test_empty_token_is_never_valid(self):
        params = inbound()
        self.assertFalse(valid_signature(WEBHOOK, params, "", signature(WEBHOOK, params, "")))

    def test_non_ascii_signature_is_false_not_an_error(self):
        self.assertFalse(valid_signature(WEBHOOK, inbound(), TOKEN, "ñññ=="))


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
class ZoneTests(unittest.TestCase):
    def test_every_country_in_the_table(self):
        cases = {"+34600000000": "Europe/Madrid", "+393331234567": "Europe/Rome", "+447700900123": "Europe/London",
                 "+33612345678": "Europe/Paris", "+4915112345678": "Europe/Berlin",
                 "+351912345678": "Europe/Lisbon", "+41791234567": "Europe/Zurich",
                 "+31612345678": "Europe/Amsterdam", "+32470123456": "Europe/Brussels"}
        for number, zone in cases.items():
            with self.subTest(number=number):
                self.assertEqual(zone_for_number(number, "UTC"), zone)
                ZoneInfo(zone)                                     # a real zone name

    def test_whatsapp_prefix_is_ignored(self):
        self.assertEqual(zone_for_number("whatsapp:+34600000000", "UTC"), "Europe/Madrid")
        self.assertEqual(zone_for_number("whatsapp:+351912345678", "UTC"), "Europe/Lisbon")

    def test_unknown_country_gets_the_default(self):
        for number in ("+14155238886", "+353861234567", "+5511999999999", "+3", "", "whatsapp:+1555"):
            with self.subTest(number=number):
                self.assertEqual(zone_for_number(number, "America/New_York"), "America/New_York")

    def test_number_without_plus_gets_the_default(self):
        self.assertEqual(zone_for_number("34600000000", "UTC"), "UTC")

    def test_longest_prefix_wins_whatever_the_table_order(self):
        table = {"+35": "Europe/Dublin", "+351": "Europe/Lisbon"}      # the short one first on purpose
        with mock.patch.dict(ZONES_BY_PREFIX, table, clear=True):
            self.assertEqual(zone_for_number("+351912345678", "UTC"), "Europe/Lisbon")
            self.assertEqual(zone_for_number("+353861234567", "UTC"), "Europe/Dublin")


class ButtonTests(unittest.TestCase):
    def test_done_and_later(self):
        self.assertEqual(parse_button("done:12"), ("done", 12))
        self.assertEqual(parse_button("later:7"), ("later", 7))
        self.assertEqual(parse_button(" done:12 "), ("done", 12))
        self.assertEqual(parse_button("done:123456789"), ("done", 123456789))

    def test_bad_payloads(self):
        for payload in ("", "done", "done:", ":12", "done:abc", "explode:1", "DONE:12", "done:-1", "done:1.5",
                        "done: 12", "done:12:3", "done:²", "done:١٢", "done:1234567890", "stop:1"):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_button(payload))

    def test_mask_keeps_last_three_digits_only(self):
        self.assertEqual(mask(USER), "***123")
        self.assertEqual(mask("12"), "***")


# ---------------------------------------------------------------------------------------------
# Webhook (messages coming in)
# ---------------------------------------------------------------------------------------------
class WebhookTests(Base):
    def test_text_message_is_handled(self):
        self.assertEqual(self.post(inbound("Call mum at 5")), 200)
        self.assertEqual(self.app.handled, [{"user_id": USER, "text": "Call mum at 5", "channel": "whatsapp",
                                             "tz": "Europe/Madrid"}])

    def test_zone_comes_from_the_country_code(self):
        self.post(inbound("hi", sender="whatsapp:+447700900123"))
        self.assertEqual(self.app.handled[0]["tz"], "Europe/London")
        self.assertEqual(self.app.handled[0]["user_id"], "whatsapp:+447700900123")

    def test_unknown_country_uses_the_app_default_zone(self):
        app = FakeApp(default_tz="Atlantic/Canary")
        self.wa.attach(app)
        self.post(inbound("hi", sender="whatsapp:+14155550100"))
        self.assertEqual(app.handled[0]["tz"], "Atlantic/Canary")
        app.db.close()

    def test_bad_signature_is_403_and_nothing_happens(self):
        status = self.wa.handle_webhook(inbound(), "AAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        self.assertEqual(status, 403)
        self.assertEqual(self.app.handled, [])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM inbound_seen"), 0)

    def test_missing_signature_is_403(self):
        self.assertEqual(self.wa.handle_webhook(inbound(), ""), 403)
        self.assertEqual(self.wa.handle_webhook(inbound(), None), 403)
        self.assertEqual(self.app.handled, [])

    def test_signed_with_wrong_token_or_url_is_403(self):
        self.assertEqual(self.post(inbound(), token="e" * 32), 403)
        self.assertEqual(self.post(inbound(), url=PUBLIC), 403)
        self.assertEqual(self.app.handled, [])

    def test_tampered_body_is_403(self):
        params = inbound("Call mum at 5")
        given = signature(WEBHOOK, params, TOKEN)
        params["Body"] = "STOP"
        self.assertEqual(self.wa.handle_webhook(params, given), 403)
        self.assertEqual(self.app.handled, [])

    def test_trailing_slash_in_public_url_is_ignored(self):
        wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC + "/")
        self.assertEqual(wa.webhook_url, WEBHOOK)

    def test_duplicate_message_sid_is_handled_once(self):
        self.assertEqual(self.post(inbound("Call mum at 5")), 200)
        self.assertEqual(self.post(inbound("Call mum at 5")), 200)
        self.assertEqual(len(self.app.handled), 1)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM inbound_seen"), 1)

    def test_different_message_sids_are_both_handled(self):
        self.post(inbound("one", sid="SM1"))
        self.post(inbound("two", sid="SM2"))
        self.assertEqual([h["text"] for h in self.app.handled], ["one", "two"])

    def test_duplicate_tap_is_handled_once(self):
        params = inbound("Done", ButtonPayload="done:12", ButtonText="Done")
        self.post(params)
        self.post(params)
        self.assertEqual(self.app.taps, [(USER, 12, "done")])

    def test_rejected_post_does_not_block_the_real_one(self):
        self.wa.handle_webhook(inbound("hi"), "bad")
        self.post(inbound("hi"))
        self.assertEqual(len(self.app.handled), 1)

    def test_message_without_sid_is_still_handled(self):
        params = inbound("hi")
        del params["MessageSid"]
        self.post(params)
        self.post(params)
        self.assertEqual(len(self.app.handled), 2)

    def test_done_button_taps_that_commitment(self):
        self.assertEqual(self.post(inbound("Done", ButtonPayload="done:12", ButtonText="Done")), 200)
        self.assertEqual(self.app.taps, [(USER, 12, "done")])
        self.assertEqual(self.app.handled, [])

    def test_later_button_taps_that_commitment(self):
        self.post(inbound("Later", ButtonPayload="later:7", ButtonText="Later"))
        self.assertEqual(self.app.taps, [(USER, 7, "later")])
        self.assertEqual(self.app.handled, [])

    def test_tap_that_is_not_open_falls_back_to_text(self):
        self.app.tap_answer = []
        self.post(inbound("Done", ButtonPayload="done:12", ButtonText="Done"))
        self.assertEqual(self.app.taps, [(USER, 12, "done")])
        self.assertEqual(self.app.handled[0]["text"], "Done")

    def test_fallback_uses_button_text_when_body_is_empty(self):
        self.app.tap_answer = []
        self.post(inbound("", ButtonPayload="later:3", ButtonText="Later"))
        self.assertEqual(self.app.handled[0]["text"], "Later")

    def test_bad_payloads_are_read_as_text(self):
        for number, payload in enumerate(("done:abc", "explode:1", "done", "later:-4", "done:99999999999")):
            with self.subTest(payload=payload):
                self.post(inbound("Done", sid=f"SMbad{number}", ButtonPayload=payload, ButtonText="Done"))
        self.assertEqual(self.app.taps, [])
        self.assertEqual([h["text"] for h in self.app.handled], ["Done"] * 5)

    def test_media_only_does_nothing(self):
        self.assertEqual(self.post(inbound("", NumMedia="1", MediaUrl0="https://api.twilio.com/x.jpg")), 200)
        self.assertEqual(self.app.handled, [])
        self.assertEqual(self.app.taps, [])

    def test_media_with_caption_is_handled(self):
        self.post(inbound("Pay this bill tomorrow", NumMedia="1", MediaUrl0="https://api.twilio.com/x.jpg"))
        self.assertEqual(self.app.handled[0]["text"], "Pay this bill tomorrow")

    def test_blank_body_does_nothing(self):
        self.assertEqual(self.post(inbound("   ")), 200)
        self.assertEqual(self.app.handled, [])

    def test_sender_that_is_not_whatsapp_is_ignored(self):
        for number, sender in enumerate(("+34600000123", "whatsapp:+34abc", "whatsapp:34600000123", "",
                                         "whatsapp:+1", "web:local")):
            with self.subTest(sender=sender):
                self.assertEqual(self.post(inbound("hi", sid=f"SMx{number}", sender=sender)), 200)
        self.assertEqual(self.app.handled, [])

    def test_parse_qs_style_params_work(self):
        params = {name: [value] for name, value in inbound("Call mum at 5").items()}
        self.assertEqual(self.post(params), 200)
        self.assertEqual(self.app.handled[0]["text"], "Call mum at 5")

    def test_not_attached_is_a_bug_but_bad_signature_is_still_403(self):
        wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC)
        self.assertEqual(wa.handle_webhook(inbound(), "bad"), 403)
        with self.assertRaises(RuntimeError):
            wa.handle_webhook(inbound(), signature(WEBHOOK, inbound(), TOKEN))

    def test_reply_goes_to_the_outbox_not_the_http_answer(self):
        def store_reply(user_id, text):
            message_id = self.app.db.add_message(user_id, "bot", "Got it", "confirm", "en", None, self.app.now)
            self.wa.on_message(user_id, message_id, Reply("confirm", "Got it"))

        self.app.db.user(USER, NOW)
        self.app.on_handle = store_reply
        self.post(inbound("Call mum at 5"))
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.opener.requests, [])                  # nothing sent during the webhook
        self.assertEqual(self.wa.deliver_due(), 1)
        self.assertEqual(self.opener.forms()[0]["Body"], "Got it")


# ---------------------------------------------------------------------------------------------
# Outbox (queueing)
# ---------------------------------------------------------------------------------------------
class OutboxTests(Base):
    def test_whatsapp_message_is_queued(self):
        message_id = self.queue("Got it")
        rows = self.outbox()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["message_id"], row["user_id"], row["status"], row["attempts"]),
                         (message_id, USER, "queued", 0))
        self.assertEqual(row["next_attempt_at"], to_db(NOW))
        self.assertEqual(row["created_at"], to_db(NOW))
        self.assertIsNone(row["provider_sid"])

    def test_web_user_is_never_queued(self):
        self.queue("Got it", user="web:local")
        self.queue("Got it", user="web:abc123")
        self.assertEqual(self.outbox(), [])

    def test_same_message_twice_gives_one_row(self):
        message_id = self.queue("Got it")
        self.wa.on_message(USER, message_id, Reply("confirm", "Got it"))
        self.assertEqual(len(self.outbox()), 1)

    def test_web_user_needs_no_attached_app(self):
        wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC)
        wa.on_message("web:local", 1, Reply("confirm", "Got it"))      # quietly nothing

    def test_explicit_clock_wins_over_the_app_clock(self):
        later = NOW + timedelta(hours=3)
        wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC,
                      clock=lambda: later)
        wa.attach(self.app)
        self.app.db.user(USER, NOW)
        message_id = self.app.db.add_message(USER, "bot", "Got it", "confirm", "en", None, NOW)
        wa.on_message(USER, message_id, Reply("confirm", "Got it"))
        self.assertEqual(self.outbox()[0]["next_attempt_at"], to_db(later))


# ---------------------------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------------------------
class DeliveryTests(Base):
    def test_plain_message_is_sent(self):
        self.queue("Got it: “Call mum”. I'll nudge you today at 17:00.")
        self.assertEqual(self.wa.deliver_due(), 1)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, API_URL.format(sid=ACCOUNT))
        self.assertEqual(request.get_method(), "POST")
        login = base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
        self.assertEqual(request.get_header("Authorization"), "Basic " + login)
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(self.opener.timeouts, [HTTP_TIMEOUT])
        self.assertEqual(self.opener.forms()[0], {"From": FROM, "To": USER,
                                                  "Body": "Got it: “Call mum”. I'll nudge you today at 17:00."})
        self.assertTrue(self.opener.responses[0].closed)

    def test_success_records_sent_and_provider_sid(self):
        self.queue()
        self.wa.deliver_due()
        row = self.outbox()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["provider_sid"], "SM%032d" % 1)
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["last_error"])

    def test_sent_row_is_never_sent_again(self):
        self.queue()
        self.wa.deliver_due()
        self.advance(86400)
        self.assertEqual(self.wa.deliver_due(), 0)
        self.assertEqual(len(self.opener.requests), 1)

    def test_rows_go_out_oldest_first(self):
        for text in ("first", "second", "third"):
            self.queue(text)
        self.assertEqual(self.wa.deliver_due(), 3)
        self.assertEqual([f["Body"] for f in self.opener.forms()], ["first", "second", "third"])

    def test_row_not_yet_due_waits(self):
        self.queue()
        self.app.db.execute("UPDATE outbox SET next_attempt_at = ?", (to_db(NOW + timedelta(seconds=10)),))
        self.assertEqual(self.wa.deliver_due(), 0)
        self.advance(10)
        self.assertEqual(self.wa.deliver_due(), 1)

    def test_plain_nudge_says_how_to_answer(self):
        cid = self.add_commitment("Call mum")
        self.queue("Time for “Call mum”.", kind="nudge", commitment_id=cid)
        self.wa.deliver_due()
        self.assertEqual(self.opener.forms()[0]["Body"], "Time for “Call mum”.\n\nReply DONE or LATER")

    def test_spanish_nudge_suffix(self):
        cid = self.add_commitment("Llamar a mamá")
        self.queue("Es la hora de “Llamar a mamá”.", kind="nudge", lang="es", commitment_id=cid)
        self.wa.deliver_due()
        self.assertTrue(self.opener.forms()[0]["Body"].endswith("\n\nResponde HECHO o MÁS TARDE"))

    def test_other_kinds_get_no_suffix(self):
        self.queue("Done. Nice one.", kind="done_ack")
        self.wa.deliver_due()
        self.assertEqual(self.opener.forms()[0]["Body"], "Done. Nice one.")

    def test_template_nudge_uses_content_sid_and_title(self):
        self.wa.content_sid = TEMPLATE
        cid = self.add_commitment("Llamar a mamá")
        self.queue("Es la hora de “Llamar a mamá”.", kind="nudge", lang="es", commitment_id=cid)
        self.wa.deliver_due()
        form = self.opener.forms()[0]
        self.assertEqual(form["ContentSid"], TEMPLATE)
        self.assertEqual(json.loads(form["ContentVariables"]), {"1": "Llamar a mamá"})
        self.assertNotIn("Body", form)
        self.assertEqual((form["From"], form["To"]), (FROM, USER))

    def test_template_is_only_for_nudges(self):
        self.wa.content_sid = TEMPLATE
        self.queue("Got it", kind="confirm")
        self.wa.deliver_due()
        form = self.opener.forms()[0]
        self.assertEqual(form["Body"], "Got it")
        self.assertNotIn("ContentSid", form)

    def test_template_nudge_without_a_commitment_falls_back_to_text(self):
        self.wa.content_sid = TEMPLATE
        self.queue("Time for it.", kind="nudge", commitment_id=None)
        self.wa.deliver_due()
        form = self.opener.forms()[0]
        self.assertNotIn("ContentSid", form)
        self.assertTrue(form["Body"].endswith("Reply DONE or LATER"))

    def test_long_message_is_capped_at_1600(self):
        self.queue("a" * 2000)
        self.wa.deliver_due()
        self.assertEqual(self.opener.forms()[0]["Body"], "a" * MAX_BODY)

    def test_message_of_exactly_1600_is_kept_whole(self):
        self.queue("b" * 1600)
        self.wa.deliver_due()
        self.assertEqual(self.opener.forms()[0]["Body"], "b" * 1600)

    def test_long_nudge_keeps_its_suffix_within_1600(self):
        cid = self.add_commitment("Call mum")
        self.queue("n" * 2000, kind="nudge", commitment_id=cid)
        self.wa.deliver_due()
        body = self.opener.forms()[0]["Body"]
        self.assertEqual(len(body), MAX_BODY)
        self.assertTrue(body.endswith("Reply DONE or LATER"))

    def test_retry_schedule_then_failed_after_5_attempts(self):
        self.opener.script = [http_error(500) for _ in range(5)]
        self.queue()
        for attempt, wait in enumerate((30, 120, 600, 3600), start=1):
            self.assertEqual(self.wa.deliver_due(), 0)
            row = self.outbox()[0]
            self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("queued", attempt, "http 500"))
            self.assertEqual(row["next_attempt_at"], to_db(self.app.now + timedelta(seconds=wait)))
            self.advance(wait - 1)
            self.wa.deliver_due()                                    # one second early: nothing
            self.assertEqual(len(self.opener.requests), attempt)
            self.advance(1)
        self.assertEqual(self.wa.deliver_due(), 0)                   # the 5th attempt
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("failed", 5, "http 500"))
        self.advance(7 * 86400)
        self.wa.deliver_due()
        self.assertEqual(len(self.opener.requests), 5)

    def test_success_after_a_failure_clears_the_error(self):
        self.opener.script = [urllib.error.URLError("no route")]
        self.queue()
        self.wa.deliver_due()
        self.advance(30)
        self.assertEqual(self.wa.deliver_due(), 1)
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("sent", 2, None))

    def test_network_errors_are_retried(self):
        errors = {"URLError": urllib.error.URLError("Call mum secret"),
                  "ConnectionResetError": ConnectionResetError("Call mum secret"),
                  "RemoteDisconnected": http.client.RemoteDisconnected("Call mum secret"),
                  "IncompleteRead": http.client.IncompleteRead(b"Call mum secret")}
        for name, error in errors.items():
            with self.subTest(error=name):
                self.app.db.execute("DELETE FROM outbox")
                self.opener.script = [error]
                self.queue()
                self.wa.deliver_due()
                row = self.outbox()[0]
                self.assertEqual((row["status"], row["last_error"]), ("queued", name))

    def test_timeout_is_retried(self):
        self.opener.script = [socket.timeout("timed out")]
        self.queue()
        self.wa.deliver_due()
        row = self.outbox()[0]
        self.assertEqual(row["status"], "queued")
        self.assertIn(row["last_error"], ("timeout", "TimeoutError"))   # Python 3.9 / 3.10+ class name
        self.assertEqual(row["next_attempt_at"], to_db(NOW + timedelta(seconds=30)))

    def test_429_and_408_and_5xx_are_retried(self):
        for code in (408, 429, 500, 502, 503):
            with self.subTest(code=code):
                self.app.db.execute("DELETE FROM outbox")
                self.opener.script = [http_error(code)]
                self.queue()
                self.wa.deliver_due()
                row = self.outbox()[0]
                self.assertEqual((row["status"], row["last_error"]), ("queued", f"http {code}"))

    def test_other_4xx_fail_at_once(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                self.app.db.execute("DELETE FROM outbox")
                self.opener.script = [http_error(code)]
                self.queue()
                self.wa.deliver_due()
                row = self.outbox()[0]
                self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("failed", 1, f"http {code}"))

    def test_error_status_returned_without_raising_is_handled_the_same(self):
        self.opener.script = [FakeResponse(503, b"")]
        self.queue()
        self.wa.deliver_due()
        self.assertEqual(self.outbox()[0]["status"], "queued")

    def test_success_without_sid_fails_without_retry(self):
        for body in (b'{"status": "queued"}', b"not json", b"[]", b'{"sid": 12}', b"\xff"):
            with self.subTest(body=body):
                self.app.db.execute("DELETE FROM outbox")
                self.opener.script = [FakeResponse(201, body)]
                self.queue()
                self.assertEqual(self.wa.deliver_due(), 0)
                row = self.outbox()[0]
                self.assertEqual((row["status"], row["last_error"]), ("failed", "no_sid"))

    def test_last_error_never_holds_message_text(self):
        self.opener.script = [http_error(500), urllib.error.URLError("Call mum secret"), OSError("Call mum secret")]
        self.queue("Call mum secret")
        for _ in range(3):
            self.wa.deliver_due()
            self.assertNotIn("secret", self.outbox()[0]["last_error"])
            self.advance(3600)

    def test_logs_never_hold_text_or_full_number(self):
        self.opener.script = [urllib.error.URLError("Call mum secret"), http_error(400)]
        with self.assertLogs("wazy.whatsapp", level="WARNING") as captured:
            self.queue("Call mum secret")
            self.wa.deliver_due()
            self.advance(30)
            self.wa.deliver_due()
        output = "\n".join(captured.output)
        self.assertNotIn("secret", output)
        self.assertNotIn("600000123", output)
        self.assertIn("***123", output)

    def test_unexpected_error_leaves_the_row_claimed_for_5_minutes(self):
        self.opener.script = [RuntimeError("bug")]
        self.queue()
        with self.assertRaises(RuntimeError):
            self.wa.deliver_due()
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["attempts"]), ("queued", 1))
        self.advance(299)
        self.assertEqual(self.wa.deliver_due(), 0)
        self.advance(1)
        self.assertEqual(self.wa.deliver_due(), 1)
        self.assertEqual(len(self.opener.requests), 2)

    def test_last_attempt_interrupted_is_marked_failed_without_sending(self):
        self.queue()
        self.app.db.execute("UPDATE outbox SET attempts = 5")
        self.assertEqual(self.wa.deliver_due(), 0)
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["last_error"]), ("failed", "interrupted"))
        self.assertEqual(self.opener.requests, [])

    def test_deliver_needs_an_attached_app(self):
        wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC)
        with self.assertRaises(RuntimeError):
            wa.deliver_due()


class ConcurrencyTests(Base):
    def run_threads(self, count: int = 8) -> list:
        barrier = threading.Barrier(count)
        results = []

        def worker():
            barrier.wait()
            results.append(self.wa.deliver_due())

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return results

    def test_eight_threads_never_send_a_row_twice(self):
        for number in range(40):
            self.queue(f"message {number}")
        self.opener.delay = 0.002
        results = self.run_threads()
        bodies = [form["Body"] for form in self.opener.forms()]
        self.assertEqual(sum(results), 40)
        self.assertEqual(len(bodies), 40)
        self.assertEqual(len(set(bodies)), 40)
        self.assertEqual({(r["status"], r["attempts"]) for r in self.outbox()}, {("sent", 1)})

    def test_eight_threads_failing_try_each_row_once(self):
        for number in range(20):
            self.queue(f"message {number}")
        self.opener.script = [http_error(500) for _ in range(100)]
        self.opener.delay = 0.002
        self.assertEqual(sum(self.run_threads()), 0)
        self.assertEqual(len(self.opener.requests), 20)
        self.assertEqual({(r["status"], r["attempts"]) for r in self.outbox()}, {("queued", 1)})

    def test_sender_thread_and_callers_together_send_once(self):
        for number in range(30):
            self.queue(f"message {number}")
        self.opener.delay = 0.001
        self.wa.interval = 0.001
        self.wa.start()
        self.run_threads(4)
        self.wa.stop()
        bodies = [form["Body"] for form in self.opener.forms()]
        self.assertEqual(sorted(bodies), sorted(f"message {n}" for n in range(30)))


# ---------------------------------------------------------------------------------------------
# Background sender
# ---------------------------------------------------------------------------------------------
class SenderThreadTests(Base):
    def wait_for(self, condition, seconds: float = 3.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_start_delivers_and_stop_ends_the_thread(self):
        self.wa.interval = 0.01
        self.wa.start()
        self.queue("Got it")
        self.assertTrue(self.wait_for(lambda: self.outbox()[0]["status"] == "sent"))
        thread = self.wa._thread
        self.wa.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.wa._thread)

    def test_thread_is_a_daemon(self):
        self.wa.start()
        self.assertTrue(self.wa._thread.daemon)

    def test_start_twice_keeps_one_thread(self):
        self.wa.start()
        first = self.wa._thread
        self.wa.start()
        self.assertIs(self.wa._thread, first)

    def test_stop_without_start_is_fine(self):
        self.wa.stop()
        self.wa.stop()

    def test_restart_after_stop(self):
        self.wa.interval = 0.01
        self.wa.start()
        self.wa.stop()
        self.wa.start()
        self.queue("again")
        self.assertTrue(self.wait_for(lambda: self.outbox()[0]["status"] == "sent"))

    def test_sender_survives_an_error(self):
        self.wa.interval = 0.01
        self.opener.script = [RuntimeError("bug")]
        with self.assertLogs("wazy.whatsapp", level="WARNING") as captured:
            self.queue("first")
            self.wa.start()
            self.assertTrue(self.wait_for(lambda: len(self.opener.requests) >= 1))
            self.queue("second")
            self.assertTrue(self.wait_for(lambda: self.outbox()[1]["status"] == "sent"))
        self.assertIn("RuntimeError", "\n".join(captured.output))

    def test_default_interval_is_two_seconds(self):
        self.assertEqual(whatsapp.SEND_EVERY_SECONDS, 2.0)
        self.assertEqual(self.wa.interval, 2.0)


if __name__ == "__main__":
    unittest.main()
