# architecturedoc.md

## Venture
Wazy (working name — "Wazy" is also an Italian WhatsApp tool for businesses at wazy.app, so the name is not settled). A contact you text a small commitment to, and it nudges you once, at the right moment.

## Architectural principles
The three to five main rules this project follows.
- Code decides when to remind; the AI only reads the message (Session 3 test: an AI checking dates added errors and doubled the cost). In the Session 6 build the AI is optional, and any time it suggests is checked by code. Its answer can never trigger an action.
- One screen: a chat. WhatsApp is the target; the Session 6 build is a WhatsApp-style web chat (see the Session 6 log). No app to install, no sign-up.
- One commitment → one nudge → one recorded outcome (done / later / no reply in 24 h / stop). Every nudge is logged in `nudge_outcomes`.
- Nothing is deleted or changed in bulk without the user asking for it (the only bulk action is STOP, typed by the user).
- Fail closed: a setting that is present but wrong stops the server from starting. When WhatsApp is reconnected, missing Twilio or Anthropic keys must stop it too.

## Stack and key dependencies
The technology we are using, and why. As built on 13 Oct:
- Python 3.9+ standard library only (`http.server`, `sqlite3`, `threading`): a new, separate slice of about 800 lines of Python in `mvp/`, instead of switching features off inside the old 4,000-line Flask app. It runs on any Mac with nothing to install, and every line can be explained.
- Reading a commitment: rules in code turn the user's words into a time (English and Spanish). Claude Haiku 4.5 is optional and only tidies the title (~€0.001 per message when switched on).
- Scheduler: a one-second loop inside the server sends due nudges and closes unanswered ones after 24 h (replaces APScheduler).
- Storage: SQLite with three tables: `commitments`, `nudge_outcomes`, `messages`.
- Screen: a WhatsApp-style web chat. Phones reach the laptop through an ngrok tunnel and need an access code (QR link, then an HttpOnly cookie).
- Target for the user test (plan unchanged): Twilio WhatsApp Business number + a Content Template with Done / Later quick replies, on an always-on host.

## Dependencies to avoid
The technology we are deliberately NOT using, and why.
- Google Calendar / Microsoft Graph: connecting real accounts is Guess 3, a separate test; OAuth and token storage add risk.
- React web app (Vercel), admin SPA: the test needs no dashboard.
- Apple IAP / RevenueCat: we test the promise to pay, not payment.
- OpenAI transcription: a second provider and bill; nobody has asked for voice.
- Multi-agent (maker + grader) chains for anything involving dates.
- Flask, Gunicorn and APScheduler in the slice: one loop does not need a framework.

## Tradeoffs consciously accepted
What we are choosing to ignore or do poorly on purpose, and what we gain by doing that.
- A separate slice instead of an MVP_MODE switch in the old app: small, safe and testable (29 tests since 25 Oct), but WhatsApp, templates and the phone allow-list must be wired into it before the user test.
- An access code instead of a phone allow-list: only people we give the QR link to can reach it, but there is one conversation per server, so one tester per server until per-person links exist.
- Rule-based reading: free, fast and predictable, but it only understands the phrasings we wrote rules for. The AI reader is there for the rest.
- State lives in SQLite, so a restart loses nothing; only the message rate limit is in memory.
- No payments, no analytics tool: outcomes are read straight from `nudge_outcomes`.
- Laptop hosting for the hackathon: no cost and nothing to deploy, but nudges only go out while the laptop is awake and the ngrok address changes every run.

## Open assumptions
Things we are guessing but have not proven yet.
- Guess 1 — people want a proactive nudge and won't find it naggy. ← this MVP tests it
- Guess 2 — the problem is common beyond us (interviews: strongest for juniors without a system, weak for students).
- Guess 3 — people will connect real accounts (not tested now).
- People will text a commitment to a bot rather than just remember it.
- One nudge per commitment is enough.
- A web chat is not a fair stand-in for WhatsApp: the user test still needs WhatsApp to measure Guess 1 properly.
- The Twilio number and templates still work: unproven. The keys on the laptop were rejected on 13 Oct, so rotating them is now a precondition for the test.

## Session log
Session 4: The choices made and new guesses added today.
- (12 Oct) Starting point declared: David's existing Wazy prototype. Target guess: Guess 1, agreed by Andrea and David. Scope cut to commitment → one nudge → Done/Later → outcome log; ~80% of existing features switched off. AI security review of the existing code done; minimum checks: rotate and enforce keys, disable the public demo and sign-up endpoints, spending caps, tell users who processes their texts.
- New: allow-list, MVP_MODE, nudge_outcomes table, "fail closed" principle, fix the false "never shares your data" claim.

Session 5:
- Defence chosen: data cycle — each nudge's outcome teaches Wazy when (and whether) to nudge that person. First step = the nudge_outcomes log (same build as the Guess 1 measure).

Session 6 (13 Oct, hackathon):
- Goal: a user texts a commitment, receives one nudge and taps Done or Later, with the outcome recorded. This is both the Guess 1 measure and the first step of the data-cycle defence.
- Shipped and demoed live: text → one-line confirmation with the exact nudge time → one nudge with Done / Later → a `nudge_outcomes` row with the response time; STOP ends all nudges; no reply after 24 h is recorded; a presenter view; phone access through ngrok with an access code.
- Changed from the plan: at the start of the sprint the Twilio and Anthropic keys in the old `.env` were rejected (401). Instead of adding MVP_MODE to the Flask app, we built the loop as a separate standard-library slice and showed it as a web chat. Reconnecting WhatsApp moved to before the user test.
- Cut to protect the goal: WhatsApp/Twilio, the phone allow-list, the AI reader on by default, per-person links, an always-on host.
- After class: code split into modules, 22 tests added, README and run script, no secrets in the package, "Later" set to the agreed rule (one more nudge next morning at 10:00; in class it was 60 minutes).
- Before the user test: rotate the Twilio and Anthropic keys and set spending caps; connect the Twilio WhatsApp webhook and template to the same store; one link (or allow-listed number) per tester; move to an always-on host; tell testers who processes their texts.

Session 6 follow-up (25 Oct):
- We created this repository on GitHub. We didn't have one on 13 Oct and handed the build in as a zip (`wazy-mvp-Andrea-David.zip`), so the first commit is that zip unchanged, which is the end-of-class version plus the after-class clean-up above.
- The Session 5 defence runs. On 25 Oct we ran the full loop (three commitments, four nudges, one Later that came back as a second nudge) and it left four rows in `nudge_outcomes` with the outcome and the seconds to respond. The screenshot is in `docs/`. This was our own run, not a user.
- No outside users yet. 19 to 23 Oct was midterm week and none of the students we asked wanted to try it during exams, so testing with users starts on Mon 26 Oct.
- Small clean-up of the code, with 7 new tests (29 in total). We fixed four bugs. "In 2 days" and "in a week" were not understood, typing "done" with no nudge open was read as a new commitment called "Done", the done rate stopped counting after 50 nudges, and Later tapped after midnight skipped a whole day. Added what hosting needs: `WAZY_TZ` (Madrid by default, so times are right on a server in another time zone) and the platform's `PORT`. The server now rejects malformed requests instead of failing on them.
- Our plan for the user test: buy a Twilio phone number for WhatsApp, and open an account with a host that keeps the code running online, because some testers will only try it if it is on their phone and always on. Then we connect the WhatsApp webhook and the Done / Later template to the same store, give each tester their own conversation, and tell them who processes their texts.
