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
        "I'll confirm in one line when I'll nudge you, then nudge you once at that time with Done / Later.",
        "I also notice when you usually tick things off, so I can suggest a better time. "
        "I always ask before changing anything.",
    ]
    if ai:
        parts.append("An AI (Anthropic's Claude) only tidies the wording of your task. Code decides the time.")
    parts.append("Text “help” to see the commands, or “stop” any time.")
    return "\n\n".join(parts)


EN_HELP = (
    "I'm Wazy. Text me one commitment per message, like “call the bank tomorrow at 9am”, and I'll nudge you "
    "once at that time with Done / Later.\n"
    "Commands:\n"
    "• “list”: see what's coming up\n"
    "• “cancel 2”: cancel number 2 on your list\n"
    "• “stop”: stop all nudges\n"
    "• “start”: turn nudges back on\n"
    "• “what did you learn”: see what I've noticed about your timing\n"
    "• “forget”: wipe what I learned and stop learning"
)

# What went wrong with a time, by detail_code. {what} is the person's words in quotes.
EN_INVALID = {
    "hour_out_of_range": "{what} isn't a real time. A day only goes up to 23:59.",
    "minute_out_of_range": "{what} isn't a real time. Minutes only go up to 59.",
    "ampm_conflict": "{what} isn't a real time. With am or pm, the hour goes from 1 to 12.",
    "no_such_date": "{what} isn't a real date. That day doesn't exist in the calendar.",
    "weekday_mismatch": "{what} doesn't match the calendar: that date is a {hint}.",
}
EN_INVALID_OTHER = "{what} isn't a time I can use."


def en_invalid_time(title: str, detail: str, detail_code: str, hint: str) -> str:
    what = quote(detail) if detail else "That"
    problem = EN_INVALID.get(detail_code, EN_INVALID_OTHER)
    return problem.format(what=what, hint=hint or "different day of the week") + " " + en_ask_when(title)


def en_past_time(title: str, detail: str) -> str:
    what = quote(detail) if detail else "That time"
    return f"{what} has already gone. {en_ask_when(title)}"


def en_recurring(title: str) -> str:
    if title:
        return (f"I can only nudge you once per commitment, so I can't repeat {quote(title)}. "
                f"When do you want the one nudge? Send it again with one time, for example "
                f"“{title} tomorrow at 9am”.")
    return ("I can only nudge you once per commitment, not on repeat. When do you want the one nudge? "
            "Send the task with one time, for example “call mum tomorrow at 9am”.")


