"""Everything Wazy says to people, in English and in Spanish.

How it works, in plain words:
- render(kind, lang, **params) returns the text of one reply. `kind` is one of REPLY_KINDS in types.py
  ("confirm", "ask_time", "nudge", ...). `lang` is "en" or "es".
- The sentences live in two dictionaries, ENGLISH and SPANISH, with one entry per kind. To change what Wazy
  says, find the kind in the right dictionary and edit the sentence. Short replies are written right there, and
  replies with a few cases (for example invalid_time) have a small function just above the dictionary.
- PARAMS lists the exact parameters each kind takes (agreed in SPEC.md section 2.1). A missing or extra
  parameter is a bug in the caller, so render() raises TypeError. An unknown kind or language raises ValueError.
- describe_when(when, now, lang) turns a nudge time into words, like "tomorrow at 09:00" or
  "el viernes a las 10:00". Day and month names are written below, not taken from the computer's settings.

Style rules: English is plain, friendly, short, UK spelling, no em dashes, no semicolons. Spanish is natural
Spain Spanish with "tú". A task title is always shown in curly quotes “...”. A reply about a problem says what
went wrong in plain words and asks one clear question with an example.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from wazy.types import REPLY_KINDS

LANGUAGES = ("en", "es")


# ---- small helpers used by the sentences -----------------------------------------------------

def quote(text: str) -> str:
    """Put a title (or the words the person typed) in curly quotes: Call mum -> “Call mum”."""
    return f"“{text}”"


def hour_text(hour: int) -> str:
    """An hour number as a clock time: 9 -> "09:00"."""
    return f"{hour:02d}:00"


def count(number: int, one: str, many: str) -> str:
    """A number with the right word: count(1, "time", "times") -> "1 time", count(3, ...) -> "3 times"."""
    return f"{number} {one if number == 1 else many}"


def es_the(clock: str) -> str:
    """Spanish article before a clock time: "las 09:00", but "la 01:30" (Spanish says "la una")."""
    return ("la " if clock.startswith("01:") else "las ") + clock


def es_at(clock: str) -> str:
    """Spanish "at" a clock time: "a las 09:00", but "a la 01:30"."""
    return "a " + es_the(clock)


def round_half_up(value: float) -> int:
    """Round to the nearest whole number, halves going up (2.5 -> 3). Python's round() would give 2."""
    return math.floor(value + 0.5)


# ---- describe_when: a nudge time in words ----------------------------------------------------

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
           "octubre", "noviembre", "diciembre"],
}

# One line per row of the SPEC 2.1 table. {wd} and {mon} are the English short names ("Thu", "Nov").
WHEN_WORDS = {
    "en": {
        "seconds": "at {clock} (in {n} s)",
        "minutes": "at {clock} (in {n} min)",
        "today": "today at {clock}",
        "tomorrow": "tomorrow at {clock}",
        "this_week": "on {weekday} at {clock}",
        "later": "on {wd} {day} {mon} at {clock}",
        "other_year": "on {wd} {day} {mon} {year} at {clock}",
    },
    "es": {
        "seconds": "{a_las} {clock} (en {n} s)",
        "minutes": "{a_las} {clock} (en {n} min)",
        "today": "hoy {a_las} {clock}",
        "tomorrow": "mañana {a_las} {clock}",
        "this_week": "el {weekday} {a_las} {clock}",
        "later": "el {weekday} {day} de {month} {a_las} {clock}",
        "other_year": "el {weekday} {day} de {month} de {year} {a_las} {clock}",
    },
}


def describe_when(when: datetime, now: datetime, lang: str) -> str:
    """Say when a nudge goes out: exact for the next hour, plain day words after that.

    `when` is shown in its own zone (the person's), and `now` is moved to that zone first, so "today"
    and "tomorrow" mean the person's today and tomorrow. Both must be timezone-aware.
    """
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language: {lang!r}")
    if when.tzinfo is None or now.tzinfo is None:
        raise ValueError("describe_when needs timezone-aware datetimes")
    now = now.astimezone(when.tzinfo)
    seconds = when.timestamp() - now.timestamp()      # real seconds between them, right even across a DST change
    days_ahead = (when.date() - now.date()).days      # calendar days on the person's own clock

    clock, n = f"{when:%H:%M}", 0
    if seconds < 120:
        row, clock, n = "seconds", f"{when:%H:%M:%S}", max(1, round_half_up(seconds))
    elif seconds < 3600:
        row, n = "minutes", max(1, round_half_up(seconds / 60))
    elif days_ahead == 0:
        row = "today"
    elif days_ahead == 1:
        row = "tomorrow"
    elif days_ahead < 7:
        row = "this_week"
    elif when.year == now.year:
        row = "later"
    else:
        row = "other_year"

    weekday = WEEKDAYS[lang][when.weekday()]
    month = MONTHS[lang][when.month - 1]
    a_las = "a la" if clock.startswith("01:") else "a las"
    return WHEN_WORDS[lang][row].format(clock=clock, n=n, weekday=weekday, wd=weekday[:3], day=when.day,
                                        month=month, mon=month[:3], year=when.year, a_las=a_las)


