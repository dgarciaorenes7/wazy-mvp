"""Storage, conversation and scheduling for the Wazy MVP (SQLite, standard library only).

The loop this file implements, and nothing more:
    a commitment is texted -> Wazy confirms in one line when it will nudge
    -> at that time ONE nudge with Done / Later -> the outcome is recorded.
Every nudge ends in exactly one row of nudge_outcomes: done, later, stop or
no_reply (no answer within 24 hours). That table is the data cycle.

Learning loop (Session 7, 1 Nov 2026): from those outcomes Wazy notices when a
person actually ticks things off. If the hour it uses when they give no time
(10:00) keeps getting Later or no reply, it ASKS the person whether to use their
usual hour instead. Nothing changes until they reply YES; every proposal and
answer is a row in learning_log. Times the person stated are never changed, and
a learned hour can only move a deadline ("by Friday") earlier, never later.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import statistics
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional

from .parser import DEFAULT_HOUR, is_deadline, not_a_commitment, read_commitment

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
    created_at TEXT NOT NULL,
    time_source TEXT NOT NULL DEFAULT 'stated' CHECK (time_source IN ('stated', 'default', 'learned'))
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
CREATE TABLE IF NOT EXISTS learning_log (      -- every proposal, answer and reset, readable by the person and by us
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT NOT NULL CHECK (event IN ('asked', 'approved', 'rejected', 'forgotten', 'paused', 'resumed')),
    old_hour    INTEGER,
    new_hour    INTEGER,
    evidence    TEXT,                      -- the counts that triggered the proposal, as JSON
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_misses (     -- messages the rules could not read a time from, plus the reply that fixed it
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    original    TEXT NOT NULL,
    reply       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
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
YES_WORDS = {"yes", "y", "yep", "yeah", "sure", "ok", "okay", "si", "sí", "vale", "dale"}
NO_WORDS = {"no", "nope", "no thanks", "no gracias", "nah"}
WHAT_WORDS = {"what did you learn", "what have you learned", "what have you learnt", "what do you know about me",
              "que has aprendido", "qué has aprendido"}
FORGET_WORDS = {"forget", "forget me", "forget it", "reset learning", "stop learning", "olvida", "olvídalo", "olvidalo"}
RESUME_WORDS = {"learn again", "start learning", "vuelve a aprender"}
# The learning rule (kept simple on purpose, Huyen: change a rule before training anything)
MIN_DEFAULT_NUDGES = 3     # at least 3 answered nudges at the default hour...
MAX_DEFAULT_DONE_SHARE = 0.5   # ...of which fewer than half were Done
MIN_DONE_AT_NEW_HOUR = 3   # and at least 3 Done taps in the same hour somewhere else
EARLIEST_HOUR, LATEST_HOUR = 8, 21   # never propose nudging outside 08:00-21:59
ASK_AGAIN_AFTER = timedelta(days=14)  # one question per fortnight at most, so learning never becomes nagging


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
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(commitments)")}
            if "time_source" not in columns:   # databases made before 1 Nov 2026
                self.conn.execute("ALTER TABLE commitments ADD COLUMN time_source TEXT NOT NULL DEFAULT 'stated'")
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
                f"Privacy: your texts are stored on this test server only{processors}. Reply STOP any time.\n\n"
                "I also notice when you usually tick things off, so I can nudge you at a better time. I always ask "
                "before changing anything. Text “what did you learn” to see it, or “forget” to wipe it and stop.")

    def learned_hour(self) -> Optional[int]:
        value = self.setting("learned_hour")
        return int(value) if value else None

    def default_hour(self) -> int:
        return self.learned_hour() or DEFAULT_HOUR

    def later_time(self, now: datetime) -> datetime:
        if self.later_minutes:
            return now + timedelta(minutes=self.later_minutes)
        hour = self.learned_hour() or LATER_HOUR
        morning = now.date() if now.hour < NIGHT_ENDS else now.date() + timedelta(days=1)
        if hour > now.hour and now.hour >= NIGHT_ENDS and self.learned_hour():
            morning = now.date()              # learned evening hour still ahead today: use it today
        return datetime.combine(morning, time(hour, 0))

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
        if self.setting("learning_pending") and (word in YES_WORDS or word in NO_WORDS):
            self.answer_learning(word in YES_WORDS, now)
            return
        if word in WHAT_WORDS:
            self._say(self.explain_learning(), now)
            return
        if word in FORGET_WORDS:
            self.forget_learning(now)
            return
        if word in RESUME_WORDS:
            self.set_setting("learning_off", "")
            self._log_learning("resumed", now=now)
            self._say("OK, I'll start noticing when you tick things off again. I'll still ask before changing anything.", now)
            return
        if word in DONE_WORDS or word in LATER_WORDS:
            open_id = self.latest_open_commitment()
            if open_id:
                self.answer_nudge(open_id, "done" if word in DONE_WORDS else "later", now=now, echo=False)
            else:
                self._say("There's no nudge waiting for an answer right now. Text me a new commitment any time.", now)
            return

        if not_a_commitment(text):
            self._say("For now I only take new commitments, one per message, e.g. “send Marta the slides "
                      "by Thursday”. I can't answer questions or change an earlier one yet.", now)
            return
        reading = read_commitment(text, now, self.api_key, self.ai_model)
        pending_title = self.setting("pending_title")
        if reading.when is None:
            if reading.title:
                self.set_setting("pending_title", reading.title)
                self.set_setting("pending_raw", text)
                self._say(f"When should I nudge you about “{reading.title}”? For example “in 2 minutes”, "
                          "“tomorrow 9am” or “Thursday”.", now)
            else:
                self._say("Tell me what to nudge you about, e.g. “send Marta the slides by Thursday”.", now)
            return
        title = reading.title or pending_title
        if not title:
            self._say("What should I nudge you about? Send it in one message, e.g. “call the bank tomorrow 9am”.", now)
            return
        if not reading.title and pending_title and self.setting("pending_raw"):
            # The rules missed the time in the first message and this reply supplied it: keep both so we can
            # add a rule for that phrasing (the founder reviews these, see architecturedoc.md, Session 7).
            self._run("INSERT INTO reader_misses (original, reply, created_at) VALUES (?, ?, ?)",
                      (self.setting("pending_raw"), text, now.isoformat()))
        self.set_setting("pending_title", "")
        self.set_setting("pending_raw", "")
        when, source = reading.when, ("stated" if reading.time_stated else "default")
        learned = self.learned_hour()
        if source == "default" and learned is not None and when.hour == DEFAULT_HOUR and when.minute == 0:
            candidate = when.replace(hour=learned)
            # Red line: a learned hour never pushes a deadline later, and never lands in the past.
            if candidate > now and not (is_deadline(text) and learned > DEFAULT_HOUR):
                when, source = candidate, "learned"
        self._run("INSERT INTO commitments (raw_text, title, nudge_at, status, reader, created_at, time_source) "
                  "VALUES (?, ?, ?, 'scheduled', ?, ?, ?)",
                  (text, title, when.isoformat(), reading.reader, now.isoformat(), source))
        why = " (your usual time)" if source == "learned" else ""
        self._say(f"Got it: “{title}”. I'll nudge you {describe_when(when, now)}{why}.", now)

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
        self.consider_learning(now)
        return True

    # ---------- the learning loop (Session 7) ----------
    def _log_learning(self, event: str, old: Optional[int] = None, new: Optional[int] = None,
                      evidence: Optional[dict] = None, now: Optional[datetime] = None) -> None:
        self._run("INSERT INTO learning_log (event, old_hour, new_hour, evidence, created_at) VALUES (?, ?, ?, ?, ?)",
                  (event, old, new, json.dumps(evidence) if evidence else None, (now or self.clock()).isoformat()))

    def learning_evidence(self) -> dict:
        """The numbers the rule looks at, from the last 30 answered nudges. Code, not a model: anyone can check it."""
        rows = self._all(
            "SELECT o.outcome, o.nudged_at, o.responded_at, c.time_source FROM nudge_outcomes o "
            "JOIN commitments c ON c.id = o.commitment_id WHERE o.outcome IN ('done', 'later', 'no_reply') "
            "ORDER BY o.id DESC LIMIT 30")
        current = self.default_hour()
        at_default = [r for r in rows if r["time_source"] in ("default", "learned")
                      and datetime.fromisoformat(r["nudged_at"]).hour == current]
        done_hours: dict = {}
        for r in rows:
            if r["outcome"] == "done" and r["responded_at"]:
                hour = datetime.fromisoformat(r["responded_at"]).hour
                done_hours[hour] = done_hours.get(hour, 0) + 1
        best = max(done_hours, key=lambda h: (done_hours[h], -abs(h - current))) if done_hours else None
        return {
            "current_hour": current,
            "answered_at_current_hour": len(at_default),
            "done_at_current_hour": sum(r["outcome"] == "done" for r in at_default),
            "best_hour": best,
            "done_at_best_hour": done_hours.get(best, 0) if best is not None else 0,
        }

    def consider_learning(self, now: Optional[datetime] = None) -> bool:
        """Propose (never apply) a better default hour. Returns True if a question was sent."""
        now = now or self.clock()
        if self.setting("learning_off") or self.setting("learning_pending"):
            return False
        last = self.setting("learning_asked_at")
        if last and now - datetime.fromisoformat(last) < ASK_AGAIN_AFTER:
            return False
        e = self.learning_evidence()
        answered, done = e["answered_at_current_hour"], e["done_at_current_hour"]
        best = e["best_hour"]
        if (answered < MIN_DEFAULT_NUDGES or done / answered >= MAX_DEFAULT_DONE_SHARE or best is None
                or best == e["current_hour"] or e["done_at_best_hour"] < MIN_DONE_AT_NEW_HOUR
                or not EARLIEST_HOUR <= best <= LATEST_HOUR):
            return False
        self.set_setting("learning_pending", json.dumps({"hour": best}))
        self.set_setting("learning_asked_at", now.isoformat())
        self._log_learning("asked", e["current_hour"], best, e, now)
        self._say(f"Quick question. When you don't give me a time I nudge you at {e['current_hour']:02d}:00, but "
                  f"{answered - done} of your last {answered} nudges at that time got Later or no reply, and you've "
                  f"ticked {e['done_at_best_hour']} things off around {best:02d}:00. Shall I use {best:02d}:00 from now "
                  "on? Times you give me yourself won't change. Reply YES or NO.", now)
        return True

    def answer_learning(self, yes: bool, now: Optional[datetime] = None) -> None:
        now = now or self.clock()
        proposal = json.loads(self.setting("learning_pending") or "{}")
        self.set_setting("learning_pending", "")
        old, new = self.default_hour(), proposal.get("hour")
        if yes and new is not None:
            self.set_setting("learned_hour", str(new))
            self._log_learning("approved", old, new, now=now)
            self._say(f"Done. When you don't give a time I'll nudge you at {new:02d}:00. Text “forget” to undo it.", now)
        else:
            self.set_setting("learning_asked_at", now.isoformat())   # the two weeks count from the answer
            self._log_learning("rejected", old, new, now=now)
            self._say(f"OK, I'll keep {old:02d}:00. I won't ask again for at least two weeks.", now)

    def explain_learning(self) -> str:
        if self.setting("learning_off"):
            return "I'm not learning anything right now (you told me to forget). Text “learn again” to turn it back on."
        learned = self.learned_hour()
        if learned:
            asked = self._one("SELECT evidence FROM learning_log WHERE event = 'asked' AND new_hour = ? "
                              "ORDER BY id DESC LIMIT 1", (learned,))
            e = json.loads(asked["evidence"]) if asked and asked["evidence"] else {}
            why = (f" I suggested it because {e['answered_at_current_hour'] - e['done_at_current_hour']} of "
                   f"{e['answered_at_current_hour']} nudges at {e['current_hour']:02d}:00 got Later or no reply, and you "
                   f"had ticked {e['done_at_best_hour']} things off around {learned:02d}:00." if e else "")
            first = f"When you don't give me a time I nudge you at {learned:02d}:00, because you said YES to that.{why}"
        else:
            e = self.learning_evidence()
            first = (f"When you don't give me a time I nudge you at {DEFAULT_HOUR:02d}:00 (my default). So far "
                     f"{e['done_at_current_hour']} of {e['answered_at_current_hour']} nudges at that hour got Done.")
        return (f"{first} What I keep: for each nudge, the hour it went out and whether you tapped Done, Later or "
                "nothing. I never change a time you gave me, and I always ask first. "
                "Text “forget” to wipe this and stop.")

    def forget_learning(self, now: Optional[datetime] = None) -> None:
        now = now or self.clock()
        old = self.learned_hour()
        for key in ("learned_hour", "learning_pending", "learning_asked_at"):
            self.set_setting(key, "")
        self.set_setting("learning_off", "1")
        self._log_learning("forgotten", old, DEFAULT_HOUR, now=now)
        self._say(f"Done. I've wiped what I learned and I'm back to {DEFAULT_HOUR:02d}:00. I won't learn from your "
                  "answers again unless you text “learn again”. Your commitments and nudges are unchanged.", now)

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
        if stale:
            self.consider_learning(now)
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
            "learning_log": self._all("SELECT * FROM learning_log ORDER BY id DESC LIMIT 20"),
            "reader_misses": self._all("SELECT * FROM reader_misses ORDER BY id DESC LIMIT 20"),
        }

    def stats(self) -> dict:
        """The Guess 1 numbers, over every row ever recorded (the lists above only show the latest 50)."""
        counts = self._one(
            "SELECT (SELECT COUNT(*) FROM commitments) AS commitments, COUNT(*) AS nudges, "
            "SUM(outcome != 'pending') AS answered, SUM(outcome = 'done') AS done FROM nudge_outcomes")
        done_seconds = [row["seconds_to_respond"] for row in self._all(
            "SELECT seconds_to_respond FROM nudge_outcomes WHERE outcome = 'done'")]
        answered, done = counts["answered"] or 0, counts["done"] or 0
        # Growth metric 1: share of commitments ticked off at the FIRST nudge (no Later needed).
        first = self._all("SELECT o.outcome FROM nudge_outcomes o WHERE o.id IN "
                          "(SELECT MIN(id) FROM nudge_outcomes WHERE outcome != 'pending' GROUP BY commitment_id)")
        first_done = sum(r["outcome"] == "done" for r in first)
        return {
            "commitments": counts["commitments"],
            "nudges": counts["nudges"],
            "done_rate": round(100 * done / answered) if answered else None,
            "first_nudge_done_rate": round(100 * first_done / len(first)) if first else None,
            "median_seconds_to_done": statistics.median(done_seconds) if done_seconds else None,
            "learned_hour": self.learned_hour(),
            "reader_misses": self._one("SELECT COUNT(*) AS n FROM reader_misses")["n"],
        }

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def reset(self) -> None:
        with self.lock:
            self.conn.executescript("DELETE FROM messages; DELETE FROM nudge_outcomes; DELETE FROM commitments; "
                                    "DELETE FROM learning_log; DELETE FROM reader_misses; "
                                    "DELETE FROM settings WHERE k != 'access_code';")
            self.conn.commit()
        self._say(self.welcome())
