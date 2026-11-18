# Exergic Labs GTM engine

Grok finds people with a fresh buying signal. This engine stores every row it receives, enriches each person through a provider waterfall (Clay, then Deepline), and promotes people to **golden** only when they have a verified work email *and* a LinkedIn URL. Outbound works from golden only. Nothing is ever sent automatically.

## Demo

All output below is real, from the production warehouse (Neon Postgres), on 2026-11-17 and 2026-11-18. Names and emails are left out on purpose; only counts are shown.

### 1. Import Grok CSVs: every row gets an outcome

```sh
uv run gtm db import
```

```text
outcome      rows
migrated      274   people carried over from the old CSV tracker
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

If a run stops, `--resume` skips people already asked in the last 24 hours. Every paid result is written to `data/enrichment/runs/findings/` before it touches the database, so a dropped connection never loses credits (`gtm enrich apply <file>` replays it).

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

### 6. Who to contact today

```sql
select name, title, company, email, linkedin_url, signal, signal_date, outreach_angle
from golden.prospects
where not do_not_contact and lead_status = 'open'
  and signal_date >= to_char(now() - interval '30 days', 'YYYY-MM-DD')
order by signal_date desc;
```

## How it works

```text
Grok CSV ──► raw ──► staging ──► silver ──► golden ──► outbound
             (append-only)  enrichment waterfall      (drafts, events, deals:
                            Clay → Deepline            FK to golden only)
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
| `gtm db import` | Load new Grok CSVs into raw and import their people. |
| `gtm db review` / `gtm db resolve ROW merge\|new\|reject` | Handle possible duplicates. |
| `gtm enrich batch --limit 25` | Pick people missing LinkedIn or a verified email; write a request file. |
| `gtm enrich run [--request FILE \| --all] [--only PROVIDER] [--dry-run] [--resume]` | Run the waterfall and write results to the warehouse. |
| `gtm enrich apply FILE` | Apply saved findings (replay a journal, or manual results). |
| `gtm db export-views` / `gtm db apply-edits` | Edit prospects in a spreadsheet and write validated changes back. |
| `gtm sql "QUERY"` | Read-only SQL, printed as JSON. |
| `gtm db status` / `gtm db migrate` | Migrations, each in its own transaction, checksummed. |

## Tests

```sh
uv run pytest -q       # 117 tests
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests run against a throwaway local Postgres (or `TEST_DATABASE_URL`), never Neon, and use fake providers, so they never spend credits.

## What's next

- **Outbound drafting:** a 3-touch email sequence and LinkedIn note per golden person, written from their signal and research into `outbound.messages`, with an approval queue. Nothing sends without a human.
- **Scheduling:** import, enrichment and a stuck-prospect report run on a schedule.

## Rules

- No LinkedIn automation, and no message is ever sent by this code.
- `data/`, `.env`, real CSVs and recordings stay local and are Git-ignored.
- No email is marked verified without a real deliverability check.

## Older local tracker

The original single-CSV tracker commands (`gtm people`, `gtm daily`, `gtm followups`, `gtm import grok`, `gtm scheduler`, `gtm research`, `gtm score`) still exist. The warehouse replaces them as the record of truth. See [docs/daily-operations.md](docs/daily-operations.md) and [docs/scoring-and-research.md](docs/scoring-and-research.md).