# ---- the parameters every kind takes (SPEC 2.1, fixed) ---------------------------------------

PARAMS = {
    "welcome": ("ai",),
    "help": (),
    "confirm": ("title", "when_text", "learned"),
    "ask_time": ("title",),
    "ask_what": ("when_text",),
    "invalid_time": ("title", "detail", "detail_code", "hint"),
    "past_time": ("title", "detail"),
    "too_far": ("title",),
    "recurring_unsupported": ("title",),
    "multiple_commitments": (),
    "not_commitment": ("reason",),
    "injection": (),
    "too_long": ("limit",),
    "stopped": (),
    "started": (),
    "done_ack": ("title",),
    "later_ack": ("title", "when_text"),
    "no_open_nudge": (),
    "list": ("items",),
    "list_empty": (),
    "cancelled": ("title",),
    "cancel_which": ("items",),
    "nothing_to_cancel": (),
    "pending_cancelled": ("title",),
    "learning_question": ("current_hour", "misses", "answered", "best_hour", "done_at_best"),
    "learning_approved": ("hour",),
    "learning_rejected": ("hour",),
    "learning_explained": ("off", "learned_hour", "default_hour", "done_at_current", "answered_at_current",
                           "evidence"),
    "learning_forgotten": ("default_hour",),
    "learning_resumed": (),
    "rate_limited": (),
    "nudge": ("title", "due_text"),
}

NOT_COMMITMENT_REASONS = ("question", "edit", "limit")


# ==== ENGLISH ===================================================================================

def en_about(title: str) -> str:
    """' about “Call mum”', or nothing when there is no title."""
    return f" about {quote(title)}" if title else ""


def en_ask_when(title: str) -> str:
    """The one question a time problem ends with."""
    return f"When should I nudge you{en_about(title)}? For example “tomorrow at 9am”."


def en_welcome(ai: bool) -> str:
    parts = [
        "Hi, I'm Wazy. Text me a small commitment in your own words, like “send Marta the slides by Thursday”. "
        "I'll tell you exactly when I'll nudge you. Then I nudge you once, at that time, and you answer "
        "Done or Later.",
        "I also notice when you usually tick things off, so I can suggest a better time. "
        "I always ask before changing anything.",
    ]
    if ai:
        parts.append("An AI (Anthropic's Claude) only tidies the wording of your task. Code decides the time.")
    parts.append("Text “help” for the commands, or “stop” to stop all nudges.")
    return "\n\n".join(parts)


EN_HELP = (
    "I'm Wazy. Text me one commitment per message, like “call the bank tomorrow at 9am”. I'll nudge you once "
    "at that time, and you answer Done or Later.\n\n"
    "Commands:\n"
    "• “list”: see what's coming up\n"
    "• “cancel 2”: cancel number 2 on your list\n"
    "• “stop”: stop all nudges\n"
    "• “start”: turn nudges back on\n"
    "• “what did you learn”: see what I've noticed about your timing\n"
    "• “forget”: drop the hour I learned and stop learning"
)

# What went wrong with a time, by detail_code. {what} is the person's words in quotes.
EN_INVALID = {
    "hour_out_of_range": "{what} isn't a real time. A day only goes up to 23:59.",
    "minute_out_of_range": "{what} isn't a real time. Minutes only go up to 59.",
    "ampm_conflict": "{what} isn't a real time. With am or pm, the hour goes from 1 to 12.",
    "no_such_date": "{what} isn't a real date. That day doesn't exist.",
    "weekday_mismatch": "{what} doesn't match the calendar: that date is a {hint}.",
}
EN_INVALID_OTHER = "{what} isn't a time I can use."


