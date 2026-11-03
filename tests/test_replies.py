"""Tests for wazy/replies.py: every reply kind in English and Spanish, and describe_when (SPEC 2.1)."""
from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from wazy.intents import classify
from wazy.replies import ENGLISH, PARAMS, SPANISH, describe_when, render
from wazy.types import REPLY_KINDS

MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")

WHEN_TEXT = {"en": "tomorrow at 09:00", "es": "mañana a las 09:00"}
ITEMS = [(1, "Call mum", "tomorrow at 09:00"), (2, "Pay rent", "on Fri 6 Nov at 10:00"),
         (3, "Room 107 key", "on Thu 12 Nov at 10:00")]
EVIDENCE = {"current_hour": 10, "answered": 12, "answered_at_current": 5, "done_at_current": 1, "misses": 4,
            "best_hour": 18, "done_at_best": 4, "by_hour": {10: {"answered": 5, "done": 1}}}


def sample(kind: str, lang: str) -> dict:
    """Typical parameters for each kind (the exact names agreed in SPEC 2.1)."""
    when_text = WHEN_TEXT[lang]
    return {
        "welcome": {"ai": False},
        "help": {},
        "confirm": {"title": "Call mum", "when_text": when_text, "learned": False},
        "ask_time": {"title": "Sort out the gym thing"},
        "ask_what": {"when_text": when_text},
        "invalid_time": {"title": "Call mum", "detail": "25:00", "detail_code": "hour_out_of_range", "hint": ""},
        "past_time": {"title": "Call mum", "detail": "yesterday"},
        "too_far": {"title": "Renew passport"},
        "recurring_unsupported": {"title": "Take vitamins"},
        "multiple_commitments": {},
        "not_commitment": {"reason": "question"},
        "injection": {},
        "too_long": {"limit": 500},
        "stopped": {},
        "started": {},
        "done_ack": {"title": "Call mum"},
        "later_ack": {"title": "Call mum", "when_text": when_text},
        "no_open_nudge": {},
        "list": {"items": ITEMS},
        "list_empty": {},
        "cancelled": {"title": "Call mum"},
        "cancel_which": {"items": ITEMS},
        "nothing_to_cancel": {},
        "pending_cancelled": {"title": "Call mum"},
        "learning_question": {"current_hour": 10, "misses": 4, "answered": 12, "best_hour": 18, "done_at_best": 4},
        "learning_approved": {"hour": 18},
        "learning_rejected": {"hour": 10},
        "learning_explained": {"off": False, "learned_hour": None, "default_hour": 10, "done_at_current": 1,
                               "answered_at_current": 5, "evidence": EVIDENCE},
        "learning_forgotten": {"default_hour": 10},
        "learning_resumed": {},
        "rate_limited": {},
        "nudge": {"title": "Call mum", "due_text": ""},
    }[kind]


def every_variant():
    """(kind, lang, params) for every kind and the interesting variations of its parameters."""
    for lang in ("en", "es"):
        for kind in REPLY_KINDS:
            yield kind, lang, sample(kind, lang)
        yield "welcome", lang, {"ai": True}
        yield "confirm", lang, {"title": "Call mum", "when_text": WHEN_TEXT[lang], "learned": True}
        yield "ask_what", lang, {"when_text": ""}
        for code, detail, hint in [("hour_out_of_range", "25:00", ""), ("minute_out_of_range", "9:75", ""),
                                   ("ampm_conflict", "13pm", ""), ("no_such_date", "31 February", ""),
                                   ("weekday_mismatch", "Monday 5 Nov", "Thursday"),
                                   ("weekday_mismatch", "Monday 5 Nov", ""), ("something_new", "x", "")]:
            for title in ("Call mum", ""):
                yield "invalid_time", lang, {"title": title, "detail": detail, "detail_code": code, "hint": hint}
        for kind in ("ask_time", "too_far", "recurring_unsupported", "done_ack", "cancelled", "pending_cancelled"):
            yield kind, lang, {"title": ""}
        yield "past_time", lang, {"title": "", "detail": "today at 9"}
        yield "past_time", lang, {"title": "Call mum", "detail": ""}
        yield "later_ack", lang, {"title": "", "when_text": WHEN_TEXT[lang]}
        for reason in ("question", "edit", "limit"):
            yield "not_commitment", lang, {"reason": reason}
        yield "list", lang, {"items": ITEMS[:1]}
        yield "cancel_which", lang, {"items": []}
        yield "nudge", lang, {"title": "Call mum", "due_text": "09:00"}
        yield "nudge", lang, {"title": "", "due_text": "01:30"}
        yield "learning_rejected", lang, {"hour": 1}
        for off, learned, evidence in [(True, None, {}), (False, 18, EVIDENCE), (False, None, {}),
                                       (False, None, None)]:
            yield "learning_explained", lang, {"off": off, "learned_hour": learned, "default_hour": 10,
                                               "done_at_current": 1, "answered_at_current": 1,
                                               "evidence": evidence}


def at(year, month, day, hour, minute=0, second=0, tz=MADRID, fold=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=tz, fold=fold)


# ---- the table of kinds and parameters ---------------------------------------------------------

class TestKindsAndParameters(unittest.TestCase):
    def test_params_cover_exactly_the_reply_kinds(self):
        self.assertEqual(set(PARAMS), set(REPLY_KINDS))

    def test_english_has_a_sentence_for_every_kind(self):
        self.assertEqual(set(ENGLISH), set(REPLY_KINDS))

    def test_spanish_has_a_sentence_for_every_kind(self):
        self.assertEqual(set(SPANISH), set(REPLY_KINDS))

    def test_params_are_the_ones_agreed_in_the_spec(self):
        agreed = {
            "welcome": ["ai"], "help": [], "confirm": ["title", "when_text", "learned"], "ask_time": ["title"],
            "ask_what": ["when_text"], "invalid_time": ["title", "detail", "detail_code", "hint"],
            "past_time": ["title", "detail"], "too_far": ["title"], "recurring_unsupported": ["title"],
            "multiple_commitments": [], "not_commitment": ["reason"], "injection": [], "too_long": ["limit"],
            "stopped": [], "started": [], "done_ack": ["title"], "later_ack": ["title", "when_text"],
            "no_open_nudge": [], "list": ["items"], "list_empty": [], "cancelled": ["title"],
            "cancel_which": ["items"], "nothing_to_cancel": [], "pending_cancelled": ["title"],
            "learning_question": ["current_hour", "misses", "answered", "best_hour", "done_at_best"],
            "learning_approved": ["hour"], "learning_rejected": ["hour"],
            "learning_explained": ["off", "learned_hour", "default_hour", "done_at_current",
                                   "answered_at_current", "evidence"],
            "learning_forgotten": ["default_hour"], "learning_resumed": [], "rate_limited": [],
            "nudge": ["title", "due_text"],
        }
        self.assertEqual({kind: list(names) for kind, names in PARAMS.items()}, agreed)

    def test_every_sentence_function_takes_exactly_the_agreed_parameters(self):
        for lang, sentences in (("en", ENGLISH), ("es", SPANISH)):
            for kind, function in sentences.items():
                with self.subTest(lang=lang, kind=kind):
                    names = list(inspect.signature(function).parameters)
                    self.assertEqual(sorted(names), sorted(PARAMS[kind]))


