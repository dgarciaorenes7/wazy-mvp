"""Run Wazy's eval cases and say which step fails most often.

What this does, in plain words:

1. It reads every case in evals/cases/*.jsonl. Each line is one JSON object: a short conversation
   ("steps") and what should be true at the end ("expect"). The format is in SPEC.md section 9.
   A line that cannot be used (bad JSON, unknown key, duplicate id, a date that does not exist)
   is a "case error": it is listed and skipped, and it never stops the run.
2. For each case it builds a brand new Wazy App with an empty in-memory database and a clock frozen
   at the case's "now". Then it plays the steps in order:
      say   send a message, like typing in the chat
      wait  move the clock forward ("30s", "90m", "2h", "1d")
      tick  run the scheduler once (it sends any nudge that is due)
      tap   press Done or Later on the newest nudge still waiting for an answer
3. It compares what Wazy did with the "expect" block. Every check that fails is blamed on one step
   of Wazy's pipeline: crash, intent, time, title, language, reply or flow (see STEP_MEANING).
   A failing case is blamed on its most basic failing step, in that order.
4. It prints a summary, writes evals/REPORT.md (pass rate by category and by failing step, so we can
   see which step fails most often) and, if asked, a JSON file with every result.

Usage:
    python3.9 evals/run.py [--cases DIR] [--category NAME] [--id ID] [--verbose]
                           [--report PATH] [--json PATH] [--no-report]

Exit code: 0 every case passes, 1 some case fails, 2 the cases themselves have errors (or there are none).
Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
if str(REPO_ROOT) not in sys.path:          # so that "import wazy" works when run as a script
    sys.path.insert(0, str(REPO_ROOT))

from wazy.types import REPLY_KINDS  # noqa: E402  (needs the sys.path line above)

USER_ID = "web:eval"
DEFAULT_TZ = "Europe/Madrid"
DEFAULT_CASES_DIR = EVALS_DIR / "cases"
DEFAULT_REPORT = EVALS_DIR / "REPORT.md"
MAX_LISTED = 200                            # failing cases listed in the report
BEFORE_AND_AFTER = "## Before and after"    # this report section is written by hand and kept on every run

# Tests set this to a function (clock, tz_name) -> app, so they can use a fake App instead of the real one.
APP_FACTORY = None

# ---------------------------------------------------------------------------------------------
# The steps a failure can be blamed on, most basic first
# ---------------------------------------------------------------------------------------------
STEPS = ("crash", "intent", "time", "title", "language", "reply", "flow")
STEP_MEANING = {
    "crash": "Wazy raised an exception",
    "intent": "Wazy misread what kind of message it was (command, question, injection, answer to a nudge...)",
    "time": "the time was read wrong (when, stated or default, deadline, or the wrong time problem)",
    "title": "the task title is wrong (left-over time words or filler, or missing words)",
    "language": "the reply is in the wrong language",
    "reply": "the reply text is missing words it should say, or says words it should not",
    "flow": "the conversation went another way (number of replies, commitments, statuses, nudges)",
}

# Replies about reading a time. Wazy giving one of these instead of another is a "time" failure.
TIME_KINDS = {
    "confirm", "ask_time", "invalid_time", "past_time", "too_far", "recurring_unsupported",
    "multiple_commitments", "ask_what",
}
# Replies that depend on recognising the message. A mix-up involving one of these is an "intent" failure.
INTENT_KINDS = {
    "injection", "not_commitment", "help", "welcome", "stopped", "started", "list", "list_empty",
    "cancelled", "cancel_which", "nothing_to_cancel", "pending_cancelled", "done_ack", "later_ack",
    "no_open_nudge", "too_long",
} | {kind for kind in REPLY_KINDS if kind.startswith("learning_")}

# ---------------------------------------------------------------------------------------------
# The case format (SPEC.md section 9)
# ---------------------------------------------------------------------------------------------
CASE_KEYS = {"id", "category", "now", "tz", "steps", "expect", "note"}
EXPECT_KEYS = {
    "last_reply_kind", "reply_kinds", "last_reply_contains", "last_reply_not_contains", "lang",
    "commitment_count", "commitments", "nudges_sent", "reader_misses",
}
COMMITMENT_KEYS = {
    "title", "title_contains", "title_not_contains", "nudge_at_local", "time_source", "status", "deadline",
}
COUNT_KEYS = ("commitment_count", "nudges_sent", "reader_misses")
TIME_SOURCES = ("stated", "default", "learned")
STATUSES = ("scheduled", "nudged", "done", "stopped", "no_reply", "cancelled")
LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")     # "2026-11-03T08:55" or with ":SS"
WAIT_TEXT = re.compile(r"(\d+[smhd])+")                                 # "90m", "2h", "1h30m"
WAIT_PART = re.compile(r"(\d+)([smhd])")
SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class CaseProblem(ValueError):
    """A case line that cannot be used: bad JSON, an unknown key, a date that does not exist..."""


@dataclass
class Case:
    id: str
    category: str
    now: str               # local time in tz, "2026-11-03T08:55:00"
    tz: str
    steps: List[dict]
    expect: dict
    where: str             # "en_clock.jsonl:12", to find the line again
    note: str = ""


@dataclass
class CaseError:
    where: str
    message: str


@dataclass
class Failure:
    step: str              # one of STEPS: the part of Wazy this failure is blamed on
    check: str             # what was checked, e.g. "last_reply_kind" or "commitment 1 title"
    expected: Any
    actual: Any

    def line(self, limit: Optional[int] = 120) -> str:
        return f"{self.check}: expected {show(self.expected, limit)}, got {show(self.actual, limit)}"


@dataclass
class CaseResult:
    case: Case
    failures: List[Failure] = field(default_factory=list)
    replies: List[dict] = field(default_factory=list)   # Wazy's replies after the welcome, in order
    position: str = "creating the App"                  # where we are, to say where a crash happened
    traceback: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def step(self) -> str:
        """The step this case is blamed on: its most basic failing step ("" if it passed)."""
        if not self.failures:
            return ""
        return min((failure.step for failure in self.failures), key=STEPS.index)

    def reason(self) -> Failure:
        """The first failure of the blamed step: the one-line reason shown in the report."""
        return next(failure for failure in self.failures if failure.step == self.step)


def show(value: Any, limit: Optional[int] = 120) -> str:
    """A value as one short line of text (JSON style, so newlines and quotes stay visible)."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if limit is not None and len(text) > limit:
        text = text[:limit - 3] + "..."
    return text


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CaseProblem(message)


