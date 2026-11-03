# Wazy v2: specification

This is the contract every module is built and tested against. If code and this file disagree, this file wins,
unless the disagreement is reported and this file is changed on purpose.

## 0. What Wazy is

A contact you text a small commitment to ("send Marta the slides by Thursday"). Wazy confirms in one line
exactly when it will nudge you, sends ONE nudge at that time with **Done** / **Later**, and records the outcome.
It works in English and Spanish, in a web chat and on WhatsApp (through Twilio).

### Product promises (must never break)
1. **Code decides when.** Times come from the rules in `timeparse.py`. The optional AI may tidy a title, and may
   suggest a time only when the rules found nothing at all; that suggestion passes the same checks as any time,
   and an AI answer can never trigger any other action.
2. **One commitment, one nudge, one recorded outcome** (`done`, `later`, `stop`, `no_reply`, `cancelled`).
   **Later** schedules exactly one more nudge.
3. **Nothing is deleted.** Cancel and STOP change a status. The only bulk action is STOP, typed by the person.
4. **Learning asks first.** Wazy may propose a better default hour; nothing changes until the person says YES.
   A time the person stated is never changed, and a learned hour never moves a deadline later.
5. **Fail closed.** A setting that is present but wrong stops the server from starting.
6. **Say what went wrong.** When Wazy cannot use a time it says why in plain words (impossible time, already
   passed, repeating, two commitments at once) and asks one clear question. It never silently guesses.
7. **Titles are clean.** The title is the task only: no time words, no vague time words, no filler
   ("remind me to", "please", "recuérdame"), never an instruction aimed at Wazy.
8. **Private by default.** Message text is never printed to logs. Each person only ever sees their own data.

### Constraints
- **Python 3.9+, standard library only.** It must run on David's Mac (Python 3.9.6) with nothing to install.
  So: `from __future__ import annotations` in every module, no `match`, no `X | Y` outside annotations,
  no `datetime.UTC`, no `zip(strict=)`, no parenthesised context managers. `zoneinfo` and `str.removeprefix` are fine.
- Every module has a docstring saying what it does in plain words. Short functions, plain names. David must be
  able to explain every line in class, so prefer readable rules over clever ones.
- User-facing English is plain and friendly, UK spelling, no em dashes. Spanish is natural Spain Spanish (tú).

## 1. Module map and ownership

| File | Owner | What it does |
|---|---|---|
| `wazy/types.py` | lead (fixed) | Shared dataclasses: `TimeResult`, `Intent`, `Reply`, constants |
| `wazy/db.py` | lead (fixed) | SQLite schema v2, helpers, UTC time text, v1 migration |
| `wazy/lang.py` | intents agent | `detect_language` |
| `wazy/intents.py` | intents agent | `classify`: commands vs text; questions, edits, injection |
| `wazy/timeparse.py` | timeparse agent | `parse_time`: the rules that turn words into a time |
| `wazy/title.py` | title agent | `make_title`: the clean task title |
| `wazy/replies.py` | replies agent | every sentence Wazy says, EN and ES; `describe_when` |
| `wazy/ai.py` | ai agent | optional Claude Haiku title tidier (and last-resort time suggestion) |
| `wazy/conversation.py`, `wazy/app.py` | conversation agent | the state machine; the `App` facade |
| `wazy/scheduler.py`, `wazy/learning.py` | scheduler agent | due nudges, no-reply, later, quiet hours; the learning rule |
| `wazy/config.py`, `wazy/server.py`, `wazy/__main__.py`, `run.sh` | server agent | settings, HTTP, access, rate limits |
| `wazy/whatsapp.py` | whatsapp agent | Twilio webhook in, messages out, signature check, retries |
| `wazy/static/*.html` | ui agent | the chat and presenter view |
| `evals/run.py` | eval agent | runs the eval cases, reports by category and by failing step |
| `evals/cases/*.jsonl` | 12 eval writers | the eval set (section 9) |

Each agent edits ONLY its own files plus its own tests `tests/test_<module>.py`. If you need something from
another module that does not exist yet, code against the interface below and use a small stub in your tests.

## 2. Interfaces (exact)

```python
# wazy/lang.py
def detect_language(text: str, fallback: str = "en") -> str      # "en" or "es"

# wazy/intents.py
def classify(text: str, *, pending: bool = False, learning_question: bool = False) -> Intent

# wazy/timeparse.py
DEFAULT_HOUR = 10
def parse_time(text: str, now: datetime, tz: tzinfo, default_hour: int = DEFAULT_HOUR) -> TimeResult
#   now: aware datetime (any zone); tz: the person's zone; result.when is aware, in tz.

# wazy/title.py
def make_title(text: str, spans: list[Span], lang: str = "en") -> str      # "" if no task is left

# wazy/replies.py
def render(kind: str, lang: str, **params) -> str
def describe_when(when: datetime, now: datetime, lang: str) -> str        # both aware; shown in when's zone

# wazy/ai.py
class AIReader:
    def __init__(self, api_key: str, model: str, timeout: float = 8.0, opener=None): ...
    def tidy(self, text: str, now: datetime, tz: tzinfo, lang: str) -> Optional[dict]
    #   returns {"title": str, "when": Optional[datetime aware in tz]} or None on any failure

# wazy/learning.py
def evidence(db: Database, user_id: str) -> dict
def consider(db: Database, user_id: str, now: datetime) -> Optional[Reply]     # a learning_question or None

# wazy/scheduler.py
class Scheduler:
    def __init__(self, db: Database, *, later_minutes: Optional[int] = None, on_message=None): ...
    def run_due(self, now: datetime) -> int        # nudges sent this call
    def later_time(self, user: dict, now: datetime) -> datetime

# wazy/app.py  (the only thing server.py, whatsapp.py, tests and evals talk to)
class App:
    def __init__(self, db_path: str = ":memory:", *, clock: Callable[[], datetime] | None = None,
                 default_tz: str = "Europe/Madrid", later_minutes: Optional[int] = None,
                 ai: Optional[AIReader] = None, on_message: Optional[Callable[[str, int, Reply], None]] = None): ...
    def handle(self, user_id: str, text: str, *, channel: str = "web") -> list[Reply]
    def tap(self, user_id: str, commitment_id: int, action: str) -> list[Reply]   # action "done" | "later"
    def tick(self) -> int                       # run the scheduler once at clock()
    def state(self, user_id: str) -> dict       # what that person's chat shows
    def admin_state(self) -> dict               # presenter view: all people, outcomes, stats
    def reset(self) -> None                     # presenter only: wipe everything except settings
    def close(self) -> None
#   clock() returns an AWARE datetime. on_message(user_id, message_id, reply) is called for every
#   Wazy message stored (replies and nudges) so whatsapp.py can deliver it.
```

