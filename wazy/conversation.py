"""The conversation: what Wazy does with each message a person sends (SPEC section 6).

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

tap() records a Done or Later button press (SPEC 6.2).

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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from . import learning
from .db import Database, from_db, to_db
from .intents import classify
from .lang import detect_language
from .replies import describe_when, render
from .timeparse import DEFAULT_HOUR, parse_time
from .title import make_title
from .types import Intent, Reply, TimeResult

if TYPE_CHECKING:  # only for the type hints; the App builds the real Scheduler
    from .scheduler import Scheduler

log = logging.getLogger(__name__)

MAX_LENGTH = 500                        # longer messages get "too_long" and nothing else happens
RATE_LIMIT = 20                         # more than 20 messages ...
RATE_WINDOW = timedelta(seconds=60)     # ... in 60 seconds is too many
WAIT_EXPIRES = timedelta(hours=2)       # a question Wazy asked is forgotten 2 hours later
MAX_SCHEDULED = 100                     # upcoming commitments one person may have at once
LIST_SIZE = 10                          # "list" shows the next 10
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


@dataclass
class Reading:
    """What Wazy understood from one message: the time (from the rules), the clean title, and who read it."""
    time: TimeResult
    title: str
    reader: str = "rules"


class Conversation:
    """The state machine of SPEC 6. Everything it remembers between messages is in the database."""

    def __init__(self, db: Database, scheduler: "Scheduler", *, ai: Any = None,
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
        """One incoming message -> Wazy's replies, in the order they were stored."""
        text = clean_text(text)
        if not text:                                                  # step 1: nothing to answer
            return []
        with self._lock_for(user_id):                                 # one message per person at a time
            return self._handle(user_id, text, now, channel, tz)

    def tap(self, user_id: str, commitment_id: int, action: str, now: datetime) -> List[Reply]:
        """A Done / Later button (SPEC 6.2). [] if that nudge is not open or not this person's."""
        if action not in ("done", "later"):
            return []
        with self._lock_for(user_id):
            user = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
            nudge = self._open_nudge(user_id, commitment_id) if user else None
            if nudge is None:
                return []
            self._update(user, last_seen_at=to_db(now))
            label = "Done" if action == "done" else "Later"
            self.db.add_message(user_id, "user", label, "text", user["lang"], nudge["commitment_id"], now)
            return self._record_answer(user, nudge, action, user["lang"], now)

    # =========================================================================================
    # handle(), step by step
    # =========================================================================================
    def _handle(self, user_id: str, text: str, now: datetime, channel: str, tz: Optional[str]) -> List[Reply]:
        """Store the message, welcome a newcomer, then steps 2 to 14."""
        first_contact = self._is_new(user_id)
        user = self._arrive(user_id, now, channel, tz)
        lang = self._language(user, text)
        self.db.add_message(user_id, "user", text, "text", lang, None, now)
        self._expire_wait(user, now)
        replies = [self._say(user, "welcome", lang, now, ai=self.ai is not None)] if first_contact else []
        if self._over_rate_limit(user, now):                          # step 2
            return replies + self._rate_limited(user, lang, now)
        if len(text) > MAX_LENGTH:
            return replies + [self._say(user, "too_long", lang, now, limit=MAX_LENGTH)]
        intent = classify(text, pending=self._waiting(user), learning_question=bool(user["learning_pending"]))
        if intent.name == "greeting" and first_contact:
            return replies                                            # the welcome already said hello
        return replies + self._respond(user, intent, text, lang, now)

    def _respond(self, user: dict, intent: Intent, text: str, lang: str, now: datetime) -> List[Reply]:
        """Steps 3 to 14 of SPEC 6: the first one that matches the message answers it."""
        name = intent.name
        if name == "stop":                                            # 3
            return self._stop(user, lang, now)
        if name == "start":                                           # 4
            return self._start(user, lang, now)
        if name == "help":                                            # 5
            return [self._say(user, "help", lang, now)]
        if name == "greeting":
            return [self._say(user, "welcome", lang, now, ai=self.ai is not None)]
        if name in ("yes", "no") and user["learning_pending"]:        # 6
            return [self._store(user["id"], learning.answer(self.db, user["id"], name == "yes", now), now)]
        if name in ("learn_what", "learn_forget", "learn_resume"):    # 7
            return self._learning(user, name, now)
        if name in ("done", "later"):                                 # 8
            return self._answer_open_nudge(user, name, lang, now)
        if name == "list":                                            # 9
            return self._list(user, lang, now)
        if name == "cancel":                                          # 10
            return self._cancel(user, intent.number, lang, now)
        if name == "cancel_pending" and self._waiting(user):          # 11
            return self._cancel_wait(user, lang, now)
        if name == "injection":                                       # 12
            return [self._say(user, "injection", lang, now)]
        if name in ("question", "edit"):                              # 13
            return [self._say(user, "not_commitment", lang, now, reason=name)]
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

    # ----- step 2: rate limit -----
    def _over_rate_limit(self, user: dict, now: datetime) -> bool:
        """More than 20 messages from this person in the last 60 seconds (this one included)."""
        count = self.db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user' "
                               "AND created_at > ?", (user["id"], to_db(now - RATE_WINDOW)))
        return count > RATE_LIMIT

    def _rate_limited(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """Say "slow down" at most once a minute; the other extra messages get no reply at all."""
        said = self.db.scalar("SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'bot' "
                              "AND kind = 'rate_limited' AND created_at > ?", (user["id"], to_db(now - RATE_WINDOW)))
        return [] if said else [self._say(user, "rate_limited", lang, now)]

    # ----- steps 3 and 4: STOP and START -----
    def _stop(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """Every upcoming or open nudge stops (nothing is deleted), and the person is opted out."""
        with self.db.transaction():
            self.db.execute("UPDATE commitments SET status = 'stopped' WHERE user_id = ? "
                            "AND status IN ('scheduled', 'nudged')", (user["id"],))
            self.db.execute("UPDATE nudge_outcomes SET outcome = 'stop', responded_at = ? "
                            "WHERE user_id = ? AND outcome = 'pending'", (to_db(now), user["id"]))
            self._update(user, opted_out=1, **NO_WAIT)
        return [self._say(user, "stopped", lang, now)]

    def _start(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        self._update(user, opted_out=0)
        return [self._say(user, "started", lang, now)]

    # ----- step 7: learning commands (learning.py writes the rows and the reply) -----
    def _learning(self, user: dict, name: str, now: datetime) -> List[Reply]:
        if name == "learn_what":
            reply = learning.explain(self.db, user["id"])
        elif name == "learn_forget":
            reply = learning.forget(self.db, user["id"], now)
        else:
            reply = learning.resume(self.db, user["id"], now)
        return [self._store(user["id"], reply, now)]

    # ----- step 8 and tap(): answering a nudge (SPEC 6.2) -----
    def _answer_open_nudge(self, user: dict, action: str, lang: str, now: datetime) -> List[Reply]:
        """A typed "done" or "later" answers this person's most recent open nudge."""
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

    def _record_answer(self, user: dict, nudge: dict, action: str, lang: str, now: datetime) -> List[Reply]:
        """Done closes the commitment. Later schedules exactly one more nudge. Then learning may ask."""
        later_at = self.scheduler.later_time(user, now) if action == "later" else None
        seconds = max(0, int((now - from_db(nudge["nudged_at"])).total_seconds()))
        with self.db.transaction():
            answered = self.db.execute(
                "UPDATE nudge_outcomes SET outcome = ?, responded_at = ?, seconds_to_respond = ? "
                "WHERE id = ? AND outcome = 'pending'", (action, to_db(now), seconds, nudge["id"])).rowcount
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
        cid, title = nudge["commitment_id"], nudge["title"]
        if action == "done":
            return self._say(user, "done_ack", lang, now, commitment_id=cid, title=title)
        when_text = describe_when(later_at.astimezone(self._zone(user)), now, lang)
        return self._say(user, "later_ack", lang, now, commitment_id=cid, title=title, when_text=when_text)

    # ----- step 9: list -----
    def _list(self, user: dict, lang: str, now: datetime) -> List[Reply]:
        """The next 10 scheduled commitments, soonest first. Their numbers are saved for "cancel 2"."""
        rows = self._upcoming(user["id"])
        if not rows:
            self._update(user, last_list=None)
            return [self._say(user, "list_empty", lang, now)]
        return [self._show_numbered(user, "list", rows, lang, now)]

    def _upcoming(self, user_id: str) -> List[dict]:
        return self.db.query("SELECT id, title, nudge_at FROM commitments WHERE user_id = ? AND status = 'scheduled' "
                             "ORDER BY nudge_at, id LIMIT ?", (user_id, LIST_SIZE))

    def _show_numbered(self, user: dict, kind: str, rows: List[dict], lang: str, now: datetime) -> Reply:
        """Say a numbered list ("list" or "cancel_which") and remember which commitment each number means."""
        zone = self._zone(user)
        items = [(n, row["title"], describe_when(from_db(row["nudge_at"]).astimezone(zone), now, lang))
                 for n, row in enumerate(rows, start=1)]
        self._update(user, last_list=json.dumps([row["id"] for row in rows]))
        return self._say(user, kind, lang, now, items=items)

    # ----- steps 10 and 11: cancel -----
    def _cancel(self, user: dict, number: Optional[int], lang: str, now: datetime) -> List[Reply]:
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
        ids = json.loads(user["last_list"]) if user["last_list"] else [r["id"] for r in self._upcoming(user["id"])]
        if 1 <= number <= len(ids):
            replies = self._cancel_commitment(user, ids[number - 1], lang, now)
            if replies:
                return replies
        return self._which_one(user, self._upcoming(user["id"]), lang, now)

    def _which_one(self, user: dict, rows: List[dict], lang: str, now: datetime) -> List[Reply]:
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
        """Forget the question Wazy is waiting on. The reader_misses row stays, unfixed."""
        title = user["pending_title"] or ""
        self._clear_wait(user)
        return [self._say(user, "pending_cancelled", lang, now, title=title)]

    # =========================================================================================
    # Step 14: reading a commitment
    # =========================================================================================
    def _read_commitment(self, user: dict, text: str, lang: str, now: datetime) -> List[Reply]:
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
            reading.title = clean_text(title)[:120]
            reading.reader = self._ai_reader_name()
        when = answer.get("when")
        if reading.time.status == "none" and self._acceptable_time(when, now):
            reading.time = TimeResult("ok", when=when.astimezone(zone), stated=True)
            reading.reader = self._ai_reader_name()

    def _ai_reader_name(self) -> str:
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
            return self._create(user, title, text, t.when, t.stated, t.deadline, reading.reader, lang, now)
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
        raw = f"{user['pending_raw']} / {text}"
        if t.status == "ok":
            return self._create(user, title, raw, t.when, t.stated, t.deadline, reading.reader, lang, now,
                                fixed_by=text)
        kind, params = problem_reply(t, title)
        miss_id = self._log_miss(user, text, kind, now) if kind in MISS_KINDS else user["pending_miss_id"]
        self._wait_for_time(user, title, user["pending_raw"], miss_id, now)
        return [self._say(user, kind, lang, now, **params)]

    def _answer_waiting_time(self, user: dict, text: str, reading: Reading, lang: str,
                             now: datetime) -> List[Reply]:
        """Wazy asked what to nudge about at the waiting time: this message is the task."""
        when = from_db(user["pending_when"]).astimezone(self._zone(user))
        if when < now - GRACE:                                        # that time went by while Wazy waited
            return self._problem(user, TimeResult("past", detail=when.strftime("%H:%M")), reading.title, text,
                                 lang, now)
        raw = f"{user['pending_raw']} / {text}"
        return self._create(user, reading.title, raw, when, bool(user["pending_stated"]),
                            bool(user["pending_deadline"]), reading.reader, lang, now)

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
        return bool(user["pending_title"] or user["pending_when"])

    def _wait_for_time(self, user: dict, title: str, raw: str, miss_id: Optional[int], now: datetime) -> None:
        """Remember the task while Wazy waits for a time for it."""
        self._update(user, **dict(NO_WAIT, pending_title=title, pending_raw=raw, pending_at=to_db(now),
                                  pending_miss_id=miss_id))

    def _wait_for_task(self, user: dict, text: str, t: TimeResult, lang: str, now: datetime) -> List[Reply]:
        """A good time but no task: remember the time and ask what it is for."""
        self._update(user, **dict(NO_WAIT, pending_when=to_db(t.when), pending_raw=text, pending_at=to_db(now),
                                  pending_stated=int(t.stated), pending_deadline=int(t.deadline)))
        return [self._say(user, "ask_what", lang, now, when_text=describe_when(t.when, now, lang))]

    def _clear_wait(self, user: dict) -> None:
        self._update(user, **NO_WAIT)

    def _log_miss(self, user: dict, text: str, kind: str, now: datetime) -> int:
        """A message Wazy could not turn into a time (kept so the rules can be improved), with the reply kind."""
        cursor = self.db.execute("INSERT INTO reader_misses (user_id, original, problem, created_at) "
                                 "VALUES (?, ?, ?, ?)", (user["id"], text, kind, to_db(now)))
        return int(cursor.lastrowid)

    # ----- SPEC 6.1: creating a commitment -----
    def _create(self, user: dict, title: str, raw: str, when: datetime, stated: bool, deadline: bool, reader: str,
                lang: str, now: datetime, fixed_by: Optional[str] = None) -> List[Reply]:
        """Store the commitment, clear the wait, re-subscribe the person, and confirm in one line."""
        when, source = self._choose_time(user, when.astimezone(self._zone(user)), stated, deadline, now)
        with self.db.transaction():
            if self._count_scheduled(user["id"]) >= MAX_SCHEDULED:
                commitment_id = None
            else:
                commitment_id = self._insert(user, title, raw, when, source, deadline, reader, now)
                if fixed_by is not None and user["pending_miss_id"]:
                    self.db.execute("UPDATE reader_misses SET reply = ?, fixed_at = ? WHERE id = ? AND user_id = ?",
                                    (fixed_by, to_db(now), user["pending_miss_id"], user["id"]))
                self._update(user, opted_out=0, **NO_WAIT)
        if commitment_id is None:
            return [self._say(user, "not_commitment", lang, now, reason="limit")]
        return [self._say(user, "confirm", lang, now, commitment_id=commitment_id, title=title,
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
        return self.db.scalar("SELECT COUNT(*) FROM commitments WHERE user_id = ? AND status = 'scheduled'",
                              (user_id,))

    def _insert(self, user: dict, title: str, raw: str, when: datetime, source: str, deadline: bool, reader: str,
                now: datetime) -> int:
        cursor = self.db.execute(
            "INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, reader, time_source, deadline, "
            "created_at) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)",
            (user["id"], raw, title, to_db(when), reader, source, int(deadline), to_db(now)))
        return int(cursor.lastrowid)

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
        try:
            return ZoneInfo(user["tz"])
        except (KeyError, ValueError):                               # a broken zone name: use the default
            return ZoneInfo(self.db.default_tz)

    def _lock_for(self, user_id: str) -> threading.RLock:
        """One lock per person: two messages from the same person are handled one after the other."""
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.RLock())
