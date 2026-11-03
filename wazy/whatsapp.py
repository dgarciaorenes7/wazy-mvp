"""WhatsApp for Wazy, through Twilio (standard library only, no Twilio SDK).

This file does three jobs:

1. In. Twilio posts every WhatsApp message a person sends to /whatsapp/webhook. handle_webhook() checks
   Twilio's signature (so nobody else can pretend to be Twilio), ignores a message it has already handled
   (Twilio can post the same message twice), and passes the text, or a Done/Later button tap, to the App.
2. Queue. on_message() puts every message Wazy stores for a "whatsapp:" person into the outbox table.
   Nothing is sent from there, so a slow network never slows the conversation down.
3. Out. A background thread calls deliver_due() every 2 seconds. It claims each due outbox row (so a row is
   never sent twice, even with several threads), posts it to Twilio's REST API and records the result.
   A failed send is tried again after 30 s, 2 min, 10 min and 1 h. After 5 attempts the row is "failed".

Privacy: message text is never logged, and phone numbers are logged only as their last 3 digits.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, Tuple

from .db import to_db

log = logging.getLogger("wazy.whatsapp")

WEBHOOK_PATH = "/whatsapp/webhook"           # public_url + this is the address Twilio signs
API_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
MAX_BODY = 1600                              # Twilio's limit for one WhatsApp message, in characters
MAX_ATTEMPTS = 5                             # after the 5th failed attempt the row is marked "failed"
RETRY_AFTER = (30, 120, 600, 3600)           # seconds to wait after failed attempt 1, 2, 3 and 4
CLAIM_SECONDS = 300                          # while one sender is sending a row, others skip it for 5 min
SEND_EVERY_SECONDS = 2.0                     # how often the background thread looks for due rows
HTTP_TIMEOUT = 10.0                          # seconds to wait for Twilio before calling it a failure
BATCH = 50                                   # at most this many rows per deliver_due() call
NUDGE_SUFFIX = {"en": "Reply DONE or LATER", "es": "Responde HECHO o LUEGO"}   # plain-text nudges only

# Country code -> time zone for a NEW WhatsApp person (SPEC section 8). Anything else gets the default.
ZONES_BY_PREFIX = {
    "+34": "Europe/Madrid",
    "+39": "Europe/Rome",
    "+44": "Europe/London",
    "+33": "Europe/Paris",
    "+49": "Europe/Berlin",
    "+351": "Europe/Lisbon",
    "+41": "Europe/Zurich",
    "+31": "Europe/Amsterdam",
    "+32": "Europe/Brussels",
}


# ---------------------------------------------------------------------------------------------
# Small helpers that need no state
# ---------------------------------------------------------------------------------------------
def _values(value: Any) -> List[str]:
    """A form field's values as a list: one value normally, several if the field name was repeated."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _field(params: dict, name: str) -> str:
    """The first value of one form field, or "" if it is missing."""
    values = _values(params.get(name, ""))
    return values[0] if values else ""


def signature(url: str, params: dict, auth_token: str) -> str:
    """Twilio's signature: base64 of HMAC-SHA1(auth token, url + each param name and value, sorted by name).

    A repeated field name contributes each of its distinct values in sorted order, as Twilio's own
    library does. Sorting is plain Python string order, which is the case-sensitive order Twilio uses.
    """
    text = url
    for name in sorted(params):
        for value in sorted(set(_values(params[name]))):
            text += name + value
    digest = hmac.new(auth_token.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_signature(url: str, params: dict, auth_token: str, given: str) -> bool:
    """True only if `given` is exactly the signature Twilio would make. A missing token or signature is False."""
    if not auth_token or not isinstance(given, str) or not given:
        return False
    expected = signature(url, params, auth_token)
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))   # same time for any guess


def zone_for_number(number: str, default: str) -> str:
    """The time zone for a new WhatsApp person, from their country code: "+34600..." -> "Europe/Madrid"."""
    phone = number.strip().removeprefix("whatsapp:")
    for prefix in sorted(ZONES_BY_PREFIX, key=len, reverse=True):     # longest first: "+351" before "+35"
        if phone.startswith(prefix):
            return ZONES_BY_PREFIX[prefix]
    return default


def parse_button(payload: str) -> Optional[Tuple[str, int]]:
    """A button tap's payload: "done:12" -> ("done", 12), "later:7" -> ("later", 7). Anything else -> None."""
    action, _, number = payload.strip().partition(":")
    if action not in ("done", "later"):
        return None
    if not (number.isascii() and number.isdigit()) or len(number) > 9:   # plain digits, small enough for SQLite
        return None
    return action, int(number)


def is_whatsapp_id(user_id: str) -> bool:
    """True for "whatsapp:+" followed by 6 to 15 digits (an international phone number)."""
    digits = user_id[len("whatsapp:+"):]
    return user_id.startswith("whatsapp:+") and digits.isascii() and digits.isdigit() and 6 <= len(digits) <= 15