def is_list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ---------------------------------------------------------------------------------------------
# Reading the cases
# ---------------------------------------------------------------------------------------------
def load_cases(cases_dir: Path) -> Tuple[List[Case], List[CaseError]]:
    """Read every *.jsonl file in cases_dir. Lines that cannot be used become CaseErrors."""
    cases: List[Case] = []
    errors: List[CaseError] = []
    first_seen: Dict[str, str] = {}
    for path in sorted(cases_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as problem:
            errors.append(CaseError(path.name, f"cannot read the file: {problem}"))
            continue
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue                                        # blank lines are allowed
            where = f"{path.name}:{number}"
            try:
                case = parse_case(line, path.stem, where)
            except CaseProblem as problem:
                errors.append(CaseError(where, str(problem)))
                continue
            if case.id in first_seen:
                errors.append(CaseError(where, f"duplicate id {case.id!r} (first used at {first_seen[case.id]})"))
                continue
            first_seen[case.id] = where
            cases.append(case)
    return cases, errors


def parse_case(line: str, default_category: str, where: str) -> Case:
    """Turn one JSONL line into a Case, or raise CaseProblem saying what is wrong with it."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as problem:
        raise CaseProblem(f"not valid JSON ({problem.msg}, column {problem.colno})") from None
    require(isinstance(data, dict), "a case must be one JSON object per line")
    unknown = sorted(set(data) - CASE_KEYS)
    require(not unknown, f"unknown key(s): {', '.join(unknown)}")
    case_id = data.get("id")
    require(isinstance(case_id, str) and case_id.strip(), '"id" must be a non-empty string')
    try:
        return build_case(data, default_category, where)
    except CaseProblem as problem:
        raise CaseProblem(f"case {case_id!r}: {problem}") from None


def build_case(data: dict, default_category: str, where: str) -> Case:
    """Check every field of a case (the category defaults to the file name, the zone to Madrid)."""
    category = data.get("category", default_category)
    tz = data.get("tz", DEFAULT_TZ)
    steps = data.get("steps")
    expect = data.get("expect", {})
    note = data.get("note", "")
    require(isinstance(category, str) and category.strip(), '"category" must be a non-empty string')
    require(isinstance(note, str), '"note" must be a string')
    local_datetime(data.get("now"), tz)                 # raises CaseProblem for an unknown zone or time
    require(isinstance(steps, list) and steps, '"steps" must be a non-empty list')
    for number, step in enumerate(steps, start=1):
        check_step(step, number)
    check_expect_block(expect)
    return Case(data["id"], category, data["now"], tz, steps, expect, where, note)


def local_datetime(text: Any, tz_name: Any) -> datetime:
    """The case's "now" (local time in tz_name, e.g. "2026-11-03T08:55") as an aware datetime."""
    require(isinstance(tz_name, str), '"tz" must be a time zone name like "Europe/Madrid"')
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        raise CaseProblem(f"unknown time zone {tz_name!r}") from None
    require(is_local_time_text(text), f'"now" must look like "2026-11-03T08:55", not {text!r}')
    naive = datetime.fromisoformat(text)
    local = naive.replace(tzinfo=zone)
    # A time in the spring-forward gap (02:30 on 28 Mar 2027 in Madrid) does not exist:
    # going to UTC and back gives a different clock time.
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise CaseProblem(f'"now" {text} does not exist in {tz_name} (the clocks skip it)')
    return local


def is_local_time_text(text: Any) -> bool:
    """True for a real date and time written "YYYY-MM-DDTHH:MM" or "YYYY-MM-DDTHH:MM:SS"."""
    if not isinstance(text, str) or not LOCAL_TIME.fullmatch(text):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:                                  # e.g. "2026-02-30T10:00"
        return False
    return True


def parse_wait(text: Any) -> int:
    """How long a wait step is, in seconds: "30s" -> 30, "90m" -> 5400, "2h" -> 7200, "1d" -> 86400."""
    if not isinstance(text, str) or not WAIT_TEXT.fullmatch(text.strip()):
        raise CaseProblem(f'a wait must look like "30s", "90m", "2h" or "1d", not {text!r}')
    return sum(int(amount) * SECONDS_PER_UNIT[unit] for amount, unit in WAIT_PART.findall(text))


def check_step(step: Any, number: int) -> None:
    """A step is {"say": text}, {"wait": "90m"}, {"tick": true} or {"tap": "done" or "later"}."""
    require(isinstance(step, dict) and len(step) == 1,
            f"step {number} must be an object with exactly one key: say, wait, tick or tap")
    action, value = next(iter(step.items()))
    if action == "say":
        require(isinstance(value, str), f'step {number}: "say" needs a string')
    elif action == "wait":
        try:
            parse_wait(value)
        except CaseProblem as problem:
            raise CaseProblem(f"step {number}: {problem}") from None
    elif action == "tick":
        require(value is True, f'step {number}: write a tick as {{"tick": true}}')
    elif action == "tap":
        require(value in ("done", "later"), f'step {number}: "tap" must be "done" or "later"')
    else:
        raise CaseProblem(f"step {number}: unknown step {action!r} (use say, wait, tick or tap)")


def check_expect_block(expect: Any) -> None:
    """Every expect key must be known and hold the right kind of value (catches typos in cases)."""
    require(isinstance(expect, dict), '"expect" must be an object')
    unknown = sorted(set(expect) - EXPECT_KEYS)
    require(not unknown, f"unknown expect key(s): {', '.join(unknown)}")
    if "last_reply_kind" in expect:
        require(expect["last_reply_kind"] in REPLY_KINDS, f"unknown reply kind {expect['last_reply_kind']!r}")
    if "reply_kinds" in expect:
        require(is_list_of_strings(expect["reply_kinds"]), '"reply_kinds" must be a list of reply kinds')
        for kind in expect["reply_kinds"]:
            require(kind in REPLY_KINDS, f"unknown reply kind {kind!r} in reply_kinds")
    for key in ("last_reply_contains", "last_reply_not_contains"):
        if key in expect:
            require(is_list_of_strings(expect[key]), f'"{key}" must be a list of strings')
    if "lang" in expect:
        require(expect["lang"] in ("en", "es"), '"lang" must be "en" or "es"')
    for key in COUNT_KEYS:
        if key in expect:
            require(is_count(expect[key]), f'"{key}" must be a whole number, 0 or more')
    if "commitments" in expect:
        require(isinstance(expect["commitments"], list), '"commitments" must be a list')
        for number, wanted in enumerate(expect["commitments"], start=1):
            check_expected_commitment(wanted, number)


def check_expected_commitment(wanted: Any, number: int) -> None:
    name = f"expected commitment {number}"
    require(isinstance(wanted, dict), f"{name} must be an object")
    unknown = sorted(set(wanted) - COMMITMENT_KEYS)
    require(not unknown, f"{name}: unknown key(s): {', '.join(unknown)}")
    if "title" in wanted:
        require(isinstance(wanted["title"], str), f'{name}: "title" must be a string')
    for key in ("title_contains", "title_not_contains"):
        if key in wanted:
            require(is_list_of_strings(wanted[key]), f'{name}: "{key}" must be a list of strings')
    if "nudge_at_local" in wanted:
        require(is_local_time_text(wanted["nudge_at_local"]),
                f'{name}: "nudge_at_local" must look like "2026-11-04T20:00", not {wanted["nudge_at_local"]!r}')
    if "time_source" in wanted:
        require(wanted["time_source"] in TIME_SOURCES, f'{name}: "time_source" must be one of {TIME_SOURCES}')
    if "status" in wanted:
        require(wanted["status"] in STATUSES, f'{name}: "status" must be one of {STATUSES}')
    if "deadline" in wanted:
        require(isinstance(wanted["deadline"], bool), f'{name}: "deadline" must be true or false')


# ---------------------------------------------------------------------------------------------
# Playing a case
# ---------------------------------------------------------------------------------------------
class FrozenClock:
    """Wazy's clock during a case: it only moves when the case says "wait".

    It counts in UTC, so "wait 1d" is always exactly 24 hours, even across a daylight saving change,
    and it hands Wazy the time in the case's zone.
    """

    def __init__(self, start: datetime) -> None:
        self.zone = start.tzinfo
        self.utc = start.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.utc.astimezone(self.zone)

    def advance(self, seconds: int) -> None:
        self.utc += timedelta(seconds=seconds)


def make_app(clock: FrozenClock, tz_name: str) -> Any:
    """A fresh App with an empty in-memory database (or the tests' fake App)."""
    if APP_FACTORY is not None:
        return APP_FACTORY(clock, tz_name)
    from wazy.app import App            # imported here so one broken module shows up as a crash per case
    return App(":memory:", clock=clock, default_tz=tz_name)


def run_case(case: Case) -> CaseResult:
    """Play one case against a fresh App, then check every expectation. Never raises."""
    result = CaseResult(case)
    app = None
    try:
        clock = FrozenClock(local_datetime(case.now, case.tz))
        app = make_app(clock, case.tz)
        player = Player(app, clock, result)
        for number, step in enumerate(case.steps, start=1):
            player.play(step, number)
        result.position = "reading the final state"
        state = app.state(USER_ID)
        result.failures.extend(check_expectations(case.expect, result.replies, state))
    except Exception as problem:        # a crash in Wazy is a result to report, not a reason to stop
        result.failures.append(Failure("crash", f"crash while {result.position}", "no exception",
                                       f"{type(problem).__name__}: {problem}"))
        result.traceback = traceback.format_exc()
    finally:
        close_quietly(app)
    return result


def close_quietly(app: Any) -> None:
    if app is None or not hasattr(app, "close"):
        return
    try:
        app.close()
    except Exception:
        pass


class Player:
    """Plays one case's steps against one App and collects Wazy's replies in result.replies."""

    def __init__(self, app: Any, clock: FrozenClock, result: CaseResult) -> None:
        self.app = app
        self.clock = clock
        self.result = result
        self.said_anything = False

    def play(self, step: dict, number: int) -> None:
        action, value = next(iter(step.items()))
        self.result.position = f"step {number} ({action})"
        if action == "say":
            self.say(value, number)
        elif action == "wait":
            self.clock.advance(parse_wait(value))
        elif action == "tick":
            self.tick(number)
        else:
            self.tap(value, number)

    def say(self, text: str, number: int) -> None:
        replies = [as_reply(reply, number) for reply in self.app.handle(USER_ID, text)]
        if not self.said_anything and replies and replies[0]["kind"] == "welcome":
            replies = replies[1:]           # the welcome on first contact does not count (SPEC 9)
        self.said_anything = True
        self.result.replies.extend(replies)

    def tick(self, number: int) -> None:
        """Run the scheduler once. tick() only returns a count, so the nudges it sent are read from the chat."""
        before = newest_message_id(self.chat())
        self.app.tick()
        new = [message for message in self.chat().get("messages", [])
               if message.get("role") == "bot" and (message.get("id") or 0) > before]
        self.result.replies.extend(as_reply(message, number) for message in new)

    def tap(self, action: str, number: int) -> None:
        """Press Done or Later on the newest open nudge, like a person tapping the button."""
        commitment_id = newest_open_commitment(self.chat())
        if commitment_id is None:
            self.result.failures.append(Failure("flow", f"step {number} (tap {action})",
                                                "an open nudge to tap", "no open nudge"))
            return
        self.result.replies.extend(as_reply(reply, number) for reply in self.app.tap(USER_ID, commitment_id, action))

    def chat(self) -> dict:
        """The person's chat as Wazy shows it. Before their first message there is nobody to ask about."""
        return self.app.state(USER_ID) if self.said_anything else {}


def as_reply(reply: Any, number: int) -> dict:
    """A Reply (from handle or tap) or a chat message (from state) as {"step", "kind", "text", "lang"}."""
    if isinstance(reply, dict):
        kind, text, lang = reply.get("kind"), reply.get("text"), reply.get("lang")
    else:
        kind, text, lang = getattr(reply, "kind", None), getattr(reply, "text", ""), getattr(reply, "lang", None)
    return {"step": number, "kind": kind, "text": text or "", "lang": lang}


def newest_message_id(state: dict) -> int:
    return max((message.get("id") or 0 for message in state.get("messages", [])), default=0)


def newest_open_commitment(state: dict) -> Optional[int]:
    """The commitment id of the newest nudge still waiting for Done or Later, or None."""
    open_ids = state.get("open") or []
    if not open_ids:
        return None
    newest = max(open_ids)
    for message in state.get("messages", []):
        if message.get("id") == newest:
            return message.get("commitment_id")
    return None


# ---------------------------------------------------------------------------------------------
# Checking the expectations
# ---------------------------------------------------------------------------------------------
def check_expectations(expect: dict, replies: List[dict], state: dict) -> List[Failure]:
    """Every expect key of a case against Wazy's replies and final state. Returns the failures."""
    failures = check_replies(expect, replies)
    failures += check_counts(expect, state)
    rows = commitments_in_order(state)
    for number, wanted in enumerate(expect.get("commitments", []), start=1):
        failures += check_commitment(number, wanted, rows)
    return failures


def check_replies(expect: dict, replies: List[dict]) -> List[Failure]:
    failures = []
    last = replies[-1] if replies else {"kind": None, "text": "", "lang": None}
    kinds = [reply["kind"] for reply in replies]
    if "last_reply_kind" in expect and last["kind"] != expect["last_reply_kind"]:
        wanted = expect["last_reply_kind"]
        failures.append(Failure(blame_kinds(wanted, last["kind"]), "last_reply_kind", wanted, last["kind"]))
    if "reply_kinds" in expect and kinds != expect["reply_kinds"]:
        wanted = expect["reply_kinds"]
        failures.append(Failure(blame_sequence(wanted, kinds), "reply_kinds", wanted, kinds))
    text = last["text"].lower()
    for part in expect.get("last_reply_contains", []):
        if part.lower() not in text:
            failures.append(Failure("reply", "last_reply_contains", part, last["text"]))
    for part in expect.get("last_reply_not_contains", []):
        if part.lower() in text:
            failures.append(Failure("reply", "last_reply_not_contains", part, last["text"]))
    if "lang" in expect and last["lang"] != expect["lang"]:
        failures.append(Failure("language", "lang", expect["lang"], last["lang"]))
    return failures


def blame_kinds(expected: Optional[str], actual: Optional[str]) -> str:
    """Which step to blame when Wazy replied with the wrong kind of reply."""
    if expected in INTENT_KINDS or actual in INTENT_KINDS:
        return "intent"
    if expected in TIME_KINDS and actual in TIME_KINDS:
        return "time"
    return "flow"


def blame_sequence(expected: List[str], actual: List[str]) -> str:
    """Blame the first pair of kinds that differ. If one list only stops early, the flow is wrong."""
    for wanted, got in zip(expected, actual):
        if wanted != got:
            return blame_kinds(wanted, got)
    return "flow"


def check_counts(expect: dict, state: dict) -> List[Failure]:
    actual = {
        "commitment_count": len(state.get("commitments", [])),
        "nudges_sent": sum(1 for message in state.get("messages", [])
                           if message.get("role") == "bot" and message.get("kind") == "nudge"),
        "reader_misses": len(state.get("reader_misses", [])),
    }
    return [Failure("flow", key, expect[key], actual[key])
            for key in COUNT_KEYS if key in expect and expect[key] != actual[key]]


def commitments_in_order(state: dict) -> List[dict]:
    """The person's commitments oldest first (state() may list them newest first)."""
    return sorted(state.get("commitments", []), key=lambda row: row.get("id") or 0)


def check_commitment(number: int, wanted: dict, rows: List[dict]) -> List[Failure]:
    """One expected commitment (number 1 = the first one created) against the real one."""
    name = f"commitment {number}"
    if number > len(rows):
        return [Failure("flow", name, "a commitment", f"only {len(rows)} commitment(s)")]
    row = rows[number - 1]
    title = row.get("title") or ""
    failures = []
    if "title" in wanted and title != wanted["title"]:
        failures.append(Failure("title", f"{name} title", wanted["title"], title))
    for part in wanted.get("title_contains", []):
        if part.lower() not in title.lower():
            failures.append(Failure("title", f"{name} title_contains", part, title))
    for part in wanted.get("title_not_contains", []):
        if part.lower() in title.lower():
            failures.append(Failure("title", f"{name} title_not_contains", part, title))
    local = str(row.get("nudge_at_local") or "")
    if "nudge_at_local" in wanted and not local.startswith(wanted["nudge_at_local"]):
        failures.append(Failure("time", f"{name} nudge_at_local", wanted["nudge_at_local"], local))
    if "time_source" in wanted and row.get("time_source") != wanted["time_source"]:
        failures.append(Failure("time", f"{name} time_source", wanted["time_source"], row.get("time_source")))
    if "deadline" in wanted and bool(row.get("deadline")) != wanted["deadline"]:
        failures.append(Failure("time", f"{name} deadline", wanted["deadline"], bool(row.get("deadline"))))
    if "status" in wanted and row.get("status") != wanted["status"]:
        failures.append(Failure("flow", f"{name} status", wanted["status"], row.get("status")))
    return failures


# ---------------------------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------------------------
def percent(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def by_category(results: List[CaseResult]) -> Dict[str, Tuple[int, int]]:
    """{category: (cases, passed)}, sorted by category name."""
    table: Dict[str, Tuple[int, int]] = {}
    for result in results:
        cases, passed = table.get(result.case.category, (0, 0))
        table[result.case.category] = (cases + 1, passed + (1 if result.passed else 0))
    return dict(sorted(table.items()))


def by_step(results: List[CaseResult]) -> List[Tuple[str, int]]:
    """Every step with its number of failing cases, most failures first (ties in pipeline order)."""
    counts = {step: 0 for step in STEPS}
    for result in results:
        if not result.passed:
            counts[result.step] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], STEPS.index(item[0])))


