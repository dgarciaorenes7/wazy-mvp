"""Tests for wazy/scheduler.py: a due nudge goes out once (even with two schedulers racing on 8 threads),
opted-out people are left alone, a late nudge says when it was due, unanswered nudges become no_reply after
exactly 24 hours, and the nudge after Later never lands in quiet hours.
Run: python3.9 -m unittest tests.test_scheduler -v"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from wazy import learning, replies
from wazy.db import Database, from_db, to_db
from wazy.scheduler import Scheduler
from wazy.types import Reply

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=MADRID)      # Tuesday 3 November 2026, 09:00 in Madrid
QUIET_STARTS, QUIET_ENDS = 23, 8                       # Wazy never chooses a time from 23:00 to 07:59


def madrid(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=MADRID)


def london(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=LONDON)


class SchedulerTest(unittest.TestCase):
    """A fresh in-memory database and a scheduler that records every message it hands over."""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.delivered = []
        self.scheduler = Scheduler(self.db, on_message=self.record)

    def record(self, user_id: str, message_id: int, reply: Reply) -> None:
        self.delivered.append((user_id, message_id, reply))

    # ----- building data -----
    def person(self, user_id: str = "web:ana", tz: str = "Europe/Madrid", lang: str = "en", **fields) -> dict:
        self.db.user(user_id, NOW, tz=tz)
        self.db.update_user(user_id, lang=lang, **fields)
        return self.user(user_id)

    def user(self, user_id: str = "web:ana") -> dict:
        return self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def commitment(self, when: datetime, user_id: str = "web:ana", title: str = "Send Marta the slides",
                   status: str = "scheduled", time_source: str = "stated") -> int:
        cursor = self.db.execute(
            "INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, time_source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, title, title, to_db(when), status, time_source, to_db(NOW)))
        return int(cursor.lastrowid)

    def tap(self, commitment_id: int, action: str, now: datetime) -> None:
        """What conversation.py does on Done / Later (kept tiny here so these tests stand alone)."""
        outcome = self.db.one("SELECT * FROM nudge_outcomes WHERE commitment_id = ? AND outcome = 'pending'",
                              (commitment_id,))
        seconds = int((now - from_db(outcome["nudged_at"])).total_seconds())
        self.db.execute("UPDATE nudge_outcomes SET outcome = ?, responded_at = ?, seconds_to_respond = ? "
                        "WHERE id = ?", (action, to_db(now), seconds, outcome["id"]))
        if action == "done":
            self.db.execute("UPDATE commitments SET status = 'done' WHERE id = ?", (commitment_id,))
        else:
            user_id = self.db.scalar("SELECT user_id FROM commitments WHERE id = ?", (commitment_id,))
            when = self.scheduler.later_time(self.user(user_id), now)
            self.db.execute("UPDATE commitments SET status = 'scheduled', nudge_at = ?, later_count = later_count + 1 "
                            "WHERE id = ?", (to_db(when), commitment_id))

    # ----- reading data -----
    def status(self, commitment_id: int) -> str:
        return self.db.scalar("SELECT status FROM commitments WHERE id = ?", (commitment_id,))

    def outcomes(self, commitment_id: int) -> list:
        return self.db.query("SELECT * FROM nudge_outcomes WHERE commitment_id = ? ORDER BY id", (commitment_id,))

    def nudge_messages(self, commitment_id: int = None) -> list:
        if commitment_id is None:
            return self.db.query("SELECT * FROM messages WHERE kind = 'nudge' ORDER BY id")
        return self.db.query("SELECT * FROM messages WHERE kind = 'nudge' AND commitment_id = ? ORDER BY id",
                             (commitment_id,))

    def spy_render(self) -> mock.MagicMock:
        """Watch the parameters the scheduler passes to the real replies.render."""
        patcher = mock.patch("wazy.scheduler.render", wraps=replies.render)
        self.addCleanup(patcher.stop)
        return patcher.start()


# =============================================================================================
# Due or not due
# =============================================================================================
class DueTest(SchedulerTest):
    def setUp(self) -> None:
        super().setUp()
        self.person()

    def test_one_second_early_is_not_due(self):
        cid = self.commitment(NOW)
        self.assertEqual(self.scheduler.run_due(NOW - timedelta(seconds=1)), 0)
        self.assertEqual(self.status(cid), "scheduled")
        self.assertEqual(self.outcomes(cid), [])
        self.assertEqual(self.delivered, [])

    def test_exactly_on_time_is_due(self):
        cid = self.commitment(NOW)
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(self.status(cid), "nudged")

    def test_overdue_is_due(self):
        cid = self.commitment(NOW - timedelta(hours=3))
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(self.status(cid), "nudged")

    def test_pending_outcome_records_due_time_and_send_time(self):
        cid = self.commitment(NOW)
        sent_at = NOW + timedelta(minutes=2)
        self.scheduler.run_due(sent_at)
        [outcome] = self.outcomes(cid)
        self.assertEqual(outcome["outcome"], "pending")
        self.assertEqual(outcome["user_id"], "web:ana")
        self.assertEqual(outcome["scheduled_for"], to_db(NOW))
        self.assertEqual(outcome["nudged_at"], to_db(sent_at))
        self.assertIsNone(outcome["responded_at"])

    def test_nudge_message_is_stored_as_a_bot_nudge(self):
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        [message] = self.nudge_messages(cid)
        self.assertEqual(message["role"], "bot")
        self.assertEqual(message["user_id"], "web:ana")
        self.assertEqual(message["lang"], "en")
        self.assertEqual(message["created_at"], to_db(NOW))
        self.assertIn("Send Marta the slides", message["text"])

    def test_on_message_gets_the_stored_message_and_a_nudge_reply_with_buttons(self):
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        [(user_id, message_id, reply)] = self.delivered
        [message] = self.nudge_messages(cid)
        self.assertEqual(user_id, "web:ana")
        self.assertEqual(message_id, message["id"])
        self.assertEqual(reply, Reply(kind="nudge", text=message["text"], lang="en", commitment_id=cid, buttons=True))

    def test_only_due_commitments_are_sent(self):
        early = self.commitment(NOW - timedelta(minutes=1))
        later = self.commitment(NOW + timedelta(minutes=1))
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(self.status(early), "nudged")
        self.assertEqual(self.status(later), "scheduled")

    def test_several_due_are_sent_oldest_first(self):
        second = self.commitment(NOW - timedelta(minutes=5), title="Second")
        first = self.commitment(NOW - timedelta(minutes=10), title="First")
        self.assertEqual(self.scheduler.run_due(NOW), 2)
        self.assertEqual([reply.commitment_id for _, _, reply in self.delivered], [first, second])

    def test_other_statuses_are_never_nudged(self):
        ids = [self.commitment(NOW - timedelta(hours=1), status=s)
               for s in ("nudged", "done", "stopped", "no_reply", "cancelled")]
        self.assertEqual(self.scheduler.run_due(NOW), 0)
        for cid in ids:
            self.assertEqual(self.outcomes(cid), [])
        self.assertEqual(self.nudge_messages(), [])

    def test_now_in_another_zone_means_the_same_instant(self):
        cid = self.commitment(NOW)
        self.assertEqual(self.scheduler.run_due(NOW.astimezone(LONDON) - timedelta(seconds=1)), 0)
        self.assertEqual(self.scheduler.run_due(NOW.astimezone(UTC)), 1)
        self.assertEqual(self.status(cid), "nudged")

    def test_works_without_on_message(self):
        scheduler = Scheduler(self.db)
        cid = self.commitment(NOW)
        self.assertEqual(scheduler.run_due(NOW), 1)
        self.assertEqual(len(self.nudge_messages(cid)), 1)

    def test_nothing_due_returns_zero(self):
        self.assertEqual(self.scheduler.run_due(NOW), 0)


# =============================================================================================
# Never twice
# =============================================================================================
class NeverTwiceTest(SchedulerTest):
    def setUp(self) -> None:
        super().setUp()
        self.person()

    def assert_one_nudge_each(self, ids) -> None:
        for cid in ids:
            self.assertEqual(len(self.outcomes(cid)), 1, cid)
            self.assertEqual(len(self.nudge_messages(cid)), 1, cid)

    def test_running_again_sends_nothing(self):
        cid = self.commitment(NOW)
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(self.scheduler.run_due(NOW), 0)
        self.assertEqual(self.scheduler.run_due(NOW + timedelta(hours=1)), 0)
        self.assert_one_nudge_each([cid])
        self.assertEqual(len(self.delivered), 1)

    def test_two_schedulers_one_after_the_other(self):
        cid = self.commitment(NOW)
        other = Scheduler(self.db, on_message=self.record)
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(other.run_due(NOW), 0)
        self.assert_one_nudge_each([cid])

    def test_a_claim_lost_to_another_scheduler_sends_nothing(self):
        cid = self.commitment(NOW)
        slow, fast = Scheduler(self.db, on_message=self.record), Scheduler(self.db, on_message=self.record)
        stale_list = slow._due_ids(NOW.astimezone(UTC))      # slow read the due list...
        self.assertEqual(fast.run_due(NOW), 1)               # ...then fast claimed it
        with mock.patch.object(slow, "_due_ids", return_value=stale_list):
            self.assertEqual(slow.run_due(NOW), 0)
        self.assert_one_nudge_each([cid])
        self.assertEqual(len(self.delivered), 1)

    def test_a_cancel_between_reading_and_claiming_wins(self):
        cid = self.commitment(NOW)
        stale_list = self.scheduler._due_ids(NOW.astimezone(UTC))
        self.db.execute("UPDATE commitments SET status = 'cancelled' WHERE id = ?", (cid,))
        with mock.patch.object(self.scheduler, "_due_ids", return_value=stale_list):
            self.assertEqual(self.scheduler.run_due(NOW), 0)
        self.assertEqual(self.status(cid), "cancelled")
        self.assertEqual(self.outcomes(cid), [])

    def test_two_schedulers_racing_on_eight_threads(self):
        ids = [self.commitment(NOW - timedelta(seconds=i), title=f"Task {i}") for i in range(80)]
        guard = threading.Lock()
        delivered, totals, errors = [], [], []

        def on_message(user_id, message_id, reply):
            with guard:
                delivered.append(reply.commitment_id)

        schedulers = [Scheduler(self.db, on_message=on_message), Scheduler(self.db, on_message=on_message)]
        start = threading.Barrier(8)

        def worker(n: int) -> None:
            try:
                start.wait()
                sent = sum(schedulers[n % 2].run_due(NOW) for _ in range(5))
                with guard:
                    totals.append(sent)
            except Exception as error:      # pragma: no cover - shown by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sum(totals), 80)
        self.assertEqual(sorted(delivered), sorted(ids))
        self.assert_one_nudge_each(ids)

    def test_two_connections_to_one_file_racing_on_eight_threads(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "race.db")
        first, second = Database(path), Database(path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.user("web:ana", NOW)
        for i in range(40):
            first.execute("INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, created_at) "
                          "VALUES ('web:ana', 't', 't', ?, 'scheduled', ?)", (to_db(NOW), to_db(NOW)))
        schedulers = [Scheduler(first), Scheduler(second)]
        guard, totals, errors = threading.Lock(), [], []
        start = threading.Barrier(8)

        def worker(n: int) -> None:
            try:
                start.wait()
                sent = sum(schedulers[n % 2].run_due(NOW) for _ in range(3))
                with guard:
                    totals.append(sent)
            except Exception as error:      # pragma: no cover - shown by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sum(totals), 40)
        self.assertEqual(first.scalar("SELECT COUNT(*) FROM nudge_outcomes"), 40)
        self.assertEqual(first.scalar("SELECT COUNT(DISTINCT commitment_id) FROM nudge_outcomes"), 40)
        self.assertEqual(first.scalar("SELECT COUNT(*) FROM messages WHERE kind = 'nudge'"), 40)

    def test_later_schedules_exactly_one_more_nudge(self):
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        self.tap(cid, "later", NOW + timedelta(minutes=1))
        next_time = from_db(self.db.scalar("SELECT nudge_at FROM commitments WHERE id = ?", (cid,)))
        self.assertEqual(next_time, madrid(2026, 11, 4, 10, 0))
        self.assertEqual(self.scheduler.run_due(next_time - timedelta(seconds=1)), 0)
        self.assertEqual(self.scheduler.run_due(next_time), 1)
        self.assertEqual(self.scheduler.run_due(next_time + timedelta(minutes=5)), 0)
        self.assertEqual([o["outcome"] for o in self.outcomes(cid)], ["later", "pending"])
        self.assertEqual(len(self.nudge_messages(cid)), 2)

    def test_done_is_never_nudged_again(self):
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        self.tap(cid, "done", NOW + timedelta(minutes=1))
        self.assertEqual(self.scheduler.run_due(NOW + timedelta(days=3)), 0)
        self.assertEqual(self.status(cid), "done")

    def test_a_failed_render_leaves_the_commitment_scheduled(self):
        cid = self.commitment(NOW)
        with mock.patch("wazy.scheduler.render", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                self.scheduler.run_due(NOW)
        self.assertEqual(self.status(cid), "scheduled")       # the claim was rolled back, nothing lost
        self.assertEqual(self.outcomes(cid), [])
        self.assertEqual(self.nudge_messages(cid), [])
        self.assertEqual(self.scheduler.run_due(NOW), 1)      # and it goes out on the next run

    def test_a_failing_delivery_does_not_stop_other_nudges(self):
        first = self.commitment(NOW - timedelta(minutes=1), title="First")
        second = self.commitment(NOW, title="Second")
        calls = []

        def broken(user_id, message_id, reply):
            calls.append(reply.commitment_id)
            raise RuntimeError("network down")

        scheduler = Scheduler(self.db, on_message=broken)
        with self.assertLogs("wazy.scheduler", level="WARNING") as logs:
            self.assertEqual(scheduler.run_due(NOW), 2)
        self.assertEqual(calls, [first, second])
        self.assertEqual(len(self.nudge_messages()), 2)        # both stay in the chat
        self.assertNotIn("First", "\n".join(logs.output))      # message text is never logged
        self.assertNotIn("network down", "\n".join(logs.output))   # nor the error's own message


# =============================================================================================
# Opted-out people and several people
# =============================================================================================
class PeopleTest(SchedulerTest):
    def test_opted_out_person_is_left_alone(self):
        self.person("web:ana", opted_out=1)
        cid = self.commitment(NOW - timedelta(hours=1))
        self.assertEqual(self.scheduler.run_due(NOW), 0)
        self.assertEqual(self.status(cid), "scheduled")
        self.assertEqual(self.outcomes(cid), [])
        self.assertEqual(self.nudge_messages(), [])
        self.assertEqual(self.delivered, [])

    def test_opted_out_person_does_not_block_others(self):
        self.person("web:ana", opted_out=1)
        self.person("web:ben")
        ana = self.commitment(NOW, user_id="web:ana")
        ben = self.commitment(NOW, user_id="web:ben")
        self.assertEqual(self.scheduler.run_due(NOW), 1)
        self.assertEqual(self.status(ana), "scheduled")
        self.assertEqual(self.status(ben), "nudged")
        self.assertEqual(self.delivered[0][0], "web:ben")

    def test_opting_back_in_sends_what_is_due(self):
        self.person("web:ana", opted_out=1)
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        self.db.update_user("web:ana", opted_out=0)
        self.assertEqual(self.scheduler.run_due(NOW + timedelta(minutes=1)), 1)
        self.assertEqual(self.status(cid), "nudged")

    def test_each_nudge_goes_to_its_own_person_in_their_language(self):
        self.person("web:ana", lang="en")
        self.person("whatsapp:+34600000000", lang="es")
        ana = self.commitment(NOW, user_id="web:ana", title="Call mum")
        pepe = self.commitment(NOW, user_id="whatsapp:+34600000000", title="Llamar a mamá")
        spy = self.spy_render()
        self.assertEqual(self.scheduler.run_due(NOW), 2)
        by_id = {reply.commitment_id: (user_id, reply) for user_id, _, reply in self.delivered}
        self.assertEqual(by_id[ana][0], "web:ana")
        self.assertEqual(by_id[ana][1].lang, "en")
        self.assertEqual(by_id[pepe][0], "whatsapp:+34600000000")
        self.assertEqual(by_id[pepe][1].lang, "es")
        self.assertIn("Llamar a mamá", by_id[pepe][1].text)
        self.assertEqual({c.args for c in spy.call_args_list}, {("nudge", "en"), ("nudge", "es")})
        spanish = self.nudge_messages(pepe)[0]
        self.assertEqual((spanish["user_id"], spanish["lang"]), ("whatsapp:+34600000000", "es"))


# =============================================================================================
# A late nudge says when it was due
# =============================================================================================
class LateTest(SchedulerTest):
    def due_text_when_sent(self, late_by: timedelta, tz: str = "Europe/Madrid", due: datetime = NOW) -> str:
        self.person(tz=tz)
        self.commitment(due)
        spy = self.spy_render()
        self.assertEqual(self.scheduler.run_due(due + late_by), 1)
        kind, lang = spy.call_args.args
        self.assertEqual((kind, lang), ("nudge", "en"))
        self.assertEqual(spy.call_args.kwargs["title"], "Send Marta the slides")
        return spy.call_args.kwargs["due_text"]

    def test_on_time_says_nothing_extra(self):
        self.assertEqual(self.due_text_when_sent(timedelta(0)), "")

    def test_exactly_ten_minutes_late_is_still_on_time(self):
        self.assertEqual(self.due_text_when_sent(timedelta(minutes=10)), "")

    def test_more_than_ten_minutes_late_says_the_due_time(self):
        self.assertEqual(self.due_text_when_sent(timedelta(minutes=10, seconds=1)), "09:00")

    def test_hours_late_says_the_local_due_time(self):
        self.assertEqual(self.due_text_when_sent(timedelta(hours=5)), "09:00")

    def test_due_time_is_on_the_persons_own_clock(self):
        due = london(2026, 11, 3, 18, 30)
        self.assertEqual(self.due_text_when_sent(timedelta(minutes=45), tz="Europe/London", due=due), "18:30")

    def test_late_nudge_text_contains_the_due_time(self):
        self.person()
        cid = self.commitment(madrid(2026, 11, 3, 7, 45))
        self.scheduler.run_due(NOW)
        self.assertIn("07:45", self.nudge_messages(cid)[0]["text"])

    def test_on_time_nudge_text_does_not_mention_a_due_time(self):
        self.person()
        cid = self.commitment(NOW)
        self.scheduler.run_due(NOW)
        self.assertNotIn("09:00", self.nudge_messages(cid)[0]["text"])


# =============================================================================================
# No reply after 24 hours, and the learning question it may lead to
# =============================================================================================
class NoReplyTest(SchedulerTest):
    def setUp(self) -> None:
        super().setUp()
        self.person()

    def sent(self, when: datetime = NOW, **fields) -> int:
        cid = self.commitment(when, **fields)
        self.scheduler.run_due(when)
        return cid

    def history(self, due: datetime, outcome: str, tapped: datetime = None, source: str = "default") -> None:
        """One old, answered nudge (written directly, as if from earlier weeks)."""
        cid = self.commitment(due, status="done" if outcome == "done" else "no_reply", time_source=source)
        self.db.execute("INSERT INTO nudge_outcomes (commitment_id, user_id, scheduled_for, nudged_at, outcome, "
                        "responded_at) VALUES (?, 'web:ana', ?, ?, ?, ?)",
                        (cid, to_db(due), to_db(due), outcome, to_db(tapped) if tapped else None))

    def test_one_second_before_24_hours_is_still_pending(self):
        cid = self.sent()
        self.scheduler.run_due(NOW + timedelta(hours=24) - timedelta(seconds=1))
        self.assertEqual(self.outcomes(cid)[0]["outcome"], "pending")
        self.assertEqual(self.status(cid), "nudged")

    def test_exactly_24_hours_becomes_no_reply(self):
        cid = self.sent()
        self.scheduler.run_due(NOW + timedelta(hours=24))
        [outcome] = self.outcomes(cid)
        self.assertEqual(outcome["outcome"], "no_reply")
        self.assertIsNone(outcome["responded_at"])
        self.assertEqual(self.status(cid), "no_reply")

    def test_24_hours_counts_real_hours_across_the_clock_change(self):
        cid = self.sent(madrid(2026, 10, 24, 12, 0))           # 10:00 UTC; that night Madrid's clocks go back
        self.scheduler.run_due(madrid(2026, 10, 25, 10, 59, 59))
        self.assertEqual(self.outcomes(cid)[0]["outcome"], "pending")
        self.scheduler.run_due(madrid(2026, 10, 25, 11, 0))    # 24 real hours later (23 on the wall clock)
        self.assertEqual(self.outcomes(cid)[0]["outcome"], "no_reply")

    def test_answered_nudges_are_not_touched(self):
        cid = self.sent()
        self.tap(cid, "done", NOW + timedelta(minutes=3))
        self.scheduler.run_due(NOW + timedelta(days=2))
        self.assertEqual(self.outcomes(cid)[0]["outcome"], "done")
        self.assertEqual(self.status(cid), "done")

    def test_no_reply_does_not_count_as_a_nudge_sent(self):
        self.sent()
        self.assertEqual(self.scheduler.run_due(NOW + timedelta(days=1)), 0)

    def test_no_reply_and_a_new_nudge_in_the_same_run(self):
        old = self.sent()
        new = self.commitment(NOW + timedelta(days=1))
        self.assertEqual(self.scheduler.run_due(NOW + timedelta(days=1)), 1)
        self.assertEqual(self.status(old), "no_reply")
        self.assertEqual(self.status(new), "nudged")

    def test_without_enough_evidence_no_question_is_asked(self):
        self.sent()
        self.scheduler.run_due(NOW + timedelta(days=1))
        self.assertEqual(self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'"), [])
        self.assertEqual(len(self.delivered), 1)              # only the nudge

    def build_evidence(self) -> None:
        """Three Done taps around 19:00, and two 10:00 default nudges answered Later."""
        for day in (20, 21, 22):
            self.history(madrid(2026, 10, day, 19, 0), "done", madrid(2026, 10, day, 19, 5), source="stated")
        for day in (26, 27):
            self.history(madrid(2026, 10, day, 10, 0), "later", madrid(2026, 10, day, 10, 2))

    def test_a_third_miss_by_no_reply_asks_the_learning_question(self):
        self.build_evidence()
        cid = self.sent(madrid(2026, 11, 3, 10, 0), time_source="default")
        when = madrid(2026, 11, 4, 10, 0)
        self.assertEqual(self.scheduler.run_due(when), 0)
        self.assertEqual(self.status(cid), "no_reply")
        [question] = self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'")
        self.assertEqual((question["role"], question["user_id"], question["lang"]), ("bot", "web:ana", "en"))
        self.assertIsNone(question["commitment_id"])
        self.assertEqual(question["created_at"], to_db(when))
        user_id, message_id, reply = self.delivered[-1]
        self.assertEqual((user_id, message_id), ("web:ana", question["id"]))
        self.assertEqual(reply, Reply(kind="learning_question", text=question["text"], lang="en"))
        self.assertEqual(json.loads(self.user()["learning_pending"]), {"hour": 19})

    def test_the_question_is_asked_once_even_with_two_misses_closing_together(self):
        self.build_evidence()
        self.sent(madrid(2026, 11, 3, 10, 0), time_source="default")
        self.sent(madrid(2026, 11, 3, 10, 0), time_source="default", title="Another")
        self.scheduler.run_due(madrid(2026, 11, 5, 10, 0))
        self.assertEqual(len(self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'")), 1)
        self.scheduler.run_due(madrid(2026, 11, 6, 10, 0))
        self.assertEqual(len(self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'")), 1)

    def test_no_question_for_someone_who_said_stop(self):
        self.build_evidence()
        cid = self.sent(madrid(2026, 11, 3, 10, 0), time_source="default")
        self.db.update_user("web:ana", opted_out=1)            # (STOP normally closes outcomes; be safe anyway)
        self.scheduler.run_due(madrid(2026, 11, 4, 10, 0))
        self.assertEqual(self.status(cid), "no_reply")
        self.assertEqual(self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'"), [])
        self.assertIsNone(self.user()["learning_pending"])

    def test_no_question_while_learning_is_off(self):
        self.build_evidence()
        self.db.update_user("web:ana", learning_off=1)
        self.sent(madrid(2026, 11, 3, 10, 0), time_source="default")
        self.scheduler.run_due(madrid(2026, 11, 4, 10, 0))
        self.assertEqual(self.db.query("SELECT * FROM messages WHERE kind = 'learning_question'"), [])


# =============================================================================================
# The time of the nudge after Later
# =============================================================================================
class LaterTimeTest(SchedulerTest):
    def later(self, tapped: datetime, learned=None, tz: str = "Europe/Madrid", minutes=None) -> datetime:
        user = self.person(tz=tz, learned_hour=learned)
        return Scheduler(self.db, later_minutes=minutes).later_time(user, tapped)

    def test_table(self):
        # (tapped, learned hour, expected) all on Madrid clocks
        table = [
            (madrid(2026, 11, 3, 15, 0), None, madrid(2026, 11, 4, 10, 0)),     # afternoon: next morning
            (madrid(2026, 11, 3, 9, 30), None, madrid(2026, 11, 4, 10, 0)),     # 10:00 ahead today, but default
            (madrid(2026, 11, 3, 8, 0), None, madrid(2026, 11, 4, 10, 0)),      #   means next morning
            (madrid(2026, 11, 3, 23, 59), None, madrid(2026, 11, 4, 10, 0)),
            (madrid(2026, 11, 3, 0, 0), None, madrid(2026, 11, 3, 10, 0)),      # small hours: this morning
            (madrid(2026, 11, 3, 4, 59), None, madrid(2026, 11, 3, 10, 0)),
            (madrid(2026, 11, 3, 5, 0), None, madrid(2026, 11, 4, 10, 0)),      # 05:00 on: next morning
            (madrid(2026, 11, 3, 18, 0), 19, madrid(2026, 11, 3, 19, 0)),       # learned hour still ahead: today
            (madrid(2026, 11, 3, 18, 59), 19, madrid(2026, 11, 3, 19, 0)),
            (madrid(2026, 11, 3, 19, 0), 19, madrid(2026, 11, 4, 19, 0)),       # learned hour reached: tomorrow
            (madrid(2026, 11, 3, 21, 0), 19, madrid(2026, 11, 4, 19, 0)),
            (madrid(2026, 11, 3, 3, 0), 19, madrid(2026, 11, 3, 19, 0)),
            (madrid(2026, 11, 3, 3, 0), 8, madrid(2026, 11, 3, 8, 0)),
            (madrid(2026, 11, 3, 7, 59), 8, madrid(2026, 11, 3, 8, 0)),
            (madrid(2026, 11, 3, 8, 0), 8, madrid(2026, 11, 4, 8, 0)),
            (madrid(2026, 11, 3, 9, 0), 10, madrid(2026, 11, 3, 10, 0)),        # a learned 10:00 is a learned hour
            (madrid(2026, 11, 3, 12, 0), 21, madrid(2026, 11, 3, 21, 0)),
            (madrid(2026, 11, 30, 22, 0), None, madrid(2026, 12, 1, 10, 0)),    # month end
            (madrid(2026, 12, 31, 20, 0), None, madrid(2027, 1, 1, 10, 0)),     # year end
            (madrid(2028, 2, 28, 20, 0), None, madrid(2028, 2, 29, 10, 0)),     # leap day
        ]
        for tapped, learned, expected in table:
            with self.subTest(tapped=tapped.isoformat(), learned=learned):
                self.assertEqual(self.later(tapped, learned), expected)

    def test_result_is_aware_and_on_the_persons_clock(self):
        when = self.later(madrid(2026, 11, 3, 15, 0))
        self.assertIs(when.tzinfo, MADRID)
        self.assertEqual(when.utcoffset(), timedelta(hours=1))

    def test_london_person_gets_london_mornings(self):
        tapped = madrid(2026, 11, 3, 10, 30)                   # 09:30 in London
        when = self.later(tapped, tz="Europe/London")
        self.assertEqual(when, london(2026, 11, 4, 10, 0))
        self.assertEqual(str(when.tzinfo), "Europe/London")
        self.assertEqual(when.astimezone(UTC), datetime(2026, 11, 4, 10, 0, tzinfo=UTC))

    def test_london_small_hours_are_judged_on_the_london_clock(self):
        tapped = madrid(2026, 11, 3, 5, 30)                    # 04:30 in London: still "this morning" there
        self.assertEqual(self.later(tapped, tz="Europe/London"), london(2026, 11, 3, 10, 0))

    def test_london_learned_hour_ahead_today(self):
        tapped = madrid(2026, 11, 3, 19, 30)                   # 18:30 in London
        self.assertEqual(self.later(tapped, learned=19, tz="Europe/London"), london(2026, 11, 3, 19, 0))

    def test_clocks_go_back_on_25_october(self):
        when = self.later(madrid(2026, 10, 24, 21, 0))
        self.assertEqual(when, madrid(2026, 10, 25, 10, 0))
        self.assertEqual(when.astimezone(UTC), datetime(2026, 10, 25, 9, 0, tzinfo=UTC))

    def test_tapped_in_the_repeated_hour_of_25_october(self):
        tapped = datetime(2026, 10, 25, 2, 30, tzinfo=MADRID, fold=1)    # the second 02:30
        self.assertEqual(self.later(tapped).astimezone(UTC), datetime(2026, 10, 25, 9, 0, tzinfo=UTC))

    def test_clocks_go_forward_on_28_march(self):
        when = self.later(madrid(2027, 3, 27, 21, 0))
        self.assertEqual(when.astimezone(UTC), datetime(2027, 3, 28, 8, 0, tzinfo=UTC))
        self.assertEqual(when.utcoffset(), timedelta(hours=2))

    def test_tapped_just_after_the_jump_on_28_march(self):
        tapped = datetime(2027, 3, 28, 1, 0, tzinfo=UTC)       # 03:00 in Madrid, minutes after the jump
        self.assertEqual(self.later(tapped).astimezone(UTC), datetime(2027, 3, 28, 8, 0, tzinfo=UTC))

    def test_later_minutes_replaces_the_rule(self):
        tapped = madrid(2026, 11, 3, 15, 0)
        self.assertEqual(self.later(tapped, minutes=2), madrid(2026, 11, 3, 15, 2))
        self.assertEqual(self.later(tapped, learned=19, minutes=90), madrid(2026, 11, 3, 16, 30))

    def test_later_minutes_is_on_the_persons_clock(self):
        when = self.later(madrid(2026, 11, 3, 15, 0), tz="Europe/London", minutes=5)
        self.assertEqual(str(when.tzinfo), "Europe/London")
        self.assertEqual(when, london(2026, 11, 3, 14, 5))

    def test_later_minutes_counts_real_minutes_across_the_clock_change(self):
        tapped = datetime(2026, 10, 25, 2, 30, tzinfo=MADRID)   # the first 02:30 (00:30 UTC)
        when = self.later(tapped, minutes=60)
        self.assertEqual(when.astimezone(UTC), datetime(2026, 10, 25, 1, 30, tzinfo=UTC))

    def test_a_damaged_learned_hour_is_ignored(self):
        for bad in (3, 23, 0):
            with self.subTest(learned=bad):
                self.assertEqual(self.later(madrid(2026, 11, 3, 15, 0), learned=bad), madrid(2026, 11, 4, 10, 0))

    def assert_never_quiet(self, day_start: datetime, zone: ZoneInfo, learned) -> None:
        """Every minute of one local day: the chosen time is ahead, on the hour, and not in quiet hours."""
        user = self.person(tz=str(zone), learned_hour=learned)
        moment = day_start.astimezone(UTC)
        day = day_start.date()
        checked = 0
        while moment.astimezone(zone).date() == day:
            when = self.scheduler.later_time(user, moment)
            local = when.astimezone(zone)
            ok = when > moment and QUIET_ENDS <= local.hour < QUIET_STARTS and local.minute == 0
            if not ok:
                self.fail(f"tapped {moment.astimezone(zone).isoformat()} learned={learned} -> {local.isoformat()}")
            moment += timedelta(minutes=1)
            checked += 1
        self.assertGreaterEqual(checked, 23 * 60)

    def test_quiet_hours_every_minute_of_an_ordinary_day(self):
        for learned in (None, 8, 9, 10, 12, 19, 21):
            with self.subTest(learned=learned):
                self.assert_never_quiet(madrid(2026, 11, 3, 0, 0), MADRID, learned)

    def test_quiet_hours_every_minute_of_both_dst_days(self):
        for start in (madrid(2026, 10, 25, 0, 0), madrid(2027, 3, 28, 0, 0)):
            for learned in (None, 8, 21):
                with self.subTest(day=start.date().isoformat(), learned=learned):
                    self.assert_never_quiet(start, MADRID, learned)

    def test_quiet_hours_every_minute_in_london(self):
        for learned in (None, 8, 21):
            with self.subTest(learned=learned):
                self.assert_never_quiet(london(2026, 11, 3, 0, 0), LONDON, learned)


# =============================================================================================
# The whole loop, as in v1: Done in the evening, Later at 10:00, then Wazy asks
# =============================================================================================
class LearningLoopTest(SchedulerTest):
    def setUp(self) -> None:
        super().setUp()
        self.person()

    def nudge_and_answer(self, due: datetime, action: str, after: timedelta, source: str) -> int:
        cid = self.commitment(due, time_source=source)
        self.assertEqual(self.scheduler.run_due(due), 1)
        tapped = due + after
        self.tap(cid, action, tapped)
        if action == "later":                                   # one more nudge, then Done this time
            self.db.execute("UPDATE commitments SET status = 'done' WHERE id = ?", (cid,))
        return cid

    def test_wazy_asks_after_three_late_mornings_and_three_done_evenings(self):
        for day in (2, 3, 4):
            self.nudge_and_answer(madrid(2026, 11, day, 19, 0), "done", timedelta(minutes=5), "stated")
        replies_seen = []
        for day in (5, 6, 7):
            self.nudge_and_answer(madrid(2026, 11, day, 10, 0), "later", timedelta(minutes=2), "default")
            replies_seen.append(learning.consider(self.db, "web:ana", madrid(2026, 11, day, 10, 2)))
        self.assertIsNone(replies_seen[0])
        self.assertIsNone(replies_seen[1])
        self.assertEqual(replies_seen[2].kind, "learning_question")
        self.assertEqual(json.loads(self.user()["learning_pending"]), {"hour": 19})
        self.assertIsNone(self.user()["learned_hour"])         # nothing changes before YES
        learning.answer(self.db, "web:ana", True, madrid(2026, 11, 7, 10, 5))
        self.assertEqual(self.user()["learned_hour"], 19)
        # Later now means the learned hour, today if it is still ahead
        self.assertEqual(self.scheduler.later_time(self.user(), madrid(2026, 11, 7, 10, 6)),
                         madrid(2026, 11, 7, 19, 0))


if __name__ == "__main__":
    unittest.main()


class BrokenZoneTest(unittest.TestCase):
    """A damaged database could hold a zone name that does not exist. The scheduler must not crash on it."""

    def test_nudge_and_later_time_fall_back_to_the_default_zone(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        now = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
        db.user("web:broken", now)
        db.update_user("web:broken", tz="Mars/Base")
        db.execute("INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, created_at) "
                   "VALUES ('web:broken', 'x', 'Call mum', ?, 'scheduled', ?)", (to_db(now), to_db(now)))
        scheduler = Scheduler(db)
        self.assertEqual(scheduler.run_due(now.replace(hour=10)), 1)          # an hour late: says when it was due
        later = scheduler.later_time(db.user("web:broken", now), now)
        self.assertEqual(later.utcoffset().total_seconds(), 3600)             # Europe/Madrid in November