`state(user_id)` returns `{"messages": [{id, role, text, kind, lang, commitment_id, created_at}] (last 200,
oldest first), "open": [message ids of nudges still waiting for Done/Later], "commitments": [...] (latest 50,
with nudge_at as UTC text and nudge_at_local in the person's zone), "outcomes": [...] (latest 50 with title),
"stats": {...}, "learning_log": [...], "reader_misses": [...]}`. Everything only for that user.

`admin_state()` returns the same keys across all people plus `"users": [{id, channel, lang, tz, opted_out,
learned_hour, commitments, last_seen_at}]` and `"focus_user"`: the most recently active user id, whose
messages are in `"messages"` so the presenter view shows a live conversation.

### 2.1 Agreed details (added at the start of the build so parallel modules fit together)

**App extras.** `App.handle(user_id, text, *, channel="web", tz=None)`: `tz` is only used when the person is
created (whatsapp.py passes the zone picked from the country code). `App.db` is the `Database`.
`App.scheduler` is the `Scheduler`. `App.clock` is the clock.

**Storing Wazy's messages.** Everyone uses `db.add_message(user_id, role, text, kind, lang, commitment_id, now)
-> message id`. Whoever stores a bot message (conversation, scheduler) then calls
`on_message(user_id, message_id, reply)` if it is set. `handle()` and `tap()` return the stored replies in order.
`tap()` also stores the person's tap as a user message ("Done" / "Later", kind "text").

**Learning functions** (`learning.py`, called by conversation and scheduler; none of them stores a message,
the caller does):
```python
def evidence(db, user_id) -> dict   # keys: current_hour, answered (answered nudges counted, max 30),
    # answered_at_current, done_at_current, misses (= answered_at_current - done_at_current),
    # best_hour (or None), done_at_best, by_hour {hour: {"answered": n, "done": n}}
