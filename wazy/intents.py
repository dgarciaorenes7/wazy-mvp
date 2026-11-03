"""Decide what kind of message a person sent to Wazy (SPEC section 5).

classify(text) answers with an Intent (see wazy/types.py). The checks run in this order:

1. "empty": nothing but spaces.
2. A command, but only when the WHOLE message is the command: "stop", "help", "list", "done",
   "later", "cancel 2", "olvídalo" ... So "stop smoking tomorrow" or "cancel my dentist appointment
   on Friday" are still commitments. Small extras are allowed around a command: "please", "thanks",
   "wazy", "ok", "hola", punctuation and emoji ("Stop please!", "hola wazy", "done, thanks").
   "yes"/"no" are commands only while Wazy is waiting for an answer to its learning question, and
   "never mind"/"skip"/"olvídalo" only while Wazy is waiting for a missing time or task.
3. "injection": instructions aimed at Wazy itself ("ignore your rules ...", "act as an AI ...").
4. "text" when the message says "remind me" / "recuérdame" anywhere: it is a commitment,
   even when it is phrased as a question ("can you remind me to call mum at 5?").
5. "edit": asking to change a reminder ("move it to 5", "cámbialo").
6. "question": "do you speak Spanish", "¿cuándo me avisas?", anything ending with "?".
7. "text": everything else. The conversation reads it as a commitment, or as the answer to the
   question Wazy just asked.

Every comparison uses a "plain" copy of the message: lower case, no accents, no apostrophes,
punctuation removed. So "¡Olvídalo!", "olvidalo" and "OLVÍDALO." all look the same. The word lists
below are written normally (with accents) and turned into the same plain form when the module loads.
"""
from __future__ import annotations

import re
from typing import FrozenSet, List, Optional

from wazy.lang import strip_accents, words_in
from wazy.types import Intent


# ---- turning a message into plain words ----------------------------------------------------------

def normalise(text: str) -> str:
    """Lower case, trimmed, single spaces, no trailing "." or "!" (SPEC section 5)."""
    text = text.replace("’", "'")             # a phone's curly apostrophe is a normal one
    text = " ".join(text.lower().split())
    return text.rstrip(".! ")


def plain_words(text: str) -> List[str]:
    """The words of a message without accents or punctuation: "¡Olvídalo!" -> ["olvidalo"]."""
    return [strip_accents(word) for word in words_in(text)]


def phrases(*items: str) -> FrozenSet[str]:
    """Store each phrase the way messages are compared, so "qué tengo" also matches "que tengo"."""
    return frozenset(" ".join(plain_words(item)) for item in items)


def contains_phrase(words: List[str], phrase_set: FrozenSet[str]) -> bool:
    """True if any phrase appears in the message as whole words ("remind me" but not "remind meg")."""
    padded = " " + " ".join(words) + " "
    return any(" " + phrase + " " in padded for phrase in phrase_set)


def starts_with_phrase(words: List[str], phrase_set: FrozenSet[str]) -> bool:
    """True if the words begin with one of the phrases."""
    return any(words[:len(phrase.split())] == phrase.split() for phrase in phrase_set)


def ends_like_question(message: str) -> bool:
    """Ends with "?" (ignoring trailing spaces, emoji and other punctuation) or starts with "¿"."""
    without_tail = re.sub(r"[^\w?]+$", "", message)
    without_head = re.sub(r"^[^\w¿]+", "", message)
    return without_tail.endswith("?") or without_head.startswith("¿")


# ---- whole-message commands ----------------------------------------------------------------------

