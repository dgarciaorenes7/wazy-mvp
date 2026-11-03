"""Guess whether a message is written in English or Spanish, and the small word helpers that go with it.

conversation.py calls detect_language(text) on every message, to answer in the person's language.
It counts clues for each language and returns the one with more: Spanish clues are the letters ñ á é í ó ú,
the marks ¿ ¡ and common Spanish words ("mañana", "que", "para", "hola", "listo"), English clues are common
English words ("the", "to", "remind", "tomorrow"). A tie, including a message with no clues at all such as
"ok", "✅" or "done", returns `fallback`, so the person keeps the language they were already using. English
command words such as "done" or "stop" are deliberately NOT clues: a Spanish speaker tapping the "Done"
button must not be switched to English. Words are compared without accents, so "manana" counts like
"mañana"; strip_accents() and words_in() do that here and are reused by intents.py and title.py.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Tuple


def strip_accents(text: str) -> str:
    """Remove accents: "olvídalo" -> "olvidalo", "mañana" -> "manana", "2º" -> "2o".

    NFKD splits a letter from its accent ("í" becomes "i" plus a separate accent mark); we then drop
    every mark (Unicode category "Mn"), which also drops the invisible emoji style selector in "✔️".
    """
    split_up = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in split_up if unicodedata.category(ch) != "Mn")


def words_in(text: str) -> List[str]:
    """The words of a message in lower case, accents kept, apostrophes dropped ("What's" -> "whats")."""
    text = unicodedata.normalize("NFC", text).lower()   # NFC: "é" typed as e + accent becomes one letter
    text = text.replace("'", "").replace("’", "")    # straight and curly apostrophes
    return re.findall(r"\w+", text)


def _plain(words: Iterable[str]) -> frozenset:
    """Store a word list without accents, the same way messages are compared."""
    return frozenset(strip_accents(word) for word in words)


# Letters and marks that only Spanish uses (in our two languages). Each one in the message is a clue.
SPANISH_LETTERS = "ñáéíóú¿¡"

SPANISH_WORDS = _plain({
    # the list in SPEC section 5
    "recuérdame", "mañana", "tengo", "que", "el", "la", "las", "los", "hoy", "para", "llamar", "enviar",
    "mandar", "comprar", "de", "del", "con", "por", "mi", "tu",
    # Spanish command words, so a one-word message like "hola" or "listo" is clearly Spanish
    "hola", "listo", "hecho", "ayuda", "ayúdame", "luego", "sí", "si", "vale", "basta", "detente", "parar",
    "empezar", "reanudar", "buenas", "buenos", "días", "terminado", "está", "después", "dale", "claro",
    "gracias", "porfa", "olvida", "olvídalo", "déjalo", "nada", "lista", "recordatorios", "cancela",
    "cancelar", "borra", "elimina", "aprendido", "sabes", "puedes", "funciona", "recuérdamelo", "recordarme",
    # other everyday Spanish words that never appear in English sentences
    "en", "es", "un", "una", "lo", "al", "se", "le", "ya", "pero", "porque", "muy", "bien", "también",
    "este", "esta", "eso", "esto", "mis", "tus", "más", "hay", "necesito", "debo", "quiero", "semana",
    "tarde", "noche", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo", "enero",
    "febrero", "marzo", "abril", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre",
    "diciembre", "escribir", "hacer", "pagar", "recoger", "terminar", "estudiar", "entregar", "reservar",
    "preparar", "revisar", "sacar", "llevar", "cita", "reunión", "cuándo", "dónde", "cómo", "qué",
    "quién", "cuál", "avísame",
})

ENGLISH_WORDS = _plain({
    # the list in SPEC section 5
    "the", "to", "at", "remind", "me", "my", "tomorrow", "today", "call", "send", "buy", "and", "with",
    "by", "on", "in",
    # other everyday English words that never appear in Spanish sentences
    "i", "you", "it", "is", "for", "of", "this", "that", "please", "what", "have", "need", "about",
    "your", "next", "week", "tonight", "morning", "evening", "afternoon", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april",
    "june", "july", "august", "september", "october", "november", "december", "email", "write", "pay",
    "pick", "finish", "study", "submit", "book", "meet", "mum", "mom", "dad", "from", "before",
    "minutes", "hours", "hour", "get",
})


def language_scores(text: str) -> Tuple[int, int]:
    """Count the clues: (Spanish score, English score)."""
    if not text:
        return 0, 0
    words = [strip_accents(word) for word in words_in(text)]
    lower = unicodedata.normalize("NFC", text).lower()

    spanish = sum(1 for ch in lower if ch in SPANISH_LETTERS)
    spanish += sum(1 for word in words if word in SPANISH_WORDS)
    spanish += count_a_las(words)
    # "y" is Spanish for "and", but on its own it is the English "y" for "yes", so it only counts
    # as a Spanish clue inside a longer message.
    if len(words) > 1:
        spanish += words.count("y")

    english = sum(1 for word in words if word in ENGLISH_WORDS)
    return spanish, english


def count_a_las(words: List[str]) -> int:
    """How often "a las" (as in "a las 9") appears: an extra Spanish clue on top of "las" itself."""
    count = 0
    for position in range(len(words) - 1):
        if words[position] == "a" and words[position + 1] == "las":
            count += 1
    return count


def detect_language(text: str, fallback: str = "en") -> str:
    """Return "es" or "en" for the language with more clues; on a tie return `fallback`."""
    spanish, english = language_scores(text)
    if spanish > english:
        return "es"
    if english > spanish:
        return "en"
    return fallback