def consider(db, user_id, now) -> Optional[Reply]   # sets learning_pending/learning_asked_at, logs "asked"
def answer(db, user_id, yes: bool, now) -> Reply      # learning_approved / learning_rejected
def explain(db, user_id) -> Reply                     # learning_explained
def forget(db, user_id, now) -> Reply                 # learning_forgotten
def resume(db, user_id, now) -> Reply                 # learning_resumed
```
Replies are rendered in the user's saved `lang`. The hour of a nudge is the hour of `scheduled_for` in the
person's zone. Only nudges whose commitment `time_source` is "default" or "learned" count at the current hour.

**render() parameters** (fixed; `conversation.py` and `replies.py` must agree): welcome(ai: bool), help(),
confirm(title, when_text, learned: bool), ask_time(title), ask_what(when_text: str, "" if none), invalid_time(title,
detail, detail_code, hint), past_time(title, detail), too_far(title), recurring_unsupported(title),
multiple_commitments(), not_commitment(reason: "question"|"edit"|"limit"), injection(), too_long(limit),
stopped(), started(), done_ack(title), later_ack(title, when_text), no_open_nudge(),
list(items=[(n, title, when_text)]), list_empty(), cancelled(title), cancel_which(items=[(n, title, when_text)]),
nothing_to_cancel(), pending_cancelled(title: str, may be ""), learning_question(current_hour, misses, answered,
best_hour, done_at_best), learning_approved(hour), learning_rejected(hour), learning_explained(off: bool,
learned_hour: Optional[int], default_hour, done_at_current, answered_at_current, evidence: dict),
learning_forgotten(default_hour), learning_resumed(), rate_limited(), nudge(title, due_text: str, "" when on time,
else the local due clock time "09:00"). `title` may be "" where the kind allows it (e.g. ask_time never gets "").
Unknown kind or missing parameter raises (a bug, caught by tests).

**What reply texts must contain** (evals check these with `last_reply_contains`): `confirm` contains the title
and `when_text`; `ask_time`, `invalid_time`, `past_time`, `too_far`, `recurring_unsupported`, `done_ack`,
`later_ack`, `cancelled`, `nudge` contain the title (when not ""); `invalid_time` contains `detail` and, for
`weekday_mismatch`, `hint`; `past_time` contains `detail`; `later_ack` contains `when_text`; `list` and
`cancel_which` contain every title. Titles are always shown in curly quotes “...”.

**describe_when(when, now, lang)** (`when` shown in its own zone, `now` converted to that zone):
| Gap | English | Spanish |
|---|---|---|
| < 120 s | `at 09:30:30 (in 30 s)` | `a las 09:30:30 (en 30 s)` |
| < 60 min | `at 09:45 (in 15 min)` | `a las 09:45 (en 15 min)` |
| same day | `today at 18:00` | `hoy a las 18:00` |
| next day | `tomorrow at 09:00` | `mañana a las 09:00` |
| < 7 days | `on Friday at 10:00` | `el viernes a las 10:00` |
| later | `on Thu 5 Nov at 10:00` (`on Thu 5 Nov 2027 at 10:00` if another year) | `el jueves 5 de noviembre a las 10:00` (`... de 2027 ...`) |
Spanish uses `a la` for 01:xx (`a la 01:30`). Seconds are shown only in the first row. Gaps are rounded to the
nearest whole second/minute, minimum 1.

**state() details.** `commitments[*]` has `id, title, raw_text, nudge_at, nudge_at_local
("YYYY-MM-DDTHH:MM:SS" in the person's zone, no offset), status, time_source, deadline (0/1), later_count,
reader, created_at`. `outcomes[*]` has `id, commitment_id, title, scheduled_for, nudged_at, outcome,
responded_at, seconds_to_respond`. `stats` has `commitments, nudges_sent, done, later, no_reply, stopped,
cancelled, pending, done_rate` (whole percent of answered nudges, 0 if none). `learning_log` and
`reader_misses` are that person's rows, newest first, max 50. Message `created_at` is UTC text.

**HTTP shapes.** `POST /api/send {"text"}` and `POST /api/answer {"commitment_id", "action"}` return
`{"ok": true, "replies": [{"kind", "text", "lang", "commitment_id", "buttons"}]}`; errors are
`{"ok": false, "error": "<code>"}` (e.g. `not_open`, `bad_json`, `too_large`, `forbidden`, `rate_limited`).
The UI then fetches `/api/state`. The web chat shows Done/Later under every message whose id is in `open`.
HTTP-level rate limit: 30 POSTs per minute per user id (429), separate from the conversation's own limit.

**WhatsApp wiring** (`whatsapp.py`):
```python
def signature(url: str, params: dict, auth_token: str) -> str          # base64 HMAC-SHA1 (Twilio's scheme)
def valid_signature(url: str, params: dict, auth_token: str, given: str) -> bool
def zone_for_number(number: str, default: str) -> str                  # "+34600..." -> "Europe/Madrid"
class WhatsApp:
    def __init__(self, *, account_sid, auth_token, from_number, public_url, content_sid="", opener=None,
                 clock=None): ...
    def attach(self, app) -> None                    # called once, after App is built with on_message=wa.on_message
    def on_message(self, user_id, message_id, reply) -> None   # queue an outbox row for whatsapp: users only
    def handle_webhook(self, params: dict, signature: str) -> int   # HTTP status: 200, or 403 on a bad signature
    def deliver_due(self) -> int                    # send due outbox rows once; returns rows sent
    def start(self) -> None / stop(self) -> None    # background sender thread
```
The webhook URL that is signed is `public_url` + `/whatsapp/webhook`.

**Config** (`config.py`): `load_config(env: Optional[Mapping[str, str]] = None, dotenv: Optional[Path] = None)
-> Config` (a dataclass) or raises `ConfigError` with a message naming the bad setting. With `env=None` it reads
`os.environ` plus `.env` (real env wins). Fields: `host, port, tz, db_path, anthropic_api_key, ai_model,
later_minutes (Optional[int]), access_code, admin_code, twilio_account_sid, twilio_auth_token, twilio_from,
twilio_content_sid, public_url`, and the property `twilio_enabled`.

## 3. Reading the time (`timeparse.py`)

Input is the person's whole message. Matching is case-insensitive and accent-insensitive
("miercoles" = "miércoles", "manana" = "mañana"). Spans always refer to the original text.

### 3.1 Clock times
| Words | Meaning |
|---|---|
| `20:00`, `20.00`, `20h`, `20h30`, `8pm`, `8 pm`, `8:30pm`, `8.30 p.m.` | that clock time |
| `12am` = 00:00, `12pm` = 12:00, `noon`, `midday`, `mediodía` = 12:00, `midnight`, `medianoche` = 00:00 of the next day |
| `half past 9`, `quarter past 9`, `quarter to 9`, `9 y media`, `9 y cuarto`, `9 menos cuarto`, `las 9 y 20` | 09:30, 09:15, 08:45, ... (hour then follows the bare-hour rule) |
| `a las 9 de la mañana` 09:00, `a las 5 de la tarde` 17:00, `a las 9 de la noche` 21:00, `a la 1` 13:00 | Spanish parts of day fix am/pm |
| `9 in the morning`, `9 tonight`, `7 in the evening`, `3 this afternoon` | part of day fixes am/pm |
| `24:00` | midnight (00:00 next day) |

**Bare hour** (`at 9`, `a las 9`, `9ish`, no am/pm, no part of day):
- With a day given ("tomorrow at 9", "Friday at 3"): 1 to 7 means pm (13:00 to 19:00), 8 to 11 means am, 12 means 12:00.
- With no day: of the two readings h and h+12, pick the earliest one that is still ahead today and falls between
  07:00 and 22:59. If neither fits, use tomorrow with the with-a-day rule. (At 10:00, "at 9" means 21:00 today;
  at 08:55, "at 9" means 09:00 today; at 23:30, "at 9" means 09:00 tomorrow.)
- Numbers that are clearly not times are ignored: "call 3 clients", "room 107", "2 slides", "page 15",
  "chapter 9", "€20", "20%", "x2", years like "2026". A number is a time only with `at`/`a las`/`@`/`:`/`h`/am/pm.

**Invalid** (status `invalid`): hour > 24, or 24 with minutes, minute > 59, `13pm`..`23pm`, `0am`/`0pm`,
`25:00`, `9:75`, `a las 26`. `detail` is the offending text, `detail_code` one of `hour_out_of_range`,
`minute_out_of_range`, `ampm_conflict`.

### 3.2 Days and dates
| Words | Meaning |
|---|---|
| `today`, `hoy` | today |
| `tonight`, `esta noche` | today at 20:00 (stated) |
| `this morning` 09:00, `this afternoon`/`esta tarde` 16:00, `this evening` 19:00, `por la mañana` 09:00, `por la tarde` 16:00, `por la noche` 20:00 | that day at that hour (stated). "tomorrow morning", "mañana por la mañana", "el lunes por la tarde" combine |
| `tomorrow`, `tmrw`, `tmr`, `mañana` (as a day) | tomorrow. "mañana por la mañana" = tomorrow 09:00. "por la mañana" alone = 09:00 |
| `day after tomorrow`, `pasado mañana` | +2 days |
| `Monday` ... `Sunday`, `lunes` ... `domingo`, `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, `el lunes`, `this Friday`, `next Friday`, `el próximo viernes`, `el viernes que viene` | the next such day; if it is today and the time is still ahead, today; otherwise +7. "next X" said on day X means +7 |
| `this weekend`, `el finde`, `este fin de semana` | Saturday (if today is Saturday or Sunday: today) |
| `next week`, `la semana que viene`, `la próxima semana` | Monday of next week |
| `end of the month`, `fin de mes` | last day of this month |
| `4 Nov`, `Nov 4`, `4th of November`, `November 4th`, `el 4 de noviembre`, `4 de nov` | that date; without a year, the next time it comes round (this year, or next year if already passed) |
| `4/11`, `04/11`, `4/11/2026`, `4-11-26`, `2026-11-04` | European day/month order (Spain). ISO `YYYY-MM-DD` |
| `on the 4th`, `el día 4`, `el 4` (only with "el día" or "on the") | day 4 of this month, or next month if passed |
| `in 2 days`, `en 3 días`, `in a week`, `dentro de una semana` | that day at the default hour (not stated) |

Weekday plus date that disagree ("Monday 5 Nov" when 5 Nov is a Thursday): status `invalid`,
`detail_code = "weekday_mismatch"`, `hint` = the real weekday name in English ("Thursday").
Dates that do not exist ("31 February", "30/02", "32 Oct", "13/13"): `invalid`, `detail_code = "no_such_date"`.

### 3.3 Relative times
`in 30 seconds`, `in 5 min`, `in 2 hours`, `in an hour`, `in half an hour`, `in an hour and a half`,
`in 1.5 hours`, `in 90 minutes`, `en 10 minutos`, `dentro de 2 horas`, `en media hora`, `en una hora y media`,
`in a couple of hours` (2 h), `in a few minutes` (5 min), `en un rato` (vague: see 3.5), `now`, `right now`,
`ahora`, `ya` (= in 1 minute, stated). Number words up to twenty in EN and ES ("in two hours", "en veinte minutos").
Under one day: exact (`now + delta`, seconds kept), stated. One day or more: that day at the default hour, not stated.

### 3.4 Combining and the default hour
- A day with no time of day gets `default_hour:00` (10:00 unless the person approved another), `stated = False`.
- A time with no day: see the bare-hour rule; explicit am/pm or 24-hour times with no day: today if still ahead,
  else tomorrow.
- "today" with a default hour that has already passed: in about an hour, rounded up to the next 5 minutes
  (stated = False). "today at 9" said at 09:10 (within 15 minutes): in 1 minute. Said more than 15 minutes
  after: status `past`.
- Deadline words make `deadline = True` and do not move the time: `by`, `before`, `no later than`, `until`,
  `due`, `antes de`, `antes del`, `para el`, `para el día`, `como tarde`, `hasta`.
- Times are built in the person's zone with `zoneinfo`. A local time that does not exist (the spring-forward
  gap, e.g. 02:30 on 28 Mar 2027 in Madrid) moves forward to the first valid minute (03:00). An ambiguous
  local time (autumn fall-back) uses the first occurrence (`fold=0`).