STOP_PHRASES = phrases(
    "stop", "unsubscribe", "stop nudges", "stop all", "stopall", "stop all nudges", "stop reminders",
    "stop all reminders", "stop it", "stop messages", "stop messaging me", "stop texting me",
    "para", "parar", "basta", "basta ya", "para ya", "detente", "deja de escribirme",
)
START_PHRASES = phrases(
    "start", "resume", "unstop", "start again", "start nudges", "resume nudges", "start reminders",
    "empezar", "empieza", "reanudar", "reanuda",
)
HELP_PHRASES = phrases(
    "help", "help me", "what can you do", "how does this work", "how does it work", "how do you work",
    "how do i use this", "how do i use it", "what do you do",
    "ayuda", "ayúdame", "qué puedes hacer", "cómo funciona", "cómo funcionas", "cómo te uso",
)
GREETING_PHRASES = phrases(
    "hi", "hello", "hey", "hiya", "hi there", "hello there", "hey there", "good morning",
    "good afternoon", "good evening",
    "hola", "buenas", "hola buenas", "buenos días", "buenas tardes", "buenas noches",
)
DONE_PHRASES = phrases(
    "done", "did it", "i did it", "done it", "all done", "finished", "completed", "ok done",
    "hecho", "listo", "ya está", "terminado", "lo hice", "ya lo hice", "ya lo he hecho",
)
LATER_PHRASES = phrases(
    "later", "snooze", "not now", "remind me later", "maybe later", "not yet",
    "luego", "más tarde", "después", "ahora no", "recuérdamelo luego", "recuérdamelo más tarde",
)
LEARN_WHAT_PHRASES = phrases(
    "what did you learn", "what have you learned", "what have you learnt", "what do you know about me",
    "what did you learn about me", "what have you learned about me", "what have you learnt about me",
    "qué has aprendido", "qué sabes de mí", "qué has aprendido de mí",
)
LEARN_FORGET_PHRASES = phrases(
    "forget", "forget me", "reset learning", "stop learning", "forget what you learned",
    "forget what you learnt",
    "olvida", "olvida lo aprendido", "olvida lo que has aprendido", "deja de aprender",
)
LEARN_RESUME_PHRASES = phrases(
    "learn again", "start learning", "resume learning",
    "vuelve a aprender", "aprende otra vez",
)
LIST_PHRASES = phrases(
    "list", "my list", "what's on", "what's coming up", "what do i have", "reminders", "my reminders",
    "show my reminders", "show me my reminders", "show reminders", "show my list", "show list",
    "list my reminders", "what are my reminders", "what's next", "what's on my list", "upcoming",
    "lista", "mi lista", "qué tengo", "mis recordatorios", "recordatorios", "ver lista",
    "qué tengo pendiente",
)
# Not "what do I have tomorrow" / "¿qué tengo mañana?": the list shows everything upcoming, so a
# question about one day is a question (SPEC: commands match the whole message only).
# Checked in this order. The lists never share a phrase (a test makes sure).
COMMAND_TABLES = (
    ("stop", STOP_PHRASES),
    ("start", START_PHRASES),
    ("help", HELP_PHRASES),
    ("greeting", GREETING_PHRASES),
    ("learn_what", LEARN_WHAT_PHRASES),
    ("learn_forget", LEARN_FORGET_PHRASES),
    ("learn_resume", LEARN_RESUME_PHRASES),
    ("done", DONE_PHRASES),
    ("later", LATER_PHRASES),
    ("list", LIST_PHRASES),
)
# These commands are naturally asked as questions, so "what's on?" or "¿qué tengo?" still count.
# The others do not: "stop?" or "cancel 2?" is somebody asking, not deciding, so it is a question.
QUESTION_SHAPED_COMMANDS = {"help", "greeting", "list", "learn_what", "learn_forget", "learn_resume"}

# Only while Wazy is waiting for a missing time or task ("When should I nudge you?").
CANCEL_PENDING_PHRASES = phrases(
    "never mind", "nevermind", "forget it", "skip", "nothing",
    "da igual", "déjalo", "olvídalo", "nada",
)

PENDING_FORGET_WORDS = phrases("forget", "olvida")

# Only while Wazy's learning question is open. Any mix of these words counts ("sí, vale").
YES_WORDS = phrases("yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "sí", "si", "vale", "dale",
                    "claro", "venga")
YES_PHRASES = phrases("of course", "go ahead", "sounds good", "claro que sí", "por supuesto")
NO_WORDS = phrases("no", "nope", "nah")
NO_PHRASES = phrases("not really", "no way", "mejor no", "para nada")

# Polite words and greetings that may surround a command: "please stop", "done, thanks", "hola wazy".
FILLERS = phrases(
    "please", "pls", "plz", "thanks", "thank you", "thx", "wazy", "ok", "okay", "hi", "hello", "hey",
    "por favor", "porfa", "gracias", "vale", "hola", "oye", "buenas",
)
GREETING_WORDS = phrases("hi", "hello", "hey", "hola", "buenas")

# Ticks that mean "done" on their own: "✅", "✔️".
DONE_SYMBOLS = {"✓", "✔", "✅", "☑"}