def en_invalid_time(title: str, detail: str, detail_code: str, hint: str) -> str:
    what = quote(detail) if detail else "That"
    problem = EN_INVALID.get(detail_code, EN_INVALID_OTHER)
    return problem.format(what=what, hint=hint or "different day of the week") + " " + en_ask_when(title)


def en_past_time(title: str, detail: str) -> str:
    what = quote(detail) if detail else "That time"
    return f"{what} has already passed. {en_ask_when(title)}"


def en_recurring(title: str) -> str:
    if title:
        return (f"I can only nudge you once per commitment, so I can't repeat {quote(title)}. "
                f"When do you want the one nudge? Send it again with one time, like "
                f"“{title} tomorrow at 9am”.")
    return ("I can only nudge you once per commitment, so I can't repeat a nudge. When do you want the one "
            "nudge? Send the task with one time, like “call mum tomorrow at 9am”.")


EN_NOT_COMMITMENT = {
    "question": "I can't answer questions yet. I only take commitments, one per message, like “send Marta the "
                "slides by Thursday”. Text “list” to see what's coming up, or “cancel 2” to cancel one.",
    "edit": "I can't change a commitment once it's set. To change the time, cancel it and send it again: text "
            "“list”, then “cancel 2” (with its number), then send the commitment with the new time.",
    "limit": "You already have 100 upcoming commitments, the most I can keep. Text “list” to see them, "
             "or “cancel 2” to make room for a new one.",
}


def en_numbered(items) -> str:
    """One line per item: 1. “Call mum”, tomorrow at 09:00"""
    return "\n".join(f"{n}. {quote(title)}, {when_text}" for n, title, when_text in items)


def en_list(items) -> str:
    example = items[0][0] if items else 1
    return f"Coming up:\n{en_numbered(items)}\nText “cancel {example}” to cancel number {example}."


def en_cancel_which(items) -> str:
    example = items[0][0] if items else 1
    lines = f"\n{en_numbered(items)}" if items else ""
    return f"Which one should I cancel?{lines}\nText “cancel” and its number, like “cancel {example}”."


def en_learning_question(current_hour: int, misses: int, answered: int, best_hour: int, done_at_best: int) -> str:
    now_at, best_at = hour_text(current_hour), hour_text(best_hour)
    return (f"Quick question. When you don't give me a time, I nudge you at {now_at}. Of your last "
            f"{count(answered, 'nudge', 'nudges')}, {misses} at {now_at} got Later or no reply, and you ticked "
            f"{count(done_at_best, 'thing', 'things')} off around {best_at}. Shall I use {best_at} from now on? "
            f"Times you give me yourself won't change. Reply YES or NO.")


def en_learning_explained(off: bool, learned_hour: Optional[int], default_hour: int, done_at_current: int,
                          answered_at_current: int, evidence: dict) -> str:
    if off:
        return (f"I'm not learning anything right now (you told me to forget). When you don't give me a time, "
                f"I nudge you at {hour_text(default_hour)}. Text “learn again” to turn learning back on.")
    if learned_hour is not None:
        first = (f"When you don't give me a time, I nudge you at {hour_text(learned_hour)}, "
                 f"because you said YES to that.")
    else:
        first = f"When you don't give me a time, I nudge you at {hour_text(default_hour)} (my default)."
    if answered_at_current:
        so_far = (f" So far you've answered Done to {done_at_current} of "
                  f"{count(answered_at_current, 'nudge', 'nudges')} at that hour.")
    else:
        so_far = " I don't have any answers at that hour yet."
    best = ""
    evidence = evidence or {}
    in_use = learned_hour if learned_hour is not None else default_hour
    if evidence.get("best_hour") not in (None, in_use) and evidence.get("done_at_best"):
        best = (f" You answer Done most often around {hour_text(evidence['best_hour'])} "
                f"({count(evidence['done_at_best'], 'time', 'times')}).")
    # "forget" keeps this record (nothing is deleted, SPEC promise 3), so the text never says it wipes it.
    keep = ("\n\nWhat I keep: for each nudge, the hour it went out and whether you answered Done, Later or "
            "nothing. I never change a time you gave me, and I always ask first. Text “forget” and I'll drop "
            "any hour I learned and stop learning. Your commitments stay as they are.")
    return first + so_far + best + keep