- Final check: not before `now - 5 s`, not more than 366 days ahead (`too_far`).
- A part of day with no clock time ("tonight", "this evening", "esta tarde", "por la noche", "this afternoon")
  for today, said after its hour: if the part of day has not ended yet (morning ends 12:00, afternoon 20:00,
  evening and night 23:00) use the "today" fallback (about an hour from now, rounded up to the next 5 minutes,
  stated = False), as long as that is before the part ends; otherwise `past`. Eval writers: avoid this edge
  except "this morning" said in the afternoon, which is `past`.
- `ya` (Spanish "now") counts only as the last word of the message or alone; `ahora mismo` anywhere.
- `mañana` means "tomorrow" unless it follows `por la`, `de la` or `esta` (then it is the morning).

### 3.5 Problems the reader reports (never guessed)
- `vague`: only vague time words: `at some point`, `sometime`, `some time`, `soon`, `later` (inside a sentence),
  `whenever`, `asap`, `when I can`, `one day`, `eventually`, `en algún momento`, `pronto`, `luego`,
  `más tarde`, `cuando pueda`, `algún día`, `en un rato`. These words go in `spans` so the title drops them.
- `past`: `yesterday`, `ayer`, `last week`, `la semana pasada`, `anteayer`, `this morning` said in the
  afternoon, an explicit date with a year that has gone, "today at 9" more than 15 minutes late.
  `detail` = the words.
