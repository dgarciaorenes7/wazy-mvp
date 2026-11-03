"""The App facade (SPEC 2 and 2.1): handle, tap, tick, state, admin_state, reset, close, plus the v1 loop and
learning tests ported to v2 (aware times, UTC in the database, one row per person).

Run: python3.9 -m unittest tests.test_app -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from wazy import learning
from wazy.app import App
from wazy.conversation import Conversation
from wazy.db import Database
from wazy.scheduler import Scheduler

MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
T0 = datetime(2026, 11, 3, 9, 30, tzinfo=MADRID)        # Tuesday 3 November 2026
MON = datetime(2026, 11, 2, 9, 0, tzinfo=MADRID)        # Monday 2 November 2026
ALICE, BOB = "web:alice", "web:bob"
STATE_KEYS = {"messages", "open", "commitments", "outcomes", "stats", "learning_log", "reader_misses"}


class Clock:
    """A clock the test moves by hand. It keeps UTC so adding hours is exact on DST days."""

    def __init__(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    def set(self, when: datetime) -> None:
        self.now = when.astimezone(UTC)


class Base(unittest.TestCase):
    def make_app(self, now: datetime = T0, **options) -> App:
        self.clock = Clock(now)
        self.delivered = []
        self.app = App(clock=self.clock, on_message=lambda uid, mid, reply: self.delivered.append((uid, mid, reply)),
                       **options)
        self.addCleanup(self.app.close)
        return self.app

    def setUp(self) -> None:
        self.make_app()

    def say(self, text: str, user: str = ALICE, when: datetime = None, **options):
        if when is not None:
            self.clock.set(when)
        return self.app.handle(user, text, **options)

    def kinds(self, replies) -> list:
        return [reply.kind for reply in replies]

    def bot_texts(self, user: str = ALICE) -> list:
        return [m["text"] for m in self.app.state(user)["messages"] if m["role"] == "bot"]

    def bot_kinds(self, user: str = ALICE) -> list:
        return [m["kind"] for m in self.app.state(user)["messages"] if m["role"] == "bot"]

    def commitments(self, user: str = ALICE) -> list:
        return list(reversed(self.app.state(user)["commitments"]))

    def tick_at(self, when: datetime) -> int:
        self.clock.set(when)
        return self.app.tick()

    def tap_at(self, when: datetime, commitment_id: int, action: str, user: str = ALICE):
        self.clock.set(when)
        return self.app.tap(user, commitment_id, action)

    def latest_open(self, user: str = ALICE):
        row = self.app.db.one("SELECT commitment_id FROM nudge_outcomes WHERE user_id = ? AND outcome = 'pending' "
                              "ORDER BY id DESC LIMIT 1", (user,))
        return row["commitment_id"] if row else None


# =============================================================================================
# The facade itself
# =============================================================================================
class Facade(Base):
    def test_app_exposes_its_parts(self):
        self.assertIsInstance(self.app.db, Database)
        self.assertIsInstance(self.app.scheduler, Scheduler)
        self.assertIsInstance(self.app.conversation, Conversation)
        self.assertIs(self.app.clock, self.clock)

    def test_the_default_clock_is_aware_in_the_default_zone(self):
        app = App(default_tz="Europe/London")
        self.addCleanup(app.close)
        now = app.clock()
        self.assertIsNotNone(now.utcoffset())
        self.assertEqual(now.tzinfo, LONDON)
        self.assertEqual(now.microsecond, 0)
        self.assertLess(abs((now - datetime.now(UTC)).total_seconds()), 5)

    def test_a_wrong_default_zone_stops_the_app_from_starting(self):
        with self.assertRaises(Exception):
            App(default_tz="Mars/Olympus_Mons")

    def test_a_clock_without_a_zone_is_read_in_the_default_zone(self):
        app = App(clock=lambda: datetime(2026, 11, 3, 9, 30))
        self.addCleanup(app.close)
        app.handle(ALICE, "call mum in 30 minutes")
        self.assertEqual(app.state(ALICE)["commitments"][0]["nudge_at"], "2026-11-03T09:00:00Z")

    def test_handle_creates_a_new_person_in_the_zone_given(self):
        self.say("hi", "whatsapp:+447700900123", channel="whatsapp", tz="Europe/London")
        person = self.app.db.one("SELECT * FROM users WHERE id = ?", ("whatsapp:+447700900123",))
        self.assertEqual((person["channel"], person["tz"]), ("whatsapp", "Europe/London"))

    def test_the_zone_is_only_used_when_the_person_is_new(self):
        self.say("hi", ALICE, tz="Europe/London")
        self.say("hi", ALICE, tz="Europe/Paris")
        self.assertEqual(self.app.db.scalar("SELECT tz FROM users WHERE id = ?", (ALICE,)), "Europe/London")

    def test_a_person_in_london_gets_london_times(self):
        self.say("call mum tomorrow at 9am", "whatsapp:+447700900123", channel="whatsapp", tz="Europe/London")
        commitment = self.app.state("whatsapp:+447700900123")["commitments"][0]
        self.assertEqual((commitment["nudge_at"], commitment["nudge_at_local"]),
                         ("2026-11-04T09:00:00Z", "2026-11-04T09:00:00"))

    def test_tick_runs_the_scheduler_at_the_clock(self):
        self.say("pay rent in 5 seconds")
        self.assertEqual(self.app.tick(), 0)
        self.clock.advance(seconds=5)
        self.assertEqual(self.app.tick(), 1)
        self.assertEqual(self.app.tick(), 0)

    def test_close_closes_the_database(self):
        app = App()
        app.close()
        with self.assertRaises(Exception):
            app.db.scalar("SELECT 1")


# =============================================================================================
# The v1 loop, ported (test_flow.py at cad4eb1)
# =============================================================================================
class TheLoop(Base):
    def test_nothing_is_sent_early_and_exactly_one_nudge_is_sent_on_time(self):
        self.say("Pay rent in 5 seconds")
        self.assertEqual(self.tick_at(T0 + timedelta(seconds=4)), 0)
        self.assertEqual(self.tick_at(T0 + timedelta(seconds=5)), 1)
        self.assertEqual(self.tick_at(T0 + timedelta(seconds=6)), 0)       # never twice
        state = self.app.state(ALICE)
        self.assertEqual([o["outcome"] for o in state["outcomes"]], ["pending"])
        self.assertEqual(len(state["open"]), 1)

    def test_done_records_the_outcome_and_response_time(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        cid = self.commitments()[0]["id"]
        self.assertEqual(self.kinds(self.tap_at(T0 + timedelta(seconds=12), cid, "done")), ["done_ack"])
        state = self.app.state(ALICE)
        self.assertEqual((state["outcomes"][0]["outcome"], state["outcomes"][0]["seconds_to_respond"]), ("done", 7))
        self.assertEqual(state["commitments"][0]["status"], "done")
        self.assertEqual(state["stats"]["done_rate"], 100)
        self.assertEqual(self.tap_at(T0 + timedelta(seconds=20), cid, "done"), [])     # nothing left open

    def test_later_means_one_more_nudge_next_morning_at_ten(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        cid = self.commitments()[0]["id"]
        replies = self.tap_at(T0 + timedelta(seconds=9), cid, "later")
        self.assertEqual(self.kinds(replies), ["later_ack"])
        self.assertIn("tomorrow at 10:00", replies[0].text)
        commitment = self.commitments()[0]
        self.assertEqual((commitment["status"], commitment["later_count"]), ("scheduled", 1))
        self.assertEqual((commitment["nudge_at"], commitment["nudge_at_local"]),
                         ("2026-11-04T09:00:00Z", "2026-11-04T10:00:00"))

    def test_later_can_be_shortened_for_a_live_demo(self):
        self.make_app(later_minutes=1)
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        self.tap_at(T0 + timedelta(seconds=10), self.commitments()[0]["id"], "later")
        self.assertEqual(self.commitments()[0]["nudge_at_local"], "2026-11-03T09:31:10")
        self.assertEqual(self.tick_at(T0 + timedelta(seconds=70)), 1)

    def test_later_tapped_after_midnight_means_this_morning(self):
        late = datetime(2026, 11, 4, 0, 30, tzinfo=MADRID)
        self.say("Pay rent in 5 seconds", when=late - timedelta(seconds=5))
        self.tick_at(late)
        self.tap_at(late, self.commitments()[0]["id"], "later")
        self.assertEqual(self.commitments()[0]["nudge_at_local"], "2026-11-04T10:00:00")

    def test_later_across_the_autumn_clock_change_is_stored_in_utc(self):
        saturday = datetime(2026, 10, 24, 18, 0, tzinfo=MADRID)        # clocks go back on Sunday 25 Oct
        self.say("Pay rent in 5 seconds", when=saturday)
        self.tick_at(saturday + timedelta(seconds=5))
        self.tap_at(saturday + timedelta(seconds=9), self.commitments()[0]["id"], "later")
        commitment = self.commitments()[0]
        self.assertEqual((commitment["nudge_at"], commitment["nudge_at_local"]),
                         ("2026-10-25T09:00:00Z", "2026-10-25T10:00:00"))

    def test_stop_ends_every_nudge(self):
        self.say("Pay rent in 5 seconds")
        self.say("Call mum tomorrow 9am")
        self.tick_at(T0 + timedelta(seconds=5))
        self.say("STOP", when=T0 + timedelta(seconds=6))
        state = self.app.state(ALICE)
        self.assertEqual(state["outcomes"][0]["outcome"], "stop")
        self.assertTrue(all(c["status"] == "stopped" for c in state["commitments"]))
        self.assertEqual(self.tick_at(T0 + timedelta(days=2)), 0)

    def test_no_answer_within_24_hours_is_recorded_as_no_reply(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        self.tick_at(T0 + timedelta(hours=24, seconds=5))
        state = self.app.state(ALICE)
        self.assertEqual(state["outcomes"][0]["outcome"], "no_reply")
        self.assertEqual(state["commitments"][0]["status"], "no_reply")
        self.assertEqual(state["open"], [])

    def test_a_nudge_answered_after_24_hours_is_no_longer_open(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        self.tick_at(T0 + timedelta(hours=25))
        self.assertEqual(self.app.tap(ALICE, self.commitments()[0]["id"], "done"), [])
        self.assertEqual(self.kinds(self.say("done")), ["no_open_nudge"])

    def test_stats_count_every_row_not_just_the_latest_fifty(self):
        for i in range(60):
            self.clock.set(T0 + timedelta(seconds=4 * i))
            self.say(f"Task {i} in 5 seconds")
        self.tick_at(T0 + timedelta(minutes=10))
        for commitment in self.commitments()[:30]:
            self.app.tap(ALICE, commitment["id"], "done")
        state = self.app.state(ALICE)
        self.assertEqual(len(state["commitments"]), 50)
        stats = state["stats"]
        self.assertEqual((stats["commitments"], stats["nudges_sent"], stats["done"], stats["pending"],
                          stats["done_rate"]), (60, 60, 30, 30, 100))


# =============================================================================================
# The v1 learning loop, ported (test_learning.py at cad4eb1)
# =============================================================================================
class LearningLoop(Base):
    def setUp(self) -> None:
        self.make_app(MON)

    def answer_next_nudge(self, text: str, nudge_at: datetime, action: str, after_minutes: int) -> None:
        self.say(text, when=nudge_at - timedelta(hours=2))
        self.assertEqual(self.tick_at(nudge_at), 1, text)
        self.tap_at(nudge_at + timedelta(minutes=after_minutes), self.latest_open(), action)

    def build_history(self) -> datetime:
        """Three things ticked off around 19:00, then 10:00 default nudges answered Later three mornings."""
        for day in range(3):
            evening = datetime(2026, 11, 2 + day, 19, 0, tzinfo=MADRID)
            self.answer_next_nudge("gym at 19:00", evening, "done", 5)
        for day in range(3):
            morning = datetime(2026, 11, 6 + day, 10, 0, tzinfo=MADRID)
            self.say("reply to Carlos tomorrow", when=morning - timedelta(hours=14))
            self.assertGreaterEqual(self.tick_at(morning), 1)
            for commitment in self.commitments():          # yesterday's Later comes back at 10:00 too
                if commitment["status"] == "nudged":
                    self.tap_at(morning + timedelta(minutes=2), commitment["id"], "later")
        return datetime(2026, 11, 8, 10, 5, tzinfo=MADRID)

    def learned_hour(self):
        return self.app.db.scalar("SELECT learned_hour FROM users WHERE id = ?", (ALICE,))

    def by_text(self) -> dict:
        return {c["raw_text"]: c for c in self.commitments()}

    def test_it_asks_before_changing_anything(self):
        self.build_history()
        self.assertEqual(self.bot_kinds().count("learning_question"), 1)
        question = [t for t in self.bot_texts() if "19:00" in t and "10:00" in t]
        self.assertTrue(question)
        self.assertIsNone(self.learned_hour())                       # nothing applied yet
        self.assertEqual(self.app.state(ALICE)["learning_log"][0]["event"], "asked")

    def test_the_question_follows_the_tap_that_triggered_it(self):
        self.build_history()
        kinds = self.bot_kinds()
        question_at = kinds.index("learning_question")
        self.assertEqual(kinds[question_at - 1], "later_ack")

    def test_the_question_is_handed_on_like_every_message(self):
        self.build_history()
        self.assertIn("learning_question", [reply.kind for _, _, reply in self.delivered])

    def test_yes_applies_the_hour_only_to_defaulted_times(self):
        now = self.build_history()
        self.assertEqual(self.kinds(self.say("yes", when=now)), ["learning_approved"])
        self.assertEqual(self.learned_hour(), 19)
        self.assertEqual(self.app.state(ALICE)["learning_log"][0]["event"], "approved")
        self.say("send the report on Friday")
        self.say("call the bank on Friday at 9am")
        report, bank = self.by_text()["send the report on Friday"], self.by_text()["call the bank on Friday at 9am"]
        self.assertEqual((report["nudge_at_local"], report["time_source"]), ("2026-11-13T19:00:00", "learned"))
        self.assertEqual((bank["nudge_at_local"], bank["time_source"]), ("2026-11-13T09:00:00", "stated"))
        self.assertTrue(any(t.endswith("(your usual time).") for t in self.bot_texts()))

    def test_a_learned_hour_never_pushes_a_deadline_later(self):
        now = self.build_history()
        self.say("yes", when=now)
        self.say("hand in the essay by Friday")
        self.assertEqual(self.by_text()["hand in the essay by Friday"]["nudge_at_local"], "2026-11-13T10:00:00")

    def test_later_after_yes_uses_the_learned_hour(self):
        now = self.build_history()
        self.say("yes", when=now)
        self.say("pay rent in 5 seconds")
        self.tick_at(now + timedelta(seconds=5))
        self.tap_at(now + timedelta(seconds=9), self.latest_open(), "later")
        self.assertEqual(self.by_text()["pay rent in 5 seconds"]["nudge_at_local"], "2026-11-08T19:00:00")

    def test_no_keeps_the_default_and_waits_two_weeks_before_asking_again(self):
        now = self.build_history()
        self.assertEqual(self.kinds(self.say("no", when=now)), ["learning_rejected"])
        self.assertIsNone(self.learned_hour())
        self.assertEqual(self.app.state(ALICE)["learning_log"][0]["event"], "rejected")
        self.assertIsNone(learning.consider(self.app.db, ALICE, now + timedelta(days=13)))
        self.assertIsNotNone(learning.consider(self.app.db, ALICE, now + timedelta(days=15)))

    def test_yes_with_no_open_question_is_not_a_learning_answer(self):
        self.make_app(MON)
        self.say("hi")
        self.assertNotIn(self.kinds(self.say("yes"))[0], ("learning_approved", "learning_rejected"))
        self.assertIsNone(self.learned_hour())

    def test_forget_wipes_it_and_stops_learning(self):
        now = self.build_history()
        self.say("yes", when=now)
        self.assertEqual(self.kinds(self.say("forget")), ["learning_forgotten"])
        self.assertIsNone(self.learned_hour())
        self.assertIsNone(learning.consider(self.app.db, ALICE, now + timedelta(days=30)))
        self.say("send the report on Friday")
        self.assertEqual(self.by_text()["send the report on Friday"]["nudge_at_local"], "2026-11-13T10:00:00")
        self.assertEqual(self.app.state(ALICE)["learning_log"][0]["event"], "forgotten")

    def test_learn_again_resumes(self):
        now = self.build_history()
        self.say("forget", when=now)
        self.assertEqual(self.kinds(self.say("learn again")), ["learning_resumed"])
        self.assertEqual(self.app.db.scalar("SELECT learning_off FROM users WHERE id = ?", (ALICE,)), 0)
        self.assertEqual(self.app.state(ALICE)["learning_log"][0]["event"], "resumed")

    def test_what_did_you_learn_explains_it_with_the_real_hour(self):
        now = self.build_history()
        self.say("yes", when=now)
        reply = self.say("what did you learn")[-1]
        self.assertEqual(reply.kind, "learning_explained")
        self.assertIn("19:00", reply.text)

    def test_not_enough_evidence_means_no_question(self):
        self.answer_next_nudge("gym at 19:00", datetime(2026, 11, 2, 19, 0, tzinfo=MADRID), "done", 5)
        morning = datetime(2026, 11, 4, 10, 0, tzinfo=MADRID)
        self.say("reply to Carlos tomorrow", when=morning - timedelta(hours=14))
        self.tick_at(morning)
        self.tap_at(morning + timedelta(minutes=2), self.latest_open(), "later")
        self.assertNotIn("learning_question", self.bot_kinds())

    def test_done_rate_counts_answered_nudges(self):
        self.build_history()
        stats = self.app.state(ALICE)["stats"]
        self.assertEqual((stats["done"], stats["later"], stats["done_rate"]), (3, 6, 33))

    def test_learning_is_per_person(self):
        self.build_history()
        self.say("hi", BOB)
        self.assertEqual(self.app.state(BOB)["learning_log"], [])
        self.assertNotIn("learning_question", self.bot_kinds(BOB))
        self.assertNotIn("learning_approved", self.kinds(self.say("yes", BOB)))


# =============================================================================================
# state() and admin_state()
# =============================================================================================
class Snapshots(Base):
    def test_state_has_exactly_the_agreed_keys(self):
        self.say("hi")
        self.assertEqual(set(self.app.state(ALICE)), STATE_KEYS)

    def test_state_of_someone_unknown_is_empty_and_creates_nobody(self):
        state = self.app.state("web:nobody")
        self.assertEqual((state["messages"], state["open"], state["commitments"], state["outcomes"]), ([], [], [], []))
        self.assertEqual(state["stats"]["commitments"], 0)
        self.assertEqual(state["stats"]["done_rate"], 0)
        self.assertIsNone(self.app.db.one("SELECT * FROM users WHERE id = 'web:nobody'"))

    def test_messages_have_the_agreed_fields_and_utc_times(self):
        self.say("hi")
        message = self.app.state(ALICE)["messages"][0]
        self.assertEqual(set(message), {"id", "role", "text", "kind", "lang", "commitment_id", "created_at"})
        self.assertEqual((message["role"], message["text"], message["kind"]), ("user", "hi", "text"))
        self.assertEqual(message["created_at"], "2026-11-03T08:30:00Z")

    def test_messages_are_the_last_200_oldest_first(self):
        for n in range(110):
            self.clock.advance(seconds=4)
            self.say("list")
        messages = self.app.state(ALICE)["messages"]
        self.assertEqual(len(messages), 200)
        self.assertEqual([m["id"] for m in messages], sorted(m["id"] for m in messages))
        self.assertEqual(messages[-1]["kind"], "list_empty")

    def test_commitments_have_the_agreed_fields(self):
        self.say("Send the deck to Ana by Friday 5pm")
        commitment = self.app.state(ALICE)["commitments"][0]
        for key in ("id", "title", "raw_text", "nudge_at", "nudge_at_local", "status", "time_source", "deadline",
                    "later_count", "reader", "created_at"):
            self.assertIn(key, commitment)
        self.assertEqual(commitment["nudge_at"], "2026-11-06T16:00:00Z")
        self.assertEqual(commitment["nudge_at_local"], "2026-11-06T17:00:00")
        self.assertEqual((commitment["deadline"], commitment["later_count"]), (1, 0))
        self.assertEqual(commitment["created_at"], "2026-11-03T08:30:00Z")

    def test_commitments_are_the_latest_50_newest_first(self):
        for n in range(55):
            self.clock.set(T0 + timedelta(seconds=4 * n))
            self.say(f"Task {n} tomorrow at 9am")
        commitments = self.app.state(ALICE)["commitments"]
        self.assertEqual(len(commitments), 50)
        self.assertEqual(commitments[0]["title"], "Task 54")

    def test_open_lists_the_nudges_waiting_for_an_answer(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        nudge = [m for m in self.app.state(ALICE)["messages"] if m["kind"] == "nudge"][0]
        self.assertEqual(self.app.state(ALICE)["open"], [nudge["id"]])
        self.say("done")
        self.assertEqual(self.app.state(ALICE)["open"], [])

    def test_open_shows_only_the_latest_nudge_of_a_commitment(self):
        self.make_app(later_minutes=1)
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        self.say("later")
        self.tick_at(T0 + timedelta(minutes=2))
        nudges = [m["id"] for m in self.app.state(ALICE)["messages"] if m["kind"] == "nudge"]
        self.assertEqual(len(nudges), 2)
        self.assertEqual(self.app.state(ALICE)["open"], [nudges[-1]])

    def test_outcomes_have_the_title_and_the_times(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        self.app.tap(ALICE, self.commitments()[0]["id"], "done")
        outcome = self.app.state(ALICE)["outcomes"][0]
        for key in ("id", "commitment_id", "title", "scheduled_for", "nudged_at", "outcome", "responded_at",
                    "seconds_to_respond"):
            self.assertIn(key, outcome)
        self.assertEqual((outcome["title"], outcome["scheduled_for"], outcome["nudged_at"]),
                         ("Pay rent", "2026-11-03T08:30:05Z", "2026-11-03T08:30:05Z"))

    def test_stats_count_every_outcome(self):
        self.make_app(later_minutes=1)
        for task in ("pay rent", "call mum", "buy milk", "water the plants"):
            self.say(f"{task} in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        ids = [c["id"] for c in self.commitments()]
        self.app.tap(ALICE, ids[0], "done")
        self.app.tap(ALICE, ids[1], "later")
        self.say("cancel 1")                       # the current list holds only the Later one
        self.tick_at(T0 + timedelta(hours=25))     # the last two get no reply
        stats = self.app.state(ALICE)["stats"]
        self.assertEqual({k: stats[k] for k in ("commitments", "nudges_sent", "done", "later", "no_reply",
                                                "stopped", "cancelled", "pending", "done_rate")},
                         {"commitments": 4, "nudges_sent": 4, "done": 1, "later": 1, "no_reply": 2, "stopped": 0,
                          "cancelled": 0, "pending": 0, "done_rate": 25})

    def test_learning_log_and_reader_misses_are_newest_first(self):
        self.say("buy milk")
        self.say("call mum")
        misses = self.app.state(ALICE)["reader_misses"]
        self.assertEqual([m["original"] for m in misses], ["call mum", "buy milk"])

    def test_admin_state_has_every_key_plus_users_and_focus(self):
        self.say("hi")
        self.assertEqual(set(self.app.admin_state()), STATE_KEYS | {"users", "focus_user"})

    def test_admin_state_with_nobody(self):
        admin = self.app.admin_state()
        self.assertEqual((admin["users"], admin["focus_user"], admin["messages"]), ([], None, []))

    def test_admin_state_shows_everyone_and_the_most_recently_active_conversation(self):
        self.say("call mum tomorrow at 9am", ALICE)
        self.clock.advance(minutes=1)
        self.say("buy milk tomorrow at 9am", BOB)
        self.say("pay rent tomorrow at 9am", BOB)
        admin = self.app.admin_state()
        self.assertEqual(admin["focus_user"], BOB)
        self.assertEqual({m["text"] for m in admin["messages"] if m["role"] == "user"},
                         {"buy milk tomorrow at 9am", "pay rent tomorrow at 9am"})
        self.assertEqual(len(admin["commitments"]), 3)
        self.assertEqual(admin["stats"]["commitments"], 3)
        users = {u["id"]: u for u in admin["users"]}
        self.assertEqual(set(users), {ALICE, BOB})
        self.assertEqual(set(users[BOB]), {"id", "channel", "lang", "tz", "opted_out", "learned_hour", "commitments",
                                           "last_seen_at"})
        self.assertEqual((users[BOB]["commitments"], users[ALICE]["commitments"]), (2, 1))
        self.assertEqual(admin["users"][0]["id"], BOB)
        self.clock.advance(minutes=1)
        self.say("list", ALICE)
        self.assertEqual(self.app.admin_state()["focus_user"], ALICE)


# =============================================================================================
# Each person only ever sees their own data
# =============================================================================================
class Privacy(Base):
    def setUp(self) -> None:
        super().setUp()
        self.say("call mum tomorrow at 9am", ALICE)
        self.say("pay rent in 5 seconds", ALICE)
        self.say("buy milk", ALICE)
        self.say("water the plants tomorrow at 10am", BOB)
        self.tick_at(T0 + timedelta(seconds=5))

    def test_state_holds_only_that_persons_rows(self):
        bob = self.app.state(BOB)
        self.assertEqual([c["title"] for c in bob["commitments"]], ["Water the plants"])
        self.assertEqual(bob["outcomes"], [])
        self.assertEqual(bob["open"], [])
        self.assertEqual(bob["reader_misses"], [])
        self.assertNotIn("call mum", " ".join(m["text"] for m in bob["messages"]).lower())
        self.assertEqual(bob["stats"]["commitments"], 1)
        alice = self.app.state(ALICE)
        self.assertNotIn("Water the plants", [c["title"] for c in alice["commitments"]])
        self.assertEqual(len(alice["open"]), 1)

    def test_list_and_cancel_only_reach_your_own(self):
        reply = self.say("list", BOB)[-1]
        self.assertNotIn("Call mum", reply.text)
        self.say("cancel 1", BOB)
        self.assertEqual({c["title"]: c["status"] for c in self.commitments(ALICE)},
                         {"Call mum": "scheduled", "Pay rent": "nudged"})

    def test_typed_done_only_answers_your_own_nudge(self):
        self.assertEqual(self.kinds(self.say("done", BOB)), ["no_open_nudge"])
        self.assertEqual(self.app.state(ALICE)["outcomes"][0]["outcome"], "pending")

    def test_tap_by_the_wrong_person_returns_nothing(self):
        cid = self.latest_open(ALICE)
        self.assertEqual(self.app.tap(BOB, cid, "done"), [])
        self.assertEqual(self.app.tap(BOB, cid, "later"), [])
        self.assertEqual(self.app.state(ALICE)["outcomes"][0]["outcome"], "pending")

    def test_a_wait_belongs_to_one_person(self):
        self.say("tomorrow at 9am", BOB)
        self.assertEqual([c["title"] for c in self.commitments(BOB)], ["Water the plants"])
        self.assertEqual(self.app.db.scalar("SELECT pending_title FROM users WHERE id = ?", (ALICE,)), "Buy milk")

    def test_stop_only_stops_your_own(self):
        self.say("stop", BOB)
        self.assertEqual({c["status"] for c in self.commitments(ALICE)}, {"scheduled", "nudged"})
        self.assertEqual(self.app.db.scalar("SELECT opted_out FROM users WHERE id = ?", (ALICE,)), 0)


# =============================================================================================
# on_message: every Wazy message is handed on, once, after it is stored
# =============================================================================================
class Delivery(Base):
    def test_on_message_is_called_for_every_bot_message(self):
        self.say("Pay rent in 5 seconds")
        self.say("buy milk")
        self.tick_at(T0 + timedelta(seconds=5))
        self.app.tap(ALICE, self.latest_open(), "later")
        bot = [m for m in self.app.state(ALICE)["messages"] if m["role"] == "bot"]
        self.assertEqual([(uid, mid, reply.kind) for uid, mid, reply in self.delivered],
                         [(ALICE, m["id"], m["kind"]) for m in bot])
        self.assertEqual([m["kind"] for m in bot], ["welcome", "confirm", "ask_time", "nudge", "later_ack"])

    def test_handed_on_replies_are_the_returned_replies(self):
        replies = self.say("Pay rent in 5 seconds")
        self.assertEqual([reply for _, _, reply in self.delivered], replies)

    def test_user_messages_are_never_handed_on(self):
        self.say("hi")
        self.say("list")
        self.assertEqual(len(self.delivered), 2)

    def test_a_failing_delivery_hook_does_not_break_the_conversation(self):
        def broken(uid, mid, reply):
            raise RuntimeError("network down")
        app = App(clock=self.clock, on_message=broken)
        self.addCleanup(app.close)
        with self.assertLogs("wazy.conversation", "WARNING") as logs:
            self.assertEqual([r.kind for r in app.handle(ALICE, "call mum tomorrow at 9am")], ["welcome", "confirm"])
        self.assertNotIn("mum", "\n".join(logs.output))                 # message text never logged
        self.assertEqual(len(app.state(ALICE)["commitments"]), 1)

    def test_no_hook_is_fine(self):
        app = App(clock=self.clock)
        self.addCleanup(app.close)
        self.assertEqual([r.kind for r in app.handle(ALICE, "hi")], ["welcome"])


# =============================================================================================
# reset()
# =============================================================================================
class Reset(Base):
    def test_reset_wipes_everyone_but_keeps_settings(self):
        self.app.db.set_setting("access_code", "keep-me-please")
        self.say("Pay rent in 5 seconds")
        self.say("buy milk", BOB)
        self.tick_at(T0 + timedelta(seconds=5))
        self.app.reset()
        for table in ("users", "messages", "commitments", "nudge_outcomes", "learning_log", "reader_misses", "outbox"):
            self.assertEqual(self.app.db.scalar(f"SELECT COUNT(*) FROM {table}"), 0, table)
        self.assertEqual(self.app.db.setting("access_code"), "keep-me-please")
        self.assertEqual(self.app.admin_state()["users"], [])

    def test_after_reset_the_next_message_gets_the_welcome(self):
        self.say("hi")
        self.app.reset()
        self.assertEqual(self.kinds(self.say("call mum tomorrow at 9am")), ["welcome", "confirm"])

    def test_after_reset_an_old_tap_reaches_nothing(self):
        self.say("Pay rent in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=5))
        old = self.latest_open()
        self.app.reset()
        self.say("Pay bills in 5 seconds")
        self.tick_at(T0 + timedelta(seconds=10))
        self.assertEqual(self.app.tap(ALICE, old, "done"), [])


if __name__ == "__main__":
    unittest.main()