def en_nudge(title: str, due_text: str) -> str:
    """The nudge itself. It does not name the buttons: the web chat shows them under the nudge, and
    whatsapp.py adds "Reply DONE or LATER" when it sends a nudge as plain text."""
    what = f"⏰ Nudge: {quote(title)}" if title else "⏰ Nudge"
    due = f" (it was due at {due_text})" if due_text else ""
    return f"{what}{due}."


ENGLISH = {
    "welcome": en_welcome,
    "help": lambda: EN_HELP,
    "confirm": lambda title, when_text, learned:
        f"Got it: {quote(title)}. I'll nudge you {when_text}{' (your usual time)' if learned else ''}.",
    "ask_time": lambda title:
        f"When should I nudge you{en_about(title)}? For example “in 2 hours”, “tomorrow at 9am” or “Friday”.",
    "ask_what": lambda when_text:
        f"What should I nudge you about {when_text}? For example “call the bank”." if when_text else
        "What should I nudge you about? Send it in one message, like “call the bank tomorrow at 9am”.",
    "invalid_time": en_invalid_time,
    "past_time": en_past_time,
    "too_far": lambda title:
        f"That's more than a year away. I can only nudge you up to a year ahead. {en_ask_when(title)}",
    "recurring_unsupported": en_recurring,
    "multiple_commitments": lambda:
        "That looks like two commitments at once, and I can only take one at a time. Which one first? "
        "Send it on its own, like “call mum at 5pm”.",
    "not_commitment": lambda reason: EN_NOT_COMMITMENT[reason],
    "injection": lambda:
        "I only take commitments, like “call mum tomorrow at 9am”, and I ignore instructions about how I work. "
        "What should I nudge you about?",
    "too_long": lambda limit:
        f"That message is over {limit} characters, which is too long for me. Can you send just the commitment, "
        f"like “call mum tomorrow at 9am”?",
    "stopped": lambda:
        "Stopped. I won't send you any more nudges, and the ones coming up are off. To come back, text “start” "
        "or send me a new commitment.",
    "started": lambda:
        "You're back on. Text me a commitment any time, like “call mum tomorrow at 9am”.",
    "done_ack": lambda title: f"Nice, {quote(title)} is done." if title else "Nice, that's done.",
    "later_ack": lambda title, when_text: f"No problem. I'll nudge you{en_about(title)} once more {when_text}.",
    "no_open_nudge": lambda:
        "There's no nudge waiting for an answer right now. Text me a new commitment any time.",
    "list": en_list,
    "list_empty": lambda:
        "Nothing coming up. Text me a commitment any time, like “call mum tomorrow at 9am”.",
    "cancelled": lambda title:
        f"Cancelled {quote(title)}. I won't nudge you about it." if title else
        "Cancelled. I won't nudge you about it.",
    "cancel_which": en_cancel_which,
    "nothing_to_cancel": lambda: "There's nothing to cancel right now.",
    "pending_cancelled": lambda title: f"OK, I've dropped {quote(title)}." if title else "OK, dropped it.",
    "learning_question": en_learning_question,
    "learning_approved": lambda hour:
        f"Great. From now on, when you don't give me a time, I'll nudge you at {hour_text(hour)}. "
        f"Text “forget” to undo this.",
    "learning_rejected": lambda hour:
        f"OK, I'll keep {hour_text(hour)}. I won't ask again for at least two weeks.",
    "learning_explained": en_learning_explained,
    "learning_forgotten": lambda default_hour:
        f"OK, I've dropped any hour I learned. When you don't give me a time, I'll nudge you at "
        f"{hour_text(default_hour)} again. I'll stop learning from your answers until you text “learn again”. "
        f"Your commitments stay as they are.",
    "learning_resumed": lambda:
        "OK, I'll start noticing when you tick things off again. I'll still ask before changing anything.",
    "rate_limited": lambda: "You're sending messages very fast. Give me a minute, then try again.",
    "nudge": en_nudge,
}


# ==== SPANISH ===================================================================================
# "Tarea" is the thing to do (not "compromiso", which in Spain sounds like a social engagement) and "aviso"
# is the nudge. The buttons are "Hecho" and "Más tarde", the same words as in the chat and on WhatsApp.

def es_de(title: str) -> str:
    """' de “Llamar a mamá”', or nothing when there is no title."""
    return f" de {quote(title)}" if title else ""


def es_ask_when(title: str) -> str:
    """The one question a time problem ends with."""
    return f"¿Cuándo te aviso{es_de(title)}? Por ejemplo “mañana a las 9”."


