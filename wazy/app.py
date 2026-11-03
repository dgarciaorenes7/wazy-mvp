"""The App: the one object the server, WhatsApp, the tests and the evals talk to (SPEC sections 2 and 2.1).

server.py and whatsapp.py call handle() for each message, tap() for each Done / Later button and tick()
every second; App adds "now" from its clock (tests and evals freeze that clock) and passes each call on.
It builds the three working parts:
  * the Database (db.py), where everything is kept,
  * the Scheduler (scheduler.py), which sends the nudges that are due,
  * the Conversation (conversation.py), which answers each message and each Done / Later tap.
It also builds the two snapshots the screens show: state(user_id) for one person's chat, and admin_state()
for the presenter view (everyone). A person's snapshot only ever holds that person's rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Optional

from zoneinfo import ZoneInfo

from .conversation import Conversation, zone_or_default
from .db import Database, from_db
from .scheduler import Scheduler
from .types import Reply

LATEST = 50                     # rows of each list in a snapshot (SPEC 2.1, state() details)
MESSAGES_SHOWN = 200            # chat messages in a snapshot (SPEC 2, state())

# "? IS NULL OR user_id = ?" in the queries below means: everyone when no user is given (admin_state).
MESSAGES_SQL = ("SELECT id, role, text, kind, lang, commitment_id, created_at FROM "
                "(SELECT * FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id")
OPEN_SQL = ("SELECT MAX(m.id) AS id FROM messages m JOIN nudge_outcomes o "
            "ON o.commitment_id = m.commitment_id AND o.outcome = 'pending' "
            "WHERE m.kind = 'nudge' AND m.role = 'bot' AND (? IS NULL OR m.user_id = ?) "
            "GROUP BY m.commitment_id ORDER BY id")
COMMITMENTS_SQL = ("SELECT c.id, c.user_id, c.title, c.raw_text, c.nudge_at, c.status, c.time_source, c.deadline, "
                   "c.later_count, c.reader, c.created_at, u.tz FROM commitments c JOIN users u ON u.id = c.user_id "
                   "WHERE (? IS NULL OR c.user_id = ?) ORDER BY c.id DESC LIMIT ?")
OUTCOMES_SQL = ("SELECT o.id, o.commitment_id, o.user_id, c.title, o.scheduled_for, o.nudged_at, o.outcome, "
                "o.responded_at, o.seconds_to_respond FROM nudge_outcomes o JOIN commitments c "
                "ON c.id = o.commitment_id WHERE (? IS NULL OR o.user_id = ?) ORDER BY o.id DESC LIMIT ?")
STATS_SQL = ("SELECT COUNT(*) AS nudges_sent, "
             "COALESCE(SUM(outcome = 'done'), 0) AS done, COALESCE(SUM(outcome = 'later'), 0) AS later, "
             "COALESCE(SUM(outcome = 'no_reply'), 0) AS no_reply, COALESCE(SUM(outcome = 'stop'), 0) AS stopped, "
             "COALESCE(SUM(outcome = 'cancelled'), 0) AS cancelled, COALESCE(SUM(outcome = 'pending'), 0) AS pending "
             "FROM nudge_outcomes WHERE (? IS NULL OR user_id = ?)")
USERS_SQL = ("SELECT u.id, u.channel, u.lang, u.tz, u.opted_out, u.learned_hour, "
             "(SELECT COUNT(*) FROM commitments c WHERE c.user_id = u.id) AS commitments, u.last_seen_at "
             "FROM users u ORDER BY u.last_seen_at DESC, u.created_at DESC")
# reset() empties these, children before parents (foreign keys). Settings and inbound_seen stay.
WIPED_TABLES = ("outbox", "messages", "nudge_outcomes", "commitments", "learning_log", "reader_misses", "users")


class App:
    """Wazy as one object. Every method uses clock() for "now", so tests and evals can freeze time."""

    def __init__(self, db_path: str = ":memory:", *, clock: Optional[Callable[[], datetime]] = None,
                 default_tz: str = "Europe/Madrid", later_minutes: Optional[int] = None, ai: Any = None,
                 on_message: Optional[Callable[[str, int, Reply], None]] = None) -> None:
        self.zone = ZoneInfo(default_tz)            # a wrong zone name fails here, before anything starts
        self.clock = clock or self._system_clock
        self.db = Database(db_path, default_tz)
        self.scheduler = Scheduler(self.db, later_minutes=later_minutes, on_message=on_message)
        self.conversation = Conversation(self.db, self.scheduler, ai=ai, on_message=on_message)

    def _system_clock(self) -> datetime:
        """The real time in the default zone, to the second."""
        return datetime.now(self.zone).replace(microsecond=0)

    def now(self) -> datetime:
        """clock(), always timezone-aware (a clock without a zone is read in the default zone)."""
        when = self.clock()
        return when if when.tzinfo is not None else when.replace(tzinfo=self.zone)

    # ----- what the server, WhatsApp, tests and evals call -----
    def handle(self, user_id: str, text: str, *, channel: str = "web", tz: Optional[str] = None) -> List[Reply]:
        """One message from a person -> Wazy's replies. `tz` is used only when the person is new."""
        return self.conversation.handle(user_id, text, self.now(), channel=channel, tz=tz)

    def tap(self, user_id: str, commitment_id: int, action: str) -> List[Reply]:
        """A Done / Later button. [] if that nudge is not open or not this person's (the server answers 409)."""
        return self.conversation.tap(user_id, commitment_id, action, self.now())

    def tick(self) -> int:
        """Run the scheduler once: send what is due, close nudges with no reply for 24 h."""
        return self.scheduler.run_due(self.now())

    def state(self, user_id: str) -> dict:
        """What one person's chat shows. Only that person's rows."""
        snapshot = self._snapshot(user_id)
        snapshot["messages"] = self._messages(user_id)
        return snapshot

    def admin_state(self) -> dict:
        """The presenter view: the same lists for everyone, the people, and the most recently active
        person's conversation (focus_user)."""
        snapshot = self._snapshot(None)
        focus = self.db.scalar("SELECT id FROM users ORDER BY last_seen_at DESC, created_at DESC LIMIT 1")
        snapshot["messages"] = self._messages(focus) if focus else []
        snapshot["focus_user"] = focus
        snapshot["users"] = self.db.query(USERS_SQL)
        return snapshot

    def reset(self) -> None:
        """Presenter only: wipe every person and everything they did. Settings (e.g. the access code) stay."""
        with self.db.transaction():
            for table in WIPED_TABLES:
                self.db.execute(f"DELETE FROM {table}")

    def close(self) -> None:
        """Close the database (the App cannot be used afterwards)."""
        self.db.close()

    # ----- building the snapshots -----
    def _snapshot(self, user_id: Optional[str]) -> dict:
        """Everything except the chat messages, for one person (user_id) or for everyone (None)."""
        open_nudges = self.db.query(OPEN_SQL, (user_id, user_id))
        commitments = self.db.query(COMMITMENTS_SQL, (user_id, user_id, LATEST))
        return {
            "open": [row["id"] for row in open_nudges],
            "commitments": [self._with_local_time(row) for row in commitments],
            "outcomes": self.db.query(OUTCOMES_SQL, (user_id, user_id, LATEST)),
            "stats": self._stats(user_id),
            "learning_log": self._newest("learning_log", user_id),
            "reader_misses": self._newest("reader_misses", user_id),
        }

    def _messages(self, user_id: str) -> List[dict]:
        """The last 200 chat messages of one person, oldest first."""
        return self.db.query(MESSAGES_SQL, (user_id, MESSAGES_SHOWN))

    def _with_local_time(self, row: dict) -> dict:
        """Add nudge_at_local: the nudge time on the person's own clock, "YYYY-MM-DDTHH:MM:SS"."""
        zone = zone_or_default(row.pop("tz"), self.db.default_tz)
        row["nudge_at_local"] = from_db(row["nudge_at"]).astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")
        return row

    def _stats(self, user_id: Optional[str]) -> dict:
        """Counts over every row (not just the latest 50). done_rate: whole percent of answered nudges."""
        stats = self.db.one(STATS_SQL, (user_id, user_id))
        stats["commitments"] = self.db.scalar("SELECT COUNT(*) FROM commitments WHERE (? IS NULL OR user_id = ?)",
                                              (user_id, user_id))
        answered = stats["done"] + stats["later"] + stats["no_reply"]
        stats["done_rate"] = round(100 * stats["done"] / answered) if answered else 0
        return stats

    def _newest(self, table: str, user_id: Optional[str]) -> List[dict]:
        """The newest 50 rows of learning_log or reader_misses."""
        return self.db.query(f"SELECT * FROM {table} WHERE (? IS NULL OR user_id = ?) ORDER BY id DESC LIMIT ?",
                             (user_id, user_id, LATEST))