- `recurring`: `every`, `each`, `daily`, `weekly`, `monthly`, `every day`, `every 5 minutes`, `on Mondays`,
  `every Monday`, `cada`, `todos los días`, `todos los lunes`, `a diario`, `cada semana`, `each morning`.
  Spans cover the whole repeating phrase.
- `multiple`: two separate time expressions that belong to two tasks joined by `and`, `y`, `,`, `;`, `then`
  ("call mum at 5 and email Bob tomorrow"). A day plus a clock time for ONE task ("tomorrow at 5", "Friday
  morning at 9") is one time, not multiple. When unsure between "ok" and "multiple", prefer "ok" with the
  first full time.
- `none`: nothing time-like at all.
Priority when several apply: `recurring` > `invalid` > `past` > `multiple` > `too_far` > `ok` > `vague` > `none`.

## 4. The title (`title.py`)

1. Remove every span from `parse_time` (times, vague words, recurring phrases, invalid times, deadline words that
   attach to them like "by", "before", "para el", "antes de").
2. Remove leading filler, repeatedly: `remind me to`, `remind me that`, `remind me about`, `remind me`,
   `reminder:`, `don't let me forget to`, `don't forget to`, `i need to`, `i have to`, `i must`, `i should`,
   `i've got to`, `i gotta`, `i want to`, `i'd like to`, `need to`, `have to`, `gotta`, `please`, `pls`, `plz`,
   `hey`, `hi`, `ok`, `wazy`, `can you remind me to`, `could you remind me to`, `recuérdame`, `recuérdame que`,
   `recuérdame de`, `acuérdate de`, `no me dejes olvidar`, `no te olvides de recordarme`, `tengo que`,
   `necesito`, `debo`, `hay que`, `quiero`, `por favor`, `porfa`, `oye`, `hola`.
3. Remove dangling joiners at either end (`to`, `at`, `on`, `by`, `in`, `for`, `the`, `this`, `next`, `and`,
   `a`, `las`, `el`, `la`, `de`, `del`, `para`, `antes`, `y`, `que`, `around`, `about`) and punctuation. Two exceptions
   (decided in integration): articles `the`, `a`, `el`, `la`, `las` stay at the start ("The dentist"), and `on`,
   `in` stay at the end ("Turn the heating on"), because timeparse spans already include connector words.
4. Keep everything else exactly as typed, including meaningful numbers ("Call 3 clients", "Room 107"), names,
   accents and emoji. Collapse spaces. Capitalise the first letter only. Cap at 120 characters on a word boundary.
5. Return `""` if nothing meaningful is left (only filler, only a time, only punctuation/emoji-free noise).
   Concretely: `""` if what is left has no letter at all (only digits, punctuation, emoji), or consists only of
   noise words: `hmm`, `hm`, `um`, `uh`, `ok`, `okay`, `k`, `lol`, `haha`, `jaja`, `thanks`, `thank you`, `thx`,
   `cool`, `nice`, `great`, `yes`, `no`, `yeah`, `nope`, `gracias`, `vale`, `bueno`, `pues`, `sí`, `si`, `vale`,
   `genial`, `perfecto`, `test`, `testing`, `prueba`, `never mind`, `nevermind`, `forget it`, `skip`, `nothing`,
   `da igual`, `déjalo`, `olvídalo`, `nada`.

Examples: "Remind me to call mum yesterday at 25:00" -> "Call mum" (with the time problem reported separately).
"I need to sort out the gym thing at some point" -> "Sort out the gym thing".
"recuérdame llamar a mamá mañana a las 9" -> "Llamar a mamá". "Send the deck to Ana by Friday 5pm" -> "Send the deck to Ana".

## 5. What a message is (`intents.py`, `lang.py`)

`classify` normalises (lower case, trim, strip trailing `.!`, collapse spaces) and checks whole-message commands:

| Intent | Words (EN / ES) |
|---|---|
| `empty` | nothing but spaces |
| `stop` | stop, unsubscribe, stop nudges, stop all, para, parar, basta, detente |
| `start` | start, resume, unstop, empezar, reanudar |
| `help` | help, ?, what can you do, how does this work, ayuda, qué puedes hacer, cómo funciona |
| `greeting` | hi, hello, hey, hola, buenas, good morning, buenos días (alone) |
| `done` | done, did it, finished, ✓, ✅, ok done, hecho, listo, ya está, terminado |
| `later` | later, snooze, not now, remind me later, luego, más tarde, después, ahora no |
| `yes` / `no` | only when `learning_question=True`: yes, y, yep, yeah, sure, ok, okay, sí, si, vale, dale, claro / no, nope, nah, no thanks, no gracias. Otherwise "no" alone is `question`-like noise -> `text` |
| `learn_what` | what did you learn, what have you learned/learnt, what do you know about me, qué has aprendido, qué sabes de mí |
| `learn_forget` | forget, forget me, reset learning, stop learning, olvida, olvida lo aprendido |
| `learn_resume` | learn again, start learning, vuelve a aprender |
| `list` | list, my list, what's on, what's coming up, what do I have, reminders, my reminders, show my reminders, lista, mi lista, qué tengo, mis recordatorios |
| `cancel` | cancel, cancel 2, cancel #2, delete 2, remove 2, cancela, cancela 2, borra 2, elimina 2 (number -> `number`) |
| `cancel_pending` | only when `pending=True`: never mind, nevermind, forget it, skip, nothing, da igual, déjalo, olvídalo, nada, cancel |
| `injection` | instructions aimed at the system: "ignore (all/your/previous) (rules/instructions)", "system prompt", "you are now", "act as", "pretend", "jailbreak", "developer mode", "olvida tus instrucciones", "ignora (las/tus) instrucciones", "eres ahora", text containing `<script`, `{{`, `{%` together with such words |
| `question` | starts with what/when/where/who/why/how/which/is/are/can/could/do/does/did/should/will (or ES qué/cuándo/dónde/quién/por qué/cómo/cuál/puedes/hay) or ends with "?", and is not a list/help/learn command |
| `edit` | change/move/reschedule/push/postpone/update/rename/edit it/that/the time, cambia/mueve/aplaza/pospón |
| `text` | anything else: read it as a commitment, or as the answer to Wazy's last question |

Traps (all agreed, all `text`): commands match the WHOLE message only, so "stop smoking tomorrow", "cancel my
dentist appointment tomorrow", "start the essay at 5", "help Ana move on Saturday" are commitments.
`do`/`does`/`did`/`is`/`are`/`can`/`could`/`should`/`will` start a question only when the message ends with "?"
or the next word is a pronoun (you, i, we, it, they, he, she, there, this, that): "do the laundry tomorrow" is
`text`, "do you speak Spanish" is `question`. Spanish: a question starts with `¿`, ends with `?`, or starts with
`qué`/`cuándo`/`dónde`/`quién`/`cómo`/`cuál`/`por qué`/`puedes` (accent-free forms count too, except `que` and
`como` which need `?`); "hay que comprar pan mañana" is `text` (`hay` is a question only as `hay algo...?` with `?`).
`act as` / `pretend` are injection only when aimed at Wazy: "act as (a|an|my) (ai|assistant|bot|system|admin|
developer|different|new|unrestricted)...", "act as if", "pretend (you are|you're|to be) (a|an|my)? (ai|assistant|
bot|human|different|...)", "pretend that you": "act as host for Ana's party" and "remind me to pretend to be
sick on Monday" are `text`. `edit` needs an object that is the reminder itself: it, that, this, the time, the
reminder, my reminder, the last one ("move the car tomorrow" is `text`; "move it to 5" is `edit`); Spanish
`cámbialo`, `muévelo`, `aplázalo`, `pospónlo`, `cambia la hora`, `cambia el recordatorio`, `mueve el
recordatorio`. `cancel` alone is always `cancel` (the conversation handles a pending wait); the other
`cancel_pending` words only while `pending=True`.

