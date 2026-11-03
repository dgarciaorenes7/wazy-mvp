"""Shared data types. Every module codes against these, so they change only by agreement (see SPEC.md)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

Span = Tuple[int, int]          # [start, end) character offsets into the ORIGINAL message text

# ---- what the time reader found (wazy/timeparse.py) -------------------------------------------
# status:
#   "ok"        one usable time was found (when is set)
#   "none"      no time expression at all
#   "vague"     only vague time words ("at some point", "soon", "luego")
#   "invalid"   a time or date that cannot exist ("25:00", "13pm", "31 February", weekday/date mismatch)
#   "past"      a time that has already gone ("yesterday", "2 Nov 2025", "today at 9" said at 11:00)
#   "too_far"   more than 366 days ahead
#   "recurring" a repeating schedule ("every 5 minutes", "cada lunes", "daily")
#   "multiple"  two or more different times for what reads as two commitments
TIME_STATUSES = ("ok", "none", "vague", "invalid", "past", "too_far", "recurring", "multiple")


@dataclass
class TimeResult:
    status: str
    when: Optional[datetime] = None      # timezone-aware, in the user's zone; set only when status == "ok"
    stated: bool = False                 # the person gave a time of day or a short delay (not just a day)
    deadline: bool = False               # "by Friday", "antes del viernes", "para el viernes"
    spans: List[Span] = field(default_factory=list)   # every time-ish span found, including vague/invalid ones
    detail: str = ""                     # the offending words, e.g. "25:00", "yesterday", "31 February"
    detail_code: str = ""                # machine code, e.g. "hour_out_of_range", "no_such_date", "weekday_mismatch"
    hint: str = ""                       # optional extra for the reply, e.g. "Thursday" for a weekday mismatch


# ---- what a message is (wazy/intents.py) ------------------------------------------------------
INTENTS = (
    "empty", "stop", "start", "help", "greeting", "done", "later", "yes", "no",
    "learn_what", "learn_forget", "learn_resume", "list", "cancel", "cancel_pending",
    "question", "edit", "injection", "text",
)


@dataclass
class Intent:
    name: str                       # one of INTENTS; "text" means: read it as a commitment (or a reply to a question)
    number: Optional[int] = None    # for "cancel 2"


# ---- what Wazy says (wazy/replies.py renders, wazy/conversation.py decides) ---------------------
REPLY_KINDS = (
    "welcome", "help", "confirm", "ask_time", "ask_what", "invalid_time", "past_time", "too_far",
    "recurring_unsupported", "multiple_commitments", "not_commitment", "injection", "too_long",
    "stopped", "started", "done_ack", "later_ack", "no_open_nudge", "list", "list_empty",
    "cancelled", "cancel_which", "nothing_to_cancel", "pending_cancelled",
    "learning_question", "learning_approved", "learning_rejected", "learning_explained",
    "learning_forgotten", "learning_resumed", "rate_limited", "nudge",
)


@dataclass
class Reply:
    kind: str                        # one of REPLY_KINDS
    text: str                        # what the person sees
    lang: str = "en"                 # "en" or "es"
    commitment_id: Optional[int] = None
    buttons: bool = False            # True only for a nudge: show Done / Later
