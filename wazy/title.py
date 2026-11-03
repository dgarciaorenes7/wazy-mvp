"""Make the clean task title for a commitment (SPEC section 4).

conversation.py calls make_title(text, spans) for every message it reads as a commitment, right after the
time reader (timeparse.py) has said where the time words are, as spans of the original text.
A message like "Hey, remind me to call mum tomorrow at 5 please" holds a task ("Call mum") wrapped in
chat: a greeting, a request to Wazy, the time and a "please". This module cuts the time spans out, peels the
chat off both ends, tidies what is left and gives it a capital first letter, at most 120 characters long.
When a request to Wazy starts a new sentence ("Good morning! Remind me to ..."), the task starts there.
If no task is left (only filler, only a time, only noise like "ok" or "thanks"), the title is "" and the
conversation asks what the reminder is for. Words are compared ignoring case, accents and apostrophes, so
"Recuerdame" matches "recuérdame" and "dont forget to" matches "don't forget to", but the words that are
kept stay exactly as typed.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from .lang import strip_accents
from .types import Span

MAX_LENGTH = 120                # longest title, in characters (SPEC 4, step 4)

# Chat before the task, removed from the start again and again ("Hey Wazy, please remind me to ...").
OPENING_FILLER = [
    # English, from SPEC 4.2. "reminder:" also matches "Reminder" with no colon (edge punctuation is ignored).
    "remind me to", "remind me that", "remind me about", "remind me", "reminder:",
    "don't let me forget to", "don't forget to", "i need to", "i have to", "i must", "i should",
    "i've got to", "i gotta", "i want to", "i'd like to", "need to", "have to", "gotta",
    "please", "pls", "plz", "hey", "hi", "ok", "wazy", "can you remind me to", "could you remind me to",
    # Spanish, from SPEC 4.2.
    "recuérdame", "recuérdame que", "recuérdame de", "acuérdate de", "no me dejes olvidar",
    "no te olvides de recordarme", "tengo que", "necesito", "debo", "hay que", "quiero",
    "por favor", "porfa", "oye", "hola",
    # Added in the same spirit (promise 7): more ways people open a request.
    "hello", "okay", "yes", "yeah", "hmm", "hm", "um", "uh", "can you", "could you",
    "vale", "bueno", "pues", "puedes", "recordarme", "me recuerdas",
    # SPEC 3.6 (after the red team).
    "would you remind me to", "can u remind me to", "nudge me to", "set a reminder to", "set a reminder for",
    "do not forget to", "good morning", "good evening", "sure", "erm", "er", "well", "so",
    "avísame", "avísame de", "recuérdamelo", "acuérdate", "que no se me olvide", "que no se me olvide de",
    "recuérdame q",
    # More of the same kind, found by the red team: requests aimed at Wazy, never part of the task.
    "will you remind me to", "would you", "will you", "can u", "could u", "i want you to", "i need you to",
    "is it possible to", "can i get a reminder to", "could i get a reminder to", "can i have a reminder to",
    "i need a reminder to", "i want a reminder to", "a reminder to", "set a reminder", "nudge me",
    "don't let me forget", "do not let me forget to", "do not let me forget", "don't forget", "do not forget",
    "good afternoon", "buenos días", "buenas tardes", "buenas noches", "avísame para",
    "no se me olvide", "no se me olvide de", "acuérdate que",
]

# Politeness after the task ("Call mum please", "Llamar a mamá, gracias"), "then" (SPEC 3.6), and a request
# to Wazy at the very end ("Call mum at 5, remind me").
CLOSING_FILLER = ["please", "pls", "plz", "por favor", "porfa", "thanks", "thank you", "thx",
                  "gracias", "muchas gracias", "wazy", "then",
                  "remind me", "recuérdame", "recuérdamelo", "avísame"]

# SPEC 3.6: a tag question at the end ("Call mum at 9, ok?"). Removed only with its "?", so
# "Turn right" and "Comprar un vale" keep their last word.
TAG_QUESTIONS = {"ok", "okay", "vale", "right", "please", "pls", "porfa", "alright"}

# SPEC 3.6: words that comment on the time ("tomorrow at 9 works", "5pm is fine"). They are cut together
# with the time right before them, and only there, so "Check the heating works" keeps its last word.
COMMENT_ON_THE_TIME = re.compile(
    r"[\s,]*(?:works(?: for me)?|is fine(?: by me| for me)?|is good|is ok|suits me"
    r"|me va bien|me viene bien|est[aá] bien)\b", re.IGNORECASE)

# SPEC 3.6: a request to Wazy after a first sentence ("Good morning! Remind me to call mum", "Ya he llamado
# a Pedro, recuérdame llamar a Juan"): the task starts at the request, and the sentence before it is chat.
REQUESTS = ["remind me", "recuérdame", "recuérdamelo", "avísame", "nudge me to", "set a reminder",
            "a reminder to", "don't forget to", "don't let me forget", "que no se me olvide", "no se me olvide"]
CLAUSE_ENDS = (",", ";", ":", ".", "!", "?", "…")

# When one of these comes just before a closing word, that word is the task itself:
# "say thanks", "send Ana a thank you", "dar las gracias".
COURTESY_IS_THE_TASK_AFTER = {"say", "send", "give", "a", "the", "my", "las", "dar", "decir"}

# Little words left hanging at either end once the time is gone ("Send the deck to Ana by").
JOINERS = {"to", "at", "on", "by", "in", "for", "the", "this", "next", "and", "a", "las", "el", "la",
           "de", "del", "para", "antes", "y", "que", "around", "about"}
# Left hanging only at the end ("Call mum after 5pm" -> "Call mum after"); at the start they open a
# real task ("After lunch, call mum").
JOINERS_AT_END = {"after", "despues"}             # "después", compared without its accent like every key
# Two exceptions, because the time reader's spans already include the connector words ("at 7pm",
# "on Friday"), so these are the person's own words, not leftovers:
KEEP_AT_START = {"the", "a", "el", "la", "las"}     # articles: "Remind me about the dentist" -> "The dentist"
KEEP_AT_END = {"on", "in"}                          # the end of a phrasal verb: "Turn the heating on"

# A message made only of these is not a task (SPEC 4.5).
NOISE = ["hmm", "hm", "um", "uh", "ok", "okay", "k", "lol", "haha", "jaja", "thanks", "thank you", "thx",
         "cool", "nice", "great", "yes", "no", "yeah", "nope", "gracias", "vale", "bueno", "pues", "sí",
         "si", "genial", "perfecto", "test", "testing", "prueba", "never mind", "nevermind", "forget it",
         "skip", "nothing", "da igual", "déjalo", "olvídalo", "nada",
         # SPEC 3.6
         "sure", "erm", "er", "then", "works", "fine", "sounds good", "perfect"]
LAUGH_OR_HUM = re.compile(r"(?:ha|ja|je)+|h+m+|u+[hm]+")    # "jajaja", "hahaha", "hmmm", "ummm"

# Promise 7: a title is never an instruction aimed at Wazy. intents.py stops these first; this is a second lock.
AIMED_AT_WAZY = re.compile(
    r"\bolvida tus instrucciones\b|\bsystem prompt\b|\byou are now\b|\beres ahora\b|\bjailbreak\b"
    r"|\bdeveloper mode\b")
IGNORE_RULES = re.compile(
    r"\bignore (?:(?:all|your|my|the|previous|any) )*(?:rules|instructions)\b"
    r"|\bignorar? (?:(?:las|tus|todas) )*(?:reglas|instrucciones)\b")
# SPEC 3.6: after "remind me" / "recuérdame", "ignore ..." is the task when the rules are someone else's
# ("las instrucciones del profe") and do not name Wazy's rules (same rule as intents.py).
ASKS_FOR_A_REMINDER = re.compile(r"\b(?:remind me|recuerdame)\b")
NAMES_WAZYS_RULES = re.compile(r"\b(?:your|tus|system|sistema|previous|prior|anteriores)\b")
SOMEONE_ELSES = re.compile(r" (?:de|del|from|of|on|for|at)\b")

# Punctuation that never belongs at the edge of a title, and the brackets and quotes that come in pairs.
EDGE_MARKS = ".,;:!?¡¿…-–—*•~\"'“”‘’«»()[]{}"
CLOSER_OF = {"(": ")", "[": "]", "{": "}", "“": "”", "‘": "’", "«": "»", '"': '"', "'": "'"}
OPENER_OF = {closer: opener for opener, closer in CLOSER_OF.items()}


def make_title(text: str, spans: Sequence[Span], lang: str = "en") -> str:
    """Return the task in `text` without the time words in `spans`, or "" if no task is left.

    `lang` is part of the shared interface. Fillers of both languages are always removed,
    because people mix them ("recuérdame call mum").
    """
    spans = with_comments_on_the_time(text, spans)
    words = tidy_gaps(remove_spans(text, spans)).split()             # 1. cut the time words out
    if aimed_at_wazy(" ".join(key(word) for word in words)):
        return ""
    head, core, tail = set_aside_emoji(words)
    core = start_at_request(core)
    core = trim_end(drop_from_start(core, OPENING_KEYS, is_loose_at_start))   # 2 and 3. peel filler, joiners
    if not looks_like_a_task(core):                                   # 5. nothing meaningful left
        return ""
    title = finish(head, core, tail)                                  # 4. punctuation, capital letter
    if len(title) > MAX_LENGTH:                                       # 4. at most 120 characters
        title = finish([], trim_end(cut_to_fit(title.split())), [])
    return title


def aimed_at_wazy(plain: str) -> bool:
    """Promise 7, second lock: "ignore your rules", "system prompt" ... (`plain` is the message as keys).
    SPEC 3.6: "recuérdame ignorar las instrucciones del profe" is a task: the "ignore" comes after the
    request for a reminder, does not name Wazy's own rules, and the rules are someone else's."""
    if AIMED_AT_WAZY.search(plain):
        return True
    ignoring = IGNORE_RULES.search(plain)
    if ignoring is None:
        return False
    after_a_request = ASKS_FOR_A_REMINDER.search(plain[:ignoring.start()]) is not None
    names_wazys_rules = NAMES_WAZYS_RULES.search(ignoring.group()) is not None
    someone_elses = SOMEONE_ELSES.match(plain, ignoring.end()) is not None
    return not (after_a_request and not names_wazys_rules and someone_elses)


def with_comments_on_the_time(text: str, spans: Sequence[Span]) -> List[Span]:
    """SPEC 3.6: stretch each span over a comment right after it, so "tomorrow at 9 works" is cut whole."""
    longer = []
    for start, end in spans:
        comment = COMMENT_ON_THE_TIME.match(text, max(0, end))
        longer.append((start, comment.end() if comment else end))
    return longer


def remove_spans(text: str, spans: Sequence[Span]) -> str:
    """Turn every character inside any span into a space. Spans may overlap, touch, or run past the
    text; each character is simply cut or not. The spaces keep the words on either side apart."""
    cut = [False] * len(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(text), end)):
            cut[index] = True
    return "".join(" " if cut[index] else char for index, char in enumerate(text))


def tidy_gaps(text: str) -> str:
    """Close the gaps a cut leaves: 'call mum , then' becomes 'call mum, then', and ', ,' becomes ','."""
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)              # brackets left empty: "call mum ( )"
    text = re.sub(r"\s+([,;.!?…])(?!\d)", r"\1", text)        # no space before a comma (but keep " .5")
    text = re.sub(r"([,;])(?:\s*[,;])+", r"\1", text)         # one comma, not two
    return " ".join(text.split())                             # single spaces only


def key(word: str) -> str:
    """The form words are compared in: lower case, no accents, no apostrophes, no punctuation at the edges."""
    plain = strip_accents(word.lower())
    return plain.replace("'", "").replace("’", "").strip(EDGE_MARKS)


def as_keys(phrases: List[str]) -> List[Tuple[str, ...]]:
    """Turn phrases into tuples of word keys, longest first, so "remind me to" wins over "remind me"."""
    return sorted((tuple(key(word) for word in phrase.split()) for phrase in phrases), key=len, reverse=True)


OPENING_KEYS = as_keys(OPENING_FILLER)
CLOSING_KEYS = as_keys(CLOSING_FILLER)
NOISE_KEYS = as_keys(NOISE)
REQUEST_KEYS = as_keys(REQUESTS)


def phrase_at(words: List[str], phrases: List[Tuple[str, ...]], at_end: bool = False) -> Optional[tuple]:
    """Return the longest phrase the words start with (or end with), or None."""
    for phrase in phrases:
        part = words[-len(phrase):] if at_end else words[:len(phrase)]
        if len(words) >= len(phrase) and tuple(key(word) for word in part) == phrase:
            return phrase
    return None


def is_loose(word: str) -> bool:
    """A joiner like "at" or "para", or a word made only of punctuation like "-" or "..."."""
    return key(word) in JOINERS or key(word) == ""


def is_loose_at_start(word: str) -> bool:
    """A loose word that may be dropped from the start (articles such as "the" stay)."""
    return is_loose(word) and key(word) not in KEEP_AT_START


def is_loose_at_end(word: str) -> bool:
    """A loose word that may be dropped from the end ("on" and "in" stay: "Turn the heating on")."""
    return (is_loose(word) and key(word) not in KEEP_AT_END) or key(word) in JOINERS_AT_END


def is_tag_question(word: str) -> bool:
    """SPEC 3.6: "ok?", "¿vale?", "right?", "please?" at the end of a commitment."""
    return "?" in word and key(word) in TAG_QUESTIONS


def set_aside_emoji(words: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """Split off the words with no letter or digit at each end (emoji such as 🎂 or 🙏), so filler
    next to them can still be found ("Call mum please 🙏"). Lone punctuation among them is dropped."""
    start, end = 0, len(words)
    while start < end and not any(char.isalnum() for char in words[start]):
        start += 1
    while end > start and not any(char.isalnum() for char in words[end - 1]):
        end -= 1
    head = [word for word in words[:start] if key(word)]
    tail = [word for word in words[end:] if key(word)]
    return head, words[start:end], tail


def start_at_request(words: List[str]) -> List[str]:
    """SPEC 3.6: "Ya he llamado a Pedro, recuérdame llamar a Juan" -> "recuérdame llamar a Juan".
    A request to Wazy that starts a new sentence or clause, with words after it, starts the task."""
    for index in range(1, len(words)):
        request = phrase_at(words[index:], REQUEST_KEYS)
        if words[index - 1].endswith(CLAUSE_ENDS) and request and len(words) > index + len(request):
            return words[index:]
    return words


def drop_from_start(words: List[str], phrases: List[Tuple[str, ...]], also_drop) -> List[str]:
    """Drop the listed phrases, and single words that `also_drop` says yes to, from the start, again and
    again. Used for opening filler (plus joiners) and for noise (plus laughter)."""
    while words:
        phrase = phrase_at(words, phrases)
        if phrase:
            words = words[len(phrase):]
        elif also_drop(words[0]):
            words = words[1:]
        else:
            break
    return words


def trim_end(words: List[str]) -> List[str]:
    """Drop politeness, joiners and lone punctuation from the end, again and again."""
    while words:
        phrase = phrase_at(words, CLOSING_KEYS, at_end=True)
        if phrase and not courtesy_is_the_task(words, len(phrase)):
            words = words[:-len(phrase)]
        elif is_tag_question(words[-1]):
            words = words[:-1]
        elif is_loose_at_end(words[-1]):
            words = words[:-1]
        else:
            break
    return words


def courtesy_is_the_task(words: List[str], polite_size: int) -> bool:
    """ "say thanks", "dar las gracias": the word before the last `polite_size` words shows they are the task."""
    if len(words) <= polite_size:
        return False
    word_before = words[-polite_size - 1]
    return key(word_before) in COURTESY_IS_THE_TASK_AFTER


def looks_like_a_task(words: List[str]) -> bool:
    """SPEC 4.5: a task has at least one letter and is not only noise words like "ok" or "jaja"."""
    has_a_letter = any(char.isalpha() for word in words for char in word)
    return has_a_letter and len(drop_from_start(words, NOISE_KEYS, is_laughter)) > 0


def is_laughter(word: str) -> bool:
    """ "jajaja", "hahaha", "hmmm", "ummm"."""
    return LAUGH_OR_HUM.fullmatch(key(word)) is not None


def finish(head: List[str], core: List[str], tail: List[str]) -> str:
    """Join the words, strip punctuation at the edges and give the title a capital first letter."""
    middle = strip_edge_marks(" ".join(core))
    return capitalise(strip_edge_marks(" ".join(head + [middle] + tail)))


def strip_edge_marks(text: str) -> str:
    """Remove punctuation at both ends, but keep a bracket or quote whose partner is in the text,
    so "Call Ana (dentist)" and 'Read "Dune"' stay whole."""
    while text and text[-1] in EDGE_MARKS and not partner_inside(text[-1], text[:-1], OPENER_OF):
        text = text[:-1].rstrip()
    while text and text[0] in EDGE_MARKS and not partner_inside(text[0], text[1:], CLOSER_OF):
        text = text[1:].lstrip()
    return text


def partner_inside(mark: str, rest: str, partners: dict) -> bool:
    """True if `mark` is a bracket or quote and its partner appears in the rest of the text."""
    return mark in partners and partners[mark] in rest


def capitalise(text: str) -> str:
    """Upper-case the first letter only; the rest stays as typed ("call NHS" -> "Call NHS",
    "iPhone case" -> "IPhone case"). A title that starts with a number ("3 clients") is left alone."""
    for index, char in enumerate(text):
        if char.isdigit():
            return text
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1:]
    return text


def cut_to_fit(words: List[str]) -> List[str]:
    """Keep whole words while the title stays within MAX_LENGTH characters."""
    kept: List[str] = []
    for word in words:
        if len(" ".join(kept + [word])) > MAX_LENGTH:
            break
        kept.append(word)
    return kept or [words[0][:MAX_LENGTH]]             # one giant word (a long link): cut it