def without_fillers(words: List[str], keep: FrozenSet[str] = frozenset()) -> List[str]:
    """Remove polite words from both ends: ["please", "cancel", "2", "thanks"] -> ["cancel", "2"].

    Fillers listed in `keep` stay (for yes/no, "ok" and "vale" are the answer itself).
    """
    words = list(words)
    changed = True
    while changed and words:
        changed = False
        for filler in FILLERS - keep:
            filler_words = filler.split()
            size = len(filler_words)
            if words[:size] == filler_words:
                words = words[size:]
                changed = True
            elif len(words) >= size and words[-size:] == filler_words:
                words = words[:-size]
                changed = True
    return words


def match_yes_no(words: List[str]) -> Optional[str]:
    """ "yes", "sí, vale", "yes please" -> "yes"; "no", "no thank you", "nope" -> "no"; else None."""
    core = without_fillers(words, keep=YES_WORDS)     # "ok thanks" -> ["ok"], "no thank you" -> ["no"]
    if not core:
        return None
    joined = " ".join(core)
    if joined in YES_PHRASES or all(word in YES_WORDS for word in core):
        return "yes"
    if joined in NO_PHRASES or all(word in NO_WORDS for word in core):
        return "no"
    return None                                       # mixed ("ok no") or something else


# ---- cancel, with an optional number -------------------------------------------------------------

CANCEL_VERBS = phrases("cancel", "delete", "remove", "cancela", "cancelar", "borra", "borrar", "elimina",
                       "eliminar")
# One-word Spanish forms that already include "it": "cancélalo" = "cancel it".
CANCEL_IT_WORDS = phrases("cancélalo", "cancélala", "bórralo", "bórrala", "elimínalo", "elimínala")
# "cancel it", "cancel that": no number, the conversation decides which one (or asks).
CANCEL_OBJECTS = phrases(
    "it", "that", "this", "this one", "that one", "reminder", "the reminder", "my reminder",
    "the last one", "the last reminder", "my last reminder",
    "eso", "esto", "recordatorio", "el recordatorio", "mi recordatorio",
)
# Words that may sit around the number: "cancel number 2", "borra la 2", "cancel the 2nd one".
NUMBER_FILLERS = phrases("the", "number", "no", "nr", "num", "item", "reminder", "one",
                         "el", "la", "lo", "número", "recordatorio", "uno")
# Written without accents because they are compared with plain words ("séptimo" arrives as "septimo").
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
    "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10, "primero": 1, "primera": 1, "segundo": 2, "segunda": 2, "tercero": 3,
    "tercera": 3, "cuarto": 4, "cuarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6,
    "septimo": 7, "septima": 7, "octavo": 8, "octava": 8, "noveno": 9, "novena": 9, "decimo": 10,
    "decima": 10,
}


def number_value(word: str) -> Optional[int]:
    """ "2", "2nd", "2º" (plain form "2o"), "two", "segunda" -> 2. None if it is not a number."""
    match = re.fullmatch(r"(\d+)(st|nd|rd|th|o|a)?", word)
    if match:
        return int(match.group(1))
    return NUMBER_WORDS.get(word)


def read_cancel_number(after_verb: List[str]) -> Optional[int]:
    """The number after "cancel": "#2", "number 2", "la 2", "the 2nd one" -> 2.

    The number must be the only real word left, so "cancel my dentist appointment" or
    "cancel the 2 o'clock meeting" are not cancel commands.
    """
    left = [word for word in after_verb if word not in NUMBER_FILLERS]
    if not left and after_verb[-1:] in (["one"], ["uno"]):
        return 1                                   # "cancel one", "cancel number one": "one" is the number
    if len(left) != 1:
        return None
    return number_value(left[0])


def match_cancel(words: List[str]) -> Optional[Intent]:
    """ "cancel", "cancel it", "cancélalo" -> cancel; "cancel 2", "borra la 2" -> cancel with number 2."""
    if len(words) == 1 and (words[0] in CANCEL_VERBS or words[0] in CANCEL_IT_WORDS):
        return Intent("cancel")
    if not words or words[0] not in CANCEL_VERBS:
        return None
    after_verb = words[1:]
    if " ".join(after_verb) in CANCEL_OBJECTS:
        return Intent("cancel")
    number = read_cancel_number(after_verb)
    if number is not None:
        return Intent("cancel", number)
    return None


