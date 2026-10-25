"""The whole loop: commitment -> confirmation -> one nudge -> Done / Later / Stop / no reply -> outcome row.
Run: python3 -m unittest discover tests"""
import unittest
from datetime import datetime, timedelta

from wazy.store import Store

T0 = datetime(2026, 10, 13, 9, 30)


def bot_texts(store):
    return [m["text"] for m in store.state()["messages"] if m["role"] == "bot"]


class TheLoop(unittest.TestCase):
    def new_store(self, later_minutes=None):
        store = Store(":memory:", later_minutes=later_minutes, clock=lambda: T0)
        self.addCleanup(store.close)
        return store

    def test_commitment_is_confirmed_with_its_time(self):
        store = self.new_store()
        store.handle_text("Send Marta the slides in 30 seconds", now=T0)
        self.assertIn("Got it: “Send Marta the slides”. I'll nudge you at 09:30:30 (in 30 s).", bot_texts(store))
        self.assertEqual(store.state()["commitments"][0]["status"], "scheduled")

    def test_nothing_is_sent_early_and_exactly_one_nudge_is_sent_on_time(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        self.assertEqual(store.run_due(T0 + timedelta(seconds=4)), 0)
        self.assertEqual(store.run_due(T0 + timedelta(seconds=5)), 1)
        self.assertEqual(store.run_due(T0 + timedelta(seconds=6)), 0)       # never twice
        state = store.state()
        self.assertEqual(len(state["outcomes"]), 1)
        self.assertEqual(state["outcomes"][0]["outcome"], "pending")
        self.assertEqual(len(state["open"]), 1)

    def test_done_records_the_outcome_and_response_time(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        cid = store.state()["commitments"][0]["id"]
        self.assertTrue(store.answer_nudge(cid, "done", now=T0 + timedelta(seconds=12)))
        state = store.state()
        self.assertEqual(state["outcomes"][0]["outcome"], "done")
        self.assertEqual(state["outcomes"][0]["seconds_to_respond"], 7)
        self.assertEqual(state["commitments"][0]["status"], "done")
        self.assertEqual(state["stats"]["done_rate"], 100)
        self.assertFalse(store.answer_nudge(cid, "done", now=T0 + timedelta(seconds=20)))  # nothing left open

    def test_later_means_one_more_nudge_next_morning_at_ten(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        cid = store.state()["commitments"][0]["id"]
        store.answer_nudge(cid, "later", now=T0 + timedelta(seconds=9))
        commitment = store.state()["commitments"][0]
        self.assertEqual(commitment["status"], "scheduled")
        self.assertEqual(commitment["nudge_at"], "2026-10-14T10:00:00")

    def test_later_can_be_shortened_for_a_live_demo(self):
        store = self.new_store(later_minutes=1)
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        store.answer_nudge(store.state()["commitments"][0]["id"], "later", now=T0 + timedelta(seconds=10))
        self.assertEqual(store.state()["commitments"][0]["nudge_at"], "2026-10-13T09:31:10")

    def test_typed_done_answers_the_open_nudge(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        store.handle_text("done", now=T0 + timedelta(seconds=8))
        self.assertEqual(store.state()["outcomes"][0]["outcome"], "done")

    def test_stop_ends_every_nudge(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.handle_text("Call mum tomorrow 9am", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        store.handle_text("STOP", now=T0 + timedelta(seconds=6))
        state = store.state()
        self.assertEqual(state["outcomes"][0]["outcome"], "stop")
        self.assertTrue(all(c["status"] == "stopped" for c in state["commitments"]))
        self.assertEqual(store.run_due(T0 + timedelta(days=2)), 0)

    def test_no_answer_within_24_hours_is_recorded_as_no_reply(self):
        store = self.new_store()
        store.handle_text("Pay rent in 5 seconds", now=T0)
        store.run_due(T0 + timedelta(seconds=5))
        store.run_due(T0 + timedelta(hours=24, seconds=5))
        state = store.state()
        self.assertEqual(state["outcomes"][0]["outcome"], "no_reply")
        self.assertEqual(state["commitments"][0]["status"], "no_reply")
        self.assertEqual(state["open"], [])

    def test_missing_time_is_asked_for_then_attached(self):
        store = self.new_store()
        store.handle_text("buy milk", now=T0)
        self.assertIn("When should I nudge you about “Buy milk”?", bot_texts(store)[-1])
        store.handle_text("tonight", now=T0)
        commitment = store.state()["commitments"][0]
        self.assertEqual((commitment["title"], commitment["nudge_at"]), ("Buy milk", "2026-10-13T20:00:00"))

    def test_long_messages_are_cut_to_300_characters(self):
        store = self.new_store()
        store.handle_text("x" * 1000, now=T0)
        user = [m for m in store.state()["messages"] if m["role"] == "user"][0]
        self.assertEqual(len(user["text"]), 300)

    def test_access_code_is_generated_once_and_kept(self):
        store = self.new_store()
        code = store.access_code()
        self.assertGreaterEqual(len(code), 12)
        self.assertEqual(store.access_code(), code)


if __name__ == "__main__":
    unittest.main()
