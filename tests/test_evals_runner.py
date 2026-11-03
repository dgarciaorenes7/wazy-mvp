"""Tests for evals/run.py, the eval runner.

They use FakeApp, a tiny stand-in for wazy.app.App plugged in through run.APP_FACTORY,
so the runner can be tested before (and independently of) the real App.
evals/ is not a package, so the runner is loaded from its file path.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from wazy.types import Reply

RUN_PATH = Path(__file__).resolve().parent.parent / "evals" / "run.py"
_spec = importlib.util.spec_from_file_location("wazy_evals_run", RUN_PATH)
run = importlib.util.module_from_spec(_spec)
sys.modules["wazy_evals_run"] = run          # dataclasses look the module up while it loads
_spec.loader.exec_module(run)

MADRID = ZoneInfo("Europe/Madrid")


class FakeApp:
    """A tiny Wazy that understands a few made-up messages:

      "add <title> in <N>m"   a commitment due in N minutes -> "confirm"
      "hi"                    -> "welcome"
      "hola"                  -> "ask_time" in Spanish
      "boom"                  -> raises RuntimeError
      anything else           -> "ask_time" and one reader_misses row
    The very first message also gets a "welcome" first, like the real App.
    """

    def __init__(self, clock, tz_name):
        self.clock = clock
        self.tz_name = tz_name
        self.messages = []
        self.commitments = []
        self.open = []
        self.misses = []
        self.closed = False
        self.state_calls = 0

    def _store(self, role, text, kind, lang="en", commitment_id=None):
        message = {"id": len(self.messages) + 1, "role": role, "text": text, "kind": kind, "lang": lang,
                   "commitment_id": commitment_id, "created_at": "2026-11-03T08:00:00Z"}
        self.messages.append(message)
        return message

    def _reply(self, kind, text, lang="en", commitment_id=None):
        self._store("bot", text, kind, lang, commitment_id)
        return Reply(kind, text, lang, commitment_id, kind == "nudge")

    def handle(self, user_id, text, *, channel="web"):
        first = not self.messages
        self._store("user", text, "text")
        replies = [self._reply("welcome", "Hi, I'm Wazy.")] if first else []
        if text == "boom":
            raise RuntimeError("boom")
        added = re.fullmatch(r"add (.+) in (\d+)m", text)
        if added:
            title, minutes = added.group(1), int(added.group(2))
            cid = len(self.commitments) + 1
            self.commitments.append({"id": cid, "title": title, "due": self.clock() + timedelta(minutes=minutes),
                                     "status": "scheduled"})
            replies.append(self._reply("confirm", f"Got it: “{title}”. I'll nudge you in {minutes} min.",
                                       commitment_id=cid))
        elif text == "hi":
            replies.append(self._reply("welcome", "Hi again."))
        elif text == "hola":
            replies.append(self._reply("ask_time", "¿Cuándo te aviso?", "es"))
        else:
            self.misses.append({"id": len(self.misses) + 1, "problem": "ask_time"})
            replies.append(self._reply("ask_time", f"When should I nudge you about “{text}”?"))
        return replies

    def tick(self):
        sent = 0
        for commitment in self.commitments:
            if commitment["status"] == "scheduled" and commitment["due"] <= self.clock():
                commitment["status"] = "nudged"
                message = self._store("bot", f"Time for “{commitment['title']}”", "nudge", "en", commitment["id"])
                self.open.append(message["id"])
                sent += 1
        return sent

    def tap(self, user_id, commitment_id, action):
        for message_id in list(self.open):
            if self.messages[message_id - 1]["commitment_id"] != commitment_id:
                continue
            self.open.remove(message_id)
            self._store("user", action.title(), "text")
            commitment = self.commitments[commitment_id - 1]
            if action == "done":
                commitment["status"] = "done"
                return [self._reply("done_ack", f"Nice, “{commitment['title']}” done.")]
            commitment["status"] = "scheduled"
            commitment["due"] = self.clock() + timedelta(minutes=30)
            return [self._reply("later_ack", f"OK, “{commitment['title']}” later.")]
        return []

    def state(self, user_id):
        self.state_calls += 1
        zone = self.clock().tzinfo
        rows = [{"id": c["id"], "title": c["title"], "status": c["status"], "time_source": "stated",
                 "deadline": 0, "nudge_at_local": c["due"].astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")}
                for c in self.commitments]
        return {"messages": list(self.messages), "open": list(self.open),
                "commitments": list(reversed(rows)),          # newest first, like "latest 50"
                "reader_misses": list(self.misses)}

    def close(self):
        self.closed = True


def make_case(steps, expect=None, now="2026-11-03T09:00:00", tz="Europe/Madrid", case_id="t-001"):
    return run.Case(case_id, "test", now, tz, steps, expect or {}, "test.jsonl:1")


def reply(kind, text="", lang="en"):
    return {"step": 1, "kind": kind, "text": text, "lang": lang}


def checks(failures):
    return [(failure.step, failure.check) for failure in failures]


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.apps = []

        def factory(clock, tz_name):
            app = FakeApp(clock, tz_name)
            self.apps.append(app)
            return app

        run.APP_FACTORY = factory

    def tearDown(self):
        run.APP_FACTORY = None

    def write_cases(self, folder, name, lines):
        path = Path(folder) / name
        path.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n",
                        encoding="utf-8")
        return path

    def main(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = run.main(list(args))
        return code, output.getvalue()


# ---------------------------------------------------------------------------------------------
class WaitAndClockTests(unittest.TestCase):
    def test_wait_units(self):
        self.assertEqual(run.parse_wait("30s"), 30)
        self.assertEqual(run.parse_wait("90m"), 90 * 60)
        self.assertEqual(run.parse_wait("2h"), 2 * 3600)
        self.assertEqual(run.parse_wait("1d"), 86400)
        self.assertEqual(run.parse_wait("1h30m"), 5400)

    def test_bad_waits_raise(self):
        for bad in ("2 hours", "", "5x", "h2", 90, None, "-1h"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    run.parse_wait(bad)

    def test_clock_waits_real_time_across_dst(self):
        # 01:30 summer time on 25 Oct 2026; two real hours later the clocks have gone back: 02:30 winter time.
        clock = run.FrozenClock(run.local_datetime("2026-10-25T01:30", "Europe/Madrid"))
        self.assertEqual(clock().utcoffset(), timedelta(hours=2))
        clock.advance(run.parse_wait("2h"))
        self.assertEqual((clock().hour, clock().minute), (2, 30))
        self.assertEqual(clock().utcoffset(), timedelta(hours=1))
        self.assertEqual(clock().tzinfo, MADRID)

    def test_now_in_spring_gap_is_a_case_problem(self):
        with self.assertRaises(run.CaseProblem):
            run.local_datetime("2027-03-28T02:30", "Europe/Madrid")

    def test_now_other_zone(self):
        local = run.local_datetime("2026-11-03T08:55:00", "Europe/London")
        self.assertEqual(local.astimezone(timezone.utc), datetime(2026, 11, 3, 8, 55, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------------------------
class StepTests(RunnerTestCase):
    def test_say_wait_tick_tap_done(self):
        case = make_case([{"say": "add call mum in 30m"}, {"wait": "30m"}, {"tick": True}, {"tap": "done"}],
                         {"reply_kinds": ["confirm", "nudge", "done_ack"], "last_reply_kind": "done_ack",
                          "nudges_sent": 1, "commitments": [{"title": "call mum", "status": "done"}]})
        result = run.run_case(case)
        self.assertEqual(result.failures, [])
        self.assertEqual([r["step"] for r in result.replies], [1, 3, 4])
        self.assertTrue(self.apps[0].closed)

    def test_tick_before_due_sends_nothing(self):
        case = make_case([{"say": "add call mum in 30m"}, {"wait": "29m"}, {"tick": True}],
                         {"reply_kinds": ["confirm"], "nudges_sent": 0})
        self.assertTrue(run.run_case(case).passed)

    def test_wait_moves_the_clock_wazy_sees(self):
        case = make_case([{"wait": "1d"}, {"say": "add x in 0m"}],
                         {"commitments": [{"nudge_at_local": "2026-11-04T09:00"}]})
        self.assertTrue(run.run_case(case).passed)

    def test_tap_later_reschedules(self):
        case = make_case([{"say": "add a in 1m"}, {"wait": "1m"}, {"tick": True}, {"tap": "later"},
                          {"wait": "30m"}, {"tick": True}],
                         {"reply_kinds": ["confirm", "nudge", "later_ack", "nudge"], "nudges_sent": 2,
                          "commitments": [{"status": "nudged", "nudge_at_local": "2026-11-03T09:31"}]})
        result = run.run_case(case)
        self.assertEqual(result.failures, [])

    def test_tap_picks_the_newest_open_nudge(self):
        case = make_case([{"say": "add first in 1m"}, {"say": "add second in 2m"}, {"wait": "2m"}, {"tick": True},
                          {"tap": "done"}],
                         {"last_reply_contains": ["second"],
                          "commitments": [{"status": "nudged"}, {"status": "done"}]})
        self.assertEqual(run.run_case(case).failures, [])

    def test_tap_without_open_nudge_is_a_flow_failure(self):
        result = run.run_case(make_case([{"say": "add a in 5m"}, {"tap": "done"}]))
        self.assertEqual(checks(result.failures), [("flow", "step 2 (tap done)")])
        self.assertEqual(result.step, "flow")

    def test_steps_before_first_message_do_not_touch_state(self):
        result = run.run_case(make_case([{"tick": True}, {"tap": "done"}, {"say": "add a in 5m"}],
                                        {"reply_kinds": ["confirm"]}))
        self.assertEqual(checks(result.failures), [("flow", "step 2 (tap done)")])
        self.assertEqual(self.apps[0].state_calls, 1)          # only the final state read

    def test_app_gets_case_zone_and_clock(self):
        run.run_case(make_case([{"say": "hola"}], now="2026-11-03T23:59:30", tz="Europe/London"))
        app = self.apps[0]
        self.assertEqual(app.tz_name, "Europe/London")
        self.assertEqual(app.clock().isoformat(), "2026-11-03T23:59:30+00:00")


class WelcomeTests(RunnerTestCase):
    def test_first_contact_welcome_is_dropped(self):
        result = run.run_case(make_case([{"say": "add a in 5m"}], {"reply_kinds": ["confirm"],
                                                                   "last_reply_kind": "confirm"}))
        self.assertEqual(result.failures, [])

    def test_a_later_welcome_counts(self):
        result = run.run_case(make_case([{"say": "add a in 5m"}, {"say": "hi"}],
                                        {"reply_kinds": ["confirm", "welcome"], "last_reply_kind": "welcome"}))
        self.assertEqual(result.failures, [])

    def test_greeting_as_first_message_only_drops_the_first_welcome(self):
        result = run.run_case(make_case([{"say": "hi"}], {"reply_kinds": ["welcome"]}))
        self.assertEqual(result.failures, [])


# ---------------------------------------------------------------------------------------------
class ExpectReplyTests(unittest.TestCase):
    def test_last_reply_kind(self):
        replies = [reply("ask_time"), reply("confirm")]
        self.assertEqual(run.check_expectations({"last_reply_kind": "confirm"}, replies, {}), [])
        failures = run.check_expectations({"last_reply_kind": "ask_time"}, replies, {})
        self.assertEqual(checks(failures), [("time", "last_reply_kind")])
        self.assertEqual((failures[0].expected, failures[0].actual), ("ask_time", "confirm"))

    def test_last_reply_kind_with_no_reply(self):
        failures = run.check_expectations({"last_reply_kind": "confirm"}, [], {})
        self.assertEqual(checks(failures), [("flow", "last_reply_kind")])

    def test_reply_kinds(self):
        replies = [reply("ask_time"), reply("confirm")]
        self.assertEqual(run.check_expectations({"reply_kinds": ["ask_time", "confirm"]}, replies, {}), [])
        self.assertEqual(checks(run.check_expectations({"reply_kinds": ["ask_time"]}, replies, {})),
                         [("flow", "reply_kinds")])

    def test_contains_is_case_insensitive(self):
        replies = [reply("confirm", "Got it: “Call Mum”. I'll nudge you TOMORROW at 09:00.")]
        expect = {"last_reply_contains": ["call mum", "tomorrow at 09:00"], "last_reply_not_contains": ["25:00"]}
        self.assertEqual(run.check_expectations(expect, replies, {}), [])
        failures = run.check_expectations({"last_reply_contains": ["friday"],
                                           "last_reply_not_contains": ["CALL MUM"]}, replies, {})
        self.assertEqual(checks(failures), [("reply", "last_reply_contains"), ("reply", "last_reply_not_contains")])

    def test_lang(self):
        replies = [reply("confirm", "Hecho", "es")]
        self.assertEqual(run.check_expectations({"lang": "es"}, replies, {}), [])
        self.assertEqual(checks(run.check_expectations({"lang": "en"}, replies, {})), [("language", "lang")])


class ExpectStateTests(unittest.TestCase):
    STATE = {
        "messages": [
            {"id": 1, "role": "user", "kind": "text"},
            {"id": 2, "role": "bot", "kind": "nudge"},
            {"id": 3, "role": "bot", "kind": "nudge"},
            {"id": 4, "role": "bot", "kind": "done_ack"},
        ],
        "commitments": [   # newest first, as state() may give them
            {"id": 2, "title": "Pay rent", "nudge_at_local": "2026-11-05T10:00:00", "status": "scheduled",
             "time_source": "default", "deadline": 1},
            {"id": 1, "title": "Call mum", "nudge_at_local": "2026-11-04T09:00:00", "status": "done",
             "time_source": "stated", "deadline": 0},
        ],
        "reader_misses": [{"id": 1}],
    }

    def check(self, expect):
        return checks(run.check_expectations(expect, [], self.STATE))

    def test_counts_pass(self):
        self.assertEqual(self.check({"commitment_count": 2, "nudges_sent": 2, "reader_misses": 1}), [])

    def test_counts_fail(self):
        self.assertEqual(self.check({"commitment_count": 1, "nudges_sent": 0, "reader_misses": 0}),
                         [("flow", "commitment_count"), ("flow", "nudges_sent"), ("flow", "reader_misses")])

    def test_commitments_in_creation_order_pass(self):
        expect = {"commitments": [
            {"title": "Call mum", "title_contains": ["MUM"], "title_not_contains": ["tomorrow"],
             "nudge_at_local": "2026-11-04T09:00", "time_source": "stated", "status": "done", "deadline": False},
            {"title": "Pay rent", "nudge_at_local": "2026-11-05T10:00:00", "time_source": "default",
             "status": "scheduled", "deadline": True},
        ]}
        self.assertEqual(self.check(expect), [])

    def test_commitment_fields_fail_with_their_steps(self):
        expect = {"commitments": [{"title": "Call Mum", "title_contains": ["dad"], "title_not_contains": ["CALL"],
                                   "nudge_at_local": "2026-11-04T09:30", "time_source": "default",
                                   "deadline": True, "status": "scheduled"}]}
        self.assertEqual(self.check(expect), [
            ("title", "commitment 1 title"), ("title", "commitment 1 title_contains"),
            ("title", "commitment 1 title_not_contains"), ("time", "commitment 1 nudge_at_local"),
            ("time", "commitment 1 time_source"), ("time", "commitment 1 deadline"),
            ("flow", "commitment 1 status"),
        ])

    def test_nudge_at_local_seconds_must_match_when_given(self):
        self.assertEqual(self.check({"commitments": [{"nudge_at_local": "2026-11-04T09:00:30"}]}),
                         [("time", "commitment 1 nudge_at_local")])

    def test_missing_commitment_is_flow(self):
        self.assertEqual(self.check({"commitments": [{}, {}, {"title": "x"}]}), [("flow", "commitment 3")])


# ---------------------------------------------------------------------------------------------
class BlameTests(RunnerTestCase):
    def test_kind_pairs(self):
        table = [
            ("confirm", "ask_time", "time"), ("past_time", "invalid_time", "time"),
            ("recurring_unsupported", "confirm", "time"), ("ask_what", "multiple_commitments", "time"),
            ("not_commitment", "confirm", "intent"), ("confirm", "injection", "intent"),
            ("list", "ask_time", "intent"), ("learning_explained", "ask_what", "intent"),
            ("done_ack", "no_open_nudge", "intent"), ("confirm", "nudge", "flow"),
            ("confirm", None, "flow"), ("rate_limited", "ask_time", "flow"),
        ]
        for expected, actual, step in table:
            with self.subTest(expected=expected, actual=actual):
                self.assertEqual(run.blame_kinds(expected, actual), step)

    def test_sequence_blames_first_difference(self):
        self.assertEqual(run.blame_sequence(["invalid_time", "confirm"], ["past_time", "confirm"]), "time")
        self.assertEqual(run.blame_sequence(["injection", "confirm"], ["ask_time", "confirm"]), "intent")
        self.assertEqual(run.blame_sequence(["ask_time", "confirm"], ["ask_time"]), "flow")
        self.assertEqual(run.blame_sequence(["confirm"], ["confirm", "nudge"]), "flow")

    def test_case_is_blamed_on_most_basic_step(self):
        result = run.CaseResult(make_case([{"say": "x"}]))
        result.failures = [run.Failure("flow", "a", 1, 2), run.Failure("reply", "b", 1, 2),
                           run.Failure("title", "c", 1, 2), run.Failure("time", "d", 1, 2)]
        self.assertEqual(result.step, "time")
        self.assertEqual(result.reason().check, "d")
        result.failures.append(run.Failure("intent", "e", 1, 2))
        self.assertEqual(result.step, "intent")

    def test_each_step_end_to_end(self):
        cases = {
            "intent": ([{"say": "hola"}], {"last_reply_kind": "injection"}),
            "time": ([{"say": "add a in 5m"}], {"commitments": [{"nudge_at_local": "2026-11-03T10:00"}]}),
            "title": ([{"say": "add a in 5m"}], {"commitments": [{"title": "b"}]}),
            "language": ([{"say": "hola"}], {"lang": "en", "last_reply_contains": ["nope"]}),
            "reply": ([{"say": "hola"}], {"last_reply_contains": ["nope"], "commitment_count": 3}),
            "flow": ([{"say": "hola"}], {"commitment_count": 1}),
        }
        for step, (steps, expect) in cases.items():
            with self.subTest(step=step):
                self.assertEqual(run.run_case(make_case(steps, expect)).step, step)

    def test_crash_in_app(self):
        result = run.run_case(make_case([{"say": "add a in 5m"}, {"say": "boom"}], {"last_reply_kind": "confirm"}))
        self.assertEqual(result.step, "crash")
        self.assertEqual(len(result.failures), 1)
        self.assertIn("step 2 (say)", result.failures[0].check)
        self.assertIn("RuntimeError: boom", result.failures[0].actual)
        self.assertIn("Traceback", result.traceback)
        self.assertTrue(self.apps[0].closed)

    def test_crash_creating_app(self):
        def broken(clock, tz_name):
            raise ImportError("no wazy.app yet")

        run.APP_FACTORY = broken
        result = run.run_case(make_case([{"say": "x"}]))
        self.assertEqual(result.step, "crash")
        self.assertIn("creating the App", result.failures[0].check)


# ---------------------------------------------------------------------------------------------
GOOD = {"id": "g-1", "category": "good", "now": "2026-11-03T09:00", "steps": [{"say": "add a in 5m"}],
        "expect": {"last_reply_kind": "confirm"}}


def good(**changes):
    case = dict(GOOD)
    case.update(changes)
    return case


class LoadingTests(RunnerTestCase):
    def test_loads_cases_with_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            case = good()
            del case["category"]
            self.write_cases(folder, "en_clock.jsonl", [case, "", good(id="g-2", tz="Europe/London", note="n")])
            cases, errors = run.load_cases(Path(folder))
        self.assertEqual(errors, [])
        self.assertEqual([(c.id, c.category, c.tz) for c in cases],
                         [("g-1", "en_clock", "Europe/Madrid"), ("g-2", "good", "Europe/London")])
        self.assertEqual(cases[1].where, "en_clock.jsonl:3")

    def test_malformed_lines_are_case_errors(self):
        bad_lines = [
            "{not json",
            "[1, 2]",
            good(id="u", expected={}),
            {k: v for k, v in GOOD.items() if k != "id"},
            good(id="s1", steps=[]),
            good(id="s2", steps=[{"say": "a", "wait": "1h"}]),
            good(id="s3", steps=[{"wait": "2 hours"}]),
            good(id="s4", steps=[{"tap": "maybe"}]),
            good(id="s5", steps=[{"tick": "yes"}]),
            good(id="s6", steps=[{"jump": 1}]),
            good(id="e1", expect={"last_reply": "confirm"}),
            good(id="e2", expect={"last_reply_kind": "confirmed"}),
            good(id="e3", expect={"commitments": [{"nudge_at_local": "2026-11-04 20:00"}]}),
            good(id="e4", expect={"commitments": [{"titel": "x"}]}),
            good(id="e5", expect={"last_reply_contains": "x"}),
            good(id="e6", expect={"commitment_count": True}),
            good(id="e7", expect={"commitments": [{"deadline": 1}]}),
            good(id="t1", tz="Mars/Olympus"),
            good(id="t2", now="2026-02-30T10:00"),
            good(id="t3", now="2027-03-28T02:30"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "bad.jsonl", [GOOD] + bad_lines + [GOOD])
            cases, errors = run.load_cases(Path(folder))
        self.assertEqual([c.id for c in cases], ["g-1"])
        self.assertEqual(len(errors), len(bad_lines) + 1)          # + the duplicate id at the end
        self.assertEqual(errors[0].where, "bad.jsonl:2")
        self.assertIn("not valid JSON", errors[0].message)
        self.assertIn("unknown key(s): expected", errors[2].message)
        self.assertIn("duplicate id 'g-1'", errors[-1].message)
        self.assertIn("first used at bad.jsonl:1", errors[-1].message)
        self.assertIn("'e2'", errors[11].message)                    # errors name the case id when known

    def test_duplicate_ids_across_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "a.jsonl", [GOOD])
            self.write_cases(folder, "b.jsonl", [GOOD])
            cases, errors = run.load_cases(Path(folder))
        self.assertEqual(len(cases), 1)
        self.assertEqual(errors[0].where, "b.jsonl:1")


# ---------------------------------------------------------------------------------------------
class ReportTests(RunnerTestCase):
    def results(self):
        return [
            run.run_case(make_case([{"say": "add a in 5m"}], {"last_reply_kind": "confirm"}, case_id="ok-1")),
            run.run_case(make_case([{"say": "add a in 5m"}], {"commitments": [{"title": "b"}]}, case_id="bad-1")),
            run.run_case(make_case([{"say": "boom"}], {}, case_id="bad-2")),
            run.run_case(make_case([{"say": "add a in 5m"}], {"commitments": [{"title": "c"}]}, case_id="bad-3")),
        ]

    def test_latest_run_section(self):
        text = run.latest_run_section(self.results(), [run.CaseError("x.jsonl:4", "bad")], "2026-11-03 15:00")
        self.assertTrue(text.startswith("## Latest run"))
        self.assertIn("Run on 2026-11-03 15:00", text)
        self.assertIn("**1 of 4 cases pass (25.0%).**", text)
        self.assertIn("| test | 4 | 1 | 25.0% |", text)
        step_row = re.compile(r"\| (crash|intent|time|title|language|reply|flow) \|")
        step_rows = [line for line in text.splitlines() if step_row.match(line)]
        self.assertEqual(step_rows[0], "| title | 2 | 66.7% | " + run.STEP_MEANING["title"] + " |")
        self.assertTrue(step_rows[1].startswith("| crash | 1 |"))
        self.assertEqual(len(step_rows), len(run.STEPS))
        self.assertIn("- `bad-1` (test, **title**): commitment 1 title: expected \"b\", got \"a\"", text)
        self.assertIn("- `x.jsonl:4`: bad", text)
        self.assertNotIn("ok-1", text)

    def test_failing_list_is_capped(self):
        results = [run.run_case(make_case([{"say": "hola"}], {"commitment_count": 1}, case_id=f"c-{n}"))
                   for n in range(run.MAX_LISTED + 5)]
        text = run.latest_run_section(results, [], "now")
        self.assertEqual(text.count("- `c-"), run.MAX_LISTED)
        self.assertIn("and 5 more", text)

    def test_report_keeps_before_and_after_section(self):
        kept = ("## Before and after (v1 vs v2)\n\nThe class inputs:\n\n"
                "```\n# not a heading, inside code\n```\n\n### Details\n\n| v1 | v2 |\n|---|---|\n| 1/4 | 4/4 |\n")
        old = "# Old title\n\n## Latest run\n\nold numbers\n\n" + kept + "\n## Something else\n\nold\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "REPORT.md"
            path.write_text(old, encoding="utf-8")
            run.write_report(path, run.latest_run_section(self.results(), [], "2026-11-03 15:00"))
            new = path.read_text(encoding="utf-8")
            run.write_report(path, run.latest_run_section(self.results(), [], "2026-11-03 16:00"))
            newer = path.read_text(encoding="utf-8")
        self.assertIn(kept.rstrip("\n"), new)
        self.assertLess(new.index("## Before and after"), new.index("## Latest run"))
        self.assertNotIn("old numbers", new)
        self.assertNotIn("Something else", new)
        self.assertEqual(new.count("## Latest run"), 1)
        self.assertEqual(newer.count(kept.rstrip("\n")), 1)            # stable over many runs
        self.assertIn("16:00", newer)

    def test_report_without_old_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "REPORT.md"
            run.write_report(path, "## Latest run\n\nx\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "# Wazy eval report\n\n## Latest run\n\nx\n")

    def test_json_results(self):
        data = run.results_as_json(self.results(), [run.CaseError("x.jsonl:1", "bad")], "now")
        self.assertEqual((data["total"], data["passed"], data["failed"], data["pass_rate"]), (4, 1, 3, 25.0))
        self.assertEqual(data["by_step"]["title"], 2)
        self.assertEqual(data["by_category"], {"test": {"cases": 4, "passed": 1}})
        bad = [c for c in data["cases"] if c["id"] == "bad-1"][0]
        self.assertEqual(bad["failures"][0], {"step": "title", "check": "commitment 1 title",
                                              "expected": "b", "actual": "a"})
        json.dumps(data)                                                # everything can be written as JSON


# ---------------------------------------------------------------------------------------------
class MainTests(RunnerTestCase):
    def test_all_pass_exit_0_and_report_written(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "good.jsonl", [GOOD])
            report = Path(folder) / "REPORT.md"
            out_json = Path(folder) / "out.json"
            code, output = self.main("--cases", folder, "--report", str(report), "--json", str(out_json))
            self.assertEqual(code, 0)
            self.assertIn("1/1 cases pass (100.0%)", output)
            self.assertIn("## Latest run", report.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["passed"], 1)

    def test_failure_exit_1_and_verbose(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "c.jsonl", [GOOD, good(id="g-2", expect={"last_reply_kind": "injection"})])
            code, output = self.main("--cases", folder, "--no-report", "--verbose")
        self.assertEqual(code, 1)
        self.assertIn("FAIL g-2 [good] blamed on: intent", output)
        self.assertIn('input:   say "add a in 5m"', output)
        self.assertIn('last_reply_kind: expected "injection", got "confirm"', output)

    def test_case_error_exit_2_but_good_cases_still_run(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "c.jsonl", [GOOD, "{broken"])
            code, output = self.main("--cases", folder, "--no-report")
        self.assertEqual(code, 2)
        self.assertIn("1/1 cases pass", output)
        self.assertIn("c.jsonl:2: not valid JSON", output)

    def test_no_cases_exit_2(self):
        with tempfile.TemporaryDirectory() as folder:
            code, _ = self.main("--cases", folder, "--no-report")
            self.assertEqual(code, 2)
            code, _ = self.main("--cases", str(Path(folder) / "missing"), "--no-report")
            self.assertEqual(code, 2)

    def test_filters_and_no_default_report_for_filtered_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_cases(folder, "c.jsonl", [GOOD, good(id="g-2", category="other",
                                                            expect={"last_reply_kind": "help"})])
            args = run.parse_args(["--cases", folder, "--category", "good"])
            self.assertIsNone(run.report_path(args))
            self.assertEqual(run.report_path(run.parse_args(["--report", "x.md", "--id", "g-1"])), Path("x.md"))
            self.assertEqual(run.report_path(run.parse_args([])), run.DEFAULT_REPORT)
            self.assertIsNone(run.report_path(run.parse_args(["--no-report"])))
            self.assertEqual(self.main("--cases", folder, "--category", "good")[0], 0)
            self.assertEqual(self.main("--cases", folder, "--id", "g-2")[0], 1)
            self.assertEqual(self.main("--cases", folder, "--id", "nope")[0], 2)
            self.assertEqual(len(self.apps), 2)


if __name__ == "__main__":
    unittest.main()