def match_command(message: str, words: List[str], pending: bool, learning_question: bool) -> Optional[Intent]:
    """The command that IS the whole message, or None."""
    asking = ends_like_question(message)
    if learning_question and not asking:
        answer = match_yes_no(words)
        if answer:
            return Intent(answer)

    core = without_fillers(words)
    if not core:
        # Nothing but symbols and polite words: "?", "✅", "ok ✅", "hola wazy", "thanks".
        if not words and "?" in message:
            return Intent("help")
        if any(ch in DONE_SYMBOLS for ch in strip_accents(message)):
            return Intent("done")
        if any(word in GREETING_WORDS for word in words):
            return Intent("greeting")
        return None

    # Try the message as typed, then without the polite words around it.
    forms = [" ".join(words), " ".join(core)]
    # While Wazy waits for a missing time, a bare "forget" / "olvida" most likely means "forget this one",
    # so it drops the wait instead of wiping what Wazy learned (that is harder to undo).
    if pending and any(form in PENDING_FORGET_WORDS for form in forms):
        return Intent("cancel_pending")
    for name, table in COMMAND_TABLES:
        if asking and name not in QUESTION_SHAPED_COMMANDS:
            continue
        if any(form in table for form in forms):
            return Intent(name)
    if asking:
        return None

    cancel = match_cancel(core)
    if cancel is not None:
        return cancel
    if pending and any(form in CANCEL_PENDING_PHRASES for form in forms):
        return Intent("cancel_pending")
    return None


# ---- injection: instructions aimed at Wazy -------------------------------------------------------

# "ignore (all / your / previous ...) (rules / instructions ...)"
IGNORE_VERBS = phrases("ignore", "disregard", "forget", "override", "bypass",
                       "ignora", "ignorar", "olvida", "olvidar", "olvídate", "sáltate")
IGNORE_IN_BETWEEN = phrases(
    "all", "your", "the", "any", "previous", "prior", "earlier", "these", "those", "of", "every",
    "current", "original", "old", "own", "system", "safety", "everything",
    "las", "los", "tus", "sus", "tu", "su", "todas", "todos", "todo", "lo", "de", "el",
    "anteriores", "previas",
)
IGNORE_TARGETS = phrases(
    "rules", "rule", "instructions", "instruction", "prompt", "prompts", "guidelines", "directions",
    "programming", "restrictions", "limits", "constraints", "policies", "above",
    "reglas", "normas", "instrucciones", "indicaciones", "restricciones", "límites", "programación",
    "órdenes", "anterior",
)
# Phrases that only make sense as instructions to a bot.
INJECTION_PHRASES = phrases(
    "system prompt", "your prompt", "your new instructions", "you are now", "you're now",
    "from now on you", "jailbreak", "developer mode", "do anything now", "imagine you are",
    "imagine you're",
    "eres ahora", "ahora eres", "modo desarrollador", "prompt del sistema", "tu prompt",
    "tus nuevas instrucciones", "imagina que eres", "finge que eres",
)
# "act as / pretend to be" counts only when the role is about Wazy itself.
ROLE_WORDS = phrases(
    "ai", "ia", "assistant", "asistente", "bot", "chatbot", "system", "sistema", "admin",
    "administrator", "administrador", "developer", "desarrollador", "root", "superuser", "different",
    "diferente", "new", "nuevo", "nueva", "unrestricted", "unfiltered", "uncensored", "evil", "hacker",
    "jailbroken", "dan", "gpt", "chatgpt", "claude", "llm", "model", "modelo", "human", "humano",
    "humana", "otro", "otra",
)
ARTICLES = phrases("a", "an", "my", "the", "your", "un", "una", "mi", "el", "la", "tu")
# "act as if I'm calm" is the person's own plan, not an instruction to Wazy.
FIRST_PERSON = phrases("i", "i'm", "i've", "i'd", "i'll", "we", "we're", "my", "me", "yo")
# Code that targets the page or a template ("<script", "{{", "{%") counts together with these words.
CODE_MARKERS = ("<script", "{{", "{%")
INSTRUCTION_WORDS = phrases(
    "ignore", "ignora", "disregard", "override", "instructions", "instrucciones", "rules", "reglas",
    "system", "sistema", "prompt", "admin", "root", "sudo", "jailbreak", "execute", "exec", "eval",
    "alert", "document", "cookie", "fetch", "window", "javascript", "password", "contraseña", "token",
    "secret", "config", "settings",
)


