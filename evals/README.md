# Wazy evals

The evals are short scripted conversations with Wazy, each with the result it should give.
`run.py` plays every one against a fresh Wazy and reports how many pass, by category and by
**which step of Wazy fails most often**.

## How to run

From the repository root (standard library only, Python 3.9+):

```
python3.9 evals/run.py                         # every case, writes evals/REPORT.md
python3.9 evals/run.py --verbose               # also print every failure in full
python3.9 evals/run.py --category en_clock     # one category (repeat the flag for more)
python3.9 evals/run.py --id en-clock-001       # one case (repeat the flag for more)
python3.9 evals/run.py --json results.json     # also save every result as JSON
python3.9 evals/run.py --no-report             # do not touch evals/REPORT.md
python3.9 evals/run.py --report /tmp/r.md      # write the report somewhere else
python3.9 evals/run.py --cases some/folder     # read the cases from another folder
```

A run with `--category` or `--id` does not overwrite `evals/REPORT.md` (it is only part of the set),
unless you also pass `--report PATH`.

Exit code: **0** every case passes, **1** some case fails, **2** some case lines are broken
("case errors") or there are no cases to run.

The unit tests of the runner itself: `python3.9 -m unittest tests.test_evals_runner`.

## The case format

One JSON object per line in `evals/cases/*.jsonl`. The full format is in
[SPEC.md section 9](../SPEC.md#9-eval-cases-evalscasesjsonl). In short:

```json
{"id": "en-clock-001", "category": "en_clock", "now": "2026-11-03T08:55:00", "tz": "Europe/Madrid",
 "steps": [{"say": "Remind me to submit the report tomorrow at 20:00"}],
 "expect": {"last_reply_kind": "confirm",
            "commitments": [{"title": "Submit the report", "nudge_at_local": "2026-11-04T20:00"}]},
 "note": "why this case matters"}
```

- `id` must be unique across all files. `category` defaults to the file name, `tz` to Europe/Madrid.
- `now` is the local time in `tz`. A time that does not exist (inside the spring-forward gap) is a case error.
- Steps run in order against one new App (empty in-memory database, user `web:eval`, clock frozen at `now`):
  - `{"say": "text"}` sends a message.
  - `{"wait": "90m"}` moves the clock: `30s`, `90m`, `2h`, `1d` (or combined, `1h30m`). The clock counts
    real time, so `1d` is exactly 24 hours, even across a daylight saving change.
  - `{"tick": true}` runs the scheduler once. The nudges it sends count as replies.
  - `{"tap": "done"}` / `{"tap": "later"}` presses the button on the **newest** nudge still open.
    If no nudge is open, that is a failure (step `flow`).
- `expect` keys (all optional, all must hold): `last_reply_kind`, `reply_kinds`, `last_reply_contains`,
  `last_reply_not_contains`, `lang`, `commitment_count`, `commitments` (each with `title`,
  `title_contains`, `title_not_contains`, `nudge_at_local`, `time_source`, `status`, `deadline`),
  `nudges_sent`, `reader_misses`.

How the checks read:

- The **welcome** Wazy sends on first contact is not a reply: if the first `say` gets a welcome first,
  it is left out of `last_reply_kind` and `reply_kinds`.
- `reply_kinds` lists every Wazy reply after that welcome, in order, nudges included.
- "contains" checks ignore upper and lower case. `title` must match exactly.
- `nudge_at_local` is compared as the start of Wazy's `"YYYY-MM-DDTHH:MM:SS"`, so `"2026-11-04T20:00"`
  matches any second of that minute, and `"2026-11-04T20:00:30"` only that second.
- `commitments` are in creation order: the first one listed is the first one Wazy created.
- `nudges_sent` counts Wazy's messages of kind `nudge`. `reader_misses` counts that person's rows.

Unknown keys, wrong value types, unknown reply kinds, impossible dates and duplicate ids are
**case errors**: the line is skipped, listed in the output and the report, and the run exits with 2.
They never stop the other cases from running.

## The steps a failure is blamed on

Every failed check is blamed on one step of Wazy. A failing case is blamed on its most basic
failing step, in this order (the first one wins):

| Step | What went wrong | Checks that point to it |
|---|---|---|
| `crash` | Wazy raised an exception | any exception (the case stops there) |
| `intent` | Wazy misread what kind of message it was | a wrong reply kind where one side is a command, question or injection kind (`injection`, `not_commitment`, `help`, `welcome`, `stopped`, `started`, `list`, `list_empty`, `cancelled`, `cancel_which`, `nothing_to_cancel`, `pending_cancelled`, `done_ack`, `later_ack`, `no_open_nudge`, `too_long`, `learning_*`) |
| `time` | the time was read wrong | `nudge_at_local`, `time_source`, `deadline`; a wrong reply kind where both sides are time replies (`confirm`, `ask_time`, `ask_what`, `invalid_time`, `past_time`, `too_far`, `recurring_unsupported`, `multiple_commitments`) |
| `title` | the task title is wrong | `title`, `title_contains`, `title_not_contains` |
| `language` | the reply is in the wrong language | `lang` |
| `reply` | the reply text is wrong | `last_reply_contains`, `last_reply_not_contains` |
| `flow` | the conversation went another way | `commitment_count`, `status`, `nudges_sent`, `reader_misses`, a tap with no open nudge, any other wrong reply kind (e.g. `nudge`, `rate_limited`, no reply at all) |

For `reply_kinds`, the first pair of kinds that differ is blamed like `last_reply_kind`.
If one list only stops early (same kinds, one is shorter), it is `flow`.

## The report

`evals/REPORT.md` is rewritten on every full run. It has:

1. **Before and after**: written by hand (the v1 vs v2 numbers for the course handout). If the old
   report has a section whose heading starts with `## Before and after`, it is kept exactly as it was,
   at the top. Everything else in the file is replaced.
2. **Latest run**: date, total pass rate, a table by category, a table by failing step sorted by count
   ("which step fails most often"), the failing cases with a one-line reason (first 200), and any case errors.

## How to add a case

1. Pick the file for its category in `evals/cases/` (or make a new `<category>.jsonl`).
2. Add one line. Give it a new unique `id`, a `now` and the `steps`.
3. Expect only what SPEC.md makes certain. Where the exact wording is open, use `title_contains` and
   `title_not_contains` rather than an exact `title`. Check every date with `datetime`/`zoneinfo`.
4. Run just that case: `python3.9 evals/run.py --id <your-id> --verbose`.
   If it prints a case error, fix the line. If it fails, decide with SPEC.md whether Wazy or the case is wrong.
