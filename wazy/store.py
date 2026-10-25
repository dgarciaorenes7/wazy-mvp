"""Storage, conversation and scheduling for the Wazy MVP (SQLite, standard library only).

The loop this file implements, and nothing more:
    a commitment is texted -> Wazy confirms in one line when it will nudge
    -> at that time ONE nudge with Done / Later -> the outcome is recorded.
Every nudge ends in exactly one row of nudge_outcomes: done, later, stop or
no_reply (no answer within 24 hours). That table is the data cycle.
"""
from __future__ import annotations

import secrets
import sqlite3
import statistics
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional

from .parser import read_commitment

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT NOT NULL CHECK (role IN ('user', 'bot')),
    text          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'nudge')),
    commitment_id INTEGER REFERENCES commitments(id),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text   TEXT NOT NULL,             -- exactly what the person typed
    title      TEXT NOT NULL,             -- what Wazy understood
    nudge_at   TEXT NOT NULL,             -- when the (next) nudge is due
    status     TEXT NOT NULL CHECK (status IN ('scheduled', 'nudged', 'done', 'stopped', 'no_reply')),
    reader     TEXT NOT NULL,             -- 'rules' or 'rules + Claude Haiku'
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nudge_outcomes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id      INTEGER NOT NULL REFERENCES commitments(id),
    nudged_at          TEXT NOT NULL,
    outcome            TEXT NOT NULL CHECK (outcome IN ('pending', 'done', 'later', 'stop', 'no_reply')),
    responded_at       TEXT,
    seconds_to_respond INTEGER
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_commitments_due ON commitments(status, nudge_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_pending ON nudge_outcomes(outcome, nudged_at);
"""

STOP_WORDS = {"stop", "para", "unsubscribe", "stop nudges"}
HELLO_WORDS = {"hi", "hello", "hey", "hola", "start", "help", "ayuda", "buenas"}
DONE_WORDS = {"done", "hecho", "ok done", "listo", "✓ done"}
LATER_WORDS = {"later", "luego", "más tarde", "mas tarde", "snooze"}
NO_REPLY_AFTER = timedelta(hours=24)
LATER_HOUR = 10            # "Later" means: one more nudge, next morning at 10:00
NIGHT_ENDS = 5             # Later tapped before 05:00 still means "this morning", not the day after
OUTCOME_LABELS = {"done": "Done", "later": "Later", "stop": "Stopped", "no_reply": "No reply"}


def local_now() -> datetime:
    """This computer's local time. The server passes a clock in the chosen time zone instead (WAZY_TZ)."""
    return datetime.now().replace(microsecond=0)


def describe_when(when: datetime, now: datetime) -> str:
    """How the confirmation states the nudge time: exact for the next few minutes, plain words after."""
    seconds = (when - now).total_seconds()
    if seconds < 120:
        return f"at {when:%H:%M:%S} (in {max(1, round(seconds))} s)"
    if seconds < 3600:
        return f"at {when:%H:%M} (in {round(seconds / 60)} min)"
    if when.date() == now.date():
        return f"today at {when:%H:%M}"
    if when.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {when:%H:%M}"
    if (when.date() - now.date()).days < 7:
        return f"{when:%A} at {when:%H:%M}"
    return f"{when:%a %d %b} at {when:%H:%M}"


class Store:
    def __init__(self, db_path: str, api_key: str = "", ai_model: str = "",
                 later_minutes: Optional[int] = None, clock: Callable[[], datetime] = local_now) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.api_key, self.ai_model = api_key, ai_model
        self.later_minutes = later_minutes
        self.clock = clock
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        if not self._one("SELECT 1 FROM messages LIMIT 1"):
            self._say(self.welcome())

    # ---------- small helpers ----------
    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def _message(self, role: str, text: str, kind: str = "text", commitment_id: Optional[int] = None,
                 now: Optional[datetime] = None) -> None:
        self._run("INSERT INTO messages (role, text, kind, commitment_id, created_at) VALUES (?, ?, ?, ?, ?)",
                  (role, text, kind, commitment_id, (now or self.clock()).isoformat()))

    def _say(self, text: str, now: Optional[datetime] = None) -> None:
        self._message("bot", text, now=now)

    def setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT v FROM settings WHERE k = ?", (key,))
        return row["v"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO settings (k, v) VALUES (?, ?)", (key, value))

    def access_code(self, fixed: str = "") -> str:
        """The code a phone needs to reach this server (fixed in .env, else generated once and kept)."""
        code = fixed or self.setting("access_code") or secrets.token_urlsafe(9)
        self.set_setting("access_code", code)
        return code

    def welcome(self) -> str:
        processors = " and read by Anthropic's Claude to understand them" if self.api_key else ""
        return ("Hi, I'm Wazy.\nText me a small commitment in your own words, like “send Marta the slides by "
                "Thursday”. I'll confirm in one line and nudge you once, at the right moment, with Done / Later.\n\n"
                f"Privacy: your texts are stored on this test server only{processors}. Reply STOP any time.")

    def later_time(self, now: datetime) -> datetime:
        if self.later_minutes:
            return now + timedelta(minutes=self.later_minutes)
        morning = now.date() if now.hour < NIGHT_ENDS else now.date() + timedelta(days=1)
        return datetime.combine(morning, time(LATER_HOUR, 0))

    def latest_open_commitment(self) -> Optional[int]:
        row = self._one("SELECT id FROM commitments WHERE status = 'nudged' ORDER BY id DESC LIMIT 1")
        return row["id"] if row else None

    # ---------- the conversation ----------
    def handle_text(self, text: str, now: Optional[datetime] = None) -> None:
        now = now or self.clock()
        text = " ".join(str(text).split())[:300]
        if not text:
            return
        self._message("user", text, now=now)
        word = text.lower().strip(" .!")

        if word in STOP_WORDS:
            with self.lock:
                self.conn.execute("UPDATE nudge_outcomes SET outcome = 'stop', responded_at = ? "
                                  "WHERE outcome = 'pending'", (now.isoformat(),))
                self.conn.execute("UPDATE commitments SET status = 'stopped' WHERE status IN ('scheduled', 'nudged')")
                self.conn.commit()
            self._say("Stopped. I won't nudge you again unless you text me a new commitment.", now)
            return
        if word in HELLO_WORDS:
            self._say(self.welcome(), now)
            return
        if word in DONE_WORDS or word in LATER_WORDS:
            open_id = self.latest_open_commitment()
            if open_id:
                self.answer_nudge(open_id, "done" if word in DONE_WORDS else "later", now=now, echo=False)
            else:
                self._say("There's no nudge waiting for an answer right now. Text me a new commitment any time.", now)
            return

        reading = read_commitment(text, now, self.api_key, self.ai_model)
        pending_title = self.setting("pending_title")
        if reading.when is None:
            if reading.title:
                self.set_setting("pending_title", reading.title)
                self._say(f"When should I nudge you about “{reading.title}”? For example “in 2 minutes”, "
                          "“tomorrow 9am” or “Thursday”.", now)
            else:
                self._say("Tell me what to nudge you about, e.g. “send Marta the slides by Thursday”.", now)
            return
        title = reading.title or pending_title
        if not title:
            self._say("What should I nudge you about? Send it in one message, e.g. “call the bank tomorrow 9am”.", now)
            return
        self.set_setting("pending_title", "")
        self._run("INSERT INTO commitments (raw_text, title, nudge_at, status, reader, created_at) "
                  "VALUES (?, ?, ?, 'scheduled', ?, ?)",
                  (text, title, reading.when.isoformat(), reading.reader, now.isoformat()))
        self._say(f"Got it: “{title}”. I'll nudge you {describe_when(reading.when, now)}.", now)

    def answer_nudge(self, commitment_id: int, action: str, now: Optional[datetime] = None, echo: bool = True) -> bool:
        """Record Done or Later for the open nudge of a commitment. Returns False if there is nothing open."""
        now = now or self.clock()
        if action not in ("done", "later"):
            return False
        with self.lock:
            outcome = self.conn.execute(
                "SELECT * FROM nudge_outcomes WHERE commitment_id = ? AND outcome = 'pending' ORDER BY id DESC LIMIT 1",
                (commitment_id,)).fetchone()
            commitment = self.conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
            if outcome is None or commitment is None:
                return False
            seconds = int((now - datetime.fromisoformat(outcome["nudged_at"])).total_seconds())
            self.conn.execute("UPDATE nudge_outcomes SET outcome = ?, responded_at = ?, seconds_to_respond = ? "
                              "WHERE id = ?", (action, now.isoformat(), seconds, outcome["id"]))
            next_nudge = self.later_time(now)
            if action == "done":
                self.conn.execute("UPDATE commitments SET status = 'done' WHERE id = ?", (commitment_id,))
            else:
                self.conn.execute("UPDATE commitments SET status = 'scheduled', nudge_at = ? WHERE id = ?",
                                  (next_nudge.isoformat(), commitment_id))
            self.conn.commit()
        if echo:
            self._message("user", "✓ Done" if action == "done" else "Later", now=now)
        if action == "done":
            self._say(f"Nice, “{commitment['title']}” is done.", now)
        else:
            self._say(f"No problem. I'll nudge you once more {describe_when(next_nudge, now)}.", now)
        return True

    # ---------- the scheduler: code decides when ----------
    def run_due(self, now: Optional[datetime] = None) -> int:
        """Send every nudge that is due and close nudges left unanswered for 24 hours. Returns nudges sent."""
        now = now or self.clock()
        with self.lock:
            due = self.conn.execute("SELECT * FROM commitments WHERE status = 'scheduled' AND nudge_at <= ?",
                                    (now.isoformat(),)).fetchall()
            for commitment in due:
                self.conn.execute("UPDATE commitments SET status = 'nudged' WHERE id = ?", (commitment["id"],))
                self.conn.execute("INSERT INTO nudge_outcomes (commitment_id, nudged_at, outcome) "
                                  "VALUES (?, ?, 'pending')", (commitment["id"], now.isoformat()))
                self.conn.execute("INSERT INTO messages (role, text, kind, commitment_id, created_at) "
                                  "VALUES ('bot', ?, 'nudge', ?, ?)",
                                  (f"⏰ Nudge: {commitment['title']}", commitment["id"], now.isoformat()))
            cutoff = (now - NO_REPLY_AFTER).isoformat()
            stale = self.conn.execute("SELECT commitment_id FROM nudge_outcomes WHERE outcome = 'pending' "
                                      "AND nudged_at <= ?", (cutoff,)).fetchall()
            for row in stale:
                self.conn.execute("UPDATE commitments SET status = 'no_reply' WHERE id = ? AND status = 'nudged'",
                                  (row["commitment_id"],))
            self.conn.execute("UPDATE nudge_outcomes SET outcome = 'no_reply' WHERE outcome = 'pending' "
                              "AND nudged_at <= ?", (cutoff,))
            self.conn.commit()
        return len(due)

    # ---------- what the screens show ----------
    def state(self) -> dict:
        messages = self._all("SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 200) ORDER BY id")
        open_nudges = [row["message_id"] for row in self._all(
            "SELECT MAX(m.id) AS message_id FROM commitments c JOIN messages m "
            "ON m.commitment_id = c.id AND m.kind = 'nudge' WHERE c.status = 'nudged' GROUP BY c.id")]
        outcomes = self._all("SELECT o.*, c.title FROM nudge_outcomes o JOIN commitments c ON c.id = o.commitment_id "
                             "ORDER BY o.id DESC LIMIT 50")
        commitments = self._all("SELECT * FROM commitments ORDER BY id DESC LIMIT 50")
        return {
            "messages": messages,
            "open": open_nudges,
            "outcomes": outcomes,
            "commitments": commitments,
            "stats": self.stats(),
        }

    def stats(self) -> dict:
        """The Guess 1 numbers, over every row ever recorded (the lists above only show the latest 50)."""
        counts = self._one(
            "SELECT (SELECT COUNT(*) FROM commitments) AS commitments, COUNT(*) AS nudges, "
            "SUM(outcome != 'pending') AS answered, SUM(outcome = 'done') AS done FROM nudge_outcomes")
        done_seconds = [row["seconds_to_respond"] for row in self._all(
            "SELECT seconds_to_respond FROM nudge_outcomes WHERE outcome = 'done'")]
        answered, done = counts["answered"] or 0, counts["done"] or 0
        return {
            "commitments": counts["commitments"],
            "nudges": counts["nudges"],
            "done_rate": round(100 * done / answered) if answered else None,
            "median_seconds_to_done": statistics.median(done_seconds) if done_seconds else None,
        }

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def reset(self) -> None:
        with self.lock:
            self.conn.executescript("DELETE FROM messages; DELETE FROM nudge_outcomes; DELETE FROM commitments; "
                                    "DELETE FROM settings WHERE k = 'pending_title';")
            self.conn.commit()
        self._say(self.welcome())