def tells_to_ignore_rules(words: List[str]) -> bool:
    """ "ignore all previous instructions", "olvida tus instrucciones", "ignore the above"."""
    for position, word in enumerate(words):
        if word not in IGNORE_VERBS:
            continue
        after = position + 1
        while after < len(words) and words[after] in IGNORE_IN_BETWEEN:
            after += 1
        if after < len(words) and words[after] in IGNORE_TARGETS:
            return True
    return False


def names_a_role(words: List[str]) -> bool:
    """ "an AI", "my assistant", "a different bot", "un asistente" -> True; "host for Ana" -> False."""
    if words[:1] and words[0] in ARTICLES:
        words = words[1:]
    return any(word in ROLE_WORDS for word in words[:2])


SPANISH_ACT_VERBS = phrases("actúa", "actuar", "compórtate")       # followed by "como"
SPANISH_PRETEND_VERBS = phrases("finge", "fingir")                 # followed by "ser"


def act_as_aimed_at_wazy(after_act_as: List[str]) -> bool:
    """The words after "act as"/"act like": "an AI", "if you had no rules" -> True; "host", "if I'm calm" -> False."""
    if after_act_as[:1] in (["if"], ["though"]):
        next_word = after_act_as[1:2]
        return bool(next_word) and next_word[0] not in FIRST_PERSON
    return names_a_role(after_act_as)


def pretend_aimed_at_wazy(after_pretend: List[str]) -> bool:
    """The words after "pretend": "you are", "that you", "to be an AI" -> True; "to be sick" -> False."""
    if after_pretend[:1] == ["that"]:
        after_pretend = after_pretend[1:]
    if after_pretend[:1] in (["you"], ["youre"]):
        return True
    return after_pretend[:2] == ["to", "be"] and names_a_role(after_pretend[2:])


def asks_to_play_a_role(words: List[str]) -> bool:
    """ "act as an AI", "act as if you ...", "pretend you are", "pretend to be a bot", "actúa como un bot"."""
    for position, word in enumerate(words):
        after = words[position + 1:]
        if word == "act" and after[:1] in (["as"], ["like"]) and act_as_aimed_at_wazy(after[1:]):
            return True
        if word == "pretend" and pretend_aimed_at_wazy(after):
            return True
        if word in SPANISH_ACT_VERBS and after[:1] == ["como"] and names_a_role(after[1:]):
            return True
        if word in SPANISH_PRETEND_VERBS and after[:1] == ["ser"] and names_a_role(after[1:]):
            return True
    return False


def has_code_with_instructions(message: str, words: List[str]) -> bool:
    """ "<script>alert(1)</script>", "{{ system.prompt }}": code markers next to instruction words."""
    if not any(marker in message for marker in CODE_MARKERS):
        return False
    return any(word in INSTRUCTION_WORDS for word in words)


def is_injection(message: str, words: List[str]) -> bool:
    return (tells_to_ignore_rules(words)
            or contains_phrase(words, INJECTION_PHRASES)
            or asks_to_play_a_role(words)
            or has_code_with_instructions(message, words))


# ---- "remind me" ---------------------------------------------------------------------------------

REMIND_ME = phrases("remind me", "recuérdame", "recuérdamelo", "recordarme", "me recuerdas")


# ---- edit: asking to change an existing reminder -------------------------------------------------

EDIT_VERBS = phrases("change", "move", "reschedule", "push", "postpone", "update", "rename", "edit",
                     "cambia", "cambiar", "mueve", "mover", "aplaza", "aplazar", "pospón", "posponer")
# The object must be the reminder itself, so "move the car tomorrow" stays a commitment.
EDIT_OBJECTS = phrases(
    "it", "the time", "the reminder", "my reminder", "the last one", "this one", "that one",
    "my last reminder", "the nudge",
    "la hora", "el recordatorio", "mi recordatorio", "eso", "esto",
)
# "this"/"that" can also start a noun ("move that box"), so they count only before one of these
# words or at the very end ("move that to 5", "change this").
AFTER_THIS_OR_THAT = phrases("to", "till", "until", "back", "forward", "by", "for", "from",
                             "please", "pls", "a", "al", "para", "hasta")