A question that clearly contains a commitment with a time ("can you remind me to call mum at 5?") is `text`:
rule of thumb, "remind me"/"recuérdame" anywhere means `text` unless it is an injection.

`detect_language`: count Spanish markers (accented letters ñ á é í ó ú ¿ ¡, and words like recuérdame, mañana,
tengo, que, el, la, las, los, a las, hoy, para, llamar, enviar, mandar, comprar, de, del, y, con, por, mi, tu)
against English markers (the, to, at, remind, me, my, tomorrow, today, call, send, buy, and, with, by, on, in).
Return "es" if the Spanish score is higher, "en" if English is higher, else `fallback`. Single command words
in Spanish (hola, listo, hecho, ayuda, para, luego, sí, vale) count as Spanish.

## 6. Conversation (`conversation.py`, `app.py`)

Every incoming message: `user = db.user(...)`; update `last_seen_at`; store the user's message (kind "text");
language = `detect_language(text, fallback=user.lang)`; save it as `user.lang` if the message had a clear signal.
Messages over 500 characters: reply `too_long`, nothing else happens. Text is cleaned of control characters
and zero-width characters and collapsed to single spaces before anything else.

Order of handling:
1. `empty` -> no reply.
2. Rate limit: more than 20 messages in 60 s from one person -> `rate_limited` (only once per minute).
3. `stop` -> every `scheduled`/`nudged` commitment of that person becomes `stopped`, pending outcomes become
   `stop`, `opted_out = 1`, pending waits cleared -> `stopped`.
4. `start` -> `opted_out = 0` -> `started`.
5. `help` -> `help`. `greeting` -> `welcome` (first contact always gets `welcome` before anything else).
6. If a learning question is open and the intent is `yes`/`no` -> learning answer.
7. `learn_what`, `learn_forget`, `learn_resume` -> learning (section 7).
8. `done`/`later` -> answer that person's most recent open nudge, else `no_open_nudge`.
9. `list` -> numbered upcoming `scheduled` commitments, soonest first, max 10, each "1. Title, Thu 5 Nov at 10:00";
   saves `last_list`. None -> `list_empty`.
10. `cancel` with a number -> cancel item N of `last_list` (or of the current list if none) -> `cancelled`;
    out of range -> `cancel_which` with the list. `cancel` with no number: if a wait is pending -> `pending_cancelled`;
    else if exactly one upcoming commitment -> cancel it; else `cancel_which` with the list; none -> `nothing_to_cancel`.
    Cancelling sets status `cancelled` and any pending outcome to `cancelled`.