def es_welcome(ai: bool) -> str:
    parts = [
        "Hola, soy Wazy. Escríbeme algo que tengas que hacer, con tus palabras, por ejemplo “mandar las "
        "diapositivas a Marta el jueves”. Te confirmo la hora exacta del aviso. A esa hora te aviso una sola "
        "vez y me respondes Hecho o Más tarde.",
        "También me fijo en cuándo sueles marcar las cosas como hechas, para proponerte una hora mejor. "
        "Siempre te pregunto antes de cambiar nada.",
    ]
    if ai:
        parts.append("Una IA (Claude, de Anthropic) solo retoca cómo está escrita tu tarea. "
                     "La hora la decide el código.")
    parts.append("Escribe “ayuda” para ver los comandos, o “para” si quieres que deje de avisarte.")
    return "\n\n".join(parts)


ES_HELP = (
    "Soy Wazy. Mándame una tarea por mensaje, por ejemplo “llamar al banco mañana a las 9”. Te aviso una vez "
    "a esa hora y me respondes Hecho o Más tarde.\n\n"
    "Comandos:\n"
    "• “lista”: ver lo que tienes pendiente\n"
    "• “cancela 2”: cancelar el número 2 de tu lista\n"
    "• “para”: dejar de recibir avisos\n"
    "• “empezar”: volver a recibir avisos\n"
    "• “qué has aprendido”: ver qué sé de tus horarios\n"
    "• “olvida”: quitar la hora que he aprendido y dejar de aprender"
)

# Qué ha fallado con la hora, según detail_code. {what} son las palabras de la persona entre comillas.
ES_INVALID = {
    "hour_out_of_range": "{what} no es una hora válida. El día solo llega hasta las 23:59.",
    "minute_out_of_range": "{what} no es una hora válida. Los minutos van del 00 al 59.",
    "ampm_conflict": "{what} no es una hora válida. Con am o pm, la hora va del 1 al 12.",
    "no_such_date": "{what} no es una fecha válida. Ese día no existe.",
    "weekday_mismatch": "{what} no cuadra con el calendario. Esa fecha cae en {hint}.",
}
ES_INVALID_OTHER = "{what} no es una hora que pueda usar."

# timeparse gives the real weekday in English ("Thursday"). A Spanish reply says it in Spanish.
ES_WEEKDAY_FROM_EN = {en.lower(): es for en, es in zip(WEEKDAYS["en"], WEEKDAYS["es"])}


def es_invalid_time(title: str, detail: str, detail_code: str, hint: str) -> str:
    what = quote(detail) if detail else "Eso"
    hint = ES_WEEKDAY_FROM_EN.get(hint.lower(), hint) or "otro día de la semana"
    problem = ES_INVALID.get(detail_code, ES_INVALID_OTHER)
    return problem.format(what=what, hint=hint) + " " + es_ask_when(title)


def es_past_time(title: str, detail: str) -> str:
    what = quote(detail) if detail else "Esa hora"
    return f"{what} ya ha pasado. {es_ask_when(title)}"


def es_recurring(title: str) -> str:
    if title:
        return (f"Solo te aviso una vez por tarea, así que no puedo repetir {quote(title)}. "
                f"¿Cuándo quieres el aviso? Mándame la tarea otra vez con una sola hora, por ejemplo "
                f"“{title} mañana a las 9”.")
    return ("Solo te aviso una vez por tarea, así que no puedo repetir avisos. ¿Cuándo quieres el aviso? "
            "Mándame la tarea con una sola hora, por ejemplo “llamar a mamá mañana a las 9”.")


ES_NOT_COMMITMENT = {
    "question": "Todavía no sé responder preguntas. Solo apunto tareas, una por mensaje, por ejemplo "
                "“mandar las diapositivas a Marta el jueves”. Escribe “lista” para ver lo que tienes pendiente, "
                "o “cancela 2” para cancelar una.",
    "edit": "No puedo cambiar una tarea ya apuntada. Para cambiar la hora, cancélala y mándamela otra vez: "
            "escribe “lista”, luego “cancela 2” (con su número) y después la tarea con la hora nueva.",
    "limit": "Ya tienes 100 tareas pendientes, que es lo máximo que puedo guardar. Escribe “lista” para "
             "verlas, o “cancela 2” para hacer hueco a una nueva.",
}


def es_numbered(items) -> str:
    """Una línea por elemento: 1. “Llamar a mamá”, mañana a las 09:00"""
    return "\n".join(f"{n}. {quote(title)}, {when_text}" for n, title, when_text in items)


