"""Tests for wazy/lang.py: English or Spanish (SPEC section 5)."""
from __future__ import annotations

import unittest

from wazy.lang import detect_language, language_scores, strip_accents


class TestDetectLanguage(unittest.TestCase):
    # (text, expected language when the fallback is "en", expected when the fallback is "es")
    CASES = [
        # empty or no clue at all: the fallback wins
        ("", "en", "es"),
        ("   ", "en", "es"),
        ("ok", "en", "es"),
        ("✅", "en", "es"),
        ("12:30", "en", "es"),
        ("done", "en", "es"),              # English command words are not clues (the Done button)
        ("stop", "en", "es"),
        ("hello", "en", "es"),
        ("y", "en", "es"),                 # alone, "y" is the English "yes", not Spanish "and"
        # single Spanish command words count as Spanish
        ("hola", "es", "es"),
        ("Hola!", "es", "es"),
        ("HOLA", "es", "es"),
        ("listo", "es", "es"),
        ("hecho", "es", "es"),
        ("Hecho", "es", "es"),
        ("ayuda", "es", "es"),
        ("para", "es", "es"),
        ("Para.", "es", "es"),
        ("luego", "es", "es"),
        ("sí", "es", "es"),
        ("si", "es", "es"),
        ("vale", "es", "es"),
        ("gracias", "es", "es"),
        ("basta", "es", "es"),
        ("más tarde", "es", "es"),
        ("mañana", "es", "es"),
        ("olvídalo", "es", "es"),
        # whole sentences
        ("Remind me to call mum tomorrow", "en", "en"),
        ("Send the slides to Marta by Thursday", "en", "en"),
        ("what do I have", "en", "en"),
        ("Buy bread", "en", "en"),
        ("Call Ana at 5", "en", "en"),
        ("tomorrow", "en", "en"),
        ("at 9", "en", "en"),
        ("Email the landlord on Monday", "en", "en"),
        ("I need to book the dentist", "en", "en"),
        ("recuérdame llamar a mamá mañana a las 9", "es", "es"),
        ("recuerdame llamar a mama manana a las 9", "es", "es"),     # typed without accents
        ("enviar el informe para el viernes", "es", "es"),
        ("¿qué tengo?", "es", "es"),
        ("Comprar pan", "es", "es"),
        ("tengo que mandar el resumen al grupo en 5 minutos", "es", "es"),
        ("a las 9", "es", "es"),
        ("Llamar al médico el lunes", "es", "es"),
        ("necesito reservar el dentista", "es", "es"),
        ("pan y leche", "es", "es"),
        # mixed messages: the side with more clues wins, a tie keeps the fallback
        ("Hola, remind me to call mum tomorrow", "en", "en"),
        ("call mamá tomorrow", "en", "en"),
        ("café with Ana at 5", "en", "en"),
        ("Remind me to comprar pan", "en", "en"),
        ("recuérdame call Ana mañana", "es", "es"),
        ("Remind me mañana", "en", "es"),
        ("llamar a Ana tomorrow", "en", "es"),
    ]

    def test_cases(self):
        for text, with_en, with_es in self.CASES:
            with self.subTest(text=text, fallback="en"):
                self.assertEqual(detect_language(text, fallback="en"), with_en)
            with self.subTest(text=text, fallback="es"):
                self.assertEqual(detect_language(text, fallback="es"), with_es)

    def test_default_fallback_is_english(self):
        self.assertEqual(detect_language(""), "en")
        self.assertEqual(detect_language("✅"), "en")

    def test_an_empty_fallback_shows_there_was_no_clear_signal(self):
        self.assertEqual(detect_language("ok", fallback=""), "")
        self.assertEqual(detect_language("hola", fallback=""), "es")


class TestScores(unittest.TestCase):
    def test_accented_letters_and_marks_each_count(self):
        # "¿", "é" and the word "que" are three Spanish clues; there are no English ones.
        self.assertEqual(language_scores("¿qué?"), (3, 0))

    def test_a_las_counts_as_an_extra_clue(self):
        self.assertEqual(language_scores("a las"), (2, 0))

    def test_no_text(self):
        self.assertEqual(language_scores(""), (0, 0))


class TestStripAccents(unittest.TestCase):
    def test_strip_accents(self):
        cases = [("olvídalo", "olvidalo"), ("mañana", "manana"), ("MÁS", "MAS"), ("2º", "2o"),
                 ("✔️", "✔"), ("plain", "plain"), ("café", "cafe")]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(strip_accents(text), expected)


if __name__ == "__main__":
    unittest.main()