# ---- every kind renders cleanly ----------------------------------------------------------------

class TestEveryKindRenders(unittest.TestCase):
    def check_clean(self, text, lang):
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        self.assertNotIn("{", text)
        self.assertNotIn("}", text)
        self.assertNotIn("—", text)      # em dash
        self.assertNotIn("–", text)      # en dash
        self.assertNotIn("“”", text)          # an empty title in quotes
        self.assertNotIn("None", text)
        self.assertNotIn("  ", text)          # no double spaces
        if lang == "en":
            self.assertNotIn(";", text)

    def test_every_kind_renders_in_english(self):
        for kind in REPLY_KINDS:
            with self.subTest(kind=kind):
                self.check_clean(render(kind, "en", **sample(kind, "en")), "en")

    def test_every_kind_renders_in_spanish(self):
        for kind in REPLY_KINDS:
            with self.subTest(kind=kind):
                self.check_clean(render(kind, "es", **sample(kind, "es")), "es")

    def test_every_variant_renders_cleanly(self):
        for kind, lang, params in every_variant():
            with self.subTest(kind=kind, lang=lang, params=params):
                self.check_clean(render(kind, lang, **params), lang)

    def test_english_and_spanish_differ_for_every_kind(self):
        for kind in REPLY_KINDS:
            with self.subTest(kind=kind):
                self.assertNotEqual(render(kind, "en", **sample(kind, "en")), render(kind, "es", **sample(kind, "es")))

    def test_titles_are_always_in_curly_quotes(self):
        for kind, lang, params in every_variant():
            title = params.get("title")
            if title:
                with self.subTest(kind=kind, lang=lang):
                    self.assertIn(f"“{title}”", render(kind, lang, **params))

    def test_curly_quotes_are_balanced(self):
        for kind, lang, params in every_variant():
            with self.subTest(kind=kind, lang=lang):
                text = render(kind, lang, **params)
                self.assertEqual(text.count("“"), text.count("”"))

    def test_spanish_questions_open_with_inverted_mark(self):
        for kind, lang, params in every_variant():
            if lang == "es":
                with self.subTest(kind=kind):
                    text = render(kind, lang, **params)
                    self.assertEqual(text.count("¿"), text.count("?"))


# ---- render() refuses bad calls ----------------------------------------------------------------

class TestRenderErrors(unittest.TestCase):
    def test_unknown_kind_raises_value_error(self):
        with self.assertRaises(ValueError):
            render("hello_there", "en")

    def test_unknown_language_raises_value_error(self):
        with self.assertRaises(ValueError):
            render("help", "fr")

    def test_language_must_be_lower_case_code(self):
        with self.assertRaises(ValueError):
            render("help", "EN")

    def test_missing_parameter_raises_type_error(self):
        with self.assertRaises(TypeError):
            render("confirm", "en", title="Call mum", learned=False)

    def test_no_parameters_for_a_kind_that_needs_them_raises(self):
        with self.assertRaises(TypeError):
            render("done_ack", "es")

    def test_extra_parameter_raises_type_error(self):
        with self.assertRaises(TypeError):
            render("done_ack", "en", title="Call mum", when_text="tomorrow at 09:00")

    def test_parameter_for_a_kind_without_parameters_raises(self):
        with self.assertRaises(TypeError):
            render("help", "en", title="Call mum")

    def test_misspelt_parameter_raises_type_error(self):
        with self.assertRaises(TypeError):
            render("nudge", "en", title="Call mum", due="09:00")

    def test_unknown_not_commitment_reason_raises_value_error(self):
        with self.assertRaises(ValueError):
            render("not_commitment", "en", reason="weather")

    def test_learning_question_needs_all_five_numbers(self):
        with self.assertRaises(TypeError):
            render("learning_question", "en", current_hour=10, misses=4, answered=12, best_hour=18)


# ---- what the replies must contain (SPEC 2.1) --------------------------------------------------

class TestConfirm(unittest.TestCase):
    def test_english_contains_title_and_when(self):
        text = render("confirm", "en", title="Submit the report", when_text="tomorrow at 20:00", learned=False)
        self.assertEqual(text, "Got it: “Submit the report”. I'll nudge you tomorrow at 20:00.")

    def test_spanish_contains_title_and_when(self):
        text = render("confirm", "es", title="Llamar a mamá", when_text="mañana a las 09:00", learned=False)
        self.assertEqual(text, "Apuntado: “Llamar a mamá”. Te aviso mañana a las 09:00.")
        self.assertFalse(text.startswith("Hecho"))      # "Hecho" is the Done button, not a confirmation

    def test_learned_time_is_marked_in_both_languages(self):
        en = render("confirm", "en", title="Call mum", when_text="tomorrow at 18:00", learned=True)
        es = render("confirm", "es", title="Llamar a mamá", when_text="mañana a las 18:00", learned=True)
        self.assertIn("tomorrow at 18:00 (your usual time).", en)
        self.assertIn("mañana a las 18:00 (tu hora habitual).", es)

    def test_stated_time_has_no_usual_time_note(self):
        self.assertNotIn("usual", render("confirm", "en", title="Call mum", when_text="today at 18:00",
                                         learned=False))


