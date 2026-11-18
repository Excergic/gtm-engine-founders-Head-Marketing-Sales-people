# Exergic Labs GTM engine

Grok finds people with a fresh buying signal. This engine stores every row it receives in a Postgres warehouse, enriches each person through a provider waterfall (Clay, then Deepline), promotes people to **golden** only when they have a verified work email *and* a LinkedIn URL, scores them against the ICP and opens a **ready** list of who to contact now. It runs by itself every day. Nothing is ever sent automatically.

**The warehouse is the only record.** Grok's CSVs are inputs: archived unchanged and stored row by row in `raw`. Nothing is tracked in, exported to, or edited in a CSV.

## Daily, on its own

```sh
uv run gtm schedule install     # every day at pipeline.run_at (07:30), via launchd
uv run gtm schedule status
uv run gtm pipeline             # the same run, by hand; --dry-run to plan only
```

| Step | Does | If it fails |
|---|---|---|
| `import` | New CSVs in `data/incoming/grok/{founders,VPs}/` → `raw` → staging / silver / golden | A bad file is reported and skipped; a file still being written waits for the next run |
| `enrich` | People never asked before **whose title is in the ICP** → Clay → Deepline (capped at `enrich_max_people`) | A failed person, provider or batch is recorded and skipped; skipped entirely below `min_deepline_credits` |
| `retry_failed` | Lookups whose last attempt failed with a retryable error | Same as above |
| `score` | ICP score for everyone; golden people in or out of ready | Recorded; the run is marked partial |

Every step runs even if an earlier one failed. A second run never overlaps a running one. The run and each step's result are stored in `ops.pipeline_runs` (job `pipeline`); output goes to `data/logs/pipeline.log`. The Mac has to be on (a missed run happens on wake).

## Demo

All output below is real, from the production warehouse (Neon Postgres), on 2026-11-17 and 2026-11-18. Names and emails are left out on purpose; only counts are shown.

### 1. Import Grok CSVs: every row gets an outcome

```sh
uv run gtm db import
```

```text
outcome      rows
migrated      274   people carried over from the old CSV tracker (since removed)
registered    100   new people
duplicate      15   identical row already imported
invalid         7   e.g. signal_date "2026-10 (approx.)" rejected by the 13-column contract
```

Original files are copied to `data/raw/YYYY-MM-DD/`, hashed, and never changed. Rows that look like an existing person (same name and company, different identifiers) are held for `gtm db review`.

### 2. Plan enrichment without spending anything

```sh
uv run gtm enrich run --request data/enrichment/requests/2026-11-17_batch1.json --dry-run
```

```json
{
  "dry_run": true,
  "steps": [
    {"stage": "linkedin_url", "provider": "deepline", "asked": 2},
    {"stage": "email",        "provider": "deepline", "asked": 5},
    {"stage": "verify",       "provider": "deepline", "asked": 9}
  ],
  "layers_now": {"staging": 2, "silver": 12}
}
```

Each provider is asked only about people still missing that field. Clay ran first on this batch and found 9 emails, so Deepline only gets the 5 misses for email lookup and the 9 Clay emails to verify.

### 3. Run it for real

```sh
uv run gtm enrich run --request data/enrichment/requests/2026-11-17_batch1.json
```

First run: verification of the 9 Clay emails.

```json
{"stage": "verify", "provider": "deepline", "asked": 9, "found": 9,
 "counts": {"now_golden": 9, "promoted_silver_to_golden": 9}}
"layers_now": {"staging": 2, "silver": 3, "golden": 9}
```

The two lookup stages found nothing on that run because of an adapter bug: it read the CLI's preview instead of the play's result. After the fix, the second run:

```json
{
  "steps": [
    {"stage": "linkedin_url", "provider": "deepline", "asked": 2, "found": 2,
     "counts": {"now_silver": 2, "promoted_staging_to_silver": 2}},
    {"stage": "email", "provider": "deepline", "asked": 5, "found": 5,
     "counts": {"now_golden": 5, "promoted_silver_to_golden": 5}},
    {"stage": "verify", "provider": "deepline", "asked": 0}
  ],
  "layers_now": {"golden": 14},
  "errors": []
}
```

14 of 14 people reached golden for 1.85 Deepline credits. BounceBan rated all 9 Clay emails `deliverable` (score 97 to 99). Deepline's email play validates the emails it finds itself, so those 5 needed no separate check.

### 4. Enrich everyone, in batches

```sh
uv run gtm enrich run --all --limit 25
```

```text
batch 1: 25/360 people, layers {'golden': 25}, errors 0
batch 2: 50/360 people, layers {'silver': 3, 'golden': 22}, errors 0
batch 3: 75/360 people, layers {'staging': 1, 'silver': 6, 'golden': 18}, errors 0
batch 4: 100/360 people, layers {'staging': 2, 'silver': 2, 'golden': 21}, errors 0
```

