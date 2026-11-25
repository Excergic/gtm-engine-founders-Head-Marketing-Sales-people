# Automation plan: 25 leads a day, replies and acceptances tracked without anyone at the keyboard

Written 2026-11-25. Simple, results first, no LLM needed to read the output.

## Why the results page showed 0 new leads today

- New leads only arrive when a Grok CSV is dropped into `data/incoming/grok/`. The last file came in on 2026-11-20.
- Two pipeline runs from 2026-11-20 are still marked `running`: the laptop slept mid-run.
- 105 people are already `ready` and unused, so "25 a day" can start immediately by serving the top 25 from that pool while sourcing refills it.

## The five steps (in build order)

1. **Fix `config/gtm.yaml`.** An uncommitted edit replaces the `icp:` scoring block and adds LinkedIn fields the model does not have, so config validation fails (19 tests) and the daily run breaks. Undo it, then re-add the follow-up days and pipeline stages properly if they are wanted.
2. **Run on a VPS, not the laptop.** The warehouse is already hosted (Neon). The VPS runs two cron jobs:
   - 06:00 IST daily: import, enrich, score, pick today's 25 leads, rebuild the results page.
   - Every 30 minutes: `gtm sync` (steps 3 and 4).
3. **Email replies tracked automatically.** Every sent email has its Gmail `thread_id`. The sync job reads each thread and writes events with plain rules:
   - a message from someone else → `reply`
   - a mailer-daemon notice → `bounce`
   - "remove me", "unsubscribe", "not interested" → `unsubscribe` (never contacted again)
   - positive replies are marked by the human, since that is a judgment call
4. **LinkedIn acceptances tracked automatically.** LinkedIn emails "X accepted your invitation" with the profile link. The sync job matches the link to pending invites and records `connection_accepted` and `connection_status = connected`. No scraping, no LinkedIn automation, no account risk.
5. **Sourcing automated.** The prompt in `docs/grok-brief.md` is sent to the Grok (xAI) API daily, asking for 50 people so at least 25 survive the checks. A short day is reported as short, never padded.

## What is needed from the owner

- A decision on the `config/gtm.yaml` edit.
- The VPS OS and SSH access (or a setup script run by hand).
- A one-time Google sign-in so the server can read Gmail (OAuth client + refresh token).
- An xAI API key for step 5. Steps 1 to 4 work without it.

## Already done

- `gtm report`: one self-contained HTML page (no JavaScript, names hidden by default) showing leads, sends, invites, replies, meetings and today's new leads. Anyone can read it without a terminal.