class TestAskingForATimeOrATask(unittest.TestCase):
    def test_ask_time_english_contains_title_and_examples(self):
        text = render("ask_time", "en", title="Sort out the gym thing")
        self.assertTrue(text.startswith("When should I nudge you about “Sort out the gym thing”?"))
        self.assertIn("For example", text)

    def test_ask_time_spanish_uses_cuando_te_aviso(self):
        text = render("ask_time", "es", title="Ordenar lo del gimnasio")
        self.assertTrue(text.startswith("¿Cuándo te aviso de “Ordenar lo del gimnasio”?"))
        self.assertIn("Por ejemplo", text)

    def test_ask_what_with_a_time_repeats_it(self):
        self.assertIn("tomorrow at 09:00", render("ask_what", "en", when_text="tomorrow at 09:00"))
        self.assertIn("mañana a las 09:00", render("ask_what", "es", when_text="mañana a las 09:00"))

    def test_ask_what_without_a_time_still_asks_with_an_example(self):
        en = render("ask_what", "en", when_text="")
        es = render("ask_what", "es", when_text="")
        self.assertIn("What should I nudge you about?", en)
        self.assertIn("“call the bank tomorrow at 9am”", en)
        self.assertIn("¿De qué quieres que te avise?", es)
        self.assertIn("por ejemplo “llamar al banco mañana a las 9”", es)


class TestInvalidTime(unittest.TestCase):
    def both(self, detail, code, hint="", title="Call mum"):
        params = {"title": title, "detail": detail, "detail_code": code, "hint": hint}
        return render("invalid_time", "en", **params), render("invalid_time", "es", **params)

    def test_hour_out_of_range(self):
        en, es = self.both("25:00", "hour_out_of_range")
        self.assertTrue(en.startswith("“25:00” isn't a real time."))
        self.assertTrue(es.startswith("“25:00” no es una hora válida."))
        for text in (en, es):
            self.assertIn("23:59", text)
            self.assertIn("“Call mum”", text)

    def test_minute_out_of_range(self):
        en, es = self.both("9:75", "minute_out_of_range")
        self.assertIn("“9:75” isn't a real time. Minutes only go up to 59.", en)
        self.assertIn("“9:75” no es una hora válida. Los minutos van del 00 al 59.", es)

    def test_ampm_conflict(self):
        en, es = self.both("13pm", "ampm_conflict")
        self.assertIn("“13pm” isn't a real time. With am or pm, the hour goes from 1 to 12.", en)
        self.assertIn("“13pm”", es)
        self.assertIn("del 1 al 12", es)

    def test_no_such_date(self):
        en, es = self.both("31 February", "no_such_date")
        self.assertIn("“31 February” isn't a real date.", en)
        self.assertIn("“31 February” no es una fecha válida. Ese día no existe.", es)

    def test_weekday_mismatch_names_the_real_day(self):
        en, es = self.both("Monday 5 Nov", "weekday_mismatch", hint="Thursday")
        self.assertIn("“Monday 5 Nov” doesn't match the calendar: that date is a Thursday.", en)
        self.assertIn("Thursday", en)

    def test_weekday_mismatch_in_spanish_says_the_day_in_spanish(self):
        text = render("invalid_time", "es", title="Llamar a Ana", detail="lunes 5 de noviembre",
                      detail_code="weekday_mismatch", hint="Thursday")
        self.assertIn("“lunes 5 de noviembre” no cuadra con el calendario. Esa fecha cae en jueves.", text)
        self.assertIn("“Llamar a Ana”", text)

    def test_weekday_mismatch_without_hint_still_explains(self):
        en, es = self.both("Monday 5 Nov", "weekday_mismatch", hint="")
        self.assertIn("different day of the week", en)
        self.assertIn("otro día de la semana", es)

    def test_unknown_code_still_names_the_words(self):
        en, es = self.both("half nine-ish", "something_new")
        self.assertIn("“half nine-ish” isn't a time I can use.", en)
        self.assertIn("“half nine-ish”", es)

    def test_every_code_asks_one_question_with_an_example(self):
        for code in ("hour_out_of_range", "minute_out_of_range", "ampm_conflict", "no_such_date",
                     "weekday_mismatch"):
            en, es = self.both("x", code, hint="Thursday")
            with self.subTest(code=code):
                self.assertEqual(en.count("?"), 1)
                self.assertEqual(es.count("?"), 1)
                self.assertTrue(en.endswith("For example “tomorrow at 9am”."))
                self.assertTrue(es.endswith("Por ejemplo “mañana a las 9”."))

    def test_without_a_title_asks_a_plain_question(self):
        en, es = self.both("25:00", "hour_out_of_range", title="")
        self.assertIn("When should I nudge you? For example", en)
        self.assertIn("¿Cuándo te aviso? Por ejemplo", es)


class TestOtherTimeProblems(unittest.TestCase):
    def test_past_time_english(self):
        text = render("past_time", "en", title="Call mum", detail="yesterday")
        self.assertTrue(text.startswith("“yesterday” has already passed."))
        self.assertIn("When should I nudge you about “Call mum”?", text)

    def test_past_time_spanish(self):
        text = render("past_time", "es", title="Llamar a mamá", detail="ayer")
        self.assertTrue(text.startswith("“ayer” ya ha pasado."))
        self.assertIn("¿Cuándo te aviso de “Llamar a mamá”?", text)

    def test_past_time_without_title_still_contains_detail(self):
        self.assertIn("“today at 9”", render("past_time", "en", title="", detail="today at 9"))
        self.assertIn("“hoy a las 9”", render("past_time", "es", title="", detail="hoy a las 9"))

    def test_too_far_contains_title_in_both_languages(self):
        en = render("too_far", "en", title="Renew passport")
        es = render("too_far", "es", title="Renovar el pasaporte")
        self.assertIn("“Renew passport”", en)
        self.assertIn("a year", en)
        self.assertIn("“Renovar el pasaporte”", es)
        self.assertIn("un año", es)

    def test_recurring_explains_one_nudge_and_contains_title(self):
        en = render("recurring_unsupported", "en", title="Take vitamins")
        es = render("recurring_unsupported", "es", title="Tomar vitaminas")
        self.assertIn("once per commitment", en)
        self.assertIn("“Take vitamins”", en)
        self.assertIn("“Take vitamins tomorrow at 9am”", en)
        self.assertIn("una vez por tarea", es)
        self.assertIn("“Tomar vitaminas”", es)
        self.assertIn("“Tomar vitaminas mañana a las 9”", es)

    def test_recurring_without_title(self):
        en = render("recurring_unsupported", "en", title="")
        self.assertIn("once per commitment", en)
        self.assertIn("When do you want the one nudge?", en)
        self.assertIn("like “call mum tomorrow at 9am”", en)

    def test_multiple_commitments_asks_for_one_at_a_time(self):
        en = render("multiple_commitments", "en")
        es = render("multiple_commitments", "es")
        self.assertIn("one at a time", en)
        self.assertEqual(en.count("?"), 1)
        self.assertIn("de una en una", es)
        self.assertEqual(es.count("?"), 1)