Batches are failure-safe. One person's failed lookup, a crashed provider, or a whole failed batch is recorded and skipped; the run carries on and only stops early if a provider reports a credit problem or 3 batches in a row fail. Deepline calls retry temporary failures with backoff and keep the real error message. Every lookup is logged in `ops.enrichment_attempts` as found, miss or error, and `gtm enrich run --retry-failed` re-runs only the retryable errors. If a run stops, `--resume` skips people already asked in the last 24 hours. Every paid result is written to `data/enrichment/runs/findings/` before it touches the database, so a dropped connection never loses credits (`gtm enrich apply <file>` replays it).

### 5. Where everyone stands

```sh
uv run gtm sql "select layer, count(*) as people,
  count(email) filter (where email_status='verified') as verified_emails,
  count(linkedin_url) as linkedin_urls
  from core.all_prospects group by layer order by layer"
```

Snapshot taken partway through the full run on 2026-11-18:

```text
layer     people  verified_emails  linkedin_urls
golden       100              100            100
silver        27                2             25
staging      247                0              0
```

Who found what:

```text
provider  field          findings
clay      email                 9
clay      linkedin_url          3
deepline  email               107
deepline  email_status        107   (verification verdicts)
deepline  linkedin_url         45
```

### 6. Score against the ICP and open the ready gate

```sh
uv run gtm icp run
```

```json
{
  "model_version": "icp-v1",
  "scored": 374,
  "tiers": {"tier_1": 223, "tier_2": 143, "tier_3": 8},
  "golden": 142,
  "ready": 113,
  "golden_not_ready_because": {"signal not fresh": 25, "tier_3 (outside ICP)": 4},
  "lifecycle_moves": {"scraped -> ready": 108, "researched -> ready": 5,
                      "scraped -> golden": 28, "researched -> golden": 1}
}
```

Golden means the data is good. **Ready** means contact them now:

| Ready needs | Why |
|---|---|
| Golden, verified email, not do-not-contact, `lead_status = open` | reachable and allowed |
| Signal 30 days old or less | timing; a stale golden person waits for a new signal |
| Tier 1 or 2 | in the ICP and worth it |
| An outreach angle and a source URL | the first message can cite the signal |

The gate runs both ways: when a signal ages past 30 days, a ready person goes back to golden. People already in outbound (`in_sequence`, `replied`, ...) are never moved. Every move is logged in `ops.state_transitions` with the reason.

**ICP score** (`icp:` in `config/gtm.yaml`, written to `core.score_snapshots`):

| Component | Points | Rule |
|---|---|---|
| Lead fit | 40 | Founder segment (founder, co-founder, CEO) or GTM leader (C-suite, VP or Head of marketing, sales, content, growth, revenue, GTM). Directors, managers and ICs are outside the ICP and always Tier 3. |
| Intent | 30 | Signal 30 days old or less and a qualifying kind (funding, launch, new in role, first GTM hire, open roles, expansion); 15 if fresh but another kind |
| Account fit | 20 | Unscored until ICP account research exists; never guessed |
| Engagement | 10 | Connected or positive reply; 5 if a connection request is pending |

Tier 1 is 70 or more, Tier 2 is 40 or more.

### 7. Who to contact today

```sh
uv run gtm icp ready
```

Lists ready people, best tier and freshest signal first, with their signal, angle and source.

### 8. Track prospects from any Claude session (MCP)

The repo ships a read-only MCP server, registered in `.mcp.json` as `gtm-warehouse`. Open Claude Code in this folder and ask things like "who's ready to contact?", "show me everything about Lewis", or "what failed in enrichment?".

| Tool | What it returns |
|---|---|
| `overview` | People per layer, golden by stage, ICP tiers, retry queue size, latest runs |
| `ready_list` | Who to reach out to now: email, LinkedIn, signal, angle, source |
| `golden_list` | Golden people filtered by `ready`, `not_ready` or `stale`, or by name or company |
| `prospect_detail` | One person: record, every signal, score and reasons, which provider found what, history |
| `recent_signals` | Signals from the last N days |
| `failures` | Enrichment lookups that failed, and whether a retry can help |

Connections are opened read-only, so no tool can change data or send anything. Run it yourself with `uv run gtm-mcp`.

## How it works

```text
Grok CSV ──► raw ──► staging ──► silver ──► golden ──► ready ──► outbound
             (append-only)  enrichment waterfall    ICP score + gate   (drafts, events,
                            Clay → Deepline         fresh signal       deals: FK to golden)
```