11. `cancel_pending` (only while a wait is pending) -> clear it -> `pending_cancelled`.
12. `injection` -> `injection` reply ("I only take commitments..."), nothing stored as a commitment, wait unchanged.
13. `question` / `edit` -> `not_commitment` (with a hint that "list" and "cancel 2" exist).
14. `text` -> read it: `t = parse_time(text, now, user_tz, default_hour)`, `title = make_title(text, t.spans, lang)`;
    if an AI reader is configured, `ai.tidy(...)` may replace the title (never with ""), and may supply `when` only
    if `t.status == "none"`.
    - Pending waits expire 2 hours after `pending_at`.
    - **Reply to a waiting task** (`pending_title` set, this message has `title == ""`): if `t.status == "ok"` ->
      create the commitment with the pending title; fill the `reader_misses` row (`reply`, `fixed_at`); clear the wait.
      If `t` has a problem -> answer that problem again for the pending title (wait kept).
    - **Reply to a waiting time** (`pending_when` set, this message has a title and `t.status == "none"`) ->
      create the commitment at the pending time.
    - `t.status == "ok"` and a title -> create (section 6.1), clear any wait.
    - `t.status == "ok"` and no title -> save `pending_when` -> `ask_what`.
    - `recurring` -> `recurring_unsupported` (nothing stored; no wait). `multiple` -> `multiple_commitments`.
    - `invalid` -> `invalid_time` naming the bad words (and the real weekday for a mismatch); save the wait for the
      title. `past` -> `past_time`, wait saved. `too_far` -> `too_far`, wait saved.
    - `vague` or `none` with a title -> `ask_time`, wait saved. No title and nothing usable -> `ask_what`
      (e.g. "hmm", "ok", emoji only), no wait.
    - Every `ask_time`, `invalid_time`, `past_time`, `too_far` adds a `reader_misses` row (`problem` = the kind)
      and stores its id in `pending_miss_id`.
    - A new commitment from an opted-out person sets `opted_out = 0` (texting a commitment re-subscribes).
    - Max 100 `scheduled` commitments per person: beyond that, a polite refusal (`not_commitment` text variant).

### 6.1 Creating a commitment
`time_source`: "stated" if `t.stated`, else "default". If the person has a `learned_hour` and the time is the
default hour exactly on the hour, use the learned hour instead (`learned`), unless that is in the past or it is a
deadline and the learned hour is later than the default. Store `nudge_at` as UTC. Reply `confirm`:
"Got it: “{title}”. I'll nudge you {describe_when}." plus " (your usual time)" when learned, plus
" (I'll nudge you once, on the day)" style note for a deadline is NOT added (keep it one line).

### 6.2 Answering a nudge
`tap(user_id, commitment_id, "done"|"later")`: the commitment must belong to that user and have a pending outcome,
else return `[]` (the server answers 409). Done -> outcome `done`, status `done`, seconds recorded -> `done_ack`.
Later -> outcome `later`, `later_count + 1`, status `scheduled`, `nudge_at = scheduler.later_time(user, now)`
-> `later_ack` stating the time. Then `learning.consider(...)` may add a `learning_question`.

## 7. Scheduler and learning (`scheduler.py`, `learning.py`)

- `run_due(now)`: for each `scheduled` commitment with `nudge_at <= now` whose user is not opted out, CLAIM it in
  one transaction (`UPDATE ... SET status='nudged' WHERE id=? AND status='scheduled'`; only if 1 row changed)
  then insert the pending outcome (`scheduled_for` = due time, `nudged_at` = now) and the bot message
  (kind "nudge", text from `render("nudge", lang, title=..., due_text=...)`, buttons). A nudge sent more than
  10 minutes late says when it was due ("due at 09:00"). Never twice. A scheduled commitment of an opted-out user
  is left alone.
- Pending outcomes older than 24 h -> `no_reply`, commitment status `no_reply`; then `learning.consider` for that user.
- `later_time`: `later_minutes` setting if set (demo); else the person's learned hour or 10:00, next morning
  (Later tapped before 05:00 still means "this morning"). A learned hour still ahead today is used today.
- Quiet hours: a time Wazy CHOOSES (default, learned, later, "today" fallback) never falls between 23:00 and 07:59.
  Default 10:00 and learned hours (08 to 21) make this true by construction; the "today" fallback that would land
  after 22:00 goes to 10:00 tomorrow instead. Tests assert it. Times the person stated are always honoured.
- Learning rule (same as v1, per person): over the person's last 30 answered nudges, if at least 3 nudges at the
  current default hour were answered and fewer than half were Done, and some other hour between 08 and 21 has at
  least 3 Done taps, ask once (`learning_question`), at most once every 14 days, never while one is open, never
  while `learning_off`. YES -> `learned_hour` set, `approved` logged -> `learning_approved`. NO -> `rejected`,
  two-week pause -> `learning_rejected`. `forget` -> learned hour cleared, `learning_off = 1`, `forgotten` logged
  -> `learning_forgotten`. `learn again` -> `learning_off = 0`, `resumed` logged -> `learning_resumed`.
  `what did you learn` -> `learning_explained` with the real numbers.

## 8. Server, WhatsApp, config

**Config** (env vars, `.env` read without overriding real env): `WAZY_HOST` (127.0.0.1), `WAZY_PORT`/`PORT`
(8765), `WAZY_TZ` (Europe/Madrid, must be a valid zone), `WAZY_DB` (data/wazy.db), `ANTHROPIC_API_KEY`
(must start `sk-ant-` if set), `WAZY_AI_MODEL` (claude-haiku-4-5-20251001), `WAZY_LATER_MINUTES` (1..1440),
`WAZY_ACCESS_CODE` (>= 8 chars, else generated once and kept), `WAZY_ADMIN_CODE` (>= 12 chars; lets a remote
browser open /demo on a hosted server), `TWILIO_ACCOUNT_SID` (AC + 32 hex), `TWILIO_AUTH_TOKEN` (32 hex),
`TWILIO_WHATSAPP_FROM` ("whatsapp:+<digits>"), `TWILIO_CONTENT_SID` (optional, HX + 32 hex: a Content Template
with Done/Later quick replies), `WAZY_PUBLIC_URL` (https URL, required when Twilio is set: used to check
signatures). Twilio settings are all-or-nothing: some set and some missing -> refuse to start.