class TestNotACommitment(unittest.TestCase):
    def test_question_mentions_list_and_cancel(self):
        text = render("not_commitment", "en", reason="question")
        self.assertIn("“list”", text)
        self.assertIn("“cancel 2”", text)
        self.assertIn("I only take commitments", text)

    def test_edit_explains_cancel_and_send_again(self):
        text = render("not_commitment", "en", reason="edit")
        self.assertIn("“list”", text)
        self.assertIn("“cancel 2”", text)
        self.assertIn("cancel it and send it again", text)

    def test_limit_says_one_hundred(self):
        text = render("not_commitment", "en", reason="limit")
        self.assertIn("100", text)
        self.assertIn("“cancel 2”", text)

    def test_spanish_reasons_use_spanish_commands(self):
        for reason in ("question", "edit", "limit"):
            with self.subTest(reason=reason):
                text = render("not_commitment", "es", reason=reason)
                self.assertIn("“lista”", text)
                self.assertIn("“cancela 2”", text)
        self.assertIn("100", render("not_commitment", "es", reason="limit"))
        self.assertIn("mándamela otra vez", render("not_commitment", "es", reason="edit"))

    def test_injection_says_it_only_takes_commitments(self):
        en = render("injection", "en")
        es = render("injection", "es")
        self.assertTrue(en.startswith("I only take commitments"))
        self.assertIn("ignore instructions", en)
        self.assertTrue(es.startswith("Solo apunto tareas"))
        self.assertIn("ignoro las instrucciones", es)

    def test_too_long_names_the_limit(self):
        self.assertIn("500 characters", render("too_long", "en", limit=500))
        self.assertIn("500 caracteres", render("too_long", "es", limit=500))


class TestWelcomeAndHelp(unittest.TestCase):
    def test_welcome_without_ai_does_not_mention_ai(self):
        self.assertNotIn("AI", render("welcome", "en", ai=False))
        self.assertNotIn("IA", render("welcome", "es", ai=False))

    def test_welcome_with_ai_says_code_decides_the_time(self):
        en = render("welcome", "en", ai=True)
        es = render("welcome", "es", ai=True)
        self.assertIn("only tidies the wording", en)
        self.assertIn("Code decides the time.", en)
        self.assertIn("La hora la decide el código.", es)

    def test_welcome_explains_the_idea(self):
        en = render("welcome", "en", ai=False)
        self.assertTrue(en.startswith("Hi, I'm Wazy."))
        self.assertIn("Done or Later", en)
        self.assertIn("“help”", en)
        es = render("welcome", "es", ai=False)
        self.assertTrue(es.startswith("Hola, soy Wazy."))
        self.assertIn("“ayuda”", es)
        self.assertIn("Hecho o Más tarde", es)

    def test_help_lists_every_command_in_english(self):
        text = render("help", "en")
        for command in ("“list”", "“cancel 2”", "“stop”", "“start”", "“what did you learn”", "“forget”"):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_help_lists_every_command_in_spanish(self):
        text = render("help", "es")
        for command in ("“lista”", "“cancela 2”", "“para”", "“empezar”", "“qué has aprendido”", "“olvida”"):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_stopped_and_started(self):
        self.assertIn("“start”", render("stopped", "en"))
        self.assertIn("“empezar”", render("stopped", "es"))
        self.assertTrue(render("started", "en").startswith("You're back on."))
        self.assertIn("Mándame una tarea", render("started", "es"))


class TestNudgeAndAnswers(unittest.TestCase):
    def test_nudge_on_time(self):
        self.assertEqual(render("nudge", "en", title="Call mum", due_text=""), "⏰ Nudge: “Call mum”.")
        self.assertEqual(render("nudge", "es", title="Llamar a mamá", due_text=""), "⏰ Aviso: “Llamar a mamá”.")

    def test_late_nudge_says_when_it_was_due(self):
        self.assertEqual(render("nudge", "en", title="Call mum", due_text="09:00"),
                         "⏰ Nudge: “Call mum” (it was due at 09:00).")
        self.assertEqual(render("nudge", "es", title="Llamar a mamá", due_text="09:00"),
                         "⏰ Aviso: “Llamar a mamá” (era para las 09:00).")

    def test_late_nudge_spanish_uses_la_for_one_oclock(self):
        self.assertIn("(era para la 01:30)", render("nudge", "es", title="Llamar a mamá", due_text="01:30"))

    def test_done_ack_contains_title(self):
        self.assertEqual(render("done_ack", "en", title="Call mum"), "Nice, “Call mum” is done.")
        self.assertIn("“Llamar a mamá”", render("done_ack", "es", title="Llamar a mamá"))

    def test_later_ack_contains_title_and_time(self):
        en = render("later_ack", "en", title="Call mum", when_text="tomorrow at 10:00")
        es = render("later_ack", "es", title="Llamar a mamá", when_text="mañana a las 10:00")
        self.assertEqual(en, "No problem. I'll nudge you about “Call mum” once more tomorrow at 10:00.")
        self.assertEqual(es, "Vale. Te aviso otra vez de “Llamar a mamá” mañana a las 10:00.")

    def test_no_open_nudge(self):
        self.assertIn("no nudge waiting", render("no_open_nudge", "en"))
        self.assertIn("ningún aviso", render("no_open_nudge", "es"))

    def test_nudge_does_not_say_how_to_answer(self):
        # The chat shows the buttons, and whatsapp.py adds "Reply DONE or LATER" to a plain-text nudge,
        # so the nudge itself must not say it too.
        for lang, params in (("en", {"title": "Call mum", "due_text": "09:00"}),
                             ("es", {"title": "Llamar a mamá", "due_text": "09:00"})):
            text = render("nudge", lang, **params)
            for words in ("Done", "Later", "Hecho", "tarde", "Reply", "Responde", "Tap", "Toca"):
                with self.subTest(lang=lang, words=words):
                    self.assertNotIn(words, text)


