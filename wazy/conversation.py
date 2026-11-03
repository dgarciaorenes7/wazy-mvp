"""The conversation: what Wazy does with each message a person sends (SPEC section 6).

app.py hands every incoming message to Conversation.handle() and every Done / Later button to
Conversation.tap() (SPEC 6.2), always with "now" from its clock. To answer, this file asks intents.py what
kind of message it is, lang.py which language it is in, timeparse.py and title.py when and what the
commitment is, learning.py about the person's usual hour, and replies.py for the words. Everything it
remembers between messages is in the database (db.py).

handle() cleans the text, stores it, and then walks the numbered list of SPEC 6 from the top, stopping at the
first step that applies:

     1. an empty message                  -> no reply
     2. too many messages in a minute     -> "slow down" (once a minute)
     3. STOP                              -> every nudge stops
     4. START                             -> nudges are back on
     5. help / hello                      -> help / welcome
     6. YES or NO to a learning question  -> learning answer
     7. what did you learn / forget / learn again -> learning
     8. done / later                      -> answers the latest open nudge
     9. list                              -> the next 10 commitments, numbered
    10. cancel (2)                        -> cancels one commitment (or the question Wazy is waiting on)
    11. never mind (while Wazy waits)     -> drops the wait
    12. an instruction aimed at Wazy      -> "I only take commitments"
    13. a question or an edit             -> "I can't do that yet"
    14. anything else                     -> read it as a commitment (time, title) and confirm it,
                                             or say exactly what is wrong with the time and wait for a fix

Every message the person sends is stored. Every message Wazy says is stored and then handed to on_message,
so WhatsApp can deliver it. Changes that touch several rows (STOP, cancel, create, Done/Later) happen in one
transaction, so they happen completely or not at all.
"""
from __future__ import annotations

import json
import logging
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Callable, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from . import learning
from .db import Database, from_db, to_db
from .intents import classify
from .lang import detect_language
from .replies import describe_when, render
from .scheduler import Scheduler
from .timeparse import DEFAULT_HOUR, parse_time
from .title import MAX_LENGTH as MAX_TITLE_LENGTH, make_title
from .types import Intent, Reply, TimeResult

log = logging.getLogger(__name__)

MAX_LENGTH = 500                        # characters: a longer message gets "too_long" and nothing else (SPEC 6)
RATE_LIMIT = 20                         # messages per RATE_WINDOW; one more is too many (SPEC 6 step 2)
RATE_WINDOW = timedelta(seconds=60)     # the window the rate limit counts in (SPEC 6 step 2)
WAIT_EXPIRES = timedelta(hours=2)       # a question Wazy asked is forgotten 2 hours later (SPEC 6 step 14)
MAX_SCHEDULED = 100                     # upcoming commitments one person may have at once (SPEC 6 step 14)
LIST_SIZE = 10                          # "list" shows the next 10 (SPEC 6 step 9)
GRACE = timedelta(seconds=5)            # a time up to 5 s ago still counts as "now" (SPEC 3.4)
MAX_AHEAD = timedelta(days=366)         # the furthest ahead a time may be (SPEC 3.4)
MISS_KINDS = ("ask_time", "invalid_time", "past_time", "too_far")   # replies that ask for a (better) time
NO_WAIT = {"pending_title": None, "pending_raw": None, "pending_when": None, "pending_stated": 0,
           "pending_deadline": 0, "pending_at": None, "pending_miss_id": None}   # the user columns of a wait


def clean_text(text: str) -> str:
    """Drop control characters, zero-width and other invisible format characters, then collapse spaces.

    Newlines and tabs become spaces. Broken halves of emoji (lone surrogates) are dropped too, because the
    database cannot store them.
    """
    kept = []
    for char in str(text or ""):
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) not in ("Cc", "Cf", "Cs"):   # control, format (zero-width), surrogate
            kept.append(char)
    return " ".join("".join(kept).split())


