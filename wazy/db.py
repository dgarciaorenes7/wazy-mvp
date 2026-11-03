"""SQLite storage for Wazy (standard library only).

Every time in the database is UTC text, "YYYY-MM-DDTHH:MM:SSZ", so string order is time order.
Use to_db() and from_db(); never write datetimes any other way.

Schema version 2 (PRAGMA user_version = 2) adds people: every row belongs to a user_id
("web:<random>" for a browser, "whatsapp:+34600000000" for WhatsApp). A version 1 database
(the single-person MVP of Oct-Nov 2026) is copied to a backup file and migrated in place,
with every old row given to the user "web:legacy".
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is not supported
    ZoneInfo = None  # type: ignore

UTC = timezone.utc
SCHEMA_VERSION = 2
LEGACY_USER = "web:legacy"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 TEXT PRIMARY KEY,                 -- "web:<token>" or "whatsapp:+34..."
    channel            TEXT NOT NULL CHECK (channel IN ('web', 'whatsapp')),
    lang               TEXT NOT NULL DEFAULT 'en' CHECK (lang IN ('en', 'es')),
    tz                 TEXT NOT NULL,                    -- IANA zone, e.g. Europe/Madrid
    opted_out          INTEGER NOT NULL DEFAULT 0,       -- 1 after STOP until a new commitment or START
    learned_hour       INTEGER,                          -- default nudge hour the person said YES to
    learning_off       INTEGER NOT NULL DEFAULT 0,       -- 1 after "forget" until "learn again"
    learning_pending   TEXT,                             -- JSON {"hour": h} while a YES/NO question is open
    learning_asked_at  TEXT,
    pending_title      TEXT,                             -- waiting for a time for this task
    pending_raw        TEXT,                             -- the message that started the wait
    pending_when       TEXT,                             -- waiting for a task for this time (UTC)
    pending_stated     INTEGER NOT NULL DEFAULT 0,
    pending_deadline   INTEGER NOT NULL DEFAULT 0,
    pending_at         TEXT,                             -- when the wait started (expires after 2 h)
    pending_miss_id    INTEGER,                          -- the reader_misses row to complete with the reply
    last_list          TEXT,                             -- JSON list of commitment ids, as numbered in the last "list"
    created_at         TEXT NOT NULL,
    last_seen_at       TEXT
);
CREATE TABLE IF NOT EXISTS commitments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES users(id),
    raw_text     TEXT NOT NULL,                          -- exactly what the person typed
    title        TEXT NOT NULL,                          -- what Wazy understood
    nudge_at     TEXT NOT NULL,                          -- UTC, when the (next) nudge is due
    status       TEXT NOT NULL CHECK (status IN ('scheduled', 'nudged', 'done', 'stopped', 'no_reply', 'cancelled')),
    reader       TEXT NOT NULL DEFAULT 'rules',          -- 'rules' or 'rules + Claude Haiku'
    time_source  TEXT NOT NULL DEFAULT 'stated' CHECK (time_source IN ('stated', 'default', 'learned')),
    deadline     INTEGER NOT NULL DEFAULT 0,
    later_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nudge_outcomes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id      INTEGER NOT NULL REFERENCES commitments(id),
    user_id            TEXT NOT NULL REFERENCES users(id),
    scheduled_for      TEXT NOT NULL,                    -- UTC, when it was due
    nudged_at          TEXT NOT NULL,                    -- UTC, when it actually went out
    outcome            TEXT NOT NULL CHECK (outcome IN ('pending', 'done', 'later', 'stop', 'no_reply', 'cancelled')),
    responded_at       TEXT,
    seconds_to_respond INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id),
    role          TEXT NOT NULL CHECK (role IN ('user', 'bot')),
    text          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'text',          -- 'text' for the person; a reply kind for Wazy
    lang          TEXT NOT NULL DEFAULT 'en',
    commitment_id INTEGER REFERENCES commitments(id),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (                     -- WhatsApp delivery of Wazy's messages
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL UNIQUE REFERENCES messages(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    status          TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    provider_sid    TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(id),
    event       TEXT NOT NULL CHECK (event IN ('asked', 'approved', 'rejected', 'forgotten', 'paused', 'resumed')),
    old_hour    INTEGER,
    new_hour    INTEGER,
    evidence    TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_misses (               -- messages Wazy could not turn into a time, and the fix
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(id),
    original    TEXT NOT NULL,
    problem     TEXT NOT NULL,                           -- the reply kind Wazy gave: ask_time, invalid_time, ...
    reply       TEXT,                                    -- the person's next message that fixed it, if any
    created_at  TEXT NOT NULL,
    fixed_at    TEXT
);
CREATE TABLE IF NOT EXISTS inbound_seen (                -- provider message ids already handled (no double handling)
    provider_sid TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_commitments_due  ON commitments(status, nudge_at);
CREATE INDEX IF NOT EXISTS idx_commitments_user ON commitments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_outcomes_pending ON nudge_outcomes(outcome, nudged_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_user    ON nudge_outcomes(user_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_user    ON messages(user_id, id);
CREATE INDEX IF NOT EXISTS idx_outbox_due       ON outbox(status, next_attempt_at);
"""