class TestSameButtonNamesEverywhere(unittest.TestCase):
    """The buttons are "Done" / "Later" and "Hecho" / "Más tarde": in the chat, on WhatsApp and in every reply."""

    def test_spanish_never_calls_later_luego(self):
        for kind, lang, params in every_variant():
            if lang == "es":
                with self.subTest(kind=kind, params=params):
                    self.assertNotIn("Luego", render(kind, lang, **params))

    def test_replies_that_name_the_buttons_use_both_names(self):
        for kind, params in (("welcome", {"ai": False}), ("help", {}),
                             ("learning_explained", sample("learning_explained", "en"))):
            with self.subTest(kind=kind):
                self.assertIn("Done", render(kind, "en", **params))
                self.assertIn("Later", render(kind, "en", **params))
                self.assertIn("Hecho", render(kind, "es", **params))
                self.assertIn("Más tarde", render(kind, "es", **params))

    def test_the_button_words_are_understood_as_typed_answers(self):
        for words, intent in (("Done", "done"), ("Later", "later"), ("DONE", "done"), ("LATER", "later"),
                              ("Hecho", "done"), ("Más tarde", "later"), ("HECHO", "done"), ("MÁS TARDE", "later")):
            with self.subTest(words=words):
                self.assertEqual(classify(words).name, intent)

    def test_spanish_confirm_does_not_start_with_a_button_name(self):
        text = render("confirm", "es", title="Llamar a mamá", when_text="mañana a las 09:00", learned=False)
        self.assertFalse(text.startswith(("Hecho", "Más tarde")))


class TestCommandsWeTellPeopleToType(unittest.TestCase):
    """Every command a reply tells people to type is read as that command (wazy/intents.py)."""

    COMMANDS = {
        "en": [("list", "list"), ("cancel 2", "cancel"), ("cancel 1", "cancel"), ("stop", "stop"),
               ("start", "start"), ("help", "help"), ("what did you learn", "learn_what"),
               ("forget", "learn_forget"), ("learn again", "learn_resume")],
        "es": [("lista", "list"), ("cancela 2", "cancel"), ("cancela 1", "cancel"), ("para", "stop"),
               ("empezar", "start"), ("ayuda", "help"), ("qué has aprendido", "learn_what"),
               ("olvida", "learn_forget"), ("vuelve a aprender", "learn_resume")],
    }

    def test_quoted_commands_are_commands(self):
        for lang, commands in self.COMMANDS.items():
            for words, intent in commands:
                with self.subTest(lang=lang, words=words):
                    self.assertEqual(classify(words).name, intent)

    def test_every_command_in_help_is_one_we_checked(self):
        for lang, commands in self.COMMANDS.items():
            text = render("help", lang)
            quoted = {part.split("”")[0] for part in text.split("“")[1:]}
            examples = {"call the bank tomorrow at 9am", "llamar al banco mañana a las 9"}
            with self.subTest(lang=lang):
                self.assertLessEqual(quoted - examples, {words for words, _ in commands})

    def test_yes_and_no_are_understood_in_the_learning_question(self):
        for words, intent in (("YES", "yes"), ("NO", "no"), ("SÍ", "yes")):
            with self.subTest(words=words):
                self.assertEqual(classify(words, learning_question=True).name, intent)


class TestSpanishStyle(unittest.TestCase):
    def test_tu_not_usted(self):
        for kind, lang, params in every_variant():
            if lang == "es":
                with self.subTest(kind=kind):
                    self.assertNotIn("usted", render(kind, lang, **params).lower())

    def test_one_oclock_takes_la(self):
        for kind, lang, params in every_variant():
            if lang == "es":
                with self.subTest(kind=kind, params=params):
                    self.assertNotIn("las 01:", render(kind, lang, **params))


class TestListAndCancel(unittest.TestCase):
    def test_list_english_numbered_lines(self):
        text = render("list", "en", items=ITEMS)
        self.assertIn("\n1. “Call mum”, tomorrow at 09:00\n", text)
        self.assertIn("\n2. “Pay rent”, on Fri 6 Nov at 10:00\n", text)
        self.assertIn("\n3. “Room 107 key”, on Thu 12 Nov at 10:00\n", text)
        self.assertIn("“cancel 1”", text)

    def test_list_spanish_numbered_lines(self):
        items = [(1, "Llamar a mamá", "mañana a las 09:00"), (2, "Pagar el alquiler", "el viernes a la 01:30")]
        text = render("list", "es", items=items)
        self.assertIn("\n1. “Llamar a mamá”, mañana a las 09:00\n", text)
        self.assertIn("\n2. “Pagar el alquiler”, el viernes a la 01:30\n", text)
        self.assertIn("“cancela 1”", text)

    def test_list_keeps_the_given_numbers(self):
        text = render("list", "en", items=[(4, "Call mum", "today at 18:00")])
        self.assertIn("4. “Call mum”, today at 18:00", text)
        self.assertIn("“cancel 4”", text)

    def test_list_accepts_lists_as_well_as_tuples(self):
        text = render("list", "en", items=[[1, "Call mum", "tomorrow at 09:00"]])
        self.assertIn("1. “Call mum”, tomorrow at 09:00", text)

    def test_cancel_which_contains_every_title_in_both_languages(self):
        for lang in ("en", "es"):
            text = render("cancel_which", lang, items=ITEMS)
            for n, title, when_text in ITEMS:
                with self.subTest(lang=lang, title=title):
                    self.assertIn(f"{n}. “{title}”, {when_text}", text)

    def test_cancel_which_asks_which_one(self):
        self.assertTrue(render("cancel_which", "en", items=ITEMS).startswith("Which one should I cancel?"))
        self.assertTrue(render("cancel_which", "es", items=ITEMS).startswith("¿Cuál cancelo?"))

    def test_list_empty_and_nothing_to_cancel(self):
        self.assertIn("Nothing coming up", render("list_empty", "en"))
        self.assertIn("No tienes nada pendiente", render("list_empty", "es"))
        self.assertIn("nothing to cancel", render("nothing_to_cancel", "en"))
        self.assertIn("nada que cancelar", render("nothing_to_cancel", "es"))

    def test_cancelled_contains_title(self):
        self.assertEqual(render("cancelled", "en", title="Call mum"), "Cancelled “Call mum”. I won't nudge you about it.")
        self.assertIn("“Llamar a mamá”", render("cancelled", "es", title="Llamar a mamá"))

    def test_pending_cancelled_without_title(self):
        self.assertEqual(render("pending_cancelled", "en", title=""), "OK, dropped it.")
        self.assertEqual(render("pending_cancelled", "es", title=""), "Vale, olvidado.")

    def test_pending_cancelled_with_title(self):
        self.assertEqual(render("pending_cancelled", "en", title="Call mum"), "OK, I've dropped “Call mum”.")
        self.assertIn("“Llamar a mamá”", render("pending_cancelled", "es", title="Llamar a mamá"))


