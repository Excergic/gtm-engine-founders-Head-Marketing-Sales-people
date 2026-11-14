# Local GTM tracker

One editable master spreadsheet on your Mac: **`data/prospects.csv`**. Grok exports and manually captured LinkedIn prospects flow into it. Core imports and CRM tracking require no API, token, CRM account, enrichment service, or internet connection. Optional agent research uses Codex and internet only when explicitly invoked. No LinkedIn automation or message sending.

```sh
uv sync --locked
uv run gtm import grok --all --dry-run
uv run gtm import grok --all
uv run gtm daily
```

Save daily CSVs into these two folders (you can add multiple files each day):

```text
data/incoming/grok/
├── founders/
└── VPs/
```

`gtm import grok --all` scans both folders and any nested subfolders. The master `segments` column records `founders`, `VPs`, or both when the same identity appears in both. Completed files skip on repeated runs, so keep originals in place and add new files with dated names. Files already directly in the inbox still work. The optional scheduler uses the same scan.

See [docs/grok-brief.md](docs/grok-brief.md) for the standing Grok prompt and naming rules. Have Grok save completed CSVs into either folder under **`data/incoming/grok/`**. Each file follows a 13-column contract (`name`, `title`, `company`, `company_website`, `country`, `city`, `linkedin_url`, `x_handle`, `source_url`, `signal`, `signal_date`, `why_fit`, `outreach_angle`), validated row by row with Pydantic; see [Grok imports](docs/grok-integration.md). Invalid values are rejected and reported; rows missing signal fields or an identity are captured and flagged for review. Do not add artificial emails or guess identities.

The dry-run previews actual local creates, updates, skips, and review cases without writing the master, import history, or snapshots. A real import keeps original CSVs untouched and records row outcomes in SQLite. It can be repeated safely. Source columns not mapped to standard fields remain visible as extra master columns and in the source history. Domain/email syntax validation is not evidence of ownership or deliverability.

## Daily use

Open `data/prospects.csv` in Numbers or Excel. Update `connection_status`, `last_contact_at`, `next_followup_at`, `notes`, `consulting_stage`, and `job_stage`. Use dates like `2026-11-20` or ISO timestamps. Keep `id` and the history columns intact. Save and close the spreadsheet before importing; applications can otherwise overwrite each other's edits. CLI writes take a backup of the previous master in `data/backups/`.

```sh
uv run gtm people
uv run gtm update-status PERSON_ID connected
uv run gtm log-activity PERSON_ID --notes 'Sent a personal introduction' --followup 2026-11-20
uv run gtm followups
uv run gtm complete-action ACTION_ID
uv run gtm daily
uv run gtm health
uv run gtm import history
uv run gtm import review
uv run gtm import retry-failed
```

Manual capture:

```sh
uv run gtm add-prospect --name 'Alex Example' \
  --company 'Example Company' --linkedin-url 'https://www.linkedin.com/in/alex-example'
uv run gtm import linkedin path/to/authorized-export.csv
```

The master contains one row per person. Consulting and job stages are independent columns on that row; this deliberately replaces the old multi-opportunity CRM model with a simpler personal tracker. There is no separate company database, deal object, or task synchronization. Shared company domains are reported as distinct reported accounts, not verified company records.

## Automatic imports on macOS

```sh
uv run gtm scheduler install --dry-run
uv run gtm scheduler install
uv run gtm scheduler status
uv run gtm scheduler uninstall
```

Installation is optional and explicitly starts a real local import through RunAtLoad, then every 15 minutes while your Mac is awake. It needs no root privileges or shell activation. No real scheduler is installed by tests. For reliable publication, Grok should write `daily.csv.tmp`, close it, then atomically rename it to `daily.csv` in the same folder.

## Configuration

All defaults work without `.env`. Copy `.env.example` if desired; exported variables take precedence. The important optional settings are `LOCAL_TRACKER_CSV`, `GROK_CSV_DIRECTORY`, `LINKEDIN_CSV_DIRECTORY`, `LOCAL_STATE_DATABASE`, and `GROK_FILE_STABILITY_SECONDS`. `config/gtm.yaml` controls accepted columns, stages, and follow-up cadence. Data, state, backups, and `.env` are Git-ignored. Back up the entire data directory separately; Git is not a data backup.

## Transition from the previous integration

The application no longer contains the Attio client, schema setup, API mocks, API credentials, or network dependencies. Old remote commands are removed. Existing CSVs, managed snapshots, and the previous `data/state/gtm.sqlite` are preserved. New import state defaults to `data/state/local.sqlite` and uses separate fingerprints. Existing remote records are left untouched; there is no automatic cloud download or deletion. See [migration notes](docs/migration.md) if you previously stored contacts only in Attio.

## Tests and guides

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests use synthetic CSVs and temporary local files; no network or credentials. See [Grok imports](docs/grok-integration.md), [daily use](docs/daily-operations.md), [LinkedIn workflow](docs/linkedin-workflow.md), [architecture](docs/architecture.md), and [troubleshooting](docs/troubleshooting.md).

## Research, scoring and CRM events

The target profile is technical execution for remote GTM Engineer, Growth Engineer and Marketing Engineer work, full-time or contract. It favors technical marketing systems over strategy/leadership roles. Evidence-backed account research and a versioned Python policy produce separate full-time/contract scores and Tier 1/2/3, with missing evidence left unscored. Existing Grok signals are unverified hints until researched.

```sh
uv run gtm research queue
uv run gtm research run --limit 3 --dry-run
uv run gtm research run --limit 3
uv run gtm score
uv run gtm event PERSON_ID connection_sent
uv run gtm event PERSON_ID connection_accepted
uv run gtm event PERSON_ID positive_reply --notes 'Interested in a technical contract'
uv run gtm portfolio-report
```

Agent runs are optional, require a compatible logged-in Codex CLI, and use your Codex allowance. They are never triggered by CSV imports or the scheduler. Scoring uses cached evidence; imports/manual updates refresh derived columns offline. See [scoring, research and CRM workflow](docs/scoring-and-research.md) and the [portfolio case-study template](docs/portfolio-case-study.md). Weights are initial hypotheses, not validated hiring probabilities. Work location and public skill evidence remain unspecified in the default profile; update YAML before making location-specific or personalized qualification claims.