EN_NOT_COMMITMENT = {
    "question": "I can't answer questions yet. I only take commitments, one per message, like “send Marta the "
                "slides by Thursday”. Text “list” to see what's coming up, or “cancel 2” to cancel one.",
    "edit": "I can't change a commitment once it's set. To change a time, cancel it and send it again: text "
            "“list”, then “cancel 2” (using its number), then send the commitment with the new time.",
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
    return (f"Quick question. When you don't give me a time I nudge you at {now_at}. Looking at your last "
            f"{answered} answered nudges, {misses} at {now_at} got Later or no reply, and you ticked "
            f"{count(done_at_best, 'thing', 'things')} off around {best_at}. Shall I use {best_at} from now on? "
            f"Times you give me yourself won't change. Reply YES or NO.")


def en_learning_explained(off: bool, learned_hour: Optional[int], default_hour: int, done_at_current: int,
                          answered_at_current: int, evidence: dict) -> str:
    if off:
        return (f"I'm not learning anything right now (you told me to forget). When you don't give me a time "
                f"I nudge you at {hour_text(default_hour)}. Text “learn again” to turn learning back on.")
    if learned_hour is not None:
        first = f"When you don't give me a time I nudge you at {hour_text(learned_hour)}, because you said YES to that."
    else:
        first = f"When you don't give me a time I nudge you at {hour_text(default_hour)} (my default)."
    so_far = (f" So far you've tapped Done on {done_at_current} of "
              f"{count(answered_at_current, 'nudge', 'nudges')} at that hour.")
    best = ""
    evidence = evidence or {}
    in_use = learned_hour if learned_hour is not None else default_hour
    if evidence.get("best_hour") not in (None, in_use) and evidence.get("done_at_best"):
        best = (f" You tap Done most often around {hour_text(evidence['best_hour'])} "
                f"({count(evidence['done_at_best'], 'time', 'times')}).")
    keep = (" What I keep: for each nudge, the hour it went out and whether you tapped Done, Later or nothing. "
            "I never change a time you gave me, and I always ask first. Text “forget” to wipe this and stop.")
    return first + so_far + best + keep


def en_nudge(title: str, due_text: str) -> str:
    what = f"⏰ Nudge: {quote(title)}" if title else "⏰ Nudge"
    due = f" (due at {due_text})" if due_text else ""
    return f"{what}{due}. Tap Done or Later."


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
        f"That's more than a year away, and I can only nudge you up to a year ahead. {en_ask_when(title)}",
    "recurring_unsupported": en_recurring,
    "multiple_commitments": lambda:
        "That looks like two commitments at once, and I can only take one at a time. Which one first? "
        "Send it on its own, for example “call mum at 5pm”.",
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
        "You're back on. Text me a commitment any time, for example “call mum tomorrow at 9am”.",
    "done_ack": lambda title: f"Nice, {quote(title)} is done." if title else "Nice, that's done.",
    "later_ack": lambda title, when_text: f"No problem. I'll nudge you{en_about(title)} once more {when_text}.",
    "no_open_nudge": lambda:
        "There's no nudge waiting for an answer right now. Text me a new commitment any time.",
    "list": en_list,
    "list_empty": lambda:
        "Nothing coming up. Text me a commitment any time, for example “call mum tomorrow at 9am”.",
    "cancelled": lambda title:
        f"Cancelled {quote(title)}. I won't nudge you about it." if title else
        "Cancelled. I won't nudge you about it.",
    "cancel_which": en_cancel_which,
    "nothing_to_cancel": lambda: "There's nothing to cancel right now.",
    "pending_cancelled": lambda title: f"OK, I've dropped {quote(title)}." if title else "OK, dropped it.",
    "learning_question": en_learning_question,
    "learning_approved": lambda hour:
        f"Done. When you don't give me a time I'll nudge you at {hour_text(hour)}. Text “forget” to undo it.",
    "learning_rejected": lambda hour:
        f"OK, I'll keep {hour_text(hour)}. I won't ask again for at least two weeks.",
    "learning_explained": en_learning_explained,
    "learning_forgotten": lambda default_hour:
        f"Done. I've wiped what I learned and I'm back to {hour_text(default_hour)}. I won't learn from your "
        f"answers again unless you text “learn again”. Your commitments and nudges are unchanged.",
    "learning_resumed": lambda:
        "OK, I'll start noticing when you tick things off again. I'll still ask before changing anything.",
    "rate_limited": lambda: "You're sending messages very fast. Give me a minute, then try again.",
    "nudge": en_nudge,
}


# ==== SPANISH ===================================================================================

def es_de(title: str) -> str:
    """' de “Llamar a mamá”', or nothing when there is no title."""
    return f" de {quote(title)}" if title else ""


def es_ask_when(title: str) -> str:
    """The one question a time problem ends with."""
    return f"¿Cuándo te aviso{es_de(title)}? Por ejemplo “mañana a las 9”."


def es_welcome(ai: bool) -> str:
    parts = [
        "Hola, soy Wazy. Mándame un pequeño compromiso con tus palabras, por ejemplo “mandar las diapositivas "
        "a Marta el jueves”. Te confirmo en una línea cuándo te aviso, y a esa hora te aviso una sola vez con "
        "Hecho / Luego.",
        "También me fijo en cuándo sueles marcar las cosas como hechas, para proponerte una hora mejor. "
        "Siempre te pregunto antes de cambiar nada.",
    ]
    if ai:
        parts.append("Una IA (Claude, de Anthropic) solo ordena la redacción de tu tarea. La hora la decide el código.")
    parts.append("Escribe “ayuda” para ver los comandos, o “para” cuando quieras que deje de avisarte.")
    return "\n\n".join(parts)