def describe_steps(steps: List[dict]) -> str:
    """The case's input on one line: say "..." | wait 2h | tick | tap done."""
    parts = []
    for step in steps:
        action, value = next(iter(step.items()))
        if action == "say":
            parts.append(f"say {show(value, None)}")
        elif action == "tick":
            parts.append("tick")
        else:
            parts.append(f"{action} {value}")
    return " | ".join(parts)


def failing(results: List[CaseResult]) -> List[CaseResult]:
    return [result for result in results if not result.passed]


# ---------------------------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------------------------
def print_summary(results: List[CaseResult], errors: List[CaseError], verbose: bool) -> None:
    passed = len(results) - len(failing(results))
    print(f"Wazy evals: {passed}/{len(results)} cases pass ({percent(passed, len(results))}), "
          f"{len(failing(results))} fail, {len(errors)} case error(s)")
    print()
    print(f"  {'Category':<22} {'Cases':>6} {'Passed':>7} {'%':>7}")
    for category, (cases, ok) in by_category(results).items():
        print(f"  {category:<22} {cases:>6} {ok:>7} {percent(ok, cases):>7}")
    print()
    print("Which step fails most often (each failing case blamed once):")
    for step, count in by_step(results):
        print(f"  {step:<9} {count:>5}   {STEP_MEANING[step]}")
    if errors:
        print()
        print(f"Case errors ({len(errors)}): these lines were skipped")
        for error in errors:
            print(f"  {error.where}: {error.message}")
    if verbose:
        for result in failing(results):
            print_failure(result)
    elif failing(results):
        print()
        print("First failing cases (all of them with --verbose):")
        for result in failing(results)[:10]:
            print(f"  {result.case.id} [{result.step}] {result.reason().line()}")


