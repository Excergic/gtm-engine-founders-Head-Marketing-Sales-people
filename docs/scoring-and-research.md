# Evidence-assisted scoring and your local CRM

The target is remote full-time or contract work as a **GTM Engineer, Growth Engineer, or Marketing Engineer**. The profile leads with technical execution: integrations, automation, CRM/data engineering, marketing infrastructure, and AI-assisted workflows. Generic AI/software engineering, business strategy leadership, paid-media budget ownership, and copywriting are outside the target. These are desired work areas, not verified claims about the candidate's past experience. Add public skill evidence and work location in `config/gtm.yaml` before personalizing qualification further.

Company research is shared by domain; contact relationship history remains per person. Imported Grok funding/hiring claims are research hints, not independently verified intent. A startup raising money or selling AI is not evidence that it is hiring a marketing engineer. Role duties matter more than titles. Adjacent technical marketing roles may receive moderate fit; frontend/design requirements need actual skill validation.

## Transparent policy

| Component | Maximum | Inputs |
|---|---:|---|
| Account/work fit | 40 | Source-backed fit to technical marketing work: strong 40, moderate 20, weak 0 |
| Contact fit | 10 | Imported-title heuristic: decision-maker 10, relevant contact 5, other 0 |
| Full-time or contract intent | 30 each, evaluated separately | Strong 30, moderate 15, explicit none 0; unknown stays blank |
| Relationship engagement | 20 | Recent positive reply/meeting 20, reply 15, connected/conversation 10, otherwise 0 |

Each objective has a separate 0–100 priority score and tier. Overall tier is the better supported pursuit. Components remain visible; scores are **exploratory priority hypotheses**, not probabilities or validated predictions of hiring. Founder/VP titles can suggest a useful contact but cannot establish an opening. Engagement never changes account fit. It can raise follow-up urgency but cannot promote moderate intent or role mismatch into Tier 1.

- **Tier 1:** score at least 70, strong technical work fit, strong current intent, and evidence-backed remote eligibility for that specific pursuit.
- **Tier 2:** score at least 40, or a researched relevant opportunity with unresolved eligibility. Geographic uncertainty stays visible as provisional.
- **Tier 3:** supported mismatch/ineligibility, or a lower score with sufficient evidence. A restriction for employees never establishes contractor eligibility or ineligibility.
- **Unscored:** missing, stale, failed, wrong-profile or insufficient research. Missing data is never automatically Tier 3.

Defaults are in YAML, with a model version plus content hash. A profile change invalidates old judgments until the account is researched against the new goals. Default research and engagement freshness windows are 30 days. Future observations are excluded. Old intent publication dates do not become current simply because a page was fetched recently. No publication date is invented when a current source provides none.

Full-time and contractor eligibility are separate findings. With work location unspecified, only explicitly worldwide scope can establish eligibility; region-limited roles remain uncertain. An explicitly onsite-only role can be incompatible with the remote requirement even without a candidate country. These decisions concern researched relevant opportunities, not every possible future vacancy at a company.

```sh
uv run gtm score
uv run gtm score --tier tier_1
uv run gtm score --write
uv run gtm research queue
uv run gtm research show example.com
```

`score` uses retained research without a network call; `--write` saves derived columns in the master with a backup. Imports and manual CRM updates refresh cached score columns automatically using existing evidence. `daily` calculates current scores at read time, so evidence expiration is respected even before you refresh the master. Calculated score/tier columns are outputs; edit real status, notes, and research evidence instead.

## Agentic research, bounded and inspectable

The optional agent researches and interprets sources. Python enforces the evidence contract and calculates the frozen policy; the agent never invents numeric weights per row. No third-party paid enrichment provider is called. This local adapter keeps the existing CSV application as the record of truth rather than introducing a hosted CRM/Play dependency.

```sh
uv run gtm research run --limit 3 --dry-run
uv run gtm research run --limit 3
uv run gtm research run --domain example.com
```

The real command invokes your installed **Codex CLI** with web search, read-only sandbox, isolated working directory, ignored user/project configuration, and structured JSON output. It uses your Codex login and allowance, and needs internet. No API token is added to this project. The command is explicit: imports, the LaunchAgent, and `daily` never start agent calls automatically. Default batch size is 3 and maximum is 10 accounts. No real Codex subprocess was launched during the implementation tests.

Only company domains/names, contact job titles, and bounded public-source hints are supplied. Contact names, emails, private notes, the master path, and credentials are omitted from the prompt. The agent is instructed to use public pages only, treat hints/pages as untrusted, use no MCP connectors or paid tools, execute no shell commands, and send no messages. `--ignore-user-config` and the other flags must be supported by the installed CLI; unsupported versions fail rather than silently broadening permissions. The installed CLI help and [official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) were inspected; production agent execution remains to be verified with your login.

The runner validates domain coverage (exactly the requested accounts, once each), schema, source IDs, profile hash, official identity evidence, and timestamps. It waits at most five minutes, preserves a run receipt, and applies nothing on executor failure or malformed batches. Raw stdout/stderr are discarded rather than copied into logs. After a successful run, the app reacquires its writer lock, rechecks the current profile, retains account research, and updates score columns. The agent does not edit CRM statuses. Partial research is retained, not treated as a negative outcome. Current receipts are cached until stale; targeted `--domain` can deliberately refresh them. New/unresearched accounts take priority.