ES_HELP = (
    "Soy Wazy. Mándame un compromiso por mensaje, por ejemplo “llamar al banco mañana a las 9”, y te aviso "
    "una vez a esa hora con Hecho / Luego.\n"
    "Comandos:\n"
    "• “lista”: ver lo que tienes pendiente\n"
    "• “cancela 2”: cancelar el número 2 de tu lista\n"
    "• “para”: dejar de recibir avisos\n"
    "• “empezar”: volver a recibir avisos\n"
    "• “qué has aprendido”: ver qué he notado sobre tus horarios\n"
    "• “olvida”: borrar lo aprendido y dejar de aprender"
)

# Qué ha fallado con la hora, según detail_code. {what} son las palabras de la persona entre comillas.
ES_INVALID = {
    "hour_out_of_range": "{what} no es una hora real: el día solo llega hasta las 23:59.",
    "minute_out_of_range": "{what} no es una hora real: los minutos solo llegan hasta 59.",
    "ampm_conflict": "{what} no es una hora real: con am o pm, la hora va del 1 al 12.",
    "no_such_date": "{what} no es una fecha real: ese día no existe en el calendario.",
    "weekday_mismatch": "{what} no cuadra con el calendario: esa fecha cae en {hint}.",
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
        return (f"Solo puedo avisarte una vez por compromiso, así que no puedo repetir {quote(title)}. "
                f"¿Cuándo quieres el aviso? Mándamelo otra vez con una sola hora, por ejemplo "
                f"“{title} mañana a las 9”.")
    return ("Solo puedo avisarte una vez por compromiso, no de forma repetida. ¿Cuándo quieres el aviso? "
            "Mándame la tarea con una sola hora, por ejemplo “llamar a mamá mañana a las 9”.")


ES_NOT_COMMITMENT = {
    "question": "Todavía no sé responder preguntas. Solo apunto compromisos, uno por mensaje, por ejemplo "
                "“mandar las diapositivas a Marta el jueves”. Escribe “lista” para ver lo que tienes pendiente, "
                "o “cancela 2” para cancelar uno.",
    "edit": "No puedo cambiar un compromiso ya apuntado. Para cambiar la hora, cancélalo y mándamelo otra vez: "
            "escribe “lista”, luego “cancela 2” (con su número) y después el compromiso con la hora nueva.",
    "limit": "Ya tienes 100 compromisos pendientes, que es lo máximo que puedo guardar. Escribe “lista” para "
             "verlos, o “cancela 2” para hacer hueco a uno nuevo.",
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
    return (f"Una pregunta rápida. Cuando no me das hora, te aviso {es_at(now_at)}. De tus últimos {answered} "
            f"avisos respondidos, hubo {misses} {es_at(now_at)} con Luego o sin respuesta, y has tocado Hecho "
            f"{count(done_at_best, 'vez', 'veces')} sobre {es_the(best_at)}. ¿Paso a avisarte {es_at(best_at)} "
            f"a partir de ahora? Las horas que me des tú no cambian. Responde SÍ o NO.")


def es_learning_explained(off: bool, learned_hour: Optional[int], default_hour: int, done_at_current: int,
                          answered_at_current: int, evidence: dict) -> str:
    if off:
        return (f"Ahora mismo no estoy aprendiendo nada (me pediste que olvidara). Cuando no me das hora, te "
                f"aviso {es_at(hour_text(default_hour))}. Escribe “vuelve a aprender” para activarlo de nuevo.")
    if learned_hour is not None:
        first = f"Cuando no me das hora, te aviso {es_at(hour_text(learned_hour))}, porque me dijiste que SÍ."
    else:
        first = f"Cuando no me das hora, te aviso {es_at(hour_text(default_hour))} (mi hora por defecto)."
    so_far = (f" Hasta ahora has tocado Hecho en {done_at_current} de "
              f"{count(answered_at_current, 'aviso', 'avisos')} a esa hora.")
    best = ""
    evidence = evidence or {}
    in_use = learned_hour if learned_hour is not None else default_hour
    if evidence.get("best_hour") not in (None, in_use) and evidence.get("done_at_best"):
        best = (f" Cuando más tocas Hecho es sobre {es_the(hour_text(evidence['best_hour']))} "
                f"({count(evidence['done_at_best'], 'vez', 'veces')}).")
    keep = (" Lo que guardo: para cada aviso, la hora a la que salió y si tocaste Hecho, Luego o nada. Nunca "
            "cambio una hora que me hayas dado y siempre te pregunto antes. Escribe “olvida” para borrarlo y "
            "que deje de aprender.")
    return first + so_far + best + keep


def es_nudge(title: str, due_text: str) -> str:
    what = f"⏰ Aviso: {quote(title)}" if title else "⏰ Aviso"
    due = f" (previsto para {es_the(due_text)})" if due_text else ""
    return f"{what}{due}. Toca Hecho o Luego."


SPANISH = {
    "welcome": es_welcome,
    "help": lambda: ES_HELP,
    "confirm": lambda title, when_text, learned:
        f"Hecho: {quote(title)}. Te aviso {when_text}{' (tu hora habitual)' if learned else ''}.",
    "ask_time": lambda title:
        f"¿Cuándo te aviso{es_de(title)}? Por ejemplo “en 2 horas”, “mañana a las 9” o “el viernes”.",
    "ask_what": lambda when_text:
        f"¿De qué te aviso {when_text}? Por ejemplo “llamar al banco”." if when_text else
        "¿De qué quieres que te avise? Mándamelo en un mensaje, por ejemplo “llamar al banco mañana a las 9”.",
    "invalid_time": es_invalid_time,
    "past_time": es_past_time,
    "too_far": lambda title:
        f"Eso queda a más de un año y solo puedo avisarte hasta dentro de un año. {es_ask_when(title)}",
    "recurring_unsupported": es_recurring,
    "multiple_commitments": lambda:
        "Eso parecen dos compromisos a la vez, y los apunto de uno en uno. ¿Cuál va primero? Mándamelo solo, "
        "por ejemplo “llamar a mamá a las 17:00”.",
    "not_commitment": lambda reason: ES_NOT_COMMITMENT[reason],
    "injection": lambda:
        "Solo apunto compromisos, como “llamar a mamá mañana a las 9”, e ignoro las instrucciones sobre cómo "
        "funciono. ¿De qué quieres que te avise?",
    "too_long": lambda limit:
        f"Ese mensaje pasa de {limit} caracteres y es demasiado largo para mí. ¿Me mandas solo el compromiso, "
        f"por ejemplo “llamar a mamá mañana a las 9”?",
    "stopped": lambda:
        "Vale, paro. No te mandaré más avisos y he anulado los que tenías pendientes. Cuando quieras volver, "
        "escribe “empezar” o mándame un compromiso nuevo.",
    "started": lambda:
        "Ya vuelvo a avisarte. Mándame un compromiso cuando quieras, por ejemplo “llamar a mamá mañana a las 9”.",
    "done_ack": lambda title:
        f"¡Genial! Marcado como hecho: {quote(title)}." if title else "¡Genial! Marcado como hecho.",
    "later_ack": lambda title, when_text: f"Sin problema. Te vuelvo a avisar{es_de(title)} {when_text}.",
    "no_open_nudge": lambda:
        "Ahora mismo no hay ningún aviso esperando respuesta. Mándame un compromiso nuevo cuando quieras.",
    "list": es_list,
    "list_empty": lambda:
        "No tienes nada pendiente. Mándame un compromiso cuando quieras, por ejemplo “llamar a mamá mañana a las 9”.",
    "cancelled": lambda title:
        f"Cancelado: {quote(title)}. No te avisaré." if title else "Cancelado. No te avisaré.",
    "cancel_which": es_cancel_which,
    "nothing_to_cancel": lambda: "Ahora mismo no hay nada que cancelar.",
    "pending_cancelled": lambda title: f"Vale, olvidado: {quote(title)}." if title else "Vale, olvidado.",
    "learning_question": es_learning_question,
    "learning_approved": lambda hour:
        f"Hecho. Cuando no me des hora, te avisaré {es_at(hour_text(hour))}. Escribe “olvida” para deshacerlo.",
    "learning_rejected": lambda hour:
        f"Vale, me quedo con {es_the(hour_text(hour))}. No te volveré a preguntar en al menos dos semanas.",
    "learning_explained": es_learning_explained,
    "learning_forgotten": lambda default_hour:
        f"Hecho. He borrado lo que había aprendido y vuelvo {es_at(hour_text(default_hour))}. No volveré a "
        f"aprender de tus respuestas salvo que escribas “vuelve a aprender”. Tus compromisos y avisos siguen igual.",
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
