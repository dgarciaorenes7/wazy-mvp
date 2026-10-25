# Wazy MVP

**Text a small commitment. Get one nudge at the right moment. Every outcome is recorded.**

Launching AI Native Ventures (ESADE) · Session 6 hackathon · Andrea Di Segni & David García · 13 October 2026

This is the thinnest slice of Wazy that tests our riskiest guess:

> **Guess 1.** We believe that one proactive nudge per commitment will get busy people to follow through on small promises without feeling nagged. We will know we are right when test users text new commitments on 3+ different days and tap **Done** on at least half of their nudges.

## Run it

Needs **Python 3.9 or newer** and nothing else (no packages to install, no keys).

```bash
./run.sh                 # or: python3 -m wazy
```

Open **http://127.0.0.1:8765/demo** for the presenter view (the chat in a phone frame, a four-step tracker and the outcome log), or **http://127.0.0.1:8765/** for the chat alone.

**On a phone:** install [ngrok](https://ngrok.com) once, then start with `./run.sh --phone` (or run `ngrok http 8765` in a second terminal). A QR code appears on the presenter view; scanning it opens the chat with the access code.

**Try:** `Send Marta the slides in 30 seconds` → Wazy confirms the exact time → the nudge arrives → tap **Done** → the outcome appears in the log with the response time.

To clear the demo, press **Shift + R** on the presenter view. Stop the server with **Ctrl + C**.

## What it does, and nothing more

1. **Text it.** A commitment in your own words, in English or Spanish (`Reply to Carlos by Thursday`, `recuérdame llamar a mamá mañana a las 9`).
2. **Confirmed.** One line saying what Wazy understood and exactly when it will nudge. If no time was given, it asks for one.
3. **One nudge** at that time, with **Done** and **Later**.
4. **Outcome recorded** in `nudge_outcomes`: `done`, `later` (one more nudge next morning at 10:00), `stop`, or `no_reply` after 24 hours. This table is the data cycle described in our Session 5 handout.

Texting `STOP` ends all nudges. Typing `done` or `later` answers the open nudge.

## How it is built

```
wazy/
  config.py     settings from environment variables or .env; refuses to start on a bad value
  parser.py     reads the commitment: rules decide the time; optional Claude Haiku tidies the title
  store.py      SQLite tables, the conversation, and the scheduler (code decides when to nudge)
  server.py     standard-library HTTP server, access rules, JSON API
  static/       chat.html (the phone) and demo.html (the presenter view)
tests/          29 unit tests: reading times, the whole loop including Later, STOP and no reply, and settings
architecturedoc.md          architecture document, with the Session 6 log
scope.md                    scope definition sheet
Wazy MVP - Explainer.pdf    five-page explainer: what it tests, how it works, how to run it, security
```

Design rules (from `architecturedoc.md`):

- **Code decides when to nudge.** The AI is optional and only reads the wording. Its answer can never trigger an action, and any time it suggests is checked by code.
- **One commitment → one nudge → one recorded outcome.**
- **Fail closed.** A setting that is present but wrong (a short access code, a malformed key, a bad port) stops the server from starting.

| Table | What it holds |
|---|---|
| `commitments` | what the person typed, what Wazy understood, when the nudge is due, its status |
| `nudge_outcomes` | one row per nudge: when it was sent, the outcome, the seconds it took to answer |
| `messages` | the conversation shown in the chat |

## Settings (all optional)

Copy `.env.example` to `.env` to change them.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | If set, Claude Haiku 4.5 tidies the title. Without it, the rules do everything. |
| `WAZY_LATER_MINUTES` | empty | Empty: **Later** re-nudges next morning at 10:00. Set e.g. `1` for a live demo. |
| `WAZY_ACCESS_CODE` | generated | Code a phone needs (min. 8 characters). Generated once and kept in the database if empty. |
| `WAZY_PORT` | `8765` | Port on this computer. If it is not set, `PORT` is used (hosting platforms set it). |
| `WAZY_HOST` | `127.0.0.1` | Set `0.0.0.0` on a hosting platform so it accepts outside connections. |
| `WAZY_TZ` | `Europe/Madrid` | The time zone every time is read in, so "tomorrow 9am" is right even on a server in another zone. |
| `WAZY_DB` | `data/wazy.db` | Where the SQLite database lives. |

## Tests

```bash
python3 -m unittest discover tests
```

## Security

- **No secrets in this package.** Nothing is needed to run it; a real key only ever goes in `.env`, which is git-ignored.
- **Only this computer is trusted.** Requests through the tunnel need the access code (sent once in the QR link, then kept in an HttpOnly cookie). The presenter view, the tunnel link and reset are local only.
- **Cross-site requests are refused** (JSON-only POSTs with an Origin check). Pages carry a Content-Security-Policy.
- **Limits:** messages are cut to 300 characters; 30 messages a minute; message text is never written to the terminal.
- **Privacy:** the first message tells the user where their texts go. Data stays in the local SQLite file.

## Putting it online

The code is ready for a host that keeps one process running (not a serverless one, because the scheduler loop has to stay awake):

- start command `python3 -m wazy`, with `WAZY_HOST=0.0.0.0` (the port comes from `PORT`)
- `WAZY_DB` on a persistent disk or volume, or the outcome log is lost on every deploy
- a fixed `WAZY_ACCESS_CODE`, and then testers open `https://<your-address>/?k=<code>` once
- the presenter view and reset only answer the machine itself, so on a host they are switched off by design

## Known limits (honest list)

- It is a **web chat, not WhatsApp yet.** The old Twilio and Anthropic keys were rejected on the day, so the WhatsApp connection is the next step (see the explainer PDF).
- **One conversation per server.** The two-week test with three users needs one link per person.
- Nudges go out **only while the computer is awake**. The user test needs an always-on host.
- One time zone for everyone (`WAZY_TZ`, Madrid by default), which is fine for our testers.
