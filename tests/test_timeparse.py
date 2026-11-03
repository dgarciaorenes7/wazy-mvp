"""Tests for wazy/timeparse.py (SPEC section 3).

Most tests are tables: (message, when it was said, expected status, expected local time, flags).
Times are wall-clock times in the person's zone ("2026-11-04 17:00"); flags say what else must be true for
an "ok" result: "s" = stated by the person, "d" = a deadline. Every expected date was checked against a printed
calendar: 2026-11-03 is a Tuesday, 2026-11-06 a Friday, 2027-03-28 and 2026-10-25 are the Madrid DST days.
"""
from __future__ import annotations

import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from wazy.timeparse import DEFAULT_HOUR, normalise, parse_time

MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")
T = "2026-11-03 10:00"          # a Tuesday morning, the default "now" of many rows


def said_at(text: str, now: str, zone=MADRID, default_hour: int = DEFAULT_HOUR):
    """parse_time with `now` written as local wall time in `zone`."""
    moment = datetime.strptime(now, "%Y-%m-%d %H:%M:%S" if now.count(":") == 2 else "%Y-%m-%d %H:%M")
    return parse_time(text, moment.replace(tzinfo=zone), zone, default_hour)


def left_after_spans(text: str, spans) -> str:
    """What the title module would start from: the text with every span removed, spaces collapsed."""
    kept = [char for index, char in enumerate(text) if not any(a <= index < b for a, b in spans)]
    return " ".join("".join(kept).split())


class TableTest(unittest.TestCase):
    """Runs rows of (text, now, status, when, flags) in one zone."""

    zone = MADRID

    def run_rows(self, rows):
        for text, now, status, when, flags in rows:
            with self.subTest(text=text, now=now):
                result = said_at(text, now, self.zone)
                self.assertEqual(result.status, status, f"detail={result.detail!r} when={result.when}")
                if status != "ok":
                    self.assertIsNone(result.when)
                    continue
                expected = when if when.count(":") == 2 else when + ":00"
                self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M:%S"), expected)
                self.assertIs(result.when.tzinfo, self.zone)
                self.assertEqual(result.stated, "s" in flags, "stated")
                self.assertEqual(result.deadline, "d" in flags, "deadline")
                self.check_spans(text, result.spans)

    def check_spans(self, text, spans):
        last_end = -1
        for start, end in spans:
            self.assertTrue(0 <= start < end <= len(text), spans)
            self.assertGreater(start, last_end, "spans are sorted and do not touch")
            last_end = end


# ---- SPEC 3.1 clock times ------------------------------------------------------------------------------