class TestLearningReplies(unittest.TestCase):
    def test_question_english_has_the_numbers(self):
        text = render("learning_question", "en", current_hour=10, misses=4, answered=12, best_hour=18,
                      done_at_best=3)
        self.assertIn("I nudge you at 10:00", text)
        self.assertIn("Of your last 12 nudges, 4 at 10:00 got Later or no reply", text)
        self.assertIn("ticked 3 things off around 18:00", text)
        self.assertIn("Shall I use 18:00 from now on?", text)
        self.assertIn("YES or NO", text)

    def test_question_spanish_has_the_numbers(self):
        text = render("learning_question", "es", current_hour=10, misses=4, answered=12, best_hour=13,
                      done_at_best=3)
        self.assertIn("te aviso a las 10:00", text)
        self.assertIn("De tus últimos 12 avisos, 4 fueron a las 10:00 y acabaron en Más tarde o sin respuesta", text)
        self.assertIn("has respondido Hecho 3 veces sobre las 13:00", text)
        self.assertIn("¿Te aviso a las 13:00 a partir de ahora?", text)
        self.assertIn("SÍ o NO", text)

    def test_approved_and_rejected_state_the_hour(self):
        self.assertIn("at 18:00", render("learning_approved", "en", hour=18))
        self.assertIn("a las 18:00", render("learning_approved", "es", hour=18))
        self.assertIn("I'll keep 10:00", render("learning_rejected", "en", hour=10))
        self.assertIn("me quedo con las 10:00", render("learning_rejected", "es", hour=10))
        self.assertIn("two weeks", render("learning_rejected", "en", hour=10))
        self.assertIn("dos semanas", render("learning_rejected", "es", hour=10))

    def test_explained_when_off(self):
        en = render("learning_explained", "en", off=True, learned_hour=None, default_hour=10, done_at_current=0,
                    answered_at_current=0, evidence={})
        es = render("learning_explained", "es", off=True, learned_hour=None, default_hour=10, done_at_current=0,
                    answered_at_current=0, evidence={})
        self.assertIn("not learning anything", en)
        self.assertIn("“learn again”", en)
        self.assertIn("no estoy aprendiendo nada", es)
        self.assertIn("“vuelve a aprender”", es)

    def test_explained_with_a_learned_hour(self):
        en = render("learning_explained", "en", off=False, learned_hour=18, default_hour=10, done_at_current=4,
                    answered_at_current=5, evidence=EVIDENCE)
        es = render("learning_explained", "es", off=False, learned_hour=18, default_hour=10, done_at_current=4,
                    answered_at_current=5, evidence=EVIDENCE)
        self.assertIn("at 18:00, because you said YES", en)
        self.assertIn("Done to 4 of 5 nudges at that hour", en)
        self.assertIn("a las 18:00, porque me dijiste que SÍ", es)
        self.assertIn("Hecho a 4 de 5 avisos a esa hora", es)
        self.assertNotIn("most often", en)    # best hour is the hour in use, so it is not repeated

    def test_explained_with_the_default_hour_shows_the_best_hour(self):
        en = render("learning_explained", "en", off=False, learned_hour=None, default_hour=10, done_at_current=1,
                    answered_at_current=5, evidence=EVIDENCE)
        es = render("learning_explained", "es", off=False, learned_hour=None, default_hour=10, done_at_current=1,
                    answered_at_current=5, evidence=EVIDENCE)
        self.assertIn("at 10:00 (my default)", en)
        self.assertIn("Done to 1 of 5 nudges", en)
        self.assertIn("most often around 18:00 (4 times)", en)
        self.assertIn("“forget”", en)
        self.assertIn("a las 10:00 (mi hora por defecto)", es)
        self.assertIn("sobre las 18:00 (4 veces)", es)
        self.assertIn("“olvida”", es)

    def test_explained_counts_use_singular_for_one(self):
        en = render("learning_explained", "en", off=False, learned_hour=None, default_hour=10, done_at_current=1,
                    answered_at_current=1, evidence={"best_hour": 18, "done_at_best": 1})
        self.assertIn("Done to 1 of 1 nudge at that hour", en)
        self.assertIn("(1 time)", en)

    def test_explained_with_no_evidence(self):
        text = render("learning_explained", "en", off=False, learned_hour=None, default_hour=10, done_at_current=0,
                      answered_at_current=0, evidence={})
        self.assertIn("I don't have any answers at that hour yet.", text)
        self.assertNotIn("0 of 0", text)
        self.assertNotIn("most often", text)

    def test_forgotten_and_resumed(self):
        self.assertIn("I'll nudge you at 10:00 again", render("learning_forgotten", "en", default_hour=10))
        self.assertIn("te avisaré otra vez a las 10:00", render("learning_forgotten", "es", default_hour=10))
        self.assertIn("“vuelve a aprender”", render("learning_forgotten", "es", default_hour=10))
        self.assertIn("ask before changing anything", render("learning_resumed", "en"))
        self.assertIn("antes de cambiar nada", render("learning_resumed", "es"))

    def test_forget_promises_only_what_it_does(self):
        # learning.forget() drops the learned hour and stops learning; nudge outcomes are kept (SPEC promise 3:
        # nothing is deleted). No text may say "forget" wipes the record of answers.
        for learned in (None, 18):
            en = render("learning_explained", "en", off=False, learned_hour=learned, default_hour=10,
                        done_at_current=1, answered_at_current=5, evidence=EVIDENCE)
            es = render("learning_explained", "es", off=False, learned_hour=learned, default_hour=10,
                        done_at_current=1, answered_at_current=5, evidence=EVIDENCE)
            with self.subTest(learned=learned):
                self.assertIn("Text “forget” and I'll drop any hour I learned and stop learning. "
                              "Your commitments stay as they are.", en)
                self.assertIn("Si escribes “olvida”, quito la hora que haya aprendido y dejo de aprender. "
                              "Tus tareas siguen igual.", es)
        en_forgotten = render("learning_forgotten", "en", default_hour=10)
        es_forgotten = render("learning_forgotten", "es", default_hour=10)
        self.assertTrue(en_forgotten.startswith("OK, I've dropped any hour I learned."))
        self.assertIn("Your commitments stay as they are.", en_forgotten)
        self.assertTrue(es_forgotten.startswith("Vale, ya no uso ninguna hora aprendida."))
        self.assertIn("Tus tareas siguen igual.", es_forgotten)
        for kind, lang, params in every_variant():
            if kind in ("help", "learning_explained", "learning_forgotten"):
                text = render(kind, lang, **params).lower()
                with self.subTest(kind=kind, lang=lang):
                    self.assertNotIn("wipe", text)
                    self.assertNotIn("borr", text)

    def test_rate_limited(self):
        self.assertIn("a minute", render("rate_limited", "en"))
        self.assertIn("un minuto", render("rate_limited", "es"))