# One-word Spanish forms that already include "it": "cámbialo" = "change it".
EDIT_IT_WORDS = phrases(
    "cámbialo", "cámbiala", "muévelo", "muévela", "aplázalo", "aplázala", "pospónlo", "pospónla",
    "cambiarlo", "cambiarla", "moverlo", "moverla", "aplazarlo", "aplazarla", "posponerlo", "posponerla",
)


def refers_to_reminder(after_verb: List[str]) -> bool:
    if starts_with_phrase(after_verb, EDIT_OBJECTS):
        return True
    if after_verb[:1] in (["this"], ["that"]):
        return len(after_verb) == 1 or after_verb[1] in AFTER_THIS_OR_THAT
    return False


def is_edit(words: List[str]) -> bool:
    """ "move it to 5", "change the time", "can you reschedule my reminder", "cámbialo", "reschedule"."""
    if any(word in EDIT_IT_WORDS for word in words):
        return True
    if len(words) == 1 and words[0] in EDIT_VERBS:
        return True
    return any(word in EDIT_VERBS and refers_to_reminder(words[position + 1:])
               for position, word in enumerate(words))


# ---- questions -----------------------------------------------------------------------------------

EN_QUESTION_WORDS = phrases("what", "what's", "when", "when's", "where", "where's", "who", "who's",
                            "whose", "whom", "why", "how", "how's", "which")
# These start a question only with "?" or before a pronoun: "do you speak Spanish" is a question,
# "do the laundry tomorrow" is a commitment.
EN_HELPER_VERBS = phrases("is", "are", "can", "could", "do", "does", "did", "should", "will")
PRONOUNS = phrases("you", "i", "we", "it", "they", "he", "she", "there", "this", "that")
# "do it" / "do this" / "do that" are orders, never questions ("does it" would be), so after "do"
# only these pronouns start a question.
DO_PRONOUNS = phrases("you", "i", "we", "they")
# Compared WITH accents: "que" and "como" without the accent are ordinary words ("que no se me olvide",
# "como pizza mañana"), so they only make a question together with "?".
ES_QUESTION_WORDS = {"qué", "cuándo", "cuando", "dónde", "donde", "quién", "quien", "quiénes", "quienes",
                     "cómo", "cuál", "cual", "cuáles", "cuales", "puedes"}


def is_question(message: str, words: List[str]) -> bool:
    if ends_like_question(message):
        return True
    start = len(words) - len(lead_in_removed(words))     # skip "hey", "please", "hola", "oye" ...
    if start >= len(words):
        return False
    first = words[start]
    following = words[start + 1] if start + 1 < len(words) else ""
    first_with_accents = words_in(message)[start]
    if first in EN_QUESTION_WORDS:
        return True
    if first in EN_HELPER_VERBS:
        pronouns = DO_PRONOUNS if first == "do" else PRONOUNS
        return following in pronouns
    if first_with_accents in ES_QUESTION_WORDS:
        return True
    return first == "por" and following == "que"         # "por qué" / "por que"


def lead_in_removed(words: List[str]) -> List[str]:
    """Drop polite words and greetings at the START only: ["hola", "qué", "tal"] -> ["qué", "tal"]."""
    words = list(words)
    changed = True
    while changed and words:
        changed = False
        for filler in FILLERS:
            filler_words = filler.split()
            if words[:len(filler_words)] == filler_words:
                words = words[len(filler_words):]
                changed = True
    return words


# ---- the public function -------------------------------------------------------------------------

def classify(text: str, *, pending: bool = False, learning_question: bool = False) -> Intent:
    """What is this message? See the module docstring for the order of the checks.

    pending: Wazy is waiting for a missing time or task (enables "never mind", "skip", "olvídalo").
    learning_question: Wazy asked its learning question and waits for yes/no.
    """
    if not text or not text.strip():
        return Intent("empty")
    message = normalise(text)
    words = plain_words(message)

    command = match_command(message, words, pending, learning_question)
    if command is not None:
        return command
    if is_injection(message, words):
        return Intent("injection")
    if contains_phrase(words, REMIND_ME):
        return Intent("text")
    if is_edit(words):
        return Intent("edit")
    if is_question(message, words):
        return Intent("question")
    return Intent("text")