CLOCK_FORMS = [
    ("Call Ana at 20:00", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 20.00", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 20h", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 20h30", T, "ok", "2026-11-03 20:30", "s"),
    ("Llamar a Ana a las 20h", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 8pm", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 8 pm", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 8:30pm", T, "ok", "2026-11-03 20:30", "s"),
    ("Call Ana at 8.30 p.m.", T, "ok", "2026-11-03 20:30", "s"),
    ("CALL ANA AT 8 P.M.", T, "ok", "2026-11-03 20:00", "s"),
    ("Call Ana at 9am", T, "ok", "2026-11-04 09:00", "s"),
    ("Take the bins out at 12am", T, "ok", "2026-11-04 00:00", "s"),
    ("Lunch with Sara at 12pm", T, "ok", "2026-11-03 12:00", "s"),
    ("Call the bank at noon", T, "ok", "2026-11-03 12:00", "s"),
    ("Call the bank at noon", "2026-11-16 13:05", "ok", "2026-11-17 12:00", "s"),
    ("Eat something at midday", "2026-11-17 08:00", "ok", "2026-11-17 12:00", "s"),
    ("recuérdame comer a mediodía", T, "ok", "2026-11-03 12:00", "s"),
    ("reservar mesa mañana a mediodía", "2026-12-31 16:45", "ok", "2027-01-01 12:00", "s"),
    ("Submit the form by midnight", "2026-11-03 21:00", "ok", "2026-11-04 00:00", "sd"),
    ("sacar la basura a medianoche", "2026-11-03 21:30", "ok", "2026-11-04 00:00", "s"),
    ("Back up the laptop at 24:00", "2026-11-30 18:00", "ok", "2026-12-01 00:00", "s"),
    ("Send the New Year message at 12am", "2026-12-31 20:00", "ok", "2027-01-01 00:00", "s"),
    ("Call Ana at 5 o'clock", T, "ok", "2026-11-03 17:00", "s"),
    ("Call Ana at 5 o’clock", T, "ok", "2026-11-03 17:00", "s"),
    ("Call Ana at nine", T, "ok", "2026-11-03 21:00", "s"),
    ("llamar a Ana a las nueve", T, "ok", "2026-11-03 21:00", "s"),
    ("comer con Ana mañana a la una", T, "ok", "2026-11-04 13:00", "s"),
    ("Send it at 08:30", "2026-12-02 14:00", "ok", "2026-12-03 08:30", "s"),
    ("Water the plants at 20:00", "2026-11-03 21:00", "ok", "2026-11-04 20:00", "s"),
    ("Call the bank at 9am", "2026-11-30 11:00", "ok", "2026-12-01 09:00", "s"),
    ("Take the bins out at 23:30", "2026-11-13 14:00", "ok", "2026-11-13 23:30", "s"),
    ("Call at 0:30", T, "ok", "2026-11-04 00:30", "s"),
]

HALF_PAST_AND_SPANISH_MINUTES = [
    ("Call Ana at half past 9", T, "ok", "2026-11-03 21:30", "s"),
    ("Call Ana at quarter past 9", "2026-11-03 08:00", "ok", "2026-11-03 09:15", "s"),
    ("Lock the office at quarter to 9", "2026-11-19 09:00", "ok", "2026-11-19 20:45", "s"),
    ("Team call on Friday at quarter past 3", T, "ok", "2026-11-06 15:15", "s"),
    ("Feed the cat at half past 6", "2026-11-20 00:30", "ok", "2026-11-20 18:30", "s"),
    ("Check the oven at half past 11", "2026-11-20 12:00", "ok", "2026-11-21 11:30", "s"),
    ("Collect the dry cleaning tomorrow at half past 7", "2026-11-23 09:00", "ok", "2026-11-24 19:30", "s"),
    ("Call at twenty past 4", T, "ok", "2026-11-03 16:20", "s"),
    ("tender la ropa a las 9 y media", T, "ok", "2026-11-03 21:30", "s"),
    ("sacar al perro a las 9 y media", "2026-11-17 23:30", "ok", "2026-11-18 09:30", "s"),
    ("llamar al taller mañana a las 9 y cuarto", "2027-01-12 07:05", "ok", "2027-01-13 09:15", "s"),
    ("salir de casa mañana a las 9 menos cuarto", "2026-11-03 22:40", "ok", "2026-11-04 08:45", "s"),
    ("entrar a la reunión el jueves a las 9 y 20", "2026-11-03 00:05", "ok", "2026-11-05 09:20", "s"),
    ("mandar las fotos el viernes a las 5 y media", "2026-11-03 15:00", "ok", "2026-11-06 17:30", "s"),
    ("llamar a las 6 y diez", T, "ok", "2026-11-03 18:10", "s"),
]

PARTS_FIX_AM_PM = [
    ("recoger el traje a las 9 de la mañana", "2026-11-03 07:00", "ok", "2026-11-03 09:00", "s"),
    ("recoger el traje a las 5 de la tarde", "2026-11-11 11:05", "ok", "2026-11-11 17:00", "s"),
    ("recoger el traje a las 5 de la tarde", "2026-11-11 18:30", "ok", "2026-11-12 17:00", "s"),
    ("cenar con Laura el sábado a las 9 de la noche", "2026-11-03 13:20", "ok", "2026-11-07 21:00", "s"),
    ("estar en el aeropuerto mañana a las 7 de la mañana", "2026-11-13 20:05", "ok", "2026-11-14 07:00", "s"),
    ("tomar la pastilla mañana a las 9 de la mañana", "2026-11-03 21:15", "ok", "2026-11-04 09:00", "s"),
    ("comer a la 1 del mediodía", T, "ok", "2026-11-03 13:00", "s"),
    ("salir a las 3 de la madrugada", T, "ok", "2026-11-04 03:00", "s"),
    ("Go for a run at 9 in the morning", "2026-11-04 10:00", "ok", "2026-11-05 09:00", "s"),
    ("Swim on Friday at 7 in the morning", "2026-11-03 12:00", "ok", "2026-11-06 07:00", "s"),
    ("Call Dad at 9 tonight", "2026-11-04 08:00", "ok", "2026-11-04 21:00", "s"),
    ("Call dad 9 tonight", T, "ok", "2026-11-03 21:00", "s"),
    ("Book a table for 4 at 7 in the evening", "2026-11-04 10:00", "ok", "2026-11-04 19:00", "s"),
    ("Drinks with Leo on Saturday at 8 in the evening", "2026-11-04 10:00", "ok", "2026-11-07 20:00", "s"),
    ("Pick up the parcel at 3 this afternoon", "2026-11-05 11:00", "ok", "2026-11-05 15:00", "s"),
    ("Call at 11 at night", T, "ok", "2026-11-03 23:00", "s"),
]

BARE_HOUR = [
    ("Remind me to call mum at 9", "2026-11-03 10:00", "ok", "2026-11-03 21:00", "s"),
    ("Remind me to call mum at 9", "2026-11-03 08:55", "ok", "2026-11-03 09:00", "s"),
    ("Remind me to call mum at 9", "2026-11-03 23:30", "ok", "2026-11-04 09:00", "s"),
    ("Call the vet at 5", "2026-11-21 00:30", "ok", "2026-11-21 17:00", "s"),
    ("Message Bea at 11", "2026-11-22 12:00", "ok", "2026-11-23 11:00", "s"),
    ("Email Jon at 2", "2026-11-23 23:30", "ok", "2026-11-24 14:00", "s"),
    ("Text Marc at 9", "2026-12-31 23:30", "ok", "2027-01-01 09:00", "s"),
    ("Take my vitamins at 9", "2026-11-04 00:20", "ok", "2026-11-04 09:00", "s"),
    ("Call grandma at 9ish", "2026-11-27 18:00", "ok", "2026-11-27 21:00", "s"),
    ("Call grandma 9ish", "2026-11-27 18:00", "ok", "2026-11-27 21:00", "s"),
    ("Pick up the kids @ 5", "2026-11-27 10:00", "ok", "2026-11-27 17:00", "s"),
    ("coger el tren a las 7", "2026-11-18 06:30", "ok", "2026-11-18 07:00", "s"),
    ("llamar a Pablo a las 9", "2026-11-16 10:00", "ok", "2026-11-16 21:00", "s"),
    ("mandar el formulario a las 11", "2026-11-18 22:30", "ok", "2026-11-19 11:00", "s"),
    ("llamar a Pedro a las 11", "2026-11-03 12:00", "ok", "2026-11-04 11:00", "s"),
    ("llamar a la tienda a la 1", "2026-11-03 09:30", "ok", "2026-11-03 13:00", "s"),
    ("Call mum at 12", T, "ok", "2026-11-03 12:00", "s"),
    ("Call mum at 12", "2026-11-03 13:00", "ok", "2026-11-04 12:00", "s"),
    ("Call mum at 10:30", "2026-11-03 11:00", "ok", "2026-11-03 22:30", "s"),
    ("Call mum at 7", "2026-11-03 06:00", "ok", "2026-11-03 07:00", "s"),
    # With a day: 1 to 7 is the afternoon, 8 to 11 the morning, 12 is noon.
    ("Go to the gym tomorrow at 7", "2026-11-25 08:00", "ok", "2026-11-26 19:00", "s"),
    ("Meet 2 friends on Thursday at 8", "2026-11-03 18:00", "ok", "2026-11-05 08:00", "s"),
    ("Call Pepe mañana a las 6", "2027-01-11 10:00", "ok", "2027-01-12 18:00", "s"),
    ("Water the plants tomorrow at 8", "2026-11-05 13:05", "ok", "2026-11-06 08:00", "s"),
    ("Lunch tomorrow at 12", T, "ok", "2026-11-04 12:00", "s"),
    ("comer con Ana mañana a la 1", "2026-11-05 09:00", "ok", "2026-11-06 13:00", "s"),
    ("tengo que llamar a Pedro mañana a las 5", T, "ok", "2026-11-04 17:00", "s"),
    ("hacer el call con el equipo mañana a las 11", "2027-01-18 16:30", "ok", "2027-01-19 11:00", "s"),
]

INVALID_CLOCKS = [
    # (text, now, detail, detail_code)
    ("Call mum at 25:00", "25:00", "hour_out_of_range"),
    ("Pay the rent at 9:75", "9:75", "minute_out_of_range"),
    ("Email Laura at 13pm", "13pm", "ampm_conflict"),
    ("Email Laura at 23pm", "23pm", "ampm_conflict"),
    ("Call the bank at 0am", "0am", "ampm_conflict"),
    ("Call the bank at 0pm", "0pm", "ampm_conflict"),
    ("Call at 25pm", "25pm", "hour_out_of_range"),
    ("pagar el alquiler a las 26", "a las 26", "hour_out_of_range"),
    ("Take the bins out at 24:30", "24:30", "hour_out_of_range"),
    ("Take the bins out at 25:15", "25:15", "hour_out_of_range"),
    ("Call Dad at 99", "at 99", "hour_out_of_range"),
    ("Call at 9:60", "9:60", "minute_out_of_range"),
    ("comprar pan ayer a las 25:00", "25:00", "hour_out_of_range"),
    ("Call Dad last week at 9:75", "9:75", "minute_out_of_range"),
    ("recuérdame llamar a Ana a las 9 menos 70", "a las 9 menos 70", "minute_out_of_range"),   # SPEC 3.6
    ("Call at 70 to 6", "70 to 6", "minute_out_of_range"),
]

INVALID_DATES = [
    # (text, now, detail, detail_code, hint)
    ("Pay the council tax on 31 February", T, "31 February", "no_such_date", ""),
    ("Recuérdame pagar la luz el 30/02", T, "30/02", "no_such_date", ""),
    ("Book flights to Lisbon on 32 Oct", "2026-09-15 08:00", "32 Oct", "no_such_date", ""),
    ("Submit the timesheet on 13/13", T, "13/13", "no_such_date", ""),
    ("Pay the electricity bill on 31 Nov", T, "31 Nov", "no_such_date", ""),
    ("Pay on 29 Feb 2027", T, "29 Feb 2027", "no_such_date", ""),
    ("Send the slides to Marta on Monday 5 Nov", T, "Monday 5 Nov", "weekday_mismatch", "Thursday"),
    ("Book a table on Wednesday 13/11", "2026-11-09 07:45", "Wednesday 13/11", "weekday_mismatch", "Friday"),
    ("Llamar a la gestoría el lunes 5 de noviembre", T, "lunes 5 de noviembre", "weekday_mismatch",
     "Thursday"),
    ("Llamar a Ana el lunes 20", T, "lunes 20", "weekday_mismatch", "Friday"),
    ("Meet on Sunday, 4 November 2026", T, "Sunday, 4 November 2026", "weekday_mismatch", "Wednesday"),
    ("examen el martes día 16", T, "martes día 16", "weekday_mismatch", "Monday"),          # SPEC 3.6
    # A gone date with the wrong weekday: invalid comes before past (SPEC 3.5 priority).
    ("buy milk on Monday 5 Nov 2026", "2027-01-10 10:00", "Monday 5 Nov 2026", "weekday_mismatch", "Thursday"),
    # A year no calendar has is never silently dropped: it is in the detail (and the span).
    ("cita el 1 de enero de 99999", T, "1 de enero de 99999", "no_such_date", ""),
    ("pagar el 1/1/99999", T, "1/1/99999", "no_such_date", ""),
]


class ClockTimes(TableTest):
    def test_clock_forms(self):
        self.run_rows(CLOCK_FORMS)

    def test_half_past_and_spanish_minutes(self):
        self.run_rows(HALF_PAST_AND_SPANISH_MINUTES)

    def test_parts_of_day_fix_am_pm(self):
        self.run_rows(PARTS_FIX_AM_PM)

    def test_bare_hour_rule(self):
        self.run_rows(BARE_HOUR)

    def test_invalid_clock_times(self):
        for text, detail, code in INVALID_CLOCKS:
            with self.subTest(text=text):
                result = said_at(text, T)
                self.assertEqual((result.status, result.detail, result.detail_code), ("invalid", detail, code))
                self.assertIsNone(result.when)

    def test_invalid_dates(self):
        for text, now, detail, code, hint in INVALID_DATES:
            with self.subTest(text=text):
                result = said_at(text, now)
                self.assertEqual((result.status, result.detail, result.detail_code, result.hint),
                                 ("invalid", detail, code, hint))


# ---- SPEC 3.2 days and dates ----------------------------------------------------------------------------

DAYS = [
    ("Remind me to call mum tomorrow", "2026-11-03 08:55", "ok", "2026-11-04 10:00", ""),
    ("Buy milk tmrw", "2026-11-03 18:40", "ok", "2026-11-04 10:00", ""),
    ("renew the parking permit tmr", "2026-11-03 23:40", "ok", "2026-11-04 10:00", ""),
    ("Return the library books tomorrow", "2026-11-04 00:30", "ok", "2026-11-05 10:00", ""),
    ("recuérdame comprar flores mañana", "2026-11-03 13:00", "ok", "2026-11-04 10:00", ""),
    ("recuerdame comprar flores manana", "2026-11-03 13:00", "ok", "2026-11-04 10:00", ""),
    ("regar las plantas mañana", T, "ok", "2026-11-04 10:00", ""),
    ("Mañana, recoger el paquete gracias", T, "ok", "2026-11-04 10:00", ""),
    ("recuérdame mañana llamar a mamá", T, "ok", "2026-11-04 10:00", ""),
    ("Pay the electricity bill the day after tomorrow", "2026-11-03 13:20", "ok", "2026-11-05 10:00", ""),
    ("ir a correos con el paquete pasado mañana", "2026-12-30 18:00", "ok", "2027-01-01 10:00", ""),
    ("hola, recuérdame escribir a Lucía pasado mañana.", T, "ok", "2026-11-05 10:00", ""),
    ("Call the notary day after tomorrow at 6pm", "2026-12-30 19:20", "ok", "2027-01-01 18:00", "s"),
    ("Replace the smoke alarm battery tomorrow", "2026-10-24 22:00", "ok", "2026-10-25 10:00", ""),
    ("tengo que llamar al casero mañana", "2026-10-25 00:40", "ok", "2026-10-26 10:00", ""),
    ("Call mum tomorrow", "2028-02-28 12:00", "ok", "2028-02-29 10:00", ""),
    ("Call mum tomorrow", "2028-02-29 12:00", "ok", "2028-03-01 10:00", ""),
    ("Call mum tomorrow", "2026-12-31 23:30", "ok", "2027-01-01 10:00", ""),
]

WEEKDAYS = [
    ("Meet Jorge on Friday at 3", "2026-11-03 09:30", "ok", "2026-11-06 15:00", "s"),
    ("Submit the expense report on Thursday", "2026-11-03 11:45", "ok", "2026-11-05 10:00", ""),
    ("Send the contract fri", "2026-11-03 20:05", "ok", "2026-11-06 10:00", ""),
    ("Book the dentist on mon at 11", "2026-11-04 12:10", "ok", "2026-11-09 11:00", "s"),
    ("Finish the essay this Friday", "2026-11-03 17:25", "ok", "2026-11-06 10:00", ""),
    ("Call the insurance company next Friday", "2026-11-03 12:35", "ok", "2026-11-06 10:00", ""),
    ("Review the budget next Friday", "2026-11-06 08:30", "ok", "2026-11-13 10:00", ""),
    ("Review the budget on Friday", "2026-11-06 08:30", "ok", "2026-11-06 10:00", ""),
    ("Collect the dress this Friday at 6pm", "2026-11-13 09:00", "ok", "2026-11-13 18:00", "s"),
    ("Call the vet Tuesday at 18:00", "2026-11-10 09:15", "ok", "2026-11-10 18:00", "s"),
    ("Send the sales report Tuesday at 9am", "2026-11-10 15:30", "ok", "2026-11-17 09:00", "s"),
    ("Renew the gym membership on Wednesday", "2026-11-04 07:45", "ok", "2026-11-04 10:00", ""),
    ("Call the accountant on Wednesday", "2026-11-04 16:20", "ok", "2026-11-11 10:00", ""),
    ("Call the landlord on Friday at 9", "2026-11-06 10:00", "ok", "2026-11-13 09:00", "s"),
    ("Print the boarding passes Friday morning at 9", "2026-11-03 18:20", "ok", "2026-11-06 09:00", "s"),
    ("recuérdame pagar la luz el miércoles", "2026-11-03 20:00", "ok", "2026-11-04 10:00", ""),
    ("recuerdame pagar la luz el miercoles", "2026-11-05 09:00", "ok", "2026-11-11 10:00", ""),
    ("recuérdame llamar a la abuela el próximo viernes", "2026-11-03 17:25", "ok", "2026-11-06 10:00", ""),
    ("recuérdame llamar a la abuela el próximo viernes", "2026-11-06 12:00", "ok", "2026-11-13 10:00", ""),
    ("recuérdame enviar la factura el viernes que viene", "2026-11-04 19:45", "ok", "2026-11-06 10:00", ""),
    ("recuérdame ir a la peluquería el martes a las 6", "2026-11-03 12:00", "ok", "2026-11-03 18:00", "s"),
    ("recuérdame ir a la peluquería el martes a las 6", "2026-11-03 19:00", "ok", "2026-11-10 18:00", "s"),
    ("recuerdame lavar el coche el sabado a las 11", "2026-11-07 08:00", "ok", "2026-11-07 11:00", "s"),
    ("RECUERDAME COMPRAR EL REGALO DE MARTA EL SABADO A LAS 5", "2026-11-03 17:45", "ok",
     "2026-11-07 17:00", "s"),
    ("tengo que ir a la tutoria el miercoles a las 5", "2027-03-04 14:00", "ok", "2027-03-10 17:00", "s"),
    ("Fix the bike on sat", T, "ok", "2026-11-07 10:00", ""),
    ("Call Dad on sun", T, "ok", "2026-11-08 10:00", ""),
    ("I sat down with Ana tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("Call on Friday the 13th", T, "ok", "2026-11-13 10:00", ""),
    ("Llamar a Ana el viernes 20", T, "ok", "2026-11-20 10:00", ""),
]

WEEKS_AND_MONTHS = [
    ("Clean the bathroom this weekend", T, "ok", "2026-11-07 10:00", ""),
    ("Fix the bike this weekend", "2026-11-07 08:20", "ok", "2026-11-07 10:00", ""),
    ("Call Dad this weekend", "2026-11-08 09:05", "ok", "2026-11-08 10:00", ""),
    ("Call Dad this weekend", "2026-11-08 15:00", "ok", "2026-11-08 16:00", ""),
    ("Call Dad next weekend", "2026-11-07 08:20", "ok", "2026-11-14 10:00", ""),
    ("necesito ir a la farmacia el finde", "2026-11-04 18:30", "ok", "2026-11-07 10:00", ""),
    ("tengo que limpiar el piso este fin de semana", "2026-11-12 22:10", "ok", "2026-11-14 10:00", ""),
    ("Book the car service next week", "2026-11-03 15:05", "ok", "2026-11-09 10:00", ""),
    ("Draft the new budget next week", "2026-12-31 18:30", "ok", "2027-01-04 10:00", ""),
    ("recuérdame llamar al banco la semana que viene", "2026-12-30 11:00", "ok", "2027-01-04 10:00", ""),
    ("pedir cita en el médico la próxima semana", "2027-03-03 07:50", "ok", "2027-03-08 10:00", ""),
    ("Call mum next week", "2028-02-29 09:00", "ok", "2028-03-06 10:00", ""),
    ("Pay the rent by the end of the month", "2026-11-03 22:10", "ok", "2026-11-30 10:00", "d"),
    ("Pay the rent by the end of the month", "2026-11-30 15:00", "ok", "2026-11-30 16:00", "d"),
    ("Send the sales figures at the end of the month", "2027-02-03 12:40", "ok", "2027-02-28 10:00", ""),
    ("Send the sales figures at the end of the month", "2028-02-10 12:00", "ok", "2028-02-29 10:00", ""),
    ("recuérdame pagar el alquiler a fin de mes", "2027-02-10 00:40", "ok", "2027-02-28 10:00", ""),
    ("recuérdame cerrar las cuentas antes de fin de mes", "2026-12-15 13:30", "ok", "2026-12-31 10:00", "d"),
]

DATES = [
    ("Remind me to pay the rent on 4 Nov", "2026-11-03 08:15", "ok", "2026-11-04 10:00", ""),
    ("Call the dentist Nov 12", "2027-02-14 13:40", "ok", "2027-11-12 10:00", ""),
    ("Book the venue for the 4th of November", "2026-11-20 19:05", "ok", "2027-11-04 10:00", ""),
    ("Send the grant application November 4th", "2026-10-24 23:10", "ok", "2026-11-04 10:00", ""),
    ("Recuérdame llamar a Pedro el 4 de noviembre", "2026-11-02 22:10", "ok", "2026-11-04 10:00", ""),
    ("Comprar el regalo de Marta 4 de nov", "2026-11-03 00:20", "ok", "2026-11-04 10:00", ""),
    ("Pick up the suit from the tailor on 4/11", "2026-11-03 18:45", "ok", "2026-11-04 10:00", ""),
    ("Renew the car insurance on 04/11", "2027-03-27 12:00", "ok", "2027-11-04 10:00", ""),
    ("Send the invoices to Marc 4/11/2026", "2026-11-03 10:30", "ok", "2026-11-04 10:00", ""),
    ("Collect the passport 12-11-26", "2026-11-03 21:50", "ok", "2026-11-12 10:00", ""),
    ("Submit the expense report 2026-11-04", "2026-11-03 06:05", "ok", "2026-11-04 10:00", ""),
    ("Clean the fridge on the 21st", "2026-11-03 12:05", "ok", "2026-11-21 10:00", ""),
    ("Pay the internet bill on the 4th", "2026-12-20 23:40", "ok", "2027-01-04 10:00", ""),
    ("Pagar el seguro del coche el día 4", "2026-11-30 00:15", "ok", "2026-12-04 10:00", ""),
    ("Pay the bill on the 31st", T, "ok", "2026-12-31 10:00", ""),
    ("Pagar el gimnasio el día 30", "2028-02-15 10:00", "ok", "2028-03-30 10:00", ""),
    ("Pay the bill on the 29th", "2028-02-20 10:00", "ok", "2028-02-29 10:00", ""),
    ("Pay the gym membership on 2 Nov", "2026-11-03 12:00", "ok", "2027-11-02 10:00", ""),
    ("Send the thank-you cards 30 Dec", "2026-12-31 23:30", "ok", "2027-12-30 10:00", ""),
    ("Send the cards on 1 Jan", "2026-12-31 23:30", "ok", "2027-01-01 10:00", ""),
    ("Pay rent on 4 Nov", "2026-11-04 09:00", "ok", "2026-11-04 10:00", ""),
    ("Pay rent on 4 Nov", "2026-11-04 11:00", "ok", "2026-11-04 12:00", ""),
    ("Birthday card on 29 Feb", "2027-11-03 10:00", "ok", "2028-02-29 10:00", ""),
    ("Dinner with Carla 4 Nov at 21:00", "2026-11-03 09:00", "ok", "2026-11-04 21:00", "s"),
    ("Call the bank on 12/11 at 3", "2026-11-03 16:20", "ok", "2026-11-12 15:00", "s"),
    ("Meet Jordi on Nov 10 at 9", "2026-11-04 19:40", "ok", "2026-11-10 09:00", "s"),
    ("Llevar el coche al taller el 20 de noviembre a las 5", "2026-11-03 11:10", "ok", "2026-11-20 17:00", "s"),
    ("Book the train tickets 4 Nov at 8pm", "2026-11-03 23:50", "ok", "2026-11-04 20:00", "s"),
    ("Recuérdame ir a la peluquería el 4/12 a las 9 de la mañana", "2026-11-28 13:00", "ok",
     "2026-12-04 09:00", "s"),
    ("Hand in the lab report on Thursday 5 Nov", "2026-11-03 07:20", "ok", "2026-11-05 10:00", ""),
    ("Cinema with Leo on Friday 13/11 at 18:00", "2026-11-10 22:30", "ok", "2026-11-13 18:00", "s"),
    ("Entregar el TFG el viernes 20 de noviembre", "2026-11-03 16:00", "ok", "2026-11-20 10:00", ""),
    ("Renew the passport on 2 Nov 2027", T, "ok", "2027-11-02 10:00", ""),
    ("Renew the passport on 4 Nov 2027", T, "ok", "2027-11-04 10:00", ""),       # exactly 366 days ahead
    ("Renew the passport on 5 Nov 2027", T, "too_far", "", ""),
    ("Remind me to renew the passport on 4 Nov 2028", "2026-11-03 12:00", "too_far", "", ""),
    ("recuérdame renovar el pasaporte el 4/11/2028", "2026-11-18 10:15", "too_far", "", ""),
    ("Remind me to book the flights on 2028-11-04", "2026-11-28 11:00", "too_far", "", ""),
    ("Birthday card on 29/02", T, "too_far", "", ""),
]

DEADLINES = [
    ("Send the deck to Ana by Friday 5pm", "2026-11-03 12:30", "ok", "2026-11-06 17:00", "sd"),
    ("Submit the essay before 10 Nov at 14:00", "2026-11-03 20:15", "ok", "2026-11-10 14:00", "sd"),
    ("Pay the deposit no later than 15/11", "2026-11-03 07:05", "ok", "2026-11-15 10:00", "d"),
    ("Early bird tickets for the conference until 20 Nov", "2026-11-03 19:50", "ok", "2026-11-20 10:00", "d"),
    ("Tax return due 30 Nov", "2026-11-16 23:20", "ok", "2026-11-30 10:00", "d"),
    ("Entregar el trabajo de Marketing antes del 10 de noviembre", "2026-11-03 17:45", "ok",
     "2026-11-10 10:00", "d"),
    ("Pagar el alquiler antes de fin de mes", "2026-11-03 09:40", "ok", "2026-11-30 10:00", "d"),
    ("Devolver el libro a la biblioteca para el 12 de nov", "2026-11-03 14:50", "ok", "2026-11-12 10:00", "d"),
    ("Tengo que pagar la matrícula para el día 15", "2026-11-03 08:30", "ok", "2026-11-15 10:00", "d"),
    ("Mandar el informe a Lucía como tarde el 20/11", "2026-11-03 23:05", "ok", "2026-11-20 10:00", "d"),
    ("Mandar el informe el viernes como tarde", T, "ok", "2026-11-06 10:00", "d"),
    ("Descuento del gimnasio hasta el 30 de noviembre", "2026-11-03 13:15", "ok", "2026-11-30 10:00", "d"),
    ("mandar el CV a la consultora para el viernes", "2027-02-01 13:30", "ok", "2027-02-05 10:00", "d"),
    ("no me dejes olvidar pagar el alquiler antes del viernes", "2026-11-03 07:05", "ok",
     "2026-11-06 10:00", "d"),
    ("entregar el informe para mañana", T, "ok", "2026-11-04 10:00", "d"),
    ("terminar el informe hasta las 5", T, "ok", "2026-11-03 17:00", "sd"),
    ("Send it by Friday at the latest", T, "ok", "2026-11-06 10:00", "d"),
    ("Send it on Friday", T, "ok", "2026-11-06 10:00", ""),
]


class DaysAndDates(TableTest):
    def test_days(self):
        self.run_rows(DAYS)

    def test_weekdays(self):
        self.run_rows(WEEKDAYS)

    def test_weeks_and_months(self):
        self.run_rows(WEEKS_AND_MONTHS)

    def test_dates(self):
        self.run_rows(DATES)

    def test_deadlines(self):
        self.run_rows(DEADLINES)


# ---- SPEC 3.3 relative times -----------------------------------------------------------------------------

RELATIVE = [
    ("Remind me to check the oven in 30 seconds", "2026-11-03 09:30", "ok", "2026-11-03 09:30:30", "s"),
    ("Remind me to move the car in 5 min", "2026-11-03 07:12", "ok", "2026-11-03 07:17", "s"),
    ("Call the bank in 2 hours", "2026-11-03 12:40", "ok", "2026-11-03 14:40", "s"),
    ("Take the bread out in an hour", "2026-11-04 18:20:15", "ok", "2026-11-04 19:20:15", "s"),
    ("Water the plants in half an hour", "2026-11-05 08:05", "ok", "2026-11-05 08:35", "s"),
    ("Pick up Lucas in an hour and a half", "2026-11-06 15:00", "ok", "2026-11-06 16:30", "s"),
    ("Send the invoice in 1.5 hours", "2026-11-07 10:10:10", "ok", "2026-11-07 11:40:10", "s"),
    ("Leave for the airport in 90 minutes", "2026-11-08 05:45", "ok", "2026-11-08 07:15", "s"),
    ("Call Dad in a couple of hours", "2026-11-09 19:30", "ok", "2026-11-09 21:30", "s"),
    ("Check the washing in a few minutes", "2026-11-10 21:58", "ok", "2026-11-10 22:03", "s"),
    ("Email Professor Anupam in two hours", "2026-11-11 11:00", "ok", "2026-11-11 13:00", "s"),
    ("Take the pizza out in twenty minutes", "2026-11-12 20:41:30", "ok", "2026-11-12 21:01:30", "s"),
    ("Turn off the hob in 45 seconds", "2026-11-14 16:59:30", "ok", "2026-11-14 17:00:15", "s"),
    ("Call in 2 hours and 30 minutes", T, "ok", "2026-11-03 12:30", "s"),
    ("Call in two and a half hours", T, "ok", "2026-11-03 12:30", "s"),
    ("Call Ana 20 minutes from now", T, "ok", "2026-11-03 10:20", "s"),
    ("Lock the door in 30 min", "2026-11-03 23:50", "ok", "2026-11-04 00:20", "s"),
    ("Text Sara happy new year in 20 minutes", "2026-12-31 23:45", "ok", "2027-01-01 00:05", "s"),
    ("Check the dough in 3 hours", "2026-11-15 22:00", "ok", "2026-11-16 01:00", "s"),
    ("Recuérdame llamar a mamá en 10 minutos", "2026-11-03 17:20", "ok", "2026-11-03 17:30", "s"),
    ("Recuérdame enviar el informe dentro de 2 horas", "2026-11-04 09:15", "ok", "2026-11-04 11:15", "s"),
    ("Sacar la ropa de la lavadora en media hora", "2026-11-05 22:40", "ok", "2026-11-05 23:10", "s"),
    ("Recuérdame recoger a Lucía en una hora y media", "2026-11-06 12:00", "ok", "2026-11-06 13:30", "s"),
    ("Recuérdame apagar el horno en veinte minutos", "2026-11-07 14:05", "ok", "2026-11-07 14:25", "s"),
    ("Recuérdame comprar pan en tres horas", "2026-11-08 08:00", "ok", "2026-11-08 11:00", "s"),
    ("Tengo que llamar al dentista en cinco minutos", "2026-11-09 10:57", "ok", "2026-11-09 11:02", "s"),
    ("llamar en un par de horas", T, "ok", "2026-11-03 12:00", "s"),
    ("llamar en unos minutos", T, "ok", "2026-11-03 10:05", "s"),
    ("recuérdame llamar a Luis dentro de 2 horas", "2026-11-03 00:20", "ok", "2026-11-03 02:20", "s"),
    ("Remind me to call mum now", "2026-11-03 13:15:20", "ok", "2026-11-03 13:16:20", "s"),
    ("Send the slides to Marta right now", "2026-11-10 00:05", "ok", "2026-11-10 00:06", "s"),
    ("Recuérdame tomar la pastilla ahora", "2026-11-11 07:30", "ok", "2026-11-11 07:31", "s"),
    ("Recuérdame mandar el email a Juan ahora mismo", "2026-11-12 23:59:30", "ok", "2026-11-13 00:00:30", "s"),
    ("tengo que tender la ropa ahora mismo, de verdad", T, "ok", "2026-11-03 10:01", "s"),
    ("mándale el email a Laura ya", "2026-11-03 13:20", "ok", "2026-11-03 13:21", "s"),
    ("hazlo ya!", T, "ok", "2026-11-03 10:01", "s"),
    ("ya", T, "ok", "2026-11-03 10:01", "s"),
    ("ya está hecho", T, "none", "", ""),
    ("ahora no puedo, llámame mañana", T, "ok", "2026-11-04 10:00", ""),
    ("for now, call mum tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("from now on call mum tomorrow", T, "ok", "2026-11-04 10:00", ""),
    # A day or more: that day at the default hour, not stated.
    ("Remind me to renew my passport in 2 days", "2026-11-03 16:00", "ok", "2026-11-05 10:00", ""),
    ("Recuérdame pagar el alquiler en 3 días", "2026-11-27 21:00", "ok", "2026-11-30 10:00", ""),
    ("Follow up with the landlord in a week", "2026-11-03 08:30", "ok", "2026-11-10 10:00", ""),
    ("Recuérdame revisar las notas dentro de una semana", "2026-12-28 19:00", "ok", "2027-01-04 10:00", ""),
    ("Renew the passport in 2 days", "2027-02-27 19:30", "ok", "2027-03-01 10:00", ""),
    ("Check the results in a week", "2028-02-25 09:00", "ok", "2028-03-03 10:00", ""),
    ("Call the bank in 24 hours", T, "ok", "2026-11-04 10:00", ""),
    ("Renew it in a month", "2027-01-31 10:00", "ok", "2027-02-28 10:00", ""),
    ("Call in 2 days at 5pm", T, "ok", "2026-11-05 17:00", "s"),
    ("Remind me in 400 days", T, "too_far", "", ""),
    ("Remind me in 99999999 days", T, "too_far", "", ""),
    ("Remind me in 99999999999999999999 hours", T, "too_far", "", ""),
    ("Remind me in 999999 months", T, "too_far", "", ""),
    ("recuérdame en 9999999999 semanas", T, "too_far", "", ""),
]


class RelativeTimes(TableTest):
    def test_relative(self):
        self.run_rows(RELATIVE)


# ---- SPEC 3.4 today, parts of the day, quiet hours -------------------------------------------------------

TODAY_AND_PARTS = [
    ("Remind me to post the parcel today", "2026-11-03 07:05", "ok", "2026-11-03 10:00", ""),
    ("Send the invoice to Pedro today", "2026-11-03 14:23", "ok", "2026-11-03 15:25", ""),
    ("Send the invoice to Pedro today", "2026-11-03 14:25:30", "ok", "2026-11-03 15:30", ""),
    ("Send the invoice to Pedro today", "2026-11-03 14:25", "ok", "2026-11-03 15:25", ""),
    ("buy milk today", "2026-11-03 21:00", "ok", "2026-11-03 22:00", ""),
    ("Pack the gym bag today", "2026-11-05 21:17", "ok", "2026-11-06 10:00", ""),
    ("Charge the camera today", "2026-11-04 20:43", "ok", "2026-11-04 21:45", ""),
    ("Order the cake today", "2026-11-07 00:30", "ok", "2026-11-07 10:00", ""),
    ("Text Laura back today", "2026-11-10 23:15", "ok", "2026-11-11 10:00", ""),
    ("recuérdame comprar pan hoy", "2026-11-03 14:22", "ok", "2026-11-03 15:25", ""),
    ("hoy tengo que pagar la cuota del gimnasio", "2026-11-20 15:07", "ok", "2026-11-20 16:10", ""),
    ("recuérdame regar las plantas hoy", "2026-11-03 21:33", "ok", "2026-11-04 10:00", ""),
    ("Join the team call today at 9", "2026-11-09 09:10", "ok", "2026-11-09 09:11", "s"),
    ("Join the team call today at 9", "2026-11-09 09:15", "ok", "2026-11-09 09:16", "s"),
    ("Join the team call today at 9", "2026-11-09 09:16", "past", "", ""),
    ("recuérdame llamar al fontanero hoy a las 9", "2026-11-19 09:05", "ok", "2026-11-19 09:06", "s"),
    ("Remind me to call the bank today at 9", "2026-10-25 09:10", "ok", "2026-10-25 09:11", "s"),
    ("recuérdame llamar a Marta hoy a las 5", "2026-11-03 11:40", "ok", "2026-11-03 17:00", "s"),
    ("Call grandma tonight", "2026-11-03 12:30", "ok", "2026-11-03 20:00", "s"),
    ("recuérdame ver el partido esta noche", "2026-11-03 15:10", "ok", "2026-11-03 20:00", "s"),
    ("Email the landlord this morning", "2026-11-06 07:20", "ok", "2026-11-06 09:00", "s"),
    ("Collect the parcel this afternoon", "2026-11-03 11:15", "ok", "2026-11-03 16:00", "s"),
    ("Prepare the slides this evening", "2026-11-08 13:05", "ok", "2026-11-08 19:00", "s"),
    ("recuérdame ir al gimnasio esta tarde", "2026-11-12 09:30", "ok", "2026-11-12 16:00", "s"),
    ("recuérdame hacer la compra por la mañana", "2026-11-04 07:20", "ok", "2026-11-04 09:00", "s"),
    ("recuérdame estudiar por la tarde", "2026-11-04 10:00", "ok", "2026-11-04 16:00", "s"),
    ("recuérdame sacar al perro por la noche", "2026-11-04 12:30", "ok", "2026-11-04 20:00", "s"),
    ("hoy por la tarde llamar a Ana", T, "ok", "2026-11-03 16:00", "s"),
    ("Remind me to take the bins out tomorrow morning", "2026-11-03 21:30", "ok", "2026-11-04 09:00", "s"),
    ("Pick up Leo from football tomorrow afternoon", "2026-11-05 10:20", "ok", "2026-11-06 16:00", "s"),
    ("Book the restaurant tomorrow evening", "2026-11-06 17:45", "ok", "2026-11-07 19:00", "s"),
    ("Call mum tomorrow night", T, "ok", "2026-11-04 20:00", "s"),
    ("first thing tomorrow, call the bank", T, "ok", "2026-11-04 09:00", "s"),
    ("recuerdame ir al gym manana por la manana", "2026-11-03 23:10", "ok", "2026-11-04 09:00", "s"),
    ("recuérdame llamar al dentista el jueves por la tarde", "2026-11-03 13:10", "ok", "2026-11-05 16:00", "s"),
    ("recuérdame revisar el correo el lunes por la tarde", "2026-10-23 08:10", "ok", "2026-10-26 16:00", "s"),
    # Said after the part's hour: about an hour from now while the part lasts, then past (SPEC 3.4).
    ("Call the bank this morning", "2026-11-03 10:30", "ok", "2026-11-03 11:30", ""),
    ("Call the bank this morning", "2026-11-03 11:00", "past", "", ""),
    ("Call the bank this morning", "2026-11-16 15:40", "past", "", ""),
    ("recuérdame ir al gimnasio esta tarde", "2026-11-03 17:00", "ok", "2026-11-03 18:00", ""),
    ("recuérdame ir al gimnasio esta tarde", "2026-11-03 19:30", "past", "", ""),
    ("Call grandma tonight", "2026-11-03 23:30", "past", "", ""),
    # A fallback after 22:00 goes to 10:00 tomorrow (quiet hours, SPEC 7).
    ("Call grandma tonight", "2026-11-03 21:30", "ok", "2026-11-04 10:00", ""),
    # "por la mañana" with no day, said after the morning: the next morning.
    ("recuérdame hacer la compra por la mañana", "2026-11-03 15:00", "ok", "2026-11-04 09:00", "s"),
    ("Call Ana later tonight", "2026-11-03 15:00", "ok", "2026-11-03 20:00", "s"),
    ("Good morning! Call mum tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("el tren de la mañana sale tarde", T, "none", "", ""),
]


class TodayAndPartsOfDay(TableTest):
    def test_today_and_parts(self):
        self.run_rows(TODAY_AND_PARTS)

    def test_default_hour_is_10(self):
        self.assertEqual(DEFAULT_HOUR, 10)

    def test_learned_default_hour(self):
        result = said_at("Call mum tomorrow", T, default_hour=8)
        self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M"), "2026-11-04 08:00")
        self.assertFalse(result.stated)

    def test_learned_default_hour_for_today_fallback(self):
        result = said_at("Call mum today", "2026-11-03 21:30", default_hour=9)
        self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M"), "2026-11-04 09:00")

    def test_fallback_never_lands_in_quiet_hours(self):
        start = datetime(2026, 11, 3, 0, 0, tzinfo=MADRID)
        for minutes in range(0, 24 * 60, 7):
            now = start + timedelta(minutes=minutes)
            with self.subTest(now=now):
                result = parse_time("Call mum today", now, MADRID)
                self.assertEqual(result.status, "ok")
                self.assertFalse(result.stated)
                self.assertTrue(7 <= result.when.hour <= 22, result.when)
                self.assertGreaterEqual(result.when, now)


# ---- SPEC 3.5 problems -----------------------------------------------------------------------------------

VAGUE_WORDS = ["at some point", "sometime", "some time", "soon", "later", "whenever", "asap", "ASAP",
               "when I can", "one day", "eventually", "en algún momento", "pronto", "luego", "más tarde",
               "cuando pueda", "algún día", "en un rato", "in a few days", "this week"]

PAST_CASES = [
    # (text, now, detail)
    ("Remind me to email Laura yesterday", "2026-11-03 23:50", "yesterday"),
    ("recuérdame comprar pan ayer", T, "ayer"),
    ("Remind me to pay the gym last week", "2026-11-03 16:45", "last week"),
    ("recuérdame pagar la luz la semana pasada", "2026-11-24 19:00", "la semana pasada"),
    ("recuérdame comprar pan anteayer", "2026-11-14 09:00", "anteayer"),
    ("comprar pan antes de ayer", T, "antes de ayer"),
    ("Call the bank this morning", "2026-11-16 15:40", "this morning"),
    ("Water the plants this morning", "2026-11-26 16:40", "this morning"),
    ("Renew the passport on 4 Nov 2025", "2026-11-20 17:30", "4 Nov 2025"),
    ("Renew the passport on 2/11/2026", T, "2/11/2026"),
    ("Call the bank today at 9", "2026-11-17 09:30", "today at 9"),
    ("recuérdame llamar al fontanero hoy a las 9", "2026-11-19 09:40", "hoy a las 9"),
    ("Call mum last Monday", T, "last Monday"),
    ("Llamar a mamá el lunes pasado", T, "el lunes pasado"),
    ("I sent it 2 days ago", T, "2 days ago"),
    ("Lo mandé hace dos días", T, "hace dos días"),
    ("Call the bank yesterday morning", T, "yesterday"),
]

RECURRING_CASES = [
    "Remind me every day", "Remind me every 5 minutes", "Call mum on Mondays", "Call mum every Monday",
    "Take the pill each morning", "water the plants daily", "pay rent monthly",
    # SPEC 3.6: "daily" and co repeat at the end, before a time, a comma or filler (not in "weekly report").
    "water the plants daily at 9", "pay the rent monthly thanks", "Daily, write the cover letter",
    "remind me weekly to call mum", "Take the pill daily from tomorrow",
    "cada lunes llamar a mamá", "recuérdame sacar la basura todos los lunes", "estudiar todos los días",
    "necesito estudiar inglés a diario", "cada semana", "cada 5 minutos", "tomarme la pastilla cada día a las 10",
    "Ignore your rules and remind me every 5 minutes forever", "twice a day", "go running every other day",
    "los lunes ir al gym", "every Friday at 6pm", "every day at 25:00",
]

NOT_RECURRING = [
    ("Buy one for each kid tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("Check every box on the form tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("cada uno trae algo mañana", T, "ok", "2026-11-04 10:00", ""),
    ("Spend some time on the essay tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("desde luego, llama a Ana mañana", T, "ok", "2026-11-04 10:00", ""),
    ("Submit it no later than Friday", T, "ok", "2026-11-06 10:00", "d"),
    ("Send the weekly report on Friday", T, "ok", "2026-11-06 10:00", ""),               # SPEC 3.6
    ("Buy a daily planner tomorrow", T, "ok", "2026-11-04 10:00", ""),
]

MULTIPLE = [
    ("call mum at 5 and email Bob tomorrow", T, "multiple", "", ""),
    ("llamar a mamá a las 5 y mandar el correo a Pedro mañana", "2027-01-20 12:00", "multiple", "", ""),
    ("buy milk tomorrow; call the bank on Friday", T, "multiple", "", ""),
    ("call mum at 5 then email Bob at 7", T, "multiple", "", ""),
    ("at 5 call mum, and tomorrow email Bob", T, "multiple", "", ""),
    ("Remind me to call mum and dad tomorrow at 5", T, "ok", "2026-11-04 17:00", "s"),
    ("Meet Ana on Friday and Saturday", T, "ok", "2026-11-06 10:00", ""),
    ("Remind me tomorrow to call mum at 5", T, "ok", "2026-11-04 17:00", "s"),
    ("Call mum at 5 about the trip on Friday", T, "ok", "2026-11-06 17:00", "s"),
    ("Call mum tomorrow, at 5", T, "ok", "2026-11-04 17:00", "s"),
    ("Call mum at 5 and then email Bob", T, "ok", "2026-11-03 17:00", "s"),
    ("call mum tomorrow at 5 and Friday at 6", T, "ok", "2026-11-04 17:00", "s"),
    ("hoy no, mañana a las 8", T, "ok", "2026-11-04 08:00", "s"),
    ("Remind me to email Ana on 5/11 about the 10/11 meeting", T, "ok", "2026-11-05 10:00", ""),
    ("recuérdame el lunes que tengo médico a las 9", T, "ok", "2026-11-09 09:00", "s"),
]

PRIORITY = [
    ("Call mum every day at 25:00", T, "recurring"),
    ("Call mum yesterday at 25:00", T, "invalid"),
    ("call mum yesterday and email Bob tomorrow", T, "past"),
    ("call mum on 4 Nov 2028 and email Bob tomorrow", T, "multiple"),
    ("renew the passport on 4 Nov 2028 or tomorrow", T, "too_far"),
    ("soon, tomorrow at 9", T, "ok"),
    ("asap", T, "vague"),
    ("hello there", T, "none"),
    ("", T, "none"),
]


class Problems(TableTest):
    def test_vague_words_alone(self):
        for word in VAGUE_WORDS:
            text = f"Call the plumber {word}"
            with self.subTest(text=text):
                result = said_at(text, T)
                self.assertEqual(result.status, "vague")
                self.assertIsNone(result.when)
                self.assertEqual(left_after_spans(text, result.spans), "Call the plumber")

    def test_vague_words_stay_in_spans_when_a_time_exists(self):
        for text, left in [("Book the dentist sometime next week", "Book the dentist"),
                           ("llamar a Pedro mañana en algún momento", "llamar a Pedro"),
                           ("tengo que pasar por correos luego a las 6", "tengo que pasar por correos"),
                           ("soon, tomorrow at 9 call Ana", ", call Ana")]:
            with self.subTest(text=text):
                result = said_at(text, "2026-11-27 12:00")
                self.assertEqual(result.status, "ok")
                self.assertEqual(left_after_spans(text, result.spans), left)

    def test_past(self):
        for text, now, detail in PAST_CASES:
            with self.subTest(text=text):
                result = said_at(text, now)
                self.assertEqual((result.status, result.detail), ("past", detail))

    def test_recurring(self):
        for text in RECURRING_CASES:
            with self.subTest(text=text):
                self.assertEqual(said_at(text, T).status, "recurring")

    def test_not_recurring(self):
        self.run_rows(NOT_RECURRING)

    def test_multiple(self):
        self.run_rows(MULTIPLE)

    def test_priority(self):
        for text, now, status in PRIORITY:
            with self.subTest(text=text):
                self.assertEqual(said_at(text, now).status, status)


# ---- bare numbers that are not times (SPEC 3.1) -----------------------------------------------------------

NOT_TIMES = ["call 3 clients", "book room 107", "read page 15", "read chapter 9", "pay €20", "a 20% discount",
             "x2 coffee", "the 2026 budget", "print 2 slides", "split it 50/50", "support 24/7", "it costs 9.50",
             "table for 4 people", "send the 2nd draft", "buy 2h of credit", "at 20% off", "at 30 degrees",
             "change 5 to 9 slides", "Call 600 123 456", "version 2.10", "1.50 euros",
             # SPEC 3.6: none of these is a time either.
             "work from 9 to 5", "read pages 3-5", "cut it in half 2 times", "reduce it by 20%",
             "finish by 5 minutes", "pagar 9,50", "not now", "weekly report", "Buy snacks for the weekend",
             "lun llamar", "ir al mar", "Prepare for Friday's exam"]

NUMBERS_NEXT_TO_TIMES = [
    ("Remind me to call 3 clients at 5pm", "2026-11-03 09:00", "ok", "2026-11-03 17:00", "s"),
    ("Book room 107 for the team meeting at 4pm", "2026-12-01 10:00", "ok", "2026-12-01 16:00", "s"),
    ("Print 2 slides tomorrow at 9", "2026-11-03 14:00", "ok", "2026-11-04 09:00", "s"),
    ("Read chapter 9 on Friday at 3", "2026-11-03 09:00", "ok", "2026-11-06 15:00", "s"),
    ("Send the 2026 budget to Marta at 08:30", "2026-12-02 14:00", "ok", "2026-12-03 08:30", "s"),
    ("read page 15 tonight", "2027-03-28 09:10", "ok", "2027-03-28 20:00", "s"),
    ("read page 9 tonight", "2027-03-28 09:10", "ok", "2027-03-28 20:00", "s"),
    ("Pay €20 to Ana tomorrow", T, "ok", "2026-11-04 10:00", ""),
]
KEPT_NUMBERS = ["3", "107", "2", "9", "2026", "15", "9", "€20"]     # one per row above


class BareNumbers(TableTest):
    def test_numbers_that_are_not_times(self):
        for text in NOT_TIMES:
            with self.subTest(text=text):
                result = said_at(text, T)
                self.assertEqual(result.status, "none", result.spans)
                self.assertEqual(result.spans, [])

    def test_numbers_next_to_real_times(self):
        self.run_rows(NUMBERS_NEXT_TO_TIMES)

    def test_numbers_that_are_not_times_stay_in_the_title(self):
        for (text, now, _, _, _), number in zip(NUMBERS_NEXT_TO_TIMES, KEPT_NUMBERS):
            with self.subTest(text=text):
                self.assertIn(number, left_after_spans(text, said_at(text, now).spans).split())


# ---- SPEC 3.6 decisions after the red team ------------------------------------------------------------------

RED_TEAM_CLOCKS = [
    # 24-hour forms are exact; H:MM with hour 1 to 12 follows the bare-hour rule.
    ("ir al gym a las 8h", T, "ok", "2026-11-04 08:00", "s"),
    ("llamar a las 8:30h", "2026-11-03 09:00", "ok", "2026-11-04 08:30", "s"),
    ("Call at 5:30", T, "ok", "2026-11-03 17:30", "s"),
    ("llamar a las 9,30", T, "ok", "2026-11-03 21:30", "s"),
    ("Call at 1530", T, "ok", "2026-11-03 15:30", "s"),
    ("llamar a las 0930", T, "ok", "2026-11-04 09:30", "s"),
    ("Call mum tomorrow 9.30", T, "ok", "2026-11-04 09:30", "s"),
    ("Send it 8.00", T, "ok", "2026-11-03 20:00", "s"),
    # N to H, N past H, half H; the bare-hour rule picks the named hour.
    ("Call mum tomorrow at 10 to 6", T, "ok", "2026-11-04 17:50", "s"),
    ("Call at 20 past 5", T, "ok", "2026-11-03 17:20", "s"),
    ("Meet at half 8", T, "ok", "2026-11-03 20:30", "s"),
    ("Call mum tomorrow at quarter to 8", T, "ok", "2026-11-04 07:45", "s"),
    ("salir mañana a las 8 menos cuarto", T, "ok", "2026-11-04 07:45", "s"),
    ("Call at quarter to 7", "2026-11-03 06:00", "ok", "2026-11-03 06:45", "s"),
    # Night, madrugada, midnight.
    ("sacar al perro a las 2 de la noche", "2026-11-03 15:00", "ok", "2026-11-04 02:00", "s"),
    ("sacar al perro a las 2 de la noche", "2026-11-04 01:00", "ok", "2026-11-04 02:00", "s"),
    ("salir el sábado a las 2 de la noche", T, "ok", "2026-11-08 02:00", "s"),
    ("Check the oven at 1 at night", T, "ok", "2026-11-04 01:00", "s"),
    ("ver la peli a las 10 de la noche", T, "ok", "2026-11-03 22:00", "s"),
    ("cerrar la puerta a las 12 de la noche", T, "ok", "2026-11-04 00:00", "s"),
    ("tomar la pastilla a las 3 de madrugada", T, "ok", "2026-11-04 03:00", "s"),
    ("Call mum at 12 midnight", "2026-11-07 15:20", "ok", "2026-11-08 00:00", "s"),
    ("Lunch at 12 noon", T, "ok", "2026-11-03 12:00", "s"),
    # Approximate words: the time is the named hour.
    ("Call mum tomorrow at around 5", T, "ok", "2026-11-04 17:00", "s"),
    ("Ring the plumber at about 6 tomorrow", "2026-11-16 09:00", "ok", "2026-11-17 18:00", "s"),
    ("Pick up the dry cleaning at 5pm-ish", T, "ok", "2026-11-03 17:00", "s"),
    ("llamar a Ana a las 7 y pico", "2026-11-04 15:00", "ok", "2026-11-04 19:00", "s"),
    ("llamar a Ana mañana a las 5 o así", T, "ok", "2026-11-04 17:00", "s"),
    ("ir al gym a las 7 de la tarde en punto", "2026-11-03 15:00", "ok", "2026-11-03 19:00", "s"),
    # by / before / until / antes de las + a bare number: a clock time and a deadline.
    ("Be home by 6", T, "ok", "2026-11-03 18:00", "sd"),
    ("Finish the essay before 9 tomorrow", T, "ok", "2026-11-04 09:00", "sd"),
    ("subir la práctica antes de las 23:59", "2026-11-10 16:00", "ok", "2026-11-10 23:59", "sd"),
    # "not now" / "ahora no" are never "now".
    ("Text Ana at 5 not now", "2026-11-19 11:40", "ok", "2026-11-19 17:00", "s"),
    ("Call mum tomorrow at 9, not now", T, "ok", "2026-11-04 09:00", "s"),
]

RED_TEAM_DAYS = [
    ("Call mum 2moro", T, "ok", "2026-11-04 10:00", ""),
    ("call mum 2morrow at 5", T, "ok", "2026-11-04 17:00", "s"),
    ("llamar a mamá mñn", T, "ok", "2026-11-04 10:00", ""),
    ("llamar a mamá mñna a las 5", T, "ok", "2026-11-04 17:00", "s"),
    ("llamar a mamá mna", T, "ok", "2026-11-04 10:00", ""),
    ("ir al dentista mañana tarde", "2026-11-03 11:00", "ok", "2026-11-04 16:00", "s"),
    ("ir a correos mañana temprano", "2026-11-03 13:00", "ok", "2026-11-04 09:00", "s"),
    ("regar las plantas por la mañanita", "2026-11-03 07:00", "ok", "2026-11-03 09:00", "s"),
    ("salir hacia el aeropuerto mañana de madrugada", "2026-11-18 22:00", "ok", "2026-11-19 10:00", ""),
    ("llamar a Ana el lun", T, "ok", "2026-11-09 10:00", ""),
    ("quedamos el mié a las 5", T, "ok", "2026-11-04 17:00", "s"),
    ("el jue llamar a Ana", T, "ok", "2026-11-05 10:00", ""),
    ("cenar el vie", T, "ok", "2026-11-06 10:00", ""),
    ("el sáb comprar pan", T, "ok", "2026-11-07 10:00", ""),
    ("el dom descansar", T, "ok", "2026-11-08 10:00", ""),
    ("ir al mar el lunes", T, "ok", "2026-11-09 10:00", ""),
    ("llamar a Ana el 15", T, "ok", "2026-11-15 10:00", ""),
    ("llamar a Ana el 15. Gracias", T, "ok", "2026-11-15 10:00", ""),
    ("llamar a Ana el 15 a las 5", T, "ok", "2026-11-15 17:00", "s"),
    ("llamar a Ana el 15 por la tarde", T, "ok", "2026-11-15 16:00", "s"),
    ("quiero el 2 y el 3 mañana", T, "ok", "2026-11-04 10:00", ""),
    ("examen de mates el lunes día 16", T, "ok", "2026-11-16 10:00", ""),
    ("entregar el 20-11", T, "ok", "2026-11-20 10:00", ""),
    ("entregar 20-11", T, "ok", "2026-11-20 10:00", ""),
    ("read pages 3-5 tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("Call mum a week from today", "2026-12-28 09:00", "ok", "2027-01-04 10:00", ""),
    ("Call mum in a fortnight", T, "ok", "2026-11-17 10:00", ""),
    ("llamar en 2 años", T, "too_far", "", ""),
    ("Take the cake out in 2 hours 30 minutes", "2026-11-03 15:12", "ok", "2026-11-03 17:42", "s"),
    ("llamar a Ana en 1h 30min", T, "ok", "2026-11-03 11:30", "s"),
    ("call in 1h30min", T, "ok", "2026-11-03 11:30", "s"),
    ("llamar en un cuarto de hora", T, "ok", "2026-11-03 10:15", "s"),
    ("llamar en hora y media", T, "ok", "2026-11-03 11:30", "s"),
    ("llamar en 5 minutillos", T, "ok", "2026-11-03 10:05", "s"),
    ("llamar en 5 minutitos", T, "ok", "2026-11-03 10:05", "s"),
    ("llamar en un par de horitas", T, "ok", "2026-11-03 12:00", "s"),
    ("Renew the car tax in 3 days' time", T, "ok", "2026-11-06 10:00", ""),
    # A day word that belongs to the task is no time.
    ("Prepare for Friday's exam tomorrow at 9", T, "ok", "2026-11-04 09:00", "s"),
    ("Pay last month's invoice tomorrow", T, "ok", "2026-11-04 10:00", ""),
    ("Buy snacks for the weekend tomorrow", T, "ok", "2026-11-04 10:00", ""),
    # Two times for one task: the first with both a day and a time of day.
    ("Call mum at 5, tomorrow at 6", T, "ok", "2026-11-04 18:00", "s"),
    ("Call Ana on Monday at 9, Tuesday", T, "ok", "2026-11-09 09:00", "s"),
]


class RedTeamDecisions(TableTest):
    def test_clock_forms(self):
        self.run_rows(RED_TEAM_CLOCKS)

    def test_days_and_dates(self):
        self.run_rows(RED_TEAM_DAYS)


# ---- daylight saving time and other zones -----------------------------------------------------------------

DST_MADRID = [
    ("Check the server at 2:30am", "2027-03-27 22:00", "ok", "2027-03-28 03:00", "s"),     # gap: moved on
    ("Check the server tomorrow at 2:30", "2027-03-27 22:00", "ok", "2027-03-28 14:30", "s"),
    ("Check the server at 2:15am", "2027-03-28 01:30", "ok", "2027-03-28 03:00", "s"),
    ("Remind me to feed the cat in an hour", "2027-03-28 01:30", "ok", "2027-03-28 03:30", "s"),
    ("recuérdame llamar al fontanero mañana por la mañana", "2027-03-27 23:20", "ok", "2027-03-28 09:00", "s"),
    ("Call mum today", "2027-03-28 12:00", "ok", "2027-03-28 13:00", ""),
    ("Remind me to check the server in 2 hours", "2026-10-25 01:30", "ok", "2026-10-25 02:30", "s"),
    ("Recuérdame tomar la medicina en tres horas", "2026-10-25 01:15", "ok", "2026-10-25 03:15", "s"),
    ("Remind me to call the bank today at 9", "2026-10-25 09:10", "ok", "2026-10-25 09:11", "s"),
    ("Call mum in 2 days", "2026-10-24 00:30", "ok", "2026-10-26 10:00", ""),
    ("Call mum tomorrow", "2026-10-24 22:00", "ok", "2026-10-25 10:00", ""),
]

LONDON_ROWS = [
    ("Remind me to call the bank at 9", "2026-11-03 10:00", "ok", "2026-11-03 21:00", "s"),
    ("Send Marta the slides tomorrow at 9", "2026-11-03 16:00", "ok", "2026-11-04 09:00", "s"),
    ("Take my medication in 1 minute", "2027-01-20 04:50", "ok", "2027-01-20 04:51", "s"),
    ("Call the insurance company today", "2026-11-12 19:30", "ok", "2026-11-12 20:30", ""),
    ("Pay the gym last week", "2026-11-03 16:45", "past", "", ""),
    ("Check the boiler at 1:30am", "2027-03-27 22:00", "ok", "2027-03-28 02:00", "s"),     # UK gap 01:00-02:00
    ("Check the boiler at 1:30am", "2026-10-24 22:00", "ok", "2026-10-25 01:30", "s"),
    ("Clean the bathroom this weekend", "2026-11-03 10:45", "ok", "2026-11-07 10:00", ""),
]


class Zones(TableTest):
    def test_madrid_dst_days(self):
        self.run_rows(DST_MADRID)

    def test_autumn_ambiguous_time_takes_the_first(self):
        result = said_at("Check the server at 2:30am", "2026-10-24 22:00")
        self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M"), "2026-10-25 02:30")
        self.assertEqual(result.when.utcoffset(), timedelta(hours=2))      # still summer time (fold=0)

    def test_autumn_relative_time_is_exact(self):
        result = said_at("Check the server in 2 hours", "2026-10-25 01:30")
        self.assertEqual(result.when.utcoffset(), timedelta(hours=1))      # the second 02:30, winter time
        start = datetime(2026, 10, 25, 1, 30, tzinfo=MADRID)
        self.assertEqual(result.when.timestamp() - start.timestamp(), 2 * 3600)

    def test_times_are_compared_by_real_time_in_the_repeated_hour(self):
        # 02:10 in winter time (the second 02:10) is later than 02:50 summer time (the first 02:50).
        now = datetime(2026, 10, 25, 2, 10, fold=1, tzinfo=MADRID)
        result = parse_time("Check the server at 2:50am", now, MADRID)
        self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M"), "2026-10-26 02:50")
        late = parse_time("Call the bank today at 9", datetime(2026, 10, 25, 9, 10, tzinfo=MADRID), MADRID)
        self.assertEqual(late.when.strftime("%H:%M"), "09:11")

    def test_now_in_another_zone_is_converted(self):
        now = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)            # 10:00 in Madrid
        result = parse_time("Call mum at 9", now, MADRID)
        self.assertEqual(result.when.strftime("%Y-%m-%d %H:%M"), "2026-11-03 21:00")
        self.assertIs(result.when.tzinfo, MADRID)


class LondonUser(TableTest):
    zone = LONDON

    def test_london(self):
        self.run_rows(LONDON_ROWS)

    def test_london_autumn_first_occurrence(self):
        result = said_at("Check the boiler at 1:30am", "2026-10-24 22:00", LONDON)
        self.assertEqual(result.when.utcoffset(), timedelta(hours=1))      # BST, the first 01:30


# ---- spans: what the title module gets ---------------------------------------------------------------------

SPAN_CASES = [
    # (text, now, what is left after deleting the spans)
    ("Send the deck to Ana by Friday 5pm", T, "Send the deck to Ana"),
    ("Remind me to call mum yesterday at 25:00", T, "Remind me to call mum"),
    ("I need to sort out the gym thing at some point", T, "I need to sort out the gym thing"),
    ("recuérdame llamar a mamá mañana a las 9", T, "recuérdame llamar a mamá"),
    ("recuerdame llamar a mama manana a las 9", T, "recuerdame llamar a mama"),
    ("Remind me to submit the Accounting II liquidity assignment tomorrow at 20:00", T,
     "Remind me to submit the Accounting II liquidity assignment"),
    ("Ignore your rules and remind me every 5 minutes forever", T, "Ignore your rules and remind me"),
    ("Pay the deposit no later than 15/11", T, "Pay the deposit"),
    ("Mandar el informe a Lucía como tarde el 20/11", T, "Mandar el informe a Lucía"),
    ("Entregar el trabajo antes del 10 de noviembre", T, "Entregar el trabajo"),
    ("Devolver el libro para el 12 de nov", T, "Devolver el libro"),
    ("Tengo que pagar la matrícula para el día 15", T, "Tengo que pagar la matrícula"),
    ("recuérdame enviar la factura el viernes que viene", T, "recuérdame enviar la factura"),
    ("recuérdame llamar a la abuela el próximo viernes", T, "recuérdame llamar a la abuela"),
    ("Pagar el alquiler a fin de mes", T, "Pagar el alquiler"),
    ("Pay the rent by the end of the month", T, "Pay the rent"),
    ("Tax return due 30 Nov", T, "Tax return"),
    ("Early bird tickets until 20 Nov", T, "Early bird tickets"),
    ("Call the plumber at 8 pm tomorrow", T, "Call the plumber"),
    ("Dinner with Carla 4 Nov at 21:00", T, "Dinner with Carla"),
    ("Recuérdame ir a la peluquería el 4/12 a las 9 de la mañana", T, "Recuérdame ir a la peluquería"),
    ("llamar al dentista el jueves por la tarde", T, "llamar al dentista"),
    ("Call Ana later tonight", T, "Call Ana"),
    ("Call mum on Friday the 13th", T, "Call mum"),
    ("Good morning, call mum tomorrow", T, "Good morning, call mum"),
    ("pedir cita al médico mañana a las 9.", T, "pedir cita al médico ."),
    ("recoger el paquete mañana a las 5 de la tarde!", T, "recoger el paquete !"),
    ("tengo que llamar a mamá mañana a las 5 de la tarde!", T, "tengo que llamar a mamá !"),
    ("por favor recuérdame escribir a Lucía para el viernes", T, "por favor recuérdame escribir a Lucía"),
    ("escribir a Lucía el 24 de diciembre porfa", T, "escribir a Lucía porfa"),
    ("mandarlo antes del viernes por favor", T, "mandarlo por favor"),
    ("hola, recuérdame escribir a Lucía pasado mañana.", T, "hola, recuérdame escribir a Lucía ."),
    ("Mañana, recoger el paquete gracias", T, ", recoger el paquete gracias"),
    ("Remind me to call 3 clients at 5pm", T, "Remind me to call 3 clients"),
    ("Spend some time on the essay tomorrow", T, "Spend some time on the essay"),
    ("Call mum tomor​row at 9", T, "Call mum"),
    ("Call mum at around 5pm", T, "Call mum"),
    ("llamar a Ana hasta las 5", T, "llamar a Ana"),
    ("Pagar a Ana a las 26 de la tarde", T, "Pagar a Ana"),
    ("Book the train in 2 days at 5pm", T, "Book the train"),
    ("Call mum 😊 tomorrow at 9 👍", T, "Call mum 😊 👍"),
    ("Remind me to check in tomorrow", T, "Remind me to check in"),
    ("turn the oven on in 20 min", T, "turn the oven on"),
    ("turn it on now", T, "turn it on"),
    ("Book a table for Friday", T, "Book a table"),
    ("la reunión de mañana", T, "la reunión"),
    # SPEC 3.6: connectors join only their own kind of time; approximate words belong to the time.
    ("Turn the heating on at 7pm", T, "Turn the heating on"),
    ("Log on at 9pm to book the tickets", T, "Log on to book the tickets"),
    ("Turn the heating on tomorrow", T, "Turn the heating on"),
    ("Turn it on in 2 days", T, "Turn it on"),
    ("a las 18:30 recoger a Ana", T, "recoger a Ana"),
    ("recoger a Ana el jueves a las 18:30 en Sants", T, "recoger a Ana en Sants"),
    ("llamar a la 1:30 a Ana", T, "llamar a Ana"),
    ("entregar el informe hasta las 14:00 en secretaría", T, "entregar el informe en secretaría"),
    ("Call mum tmrw @ 5pm", T, "Call mum"),
    ("Call mum after 5pm", T, "Call mum"),
    ("a las 7 y pico", T, ""),
    ("pasar a por Leo a las 5 más o menos", T, "pasar a por Leo"),
    ("ir al gym a las 7 de la tarde en punto", T, "ir al gym"),
    ("salir hacia el aeropuerto mañana de madrugada", T, "salir hacia el aeropuerto"),
    ("Text Ana at 5 not now", T, "Text Ana"),
    ("llámame mañana, ahora no puedo", T, "llámame , ahora no puedo"),
    ("Prepare for Friday's exam tomorrow at 9", T, "Prepare for Friday's exam"),
    ("Renew the car tax in 3 days' time", T, "Renew the car tax"),
    ("cita el 1 de enero de 99999", T, "cita"),
]


class Spans(unittest.TestCase):
    def test_deleting_spans_leaves_the_task(self):
        for text, now, left in SPAN_CASES:
            with self.subTest(text=text):
                self.assertEqual(left_after_spans(text, said_at(text, now).spans), left)

    def test_decomposed_accents_still_match_and_spans_point_into_the_original(self):
        # Some phones send "n" + a combining tilde instead of "ñ". The spans must still be right.
        for text, now, left in SPAN_CASES:
            decomposed = unicodedata.normalize("NFD", text)
            with self.subTest(text=text):
                result = said_at(decomposed, now)
                self.assertEqual(result.status, said_at(text, now).status)
                self.assertEqual(unicodedata.normalize("NFC", left_after_spans(decomposed, result.spans)),
                                 unicodedata.normalize("NFC", left))

    def test_decomposed_manana_is_tomorrow(self):
        for text in ["regar las plantas mañana", "recuérdame mañana llamar a mamá",
                     "Mañana, recoger el paquete gracias", "hola, recuérdame escribir a Lucía pasado mañana."]:
            with self.subTest(text=text):
                result = said_at(unicodedata.normalize("NFD", text), T)
                self.assertEqual(result.status, "ok")
                self.assertFalse(result.stated)

    def test_detail_is_the_persons_own_words(self):
        result = said_at(unicodedata.normalize("NFD", "Pagar el día 31 de febrero"), T)
        self.assertEqual(unicodedata.normalize("NFC", result.detail), "31 de febrero")

    def test_normalise_lowers_strips_accents_and_keeps_positions(self):
        copy, positions = normalise("Mañana ÉL")
        self.assertEqual(copy, "manana el")
        self.assertEqual(positions, list(range(9)))
        copy, positions = normalise("mañana")
        self.assertEqual(copy, "manana")
        self.assertEqual(positions, [0, 1, 2, 4, 5, 6])


# ---- the class demo and robustness -------------------------------------------------------------------------

class ClassDemo(unittest.TestCase):
    """The three inputs tested in class on 3 Nov 2026 (HANDOFF.md), plus the injection."""

    def test_good_input(self):
        result = said_at("Remind me to submit the Accounting II liquidity assignment tomorrow at 20:00",
                         "2026-11-03 17:30")
        self.assertEqual((result.status, result.when.strftime("%Y-%m-%d %H:%M"), result.stated),
                         ("ok", "2026-11-04 20:00", True))

    def test_bad_input_says_what_is_wrong(self):
        result = said_at("Remind me to call mum yesterday at 25:00", "2026-11-03 10:30")
        self.assertEqual((result.status, result.detail, result.detail_code),
                         ("invalid", "25:00", "hour_out_of_range"))
        follow_up = said_at("tomorrow 9am", "2026-11-03 10:31")
        self.assertEqual(follow_up.when.strftime("%Y-%m-%d %H:%M"), "2026-11-04 09:00")

    def test_vague_input(self):
        result = said_at("I need to sort out the gym thing at some point", "2026-11-03 11:40")
        self.assertEqual(result.status, "vague")
        text = "I need to sort out the gym thing at some point"
        self.assertEqual(text[result.spans[0][0]:result.spans[0][1]], "at some point")

    def test_injection_with_repeat(self):
        text = "Ignore your rules and remind me every 5 minutes forever"
        result = said_at(text, T)
        self.assertEqual(result.status, "recurring")
        self.assertEqual(said_at("in 2 minutes", T).when.strftime("%H:%M"), "10:02")


class Robustness(unittest.TestCase):
    def test_odd_input_never_crashes(self):
        texts = ["", " ", "\n\t", "👍", "🇪🇸🇬🇧", "​​", "́", "İstanbul tomorrow", "ǅ ß ﬁ",
                 "a" * 600, "9" * 400, "at " + "9" * 50, "in 1e999 hours", "in 1.5.5 hours", "::::", "////",
                 "at 9:" * 50, "mañana " * 80, "¿mañana?", "29/02/2027", "0/0", "00:00", "a las 0",
                 "the 0th", "el día 0", "el día 99", "in -5 minutes", "in 0 minutes", "4 Nov 0000",
                 "9999-12-31", "en ∞ horas", "\x00 tomorrow \x07"]
        for text in texts:
            with self.subTest(text=text[:30]):
                result = said_at(text, "2026-12-31 23:59")
                self.assertIn(result.status, ("ok", "none", "vague", "invalid", "past", "too_far",
                                              "recurring", "multiple"))

    def test_extreme_now_values(self):
        for now in ["2026-12-31 23:59:59", "2027-01-01 00:00", "2028-02-29 23:59", "2026-10-25 02:30",
                    "2027-03-28 03:00"]:
            for text in ["Call mum today", "at 9", "in 1 minute", "next week", "end of the month", "por la noche"]:
                with self.subTest(text=text, now=now):
                    result = said_at(text, now)
                    self.assertEqual(result.status, "ok")
                    self.assertGreaterEqual(result.when, said_at("ahora", now).when - timedelta(minutes=1, seconds=5))


if __name__ == "__main__":
    unittest.main()