# ---------------------------------------------------------------------------------------------
# Time in the database
# ---------------------------------------------------------------------------------------------
def to_db(when: datetime) -> str:
    """An aware datetime -> UTC text. A naive datetime is refused: it would be ambiguous."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("to_db needs a timezone-aware datetime")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_db(text: Optional[str]) -> Optional[datetime]:
    """UTC text from the database -> aware datetime in UTC (None stays None)."""
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _statements(script: str) -> List[str]:
    """Split a SQL script into statements (comments may contain semicolons), for use inside a transaction."""
    out, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            out.append(buffer.strip())
            buffer = ""
    return [s for s in out if s]


# ---------------------------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------------------------
class Database:
    """One SQLite connection shared by the HTTP threads and the scheduler, guarded by one lock.

    Reads and writes are small, so one lock is simpler and safe. Use transaction() for anything
    that must happen all together (e.g. claiming a due nudge).
    """

    def __init__(self, path: str = ":memory:", default_tz: str = "Europe/Madrid") -> None:
        self.path = path
        self.default_tz = default_tz
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # autocommit; we BEGIN ourselves
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.execute("PRAGMA busy_timeout = 5000")
            self._upgrade()

    # ----- basic access -----
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All-or-nothing block. Nested calls join the outer transaction."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ----- settings (global key/value, e.g. the access code) -----
    def setting(self, key: str, default: str = "") -> str:
        value = self.scalar("SELECT v FROM settings WHERE k = ?", (key,))
        return default if value is None else str(value)

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                     (key, value))

    # ----- people -----
    def user(self, user_id: str, now: datetime, channel: Optional[str] = None, tz: Optional[str] = None) -> dict:
        """Get a person, creating them on first contact. Never changes an existing person's zone."""
        row = self.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row:
            return row
        channel = channel or ("whatsapp" if user_id.startswith("whatsapp:") else "web")
        self.execute("INSERT OR IGNORE INTO users (id, channel, tz, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, channel, tz or self.default_tz, to_db(now)))
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))  # type: ignore[return-value]

    def update_user(self, user_id: str, **fields: Any) -> None:
        """Set columns on a person, e.g. update_user(uid, lang='es', pending_title=None)."""
        if not fields:
            return
        allowed = {r["name"] for r in self.query("PRAGMA table_info(users)")} - {"id"}
        bad = set(fields) - allowed
        if bad:
            raise KeyError(f"unknown user fields: {sorted(bad)}")
        cols = ", ".join(f"{name} = ?" for name in fields)
        self.execute(f"UPDATE users SET {cols} WHERE id = ?", (*fields.values(), user_id))

    # ----- messages -----
    def add_message(self, user_id: str, role: str, text: str, kind: str = "text", lang: str = "en",
                    commitment_id: Optional[int] = None, now: Optional[datetime] = None) -> int:
        """Store one chat message (the person's or Wazy's) and return its id."""
        when = now or datetime.now(UTC)
        cursor = self.execute(
            "INSERT INTO messages (user_id, role, text, kind, lang, commitment_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, role, text, kind, lang, commitment_id, to_db(when)))
        return int(cursor.lastrowid)

    # ----- schema and migration -----
    def _upgrade(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if version == 0 and "commitments" in tables:
            self._migrate_v1()
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database {self.path} is schema version {version}, newer than this Wazy "
                               f"({SCHEMA_VERSION}). Refusing to open it.")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v1(self) -> None:
        """Single-person database (Oct-Nov 2026) -> version 2. Keeps a backup copy first."""
        if self.path != ":memory:":
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            backup = f"{self.path}.v1-backup-{stamp}"
            shutil.copy2(self.path, backup)
        zone = ZoneInfo(self.default_tz) if ZoneInfo else None

        def utc(text: Optional[str]) -> Optional[str]:
            if not text:
                return None
            naive = datetime.fromisoformat(text)
            aware = naive.replace(tzinfo=zone) if (zone and naive.tzinfo is None) else naive
            if aware.tzinfo is None:
                aware = aware.astimezone()          # this computer's zone
            return to_db(aware)

        c = self.conn
        old_settings = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM settings")} \
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'settings'").fetchone() else {}
        c.execute("PRAGMA foreign_keys = OFF")      # old tables reference each other while being moved
        c.execute("BEGIN IMMEDIATE")
        try:
            for name in ("messages", "commitments", "nudge_outcomes", "learning_log", "reader_misses", "settings"):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone():
                    c.execute(f"ALTER TABLE {name} RENAME TO v1_{name}")
            for index in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                                 "AND name LIKE 'idx_%'")]:
                c.execute(f"DROP INDEX IF EXISTS {index}")
            for statement in _statements(SCHEMA):
                c.execute(statement)
            now = to_db(datetime.now(UTC))
            learned = old_settings.get("learned_hour") or None
            c.execute("INSERT INTO users (id, channel, tz, created_at, learned_hour, learning_off, learning_pending, "
                      "learning_asked_at) VALUES (?, 'web', ?, ?, ?, ?, ?, ?)",
                      (LEGACY_USER, self.default_tz, now, int(learned) if learned else None,
                       1 if old_settings.get("learning_off") else 0, old_settings.get("learning_pending") or None,
                       utc(old_settings.get("learning_asked_at"))))
            if old_settings.get("access_code"):
                c.execute("INSERT INTO settings (k, v) VALUES ('access_code', ?)", (old_settings["access_code"],))
            for r in c.execute("SELECT * FROM v1_commitments").fetchall():
                r = dict(r)
                c.execute("INSERT INTO commitments (id, user_id, raw_text, title, nudge_at, status, reader, "
                          "time_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (r["id"], LEGACY_USER, r["raw_text"], r["title"], utc(r["nudge_at"]), r["status"],
                           r.get("reader") or "rules", r.get("time_source") or "stated", utc(r["created_at"])))
            for r in c.execute("SELECT * FROM v1_nudge_outcomes").fetchall():
                r = dict(r)
                c.execute("INSERT INTO nudge_outcomes (id, commitment_id, user_id, scheduled_for, nudged_at, outcome, "
                          "responded_at, seconds_to_respond) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (r["id"], r["commitment_id"], LEGACY_USER, utc(r["nudged_at"]), utc(r["nudged_at"]),
                           r["outcome"], utc(r["responded_at"]), r["seconds_to_respond"]))
            for r in c.execute("SELECT * FROM v1_messages").fetchall():
                r = dict(r)
                c.execute("INSERT INTO messages (id, user_id, role, text, kind, commitment_id, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (r["id"], LEGACY_USER, r["role"], r["text"],
                           "nudge" if r.get("kind") == "nudge" else "text", r["commitment_id"], utc(r["created_at"])))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'v1_learning_log'").fetchone():
                for r in c.execute("SELECT * FROM v1_learning_log").fetchall():
                    r = dict(r)
                    c.execute("INSERT INTO learning_log (user_id, event, old_hour, new_hour, evidence, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (LEGACY_USER, r["event"], r["old_hour"], r["new_hour"],
                                                           r["evidence"], utc(r["created_at"])))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'v1_reader_misses'").fetchone():
                for r in c.execute("SELECT * FROM v1_reader_misses").fetchall():
                    r = dict(r)
                    c.execute("INSERT INTO reader_misses (user_id, original, problem, reply, created_at, fixed_at) "
                              "VALUES (?, ?, 'ask_time', ?, ?, ?)", (LEGACY_USER, r["original"], r["reply"],
                                                                     utc(r["created_at"]), utc(r["created_at"])))
            for name in ("messages", "commitments", "nudge_outcomes", "learning_log", "reader_misses", "settings"):
                c.execute(f"DROP TABLE IF EXISTS v1_{name}")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            problems = c.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Migration left {len(problems)} broken references; nothing was changed.")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.execute("PRAGMA foreign_keys = ON")