| Layer | Rule |
|---|---|
| `raw` | Every file and row exactly as received. Append-only. |
| `staging` | No verified email and no LinkedIn URL yet. |
| `silver` | Exactly one of: verified email, LinkedIn URL. |
| `golden` | Verified email **and** LinkedIn URL, plus name, title, company. |

- **One ID per person for life** (`core.prospect_registry`). Email and LinkedIn URL are unique across all layers, so the same person can't exist twice.
- **Promotion is automatic and one-way.** `ops.update_prospect` fills blanks and moves a person up a layer as soon as the data qualifies. It never demotes. A bounce sets `email_status = bounced`.
- **Fill blanks only.** A provider never overwrites a value; conflicts are kept and reported.
- **Provenance.** Every provider response is stored in `raw.provider_responses`, and every field change in `core.enrichment` with its run ID.
- **Columns match the Grok contract** (13 columns: `name`, `title`, `company`, `company_website`, `country`, `city`, `linkedin_url`, `x_handle`, `source_url`, `signal`, `signal_date`, `why_fit`, `outreach_angle`). See [docs/grok-brief.md](docs/grok-brief.md).
- **Outbound tables** (`outbound.messages`, `outbound.events`, `outbound.deals`, `outbound.tasks`) reference `golden.prospects`, so a message can't exist for someone who isn't golden.

Full design: [docs/architecture.md](docs/architecture.md).

## Enrichment providers

The waterfall lives in `config/gtm.yaml`:

```yaml
enrichment:
  waterfall:
    linkedin_url: [clay, deepline]
    email:        [clay, deepline]
    verify:       [deepline]
  providers:
    clay:     {adapter: clay_results, results_dir: enrichment/clay}
    deepline: {adapter: deepline,
               linkedin_play: prebuilt/person-to-linkedin-harvestapi,
               email_play: prebuilt/person-to-email,
               verify_tool: bounceban_verify_single}
    manual:   {adapter: manual}
```

| Provider | How it runs |
|---|---|
| Clay | Through the Claude connection; results are saved as JSON in `data/enrichment/clay/` and read by the adapter. |
| Deepline | `deepline` CLI. LinkedIn lookup only accepts identity-verified profiles; email lookup only accepts the play's final answer, never a candidate it rejected. |
| Manual | JSON findings in `data/enrichment/manual/`. Use only for emails you actually confirmed. |

To add a provider: write a class with `find(stage, people)` that returns `Finding` records, register it with `@register("name")` in `src/gtm/enrichers/`, and add it to the waterfall. Nothing else changes.

## Setup

```sh
uv sync --locked
cp .env.example .env          # set DATABASE_URL (Neon pooler URL)
uv run gtm db migrate
uv run gtm db status
```

For Deepline: `npm install -g deepline && deepline auth register`. Check credits with `deepline billing balance`.

## Commands

| Command | What it does |
|---|---|
| `gtm pipeline [--dry-run]` | The daily run: import → enrich new ICP people → retry failures → score. |
| `gtm schedule install [--at HH:MM]` / `status` / `uninstall` | Run `gtm pipeline` every day (launchd). |
| `gtm db import` | Load new Grok CSVs into raw and import their people. |
| `gtm db review` / `gtm db resolve ROW merge\|new\|reject` | Handle possible duplicates. |
| `gtm enrich batch --limit 25` | Pick people missing LinkedIn or a verified email; write a request file. |
| `gtm enrich run [--request FILE \| --all] [--only PROVIDER] [--dry-run] [--resume]` | Run the waterfall and write results to the warehouse. |
| `gtm enrich apply FILE` | Apply saved findings (replay a journal, or manual results). |
| `gtm icp run [--dry-run]` | Score everyone against the ICP and move golden people in or out of ready. |
| `gtm icp ready` | Who to contact now. |
| `gtm enrich run --retry-failed` | Retry only the lookups whose last attempt failed with a retryable error. |
| `gtm-mcp` | Read-only MCP server for Claude (see `.mcp.json`). |
| `gtm sql "QUERY"` | Read-only SQL, printed as JSON. |
| `gtm db status` / `gtm db migrate` | Migrations, each in its own transaction, checksummed. |

## Tests

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests run against a throwaway local Postgres (or `TEST_DATABASE_URL`), never Neon, and use fake providers, so they never spend credits.

## What's next

- **Outbound drafting:** a 3-touch email sequence and LinkedIn note per golden person, written from their signal and research into `outbound.messages`, with an approval queue. Nothing sends without a human.
- **Stuck report:** a daily list of people who have sat too long in a state.

## Rules

- No LinkedIn automation, and no message is ever sent by this code.
- The warehouse is the only record; CSVs are inputs only.
- `data/`, `.env`, real CSVs and recordings stay local and are Git-ignored.
- No email is marked verified without a real deliverability check.