# ---- describe_when (SPEC 2.1 table) -------------------------------------------------------------

class TestDescribeWhenTable(unittest.TestCase):
    def check(self, when, now, english, spanish):
        self.assertEqual(describe_when(when, now, "en"), english)
        self.assertEqual(describe_when(when, now, "es"), spanish)

    def test_under_two_minutes_shows_seconds(self):
        self.check(at(2026, 11, 3, 9, 30, 30), at(2026, 11, 3, 9, 30, 0),
                   "at 09:30:30 (in 30 s)", "a las 09:30:30 (en 30 s)")

    def test_under_an_hour_shows_minutes(self):
        self.check(at(2026, 11, 3, 9, 45), at(2026, 11, 3, 9, 30),
                   "at 09:45 (in 15 min)", "a las 09:45 (en 15 min)")

    def test_same_day(self):
        self.check(at(2026, 11, 3, 18, 0), at(2026, 11, 3, 9, 0), "today at 18:00", "hoy a las 18:00")

    def test_next_day(self):
        self.check(at(2026, 11, 4, 9, 0), at(2026, 11, 3, 9, 0), "tomorrow at 09:00", "mañana a las 09:00")

    def test_within_a_week(self):
        # 3 Nov 2026 is a Tuesday, so 6 Nov is a Friday.
        self.check(at(2026, 11, 6, 10, 0), at(2026, 11, 3, 9, 0), "on Friday at 10:00", "el viernes a las 10:00")

    def test_six_days_ahead_is_still_a_weekday_name(self):
        self.check(at(2026, 11, 9, 10, 0), at(2026, 11, 3, 9, 0), "on Monday at 10:00", "el lunes a las 10:00")

    def test_later_shows_the_date(self):
        self.check(at(2026, 11, 5, 10, 0), at(2026, 10, 27, 9, 0),
                   "on Thu 5 Nov at 10:00", "el jueves 5 de noviembre a las 10:00")

    def test_seven_days_ahead_shows_the_date(self):
        self.check(at(2026, 11, 10, 10, 0), at(2026, 11, 3, 9, 0),
                   "on Tue 10 Nov at 10:00", "el martes 10 de noviembre a las 10:00")

    def test_another_year_shows_the_year(self):
        # 7 Jan 2027 is a Thursday.
        self.check(at(2027, 1, 7, 10, 0), at(2026, 12, 28, 9, 0),
                   "on Thu 7 Jan 2027 at 10:00", "el jueves 7 de enero de 2027 a las 10:00")

    def test_next_year_within_a_week_uses_the_weekday(self):
        # 1 Jan 2027 is a Friday.
        self.check(at(2027, 1, 1, 10, 0), at(2026, 12, 30, 9, 0), "on Friday at 10:00", "el viernes a las 10:00")

    def test_new_years_eve_to_new_years_day_is_tomorrow(self):
        self.check(at(2027, 1, 1, 9, 0), at(2026, 12, 31, 20, 0), "tomorrow at 09:00", "mañana a las 09:00")

    def test_far_date_in_september_uses_short_month(self):
        self.assertEqual(describe_when(at(2027, 9, 15, 10, 0), at(2027, 1, 4, 9, 0), "en"),
                         "on Wed 15 Sep at 10:00")
        self.assertEqual(describe_when(at(2027, 9, 15, 10, 0), at(2027, 1, 4, 9, 0), "es"),
                         "el miércoles 15 de septiembre a las 10:00")

    def test_saturday_in_spanish_keeps_its_accent(self):
        self.assertEqual(describe_when(at(2026, 11, 7, 10, 0), at(2026, 11, 3, 9, 0), "es"),
                         "el sábado a las 10:00")


class TestDescribeWhenRounding(unittest.TestCase):
    NOW = at(2026, 11, 3, 9, 30, 0)

    def test_seconds_round_to_nearest(self):
        when = self.NOW + timedelta(seconds=29, milliseconds=600)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:30:29 (in 30 s)")

    def test_seconds_round_half_up(self):
        when = self.NOW + timedelta(seconds=30, milliseconds=500)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:30:30 (in 31 s)")

    def test_seconds_minimum_is_one(self):
        when = self.NOW + timedelta(milliseconds=200)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:30:00 (in 1 s)")
        self.assertEqual(describe_when(when, self.NOW, "es"), "a las 09:30:00 (en 1 s)")

    def test_a_time_just_behind_now_still_says_one_second(self):
        when = self.NOW - timedelta(seconds=3)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:29:57 (in 1 s)")

    def test_119_seconds_still_shows_seconds(self):
        when = self.NOW + timedelta(seconds=119)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:31:59 (in 119 s)")

    def test_120_seconds_switches_to_minutes(self):
        when = self.NOW + timedelta(seconds=120)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:32 (in 2 min)")
        self.assertEqual(describe_when(when, self.NOW, "es"), "a las 09:32 (en 2 min)")

    def test_minutes_round_to_nearest(self):
        self.assertEqual(describe_when(self.NOW + timedelta(minutes=14, seconds=29), self.NOW, "en"),
                         "at 09:44 (in 14 min)")
        self.assertEqual(describe_when(self.NOW + timedelta(minutes=14, seconds=30), self.NOW, "en"),
                         "at 09:44 (in 15 min)")

    def test_seconds_are_not_shown_after_two_minutes(self):
        when = self.NOW + timedelta(minutes=5, seconds=10)
        self.assertEqual(describe_when(when, self.NOW, "en"), "at 09:35 (in 5 min)")

    def test_exactly_one_hour_is_today(self):
        self.assertEqual(describe_when(self.NOW + timedelta(hours=1), self.NOW, "en"), "today at 10:30")


