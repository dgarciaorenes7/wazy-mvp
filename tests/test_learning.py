"""The Session 7 learning loop: Wazy notices when a person ticks things off, ASKS before changing their
default nudge hour, never touches a time they gave, never pushes a deadline later, and forgets on request.
Run: python3 -m unittest discover tests"""
import unittest
from datetime import datetime, timedelta

from wazy.store import Store

MON = datetime(2026, 11, 2, 9, 0)  # Monday 2 November 2026, 09:00


def bot_texts(store):
    return [m["text"] for m in store.state()["messages"] if m["role"] == "bot"]


class LearningLoop(unittest.TestCase):
    def new_store(self):
        store = Store(":memory:", clock=lambda: MON)
        self.addCleanup(store.close)
        return store

    def answer_next_nudge(self, store, text, nudge_at, action, after_minutes):
        store.handle_text(text, now=nudge_at - timedelta(hours=2))
        self.assertEqual(store.run_due(nudge_at), 1, text)
        store.answer_nudge(store.latest_open_commitment(), action, now=nudge_at + timedelta(minutes=after_minutes))

    def build_history(self, store):
        """Three things ticked off around 19:00, then three 10:00 default nudges answered Later."""
        for day in range(3):
            evening = datetime(2026, 11, 2 + day, 19, 0)
            self.answer_next_nudge(store, "gym at 19:00", evening, "done", 5)
        for day in range(3):
            morning = datetime(2026, 11, 6 + day, 10, 0)
            store.handle_text("reply to Carlos tomorrow", now=morning - timedelta(hours=14))
            self.assertGreaterEqual(store.run_due(morning), 1)
            for c in store.state()["commitments"]:          # yesterday's Later comes back at 10:00 too
                if c["status"] == "nudged":
                    store.answer_nudge(c["id"], "later", now=morning + timedelta(minutes=2))
        return datetime(2026, 11, 8, 10, 5)

    def test_defaulted_and_stated_times_are_recorded_as_such(self):
        store = self.new_store()
        store.handle_text("reply to Carlos tomorrow", now=MON)
        store.handle_text("call the bank tomorrow at 9am", now=MON)
        sources = {c["raw_text"]: c["time_source"] for c in store.state()["commitments"]}
        self.assertEqual(sources["reply to Carlos tomorrow"], "default")
        self.assertEqual(sources["call the bank tomorrow at 9am"], "stated")

    def test_it_asks_before_changing_anything(self):
        store = self.new_store()
        self.build_history(store)
        question = [t for t in bot_texts(store) if t.startswith("Quick question")]
        self.assertEqual(len(question), 1)
        self.assertIn("Shall I use 19:00", question[0])
        self.assertIsNone(store.learned_hour())                       # nothing applied yet
        self.assertEqual(store.state()["learning_log"][0]["event"], "asked")

    def test_yes_applies_the_hour_only_to_defaulted_times(self):
        store = self.new_store()
        now = self.build_history(store)
        store.handle_text("yes", now=now)
        self.assertEqual(store.learned_hour(), 19)
        self.assertEqual(store.state()["learning_log"][0]["event"], "approved")
        store.handle_text("send the report on Friday", now=now)
        store.handle_text("call the bank on Friday at 9am", now=now)
        by_text = {c["raw_text"]: c for c in store.state()["commitments"]}
        self.assertEqual(by_text["send the report on Friday"]["nudge_at"], "2026-11-13T19:00:00")
        self.assertEqual(by_text["send the report on Friday"]["time_source"], "learned")
        self.assertEqual(by_text["call the bank on Friday at 9am"]["nudge_at"], "2026-11-13T09:00:00")
        self.assertTrue(any(t.endswith("(your usual time).") for t in bot_texts(store)))

    def test_a_learned_hour_never_pushes_a_deadline_later(self):
        store = self.new_store()
        now = self.build_history(store)
        store.handle_text("yes", now=now)
        store.handle_text("hand in the essay by Friday", now=now)
        essay = [c for c in store.state()["commitments"] if c["raw_text"] == "hand in the essay by Friday"][0]
        self.assertEqual(essay["nudge_at"], "2026-11-13T10:00:00")

    def test_no_keeps_the_default_and_it_waits_two_weeks_before_asking_again(self):
        store = self.new_store()
        now = self.build_history(store)
        store.handle_text("no", now=now)
        self.assertIsNone(store.learned_hour())
        self.assertEqual(store.state()["learning_log"][0]["event"], "rejected")
        self.assertFalse(store.consider_learning(now + timedelta(days=13)))
        self.assertTrue(store.consider_learning(now + timedelta(days=15)))

    def test_forget_wipes_it_and_stops_learning(self):
        store = self.new_store()
        now = self.build_history(store)
        store.handle_text("yes", now=now)
        store.handle_text("forget", now=now)
        self.assertIsNone(store.learned_hour())
        self.assertFalse(store.consider_learning(now + timedelta(days=30)))
        store.handle_text("send the report on Friday", now=now)
        report = [c for c in store.state()["commitments"] if c["raw_text"] == "send the report on Friday"][0]
        self.assertEqual(report["nudge_at"], "2026-11-13T10:00:00")
        self.assertEqual(store.state()["learning_log"][0]["event"], "forgotten")

    def test_what_did_you_learn_explains_it_in_plain_words(self):
        store = self.new_store()
        now = self.build_history(store)
        store.handle_text("yes", now=now)
        store.handle_text("what did you learn", now=now)
        self.assertIn("I nudge you at 19:00, because you said YES", bot_texts(store)[-1])

    def test_not_enough_evidence_means_no_question(self):
        store = self.new_store()
        self.answer_next_nudge(store, "gym at 19:00", datetime(2026, 11, 2, 19, 0), "done", 5)
        morning = datetime(2026, 11, 4, 10, 0)
        store.handle_text("reply to Carlos tomorrow", now=morning - timedelta(hours=14))
        store.run_due(morning)
        store.answer_nudge(store.latest_open_commitment(), "later", now=morning + timedelta(minutes=2))
        self.assertFalse(any(t.startswith("Quick question") for t in bot_texts(store)))

    def test_a_missed_time_and_the_reply_that_fixed_it_are_kept_for_review(self):
        store = self.new_store()
        store.handle_text("ping Laura about the flat", now=MON)
        store.handle_text("tomorrow 6pm", now=MON)
        misses = store.state()["reader_misses"]
        self.assertEqual(len(misses), 1)
        self.assertEqual(misses[0]["original"], "ping Laura about the flat")
        self.assertEqual(misses[0]["reply"], "tomorrow 6pm")

    def test_first_nudge_done_rate_counts_only_first_answers(self):
        store = self.new_store()
        self.build_history(store)
        self.assertEqual(store.stats()["first_nudge_done_rate"], 50)   # 3 done first time, 3 later first time


if __name__ == "__main__":
    unittest.main()