def print_failure(result: CaseResult) -> None:
    """Everything about one failing case: input, Wazy's replies, and expected vs actual."""
    case = result.case
    print()
    print(f"FAIL {case.id} [{case.category}] blamed on: {result.step}   ({case.where})")
    print(f"  now:     {case.now} {case.tz}")
    print(f"  input:   {describe_steps(case.steps)}")
    for reply in result.replies:
        print(f"  reply:   step {reply['step']} {reply['kind']}/{reply['lang']}: {show(reply['text'], None)}")
    for failure in result.failures:
        print(f"  - [{failure.step}] {failure.line(None)}")
    if result.traceback:
        print("  traceback (last lines):")
        for line in result.traceback.strip().splitlines()[-6:]:
            print(f"    {line}")


# ---------------------------------------------------------------------------------------------
# REPORT.md and JSON
# ---------------------------------------------------------------------------------------------
def before_and_after_section(text: str) -> str:
    """The hand-written "## Before and after" section of an old report, exactly as it was ("" if none).

    It runs from its heading to the next "#" or "##" heading outside a code block, or to the end.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith(BEFORE_AND_AFTER)), None)
    if start is None:
        return ""
    end = start + 1
    in_code = False
    while end < len(lines):
        line = lines[end]
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"#{1,2} ", line):
            break
        end += 1
    return "".join(lines[start:end])


def latest_run_section(results: List[CaseResult], errors: List[CaseError], run_at: str,
                       filters: str = "") -> str:
    """The generated "## Latest run" part of REPORT.md."""
    bad = failing(results)
    passed = len(results) - len(bad)
    lines = ["## Latest run", "",
             f"Run on {run_at} with Python {sys.version.split()[0]} (`python3.9 evals/run.py`).{filters}", "",
             f"**{passed} of {len(results)} cases pass ({percent(passed, len(results))}).** "
             f"{len(bad)} fail. {len(errors)} case error(s).", "",
             "### By category", "",
             "| Category | Cases | Passed | % |", "|---|---:|---:|---:|"]
    for category, (cases, ok) in by_category(results).items():
        lines.append(f"| {category} | {cases} | {ok} | {percent(ok, cases)} |")
    lines += [f"| **Total** | {len(results)} | {passed} | {percent(passed, len(results))} |", "",
              "### Which step fails most often", "",
              "Each failing case is blamed on its most basic failing step, in this order: "
              + ", ".join(STEPS) + ".", "",
              "| Step | Failing cases | Share of failures | What it means |", "|---|---:|---:|---|"]
    for step, count in by_step(results):
        lines.append(f"| {step} | {count} | {percent(count, len(bad))} | {STEP_MEANING[step]} |")
    lines += ["", "### Failing cases", ""]
    lines += [f"- `{r.case.id}` ({r.case.category}, **{r.step}**): {r.reason().line()}" for r in bad[:MAX_LISTED]]
    if len(bad) > MAX_LISTED:
        lines.append(f"- ... and {len(bad) - MAX_LISTED} more (run with `--verbose` to see all)")
    if not bad:
        lines.append("None. Every case passes.")
    if errors:
        lines += ["", "### Case errors (skipped lines)", ""]
        lines += [f"- `{error.where}`: {error.message}" for error in errors[:MAX_LISTED]]
    return "\n".join(lines) + "\n"


def write_report(path: Path, latest: str) -> None:
    """Write REPORT.md: the hand-written "Before and after" section (kept as it was), then the latest run."""
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    kept = before_and_after_section(old)
    parts = ["# Wazy eval report", ""]
    if kept:
        parts += [kept.rstrip("\n"), ""]
    parts.append(latest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def results_as_json(results: List[CaseResult], errors: List[CaseError], run_at: str) -> dict:
    """Everything the run found, for other programs to read."""
    passed = len(results) - len(failing(results))
    return {
        "run_at": run_at,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(100.0 * passed / len(results), 1) if results else 0.0,
        "by_category": {category: {"cases": cases, "passed": ok}
                        for category, (cases, ok) in by_category(results).items()},
        "by_step": dict(by_step(results)),
        "case_errors": [{"where": error.where, "message": error.message} for error in errors],
        "cases": [{
            "id": r.case.id, "category": r.case.category, "where": r.case.where, "passed": r.passed,
            "step": r.step, "input": describe_steps(r.case.steps), "replies": r.replies,
            "failures": [{"step": f.step, "check": f.check, "expected": f.expected, "actual": f.actual}
                         for f in r.failures],
            "traceback": r.traceback,
        } for r in results],
    }


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Wazy's eval cases (SPEC.md section 9).")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_DIR), help="folder with the *.jsonl case files")
    parser.add_argument("--category", action="append", help="only this category (repeat for more)")
    parser.add_argument("--id", action="append", help="only this case id (repeat for more)")
    parser.add_argument("--verbose", action="store_true", help="show every failure in full")
    parser.add_argument("--report", help="where to write the Markdown report (default evals/REPORT.md)")
    parser.add_argument("--json", help="also write every result as JSON to this file")
    parser.add_argument("--no-report", action="store_true", help="do not write the Markdown report")
    return parser.parse_args(argv)


def report_path(args: argparse.Namespace) -> Optional[Path]:
    """Where to write REPORT.md. A filtered run does not overwrite the full report unless --report is given."""
    if args.no_report:
        return None
    if args.report:
        return Path(args.report)
    if args.category or args.id:
        return None
    return DEFAULT_REPORT


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cases_dir = Path(args.cases)
    if not cases_dir.is_dir():
        print(f"No such folder: {cases_dir}")
        return 2
    cases, errors = load_cases(cases_dir)
    chosen = [case for case in cases
              if (not args.category or case.category in args.category) and (not args.id or case.id in args.id)]
    if not chosen:
        print(f"No cases to run in {cases_dir}" + (" with that --category/--id." if cases else "."))
        for error in errors:
            print(f"  case error {error.where}: {error.message}")
        return 2
    results = [run_case(case) for case in chosen]
    print_summary(results, errors, args.verbose)

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    filters = ""
    if args.category or args.id:
        filters = f" Only some cases: category {args.category or 'any'}, id {args.id or 'any'}."
    path = report_path(args)
    if path is not None:
        write_report(path, latest_run_section(results, errors, run_at, filters))
        print(f"\nReport written to {path}")
    if args.json:
        Path(args.json).write_text(json.dumps(results_as_json(results, errors, run_at), ensure_ascii=False,
                                              indent=1, default=str), encoding="utf-8")
        print(f"JSON results written to {args.json}")

    if errors:
        return 2
    return 0 if not failing(results) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")     # a terminal that cannot show “ or ñ must not stop the run
    sys.exit(main())