def es_list(items) -> str:
    example = items[0][0] if items else 1
    return (f"Lo que tienes pendiente:\n{es_numbered(items)}\n"
            f"Escribe “cancela {example}” para cancelar el número {example}.")


def es_cancel_which(items) -> str:
    example = items[0][0] if items else 1
    lines = f"\n{es_numbered(items)}" if items else ""
    return f"¿Cuál cancelo?{lines}\nEscribe “cancela” y su número, por ejemplo “cancela {example}”."


def es_learning_question(current_hour: int, misses: int, answered: int, best_hour: int, done_at_best: int) -> str:
    now_at, best_at = hour_text(current_hour), hour_text(best_hour)
    return (f"Una pregunta rápida. Cuando no me das hora, te aviso {es_at(now_at)}. De tus últimos "
            f"{count(answered, 'aviso', 'avisos')}, {misses} fueron {es_at(now_at)} y acabaron en Más tarde o "
            f"sin respuesta, y has respondido Hecho {count(done_at_best, 'vez', 'veces')} sobre "
            f"{es_the(best_at)}. ¿Te aviso {es_at(best_at)} a partir de ahora? Las horas que me des tú no "
            f"cambian. Responde SÍ o NO.")


def es_learning_explained(off: bool, learned_hour: Optional[int], default_hour: int, done_at_current: int,
                          answered_at_current: int, evidence: dict) -> str:
    if off:
        return (f"Ahora mismo no estoy aprendiendo nada (me pediste que lo olvidara). Cuando no me das hora, te "
                f"aviso {es_at(hour_text(default_hour))}. Escribe “vuelve a aprender” para activarlo otra vez.")
    if learned_hour is not None:
        first = f"Cuando no me das hora, te aviso {es_at(hour_text(learned_hour))}, porque me dijiste que SÍ."
    else:
        first = f"Cuando no me das hora, te aviso {es_at(hour_text(default_hour))} (mi hora por defecto)."
    if answered_at_current:
        so_far = (f" Hasta ahora has respondido Hecho a {done_at_current} de "
                  f"{count(answered_at_current, 'aviso', 'avisos')} a esa hora.")
    else:
        so_far = " Todavía no tengo respuestas a esa hora."
    best = ""
    evidence = evidence or {}
    in_use = learned_hour if learned_hour is not None else default_hour
    if evidence.get("best_hour") not in (None, in_use) and evidence.get("done_at_best"):
        best = (f" Sueles responder Hecho sobre {es_the(hour_text(evidence['best_hour']))} "
                f"({count(evidence['done_at_best'], 'vez', 'veces')}).")
    # "olvida" no borra este registro (nunca se borra nada, promesa 3 de SPEC): el texto no dice que lo borre.
    keep = ("\n\nLo que guardo: para cada aviso, la hora a la que salió y si respondiste Hecho, Más tarde o "
            "nada. Nunca cambio una hora que me hayas dado y siempre te pregunto antes. Si escribes “olvida”, "
            "quito la hora que haya aprendido y dejo de aprender. Tus tareas siguen igual.")
    return first + so_far + best + keep


def es_nudge(title: str, due_text: str) -> str:
    """El aviso. No nombra los botones: el chat los muestra debajo, y whatsapp.py añade
    "Responde HECHO o MÁS TARDE" cuando lo manda como texto."""
    what = f"⏰ Aviso: {quote(title)}" if title else "⏰ Aviso"
    due = f" (era para {es_the(due_text)})" if due_text else ""
    return f"{what}{due}."


