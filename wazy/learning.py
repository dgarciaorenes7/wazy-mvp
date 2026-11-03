"""The learning rule: notice when a person really ticks things off, and ASK before changing anything.

When someone gives a day but no time ("send the report on Friday"), Wazy nudges at their usual hour:
10:00, unless they approved another one. If, for one person, nudges at that hour keep getting Later or
no reply, while they often tap Done at some other hour, Wazy asks once: "Shall I use 19:00 from now on?".
Nothing changes until they say YES (product promise 4).

The rule is the same as in v1, now per person. Over that person's last 30 answered nudges:
  1. at least 3 nudges at their usual hour were answered (Done, Later or no reply),
  2. fewer than half of those were Done,
  3. some other hour between 08:00 and 21:59 has at least 3 Done taps,
  4. and no question is open, they have not said "forget", and the last question was 14 or more days ago.

Where the hours come from:
  - the hour of a nudge is the hour it was due (`scheduled_for`) on the person's own clock, and only
    nudges whose time Wazy chose (time_source "default" or "learned") count at the usual hour,
  - the hour of a Done tap is the hour the person tapped it (`responded_at`) on their own clock.

Every question, answer, "forget" and "learn again" is a row in learning_log (the question with the
numbers behind it), so the person and David can see exactly why Wazy asked.
None of these functions stores a chat message: the caller (conversation.py or scheduler.py) stores the
Reply and delivers it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .db import Database, from_db, to_db
from .replies import render
from .timeparse import DEFAULT_HOUR
from .types import Reply

LAST_ANSWERED = 30                      # look at the person's last 30 answered nudges
MIN_DEFAULT_NUDGES = 3                  # at least 3 answered nudges at the usual hour...
MAX_DONE_SHARE = 0.5                    # ...of which fewer than half were Done
MIN_DONE_AT_NEW_HOUR = 3                # and at least 3 Done taps at one other hour
EARLIEST_HOUR, LATEST_HOUR = 8, 21      # never propose nudging outside 08:00-21:59
ASK_AGAIN_AFTER = timedelta(days=14)    # one question per fortnight at most, so learning never becomes nagging
CHOSEN_BY_WAZY = ("default", "learned")  # time sources that count at the usual hour (not "stated")


# ---------------------------------------------------------------------------------------------
# Reading the numbers
# ---------------------------------------------------------------------------------------------
def usual_hour(user: dict) -> int:
    """The hour Wazy uses when the person gives no time: the hour they said YES to, else 10:00.

    A learned hour outside 08-21 can only come from a damaged database; it is ignored so Wazy never
    picks a time in quiet hours (23:00 to 07:59).
    """
    learned = user["learned_hour"]
    if learned is not None and EARLIEST_HOUR <= learned <= LATEST_HOUR:
        return learned
    return DEFAULT_HOUR


def evidence(db: Database, user_id: str) -> dict:
    """The numbers the rule looks at, for one person. Code, not a model: anyone can check them.

    Keys: current_hour (their usual hour), answered (answered nudges looked at, max 30),
    answered_at_current and done_at_current (nudges Wazy timed at the usual hour, and how many got Done),
    misses (answered_at_current - done_at_current), best_hour (the other hour between 08 and 21 with the
    most Done taps, or None), done_at_best (Done taps at best_hour), by_hour ({hour the nudge was due:
    {"answered": n, "done": n}}, over all answered nudges looked at).
    """
    user = _user(db, user_id)
    zone = ZoneInfo(user["tz"])
    current = usual_hour(user)
    rows = db.query(
        "SELECT o.outcome, o.scheduled_for, o.responded_at, c.time_source "
        "FROM nudge_outcomes o JOIN commitments c ON c.id = o.commitment_id "
        "WHERE o.user_id = ? AND o.outcome IN ('done', 'later', 'no_reply') "    # answered (no reply counts)
        "ORDER BY o.id DESC LIMIT ?", (user_id, LAST_ANSWERED))
    by_hour: dict = {}
    done_taps: dict = {}            # hour the person tapped Done -> how many times
    answered_at_current = done_at_current = 0
    for row in rows:
        hour = _local_hour(row["scheduled_for"], zone)
        done = 1 if row["outcome"] == "done" else 0
        counts = by_hour.setdefault(hour, {"answered": 0, "done": 0})
        counts["answered"] += 1
        counts["done"] += done
        if row["time_source"] in CHOSEN_BY_WAZY and hour == current:
            answered_at_current += 1
            done_at_current += done
        if done and row["responded_at"]:
            tap_hour = _local_hour(row["responded_at"], zone)
            done_taps[tap_hour] = done_taps.get(tap_hour, 0) + 1
    best = _best_hour(done_taps, current)
    return {
        "current_hour": current,
        "answered": len(rows),
        "answered_at_current": answered_at_current,
        "done_at_current": done_at_current,
        "misses": answered_at_current - done_at_current,
        "best_hour": best,
        "done_at_best": done_taps.get(best, 0) if best is not None else 0,
        "by_hour": by_hour,
    }


def _best_hour(done_taps: dict, current: int) -> Optional[int]:
    """The other hour between 08 and 21 with the most Done taps.

    On a tie, the hour closest to the usual hour wins, then the earlier one.
    """
    candidates = [hour for hour in done_taps if hour != current and EARLIEST_HOUR <= hour <= LATEST_HOUR]
    if not candidates:
        return None
    return max(candidates, key=lambda hour: (done_taps[hour], -abs(hour - current), -hour))


def _rule_says_ask(e: dict) -> bool:
    """Rules 1 to 3 from the top of this file, on the numbers from evidence()."""
    answered, done = e["answered_at_current"], e["done_at_current"]
    return (answered >= MIN_DEFAULT_NUDGES
            and done < answered * MAX_DONE_SHARE
            and e["best_hour"] is not None
            and e["done_at_best"] >= MIN_DONE_AT_NEW_HOUR)


def _may_ask(user: dict, now: datetime) -> bool:
    """Rule 4: no open question, learning not switched off, last question 14 or more days ago."""
    if user["learning_off"] or user["learning_pending"]:
        return False
    asked_at = from_db(user["learning_asked_at"])
    return asked_at is None or now - asked_at >= ASK_AGAIN_AFTER


# ---------------------------------------------------------------------------------------------
# What conversation.py and scheduler.py call
# ---------------------------------------------------------------------------------------------
def consider(db: Database, user_id: str, now: datetime) -> Optional[Reply]:
    """Propose (never apply) a better usual hour. Returns the learning_question to send, or None."""
    with db.transaction():          # check and mark in one step, so two callers never both ask
        user = _user(db, user_id)
        if not _may_ask(user, now):
            return None
        e = evidence(db, user_id)
        if not _rule_says_ask(e):
            return None
        db.update_user(user_id, learning_pending=json.dumps({"hour": e["best_hour"]}),
                       learning_asked_at=to_db(now))
        _log(db, user_id, "asked", e["current_hour"], e["best_hour"], e, now)
        text = render("learning_question", user["lang"], current_hour=e["current_hour"], misses=e["misses"],
                      answered=e["answered"], best_hour=e["best_hour"], done_at_best=e["done_at_best"])
    return Reply(kind="learning_question", text=text, lang=user["lang"])


def answer(db: Database, user_id: str, yes: bool, now: datetime) -> Reply:
    """The person answered the open question. YES: use the new hour. NO: keep the old one, ask again in 2 weeks."""
    with db.transaction():
        user = _user(db, user_id)
        proposal = json.loads(user["learning_pending"] or "{}")
        old_hour, new_hour = usual_hour(user), proposal.get("hour")
        e = evidence(db, user_id)
        if yes and new_hour is not None:
            db.update_user(user_id, learning_pending=None, learned_hour=new_hour)
            _log(db, user_id, "approved", old_hour, new_hour, e, now)
            kind, hour = "learning_approved", new_hour
        else:
            # the two weeks count from the answer, not from the question
            db.update_user(user_id, learning_pending=None, learning_asked_at=to_db(now))
            _log(db, user_id, "rejected", old_hour, new_hour, e, now)
            kind, hour = "learning_rejected", old_hour
        text = render(kind, user["lang"], hour=hour)
    return Reply(kind=kind, text=text, lang=user["lang"])


def explain(db: Database, user_id: str) -> Reply:
    """'What did you learn': the usual hour, why, and the real numbers behind it."""
    user = _user(db, user_id)
    e = evidence(db, user_id)
    text = render("learning_explained", user["lang"], off=bool(user["learning_off"]),
                  learned_hour=user["learned_hour"], default_hour=DEFAULT_HOUR,
                  done_at_current=e["done_at_current"], answered_at_current=e["answered_at_current"], evidence=e)
    return Reply(kind="learning_explained", text=text, lang=user["lang"])


def forget(db: Database, user_id: str, now: datetime) -> Reply:
    """'Forget': wipe the learned hour, close any question, and stop learning until 'learn again'.

    Nudge outcomes are not deleted (nothing is deleted); they are simply not used while learning is off.
    """
    with db.transaction():
        user = _user(db, user_id)
        db.update_user(user_id, learned_hour=None, learning_pending=None, learning_asked_at=None, learning_off=1)
        _log(db, user_id, "forgotten", user["learned_hour"], DEFAULT_HOUR, None, now)
        text = render("learning_forgotten", user["lang"], default_hour=DEFAULT_HOUR)
    return Reply(kind="learning_forgotten", text=text, lang=user["lang"])


def resume(db: Database, user_id: str, now: datetime) -> Reply:
    """'Learn again': notice Done taps again (it will still ask before changing anything)."""
    with db.transaction():
        user = _user(db, user_id)
        db.update_user(user_id, learning_off=0)
        _log(db, user_id, "resumed", None, None, None, now)
        text = render("learning_resumed", user["lang"])
    return Reply(kind="learning_resumed", text=text, lang=user["lang"])


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def _user(db: Database, user_id: str) -> dict:
    user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        raise KeyError(f"no such user: {user_id}")
    return user


def _local_hour(utc_text: str, zone: ZoneInfo) -> int:
    """UTC text from the database -> the hour on the person's clock."""
    return from_db(utc_text).astimezone(zone).hour


def _log(db: Database, user_id: str, event: str, old_hour: Optional[int], new_hour: Optional[int],
         numbers: Optional[dict], now: datetime) -> None:
    """One row in learning_log; `numbers` is the evidence behind the event, stored as JSON."""
    db.execute("INSERT INTO learning_log (user_id, event, old_hour, new_hour, evidence, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (user_id, event, old_hour, new_hour, json.dumps(numbers) if numbers is not None else None,
                to_db(now)))
