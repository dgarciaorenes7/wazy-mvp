"""Concurrency: the HTTP threads (handle / tap / state), the scheduler thread (tick) and the WhatsApp sender
share one Database. These tests run real threads against a real App (in memory, and two Apps on one WAL file,
as two processes would during a quick restart) and check the promises that must survive any interleaving:

  * one nudge per claim: nudge messages per commitment == outcomes, == 1 + later_count once the last one went
    out; at most one pending outcome; the commitment's status always matches its last outcome,
  * a Done tap and the 24 h no-reply close: exactly one wins, and a tap that loses stores no "Done";
    two Done taps: one done_ack; two Later taps: later_count + 1 once,
  * after STOP returns, no new nudge (nor a learning question) for that person, also not over WhatsApp,
  * Database.transaction(): all or nothing, nested blocks join, one thread at a time, no dirty reads, and a
    failed COMMIT never leaves the connection stuck,
  * the WhatsApp outbox: one row per message, each row sent at most once.

Run: python3.9 -m unittest tests.test_concurrency -v
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from wazy.app import App
from wazy.db import Database
from wazy.types import Reply
from wazy.whatsapp import WhatsApp

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 11, 3, 9, 30, tzinfo=MADRID)          # Tuesday 3 November 2026, 09:30 in Madrid
ANA = "web:ana"
PHONE = "whatsapp:+34600000001"
ACCOUNT, TOKEN = "AC" + "a" * 32, "b" * 32
FROM, PUBLIC = "whatsapp:+14155238886", "https://wazy.example.com"
# What an answered nudge's last outcome means for the commitment's status.
STATUS_FOR_LAST_OUTCOME = {
    None: {"scheduled", "stopped", "cancelled"},           # never nudged
    "pending": {"nudged"},
    "done": {"done"},
    "no_reply": {"no_reply"},
    "stop": {"stopped"},
    "cancelled": {"cancelled"},
    "later": {"scheduled", "stopped", "cancelled"},        # the nudge after Later has not gone out (yet)
}


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
class Clock:
    """A clock several threads may read and move. Kept in UTC so adding hours is exact."""

    def __init__(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.now

    def advance(self, **delta) -> None:
        with self.lock:
            self.now += timedelta(**delta)

    def set(self, when: datetime) -> None:
        with self.lock:
            self.now = when.astimezone(UTC)


class Recorder:
    """An on_message hook that remembers every call, in order, from any thread."""

    def __init__(self) -> None:
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, user_id: str, message_id: int, reply: Reply) -> None:
        with self.lock:
            self.calls.append((user_id, message_id, reply.kind, reply.commitment_id))


class FakeTwilio:
    """Stands in for urllib's urlopen: every POST succeeds with a new message id, unless a failure is queued."""

    def __init__(self) -> None:
        self.forms = []
        self.sids = []
        self.failures = []              # HTTP status codes to answer with, one per request, before succeeding
        self.lock = threading.Lock()

    def __call__(self, request, timeout=None):
        import urllib.error
        import urllib.parse
        with self.lock:
            self.forms.append(dict(urllib.parse.parse_qsl(request.data.decode("utf-8"))))
            if self.failures:
                raise urllib.error.HTTPError(request.full_url, self.failures.pop(0), "error", {}, None)
            sid = "SM%032d" % (len(self.sids) + 1)
            self.sids.append(sid)
        time.sleep(0.0005)              # a little network time, so senders overlap
        return _Response(json.dumps({"sid": sid}).encode())


class _Response:
    status = 201

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body

    def close(self) -> None:
        pass


def run_together(test: unittest.TestCase, *workers, timeout: float = 20.0) -> None:
    """Start every worker at the same moment and wait. Any exception (e.g. "database is locked") or a thread
    that never finishes (a deadlock) fails the test."""
    barrier = threading.Barrier(len(workers))
    errors = []

    def wrap(work):
        def run():
            try:
                barrier.wait(timeout)
                work()
            except BaseException:       # anything at all is a failure, shown with its traceback
                errors.append(traceback.format_exc())
        return run

    threads = [threading.Thread(target=wrap(work), daemon=True) for work in workers]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    test.assertFalse([t for t in threads if t.is_alive()], "a thread never finished (deadlock?)")
    test.assertEqual(errors, [], "\n".join(errors))