**HTTP** (stdlib `ThreadingHTTPServer`):
- `GET /`, `/chat`: the chat. Remote browsers need the access code once (`/?k=CODE`, moved into an HttpOnly cookie
  `wazy_access`); each browser also gets its own random id cookie `wazy_user` (128-bit, HttpOnly, SameSite=Lax,
  Secure behind https) so every tester has a separate conversation. user_id = "web:" + that id. Local browsers
  get "web:local".
- `GET /api/state` -> `app.state(user_id)`. `POST /api/send {"text"}` -> `app.handle`. `POST /api/answer
  {"commitment_id", "action"}` -> `app.tap` (409 if not open / not theirs).
- `GET /demo`, `GET /api/admin/state`, `GET /api/link`, `POST /api/reset`: local only, or with the admin cookie
  (`/demo?admin=CODE`).
- `GET /healthz` -> `{"ok": true}` (no auth, no data).
- `POST /whatsapp/webhook`: Twilio form post; signature checked (`X-Twilio-Signature`, HMAC-SHA1 of the public URL
  + sorted params, compared with `hmac.compare_digest`); bad or missing signature -> 403. Duplicate `MessageSid`
  -> 200 and ignored. Body: `From`, `Body`, `ButtonPayload` (e.g. "done:12"/"later:12"), `ButtonText`.
  Reply 200 with empty TwiML; Wazy's answers go out through the REST API (outbox), not in the HTTP response.
- Local = client address 127.0.0.1/::1 AND no `X-Forwarded-For`/`Forwarded`/`X-Real-IP` header AND a `Host`
  of localhost, 127.0.0.1 or [::1] (any port), which blocks DNS rebinding (added after the security review).
- Every POST: JSON only (415 otherwise; the webhook takes form data), same-origin `Origin` if present (403),
  body <= 4096 bytes (413), JSON object (400). Security headers on every reply (nosniff, no-referrer,
  frame SAMEORIGIN, CSP on HTML, no-store). Message text never logged.

**WhatsApp out** (`whatsapp.py`): `on_message` puts every Wazy message for a `whatsapp:` user in `outbox`
(queued). A sender thread delivers due outbox rows through Twilio's REST API with urllib (Basic auth), using the
Content Template with Done/Later buttons for nudges when `TWILIO_CONTENT_SID` is set (variables: {"1": title}),
plain text otherwise ("Reply DONE or LATER"). Failures retry with backoff (30 s, 2 min, 10 min, 1 h) up to 5
attempts, then `failed`. Never more than one send per outbox row. Phone-number country code sets the zone of a
NEW WhatsApp user: +34 Europe/Madrid, +39 Europe/Rome, +44 Europe/London, +33 Europe/Paris, +49 Europe/Berlin,
+351 Europe/Lisbon, +41 Europe/Zurich, +31 Europe/Amsterdam, +32 Europe/Brussels, anything else the default.

## 9. Eval cases (`evals/cases/*.jsonl`)

One JSON object per line:

```json
{"id": "en-clock-001", "category": "en_clock", "now": "2026-11-03T08:55:00", "tz": "Europe/Madrid",
 "steps": [{"say": "Remind me to submit the report tomorrow at 20:00"}],
 "expect": {"last_reply_kind": "confirm",
            "commitments": [{"title": "Submit the report", "nudge_at_local": "2026-11-04T20:00",
                             "time_source": "stated", "status": "scheduled"}]},
 "note": "optional: why this case matters"}
```

- `now` is local time in `tz` (default tz Europe/Madrid). Steps run in order against one fresh `App` with an
  in-memory database and a clock frozen at `now`; `{"wait": "90m"}` / `{"wait": "2h"}` / `{"wait": "1d"}` /
  `{"wait": "30s"}` moves the clock; `{"tick": true}` runs the scheduler; `{"tap": "done"}` / `{"tap": "later"}`
  taps the most recent open nudge; `{"say": "..."}` sends a message. The welcome on first contact does not
  count as a reply for `last_reply_kind`.
- `expect` keys (all optional, all must hold):
  - `last_reply_kind`: kind of the last Wazy reply. `reply_kinds`: the kinds of all Wazy replies after the
    welcome, in order (nudges included).
  - `last_reply_contains` / `last_reply_not_contains`: lists of substrings (case-insensitive).
  - `lang`: language of the last reply.
  - `commitment_count`: number of commitments for the user.
  - `commitments`: list, in creation order, each with any of: `title` (exact), `title_contains` (list),
    `title_not_contains` (list, case-insensitive), `nudge_at_local` ("YYYY-MM-DDTHH:MM" or with ":SS"),
    `time_source`, `status`, `deadline` (bool).
  - `nudges_sent`: number of nudges sent in total.
  - `reader_misses`: number of reader_misses rows.
- Write only expectations you are sure follow from this SPEC. Where the SPEC leaves room, use `title_contains`
  and `title_not_contains` instead of an exact title.
- Spread `now` across times of day and dates, including around midnight, month ends, the year end, and the
  Madrid DST changes (25 Oct 2026 03:00->02:00, 28 Mar 2027 02:00->03:00). 2026-11-03 is a Tuesday.
