# Real-world review

How this engine compares with the way GTM teams run signal-based outbound in 2026, judged
on its first live week (2026-11-17 to 2026-11-21). Numbers are from the production
warehouse; benchmarks are from public 2026 reports (mostly vendors, so treat them as
directional).

## The live test

| | Result |
|---|---|
| People scored | 566 |
| Passed every check (ready) | 205 |
| Held: verified email at a *previous* employer | 61 of 266 otherwise-ready people (23%) |
| Emails sent (human pressed send on each) | 50 (25 founders, 25 VPs, one per company) |
| Confirmed in Gmail and recorded | 50 of 50 |
| Bounced | 0 of 50 |
| Replies after 2 days | 1 (negative, asked to be removed; opt-out recorded, follow-ups cancelled) |
| Positive replies | 0 so far |
| Scheduled runs that completed | 1 of 3 days |

**Reading the reply rate.** 1 in 50 is 2% after two days and before any follow-up. The
cross-industry average is about 3.4% and a good campaign is 5 to 10%. Signal-triggered
outreach is reported far higher by some vendors (15 to 25%), though definitions vary. Fifty
emails and one step are too few to judge; the honest verdict comes after the day-4 follow-up.

## Where it matches or beats common practice

- **A real warehouse, not a spreadsheet.** Raw, staging, silver and golden layers with
  lineage, a duplicate review queue and a state history. Most Clay-table setups have no
  record of why a person is where they are; this one does, for every row.
- **Verification that checks the job, not just the inbox.** Deliverability checks prove an
  address works, not that it is current. The domain check caught 23% of "verified" emails
  pointing at old employers. Many teams send those and wonder why reply rates fall.
- **Spend control.** Waterfall enrichment (Clay, then Deepline) with a credit floor, a retry
  queue for failures, fill-blanks-only writes with provenance, and tests that use fake
  providers so they can never spend credits.
- **Signal-cited copy with a human in the loop.** One person per company, the offer chosen
  from the signal, a style lint (no filler, no placeholders, opt-out line), and nothing sends
  without a person pressing send. This matches the standardized-signal-object pattern teams
  describe, and keeps the quality gate that fully automated AI SDRs lose.
- **Opt-outs honoured within hours** and stored as do-not-contact, ahead of Google's 48-hour
  expectation.

## Gaps, most important first

1. **Sending infrastructure.** All 50 went from a personal `@gmail.com` inbox in about 11
   minutes. Common practice is a secondary sending domain with SPF, DKIM and DMARC, two or
   three warmed inboxes, a 3-week-plus warmup, and about 25 to 40 sends per inbox per day,
   spread through the day. This is the biggest risk to the account and to deliverability.
2. **Compliance basics.** No postal address in the footer (CAN-SPAM for US recipients), and
   no written legitimate interest assessment for EU recipients (several prospects are in
   the EU). Opt-out was recorded by hand.
3. **Only step 1 exists.** Follow-up tasks are created, but nothing drafts them. Teams
   typically run several touches; much of the reply volume comes after the first email.
4. **Replies are tracked by hand.** The schema has reply status (positive, neutral,
   negative, out of office), but nothing reads the inbox and classifies replies.
5. **The scheduler lives on a laptop.** launchd missed two of three days because the Mac was
   asleep, and a crashed run stays marked `running` until the next run starts. Production
   jobs run on a server or cloud cron.
6. **No learning loop.** Each message stores its signal and offer, but there is no report of
   reply rate by signal type, offer or segment, which is how signal-based teams decide
   which signals are worth buying.
7. **Sends outside the system.** 23 emails went from older drafts that the warehouse never
   saw, so they get no follow-ups and one bounce went unrecorded. Every send should start
   as a stored draft.
8. **Slow on hot signals.** The freshness window is 30 days; teams act on high-urgency
   signals (a new sales leader, a GTM job post) within hours or a day.
9. **No CRM.** Fine for one operator (the warehouse is the CRM, with a read-only MCP
   server); a team would expect HubSpot or Salesforce sync.

Email only is deliberate: there is no LinkedIn automation by rule.

## Next, in order

1. Sending domain + 2 to 3 warmed inboxes, a per-inbox daily cap, sends spread out; postal
   address in the footer; a one-page legitimate interest assessment.
2. Move the daily run to a cloud cron and close crashed runs at once.
3. Follow-up drafts (step 2 and 3) from the stored tasks, still human-sent.
4. Reply sync: read the inbox, classify replies, auto-record opt-outs.
5. A weekly report: replies and meetings by signal type, offer and segment.

## Sources

- Reply-rate benchmarks: [Lemlist](https://lemlist.com/blog/cold-email-benchmarks/),
  [Apollo](https://www.apollo.io/insights/what-is-a-good-benchmark-for-reply-rates-in-cold-outreach),
  [FirstSales](https://firstsales.io/blog/cold-email-reply-rate-benchmarks-2026/),
  [Cleverly](https://www.cleverly.co/blog/cold-email-statistics),
  [Unify](https://unifygtm.com/explore/cold-email-audit-fix-declining-reply-rates)
- Sender rules: [Google email sender guidelines FAQ](https://support.google.com/a/answer/14229414)
- Infrastructure and warmup: [Instantly](https://instantly.ai/blog/warm-up-email-domain/),
  [Clay](https://www.clay.com/blog/21-tips-for-keeping-cold-emails-out-of-spam-in-2023),
  [Rework](https://resources.rework.com/libraries/outbound-prospecting/cold-email-infrastructure),
  [Hunter](https://hunter.io/cold-email-guide/email-warmup/)
- Compliance: [Instantly on GDPR and CAN-SPAM](https://instantly.ai/blog/b2b-email-list-compliance-gdpr-canspam),
  [Lemlist on GDPR](https://www.lemlist.com/blog/gdpr-and-cold-emailing); check the FTC's
  CAN-SPAM guide and local counsel before relying on these.
- Stack patterns: [SpurIQ](https://dev.to/spuriqai/building-a-signal-based-outbound-system-the-gtm-engineering-stack-2026-40hg),
  [Clay](https://clay.com/blog/gtm-engineering), [Overloop](https://overloop.com/blog/gtm-engineer)