def after(seconds: float, work):
    """work, started a moment late. Varying the delay from round to round lets each side of a race win
    sometimes (a tick is quicker to reach its claim than a tap or a STOP, so it would nearly always win)."""
    def run():
        time.sleep(seconds)
        work()
    return run


def check_invariants(test: unittest.TestCase, db: Database) -> None:
    """One commitment, one nudge per claim, one recorded outcome per nudge; Later adds exactly one nudge."""
    for c in db.query("SELECT * FROM commitments ORDER BY id"):
        cid = c["id"]
        outcomes = [r["outcome"] for r in db.query(
            "SELECT outcome FROM nudge_outcomes WHERE commitment_id = ? ORDER BY id", (cid,))]
        nudges = db.scalar("SELECT COUNT(*) FROM messages WHERE kind = 'nudge' AND commitment_id = ?", (cid,))
        last = outcomes[-1] if outcomes else None
        where = f"commitment {cid}: status {c['status']}, later_count {c['later_count']}, outcomes {outcomes}"
        test.assertEqual(nudges, len(outcomes), "one nudge message per claim; " + where)
        test.assertLessEqual(outcomes.count("pending"), 1, where)
        test.assertTrue(all(o == "later" for o in outcomes[:-1]), "only Later leads to another nudge; " + where)
        test.assertEqual(outcomes.count("later"), c["later_count"], where)
        test.assertIn(c["status"], STATUS_FOR_LAST_OUTCOME[last], where)
        waiting = last in (None, "later")                  # the next nudge has not gone out
        test.assertEqual(nudges, c["later_count"] + (0 if waiting else 1), where)
        for kind, outcome in (("done_ack", "done"), ("later_ack", "later")):
            acks = db.scalar("SELECT COUNT(*) FROM messages WHERE kind = ? AND commitment_id = ?", (kind, cid))
            test.assertEqual(acks, outcomes.count(outcome), f"{kind}: " + where)


def bot_messages(db: Database, user_id: str, kind: str) -> list:
    return [r["id"] for r in db.query("SELECT id FROM messages WHERE user_id = ? AND role = 'bot' AND kind = ? "
                                      "ORDER BY id", (user_id, kind))]


def make_app(clock: Clock, path: str = ":memory:", **options) -> App:
    return App(path, clock=clock, **options)


def new_commitment(app: App, user_id: str, text: str = "Call mum in 1 minute", **options) -> int:
    replies = app.handle(user_id, text, **options)
    confirm = [r for r in replies if r.kind == "confirm"]
    assert confirm, [r.kind for r in replies]
    return confirm[0].commitment_id


class TempFolder(unittest.TestCase):
    def temp_path(self, name: str) -> str:
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        return os.path.join(folder, name)


