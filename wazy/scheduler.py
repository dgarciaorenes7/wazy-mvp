"""The scheduler: sends each nudge once, at its time, and closes the nudges nobody answered.

The app calls run_due(now) every few seconds. It:
  1. finds the commitments that are due (status 'scheduled', nudge_at <= now) of people who have not
     said STOP (an opted-out person's commitments are left alone),
  2. CLAIMS each one in a single transaction: the status changes from 'scheduled' to 'nudged' only if it
     is still 'scheduled'. Only the caller that changed that one row goes on, so even two schedulers
     racing can never send the same nudge twice (product promise 2),
  3. in that same transaction records a pending outcome and stores the nudge message (with Done / Later
     buttons); a nudge sent more than 10 minutes late says when it was due ("due at 09:00"),
  4. after the transaction hands the message to on_message (so WhatsApp can deliver it),
  5. closes nudges still unanswered after 24 hours as 'no_reply', then lets the learning rule
     (learning.py) decide whether to ask that person a question.

later_time() answers "when is the one extra nudge after Later?": the person's usual hour (10:00 unless
they approved another) next morning. Later tapped before 05:00 still means this morning, and a learned
hour still ahead today is used today. Usual hours are always between 08 and 21, so a time Wazy chooses
here never falls in quiet hours (23:00 to 07:59). The demo setting later_minutes replaces all of this
with "in N minutes".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from . import learning
from .db import Database, from_db, to_db
from .replies import render
from .types import Reply

UTC = timezone.utc
LATE_AFTER = timedelta(minutes=10)      # a nudge more than 10 minutes late says when it was due
NO_REPLY_AFTER = timedelta(hours=24)    # a nudge unanswered for 24 hours is closed as 'no_reply'
NIGHT_ENDS = 5                          # Later tapped before 05:00 still means "this morning"

log = logging.getLogger(__name__)


class Scheduler:
    """Sends due nudges, closes unanswered ones, and works out the time of the nudge after Later."""

    def __init__(self, db: Database, *, later_minutes: Optional[int] = None,
                 on_message: Optional[Callable[[str, int, Reply], None]] = None) -> None:
        self.db = db
        self.later_minutes = later_minutes      # demo setting: Later means "in N minutes"
        self.on_message = on_message            # called as on_message(user_id, message_id, reply)

    # ----- sending nudges -----
    def run_due(self, now: datetime) -> int:
        """Send every nudge that is due and close nudges unanswered for 24 hours. Returns nudges sent."""
        now = now.astimezone(UTC)       # work in UTC so adding and subtracting hours is exact on DST days
        sent = 0
        for commitment_id in self._due_ids(now):
            if self._send_nudge(commitment_id, now):
                sent += 1
        self._close_unanswered(now)
        return sent

    def _due_ids(self, now: datetime) -> List[int]:
        """Ids of scheduled commitments that are due, of people who have not opted out, oldest first."""
        rows = self.db.query(
            "SELECT c.id FROM commitments c JOIN users u ON u.id = c.user_id "
            "WHERE c.status = 'scheduled' AND c.nudge_at <= ? AND u.opted_out = 0 "
            "ORDER BY c.nudge_at, c.id", (to_db(now),))
        return [row["id"] for row in rows]

    def _send_nudge(self, commitment_id: int, now: datetime) -> bool:
        """Claim one due commitment and store its nudge. False if someone else claimed it first."""
        with self.db.transaction():
            claimed = self.db.execute(
                "UPDATE commitments SET status = 'nudged' WHERE id = ? AND status = 'scheduled'",
                (commitment_id,)).rowcount
            if claimed != 1:
                return False            # another scheduler, a cancel or STOP got there first
            c = self.db.one("SELECT c.user_id, c.title, c.nudge_at, u.lang, u.tz FROM commitments c "
                            "JOIN users u ON u.id = c.user_id WHERE c.id = ?", (commitment_id,))
            self.db.execute("INSERT INTO nudge_outcomes (commitment_id, user_id, scheduled_for, nudged_at, outcome) "
                            "VALUES (?, ?, ?, ?, 'pending')", (commitment_id, c["user_id"], c["nudge_at"], to_db(now)))
            text = render("nudge", c["lang"], title=c["title"], due_text=self._due_text(c["nudge_at"], c["tz"], now))
            message_id = self.db.add_message(c["user_id"], "bot", text, "nudge", c["lang"], commitment_id, now)
        reply = Reply(kind="nudge", text=text, lang=c["lang"], commitment_id=commitment_id, buttons=True)
        self._deliver(c["user_id"], message_id, reply)
        return True

    @staticmethod
    def _due_text(nudge_at: str, tz: str, now: datetime) -> str:
        """'' when the nudge is on time, else the local clock time it was due, e.g. '09:00'."""
        due = from_db(nudge_at)
        if now - due <= LATE_AFTER:
            return ""
        return due.astimezone(ZoneInfo(tz)).strftime("%H:%M")

    # ----- closing nudges nobody answered -----
    def _close_unanswered(self, now: datetime) -> None:
        """Pending outcomes 24 hours old become 'no_reply'; then the learning rule may ask a question."""
        cutoff = to_db(now - NO_REPLY_AFTER)
        stale = self.db.query("SELECT id, commitment_id, user_id FROM nudge_outcomes "
                              "WHERE outcome = 'pending' AND nudged_at <= ? ORDER BY id", (cutoff,))
        people: List[str] = []
        for row in stale:
            if self._close(row["id"], row["commitment_id"]) and row["user_id"] not in people:
                people.append(row["user_id"])
        for user_id in people:
            self._ask_learning_question(user_id, now)

    def _close(self, outcome_id: int, commitment_id: int) -> bool:
        """Mark one outcome (and its commitment) as no_reply. False if it was answered meanwhile."""
        with self.db.transaction():
            closed = self.db.execute("UPDATE nudge_outcomes SET outcome = 'no_reply' "
                                     "WHERE id = ? AND outcome = 'pending'", (outcome_id,)).rowcount
            if closed == 1:
                self.db.execute("UPDATE commitments SET status = 'no_reply' WHERE id = ? AND status = 'nudged'",
                                (commitment_id,))
        return closed == 1

    def _ask_learning_question(self, user_id: str, now: datetime) -> None:
        """Let learning.consider() decide; if it has a question, store it and deliver it."""
        opted_out = self.db.scalar("SELECT opted_out FROM users WHERE id = ?", (user_id,))
        if opted_out:
            return                      # never write to someone who said STOP
        reply = learning.consider(self.db, user_id, now)
        if reply is None:
            return
        message_id = self.db.add_message(user_id, "bot", reply.text, reply.kind, reply.lang, None, now)
        self._deliver(user_id, message_id, reply)

    def _deliver(self, user_id: str, message_id: int, reply: Reply) -> None:
        """Hand a stored message to on_message. One failed delivery must not stop other people's nudges."""
        if self.on_message is None:
            return
        try:
            self.on_message(user_id, message_id, reply)
        except Exception:               # the message is stored and shown in the chat; log the id only, never text
            log.exception("on_message failed for message %s", message_id)

    # ----- the nudge after Later -----
    def later_time(self, user: dict, now: datetime) -> datetime:
        """When to nudge again after Later, as an aware datetime on the person's clock (user["tz"])."""
        zone = ZoneInfo(user["tz"])
        if self.later_minutes:
            return (now.astimezone(UTC) + timedelta(minutes=self.later_minutes)).astimezone(zone)
        local = now.astimezone(zone)
        hour = learning.usual_hour(user)
        today = datetime(local.year, local.month, local.day, hour, tzinfo=zone)
        if local.hour < NIGHT_ENDS:
            return today                # tapped in the small hours: "Later" means this morning
        if hour == user["learned_hour"] and today > local:
            return today                # their own learned hour is still ahead today
        tomorrow = local.date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, tzinfo=zone)
