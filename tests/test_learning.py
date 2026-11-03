"""Tests for wazy/learning.py: the numbers behind the rule (evidence), every boundary of the rule, YES and NO,
forget and learn again, explain with the real numbers, and two people who never affect each other.
Run: python3.9 -m unittest tests.test_learning -v"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from wazy import replies
from wazy.db import Database, to_db
from wazy.learning import (ASK_AGAIN_AFTER, EARLIEST_HOUR, LAST_ANSWERED, LATEST_HOUR, MIN_DEFAULT_NUDGES,
                           MIN_DONE_AT_NEW_HOUR, answer, consider, evidence, explain, forget, resume, usual_hour)
from wazy.timeparse import DEFAULT_HOUR
from wazy.types import Reply

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=MADRID)     # Tuesday 3 November 2026, 10:30 in Madrid
ANA, BEN = "web:ana", "whatsapp:+34600000000"


def madrid(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=MADRID)


class LearningTest(unittest.TestCase):
    """A fresh in-memory database with one person, Ana, in Madrid, speaking English."""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.person(ANA)

    def person(self, user_id: str, tz: str = "Europe/Madrid", lang: str = "en", **fields) -> None:
        self.db.user(user_id, NOW, tz=tz)
        self.db.update_user(user_id, lang=lang, **fields)

    def user(self, user_id: str = ANA) -> dict:
        return self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def nudge(self, due: datetime, outcome: str, tapped: datetime = None, source: str = "default",
              user_id: str = ANA) -> None:
        """One past nudge: due at `due`, answered with `outcome`, tapped at `tapped` (None for no reply)."""
        status = {"done": "done", "later": "done", "no_reply": "no_reply", "pending": "nudged",
                  "stop": "stopped", "cancelled": "cancelled"}[outcome]
        cursor = self.db.execute(
            "INSERT INTO commitments (user_id, raw_text, title, nudge_at, status, time_source, created_at) "
            "VALUES (?, 't', 't', ?, ?, ?, ?)", (user_id, to_db(due), status, source, to_db(due)))
        self.db.execute(
            "INSERT INTO nudge_outcomes (commitment_id, user_id, scheduled_for, nudged_at, outcome, responded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cursor.lastrowid, user_id, to_db(due), to_db(due), outcome, to_db(tapped) if tapped else None))

    def dones(self, count: int = 3, hour: int = 19, user_id: str = ANA, tz: ZoneInfo = MADRID) -> None:
        """`count` stated nudges at `hour`, each ticked off 5 minutes later (October, before the misses)."""
        for day in range(1, count + 1):
            due = datetime(2026, 10, day, hour, 0, tzinfo=tz)
            self.nudge(due, "done", due + timedelta(minutes=5), source="stated", user_id=user_id)

    def mornings(self, count: int = 3, done: int = 0, hour: int = 10, user_id: str = ANA,
                 tz: ZoneInfo = MADRID, source: str = "default") -> None:
        """`count` nudges Wazy timed at `hour`; the first `done` got Done, the rest Later (late October)."""
        for i in range(count):
            due = datetime(2026, 10, 20 + i, hour, 0, tzinfo=tz)
            outcome = "done" if i < done else "later"
            self.nudge(due, outcome, due + timedelta(minutes=2), source=source, user_id=user_id)

    def log(self, user_id: str = ANA) -> list:
        return self.db.query("SELECT * FROM learning_log WHERE user_id = ? ORDER BY id DESC", (user_id,))

    def spy_render(self) -> mock.MagicMock:
        """Watch the parameters learning.py passes to the real replies.render."""
        patcher = mock.patch("wazy.learning.render", wraps=replies.render)
        self.addCleanup(patcher.stop)
        return patcher.start()


# =============================================================================================
# The usual hour
# =============================================================================================
class UsualHourTest(unittest.TestCase):
    def test_default_is_ten(self):
        self.assertEqual(DEFAULT_HOUR, 10)
        self.assertEqual(usual_hour({"learned_hour": None}), 10)

    def test_learned_hour_is_used(self):
        for hour in (EARLIEST_HOUR, 12, 19, LATEST_HOUR):
            with self.subTest(hour=hour):
                self.assertEqual(usual_hour({"learned_hour": hour}), hour)

    def test_a_learned_hour_in_quiet_hours_is_ignored(self):
        for hour in (0, 3, 7, 22, 23):
            with self.subTest(hour=hour):
                self.assertEqual(usual_hour({"learned_hour": hour}), 10)


# =============================================================================================
# evidence(): the numbers
# =============================================================================================
class EvidenceTest(LearningTest):
    def test_no_history(self):
        self.assertEqual(evidence(self.db, ANA), {
            "current_hour": 10, "answered": 0, "answered_at_current": 0, "done_at_current": 0, "misses": 0,
            "best_hour": None, "done_at_best": 0, "by_hour": {}})

    def test_typical_history(self):
        self.dones(3, hour=19)
        self.mornings(3, done=1)
        self.assertEqual(evidence(self.db, ANA), {
            "current_hour": 10, "answered": 6, "answered_at_current": 3, "done_at_current": 1, "misses": 2,
            "best_hour": 19, "done_at_best": 3,
            "by_hour": {10: {"answered": 3, "done": 1}, 19: {"answered": 3, "done": 3}}})

    def test_only_the_last_30_answered_nudges_count(self):
        self.mornings(5)                                        # the oldest five: Later at 10:00
        for i in range(LAST_ANSWERED):                          # then 30 newer stated nudges at 19:00
            due = madrid(2026, 11, 1, 19, 0) + timedelta(days=i)
            self.nudge(due, "done", due + timedelta(minutes=1), source="stated")
        e = evidence(self.db, ANA)
        self.assertEqual(e["answered"], 30)
        self.assertEqual(e["answered_at_current"], 0)
        self.assertEqual(e["done_at_best"], 30)

    def test_29_newer_nudges_leave_one_old_miss_in_view(self):
        self.mornings(5)
        for i in range(LAST_ANSWERED - 1):
            due = madrid(2026, 11, 1, 19, 0) + timedelta(days=i)
            self.nudge(due, "done", due + timedelta(minutes=1), source="stated")
        self.assertEqual(evidence(self.db, ANA)["answered_at_current"], 1)

    def test_stated_times_at_ten_do_not_count_at_the_usual_hour(self):
        self.mornings(3, source="stated")
        e = evidence(self.db, ANA)
        self.assertEqual(e["answered_at_current"], 0)
        self.assertEqual(e["by_hour"], {10: {"answered": 3, "done": 0}})

    def test_learned_times_count_at_the_usual_hour(self):
        self.db.update_user(ANA, learned_hour=19)
        self.mornings(3, hour=19, source="learned")
        e = evidence(self.db, ANA)
        self.assertEqual((e["current_hour"], e["answered_at_current"], e["misses"]), (19, 3, 3))

    def test_no_reply_counts_as_answered_and_as_a_miss(self):
        self.nudge(madrid(2026, 10, 20, 10, 0), "no_reply")
        e = evidence(self.db, ANA)
        self.assertEqual((e["answered"], e["answered_at_current"], e["misses"]), (1, 1, 1))

    def test_pending_stopped_and_cancelled_nudges_are_not_answered(self):
        for outcome in ("pending", "stop", "cancelled"):
            self.nudge(madrid(2026, 10, 20, 10, 0), outcome)
        self.assertEqual(evidence(self.db, ANA)["answered"], 0)

    def test_hour_of_a_nudge_is_its_due_hour_on_the_persons_clock(self):
        self.person(BEN, tz="Europe/London")
        self.mornings(3, user_id=BEN, tz=LONDON)                # 10:00 in London is 11:00 in Madrid
        e = evidence(self.db, BEN)
        self.assertEqual(e["answered_at_current"], 3)
        self.assertEqual(list(e["by_hour"]), [10])

    def test_hour_of_a_done_tap_is_when_it_was_tapped(self):
        for day in (1, 2, 3):                                   # due at 10:00, ticked off in the evening
            due = madrid(2026, 10, day, 10, 0)
            self.nudge(due, "done", madrid(2026, 10, day, 19, 40))
        e = evidence(self.db, ANA)
        self.assertEqual((e["done_at_current"], e["best_hour"], e["done_at_best"]), (3, 19, 3))

    def test_done_tap_hour_is_on_the_persons_clock(self):
        self.person(BEN, tz="Europe/London")
        self.dones(3, hour=19, user_id=BEN, tz=LONDON)          # 19:05 in London (20:05 in Madrid)
        self.assertEqual(evidence(self.db, BEN)["best_hour"], 19)

    def test_done_tap_hour_follows_summer_time(self):
        for day in (20, 21, 22):                                # 17:05 UTC is 19:05 in Madrid in summer
            due = datetime(2026, 6, day, 17, 0, tzinfo=UTC)
            self.nudge(due, "done", due + timedelta(minutes=5), source="stated")
        self.assertEqual(evidence(self.db, ANA)["best_hour"], 19)

    def test_a_done_without_a_tap_time_is_not_a_tap(self):
        for day in (1, 2, 3):
            self.nudge(madrid(2026, 10, day, 19, 0), "done", None, source="stated")
        e = evidence(self.db, ANA)
        self.assertEqual((e["best_hour"], e["done_at_best"]), (None, 0))
        self.assertEqual(e["by_hour"][19], {"answered": 3, "done": 3})

    def test_best_hour_is_never_the_usual_hour(self):
        self.mornings(5, done=5)
        self.assertIsNone(evidence(self.db, ANA)["best_hour"])

    def test_best_hour_is_never_outside_8_to_21(self):
        self.dones(5, hour=22)
        self.dones(5, hour=7)
        self.assertIsNone(evidence(self.db, ANA)["best_hour"])

    def test_best_hour_inside_the_window_wins_over_a_busier_one_outside(self):
        self.dones(5, hour=22)
        self.dones(3, hour=21)
        self.assertEqual(evidence(self.db, ANA)["best_hour"], 21)

    def test_best_hour_has_the_most_done_taps(self):
        self.dones(3, hour=19)
        self.dones(4, hour=14)
        e = evidence(self.db, ANA)
        self.assertEqual((e["best_hour"], e["done_at_best"]), (14, 4))

    def test_tie_goes_to_the_hour_closest_to_the_usual_one(self):
        self.dones(3, hour=19)
        self.dones(3, hour=8)
        self.assertEqual(evidence(self.db, ANA)["best_hour"], 8)

    def test_tie_at_the_same_distance_goes_to_the_earlier_hour(self):
        self.dones(3, hour=12)
        self.dones(3, hour=8)
        self.assertEqual(evidence(self.db, ANA)["best_hour"], 8)

    def test_current_hour_follows_the_learned_hour(self):
        self.db.update_user(ANA, learned_hour=19)
        self.mornings(3)                                       # default 10:00 nudges no longer count
        e = evidence(self.db, ANA)
        self.assertEqual((e["current_hour"], e["answered_at_current"]), (19, 0))

    def test_unknown_person(self):
        with self.assertRaises(KeyError):
            evidence(self.db, "web:nobody")


# =============================================================================================
# consider(): every boundary of the rule
# =============================================================================================
class ConsiderTest(LearningTest):
    def test_asks_with_the_numbers(self):
        self.dones(3, hour=19)
        self.mornings(3)
        spy = self.spy_render()
        reply = consider(self.db, ANA, NOW)
        self.assertEqual(reply.kind, "learning_question")
        self.assertEqual(reply.lang, "en")
        self.assertIsNone(reply.commitment_id)
        self.assertFalse(reply.buttons)
        self.assertTrue(reply.text)
        spy.assert_called_once_with("learning_question", "en", current_hour=10, misses=3, answered=6,
                                    best_hour=19, done_at_best=3)

    def test_asking_marks_the_question_open_and_changes_nothing_else(self):
        self.dones(3, hour=19)
        self.mornings(3)
        consider(self.db, ANA, NOW)
        user = self.user()
        self.assertEqual(json.loads(user["learning_pending"]), {"hour": 19})
        self.assertEqual(user["learning_asked_at"], to_db(NOW))
        self.assertIsNone(user["learned_hour"])                # learning asks first

    def test_asking_is_logged_with_the_evidence(self):
        self.dones(3, hour=19)
        self.mornings(3)
        consider(self.db, ANA, NOW)
        [row] = self.log()
        self.assertEqual((row["event"], row["old_hour"], row["new_hour"]), ("asked", 10, 19))
        self.assertEqual(row["created_at"], to_db(NOW))
        logged = json.loads(row["evidence"])
        self.assertEqual(logged["misses"], 3)
        self.assertEqual(logged["by_hour"], {"10": {"answered": 3, "done": 0}, "19": {"answered": 3, "done": 3}})

    def test_two_nudges_at_the_usual_hour_are_not_enough(self):
        self.dones(3, hour=19)
        self.mornings(MIN_DEFAULT_NUDGES - 1)
        self.assertIsNone(consider(self.db, ANA, NOW))
        self.assertIsNone(self.user()["learning_pending"])
        self.assertEqual(self.log(), [])

    def test_three_nudges_at_the_usual_hour_are_enough(self):
        self.dones(3, hour=19)
        self.mornings(MIN_DEFAULT_NUDGES)
        self.assertIsNotNone(consider(self.db, ANA, NOW))

    def test_half_done_is_not_fewer_than_half(self):
        self.dones(3, hour=19)
        self.mornings(4, done=2)
        self.assertIsNone(consider(self.db, ANA, NOW))

    def test_one_done_in_four_is_fewer_than_half(self):
        self.dones(3, hour=19)
        self.mornings(4, done=1)
        self.assertIsNotNone(consider(self.db, ANA, NOW))

    def test_one_done_in_three_is_fewer_than_half(self):
        self.dones(3, hour=19)
        self.mornings(3, done=1)
        self.assertIsNotNone(consider(self.db, ANA, NOW))

    def test_two_done_in_three_is_more_than_half(self):
        self.dones(3, hour=19)
        self.mornings(3, done=2)
        self.assertIsNone(consider(self.db, ANA, NOW))

    def test_two_done_taps_at_another_hour_are_not_enough(self):
        self.dones(MIN_DONE_AT_NEW_HOUR - 1, hour=19)
        self.mornings(3)
        self.assertIsNone(consider(self.db, ANA, NOW))

    def test_three_done_taps_at_another_hour_are_enough(self):
        self.dones(MIN_DONE_AT_NEW_HOUR, hour=19)
        self.mornings(3)
        self.assertIsNotNone(consider(self.db, ANA, NOW))

    def test_best_hour_outside_8_to_21_is_never_proposed(self):
        for hour in (7, 22, 23):
            with self.subTest(hour=hour):
                self.setUp()
                self.dones(5, hour=hour)
                self.mornings(3)
                self.assertIsNone(consider(self.db, ANA, NOW))

    def test_the_edges_of_the_window_can_be_proposed(self):
        for hour in (EARLIEST_HOUR, LATEST_HOUR):
            with self.subTest(hour=hour):
                self.setUp()
                self.dones(3, hour=hour)
                self.mornings(3)
                consider(self.db, ANA, NOW)
                self.assertEqual(json.loads(self.user()["learning_pending"]), {"hour": hour})

    def test_never_while_a_question_is_open(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.db.update_user(ANA, learning_pending=json.dumps({"hour": 14}))
        self.assertIsNone(consider(self.db, ANA, NOW))
        self.assertEqual(json.loads(self.user()["learning_pending"]), {"hour": 14})

    def test_asking_twice_asks_once(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.assertIsNotNone(consider(self.db, ANA, NOW))
        self.assertIsNone(consider(self.db, ANA, NOW + timedelta(days=30)))
        self.assertEqual(len(self.log()), 1)

    def test_never_while_learning_is_off(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.db.update_user(ANA, learning_off=1)
        self.assertIsNone(consider(self.db, ANA, NOW))

    def test_not_within_14_days_of_the_last_question(self):
        self.dones(3, hour=19)
        self.mornings(3)
        asked = NOW.astimezone(UTC) - ASK_AGAIN_AFTER + timedelta(seconds=1)     # 14 real days minus 1 s
        self.db.update_user(ANA, learning_asked_at=to_db(asked))
        self.assertIsNone(consider(self.db, ANA, NOW))

    def test_exactly_14_days_after_the_last_question(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.db.update_user(ANA, learning_asked_at=to_db(NOW.astimezone(UTC) - timedelta(days=14)))
        self.assertIsNotNone(consider(self.db, ANA, NOW))

    def test_14_days_are_real_days_across_the_clock_change(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.db.update_user(ANA, learning_asked_at=to_db(madrid(2026, 10, 20, 10, 30)))   # summer time
        self.assertIsNone(consider(self.db, ANA, madrid(2026, 11, 3, 9, 29)))           # 13 d 23 h 59 min
        self.assertIsNotNone(consider(self.db, ANA, madrid(2026, 11, 3, 9, 30)))        # 14 d exactly

    def test_now_in_any_zone(self):
        self.dones(3, hour=19)
        self.mornings(3)
        self.db.update_user(ANA, learning_asked_at=to_db(NOW.astimezone(UTC) - timedelta(days=14)))
        self.assertIsNotNone(consider(self.db, ANA, NOW.astimezone(UTC)))

    def test_spanish_person_is_asked_in_spanish(self):
        self.db.update_user(ANA, lang="es")
        self.dones(3, hour=19)
        self.mornings(3)
        spy = self.spy_render()
        reply = consider(self.db, ANA, NOW)
        self.assertEqual(reply.lang, "es")
        self.assertEqual(spy.call_args.args, ("learning_question", "es"))

    def test_after_a_learned_hour_the_rule_looks_at_that_hour(self):
        self.db.update_user(ANA, learned_hour=19)
        self.mornings(3, hour=19, source="learned")            # 19:00 stopped working...
        self.dones(3, hour=9)                                  # ...and 09:00 works now
        spy = self.spy_render()
        consider(self.db, ANA, NOW)
        self.assertEqual(spy.call_args.kwargs["current_hour"], 19)
        self.assertEqual(json.loads(self.user()["learning_pending"]), {"hour": 9})
        self.assertEqual((self.log()[0]["old_hour"], self.log()[0]["new_hour"]), (19, 9))

    def test_a_failed_render_leaves_no_open_question(self):
        self.dones(3, hour=19)
        self.mornings(3)
        with mock.patch("wazy.learning.render", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                consider(self.db, ANA, NOW)
        self.assertIsNone(self.user()["learning_pending"])     # rolled back: never a question nobody saw
        self.assertEqual(self.log(), [])


# =============================================================================================
# answer(): YES and NO
# =============================================================================================
class AnswerTest(LearningTest):
    def setUp(self) -> None:
        super().setUp()
        self.dones(3, hour=19)
        self.mornings(3)
        consider(self.db, ANA, NOW)
        self.answered_at = NOW + timedelta(minutes=5)

    def test_yes_sets_the_learned_hour(self):
        spy = self.spy_render()
        reply = answer(self.db, ANA, True, self.answered_at)
        self.assertEqual((reply.kind, reply.lang), ("learning_approved", "en"))
        spy.assert_called_once_with("learning_approved", "en", hour=19)
        user = self.user()
        self.assertEqual(user["learned_hour"], 19)
        self.assertIsNone(user["learning_pending"])
        self.assertEqual(usual_hour(user), 19)

    def test_yes_is_logged(self):
        answer(self.db, ANA, True, self.answered_at)
        row = self.log()[0]
        self.assertEqual((row["event"], row["old_hour"], row["new_hour"]), ("approved", 10, 19))
        self.assertEqual(row["created_at"], to_db(self.answered_at))
        self.assertIsNotNone(json.loads(row["evidence"]))

    def test_no_keeps_the_hour(self):
        spy = self.spy_render()
        reply = answer(self.db, ANA, False, self.answered_at)
        self.assertEqual((reply.kind, reply.lang), ("learning_rejected", "en"))
        spy.assert_called_once_with("learning_rejected", "en", hour=10)
        user = self.user()
        self.assertIsNone(user["learned_hour"])
        self.assertIsNone(user["learning_pending"])

    def test_no_is_logged(self):
        answer(self.db, ANA, False, self.answered_at)
        row = self.log()[0]
        self.assertEqual((row["event"], row["old_hour"], row["new_hour"]), ("rejected", 10, 19))

    def test_no_pauses_for_two_weeks_from_the_answer(self):
        answer(self.db, ANA, False, self.answered_at)
        self.assertEqual(self.user()["learning_asked_at"], to_db(self.answered_at))
        self.assertIsNone(consider(self.db, ANA, self.answered_at + timedelta(days=13, hours=23)))
        self.assertIsNotNone(consider(self.db, ANA, self.answered_at + ASK_AGAIN_AFTER))

    def test_yes_with_no_open_question_changes_nothing(self):
        answer(self.db, ANA, False, self.answered_at)
        answer(self.db, ANA, True, self.answered_at)
        self.assertIsNone(self.user()["learned_hour"])

    def test_spanish_answer(self):
        self.db.update_user(ANA, lang="es")
        self.assertEqual(answer(self.db, ANA, True, self.answered_at).lang, "es")


# =============================================================================================
# forget() and resume()
# =============================================================================================
class ForgetResumeTest(LearningTest):
    def setUp(self) -> None:
        super().setUp()
        self.dones(3, hour=19)
        self.mornings(3)
        consider(self.db, ANA, NOW)
        answer(self.db, ANA, True, NOW)

    def test_forget_wipes_the_learned_hour_and_stops_learning(self):
        spy = self.spy_render()
        reply = forget(self.db, ANA, NOW)
        self.assertEqual((reply.kind, reply.lang), ("learning_forgotten", "en"))
        spy.assert_called_once_with("learning_forgotten", "en", default_hour=10)
        user = self.user()
        self.assertIsNone(user["learned_hour"])
        self.assertEqual(user["learning_off"], 1)
        self.assertIsNone(user["learning_asked_at"])
        self.assertEqual(usual_hour(user), 10)

    def test_forget_is_logged(self):
        forget(self.db, ANA, NOW)
        row = self.log()[0]
        self.assertEqual((row["event"], row["old_hour"], row["new_hour"]), ("forgotten", 19, 10))

    def test_forget_closes_an_open_question(self):
        self.db.update_user(ANA, learning_pending=json.dumps({"hour": 14}))
        forget(self.db, ANA, NOW)
        self.assertIsNone(self.user()["learning_pending"])

    def test_after_forget_it_never_asks(self):
        forget(self.db, ANA, NOW)
        self.assertIsNone(consider(self.db, ANA, NOW + timedelta(days=60)))

    def test_forget_deletes_nothing(self):
        before = self.db.scalar("SELECT COUNT(*) FROM nudge_outcomes")
        forget(self.db, ANA, NOW)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM nudge_outcomes"), before)

    def test_resume_turns_learning_back_on(self):
        forget(self.db, ANA, NOW)
        spy = self.spy_render()
        reply = resume(self.db, ANA, NOW + timedelta(minutes=1))
        self.assertEqual((reply.kind, reply.lang), ("learning_resumed", "en"))
        spy.assert_called_once_with("learning_resumed", "en")
        self.assertEqual(self.user()["learning_off"], 0)
        self.assertEqual(self.log()[0]["event"], "resumed")

    def test_after_resume_it_may_ask_again_but_still_asks_first(self):
        forget(self.db, ANA, NOW)
        resume(self.db, ANA, NOW)
        self.assertIsNotNone(consider(self.db, ANA, NOW))
        self.assertIsNone(self.user()["learned_hour"])


# =============================================================================================
# explain(): the real numbers
# =============================================================================================
class ExplainTest(LearningTest):
    def test_before_anything_is_learned(self):
        self.dones(3, hour=19)
        self.mornings(4, done=1)
        spy = self.spy_render()
        reply = explain(self.db, ANA)
        self.assertEqual((reply.kind, reply.lang), ("learning_explained", "en"))
        self.assertTrue(reply.text)
        spy.assert_called_once_with("learning_explained", "en", off=False, learned_hour=None, default_hour=10,
                                    done_at_current=1, answered_at_current=4, evidence=evidence(self.db, ANA))

    def test_with_a_learned_hour(self):
        self.dones(3, hour=19)
        self.mornings(3)
        consider(self.db, ANA, NOW)
        answer(self.db, ANA, True, NOW)
        spy = self.spy_render()
        explain(self.db, ANA)
        params = spy.call_args.kwargs
        self.assertEqual((params["off"], params["learned_hour"], params["default_hour"]), (False, 19, 10))
        self.assertEqual(params["evidence"]["current_hour"], 19)

    def test_when_learning_is_off(self):
        forget(self.db, ANA, NOW)
        spy = self.spy_render()
        explain(self.db, ANA)
        self.assertEqual((spy.call_args.kwargs["off"], spy.call_args.kwargs["learned_hour"]), (True, None))

    def test_text_shows_the_real_numbers(self):
        self.dones(3, hour=19)
        self.mornings(4, done=1)
        text = explain(self.db, ANA).text
        self.assertIn("10:00", text)
        self.assertRegex(text, r"\b1\b.*\b4\b")                 # Done on 1 of the 4 nudges at 10:00
        self.assertIn("19:00", text)                           # the hour Done is tapped most

    def test_explain_changes_nothing(self):
        explain(self.db, ANA)
        self.assertEqual(self.log(), [])
        self.assertIsNone(self.user()["learning_pending"])

    def test_spanish(self):
        self.db.update_user(ANA, lang="es")
        self.assertEqual(explain(self.db, ANA).lang, "es")


# =============================================================================================
# Two people never affect each other
# =============================================================================================
class TwoPeopleTest(LearningTest):
    def setUp(self) -> None:
        super().setUp()
        self.person(BEN, lang="es")
        self.dones(3, hour=19)
        self.mornings(3)

    def test_evidence_is_per_person(self):
        self.assertEqual(evidence(self.db, BEN)["answered"], 0)
        self.assertEqual(evidence(self.db, ANA)["answered"], 6)

    def test_one_persons_history_never_asks_the_other(self):
        self.assertIsNone(consider(self.db, BEN, NOW))
        self.assertIsNotNone(consider(self.db, ANA, NOW))
        self.assertIsNone(self.user(BEN)["learning_pending"])
        self.assertEqual(self.log(BEN), [])

    def test_answers_forget_and_resume_touch_only_that_person(self):
        self.mornings(3, user_id=BEN)
        self.dones(3, hour=8, user_id=BEN)
        consider(self.db, ANA, NOW)
        consider(self.db, BEN, NOW)
        answer(self.db, ANA, True, NOW)
        answer(self.db, BEN, False, NOW)
        self.assertEqual((self.user(ANA)["learned_hour"], self.user(BEN)["learned_hour"]), (19, None))
        forget(self.db, BEN, NOW)
        self.assertEqual((self.user(ANA)["learning_off"], self.user(BEN)["learning_off"]), (0, 1))
        self.assertEqual(self.user(ANA)["learned_hour"], 19)
        self.assertEqual([r["event"] for r in self.log(ANA)], ["approved", "asked"])
        self.assertEqual([r["event"] for r in self.log(BEN)], ["forgotten", "rejected", "asked"])

    def test_each_person_hears_their_own_language(self):
        self.mornings(3, user_id=BEN)
        self.dones(3, hour=8, user_id=BEN)
        self.assertEqual(consider(self.db, ANA, NOW).lang, "en")
        self.assertEqual(consider(self.db, BEN, NOW).lang, "es")
        self.assertIsInstance(explain(self.db, BEN), Reply)


if __name__ == "__main__":
    unittest.main()
