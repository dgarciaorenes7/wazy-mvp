"""How Wazy reads a commitment. Run: python3 -m unittest discover tests"""
import unittest
from datetime import datetime, timedelta

from wazy.parser import parse_rules

NOW = datetime(2026, 10, 13, 9, 30)  # a Tuesday morning


class ReadsTheTime(unittest.TestCase):
    def check(self, text, title, when):
        got_title, got_when = parse_rules(text, NOW)
        self.assertEqual(got_title, title, text)
        self.assertEqual(got_when, when, text)

    def test_relative_seconds_minutes_hours(self):
        self.check("Send Marta the slides in 30 seconds", "Send Marta the slides", NOW + timedelta(seconds=30))
        self.check("Pay rent in 2 minutes", "Pay rent", NOW + timedelta(minutes=2))
        self.check("check the oven in an hour", "Check the oven", NOW + timedelta(hours=1))

    def test_weekday_without_time_means_ten_oclock(self):
        self.check("Reply to Carlos by Thursday", "Reply to Carlos", datetime(2026, 10, 15, 10, 0))

    def test_same_weekday_later_today_and_next_week(self):
        self.check("email Ana on Tuesday", "Email Ana", datetime(2026, 10, 13, 10, 0))
        self.check("email Ana next Tuesday", "Email Ana", datetime(2026, 10, 20, 10, 0))

    def test_tomorrow_with_am_pm(self):
        self.check("call the bank tomorrow 9am", "Call the bank", datetime(2026, 10, 14, 9, 0))
        self.check("remind me to book the gym tomorrow at 6:30pm", "Book the gym", datetime(2026, 10, 14, 18, 30))

    def test_clock_only_rolls_to_tomorrow_when_passed(self):
        self.check("take the bins out at 8:00", "Take the bins out", datetime(2026, 10, 14, 8, 0))
        self.check("gym at 18:30", "Gym", datetime(2026, 10, 13, 18, 30))

    def test_small_bare_hour_means_afternoon(self):
        self.check("call mum at 3", "Call mum", datetime(2026, 10, 13, 15, 0))

    def test_parts_of_day(self):
        self.check("buy milk tonight", "Buy milk", datetime(2026, 10, 13, 20, 0))

    def test_spanish(self):
        self.check("recuérdame llamar a mamá mañana a las 9", "Llamar a mamá", datetime(2026, 10, 14, 9, 0))
        self.check("pagar el alquiler el viernes", "Pagar el alquiler", datetime(2026, 10, 16, 10, 0))

    def test_no_time_given(self):
        self.check("buy milk", "Buy milk", None)

    def test_numbers_that_are_not_times(self):
        self.check("send 2 reports to Paula", "Send 2 reports to Paula", None)

    def test_time_only_reply_has_no_title(self):
        self.check("tomorrow 9am", "", datetime(2026, 10, 14, 9, 0))


if __name__ == "__main__":
    unittest.main()
