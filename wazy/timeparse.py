"""Read the time in a person's message (SPEC section 3).

parse_time() is the only door into this module. It takes the whole message ("call mum tomorrow at 5"),
the moment it arrived and the person's time zone. It answers with a TimeResult: one exact moment, or the
reason there is none (only vague words, already past, impossible, repeating, two tasks at once, too far).
Code decides the time, never a guess: when the words do not say enough, the status says so.

It works in four steps:
1. Normalise. Make a lower-case copy without accents ("Mañana" -> "manana"), and remember for every letter
   of the copy where it sits in the message, so a span found in the copy can be given back as a span of
   exactly what the person typed.
2. Find pieces. Small named rules each look for one family of time words: repeating words, words about the
   past, delays ("in 2 hours"), dates, clock times, parts of the day, days, "now", vague words. A rule may not
   reuse characters an earlier rule took, so "9:30" is never also read as "9".
3. Group the pieces that sit next to each other into one time ("tomorrow" + "at 5"), and decide whether two
   separate times belong to two different tasks.
4. Turn the chosen group into a date and time in the person's zone, then run the final checks.

The result lists every piece's span, plus the little words stuck to it ("by", "at", "el", "antes de"), so
the title module can delete them and keep only the task.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable, List, Optional, Tuple

from .types import Span, TimeResult

DEFAULT_HOUR = 10                       # a day with no time of day gets this hour (SPEC 3.4)

DAY = timedelta(days=1)
SOON = timedelta(minutes=1)             # "now", "ya", and "today at 9" said a few minutes late
LATE_GRACE = timedelta(minutes=15)      # how late "today at 9" may be said and still count
TOLERANCE = timedelta(seconds=5)        # a time up to 5 s ago is not "past": the message took a moment to arrive
MAX_AHEAD = timedelta(days=366)         # further ahead than this is "too_far"
LONGEST_DELAY_DAYS = 400                # "in 99999999 days" is cut to this: still too far, and no calendar overflow
FIRST_DAY_HOUR, LAST_DAY_HOUR = 7, 22   # a bare "at 9" is read as a time between 07:00 and 22:59
LAST_FALLBACK_HOUR = 22                 # the "about an hour from now" fallback never lands after 22:00

# When several problems apply, the first one in this list is reported (SPEC 3.5).
PRIORITY = ("recurring", "invalid", "past", "multiple", "too_far", "ok", "vague", "none")


# ---- 1. Normalising ----------------------------------------------------------------------------------

INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­"}   # zero-width characters, soft hyphen


def is_skipped(char: str) -> bool:
    """An accent typed as its own character (some phones send "n" + "~" for "ñ"), or an invisible character."""
    return unicodedata.combining(char) > 0 or char in INVISIBLE


def normalise(text: str) -> Tuple[str, List[int]]:
    """A lower-case copy of the text without accents ("Mañana" -> "manana"), and for each character of the
    copy, its position in the text.

    Usually the copy is as long as the text and position i is just i. Accents typed as separate characters
    and invisible characters are left out of the copy, so the list of positions finds the way back.
    """
    chars, positions = [], []
    for index, char in enumerate(text):
        if is_skipped(char):
            continue
        low = char.lower()
        if len(low) != 1:           # a few letters (like the Turkish "İ") lower-case into two: keep them as they are
            low = char
        # NFD splits "ñ" into "n" plus a tilde mark; keeping only the first part removes the accent.
        chars.append(unicodedata.normalize("NFD", low)[0])
        positions.append(index)
    copy = "".join(chars).replace("’", "'")     # a curly apostrophe counts as a straight one ("o’clock")
    return copy, positions


def one_of(phrases: Iterable[str]) -> str:
    """A regex group matching any of the phrases, longest first. A space in a phrase means one or more spaces."""
    ordered = sorted(set(phrases), key=len, reverse=True)
    return "(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ordered) + ")"


def squash(words: str) -> str:
    """'antes   de' -> 'antes de', so a matched phrase can be looked up in a table."""
    return " ".join(words.split())


# ---- 2. Words the rules know ----------------------------------------------------------------------------

EN_HOURS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
ES_HOURS = {"una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
            "nueve": 9, "diez": 10, "once": 11, "doce": 12}
NUMBER_WORDS = dict(EN_HOURS, **ES_HOURS)
NUMBER_WORDS.update({
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty five": 25, "twenty-five": 25, "thirty": 30, "forty": 40,
    "forty five": 45, "forty-five": 45, "fifty": 50,
    "un": 1, "uno": 1, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veinticinco": 25, "treinta": 30, "cuarenta": 40,
    "cuarenta y cinco": 45, "cincuenta": 50,
})
# How much "a couple of hours" or "half an hour" is. None means "no exact number" ("a few").
QUANTITY_WORDS = {"a": 1, "an": 1, "a couple": 2, "a couple of": 2, "un par de": 2, "half an": 0.5,
                  "half a": 0.5, "media": 0.5, "a few": None, "unos": None, "unas": None,
                  "unos pocos": None, "unas pocas": None}
FRACTIONS = {"and a half": 0.5, "y media": 0.5, "and a quarter": 0.25, "y cuarto": 0.25}

UNIT_WORDS = {
    "second": ("seconds", "second", "secs", "sec", "s", "segundos", "segundo", "segs", "seg"),
    "minute": ("minutes", "minute", "mins", "min", "m", "minutos", "minuto"),
    "hour": ("hours", "hour", "hrs", "hr", "h", "horas", "hora"),
    "day": ("days", "day", "dias", "dia"),
    "week": ("weeks", "week", "semanas", "semana"),
    "month": ("months", "month", "meses", "mes"),
}
UNIT_OF = {word: unit for unit, words in UNIT_WORDS.items() for word in words}
SECONDS_IN = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}

WEEKDAYS = {
    "monday": 0, "mon": 0, "lunes": 0, "tuesday": 1, "tue": 1, "tues": 1, "martes": 1,
    "wednesday": 2, "wed": 2, "weds": 2, "miercoles": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "jueves": 3, "friday": 4, "fri": 4, "viernes": 4, "saturday": 5, "sat": 5, "sabado": 5,
    "sunday": 6, "sun": 6, "domingo": 6,
}
SHORT_DAYS_THAT_ARE_WORDS = {"mon", "wed", "sat", "sun"}     # "I sat down", "the sun": need "on"/"next"/...
SPANISH_WEEKDAYS = {"lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

MONTHS = {
    "january": 1, "jan": 1, "enero": 1, "ene": 1, "february": 2, "feb": 2, "febrero": 2,
    "march": 3, "mar": 3, "marzo": 3, "april": 4, "apr": 4, "abril": 4, "abr": 4, "may": 5, "mayo": 5,
    "june": 6, "jun": 6, "junio": 6, "july": 7, "jul": 7, "julio": 7, "august": 8, "aug": 8, "agosto": 8,
    "september": 9, "sep": 9, "sept": 9, "septiembre": 9, "setiembre": 9, "october": 10, "oct": 10,
    "octubre": 10, "november": 11, "nov": 11, "noviembre": 11, "december": 12, "dec": 12,
    "diciembre": 12, "dic": 12,
}

# Parts of the day: (the hour it means on its own, the hour it ends). SPEC 3.2 and 3.4.
PART_HOURS = {"morning": (9, 12), "afternoon": (16, 20), "evening": (19, 23), "night": (20, 23)}
TODAY_PARTS = {"this morning": "morning", "this afternoon": "afternoon", "this evening": "evening",
               "tonight": "night", "this night": "night", "esta manana": "morning",
               "esta tarde": "afternoon", "esta noche": "night"}
FREE_PARTS = {"in the morning": "morning", "in the afternoon": "afternoon", "in the evening": "evening",
              "at night": "night", "por la manana": "morning", "por la tarde": "afternoon",
              "por la noche": "night", "en la manana": "morning", "en la tarde": "afternoon",
              "en la noche": "night", "first thing in the morning": "morning", "first thing": "morning",
              "a primera hora": "morning"}
# These only fix am/pm right after a clock time ("a las 5 de la tarde"); "el tren de la mañana" is no time.
CLOCK_PARTS = {"de la manana": "morning", "de la tarde": "afternoon", "de la noche": "night",
               "de la madrugada": "early", "del mediodia": "noon"}
# These only count right after a day ("tomorrow morning", "Friday evening"); "good morning" is no time.
DAY_PARTS = {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "night": "night"}
ALL_PARTS = dict(TODAY_PARTS, **FREE_PARTS, **CLOCK_PARTS, **DAY_PARTS)

DAYS_AHEAD = {"today": 0, "hoy": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1, "tomorow": 1, "tommorow": 1,
              "tommorrow": 1, "manana": 1, "the day after tomorrow": 2, "day after tomorrow": 2,
              "pasado manana": 2}

VAGUE_WORDS = [
    "at some point", "sometime", "some time", "someday", "some day", "soon", "soonish", "later", "later on",
    "whenever", "whenever i can", "asap", "a.s.a.p.", "a.s.a.p", "as soon as possible", "when i can",
    "when i get a chance", "when i have time", "when possible", "one day", "one of these days",
    "eventually", "at some stage", "in a while", "in a bit", "in a moment", "this week", "later this week",
    "end of the week", "the end of the week", "next month", "next year",
    "en algun momento", "pronto", "luego", "mas tarde", "mas adelante", "cuando pueda", "cuando puedas",
    "cuando tenga tiempo", "algun dia", "un dia de estos", "en un rato", "dentro de un rato", "en un ratito",
    "en un momento", "lo antes posible", "cuanto antes", "esta semana", "el mes que viene",
    "el proximo mes", "el ano que viene",
]

PAST_WORDS = [
    "yesterday", "the day before yesterday", "day before yesterday", "last night", "last week",
    "last weekend", "last month", "last year", "the other day", "earlier today",
    "ayer", "anteayer", "antier", "antes de ayer", "anoche", "antes de anoche", "la semana pasada",
    "el fin de semana pasado", "el finde pasado", "el mes pasado", "el ano pasado", "el otro dia",
]

REPEAT_UNITS = [
    "second", "seconds", "minute", "minutes", "min", "mins", "hour", "hours", "day", "days", "week", "weeks",
    "fortnight", "month", "months", "year", "years", "morning", "mornings", "afternoon", "afternoons",
    "evening", "evenings", "night", "nights", "weekday", "weekdays", "weekend", "weekends",
    "segundo", "segundos", "minuto", "minutos", "hora", "horas", "dia", "dias", "semana", "semanas",
    "mes", "meses", "ano", "anos", "manana", "mananas", "tarde", "tardes", "noche", "noches",
    "fin de semana", "fines de semana", "finde", "findes", "quincena",
    "mondays", "tuesdays", "wednesdays", "thursdays", "fridays", "saturdays", "sundays", "sabados", "domingos",
] + list(WEEKDAYS)
REPEAT_WORDS = ["daily", "weekly", "monthly", "yearly", "annually", "hourly", "nightly", "fortnightly",
                "biweekly", "everyday", "forever", "a diario", "diariamente", "semanalmente", "mensualmente",
                "anualmente", "para siempre", "cada dia"]

# Little words that belong to the time next to them, so the title drops them too (SPEC 4.1).
DEADLINE_WORDS = {"no later than", "not later than", "by", "before", "until", "till", "til", "due",
                  "antes de", "antes del", "para", "como tarde", "como muy tarde", "hasta",
                  "a mas tardar", "at the latest", "at the very latest"}
# ("in" is not here: "in 2 hours" and "in the morning" are inside their own patterns, and "check in tomorrow"
# must keep its "in".)
LEFT_CONNECTORS = DEADLINE_WORDS | {
    "at", "on", "for", "from", "around", "about", "el", "del", "de", "a", "al", "en", "dentro de",
    "sobre", "hacia", "desde", "a eso de", "eso de"}
RIGHT_CONNECTORS = {"como tarde", "como muy tarde", "at the latest", "at the very latest", "a mas tardar",
                    "sharp"}

# Words that are not a task when deciding whether a message holds two tasks (SPEC 3.5 "multiple").
NOT_TASK_WORDS = {
    "remind", "me", "to", "please", "pls", "plz", "and", "then", "the", "an", "i", "need", "have", "must",
    "should", "want", "also", "hey", "hi", "ok", "wazy", "don", "dont", "forget", "let", "it", "that",
    "this", "recuerdame", "recordarme", "acuerdate", "que", "de", "del", "y", "luego", "el", "la", "los",
    "las", "un", "una", "tengo", "necesito", "debo", "hay", "quiero", "porfa", "por", "favor", "oye",
    "hola", "tambien", "no", "olvidar", "dejes", "olvides", "te",
}


# ---- 3. Patterns --------------------------------------------------------------------------------------
# Every pattern runs on the normalised copy (lower case, no accents).

NUMBER = one_of(NUMBER_WORDS)
EN_HOUR = one_of(EN_HOURS)
ES_HOUR = one_of(ES_HOURS)
WEEKDAY = one_of(WEEKDAYS)
MONTH = one_of(MONTHS)
UNIT = one_of(UNIT_OF) + r"(?![a-z])"                    # "5 min" but not "5 mum"
QUANTITY = r"(?:\d+(?:[.,]\d+)?|" + one_of(list(NUMBER_WORDS) + list(QUANTITY_WORDS)) + r"(?=\s))"
MERIDIEM = r"(?P<mer>[ap])\.?m\b\.?"                     # am, a.m., pm, p.m. (not "amigos")
NO_DIGIT_BEFORE = r"(?<![\w.,:/€$£])"                    # a number standing alone, not "v2", "€20" or "1/2"
# A number followed by these is an amount, not a time: "at 20%", "at 30 degrees", "20.50 euros".
AMOUNT_AFTER = re.compile(r"\s*(?:%|€|\$|£|euros?\b|eur\b|dollars?\b|pounds?\b|libras?\b|degrees?\b|"
                          r"grados?\b|kg\b|km\b|kilos?\b|percent\b|por\s+ciento\b)")
# A word just before a number that makes it a clock time even without ":" ("at 9.30", "hasta las 5").
TIME_WORD_BEFORE = re.compile(r"(?:\b(?:at|by|before|until|till|from|around|about|a\s+las?|las?|hasta|"
                              r"antes\s+de|para|sobre|hacia|desde)|@)\s*$")
# A number right after one of these is a label, not a time: "read page 9 tonight", "sala 3 por la tarde".
LABEL_BEFORE = re.compile(r"\b(?:page|pages|pg|p|chapter|ch|room|slide|slides|number|no|item|step|question|"
                          r"exercise|task|level|unit|section|table|gate|platform|line|bus|flat|floor|seat|"
                          r"row|episode|lesson|pagina|paginas|capitulo|tema|habitacion|sala|ejercicio|"
                          r"pregunta|numero|leccion|mesa|planta|puerta|linea|episodio|apartado)\.?\s*#?\s*$")
# "la"/"las" without "a" starts a time only after one of these ("hasta las 5", "antes de las 9").
BEFORE_BARE_LAS = re.compile(r"\b(?:hasta|antes\s+de|para|sobre|hacia|desde|eso\s+de|tipo)\s+$")

# Repeating schedules (SPEC 3.5 "recurring"): "every 5 minutes", "cada lunes", "todos los días", "on Mondays".
REPEATING = [
    re.compile(r"\b(?:every|each|cada)\s+(?:other\s+|single\s+|few\s+|couple\s+of\s+)?(?:(?:\d+|" + NUMBER
               + r")\s+)?" + one_of(REPEAT_UNITS) + r"\b"),
    re.compile(r"\b(?:todos|todas)\s+(?:los|las)\s+(?:(?:\d+|" + NUMBER + r")\s+)?" + one_of(REPEAT_UNITS)
               + r"\b"),
    re.compile(r"\b(?:once|twice|three\s+times|\d+\s+times|una\s+vez|dos\s+veces|\d+\s+veces)\s+"
               r"(?:a|an|per|al|a\s+la|por)\s+(?:day|week|month|year|hour|dia|semana|mes|ano|hora)\b"),
    re.compile(r"\b(?:on\s+)?(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|weekdays|"
               r"weekends)\b"),
    re.compile(r"\b(?:los\s+(?:lunes|martes|miercoles|jueves|viernes|sabados|domingos|fines\s+de\s+semana|"
               r"findes)|por\s+las\s+(?:mananas|tardes|noches))\b"),
    re.compile(r"\b" + one_of(REPEAT_WORDS) + r"\b"),
]

# Times that have already gone (SPEC 3.5 "past"): "yesterday", "last Monday", "2 days ago", "hace 2 días".
PAST = [
    re.compile(r"\b" + one_of(PAST_WORDS) + r"\b"),
    re.compile(r"\blast\s+" + WEEKDAY + r"\b"),
    re.compile(r"\b(?:el\s+)?" + WEEKDAY + r"\s+pasado\b"),
    re.compile(r"\b" + QUANTITY + r"\s*" + UNIT + r"\s+ago\b"),
    re.compile(r"\bhace\s+" + QUANTITY + r"\s*" + UNIT),
]

# Delays (SPEC 3.3): "in 2 hours", "in an hour and a half", "en media hora", "dentro de 2 días",
# "in 2 hours and 30 minutes", "20 minutes from now".
DELAY = (r"(?P<qty>" + QUANTITY + r")(?P<frac1>\s+" + one_of(FRACTIONS) + r")?\s*(?P<unit>" + UNIT + r")"
         r"(?P<frac2>\s+" + one_of(FRACTIONS) + r")?"
         r"(?:\s+(?:and|y)\s+(?P<qty2>\d+|" + NUMBER + r")\s*(?P<unit2>" + UNIT + r"))?")
IN_DELAY = re.compile(r"\b(?:in|within|en|dentro\s+de|de\s+aqui\s+a)\s+" + DELAY + r"(?:\s+from\s+now\b)?")
DELAY_FROM_NOW = re.compile(r"\b" + DELAY + r"\s+from\s+now\b")

# Dates (SPEC 3.2).
YEAR = r"(?P<y>\d{4}|\d{2})"
ISO_DATE = re.compile(r"\b(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b")                   # 2026-11-04
NUMERIC_DATE_WITH_YEAR = re.compile(r"(?<![\d/.-])(?P<d>\d{1,2})(?P<sep>[/.-])(?P<mo>\d{1,2})(?P=sep)"
                                    + YEAR + r"(?![\d/.-]?\d)")                           # 4/11/2026, 4-11-26
NUMERIC_DATE = re.compile(r"(?<![\d/.,])(?P<d>\d{1,2})/(?P<mo>\d{1,2})(?![\d/]|\.\d)")     # 4/11, 04/11
DAY_MONTH = re.compile(r"\b(?:the\s+)?(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+|de\s+)?(?P<mon>" + MONTH
                       + r")\b\.?(?:,?\s+(?:de\s+|del\s+)?(?P<y>\d{4}))?(?![:.]?\d)")    # 4 Nov, 4 de noviembre
MONTH_DAY = re.compile(r"\b(?P<mon>" + MONTH + r")\.?\s+(?:the\s+)?(?P<d>\d{1,2})(?:st|nd|rd|th)?\b"
                       r"(?:,?\s+(?P<y>\d{4}))?(?!\s*(?:[:.]\d|[ap]\.?m\b|h\b))")         # Nov 4, November 4th
ORDINAL_DAY = re.compile(r"\bthe\s+(?P<d>\d{1,2})(?:st|nd|rd|th)\b")                       # (on) the 4th
ORDINAL_TRIGGER = re.compile(r"\b(?:on|by|before|until|till|due|for|from|after)\s+$")
DIA_DAY = re.compile(r"\bdia\s+(?P<d>\d{1,2})\b(?![:.]\d)")                                # (el) día 4
WEEKDAY_NUMBER = re.compile(r"\b(?P<wd>" + WEEKDAY + r")\s*,?\s+(?P<the>the\s+|el\s+|dia\s+)?(?P<d>\d{1,2})"
                            r"(?P<suf>st|nd|rd|th)?\b(?!\s*(?:[:./]\d|h\b|[ap]\.?m\b|de\b|y\b))")  # viernes 20
WEEKDAY_BEFORE = re.compile(r"\b(?P<wd>" + WEEKDAY + r")\s*,?\s*(?:the\s+|el\s+|dia\s+)?$")    # "Monday" 5 Nov

# Clock times (SPEC 3.1).
COLON_TIME = re.compile(NO_DIGIT_BEFORE + r"(?P<h>\d{1,2})(?P<sep>[:.])(?P<m>\d{2})(?!\d)(?![.,:]\d)"
                        r"(?:\s?" + MERIDIEM + r")?(?:\s?(?:h|hrs|hs)\b)?")                 # 20:00, 8.30pm
AM_PM_TIME = re.compile(NO_DIGIT_BEFORE + r"(?P<h>\d{1,2})\s?" + MERIDIEM)               # 8pm, 8 p.m.
H_TIME = re.compile(NO_DIGIT_BEFORE + r"(?P<h>\d{1,2})h(?P<m>\d{2})?\b")                  # 20h, 20h30
PAST_TO_TIME = re.compile(r"\b(?P<mins>half|(?:a\s+)?quarter|\d{1,2}|" + NUMBER + r")(?P<word>\s+minutes?)?"
                          r"\s+(?P<dir>past|to)\s+(?P<h>\d{1,2}(?!\d)|" + EN_HOUR + r"\b)(?![:.]\d)")
AT_TIME = re.compile(r"(?:\bat|@)\s*(?P<h>\d{1,2}(?!\d)|" + EN_HOUR + r"\b)(?![:.]\d)"
                     r"(?P<tail>\s*-?ish\b|\s+o'?clock\b)?")                                # at 9, @ 5, at nine
LOOSE_TIME = re.compile(NO_DIGIT_BEFORE + r"(?P<h>\d{1,2}|" + EN_HOUR + r")(?:\s*-?ish\b|\s+o'?clock\b|"
                        r"\s+en\s+punto\b)")                                               # 9ish, 9 o'clock
LAS_TIME = re.compile(r"\b(?P<trig>a\s+las?|las?)\s+(?P<h>\d{1,2}(?!\d)|" + ES_HOUR + r"\b)(?![:.]\d)"
                      r"(?:\s*h\b)?(?P<extra>\s+y\s+media|\s+y\s+cuarto|\s+menos\s+cuarto|"
                      r"\s+(?:y|menos)\s+(?:\d{1,2}(?!\d)|" + NUMBER + r"\b)(?:\s+minutos?)?)?"
                      r"(?:\s+en\s+punto)?")                                               # a las 9 y media
NUMBER_BEFORE_PART = re.compile(NO_DIGIT_BEFORE + r"(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?(?=\s+" + one_of(
    ["in the morning", "in the afternoon", "in the evening", "at night", "tonight", "this morning",
     "this afternoon", "this evening"] + list(CLOCK_PARTS)) + r"\b)")                       # 9 tonight
NOON_MIDNIGHT = re.compile(r"(?<!del )(?<!de la )\b(?P<w>noon|midday|mediodia|midnight|medianoche)\b")

# Parts of the day, days, weeks.
PART_OF_DAY = re.compile(r"\b(?P<w>" + one_of(ALL_PARTS) + r")\b")
DAY_WORD = re.compile(r"\b(?P<w>" + one_of(DAYS_AHEAD) + r")\b")
WEEKDAY_WORD = re.compile(r"\b(?:(?P<this>this|este)\s+|(?P<next>next|this\s+coming|coming|el\s+proximo|"
                          r"proximo)\s+)?(?P<wd>" + WEEKDAY + r")\b\.?(?P<later>\s+que\s+viene|\s+proximo)?")
WEEKEND = re.compile(r"\b(?:(?:this|the|este|el)\s+|(?P<next>next|el\s+proximo)\s+)?"
                     r"(?:weekend|finde|fin\s+de\s+semana)\b(?P<later>\s+que\s+viene|\s+proximo)?")
NEXT_WEEK = re.compile(r"\b" + one_of(["next week", "la semana que viene", "la proxima semana",
                                       "la semana proxima", "the following week"]) + r"\b")
MONTH_END = re.compile(r"\b" + one_of(["end of the month", "the end of the month", "the end of this month",
                                       "end of this month", "end of month", "fin de mes", "final de mes",
                                       "finales de mes", "fin del mes", "final del mes"]) + r"\b")

# "now" words (SPEC 3.3, 3.4). "ya" counts only as the last word; "now" not in "from now on", "for now".
NOW_WORD = re.compile(r"\b(?P<w>right\s+now|ahora\s+mismo|now|ahora)\b")
NOT_NOW_BEFORE = re.compile(r"\b(?:from|for|until|till|by|just|por|hasta|desde|de|que)\s+$")
NOT_NOW_AFTER = re.compile(r"\s+(?:that|on|no|que)\b")
LAST_YA = re.compile(r"(?<![a-z])ya[\s.!]*$")

VAGUE = re.compile(r"(?<![a-z])(?P<w>" + one_of(VAGUE_WORDS) + r")(?![a-z])")
NOT_VAGUE_BEFORE = {"luego": re.compile(r"\b(?:desde|hasta)\s+$"),          # "desde luego" = of course
                    "some time": re.compile(r"\b(?:spend|spent|take|find|make|need|have|for)\s+$")}

LEFT_WORD = re.compile(r"\b(?P<w>" + one_of(LEFT_CONNECTORS) + r")\s+$")
RIGHT_WORD = re.compile(r"\s*,?\s*(?P<w>" + one_of(RIGHT_CONNECTORS) + r")\b")
JOINER = re.compile(r"\b(?:and|then|y|e)\b|[,;]")      # what links two tasks: "call mum at 5 and email Bob"
GLUE = re.compile(r"[\s,\-]*")                          # what may sit between two pieces of ONE time


# ---- 4. Pieces --------------------------------------------------------------------------------------------

USABLE = ("clock", "day", "part", "relative", "now")    # the kinds that can make a time
# The slots a kind fills. Two groups of pieces join into one time only if they fill different slots.
SLOTS = {"day": {"day"}, "clock": {"clock"}, "part": {"part"},
         "relative": {"day", "clock", "part"}, "now": {"day", "clock", "part"}}


@dataclass
class Piece:
    """One run of time words found in the message, and what it means."""
    kind: str                # clock, day, part, relative, now, vague, recurring, past or invalid
    start: int               # where the time words start and end in the text
    end: int
    left: int = 0            # the span to delete: the time words plus attached words like "by" or "el"
    right: int = 0
    hour: int = 0            # clock: the hour (am/pm already applied when exact)
    minute: int = 0          # clock: the minute
    exact: bool = False      # clock: the hour is certain (am/pm, 24-hour form, noon); False for a bare "at 9"
    next_day: bool = False   # clock: midnight or "24:00", which is 00:00 of the following day
    day: Optional[date] = None   # day: a fixed date ("tomorrow", "4 Nov", "in 2 days")
    weekday: int = -1            # day: 0 = Monday ... 6 = Sunday, for "Friday", "el viernes"
    skip_today: bool = False     # day: "next Friday" said on a Friday means a week later
    today: bool = False          # part: the person said it is for today ("tonight", "esta tarde")
    part: str = ""               # part: morning, afternoon, evening, night, early (madrugada), noon (mediodía)
    needs: str = ""              # part: only counts right after a "clock" or a "day" piece
    delta: timedelta = timedelta(0)  # relative: how long from now
    deadline: bool = False       # a deadline word is attached ("by", "antes de", "como tarde")
    code: str = ""               # invalid: the detail_code
    hint: str = ""               # invalid: the real weekday when a weekday and a date disagree


def clock_problem(hour: int, minute: int, meridiem: str) -> str:
    """The detail_code for a clock time that cannot exist, or "" if it is fine (SPEC 3.1 "Invalid")."""
    if hour > 24 or (hour == 24 and minute > 0):
        return "hour_out_of_range"
    if minute > 59:
        return "minute_out_of_range"
    if meridiem and not 1 <= hour <= 12:
        return "ampm_conflict"                  # 13pm, 0am
    return ""


def hour_with_day(hour: int) -> int:
    """A bare hour when the day is known: 1 to 7 means the afternoon, 8 to 12 the morning (SPEC 3.1)."""
    return hour + 12 if 1 <= hour <= 7 else hour


def with_part(clock: Piece, part: str) -> Piece:
    """A bare hour made exact by a part of the day: "5 de la tarde" is 17:00, "12 at night" is midnight."""
    hour, next_day = clock.hour, False
    if part == "early" and hour == 12:                 # "las 12 de la madrugada"
        hour = 0
    elif part in ("afternoon", "evening", "noon") and hour < 12:
        hour += 12
    elif part == "night":
        if hour == 12:
            hour, next_day = 0, True
        elif hour >= 6:
            hour += 12
        else:
            next_day = True                            # "2 at night" is in the small hours after midnight
    return replace(clock, hour=hour, exact=True, next_day=next_day)


def earlier(first: datetime, second: datetime) -> bool:
    """Is `first` really before `second`? Python compares two times of the same zone by their wall clock,
    which is wrong in the autumn hour that happens twice, so we compare real timestamps instead."""
    return first.timestamp() < second.timestamp()


def day_or_next(day: date, next_day: bool) -> date:
    """The day itself, or the day after it for a time like midnight that belongs to the next day."""
    return day + DAY if next_day else day


def amount(words: str) -> Optional[float]:
    """The number in "2", "1.5", "two", "a couple of", "half an"; None for "a few" (no exact number)."""
    words = squash(words)
    if words[0].isdigit():
        return float(words.replace(",", "."))
    if words in QUANTITY_WORDS:
        return QUANTITY_WORDS[words]
    return NUMBER_WORDS[words]


def number(words: str) -> int:
    """An hour or minute written in digits or in words ("9", "nine", "nueve")."""
    words = squash(words)
    return int(words) if words.isdigit() else NUMBER_WORDS[words]


def real_date(day: int, month: int) -> bool:
    """Can this day and month exist in some year? (31 February and 13/13 cannot; 29 February can.)"""
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2000, month)[1]


def make_date(year: int, month: int, day: int) -> Optional[date]:
    """date(year, month, day), or None if that day is not in that year's calendar."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def add_months(start: date, months: int) -> date:
    """The same day `months` months later, or that month's last day ("in a month" from 31 Jan is 28 Feb)."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def full_year(text: Optional[str]) -> Optional[int]:
    """'2026' -> 2026, '26' -> 2026, None -> None."""
    if not text:
        return None
    return int(text) + 2000 if len(text) == 2 else int(text)


def has_task_words(text: str) -> bool:
    """Is there a word here that could be a task ("call", "mum"), not just "remind me to"?"""
    return any(word not in NOT_TASK_WORDS for word in re.findall(r"[a-z]{2,}", text))


# ---- 5. The reader ------------------------------------------------------------------------------------------

def parse_time(text: str, now: datetime, tz: tzinfo, default_hour: int = DEFAULT_HOUR) -> TimeResult:
    """Read the time in `text`, said at `now` (aware, any zone) by a person living in `tz`.

    result.when is aware and in `tz`, and set only when result.status is "ok".
    """
    return Reader(text, now, tz, default_hour).read()


class Reader:
    """Reads one message: holds the text, the person's clock and the pieces found so far."""

    def __init__(self, text: str, now: datetime, tz: tzinfo, default_hour: int) -> None:
        self.text = text
        self.low, self.positions = normalise(text)    # every rule reads the copy; spans go back to the text
        self.tz = tz
        self.now = now.astimezone(tz)
        self.today = self.now.date()
        self.default_hour = default_hour
        self.pieces: List[Piece] = []
        self.taken = [False] * len(self.low)

    def read(self) -> TimeResult:
        """Find the pieces, pick the time (or the problem) and attach the spans."""
        self.find_pieces()
        candidates = self.problems_found()
        groups = self.time_groups()
        if len(groups) > 1 and self.two_tasks(groups):
            candidates.append(TimeResult("multiple"))
            candidates += [result for result in map(self.resolve, groups) if result.status == "past"]
        elif groups:
            candidates.append(self.resolve(self.combine(groups)))
        if any(piece.kind == "vague" for piece in self.pieces):
            candidates.append(TimeResult("vague"))
        result = min(candidates, key=lambda r: PRIORITY.index(r.status), default=TimeResult("none"))
        result.spans = self.spans()
        return result

    # -- finding pieces --

    def find_pieces(self) -> None:
        """Run every family of rules. The order matters: a rule cannot reuse what an earlier one took."""
        self.find_simple("recurring", REPEATING)
        self.find_simple("past", PAST)
        self.find_delays()
        self.find_dates()
        self.find_clocks()
        self.find_parts()
        self.find_days()
        self.find_now()
        self.find_vague()
        self.pieces.sort(key=lambda piece: piece.start)
        for piece in self.pieces:
            self.attach_connectors(piece)
        self.drop_lonely_parts()

    def take(self, piece: Piece) -> None:
        """Keep a piece, unless an earlier rule already used some of its characters."""
        if any(self.taken[piece.start:piece.end]):
            return
        for index in range(piece.start, piece.end):
            self.taken[index] = True
        piece.left, piece.right = piece.start, piece.end
        self.pieces.append(piece)

    def find_simple(self, kind: str, patterns: List[re.Pattern]) -> None:
        """Pieces whose meaning is just their kind: repeating words and words about the past."""
        for pattern in patterns:
            for match in pattern.finditer(self.low):
                self.take(Piece(kind, match.start(), match.end()))

    def find_delays(self) -> None:
        """'in 2 hours', 'en media hora', 'in a week'. Under a day: exact. A day or more: that day."""
        for pattern in (IN_DELAY, DELAY_FROM_NOW):
            for match in pattern.finditer(self.low):
                self.take(self.delay_piece(match))

    def delay_piece(self, match: re.Match) -> Piece:
        """The piece for one delay: exact under a day, a fixed date from a day on, vague for "a few days"."""
        start, end = match.start(), match.end()
        unit = UNIT_OF[match.group("unit")]
        count = amount(match.group("qty"))
        if count is None:                       # "a few": 5 minutes (SPEC 3.3); "a few days" is too vague
            if unit != "minute":
                return Piece("vague", start, end)
            count = 5
        for fraction in (match.group("frac1"), match.group("frac2")):
            if fraction:
                count += FRACTIONS[squash(fraction)]
        if unit == "month":
            return Piece("day", start, end, day=add_months(self.today, round(min(count, 14))))
        seconds = count * SECONDS_IN[unit]
        if match.group("qty2"):                 # "in 2 hours and 30 minutes"
            seconds += number(match.group("qty2")) * SECONDS_IN.get(UNIT_OF[match.group("unit2")], 0)
        if seconds > LONGEST_DELAY_DAYS * 86400:   # far beyond the limit: too far anyway, and huge numbers
            return Piece("day", start, end, day=self.today + timedelta(days=LONGEST_DELAY_DAYS))  # would overflow
        delta = timedelta(seconds=seconds)
        if delta < DAY:
            return Piece("relative", start, end, delta=delta)
        if unit in ("day", "week"):             # count calendar days, so a clock change cannot shift the date
            return Piece("day", start, end, day=self.today + timedelta(days=round(seconds / 86400)))
        return Piece("day", start, end, day=self.after(delta).date())

    def find_dates(self) -> None:
        """'4 Nov', 'Nov 4th', 'el 4 de noviembre', '4/11', '2026-11-04', 'on the 4th', 'el día 4'."""
        for pattern in (ISO_DATE, NUMERIC_DATE_WITH_YEAR, DAY_MONTH, MONTH_DAY, NUMERIC_DATE):
            for match in pattern.finditer(self.low):
                parts = match.groupdict()
                day = int(parts["d"])
                month = MONTHS[parts["mon"]] if parts.get("mon") else int(parts.get("mo") or 0)
                not_a_date = not (1 <= day <= 31 and 1 <= month <= 31) or match.group(0) == "24/7"
                if pattern is NUMERIC_DATE and not_a_date:
                    continue                    # "50/50" and "24/7" are no dates; "4/11" (or the impossible 13/13) is
                piece = self.date_piece(match.start(), match.end(), day, month, full_year(parts.get("y")))
                self.check_weekday_before(piece)
                self.take(piece)
        for pattern in (ORDINAL_DAY, DIA_DAY):
            for match in pattern.finditer(self.low):
                if pattern is ORDINAL_DAY and not ORDINAL_TRIGGER.search(self.low[:match.start()]):
                    continue                    # "the 2nd draft" is no date; "on the 2nd" is
                self.take(self.month_day_piece(match.start(), match.end(), int(match.group("d"))))
        for match in WEEKDAY_NUMBER.finditer(self.low):
            spanish = match.group("wd") in SPANISH_WEEKDAYS
            if not (spanish or match.group("suf") or match.group("the")):
                continue                        # "Friday 5" in English: the 5 is not clearly a date
            piece = self.month_day_piece(match.start(), match.end(), int(match.group("d")))
            self.check_weekday(piece, WEEKDAYS[match.group("wd")])
            self.take(piece)

    def date_piece(self, start: int, end: int, day: int, month: int, year: Optional[int]) -> Piece:
        """A day piece for a full date, or the problem with it (no such date, or a year that has gone)."""
        when = None
        if real_date(day, month):
            when = make_date(year, month, day) if year else self.next_date(day, month)
        if when is None:
            return Piece("invalid", start, end, code="no_such_date")
        if year and when < self.today:
            return Piece("past", start, end)
        return Piece("day", start, end, day=when)

    def next_date(self, day: int, month: int) -> Optional[date]:
        """A date without a year: the next time it comes round, today included."""
        for year in range(self.today.year, self.today.year + 5):     # five years always reach a 29 February
            when = make_date(year, month, day)
            if when and when >= self.today:
                return when
        return None

    def month_day_piece(self, start: int, end: int, day: int) -> Piece:
        """'on the 4th', 'el día 4': that day this month, or next month if it has passed."""
        year, month = self.today.year, self.today.month
        for _ in range(13):
            when = make_date(year, month, day)
            if when and when >= self.today:
                return Piece("day", start, end, day=when)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return Piece("invalid", start, end, code="no_such_date")

    def check_weekday_before(self, piece: Piece) -> None:
        """'Monday 5 Nov': take the weekday into the piece and check that it matches the date."""
        match = WEEKDAY_BEFORE.search(self.low[:piece.start])
        if match and not any(self.taken[match.start():piece.start]):
            piece.start = match.start()
            self.check_weekday(piece, WEEKDAYS[match.group("wd")])

    @staticmethod
    def check_weekday(piece: Piece, said: int) -> None:
        """Turn the piece into a weekday_mismatch problem if the date falls on another weekday."""
        if piece.kind == "day" and piece.day.weekday() != said:
            piece.kind, piece.code, piece.hint = "invalid", "weekday_mismatch", WEEKDAY_NAMES[piece.day.weekday()]

    def find_clocks(self) -> None:
        """Clock times, from the most explicit form to the loosest."""
        for match in COLON_TIME.finditer(self.low):                     # 20:00, 20.00, 8:30pm, 9:75
            hour = match.group("h")
            if match.group("sep") == "." and not (match.group("mer") or len(hour) == 2
                                                  or self.time_word_before(match.start())):
                continue                        # "costs 9.50" is money; "at 9.30" and "20.00" are times
            if not match.group("mer") and AMOUNT_AFTER.match(self.low, match.end()):
                continue
            self.add_clock(match, int(hour), int(match.group("m")), match.group("mer"),
                           zero_padded=hour.startswith("0") and len(hour) == 2)
        for match in AM_PM_TIME.finditer(self.low):                     # 8pm, 8 p.m., 13pm
            self.add_clock(match, int(match.group("h")), 0, match.group("mer"))
        for match in H_TIME.finditer(self.low):                         # 20h, 20h30 (but not "study 2h")
            hour = int(match.group("h"))
            if match.group("m") or (len(match.group("h")) == 2 and hour != 24) \
                    or self.time_word_before(match.start()):
                self.add_clock(match, hour, int(match.group("m") or 0))
        for match in PAST_TO_TIME.finditer(self.low):                   # half past 9, quarter to 9
            self.add_past_to_clock(match)
        for match in AT_TIME.finditer(self.low):                        # at 9, @ 5, at nine o'clock
            if match.group("h") == "one" and not match.group("tail"):
                continue                        # "at one point" is no time
            if not AMOUNT_AFTER.match(self.low, match.end()):
                self.add_clock(match, number(match.group("h")), 0)
        for match in LAS_TIME.finditer(self.low):                       # a las 9, a la una, a las 9 y media
            if match.group("trig").startswith("a") or BEFORE_BARE_LAS.search(self.low[:match.start()]):
                self.add_las_clock(match)
        for match in LOOSE_TIME.finditer(self.low):                     # 9ish, 9 o'clock, 9 en punto
            self.add_clock(match, number(match.group("h")), 0)
        for match in NUMBER_BEFORE_PART.finditer(self.low):             # 9 tonight, 5 de la tarde
            hour = int(match.group("h"))                                # (not "page 15 tonight")
            if 1 <= hour <= 12 and not LABEL_BEFORE.search(self.low[:match.start()]):
                self.add_clock(match, hour, int(match.group("m") or 0))
        for match in NOON_MIDNIGHT.finditer(self.low):                  # noon, mediodía, midnight
            hour = 24 if match.group("w") in ("midnight", "medianoche") else 12
            self.add_clock(match, hour, 0, exact=True)

    def time_word_before(self, position: int) -> bool:
        """Is there a word like "at", "a las" or "hasta" just before this position?"""
        return bool(TIME_WORD_BEFORE.search(self.low[:position]))

    def add_past_to_clock(self, match: re.Match) -> None:
        """'half past 9' is 9:30, 'quarter to 9' is 8:45. '5 to 9' is no time ("change 5 to 9")."""
        mins_text = squash(match.group("mins"))
        if match.group("dir") == "to" and mins_text.isdigit() and not match.group("word"):
            return
        minutes = 30 if mins_text == "half" else 15 if mins_text.endswith("quarter") else number(mins_text)
        hour = number(match.group("h"))
        if match.group("dir") == "to":
            hour, minutes = (hour - 1) or 12, 60 - minutes
        self.add_clock(match, hour, minutes)

    def add_las_clock(self, match: re.Match) -> None:
        """'a las 9 y media' 9:30, 'a las 9 y cuarto' 9:15, 'a las 9 menos cuarto' 8:45, 'las 9 y 20' 9:20."""
        hour, minutes = number(match.group("h")), 0
        extra = squash(match.group("extra") or "")
        if extra:
            sign, amount_text = extra.split(" ", 1)
            amount_text = amount_text.replace("minutos", "").replace("minuto", "").strip()
            minutes = {"media": 30, "cuarto": 15}.get(amount_text) or number(amount_text)
            if sign == "menos":
                hour, minutes = (hour - 1) or 12, 60 - minutes
        self.add_clock(match, hour, minutes)

    def add_clock(self, match: re.Match, hour: int, minute: int, meridiem: Optional[str] = None,
                  zero_padded: bool = False, exact: bool = False) -> None:
        """Check the numbers and keep a clock piece, or an invalid piece naming the problem."""
        code = clock_problem(hour, minute, meridiem or "")
        if code:
            self.take(Piece("invalid", match.start(), match.end(), code=code))
            return
        piece = Piece("clock", match.start(), match.end(), hour=hour, minute=minute)
        if meridiem:                            # 12am is 00:00, 12pm is 12:00, 8pm is 20:00
            piece.hour, piece.exact = hour % 12 + (12 if meridiem == "p" else 0), True
        elif hour == 24:                        # 24:00 and midnight: 00:00 of the next day
            piece.hour, piece.exact, piece.next_day = 0, True, True
        else:                                   # 20:00, 0:30 and 08:30 are certain; 9:30 is a bare hour
            piece.exact = exact or hour >= 13 or hour == 0 or zero_padded
        self.take(piece)

    def find_parts(self) -> None:
        """'tonight', 'this afternoon', 'por la tarde', 'de la noche', 'tomorrow morning'."""
        for match in PART_OF_DAY.finditer(self.low):
            phrase = squash(match.group("w"))
            needs = "clock" if phrase in CLOCK_PARTS else "day" if phrase in DAY_PARTS else ""
            self.take(Piece("part", match.start(), match.end(), part=ALL_PARTS[phrase], needs=needs,
                            today=phrase in TODAY_PARTS))

    def find_days(self) -> None:
        """'today', 'tomorrow', 'mañana', 'Friday', 'el próximo viernes', 'this weekend', 'next week'."""
        for match in DAY_WORD.finditer(self.low):
            word = squash(match.group("w"))
            if word == "manana" and re.search(r"\b(?:la|las)\s+$", self.low[:match.start()]):
                continue                        # "toda la mañana" is the morning, not tomorrow
            self.take(Piece("day", match.start(), match.end(), day=self.today + timedelta(days=DAYS_AHEAD[word])))
        for match in WEEKDAY_WORD.finditer(self.low):
            if self.weekday_is_clear(match):
                self.take(Piece("day", match.start(), match.end(), weekday=WEEKDAYS[match.group("wd")],
                                skip_today=bool(match.group("next") or match.group("later"))))
        for match in WEEKEND.finditer(self.low):
            self.take(Piece("day", match.start(), match.end(),
                            day=self.weekend(bool(match.group("next") or match.group("later")))))
        for match in NEXT_WEEK.finditer(self.low):                       # Monday of next week
            self.take(Piece("day", match.start(), match.end(),
                            day=self.today + timedelta(days=7 - self.today.weekday())))
        for match in MONTH_END.finditer(self.low):                       # last day of this month
            last = calendar.monthrange(self.today.year, self.today.month)[1]
            self.take(Piece("day", match.start(), match.end(), day=self.today.replace(day=last)))

    def weekday_is_clear(self, match: re.Match) -> bool:
        """'sun', 'sat', 'wed', 'mon' are also English words: they need 'on', 'next', 'this' or a number."""
        if match.group("wd") not in SHORT_DAYS_THAT_ARE_WORDS or match.group("this") or match.group("next"):
            return True
        before = self.low[:match.start()]
        return bool(re.search(r"\b(?:on|by|until|till|before|due|from|el)\s+$", before)
                    or re.match(r"\s+\d", self.low[match.end():]))

    def weekend(self, skip_this_one: bool) -> date:
        """'this weekend' is the coming Saturday, or today on a Saturday or Sunday."""
        weekday = self.today.weekday()
        if weekday < 5:
            return self.today + timedelta(days=5 - weekday)
        if skip_this_one:                       # "next weekend" said at the weekend
            return self.today + timedelta(days=12 - weekday)
        return self.today

    def find_now(self) -> None:
        """'now', 'right now', 'ahora', 'ahora mismo', and 'ya' as the last word: in 1 minute."""
        for match in NOW_WORD.finditer(self.low):
            clear = squash(match.group("w")) in ("right now", "ahora mismo")
            if clear or not (NOT_NOW_BEFORE.search(self.low[:match.start()])
                             or NOT_NOW_AFTER.match(self.low, match.end())):
                self.take(Piece("now", match.start(), match.end()))
        match = LAST_YA.search(self.low)
        if match:
            self.take(Piece("now", match.start(), match.start() + 2))

    def find_vague(self) -> None:
        """'at some point', 'soon', 'later', 'luego', 'en un rato': reported, and dropped from the title."""
        for match in VAGUE.finditer(self.low):
            word = squash(match.group("w"))
            if word == "later" and re.match(r"\s+than\b", self.low[match.end():]):
                continue                        # "no later than" is a deadline word, not a vague one
            not_before = NOT_VAGUE_BEFORE.get(word)
            if not_before and not_before.search(self.low[:match.start()]):
                continue
            self.take(Piece("vague", match.start(), match.end()))

    def attach_connectors(self, piece: Piece) -> None:
        """Grow the span over little words stuck to the time ('by', 'at', 'el', 'antes de', 'como tarde').
        Delays and "now" already hold their own first word ("in", "en"): "turn the oven on in 20 min"."""
        while piece.kind not in ("relative", "now"):
            match = LEFT_WORD.search(self.low[:piece.left])
            if not match or any(self.taken[match.start():piece.left]):
                break
            piece.left = match.start()
            piece.deadline = piece.deadline or squash(match.group("w")) in DEADLINE_WORDS
        match = RIGHT_WORD.match(self.low, piece.right)
        if match and not any(self.taken[piece.right:match.end()]):
            piece.right = match.end()
            piece.deadline = piece.deadline or squash(match.group("w")) in DEADLINE_WORDS
        for index in range(piece.left, piece.right):
            self.taken[index] = True

    def drop_lonely_parts(self) -> None:
        """'de la tarde' counts only right after a clock time, 'morning' only right after a day
        (or right after an impossible or past one: "a las 26 de la tarde", "yesterday morning")."""
        allowed_before = {"clock": ("clock", "invalid"), "day": ("day", "invalid", "past")}
        kept: List[Piece] = []
        for piece in self.pieces:
            if piece.kind == "part" and piece.needs:
                before = next((p for p in reversed(kept) if p.kind not in ("vague", "recurring")), None)
                if not (before and before.kind in allowed_before[piece.needs] and self.glued(before, piece)):
                    continue
                if before.kind in ("invalid", "past"):     # it belongs to the problem: only its span is kept
                    before.right = piece.right
                    continue
            kept.append(piece)
        self.pieces = kept

    # -- grouping --

    def glued(self, first: Piece, second: Piece) -> bool:
        """Only spaces, commas or vague words between two pieces: they are one time ('tomorrow, at 5')."""
        gap = "".join(char for index, char in enumerate(self.low[first.right:second.left], first.right)
                      if not self.inside_vague(index))
        return GLUE.fullmatch(gap) is not None

    def inside_vague(self, index: int) -> bool:
        """Is this position inside a vague word ("soon", "luego")?"""
        return any(p.kind == "vague" and p.left <= index < p.right for p in self.pieces)

    def time_groups(self) -> List[List[Piece]]:
        """Pieces that can make a time, grouped when they sit next to each other."""
        groups: List[List[Piece]] = []
        for piece in self.pieces:
            if piece.kind not in USABLE:
                continue
            if groups and self.glued(groups[-1][-1], piece):
                groups[-1].append(piece)
            else:
                groups.append([piece])
        return groups

    def two_tasks(self, groups: List[List[Piece]]) -> bool:
        """'call mum at 5 and email Bob tomorrow': two times joined by and/y/,/;/then, with a task on each side."""
        blank = self.blanked()
        for first, second in zip(groups, groups[1:]):
            gap_start = first[-1].right
            joiner = JOINER.search(self.low[gap_start:second[0].left])
            if joiner and has_task_words(blank[:gap_start + joiner.start()]) \
                    and has_task_words(blank[gap_start + joiner.end():]):
                return True
        return False

    def blanked(self) -> str:
        """The normalised text with every piece replaced by spaces: what is left is the task."""
        chars = list(self.low)
        for piece in self.pieces:
            chars[piece.left:piece.right] = " " * (piece.right - piece.left)
        return "".join(chars)

    @staticmethod
    def combine(groups: List[List[Piece]]) -> List[Piece]:
        """Join groups that complete each other ('tomorrow ... at 5'). If two say the same thing differently
        ('Friday ... Saturday'), keep only the first full time: a day with a time of day, else the first group
        (SPEC 3.5). So "hoy no, mañana a las 8" is tomorrow at 8."""
        def slots(group: List[Piece]) -> set:
            return set().union(*(SLOTS[piece.kind] for piece in group))

        merged: List[Piece] = []
        used: set = set()
        for group in groups:
            if slots(group) & used:
                full = [g for g in groups if "day" in slots(g) and slots(g) & {"clock", "part"}]
                return full[0] if full else groups[0]
            merged += group
            used |= slots(group)
        return merged

    def in_text(self, start: int, end: int) -> Span:
        """Positions in the copy -> positions in what the person typed. Accent marks typed as separate
        characters right after the last letter belong to it, so they go too."""
        text_start, text_end = self.positions[start], self.positions[end - 1] + 1
        while text_end < len(self.text) and is_skipped(self.text[text_end]):
            text_end += 1
        return text_start, text_end

    def words(self, start: int, end: int) -> str:
        """The person's own words between two positions of the copy (for `detail`)."""
        text_start, text_end = self.in_text(start, end)
        return self.text[text_start:text_end]

    def spans(self) -> List[Span]:
        """Every piece's span in the original text, sorted, with overlapping ones joined."""
        joined: List[List[int]] = []
        for left, right in sorted(self.in_text(piece.left, piece.right) for piece in self.pieces):
            if joined and left <= joined[-1][1]:
                joined[-1][1] = max(joined[-1][1], right)
            else:
                joined.append([left, right])
        return [(left, right) for left, right in joined]

    def problems_found(self) -> List[TimeResult]:
        """Pieces that are problems on their own: repeating, impossible, or in the past."""
        found = []
        for piece in self.pieces:
            words = self.words(piece.start, piece.end)
            if piece.kind == "recurring":
                found.append(TimeResult("recurring", detail=words))
            elif piece.kind == "invalid":
                found.append(TimeResult("invalid", detail=words, detail_code=piece.code, hint=piece.hint))
            elif piece.kind == "past":
                found.append(TimeResult("past", detail=words))
        return found

    # -- turning a group into a moment --

    def resolve(self, group: List[Piece]) -> TimeResult:
        """One group of pieces ('Friday' + 'at 5') becomes a moment; then the final checks (SPEC 3.4)."""
        result = self.moment(group)
        if result.status == "ok":
            if self.from_now(result.when) < -TOLERANCE:
                result = TimeResult("past")
            elif self.from_now(result.when) > MAX_AHEAD:
                result = TimeResult("too_far")
            else:
                result.deadline = any(piece.deadline for piece in group)
        if result.status == "past":
            result.detail = self.words(min(p.start for p in group), max(p.end for p in group))
        return result

    def moment(self, group: List[Piece]) -> TimeResult:
        """The moment one group means: a delay, or a day (maybe today) with a clock time or part of day."""
        def first(kind: str) -> Optional[Piece]:
            return next((piece for piece in group if piece.kind == kind), None)

        clock, part, day = first("clock"), first("part"), first("day")
        if first("now"):
            return self.ok(self.after(SOON), stated=True)
        if first("relative"):
            return self.ok(self.after(first("relative").delta), stated=True)
        if clock and part and not clock.exact:
            clock = with_part(clock, part.part)
        if day is None and part and part.today:
            return self.on_today(clock, part)
        if day is None:
            return self.clock_alone(clock) if clock else self.part_today(part, said_today=False)
        if day.weekday >= 0:
            return self.on_day(self.weekday_date(day, clock, part), clock, part)
        if day.day == self.today:
            return self.on_today(clock, part)
        return self.on_day(day.day, clock, part)

    def time_of_day(self, clock: Optional[Piece], part: Optional[Piece]) -> Tuple[int, int, bool]:
        """(hour, minute, next_day) when the day is known: the clock, else the part of day, else the default."""
        if clock:
            return (clock.hour if clock.exact else hour_with_day(clock.hour)), clock.minute, clock.next_day
        if part:
            return PART_HOURS[part.part][0], 0, False
        return self.default_hour, 0, False

    def weekday_date(self, day: Piece, clock: Optional[Piece], part: Optional[Piece]) -> date:
        """The next such weekday; today only if the time is still ahead and the person did not say 'next'."""
        hour, minute, next_day = self.time_of_day(clock, part)
        ahead = (day.weekday - self.today.weekday()) % 7
        if ahead == 0 and (day.skip_today or
                           not self.ahead(self.at(day_or_next(self.today, next_day), hour, minute))):
            ahead = 7
        return self.today + timedelta(days=ahead)

    def on_day(self, when: date, clock: Optional[Piece], part: Optional[Piece]) -> TimeResult:
        """A day other than today: the clock time, or the part of day, or the default hour (not stated)."""
        hour, minute, next_day = self.time_of_day(clock, part)
        return self.ok(self.at(day_or_next(when, next_day), hour, minute), stated=bool(clock or part))

    def on_today(self, clock: Optional[Piece], part: Optional[Piece]) -> TimeResult:
        """'today', 'hoy', 'tonight', 'today at 9': late by up to 15 minutes still counts (SPEC 3.4)."""
        if clock:
            hour, minute, next_day = self.time_of_day(clock, None)
            target = self.at(day_or_next(self.today, next_day), hour, minute)
            if self.from_now(target) >= -TOLERANCE:
                return self.ok(target, stated=True)
            if -self.from_now(target) <= LATE_GRACE:
                return self.ok(self.after(SOON), stated=True)
            return TimeResult("past")
        if part:
            return self.part_today(part, said_today=True)
        target = self.at(self.today, self.default_hour)
        if self.ahead(target):
            return self.ok(target, stated=False)
        return self.fallback(self.about_an_hour())

    def part_today(self, part: Piece, said_today: bool) -> TimeResult:
        """A part of day with no clock time ("tonight", "por la tarde"): today at its hour if still ahead.
        Said after its hour but before it ends: about an hour from now. After it ends: "past" if the person
        said today ("this morning"), else the same part tomorrow ("por la mañana" said at 15:00)."""
        first_hour, end_hour = PART_HOURS[part.part]
        target = self.at(self.today, first_hour)
        if self.ahead(target):
            return self.ok(target, stated=True)
        soon = self.about_an_hour()
        if earlier(soon, self.at(self.today, end_hour)):
            return self.fallback(soon)
        if said_today:                          # "this morning" said in the afternoon
            return TimeResult("past")
        return self.ok(self.at(self.today + DAY, first_hour), stated=True)   # "por la mañana" said at 15:00

    def clock_alone(self, clock: Piece) -> TimeResult:
        """A clock time with no day (SPEC 3.1 bare-hour rule and SPEC 3.4)."""
        if clock.next_day:                      # midnight: the coming one
            return self.ok(self.at(self.today + DAY, 0, 0), stated=True)
        if clock.exact:                         # 20:00, 9pm: today if still ahead, else tomorrow
            target = self.at(self.today, clock.hour, clock.minute)
            if not self.ahead(target):
                target = self.at(self.today + DAY, clock.hour, clock.minute)
            return self.ok(target, stated=True)
        for hour in (clock.hour, clock.hour + 12):   # "at 9": 09:00 or 21:00, the first still ahead in 07-22
            if FIRST_DAY_HOUR <= hour <= LAST_DAY_HOUR:
                target = self.at(self.today, hour, clock.minute)
                if self.ahead(target):
                    return self.ok(target, stated=True)
        return self.ok(self.at(self.today + DAY, hour_with_day(clock.hour), clock.minute), stated=True)

    def about_an_hour(self) -> datetime:
        """Now plus an hour, rounded up to the next 5 minutes (14:23 -> 15:25)."""
        later = self.after(timedelta(hours=1))
        seconds = later.minute * 60 + later.second + (1 if later.microsecond else 0)
        rounded_up = -(-seconds // 300) * 300            # up to a whole multiple of 300 s (5 minutes)
        top_of_hour = later.replace(minute=0, second=0, microsecond=0)
        return self.after(timedelta(seconds=rounded_up), start=top_of_hour)

    def fallback(self, soon: datetime) -> TimeResult:
        """A time Wazy chooses never lands after 22:00: then it is the default hour tomorrow (SPEC 7)."""
        if earlier(self.at(self.today, LAST_FALLBACK_HOUR), soon):
            return self.ok(self.at(self.today + DAY, self.default_hour), stated=False)
        return self.ok(soon, stated=False)

    # -- clock helpers --

    def from_now(self, moment: datetime) -> timedelta:
        """Real time from now until `moment` (negative if it has gone)."""
        return timedelta(seconds=moment.timestamp() - self.now.timestamp())

    def ahead(self, moment: datetime) -> bool:
        """Is `moment` still to come?"""
        return earlier(self.now, moment)

    @staticmethod
    def ok(when: datetime, stated: bool) -> TimeResult:
        """A usable time. `stated`: the person gave the time of day (or a short delay), not just a day."""
        return TimeResult("ok", when=when, stated=stated)

    def after(self, delta: timedelta, start: Optional[datetime] = None) -> datetime:
        """`start` (default: now) plus real elapsed time, in the person's zone (exact across clock changes)."""
        start = self.now if start is None else start
        return (start.astimezone(timezone.utc) + delta).astimezone(self.tz)

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        """`day` at hour:minute on the person's wall clock. A time skipped when clocks spring forward moves
        on to the first minute that exists (02:30 -> 03:00); a time that happens twice in autumn takes the first."""
        wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)   # fold=0: the first one
        for _ in range(240):
            round_trip = wall.astimezone(timezone.utc).astimezone(self.tz)
            if round_trip.replace(tzinfo=None) == wall.replace(tzinfo=None):
                return wall
            wall += timedelta(minutes=1)        # still in the gap: try the next minute
        return wall