class TestDescribeWhenMidnightAndZones(unittest.TestCase):
    def test_crossing_midnight_within_the_hour_shows_minutes(self):
        self.assertEqual(describe_when(at(2026, 11, 4, 0, 10), at(2026, 11, 3, 23, 50), "en"),
                         "at 00:10 (in 20 min)")
        self.assertEqual(describe_when(at(2026, 11, 4, 0, 10), at(2026, 11, 3, 23, 50), "es"),
                         "a las 00:10 (en 20 min)")

    def test_crossing_midnight_after_an_hour_is_tomorrow(self):
        self.assertEqual(describe_when(at(2026, 11, 4, 0, 30), at(2026, 11, 3, 23, 0), "en"), "tomorrow at 00:30")
        self.assertEqual(describe_when(at(2026, 11, 4, 0, 30), at(2026, 11, 3, 23, 0), "es"),
                         "mañana a las 00:30")

    def test_spanish_uses_a_la_for_one_oclock_in_every_row(self):
        now = at(2026, 11, 3, 0, 10)
        self.assertEqual(describe_when(at(2026, 11, 3, 1, 30), now, "es"), "hoy a la 01:30")
        self.assertEqual(describe_when(at(2026, 11, 4, 1, 30), now, "es"), "mañana a la 01:30")
        self.assertEqual(describe_when(at(2026, 11, 6, 1, 30), now, "es"), "el viernes a la 01:30")
        self.assertEqual(describe_when(at(2026, 11, 20, 1, 30), now, "es"), "el viernes 20 de noviembre a la 01:30")
        self.assertEqual(describe_when(at(2026, 11, 3, 1, 30, 30), at(2026, 11, 3, 1, 30), "es"),
                         "a la 01:30:30 (en 30 s)")
        self.assertEqual(describe_when(at(2026, 11, 3, 1, 45), at(2026, 11, 3, 1, 30), "es"),
                         "a la 01:45 (en 15 min)")

    def test_spanish_uses_a_las_for_other_hours(self):
        now = at(2026, 11, 3, 0, 0)
        self.assertEqual(describe_when(at(2026, 11, 3, 13, 30), now, "es"), "hoy a las 13:30")
        self.assertEqual(describe_when(at(2026, 11, 4, 0, 30), at(2026, 11, 3, 22, 0), "es"),
                         "mañana a las 00:30")
        self.assertEqual(describe_when(at(2026, 11, 3, 10, 15), now, "es"), "hoy a las 10:15")

    def test_english_has_no_special_case_for_one_oclock(self):
        self.assertEqual(describe_when(at(2026, 11, 4, 1, 30), at(2026, 11, 3, 22, 0), "en"), "tomorrow at 01:30")

    def test_when_in_london_now_in_madrid_uses_london_day(self):
        # 00:30 in Madrid is still 23:30 the day before in London, so 09:00 London is "tomorrow".
        when = at(2026, 11, 4, 9, 0, tz=LONDON)
        now = at(2026, 11, 4, 0, 30, tz=MADRID)
        self.assertEqual(describe_when(when, now, "en"), "tomorrow at 09:00")
        self.assertEqual(describe_when(when, now, "es"), "mañana a las 09:00")

    def test_when_in_london_is_shown_on_the_london_clock(self):
        # 09:30 in Madrid is 08:30 in London: 09:00 London is 30 minutes away.
        when = at(2026, 11, 4, 9, 0, tz=LONDON)
        now = at(2026, 11, 4, 9, 30, tz=MADRID)
        self.assertEqual(describe_when(when, now, "en"), "at 09:00 (in 30 min)")

    def test_spring_forward_counts_real_minutes(self):
        # 28 Mar 2027 in Madrid: 02:00 jumps to 03:00, so 01:30 to 03:00 is only 30 real minutes.
        self.assertEqual(describe_when(at(2027, 3, 28, 3, 0), at(2027, 3, 28, 1, 30), "en"), "at 03:00 (in 30 min)")

    def test_fall_back_counts_real_minutes(self):
        # 25 Oct 2026 in Madrid: 02:50 (summer time) to 02:20 (winter time, second time round) is 30 minutes.
        when = at(2026, 10, 25, 2, 20, fold=1)
        now = at(2026, 10, 25, 2, 50, fold=0)
        self.assertEqual(describe_when(when, now, "en"), "at 02:20 (in 30 min)")


class TestDescribeWhenErrors(unittest.TestCase):
    def test_unknown_language_raises(self):
        with self.assertRaises(ValueError):
            describe_when(at(2026, 11, 4, 9), at(2026, 11, 3, 9), "fr")

    def test_naive_when_raises(self):
        with self.assertRaises(ValueError):
            describe_when(datetime(2026, 11, 4, 9), at(2026, 11, 3, 9), "en")

    def test_naive_now_raises(self):
        with self.assertRaises(ValueError):
            describe_when(at(2026, 11, 4, 9), datetime(2026, 11, 3, 9), "en")

    def test_result_works_inside_confirm(self):
        when_text = describe_when(at(2026, 11, 4, 20, 0), at(2026, 11, 3, 8, 55), "en")
        self.assertEqual(render("confirm", "en", title="Submit the report", when_text=when_text, learned=False),
                         "Got it: “Submit the report”. I'll nudge you tomorrow at 20:00.")


if __name__ == "__main__":
    unittest.main()