def problem_reply(t: TimeResult, title: str) -> Tuple[str, dict]:
    """The reply kind and its parameters that say what is wrong with a time (SPEC 3.5 and 6, step 14)."""
    if t.status == "invalid":
        return "invalid_time", {"title": title, "detail": t.detail, "detail_code": t.detail_code, "hint": t.hint}
    if t.status == "past":
        return "past_time", {"title": title, "detail": t.detail}
    if t.status == "too_far":
        return "too_far", {"title": title}
    if t.status == "recurring":
        return "recurring_unsupported", {"title": title}
    if t.status == "multiple":
        return "multiple_commitments", {}
    return "ask_time", {"title": title}            # vague, or no time at all


def zone_or_default(name: str, default: str) -> tzinfo:
    """The time zone called `name`. A broken name (only possible in a damaged database) gives `default`."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return ZoneInfo(default)


@dataclass
class Reading:
    """What Wazy understood from one message: the time (from the rules), the clean title, and who read it."""
    time: TimeResult
    title: str
    reader: str = "rules"


class Conversation:
    """The state machine of SPEC 6. Everything it remembers between messages is in the database."""

    def __init__(self, db: Database, scheduler: Scheduler, *, ai: Any = None,
                 on_message: Optional[Callable[[str, int, Reply], None]] = None) -> None:
        self.db = db
        self.scheduler = scheduler
        self.ai = ai                    # optional: any object with .tidy(text, now, tz, lang) and .model
        self.on_message = on_message
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # =========================================================================================
    # The two ways in
    # =========================================================================================
    def handle(self, user_id: str, text: str, now: datetime, *, channel: str = "web",
               tz: Optional[str] = None) -> List[Reply]:
        """One incoming message -> Wazy's replies, in the order they were stored (SPEC 6)."""
        text = clean_text(text)
        if not text:                                                  # step 1: an empty message gets no reply
            return []
        with self._lock_for(user_id):                                 # one message per person at a time
            return self._handle(user_id, text, now, channel, tz)

    def tap(self, user_id: str, commitment_id: int, action: str, now: datetime) -> List[Reply]:
        """A Done / Later button (SPEC 6.2). [] if that nudge is not open or not this person's."""
        if action not in ("done", "later"):
            return []
        with self._lock_for(user_id):
            user = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
            if user is None:                                          # someone Wazy has never met
                return []
            nudge = self._open_nudge(user_id, commitment_id)
            if nudge is None:                                         # not open, or not this person's
                return []
            self._update(user, last_seen_at=to_db(now))
            # The tap is stored as the person's "Done" / "Later" message by _record_answer, and only if
            # it wins the nudge: if the 24 h no-reply close got there first, nothing is stored.
            return self._record_answer(user, nudge, action, user["lang"], now, store_tap=True)

    # =========================================================================================
    # handle(), step by step
    # =========================================================================================
    def _handle(self, user_id: str, text: str, now: datetime, channel: str, tz: Optional[str]) -> List[Reply]:
        """Note the person and their message, welcome a newcomer, then the first step of SPEC 6 that applies."""
        first_contact = self._is_new(user_id)
        user = self._arrive(user_id, now, channel, tz)
        lang = self._language(user, text)
        self.db.add_message(user_id, "user", text, "text", lang, None, now)
        self._expire_wait(user, now)
        replies = [self._welcome(user, lang, now)] if first_contact else []
        if self._over_rate_limit(user, now):                          # step 2
            return replies + self._rate_limited(user, lang, now)
        if len(text) > MAX_LENGTH:
            return replies + [self._say(user, "too_long", lang, now, limit=MAX_LENGTH)]
        intent = classify(text, pending=self._waiting(user), learning_question=bool(user["learning_pending"]))
        if intent.name == "greeting" and first_contact:
            return replies                                            # the welcome already said hello
        return replies + self._respond(user, intent, text, lang, now)

    def _respond(self, user: dict, intent: Intent, text: str, lang: str, now: datetime) -> List[Reply]:
        """Steps 3 to 14 of SPEC 6, in order: the first one that matches the message answers it."""
        name = intent.name
        if name == "stop":                                            # 3
            return self._stop(user, lang, now)
        if name == "start":                                           # 4
            return self._start(user, lang, now)
        if name == "help":                                            # 5
            return self._help(user, lang, now)
        if name == "greeting":                                        # 5
            return [self._welcome(user, lang, now)]
        if name in ("yes", "no") and user["learning_pending"]:        # 6
            return self._learning_answer(user, name == "yes", now)
        if name in ("learn_what", "learn_forget", "learn_resume"):    # 7
            return self._learning_command(user, name, now)
        if name in ("done", "later"):                                 # 8
            return self._answer_open_nudge(user, name, lang, now)
        if name == "list":                                            # 9
            return self._list(user, lang, now)
        if name == "cancel":                                          # 10
            return self._cancel(user, intent.number, lang, now)
        if name == "cancel_pending" and self._waiting(user):          # 11
            return self._cancel_wait(user, lang, now)
        if name == "injection":                                       # 12
            return self._refuse_injection(user, lang, now)
        if name in ("question", "edit"):                              # 13
            return self._not_a_commitment(user, name, lang, now)
        return self._read_commitment(user, text, lang, now)           # 14

    # ----- before the steps: who is this, which language, is a wait still fresh -----
    def _is_new(self, user_id: str) -> bool:
        """First contact: this person has no messages yet (they get the welcome before anything else)."""
        return not self.db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,))

    def _arrive(self, user_id: str, now: datetime, channel: str, tz: Optional[str]) -> dict:
        """Get the person (created on first contact, in zone `tz` if given) and note that they were here."""
        user = self.db.user(user_id, now, channel=channel, tz=tz)
        self._update(user, last_seen_at=to_db(now))
        return user

    def _language(self, user: dict, text: str) -> str:
        """English or Spanish. Saved for this person only when the words decide it, not the fallback."""
        if_unsure_english = detect_language(text, fallback="en")
        if_unsure_spanish = detect_language(text, fallback="es")
        if if_unsure_english != if_unsure_spanish:                   # no clear signal: keep what we had
            return user["lang"]
        if if_unsure_english != user["lang"]:
            self._update(user, lang=if_unsure_english)
        return if_unsure_english

    def _expire_wait(self, user: dict, now: datetime) -> None:
        """A question Wazy asked ("When should I nudge you?") is forgotten 2 hours after it was asked."""
        asked = from_db(user["pending_at"])
        if asked is not None and now - asked >= WAIT_EXPIRES:
            self._clear_wait(user)

    def _welcome(self, user: dict, lang: str, now: datetime) -> Reply:
        """The welcome (SPEC 6 step 5, and the first reply anyone ever gets)."""
        return self._say(user, "welcome", lang, now, ai=self.ai is not None)

    # ----- step 2: rate limit -----
    def _over_rate_limit(self, user: dict, now: datetime) -> bool:
        """SPEC 6 step 2: more than 20 messages from this person in the last 60 seconds (this one included)."""
        count = self.db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user' "
                               "AND created_at > ?", (user["id"], to_db(now - RATE_WINDOW)))
        return count > RATE_LIMIT

    def _rate_limited(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 2: say "slow down" at most once a minute; the other extra messages get no reply."""
        said = self.db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'bot' "
                              "AND kind = 'rate_limited' AND created_at > ?", (user["id"], to_db(now - RATE_WINDOW)))
        if said:
            return []
        return [self._say(user, "rate_limited", lang, now)]

    # ----- steps 3 to 7: STOP, START, help, learning -----
    def _stop(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 3: every upcoming or open nudge stops (nothing is deleted), and the person is opted out."""
        with self.db.transaction():
            self.db.execute("UPDATE commitments SET status = 'stopped' WHERE user_id = ? "
                            "AND status IN ('scheduled', 'nudged')", (user["id"],))
            self.db.execute("UPDATE nudge_outcomes SET outcome = 'stop', responded_at = ? "
                            "WHERE user_id = ? AND outcome = 'pending'", (to_db(now), user["id"]))
            self._update(user, opted_out=1, **NO_WAIT)
        return [self._say(user, "stopped", lang, now)]

    def _start(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 4: nudges are back on."""
        self._update(user, opted_out=0)
        return [self._say(user, "started", lang, now)]

    def _help(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 5: what Wazy can do."""
        return [self._say(user, "help", lang, now)]

    def _learning_answer(self, user: dict, yes: bool, now: datetime) -> List[Reply]:
        """SPEC 6 step 6: YES or NO to the open learning question (learning.py records it and words the reply)."""
        reply = learning.answer(self.db, user["id"], yes, now)
        return [self._store(user["id"], reply, now)]

    def _learning_command(self, user: dict, name: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 7: "what did you learn", "forget" or "learn again" (learning.py does the work)."""
        if name == "learn_what":
            reply = learning.explain(self.db, user["id"])
        elif name == "learn_forget":
            reply = learning.forget(self.db, user["id"], now)
        else:
            reply = learning.resume(self.db, user["id"], now)
        return [self._store(user["id"], reply, now)]

    # ----- step 8 and tap(): answering a nudge (SPEC 6.2) -----
    def _answer_open_nudge(self, user: dict, action: str, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 8: a typed "done" or "later" answers this person's most recent open nudge."""
        nudge = self._open_nudge(user["id"])
        if nudge is None:
            return [self._say(user, "no_open_nudge", lang, now)]
        return self._record_answer(user, nudge, action, lang, now)

    def _open_nudge(self, user_id: str, commitment_id: Optional[int] = None) -> Optional[dict]:
        """The latest nudge of this person still waiting for Done/Later (of one commitment, if given)."""
        sql = ("SELECT o.id, o.commitment_id, o.nudged_at, c.title FROM nudge_outcomes o "
               "JOIN commitments c ON c.id = o.commitment_id WHERE o.user_id = ? AND o.outcome = 'pending'")
        params: tuple = (user_id,)
        if commitment_id is not None:
            sql += " AND o.commitment_id = ?"
            params += (commitment_id,)
        return self.db.one(sql + " ORDER BY o.id DESC LIMIT 1", params)

    def _record_answer(self, user: dict, nudge: dict, action: str, lang: str, now: datetime,
                       store_tap: bool = False) -> List[Reply]:
        """SPEC 6.2: Done closes the commitment. Later schedules exactly one more nudge. Then learning may ask.

        store_tap (from tap()): also store the button as the person's "Done" / "Later" message.
        """
        later_at = self.scheduler.later_time(user, now) if action == "later" else None
        waited = now - from_db(nudge["nudged_at"])
        seconds = max(0, int(waited.total_seconds()))               # how long the person took to answer
        with self.db.transaction():
            answered = self.db.execute(
                "UPDATE nudge_outcomes SET outcome = ?, responded_at = ?, seconds_to_respond = ? "
                "WHERE id = ? AND outcome = 'pending'", (action, to_db(now), seconds, nudge["id"])).rowcount
            # Claim first, then store the tap: a tap that lost the nudge leaves no unanswered "Done" in the chat.
            if answered and store_tap:
                label = "Done" if action == "done" else "Later"
                self.db.add_message(user["id"], "user", label, "text", lang, nudge["commitment_id"], now)
            if answered and action == "done":
                self.db.execute("UPDATE commitments SET status = 'done' WHERE id = ?", (nudge["commitment_id"],))
            elif answered:
                self.db.execute("UPDATE commitments SET status = 'scheduled', nudge_at = ?, "
                                "later_count = later_count + 1 WHERE id = ?",
                                (to_db(later_at), nudge["commitment_id"]))
        if not answered:                       # someone (or the 24 h timeout) answered it a moment ago
            return []
        replies = [self._ack(user, nudge, action, later_at, lang, now)]
        question = learning.consider(self.db, user["id"], now)
        if question is not None:
            replies.append(self._store(user["id"], question, now))
        return replies

    def _ack(self, user: dict, nudge: dict, action: str, later_at: Optional[datetime], lang: str,
             now: datetime) -> Reply:
        """Confirm the answer: done_ack for Done, or later_ack with the new nudge time for Later."""
        cid, title = nudge["commitment_id"], nudge["title"]
        if action == "done":
            return self._say(user, "done_ack", lang, now, commitment_id=cid, title=title)
        when_text = describe_when(later_at.astimezone(self._zone(user)), now, lang)
        return self._say(user, "later_ack", lang, now, commitment_id=cid, title=title, when_text=when_text)

    # ----- step 9: list -----
    def _list(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 9: the next 10 scheduled commitments, soonest first, numbered for "cancel 2"."""
        rows = self._upcoming(user["id"])
        if not rows:
            self._update(user, last_list=None)
            return [self._say(user, "list_empty", lang, now)]
        return [self._show_numbered(user, "list", rows, lang, now)]

    def _upcoming(self, user_id: str) -> List[dict]:
        """This person's next 10 scheduled commitments, soonest first."""
        return self.db.query("SELECT id, title, nudge_at FROM commitments WHERE user_id = ? AND status = 'scheduled' "
                             "ORDER BY nudge_at, id LIMIT ?", (user_id, LIST_SIZE))

    def _show_numbered(self, user: dict, kind: str, rows: List[dict], lang: str, now: datetime) -> Reply:
        """Say a numbered list ("list" or "cancel_which") and remember which commitment each number means."""
        zone = self._zone(user)
        items = []
        for number, row in enumerate(rows, start=1):
            when = from_db(row["nudge_at"]).astimezone(zone)
            items.append((number, row["title"], describe_when(when, now, lang)))
        self._update(user, last_list=json.dumps([row["id"] for row in rows]))
        return self._say(user, kind, lang, now, items=items)

    # ----- steps 10 and 11: cancel -----
    def _cancel(self, user: dict, number: Optional[int], lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 10: "cancel 2" cancels item 2; a bare "cancel" drops a wait, else cancels the only one."""
        if number is not None:
            return self._cancel_number(user, number, lang, now)
        if self._waiting(user):                                       # "cancel" drops the question first
            return self._cancel_wait(user, lang, now)
        rows = self._upcoming(user["id"])
        if len(rows) == 1:
            return self._cancel_commitment(user, rows[0]["id"], lang, now)
        return self._which_one(user, rows, lang, now)

    def _cancel_number(self, user: dict, number: int, lang: str, now: datetime) -> List[Reply]:
        """Cancel item N of the list the person last saw (or of the current list if they never saw one)."""
        if user["last_list"]:
            ids = json.loads(user["last_list"])
        else:
            ids = [row["id"] for row in self._upcoming(user["id"])]
        if 1 <= number <= len(ids):
            replies = self._cancel_commitment(user, ids[number - 1], lang, now)
            if replies:
                return replies
        return self._which_one(user, self._upcoming(user["id"]), lang, now)

    def _which_one(self, user: dict, rows: List[dict], lang: str, now: datetime) -> List[Reply]:
        """Ask which one to cancel, showing the numbered list (or say there is nothing to cancel)."""
        if not rows:
            return [self._say(user, "nothing_to_cancel", lang, now)]
        return [self._show_numbered(user, "cancel_which", rows, lang, now)]

    def _cancel_commitment(self, user: dict, commitment_id: int, lang: str, now: datetime) -> List[Reply]:
        """Status "cancelled" (nothing is deleted); an open nudge's outcome becomes "cancelled" too.
        [] if it is not this person's or is already finished."""
        row = self.db.one("SELECT id, title FROM commitments WHERE id = ? AND user_id = ?",
                          (commitment_id, user["id"]))
        with self.db.transaction():
            changed = self.db.execute("UPDATE commitments SET status = 'cancelled' WHERE id = ? AND user_id = ? "
                                      "AND status IN ('scheduled', 'nudged')", (commitment_id, user["id"])).rowcount
            if changed:
                self.db.execute("UPDATE nudge_outcomes SET outcome = 'cancelled', responded_at = ? "
                                "WHERE commitment_id = ? AND outcome = 'pending'", (to_db(now), commitment_id))
        if not changed:
            return []
        return [self._say(user, "cancelled", lang, now, commitment_id=row["id"], title=row["title"])]

    def _cancel_wait(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 steps 10 and 11: forget the question Wazy is waiting on. The reader_misses row stays, unfixed."""
        title = user["pending_title"] or ""
        self._clear_wait(user)
        return [self._say(user, "pending_cancelled", lang, now, title=title)]

    # ----- steps 12 and 13: not a commitment -----
    def _refuse_injection(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 12: "I only take commitments". Nothing is stored as a commitment and any wait stays."""
        return [self._say(user, "injection", lang, now)]

    def _not_a_commitment(self, user: dict, reason: str, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 13: a question or an edit is not a commitment (the reply mentions "list" and "cancel 2")."""
        return [self._say(user, "not_commitment", lang, now, reason=reason)]

    # =========================================================================================
    # Step 14: reading a commitment
    # =========================================================================================
    def _read_commitment(self, user: dict, text: str, lang: str, now: datetime) -> List[Reply]:
        """SPEC 6 step 14: a new commitment, or the answer to the question Wazy is waiting on."""
        reading = self._read(user, text, lang, now)
        if user["pending_title"] and not reading.title:               # the answer to "when ... about X?"
            return self._answer_waiting_task(user, text, reading, lang, now)
        if user["pending_when"] and reading.title and reading.time.status == "none":   # "what ... at T?"
            return self._answer_waiting_time(user, text, reading, lang, now)
        return self._read_new(user, text, reading, lang, now)

    def _read(self, user: dict, text: str, lang: str, now: datetime) -> Reading:
        """The rules read the time and the title. The optional AI may then tidy the title (SPEC 6, step 14)."""
        zone = self._zone(user)
        t = parse_time(text, now, zone, DEFAULT_HOUR)
        reading = Reading(time=t, title=make_title(text, t.spans, lang))
        if reading.title and self.ai is not None:                     # no task found: nothing to tidy
            self._let_ai_tidy(reading, text, now, zone, lang)
        return reading

    def _let_ai_tidy(self, reading: Reading, text: str, now: datetime, zone: tzinfo, lang: str) -> None:
        """The AI may replace the title (never with ""), and may give a time only when the rules found none.
        Its time passes the same checks as any time. If the AI fails in any way, the rules' reading stands."""
        try:
            answer = self.ai.tidy(text, now, zone, lang)
        except Exception as error:                                    # optional helper: never break a message
            log.warning("AI reader failed (%s); using the rules only", type(error).__name__)
            return
        if not isinstance(answer, dict):
            return
        title = answer.get("title")
        if isinstance(title, str) and clean_text(title):
            reading.title = clean_text(title)[:MAX_TITLE_LENGTH]
            reading.reader = self._ai_reader_name()
        when = answer.get("when")
        if reading.time.status == "none" and self._acceptable_time(when, now):
            reading.time = TimeResult("ok", when=when.astimezone(zone), stated=True)
            reading.reader = self._ai_reader_name()

    def _ai_reader_name(self) -> str:
        """Who read a commitment the AI helped with, as stored in commitments.reader."""
        model = str(getattr(self.ai, "model", "")).lower()
        return "rules + Claude Haiku" if "haiku" in model else "rules + Claude"

    @staticmethod
    def _acceptable_time(when: Any, now: datetime) -> bool:
        """The final check of SPEC 3.4: an aware time, not before now - 5 s, not more than 366 days ahead."""
        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            return False
        return now - GRACE <= when <= now + MAX_AHEAD

    def _read_new(self, user: dict, text: str, reading: Reading, lang: str, now: datetime) -> List[Reply]:
        """A message that is not the answer to a question Wazy asked."""
        t, title = reading.time, reading.title
        if t.status == "ok" and title:
            return self._create(user, reading, text, lang, now)
        if t.status == "ok":
            return self._wait_for_task(user, text, t, lang, now)
        if t.status in ("vague", "none") and not title:
            return self._ask_what(user, lang, now)
        return self._problem(user, t, title, text, lang, now)

    def _answer_waiting_task(self, user: dict, text: str, reading: Reading, lang: str,
                             now: datetime) -> List[Reply]:
        """Wazy asked when to nudge about the waiting task. A good time creates it and fixes the miss row;
        any problem is answered again for that task, and the wait is kept."""
        t, title = reading.time, user["pending_title"]
        if t.status == "ok":
            task = Reading(time=t, title=title, reader=reading.reader)
            raw = f"{user['pending_raw']} / {text}"
            return self._create(user, task, raw, lang, now, fixed_by=text)
        kind, params = problem_reply(t, title)
        miss_id = self._log_miss(user, text, kind, now) if kind in MISS_KINDS else user["pending_miss_id"]
        self._wait_for_time(user, title, user["pending_raw"], miss_id, now)
        return [self._say(user, kind, lang, now, **params)]

    def _answer_waiting_time(self, user: dict, text: str, reading: Reading, lang: str,
                             now: datetime) -> List[Reply]:
        """Wazy asked what to nudge about at the waiting time: this message is the task."""
        when = from_db(user["pending_when"]).astimezone(self._zone(user))
        if when < now - GRACE:                                        # that time went by while Wazy waited
            gone = TimeResult("past", detail=when.strftime("%H:%M"))
            return self._problem(user, gone, reading.title, text, lang, now)
        saved = TimeResult("ok", when=when, stated=bool(user["pending_stated"]),
                           deadline=bool(user["pending_deadline"]))
        task = Reading(time=saved, title=reading.title, reader=reading.reader)
        raw = f"{user['pending_raw']} / {text}"
        return self._create(user, task, raw, lang, now)

    def _problem(self, user: dict, t: TimeResult, title: str, text: str, lang: str, now: datetime) -> List[Reply]:
        """Say what is wrong with the time. If Wazy asked for a (better) time, log the miss and wait for it."""
        kind, params = problem_reply(t, title)
        if kind in MISS_KINDS:
            miss_id = self._log_miss(user, text, kind, now)
            if title:
                self._wait_for_time(user, title, text, miss_id, now)
            else:
                self._clear_wait(user)
        else:                                                         # repeating or two tasks: no wait
            self._clear_wait(user)
        return [self._say(user, kind, lang, now, **params)]

    def _ask_what(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """No task and no usable time: ask what to nudge about (mentioning a time Wazy already waits to use)."""
        when = from_db(user["pending_when"])
        when_text = describe_when(when.astimezone(self._zone(user)), now, lang) if when else ""
        return [self._say(user, "ask_what", lang, now, when_text=when_text)]

    # ----- the two kinds of wait -----
    def _waiting(self, user: dict) -> bool:
        """Wazy is waiting for a missing time (pending_title) or a missing task (pending_when)."""
        return bool(user["pending_title"] or user["pending_when"])

    def _wait_for_time(self, user: dict, title: str, raw: str, miss_id: Optional[int], now: datetime) -> None:
        """Remember the task while Wazy waits for a time for it."""
        self._set_wait(user, pending_title=title, pending_raw=raw, pending_at=to_db(now), pending_miss_id=miss_id)

    def _wait_for_task(self, user: dict, text: str, t: TimeResult, lang: str, now: datetime) -> List[Reply]:
        """A good time but no task: remember the time and ask what it is for."""
        self._set_wait(user, pending_when=to_db(t.when), pending_raw=text, pending_at=to_db(now),
                       pending_stated=int(t.stated), pending_deadline=int(t.deadline))
        return [self._say(user, "ask_what", lang, now, when_text=describe_when(t.when, now, lang))]

    def _set_wait(self, user: dict, **fields: Any) -> None:
        """Replace whatever Wazy was waiting for with `fields`; the wait columns not given are emptied."""
        wait = dict(NO_WAIT)
        wait.update(fields)
        self._update(user, **wait)

    def _clear_wait(self, user: dict) -> None:
        """Wazy is no longer waiting for anything."""
        self._update(user, **NO_WAIT)

    def _log_miss(self, user: dict, text: str, kind: str, now: datetime) -> int:
        """A message Wazy could not turn into a time (kept so the rules can be improved), with the reply kind."""
        cursor = self.db.execute("INSERT INTO reader_misses (user_id, original, problem, created_at) "
                                 "VALUES (?, ?, ?, ?)", (user["id"], text, kind, to_db(now)))
        return int(cursor.lastrowid)

    # ----- SPEC 6.1: creating a commitment -----
    def _create(self, user: dict, reading: Reading, raw: str, lang: str, now: datetime,
                fixed_by: Optional[str] = None) -> List[Reply]:
        """SPEC 6.1: store the commitment, clear the wait, re-subscribe the person, and confirm in one line.

        `raw` is what the person typed; `fixed_by` is their reply that answered Wazy's question, saved in
        that question's reader_misses row."""
        t = reading.time
        when, source = self._choose_time(user, t.when.astimezone(self._zone(user)), t.stated, t.deadline, now)
        with self.db.transaction():
            if self._count_scheduled(user["id"]) >= MAX_SCHEDULED:
                commitment_id = None
            else:
                commitment_id = self._insert(user, reading.title, raw, when, source, t.deadline, reading.reader,
                                             now)
                if fixed_by is not None and user["pending_miss_id"]:
                    self._mark_miss_fixed(user, fixed_by, now)
                self._update(user, opted_out=0, **NO_WAIT)
        if commitment_id is None:                                     # too many upcoming commitments
            return [self._say(user, "not_commitment", lang, now, reason="limit")]
        return [self._say(user, "confirm", lang, now, commitment_id=commitment_id, title=reading.title,
                          when_text=describe_when(when, now, lang), learned=source == "learned")]

    @staticmethod
    def _choose_time(user: dict, when: datetime, stated: bool, deadline: bool,
                     now: datetime) -> Tuple[datetime, str]:
        """Which time to use, and where it came from: "stated", "default" or "learned" (SPEC 6.1).

        A learned hour replaces only Wazy's own default hour (10:00 exactly), never a time the person gave,
        never a time already gone, and never moves a deadline later."""
        if stated:
            return when, "stated"
        learned = learning.usual_hour(user)                           # 10 unless they said YES to another
        if learned == DEFAULT_HOUR or (when.hour, when.minute, when.second) != (DEFAULT_HOUR, 0, 0):
            return when, "default"
        candidate = when.replace(hour=learned)
        if candidate <= now or (deadline and learned > DEFAULT_HOUR):
            return when, "default"
        return candidate, "learned"

    def _count_scheduled(self, user_id: str) -> int:
        """How many upcoming (scheduled) commitments this person has."""
        return self.db.scalar("SELECT COUNT(*) FROM commitments WHERE user_id = ? AND status = 'scheduled'",
                              (user_id,))

    def _insert(self, user: dict, title: str, raw: str, when: datetime, source: str, deadline: bool, reader: str,
                now: datetime) -> int:
        """Add the commitments row (status "scheduled", nudge_at in UTC) and return its id."""
        cursor = self.db.execute(
            "INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, reader, time_source, deadline, "
            "created_at) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)",
            (user["id"], raw, title, to_db(when), reader, source, int(deadline), to_db(now)))
        return int(cursor.lastrowid)

    def _mark_miss_fixed(self, user: dict, reply: str, now: datetime) -> None:
        """The reader_misses row of Wazy's question gets the reply that fixed it, and when."""
        self.db.execute("UPDATE reader_misses SET reply = ?, fixed_at = ? WHERE id = ? AND user_id = ?",
                        (reply, to_db(now), user["pending_miss_id"], user["id"]))

    # =========================================================================================
    # Small helpers
    # =========================================================================================
    def _say(self, user: dict, kind: str, lang: str, now: datetime, commitment_id: Optional[int] = None,
             **params: Any) -> Reply:
        """Render one of Wazy's sentences, store it and hand it on."""
        reply = Reply(kind=kind, text=render(kind, lang, **params), lang=lang, commitment_id=commitment_id)
        return self._store(user["id"], reply, now)

    def _store(self, user_id: str, reply: Reply, now: datetime) -> Reply:
        """Store a Wazy message, then call on_message (WhatsApp delivery). A delivery hook that fails is
        logged (never the text) and does not stop the conversation."""
        message_id = self.db.add_message(user_id, "bot", reply.text, reply.kind, reply.lang, reply.commitment_id, now)
        if self.on_message is not None:
            try:
                self.on_message(user_id, message_id, reply)
            except Exception as error:
                log.warning("on_message failed for message %s (%s)", message_id, type(error).__name__)
        return reply

    def _update(self, user: dict, **fields: Any) -> None:
        """Change columns of the person in the database and in our copy of the row."""
        self.db.update_user(user["id"], **fields)
        user.update(fields)

    def _zone(self, user: dict) -> tzinfo:
        """The person's time zone."""
        return zone_or_default(user["tz"], self.db.default_tz)

    def _lock_for(self, user_id: str) -> threading.RLock:
        """One lock per person: two messages from the same person are handled one after the other."""
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.RLock())