# =============================================================================================
# Database.transaction()
# =============================================================================================
class TransactionTests(TempFolder):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
        self.db.execute("INSERT INTO counter VALUES (0)")
        self.db.execute("CREATE TABLE log (thread INTEGER, step TEXT)")

    def count(self) -> int:
        return self.db.scalar("SELECT n FROM counter")

    def test_an_exception_inside_undoes_every_statement_and_frees_the_lock(self):
        with self.assertRaises(ValueError):
            with self.db.transaction():
                self.db.execute("UPDATE counter SET n = 5")
                self.db.set_setting("k", "v")
                raise ValueError("bug")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.db.setting("k", "none"), "none")
        self.assertFalse(self.db.conn.in_transaction)
        self.assertEqual(self.db._depth, 0)
        other = threading.Thread(target=self.db.execute, args=("UPDATE counter SET n = 1",))
        other.start()
        other.join(2)
        self.assertFalse(other.is_alive(), "the lock was not released")
        self.assertEqual(self.count(), 1)

    def test_nested_blocks_join_the_outer_one(self):
        path = self.temp_path("nested.db")
        db, watcher = Database(path), Database(path)
        self.addCleanup(db.close)
        self.addCleanup(watcher.close)
        with db.transaction():
            with db.transaction():
                db.set_setting("inner", "1")
            self.assertTrue(db.conn.in_transaction)            # the inner block did not commit on its own
            self.assertEqual(watcher.setting("inner", "unseen"), "unseen")
            db.set_setting("outer", "1")
        self.assertEqual((watcher.setting("inner"), watcher.setting("outer")), ("1", "1"))
        with self.assertRaises(KeyError):                      # an error in the inner block undoes both
            with db.transaction():
                db.set_setting("a", "1")
                with db.transaction():
                    db.set_setting("b", "1")
                    raise KeyError("bug")
        self.assertEqual((db.setting("a", "-"), db.setting("b", "-")), ("-", "-"))
        self.assertEqual(db._depth, 0)

    def test_one_transaction_at_a_time_and_never_interleaved(self):
        def worker(n: int):
            def run():
                for _ in range(25):
                    with self.db.transaction():
                        self.db.execute("INSERT INTO log VALUES (?, 'begin')", (n,))
                        value = self.count()
                        time.sleep(0)                          # invite another thread in: it must wait
                        self.db.execute("UPDATE counter SET n = ?", (value + 1,))
                        self.db.execute("INSERT INTO log VALUES (?, 'end')", (n,))
            return run

        run_together(self, *[worker(n) for n in range(8)])
        self.assertEqual(self.count(), 200)                     # no update was lost
        steps = self.db.query("SELECT thread, step FROM log ORDER BY rowid")
        for begin, end in zip(steps[0::2], steps[1::2]):        # every begin is followed by its own end
            self.assertEqual((begin["step"], end["step"], begin["thread"]), ("begin", "end", end["thread"]))

    def test_a_read_from_another_thread_waits_and_never_sees_uncommitted_rows(self):
        inside, seen = threading.Event(), []

        def writer():
            with self.assertRaises(RuntimeError):
                with self.db.transaction():
                    self.db.execute("UPDATE counter SET n = 99")
                    inside.set()
                    time.sleep(0.2)                            # the reader is waiting now
                    raise RuntimeError("changed my mind")

        def reader():
            inside.wait(5)
            seen.append(self.count())                          # same connection: only the lock stops a dirty read

        run_together(self, writer, reader)
        self.assertEqual(seen, [0])

    def test_reads_and_writes_outside_transactions_from_many_threads(self):
        def worker():
            for _ in range(50):
                self.db.execute("UPDATE counter SET n = n + 1")
                self.db.query("SELECT * FROM counter")
                self.db.one("SELECT n FROM counter")

        run_together(self, *[worker for _ in range(6)])
        self.assertEqual(self.count(), 300)

    def test_a_failed_commit_does_not_leave_the_connection_stuck(self):
        # Without WAL (some file systems refuse it) a reader in another process makes COMMIT fail with
        # "database is locked". SQLite then keeps the transaction open; it must be rolled back, or every later
        # transaction fails with "cannot start a transaction within a transaction" and later writes are lost.
        path = self.temp_path("journal.db")
        db = Database(path)
        self.addCleanup(db.close)
        db.conn.execute("PRAGMA journal_mode = DELETE")
        db.conn.execute("PRAGMA busy_timeout = 50")
        reader = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM settings").fetchone()     # holds a shared lock
        with self.assertRaises(sqlite3.OperationalError):
            with db.transaction():
                db.set_setting("lost", "1")
        reader.execute("ROLLBACK")
        self.assertFalse(db.conn.in_transaction)
        self.assertEqual(db._depth, 0)
        with db.transaction():
            db.set_setting("kept", "1")
        self.assertEqual(reader.execute("SELECT k FROM settings").fetchall(), [("kept",)])

    def test_sqlite_rolling_back_by_itself_does_not_hide_the_real_error(self):
        # On "disk full" SQLite ends the transaction itself; a second ROLLBACK would raise "cannot rollback -
        # no transaction is active" and hide the real reason from the log.
        self.db.conn.execute("PRAGMA max_page_count = 40")
        with self.assertRaises(sqlite3.OperationalError) as caught:
            with self.db.transaction():
                for n in range(100):
                    self.db.set_setting(f"k{n}", "x" * 4000)
        self.assertIn("full", str(caught.exception))
        self.assertFalse(self.db.conn.in_transaction)
        self.assertEqual(self.db._depth, 0)

    def test_begin_that_times_out_leaves_nothing_behind(self):
        path = self.temp_path("busy.db")
        first, second = Database(path), Database(path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.conn.execute("PRAGMA busy_timeout = 50")
        with second.transaction():
            with self.assertRaises(sqlite3.OperationalError):  # "database is locked": the other one writes
                with first.transaction():
                    pass
            self.assertEqual(first._depth, 0)
        with first.transaction():
            first.set_setting("after", "1")
        self.assertEqual(second.setting("after"), "1")


# =============================================================================================
# Nudges, taps, STOP and cancel racing on one App (in memory)
# =============================================================================================
class NudgeRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(T0)
        self.recorder = Recorder()
        self.app = make_app(self.clock, later_minutes=1, on_message=self.recorder)
        self.addCleanup(self.app.close)
        self.db = self.app.db

    def nudge_now(self, user_id: str = ANA) -> int:
        """A new commitment for user_id, already nudged (its outcome is pending)."""
        cid = new_commitment(self.app, user_id)
        self.clock.advance(minutes=1)
        self.assertEqual(self.app.tick(), 1)
        return cid

    def outcome(self, cid: int) -> list:
        return [r["outcome"] for r in self.db.query(
            "SELECT outcome FROM nudge_outcomes WHERE commitment_id = ? ORDER BY id", (cid,))]

    def status(self, cid: int) -> str:
        return self.db.scalar("SELECT status FROM commitments WHERE id = ?", (cid,))

    def test_ticks_taps_stop_and_cancel_for_one_person(self):
        texts = ["Call mum in 1 minute", "Send Marta the slides in 2 minutes", "Water the plants in 1 minute",
                 "Book the dentist in 3 minutes", "Pay the rent in 1 minute", "Buy milk in 2 minutes"]
        for text in texts:
            new_commitment(self.app, ANA, text)
        sent, guard = [], threading.Lock()

        def ticker(seed: int):
            def run():
                rng = random.Random(seed)
                for n in range(60):
                    if (seed, n) == (1, 30):
                        self.clock.advance(hours=25)            # once: unanswered nudges reach 24 h
                    else:
                        self.clock.advance(seconds=rng.randint(5, 30))
                    count = self.app.tick()
                    with guard:
                        sent.append(count)
                    self.app.state(ANA)
            return run

        def tapper(action: str, seed: int):
            def run():
                rng = random.Random(seed)
                for _ in range(80):
                    last = self.db.scalar("SELECT MAX(id) FROM commitments")
                    self.app.tap(ANA, rng.randint(1, last), action)
            return run

        def typer():
            script = ["later", "done", "list", "cancel 2", "stop", "Call mum in 1 minute", "start",
                      "Send the report in 2 minutes", "cancel", "Water the plants in 1 minute", "done", "later"]
            for _ in range(4):
                for text in script:
                    self.app.handle(ANA, text)
                    self.app.admin_state()

        run_together(self, ticker(1), ticker(2), ticker(3), tapper("done", 4), tapper("done", 5),
                     tapper("later", 6), tapper("later", 7), typer)
        check_invariants(self, self.db)
        self.assertEqual(sum(sent), self.db.scalar("SELECT COUNT(*) FROM messages WHERE kind = 'nudge'"))
        self.assert_nothing_old_after_stop(ANA)

    def assert_nothing_old_after_stop(self, user_id: str) -> None:
        """Every nudge stored after a STOP is for a commitment the person made after that STOP."""
        stops = bot_messages(self.db, user_id, "stopped")
        for nudge in self.db.query("SELECT id, commitment_id FROM messages WHERE user_id = ? AND kind = 'nudge'",
                                   (user_id,)):
            before = [s for s in stops if s < nudge["id"]]
            if before:
                confirm = self.db.scalar("SELECT MIN(id) FROM messages WHERE kind = 'confirm' AND commitment_id = ?",
                                         (nudge["commitment_id"],))
                self.assertGreater(confirm, before[-1], f"nudge {nudge} after STOP {before[-1]}")

    def test_done_and_the_24_hour_close_racing_exactly_one_wins(self):
        for n in range(30):
            cid = self.nudge_now()
            self.clock.advance(hours=24)                      # exactly the no-reply boundary
            result = []
            run_together(self, lambda: result.append(self.app.tap(ANA, cid, "done")),
                         after((n % 6) * 0.0002, self.app.tick))
            outcomes = self.outcome(cid)
            self.assertEqual(len(outcomes), 1)
            self.assertIn(outcomes[0], ("done", "no_reply"))
            acks = [r.kind for r in result[0]]
            if outcomes[0] == "done":
                self.assertEqual(acks[:1], ["done_ack"])
                self.assertEqual(self.status(cid), "done")
            else:
                self.assertEqual(acks, [])                     # the server answers 409
                self.assertEqual(self.status(cid), "no_reply")
            self.clock.advance(minutes=5)
        check_invariants(self, self.db)

    def test_two_done_taps_and_a_typed_done_give_one_done_ack(self):
        for _ in range(25):
            cid = self.nudge_now()
            results = []
            run_together(self, lambda: results.append(self.app.tap(ANA, cid, "done")),
                         lambda: results.append(self.app.tap(ANA, cid, "done")),
                         lambda: results.append(self.app.handle(ANA, "done")))
            kinds = [r.kind for replies in results for r in replies]
            self.assertEqual(kinds.count("done_ack"), 1, kinds)
            self.assertEqual(self.outcome(cid), ["done"])
            self.clock.advance(minutes=2)
        check_invariants(self, self.db)

    def test_two_later_taps_add_one_to_later_count(self):
        for _ in range(20):
            cid = self.nudge_now()
            results = []
            run_together(self, lambda: results.append(self.app.tap(ANA, cid, "later")),
                         lambda: results.append(self.app.tap(ANA, cid, "later")),
                         lambda: results.append(self.app.handle(ANA, "later")))
            kinds = [r.kind for replies in results for r in replies]
            self.assertEqual(kinds.count("later_ack"), 1, kinds)
            self.assertEqual(self.db.scalar("SELECT later_count FROM commitments WHERE id = ?", (cid,)), 1)
            self.assertEqual(self.status(cid), "scheduled")
            self.clock.advance(minutes=1)
            self.assertEqual(self.app.tick(), 1)               # exactly one more nudge
            self.assertEqual(self.app.tick(), 0)
            self.assertTrue(self.app.tap(ANA, cid, "done"))
            self.clock.advance(minutes=2)
        check_invariants(self, self.db)

    def test_stop_racing_a_tick_then_no_new_nudge(self):
        for n in range(25):
            user = f"web:stop{n}"
            cid = new_commitment(self.app, user)
            self.clock.advance(minutes=1)                       # due now
            run_together(self, lambda: self.app.handle(user, "stop"), after((n % 6) * 0.0003, self.app.tick))
            stopped = bot_messages(self.db, user, "stopped")
            self.assertEqual(len(stopped), 1)
            for step in (dict(seconds=1), dict(minutes=5), dict(hours=25)):
                self.clock.advance(**step)
                self.app.tick()
            nudges = bot_messages(self.db, user, "nudge")
            self.assertLessEqual(len(nudges), 1)
            self.assertTrue(all(m < stopped[0] for m in nudges), (nudges, stopped))
            self.assertEqual(self.status(cid), "stopped")
            self.assertIn(self.outcome(cid), ([], ["stop"]))
            self.assertEqual(bot_messages(self.db, user, "learning_question"), [])
        check_invariants(self, self.db)

    def test_no_learning_question_is_written_after_stop(self):
        # The 24 h close may ask the learning question. If STOP lands between the scheduler's "has this person
        # said STOP?" check and storing the question, the question is written to someone who said STOP.
        cid = self.nudge_now()
        self.clock.advance(hours=24)
        question = Reply(kind="learning_question", text="Shall I nudge you at 19:00 from now on?", lang="en")
        stopper = []

        def consider_while_stop_arrives(db, user_id, now):
            stopper.append(threading.Thread(target=self.app.handle, args=(ANA, "stop")))
            stopper[0].start()
            stopper[0].join(0.3)        # let STOP through if nothing holds it back
            return question

        with mock.patch("wazy.scheduler.learning.consider", side_effect=consider_while_stop_arrives):
            self.app.tick()
        stopper[0].join(5)
        self.assertEqual(self.outcome(cid), ["no_reply"])
        stopped = bot_messages(self.db, ANA, "stopped")
        asked = bot_messages(self.db, ANA, "learning_question")
        self.assertEqual(len(stopped), 1)
        self.assertTrue(all(q < stopped[0] for q in asked), (asked, stopped))

    def test_a_tap_that_loses_to_the_24_hour_close_stores_nothing(self):
        # tap() sees the nudge still open, then the scheduler closes it as no_reply (the 24 h boundary) before
        # tap() claims it. tap() returns [] (the server answers 409 not_open) and must not leave a "Done" from
        # the person in the chat with no answer: the tap is stored only once the claim has succeeded.
        cid = self.nudge_now()
        self.clock.advance(hours=24)
        conversation = self.app.conversation
        real_open_nudge = conversation._open_nudge

        def open_nudge_then_tick(*args, **kwargs):
            nudge = real_open_nudge(*args, **kwargs)
            self.app.tick()                                     # the scheduler thread gets in here
            return nudge

        with mock.patch.object(conversation, "_open_nudge", side_effect=open_nudge_then_tick):
            self.assertEqual(self.app.tap(ANA, cid, "done"), [])
        self.assertEqual(self.outcome(cid), ["no_reply"])
        taps = self.db.scalar("SELECT COUNT(*) FROM messages WHERE role = 'user' AND text = 'Done'")
        self.assertEqual(taps, 0)

    def test_a_tap_that_wins_stores_the_tap_before_the_answer(self):
        cid = self.nudge_now()
        replies = self.app.tap(ANA, cid, "done")
        self.assertEqual([r.kind for r in replies], ["done_ack"])
        last_two = self.db.query("SELECT role, text, kind FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 2",
                                 (ANA,))
        self.assertEqual([(r["role"], r["kind"]) for r in reversed(last_two)], [("user", "text"), ("bot", "done_ack")])
        self.assertEqual(last_two[1]["text"], "Done")


# =============================================================================================
# WhatsApp: the outbox, and STOP over WhatsApp
# =============================================================================================
class WhatsAppRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(T0)
        self.twilio = FakeTwilio()
        self.wa = WhatsApp(account_sid=ACCOUNT, auth_token=TOKEN, from_number=FROM, public_url=PUBLIC,
                           opener=self.twilio)
        self.app = make_app(self.clock, later_minutes=1, on_message=self.wa.on_message)
        self.wa.attach(self.app)
        self.addCleanup(self.app.close)
        self.addCleanup(self.wa.stop)
        self.db = self.app.db

    def drain(self) -> None:
        while self.wa.deliver_due():
            pass

    def nudge_texts(self, user_id: str) -> list:
        return [r["text"] for r in self.db.query("SELECT text FROM messages WHERE user_id = ? AND kind = 'nudge'",
                                                 (user_id,))]

    def posted_after(self, mark: int, texts: list) -> list:
        return [f.get("Body", "") for f in self.twilio.forms[mark:] if any(f.get("Body", "").startswith(t)
                                                                           for t in texts)]

    def test_every_outbox_row_is_sent_at_most_once(self):
        phones = [f"whatsapp:+3460000000{n}" for n in range(4)]
        stop = threading.Event()

        def person(user_id: str):
            def run():
                for text in ("Call mum in 1 minute", "Send the slides in 2 minutes", "list", "done", "later",
                             "Pay the rent in 1 minute", "done"):
                    self.app.handle(user_id, text, channel="whatsapp", tz="Europe/Madrid")
            return run

        def ticker():
            for _ in range(40):
                self.clock.advance(seconds=15)
                self.app.tick()
            stop.set()

        def sender():
            while not stop.is_set():
                self.wa.deliver_due()

        def duplicate_hooks():                                  # on_message twice for the same messages
            for _ in range(20):
                for row in self.db.query("SELECT id, user_id FROM messages WHERE role = 'bot'"):
                    self.wa.on_message(row["user_id"], row["id"], Reply("confirm", "", "en"))

        run_together(self, *[person(p) for p in phones], ticker, sender, sender, duplicate_hooks)
        self.drain()
        rows = self.db.query("SELECT o.*, m.kind, c.status AS commitment_status FROM outbox o "
                             "JOIN messages m ON m.id = o.message_id "
                             "LEFT JOIN commitments c ON c.id = m.commitment_id")
        bot = self.db.scalar("SELECT COUNT(*) FROM messages WHERE role = 'bot' AND user_id LIKE 'whatsapp:%'")
        self.assertEqual(len(rows), bot)                        # one row per message
        self.assertEqual({r["attempts"] for r in rows}, {1})    # no row was ever claimed twice
        sent = [r for r in rows if r["status"] == "sent"]
        self.assertEqual(sorted(r["provider_sid"] for r in sent), sorted(self.twilio.sids))   # one POST each
        self.assertEqual(len(self.twilio.forms), len(sent))
        for row in rows:
            if row["status"] != "sent":                         # only a nudge answered before it went out
                self.assertEqual((row["kind"], row["status"]), ("nudge", "failed"), row)
                self.assertNotEqual(row["commitment_status"], "nudged", row)
        check_invariants(self, self.db)

    def test_stop_racing_a_tick_then_no_nudge_goes_out(self):
        for n in range(15):
            user = f"whatsapp:+3461000000{n:02d}"
            new_commitment(self.app, user, channel="whatsapp", tz="Europe/Madrid")
            self.drain()
            self.clock.advance(minutes=1)
            run_together(self, lambda: self.app.handle(user, "stop", channel="whatsapp"),
                         after((n % 5) * 0.0003, self.app.tick))
            mark = len(self.twilio.forms)                      # STOP has returned: nothing was sent since
            self.drain()
            self.assertEqual(self.posted_after(mark, self.nudge_texts(user)), [])
        check_invariants(self, self.db)

    def test_stop_between_the_claim_and_the_delivery_of_a_nudge(self):
        # Deterministic version of the race above: the tick has stored the nudge and is about to hand it to
        # WhatsApp when STOP is handled. The nudge must not go out after "stopped".
        new_commitment(self.app, PHONE, channel="whatsapp", tz="Europe/Madrid")
        self.drain()
        real = self.wa.on_message

        def stop_first(user_id, message_id, reply):
            if reply.kind == "nudge":
                self.app.handle(PHONE, "stop", channel="whatsapp")
            real(user_id, message_id, reply)

        self.app.scheduler.on_message = stop_first
        self.clock.advance(minutes=1)
        self.assertEqual(self.app.tick(), 1)
        mark = len(self.twilio.forms)
        self.drain()
        self.assertEqual(self.posted_after(mark, self.nudge_texts(PHONE)), [])
        self.assertEqual(len(self.twilio.forms) - mark, 1)       # just "stopped"

    def test_a_nudge_waiting_for_a_retry_is_not_sent_after_stop(self):
        new_commitment(self.app, PHONE, channel="whatsapp", tz="Europe/Madrid")
        self.drain()
        self.clock.advance(minutes=1)
        self.twilio.failures = [503]                           # Twilio is down for the nudge: retry in 30 s
        self.assertEqual(self.app.tick(), 1)
        self.drain()
        self.app.handle(PHONE, "stop", channel="whatsapp")
        mark = len(self.twilio.forms)
        self.clock.advance(minutes=2)
        self.drain()
        self.assertEqual(self.posted_after(mark, self.nudge_texts(PHONE)), [])
        nudge_row = self.db.one("SELECT o.status, o.last_error FROM outbox o JOIN messages m ON m.id = o.message_id "
                                "WHERE m.kind = 'nudge'")
        self.assertEqual(nudge_row["status"], "failed")

    def test_an_open_nudge_waiting_for_a_retry_is_still_sent(self):
        new_commitment(self.app, PHONE, channel="whatsapp", tz="Europe/Madrid")
        self.drain()
        self.clock.advance(minutes=1)
        self.twilio.failures = [503]
        self.app.tick()
        self.drain()
        mark = len(self.twilio.forms)
        self.clock.advance(seconds=30)
        self.drain()
        self.assertEqual(len(self.posted_after(mark, self.nudge_texts(PHONE))), 1)


# =============================================================================================
# Two Apps on one file (two processes overlapping during a quick restart)
# =============================================================================================
class FileDatabaseTests(TempFolder):
    def setUp(self) -> None:
        self.clock = Clock(T0)
        path = self.temp_path("wazy.db")
        self.first = make_app(self.clock, path, later_minutes=1)
        self.second = make_app(self.clock, path, later_minutes=1)
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        self.assertEqual(self.first.db.scalar("PRAGMA journal_mode"), "wal")

    def test_two_apps_ticking_and_tapping_never_double_claim(self):
        for n in range(12):
            new_commitment(self.first if n % 2 else self.second, ANA, "Call mum in 1 minute")
        apps = (self.first, self.second)

        def ticker(app):
            def run():
                for _ in range(25):
                    self.clock.advance(seconds=10)
                    app.tick()
            return run

        def tapper(app, action, seed):
            def run():
                rng = random.Random(seed)
                for _ in range(40):
                    app.tap(ANA, rng.randint(1, 12), action)
            return run

        run_together(self, *[ticker(a) for a in apps for _ in range(2)],
                     *[tapper(a, act, i) for i, (a, act) in enumerate([(a, act) for a in apps
                                                                        for act in ("done", "later")])])
        check_invariants(self, self.first.db)

    def test_double_done_and_double_later_across_two_apps(self):
        for action, ack in (("done", "done_ack"), ("later", "later_ack")):
            for _ in range(10):
                cid = new_commitment(self.first, ANA)
                self.clock.advance(minutes=1)
                run_together(self, self.first.tick, self.second.tick)
                results = []
                run_together(self, lambda: results.append(self.first.tap(ANA, cid, action)),
                             lambda: results.append(self.second.tap(ANA, cid, action)))
                kinds = [r.kind for replies in results for r in replies]
                self.assertEqual(kinds.count(ack), 1, kinds)
                self.clock.advance(minutes=2)
                run_together(self, self.first.tick, self.second.tick)
                self.first.tap(ANA, cid, "done")
        check_invariants(self, self.first.db)
        later = self.first.db.query("SELECT later_count FROM commitments WHERE later_count > 0")
        self.assertEqual([r["later_count"] for r in later], [1] * 10)


if __name__ == "__main__":
    unittest.main()