def mask(user_id: str) -> str:
    """A phone number safe to log: only its last 3 digits, e.g. "***123"."""
    return "***" + user_id[-3:] if len(user_id) > 3 else "***"


class SendFailed(Exception):
    """One send did not work. `code` is short and never contains message text: "http 500", "URLError", ..."""

    def __init__(self, code: str, permanent: bool) -> None:
        super().__init__(code)
        self.code = code
        self.permanent = permanent          # True: trying again cannot help (wrong number, bad credentials)


# ---------------------------------------------------------------------------------------------
# The WhatsApp channel
# ---------------------------------------------------------------------------------------------
class WhatsApp:
    """Twilio webhook in, outbox out. Build it, build the App with on_message=wa.on_message, then attach."""

    def __init__(self, *, account_sid: str, auth_token: str, from_number: str, public_url: str,
                 content_sid: str = "", opener: Optional[Callable[..., Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number                   # "whatsapp:+14155238886"
        self.content_sid = content_sid                   # Content Template with Done/Later buttons, or ""
        self.webhook_url = public_url.rstrip("/") + WEBHOOK_PATH
        self.api_url = API_URL.format(sid=urllib.parse.quote(account_sid, safe=""))
        self.opener = opener or urllib.request.urlopen   # tests pass a fake instead of the real network
        self.clock = clock
        self.app: Any = None
        self.interval = SEND_EVERY_SECONDS
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def attach(self, app: Any) -> None:
        """Connect to the App (called once). Without a clock of our own we use the App's clock."""
        self.app = app
        if self.clock is None:
            self.clock = getattr(app, "clock", None)

    # ----- time and database -----
    def _now(self) -> datetime:
        """Now, in UTC (adding seconds to UTC times is always exact, even on a clock-change day)."""
        if self.clock is not None:
            return self.clock().astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def _db(self) -> Any:
        if self.app is None:
            raise RuntimeError("WhatsApp.attach(app) must be called first")
        return self.app.db

    # ----- 1. messages coming in -----
    def handle_webhook(self, params: dict, signature: str) -> int:
        """Handle one Twilio form post. Returns the HTTP status: 403 for a bad signature, else 200.

        A Done/Later button ("done:12") goes to app.tap. If that nudge is no longer open, or the payload is
        not one of ours, the words of the message are read as ordinary text instead.
        """
        if not valid_signature(self.webhook_url, params, self.auth_token, signature):
            return 403
        db = self._db()
        if self._seen_before(db, _field(params, "MessageSid")):
            return 200                                    # Twilio sent this one before: already handled
        user_id = _field(params, "From").strip()
        if not is_whatsapp_id(user_id):
            return 200                                    # not a WhatsApp number: nothing to do
        text = _field(params, "Body").strip() or _field(params, "ButtonText").strip()
        button = parse_button(_field(params, "ButtonPayload"))
        if button is not None:
            action, commitment_id = button
            if self.app.tap(user_id, commitment_id, action):
                return 200                                # the tap answered that exact nudge
        if text:                                          # a photo with no caption has no text: nothing to do
            zone = zone_for_number(user_id, db.default_tz)
            self.app.handle(user_id, text, channel="whatsapp", tz=zone)
        return 200

    def _seen_before(self, db: Any, message_sid: str) -> bool:
        """Remember this Twilio message id. True if it was already there (so the message was handled)."""
        if not message_sid:
            return False                                  # Twilio always sends one; without it we cannot tell
        with db.transaction() as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO inbound_seen (provider_sid, created_at) VALUES (?, ?)",
                                  (message_sid, to_db(self._now())))
            return cursor.rowcount == 0

    # ----- 2. queueing Wazy's messages -----
    def on_message(self, user_id: str, message_id: int, reply: Any) -> None:
        """Queue one stored Wazy message for delivery, for WhatsApp people only. Never two rows per message."""
        if not str(user_id).startswith("whatsapp:"):
            return
        now = to_db(self._now())
        self._db().execute(
            "INSERT OR IGNORE INTO outbox (message_id, user_id, status, attempts, next_attempt_at, created_at) "
            "VALUES (?, ?, 'queued', 0, ?, ?)", (message_id, user_id, now, now))

    # ----- 3. sending -----
    def deliver_due(self) -> int:
        """Send every outbox row that is due, oldest first. Returns how many were sent."""
        db = self._db()
        now = to_db(self._now())
        db.execute("UPDATE outbox SET status = 'failed', last_error = 'interrupted' "    # the server stopped
                   "WHERE status = 'queued' AND attempts >= ? AND next_attempt_at <= ?",  # during a last attempt
                   (MAX_ATTEMPTS, now))
        due = db.query("SELECT id FROM outbox WHERE status = 'queued' AND next_attempt_at <= ? "
                       "ORDER BY id LIMIT ?", (now, BATCH))
        sent = 0
        for row in due:
            if self._claim(db, row["id"]) and self._send_row(db, row["id"]):
                sent += 1
        return sent

    def _claim(self, db: Any, outbox_id: int) -> bool:
        """Take one row for this thread: count the attempt and hide the row from other senders for a while.

        The UPDATE only changes the row if it is still queued and due, so when two threads try at once,
        exactly one of them sees "1 row changed" and sends it.
        """
        now = self._now()
        with db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ? "
                "WHERE id = ? AND status = 'queued' AND next_attempt_at <= ? AND attempts < ?",
                (to_db(now + timedelta(seconds=CLAIM_SECONDS)), outbox_id, to_db(now), MAX_ATTEMPTS))
            return cursor.rowcount == 1

    def _send_row(self, db: Any, outbox_id: int) -> bool:
        """Send one claimed row and record what happened. True if Twilio accepted it."""
        row = db.one("SELECT o.id, o.user_id, o.attempts, m.text, m.kind, m.lang, c.title "
                     "FROM outbox o JOIN messages m ON m.id = o.message_id "
                     "LEFT JOIN commitments c ON c.id = m.commitment_id WHERE o.id = ?", (outbox_id,))
        try:
            provider_sid = self._post(self.fields_for(row))
        except SendFailed as failure:
            self._record_failure(db, row, failure)
            return False
        db.execute("UPDATE outbox SET status = 'sent', provider_sid = ?, last_error = NULL WHERE id = ?",
                   (provider_sid, outbox_id))
        return True

    def fields_for(self, row: dict) -> dict:
        """The form fields Twilio needs for one message: From, To and either Body or a Content Template."""
        fields = {"From": self.from_number, "To": row["user_id"]}
        if row["kind"] == "nudge" and self.content_sid and row["title"]:
            fields["ContentSid"] = self.content_sid                   # the template with Done/Later buttons
            fields["ContentVariables"] = json.dumps({"1": row["title"]})
            return fields
        suffix = ""
        if row["kind"] == "nudge":                                    # no buttons: say how to answer
            suffix = "\n\n" + NUDGE_SUFFIX.get(row["lang"], NUDGE_SUFFIX["en"])
        fields["Body"] = row["text"][:MAX_BODY - len(suffix)] + suffix
        return fields

    def _post(self, fields: dict) -> str:
        """POST one message to Twilio. Returns Twilio's message id ("SM..."), or raises SendFailed."""
        login = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            self.api_url, data=urllib.parse.urlencode(fields).encode("utf-8"), method="POST",
            headers={"Authorization": "Basic " + login, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            response = self.opener(request, timeout=HTTP_TIMEOUT)
            try:
                status, body = response.status, response.read(65536)
            finally:
                response.close()
        except urllib.error.HTTPError as error:               # Twilio answered with an error status
            raise _http_failure(error.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:   # network, timeout
            raise SendFailed(type(error).__name__, False)
        if not 200 <= status < 300:
            raise _http_failure(status)
        return _message_sid(body)

    def _record_failure(self, db: Any, row: dict, failure: SendFailed) -> None:
        """Plan the next attempt (30 s, 2 min, 10 min, 1 h), or give up after the 5th or a permanent error."""
        attempts = row["attempts"]
        if failure.permanent or attempts >= MAX_ATTEMPTS:
            db.execute("UPDATE outbox SET status = 'failed', last_error = ? WHERE id = ?", (failure.code, row["id"]))
            outcome = "gave up"
        else:
            retry_at = self._now() + timedelta(seconds=RETRY_AFTER[attempts - 1])
            db.execute("UPDATE outbox SET next_attempt_at = ?, last_error = ? WHERE id = ?",
                       (to_db(retry_at), failure.code, row["id"]))
            outcome = "will retry"
        log.warning("WhatsApp message to %s not sent (%s), attempt %d of %d, %s",
                    mask(row["user_id"]), failure.code, attempts, MAX_ATTEMPTS, outcome)

    # ----- the background sender -----
    def start(self) -> None:
        """Start the sender thread (does nothing if it is already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stopping,),
                                        name="wazy-whatsapp-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the sender thread to finish and wait for it (at most a little longer than one send)."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=HTTP_TIMEOUT + 5)
            self._thread = None

    def _run(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            try:
                self.deliver_due()
            except Exception as error:      # keep the sender alive: the next round tries again
                log.warning("WhatsApp sender round failed (%s)", type(error).__name__)
            stopping.wait(self.interval)


def _http_failure(status: int) -> SendFailed:
    """An error status from Twilio. 4xx means the request itself is wrong (bad number, bad credentials),
    so trying again cannot help, except 408 (timeout) and 429 (too many requests). 5xx: try again later."""
    permanent = 400 <= status < 500 and status not in (408, 429)
    return SendFailed(f"http {status}", permanent)


def _message_sid(body: bytes) -> str:
    """Twilio's message id from a success answer. An answer without one counts as a failure.

    It is permanent (never retried): Twilio said yes, so trying again could send the message twice.
    """
    try:
        sid = json.loads(body.decode("utf-8")).get("sid")
    except (ValueError, AttributeError):
        sid = None
    if not isinstance(sid, str) or not sid:
        raise SendFailed("no_sid", True)
    return sid
