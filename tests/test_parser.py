"""How Wazy reads a commitment. Run: python3 -m unittest discover tests"""
import unittest
from datetime import datetime, timedelta

from wazy.parser import not_a_commitment, parse_rules

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

    def test_days_and_weeks_ahead_mean_ten_oclock_that_day(self):
        self.check("send the report in 2 days", "Send the report", datetime(2026, 10, 15, 10, 0))
        self.check("call mum in a week", "Call mum", datetime(2026, 10, 20, 10, 0))
        self.check("pagar la matrícula en 3 días", "Pagar la matrícula", datetime(2026, 10, 16, 10, 0))

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

    # Added 1 Nov 2026 after replaying 84 real messages sent to the old Wazy (Apr-Sep 2026):
    # these were the phrasings the rules missed.
    def test_numbers_written_as_words_and_half_an_hour(self):
        self.check("remind me in one hour to send the class reminder", "Send the class reminder", NOW + timedelta(hours=1))
        self.check("remind me in half an hour to text Max", "Text Max", NOW + timedelta(minutes=30))
        self.check("llamar al banco en dos horas", "Llamar al banco", NOW + timedelta(hours=2))
        self.check("recuérdame en media hora sacar la ropa", "Sacar la ropa", NOW + timedelta(minutes=30))

    def test_written_out_dates(self):
        self.check("accounting exam on 30 April at 10:45", "Accounting exam", datetime(2027, 4, 30, 10, 45))
        self.check("book the passport appointment on the 23 October at 11:36", "Book the passport appointment", datetime(2026, 10, 23, 11, 36))
        self.check("hand in the essay on October 16", "Hand in the essay", datetime(2026, 10, 16, 10, 0))
        self.check("send the deck 25th october", "Send the deck", datetime(2026, 10, 25, 10, 0))
        self.check("pagar la matrícula el 3 de noviembre", "Pagar la matrícula", datetime(2026, 11, 3, 10, 0))

    def test_day_of_month_alone_means_the_next_time_it_comes_round(self):
        self.check("driving exam on the 27th", "Driving exam", datetime(2026, 10, 27, 10, 0))
        self.check("pay the gym on the 2nd", "Pay the gym", datetime(2026, 11, 2, 10, 0))

    def test_questions_and_edits_are_not_new_commitments(self):
        for text in ("What do I have next week", "change it to april 26", "Change the date to today",
                     "Add that same event on the 23 and the 24", "can you move my meeting?", "¿qué tengo mañana?"):
            self.assertTrue(not_a_commitment(text), text)
        for text in ("Change the oil tomorrow", "Call Ana on Tuesday", "Send Marta the slides by Thursday",
                     "cancel the gym membership on Friday", "remind me to ask Leo what time the match is"):
            self.assertFalse(not_a_commitment(text), text)

    def test_impossible_date_asks_instead_of_guessing(self):
        self.check("call Ana on 31 April", "Call Ana on 31 April", None)


if __name__ == "__main__":
    unittest.main()