SPANISH = {
    "welcome": es_welcome,
    "help": lambda: ES_HELP,
    "confirm": lambda title, when_text, learned:
        f"Apuntado: {quote(title)}. Te aviso {when_text}{' (tu hora habitual)' if learned else ''}.",
    "ask_time": lambda title:
        f"¿Cuándo te aviso{es_de(title)}? Por ejemplo “en 2 horas”, “mañana a las 9” o “el viernes”.",
    "ask_what": lambda when_text:
        f"¿De qué te aviso {when_text}? Por ejemplo “llamar al banco”." if when_text else
        "¿De qué quieres que te avise? Mándamelo en un mensaje, por ejemplo “llamar al banco mañana a las 9”.",
    "invalid_time": es_invalid_time,
    "past_time": es_past_time,
    "too_far": lambda title:
        f"Eso es dentro de más de un año. Solo puedo avisarte de aquí a un año como mucho. {es_ask_when(title)}",
    "recurring_unsupported": es_recurring,
    "multiple_commitments": lambda:
        "Eso parecen dos tareas a la vez, y las apunto de una en una. ¿Cuál va primero? Mándamela sola, "
        "por ejemplo “llamar a mamá a las 17:00”.",
    "not_commitment": lambda reason: ES_NOT_COMMITMENT[reason],
    "injection": lambda:
        "Solo apunto tareas, como “llamar a mamá mañana a las 9”, e ignoro las instrucciones sobre cómo "
        "funciono. ¿De qué quieres que te avise?",
    "too_long": lambda limit:
        f"Ese mensaje pasa de {limit} caracteres y es demasiado largo para mí. ¿Me mandas solo la tarea, "
        f"por ejemplo “llamar a mamá mañana a las 9”?",
    "stopped": lambda:
        "Vale, paro. No te mandaré más avisos y he anulado los que tenías pendientes. Cuando quieras volver, "
        "escribe “empezar” o mándame una tarea nueva.",
    "started": lambda:
        "Vale, vuelvo a avisarte. Mándame una tarea cuando quieras, por ejemplo “llamar a mamá mañana a las 9”.",
    "done_ack": lambda title:
        f"¡Genial! Marcado como hecho: {quote(title)}." if title else "¡Genial! Marcado como hecho.",
    "later_ack": lambda title, when_text: f"Vale. Te aviso otra vez{es_de(title)} {when_text}.",
    "no_open_nudge": lambda:
        "Ahora mismo no tienes ningún aviso por contestar. Mándame una tarea nueva cuando quieras.",
    "list": es_list,
    "list_empty": lambda:
        "No tienes nada pendiente. Mándame una tarea cuando quieras, por ejemplo “llamar a mamá mañana a las 9”.",
    "cancelled": lambda title:
        f"He cancelado {quote(title)}. Ya no te avisaré." if title else "Cancelado. Ya no te avisaré.",
    "cancel_which": es_cancel_which,
    "nothing_to_cancel": lambda: "Ahora mismo no hay nada que cancelar.",
    "pending_cancelled": lambda title: f"Vale, me olvido de {quote(title)}." if title else "Vale, olvidado.",
    "learning_question": es_learning_question,
    "learning_approved": lambda hour:
        f"Perfecto. A partir de ahora, cuando no me des hora, te aviso {es_at(hour_text(hour))}. "
        f"Escribe “olvida” para deshacerlo.",
    "learning_rejected": lambda hour:
        f"Vale, me quedo con {es_the(hour_text(hour))}. No te volveré a preguntar en al menos dos semanas.",
    "learning_explained": es_learning_explained,
    "learning_forgotten": lambda default_hour:
        f"Vale, ya no uso ninguna hora aprendida. Cuando no me des hora, te avisaré otra vez "
        f"{es_at(hour_text(default_hour))}. Dejo de aprender de tus respuestas hasta que escribas “vuelve a "
        f"aprender”. Tus tareas siguen igual.",
    "learning_resumed": lambda:
        "Vale, vuelvo a fijarme en cuándo marcas las cosas como hechas. Seguiré preguntándote antes de cambiar nada.",
    "rate_limited": lambda: "Me estás mandando mensajes muy rápido. Espera un minuto y vuelve a intentarlo.",
    "nudge": es_nudge,
}

SENTENCES = {"en": ENGLISH, "es": SPANISH}


# ---- render: the one function the rest of Wazy calls -----------------------------------------

def render(kind: str, lang: str, **params) -> str:
    """The text of one reply. Example: render("done_ack", "en", title="Call mum") -> 'Nice, “Call mum” is done.'"""
    if kind not in REPLY_KINDS or kind not in PARAMS:
        raise ValueError(f"unknown reply kind: {kind!r}")
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language: {lang!r}")
    missing = [name for name in PARAMS[kind] if name not in params]
    extra = [name for name in params if name not in PARAMS[kind]]
    if missing or extra:
        raise TypeError(f"render({kind!r}) takes {list(PARAMS[kind])}: missing {missing}, extra {extra}")
    if kind == "not_commitment" and params["reason"] not in NOT_COMMITMENT_REASONS:
        raise ValueError(f"not_commitment reason must be one of {NOT_COMMITMENT_REASONS}")
    return SENTENCES[lang][kind](**params)