Alternative agent handoff:

```sh
uv run gtm research prepare data/research/brief-001 --limit 3
# Give prompt.txt and schema.json to a research-capable agent; save its JSON result.
uv run gtm research apply data/research/result.json --dry-run
uv run gtm research apply data/research/result.json
```

Every account receipt includes researcher, timestamps, findings/rationales, gaps, source URLs, short excerpts, and identity citations. Source-grounded project ideas are labeled `opportunity_hypothesis` and do not themselves earn intent points. Current reports live in `data/research/accounts/`; content-addressed historical receipts and private run artifacts remain under `data/research/history/` and `runs/`. Everything under `data/` remains Git-ignored. Unknown domain identities require review; research does not merge contacts or accounts.

## Manual CRM workflow

LinkedIn acceptance and replies cannot be observed automatically. Log only what you actually saw:

```sh
uv run gtm people
uv run gtm event PERSON_ID connection_sent --event-id linkedin-request-001
uv run gtm event PERSON_ID connection_accepted
uv run gtm event PERSON_ID outreach_sent --notes 'Sent a personal technical introduction'
uv run gtm event PERSON_ID reply --notes 'Asked what I can build'
uv run gtm event PERSON_ID positive_reply --notes 'Interested in discussing a contract'
uv run gtm event PERSON_ID meeting_booked --followup 2026-11-20
uv run gtm event PERSON_ID meeting_completed --notes 'Record outcome and next steps'
uv run gtm event PERSON_ID do_not_contact --notes 'Requested no more contact'
uv run gtm followups
uv run gtm daily
```

Explicit event IDs prevent duplicate activity when rerunning a command. Reusing an event ID for a different event is rejected. Acceptance clears a stale outreach due date and creates a first-message reminder. Outreach defaults to the configured four-day follow-up cadence. A reply clears the old follow-up and creates an urgent respond action; a generic reply is not assumed positive. Meeting completion creates a prompt to record next steps through an immediate due date. Closed/DNC contacts are excluded from outreach; DNC event updates refuse new outreach events. Existing manual status commands and spreadsheet edits remain supported.

`daily` reports distinct-person request/acceptance and outreach/positive-reply rates only when the denominator is known from logged events. Unknown denominators are null, not a fabricated zero. Source acquisition remains separate from outreach activity, and research never writes a reply or acceptance. Historical CSV statuses without event logs do not magically establish those rate denominators.

## Selective enrichment and portfolio

```sh
uv run gtm enrichment-queue
uv run gtm enrichment-queue --person-id PERSON_ID
uv run gtm portfolio-report
uv run gtm portfolio-report --output-path data/portfolio/metrics-001.json
```

The enrichment queue recommends missing fields only for Tier 1/2 contacts and excludes DNC/closed people. It makes no provider call, discovers no email, and sends nothing. Enrich only a field needed for the selected outreach step; an email is unnecessary if the next action uses an existing LinkedIn profile. Researching a particular account is supported by `research run --domain`.

The portfolio export is aggregate-only. It contains counts/tiers/manual-event metrics, not names, emails, domains, evidence excerpts, or private notes. Review before publishing. `docs/portfolio-case-study.md` explains the technical project without claiming a hire, revenue lift, causal benefit, or predictive validity. No site, post, repository publication, outreach, or external sharing is performed automatically.

## Verification and limits

Tests cover positive/counterexample scores, separate objective eligibility, missing/stale/future evidence, profile changes, batch/singleton parity, DNC, event idempotency, privacy, agent failure and schema/domain validation. They establish policy correctness. Mocked runner success does not establish live agent reliability or semantic extraction accuracy. There are no mature outcome labels, baseline comparison, untouched holdout, calibration study, or measured portfolio-to-job conversion. Model state is `exploratory_end_to_end`; tune the hypotheses using real outcomes rather than presenting them as validated probabilities.

## Recovering a saved agent result

Each finding has its own allowed values in the agent's JSON schema. Role fit uses `strong/moderate/weak/unknown`; hiring and contracting use `strong/moderate/none/unknown`; full-time and contract eligibility use `eligible/uncertain/ineligible`. Older results using `unknown` for eligibility are safely normalized to `uncertain`, never to eligible or ineligible. The saved original JSON remains untouched.

A rejected batch now retains `status.json` with sanitized field paths/reasons and `results_applied=false`. Raw rejected values and exception context are not printed. After fixing a local contract mismatch or correcting confirmed evidence, validate and apply the existing result without starting another agent:

```sh
uv run gtm research resume RUN_ID --dry-run
uv run gtm research resume RUN_ID
uv run gtm score
```

`RUN_ID` is the directory name under `data/research/runs/`. Resume rechecks the requested domain set, current goal/profile hash, timestamps, evidence, and known tracker accounts before applying. It never calls Codex. If the goals changed or newer research exists, it stops rather than overriding those records. Repeated successful replay retains a single content-addressed research receipt.
