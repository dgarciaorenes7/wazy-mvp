"""Tests for the reminder clauses in wazy/timeparse.py (section 5, added after the Session 8 error analysis).

A message can give the time of the thing AND when to be nudged about it. Wazy nudges once, at the nudge time.
The red line from Class 7 is tested here too: a nudge after the thing itself (or its deadline) is never set.
"Now" is Monday 9 Nov 2026 at 09:00 in Madrid unless a row says otherwise. 2026-11-11 is a Wednesday,
2026-11-13 a Friday, 2026-11-17 a Tuesday, 2026-11-18 a Wednesday, 2027-04-30 a Friday.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from zoneinfo import ZoneInfo

from wazy.app import App
from wazy.timeparse import parse_time
from wazy.title import make_title

MADRID = ZoneInfo("Europe/Madrid")
MONDAY = datetime(2026, 11, 9, 9, 0, tzinfo=MADRID)


def read(text: str, now: datetime = MONDAY):
    return parse_time(text, now, MADRID)


def local(result) -> str:
    return result.when.strftime("%Y-%m-%d %H:%M") if result.when else ""


class ReminderClauseTimes(unittest.TestCase):
    """The nudge time comes from the reminder words, measured from the time of the thing."""

    ROWS = [
        # message, expected status, expected nudge (local), expected title
        ("Call with Max on Wednesday at 2:30pm with a reminder half an hour before",
         "ok", "2026-11-11 14:00", "Call with Max"),
        ("Meeting tomorrow at 20:30 with a reminder half an hour before", "ok", "2026-11-10 20:00", "Meeting"),
        ("Accounting exam on 30 April at 10:45, remind me the day before at 5pm",
         "ok", "2027-04-29 17:00", "Accounting exam"),
        ("Computing homework on Friday at 8am with a reminder the day before", "ok", "2026-11-12 08:00",
         "Computing homework"),
        ("Passport appointment on 23 July at 11:36. Remind me the day before", "ok", "2027-07-22 11:36",
         "Passport appointment"),
        ("Weekly class tomorrow at 10:10 with a reminder at that time", "ok", "2026-11-10 10:10", "Weekly class"),
        ("Programming quiz next Friday, remind me tomorrow at 9 am", "ok", "2026-11-10 09:00", "Programming quiz"),
        ("Dentist on Thursday at 16:00, nudge me 2 hours before", "ok", "2026-11-12 14:00", "Dentist"),
        ("Flight on Friday at 7am, remind me the night before", "ok", "2026-11-12 21:00", "Flight"),
        ("Remind me half an hour before my call with Max on Wednesday at 2:30pm", "ok", "2026-11-11 14:00",
         "My call with Max"),
        ("Examen el viernes a las 10, avísame el día antes", "ok", "2026-11-12 10:00", "Examen"),
        ("Reunión mañana a las 18:00 con un recordatorio media hora antes", "ok", "2026-11-10 17:30", "Reunión"),
    ]

    def test_rows(self):
        for text, status, when, title in self.ROWS:
            with self.subTest(text=text):
                result = read(text)
                self.assertEqual(result.status, status)
                self.assertEqual(local(result), when)
                self.assertTrue(result.stated)
                lang = "es" if any(w in text for w in ("avísame", "recordatorio")) else "en"
                self.assertEqual(make_title(text, result.spans, lang), title)

    def test_a_day_only_deadline_keeps_its_flag(self):
        result = read("Pay the rent by Friday, remind me on Friday at 18:00")
        self.assertEqual((result.status, local(result), result.deadline), ("ok", "2026-11-13 18:00", True))


class RedLine(unittest.TestCase):
    """Class 7: never nudge after the thing itself, or after its deadline."""

    def test_nudge_after_the_deadline_is_refused(self):
        result = read("Submit the project files by Tuesday 17 Nov at 23:59, nudge me about it on Wednesday 18")
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.detail_code, "after_event")
        self.assertEqual(result.detail, "on Wednesday 18")
        self.assertEqual(result.hint, "Tuesday 17 Nov at 23:59")

    def test_nudge_after_a_day_only_deadline_is_refused(self):
        result = read("Pay the rent by Friday, remind me on Saturday")
        self.assertEqual((result.status, result.detail_code), ("invalid", "after_event"))

    def test_nudge_after_the_event_is_refused(self):
        result = read("Call with Max on Wednesday at 14:30, remind me on Thursday at 9")
        self.assertEqual((result.status, result.detail_code), ("invalid", "after_event"))

    def test_the_reply_says_why_and_nothing_is_scheduled(self):
        app = App(":memory:", clock=lambda: MONDAY)
        replies = [r for r in app.handle("web:t", "Submit the project files by Tuesday 17 Nov at 23:59, nudge me "
                                                  "about it on Wednesday 18") if r.kind != "welcome"]
        self.assertEqual(replies[-1].kind, "invalid_time")
        self.assertIn("after the thing itself", replies[-1].text)
        self.assertIn("When should I nudge you", replies[-1].text)
        self.assertEqual(app.state("web:t")["commitments"], [])
        follow = app.handle("web:t", "Tuesday 17 Nov at 18:00")
        self.assertEqual(follow[-1].kind, "confirm")
        self.assertEqual(app.state("web:t")["commitments"][0]["nudge_at_local"][:16], "2026-11-17T18:00")
        app.close()

    def test_spanish_reply(self):
        app = App(":memory:", clock=lambda: MONDAY)
        replies = app.handle("web:t", "Entregar el trabajo antes del martes 17 de noviembre a las 23:59, "
                                      "avísame el miércoles 18")
        self.assertEqual(replies[-1].kind, "invalid_time")
        self.assertIn("después de la propia tarea", replies[-1].text)
        app.close()


class OtherProblems(unittest.TestCase):

    def test_two_nudge_times_for_one_thing(self):
        result = read("IR meeting tomorrow at 11 am, remind me tonight at 23:59 and tomorrow morning at 10:30 am")
        self.assertEqual(result.status, "recurring")

    def test_offset_lands_in_the_past(self):
        now = datetime(2026, 11, 9, 13, 50, tzinfo=MADRID)
        result = read("Meeting today at 14:00 with a reminder half an hour before", now)
        self.assertEqual(result.status, "past")

    def test_no_time_for_the_thing_asks_when(self):
        self.assertEqual(read("Driving exam, remind me the day before").status, "none")


class UnchangedMessages(unittest.TestCase):
    """The usual messages read exactly as before: the reminder words only matter with a time for the thing."""

    ROWS = [
        ("Remind me tomorrow at 8:39 to tell Kelly to Bizum", "2026-11-10 08:39"),
        ("Remind me to call Jimena at 19:30", "2026-11-09 19:30"),
        ("Remind me in half an hour to text max", "2026-11-09 09:30"),
        ("Tomorrow at 9, remind me to call mum", "2026-11-10 09:00"),
        ("recuérdame el jueves a primera hora mandarle el deck a Samuele", "2026-11-12 09:00"),
        ("Set a reminder for the dentist on Friday at 5pm", "2026-11-13 17:00"),
    ]

    def test_rows(self):
        for text, when in self.ROWS:
            with self.subTest(text=text):
                result = read(text)
                self.assertEqual((result.status, local(result)), ("ok", when))


class WeekdayAndNumber(unittest.TestCase):
    """'Wednesday 18' in English is a date when the calendar agrees and 18 cannot be a 12-hour clock hour."""

    def test_rows(self):
        self.assertEqual(local(read("call mum on Wednesday 18")), "2026-11-18 10:00")
        self.assertEqual(local(read("call mum Friday 5")), "2026-11-13 10:00")          # unchanged
        self.assertEqual(local(read("call mum on Thursday 18")), "2026-11-12 10:00")    # 18 Nov is a Wednesday


if __name__ == "__main__":
    unittest.main()
