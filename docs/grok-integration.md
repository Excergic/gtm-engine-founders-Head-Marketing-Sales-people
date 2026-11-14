# Grok CSV integration

Have the bot save founder CSVs into `data/incoming/grok/founders/` and VP CSVs into `data/incoming/grok/VPs/`. Both directories are created on a real import. With a custom `GROK_CSV_DIRECTORY`, use the same `founders/` and `VPs/` subfolders there. No API call and no change to your existing Grok columns is needed. Publish completed files using `.tmp` → `.csv` rename in the same directory. The job additionally checks size, mtime, inode and ctime stability over `GROK_FILE_STABILITY_SECONDS`, then verifies the captured snapshot. A stability check cannot prove a paused writer is finished; atomic rename is preferred.

## The Grok contract

Every Grok CSV has these 13 columns (see [grok-brief.md](grok-brief.md)). Each row is
validated by the Pydantic `Prospect` model in `src/gtm/models.py`; the header mapping
lives in `config/gtm.yaml` and is checked against that model when config loads.

| Grok column | Master field | Validation |
|---|---|---|
| `name` | `full_name` | required, 2–200 chars |
| `title` | `job_title` | ≤200 chars; drives the ICP title rule |
| `company` | `company_name` | ≤200 chars |
| `company_website` | `website` → `domain` | valid http(s) URL / domain |
| `country`, `city` | same | ≤100 chars |
| `linkedin_url` | same | must be a personal `/in/` profile; normalized |
| `x_handle` | same | `@name`, `name`, or x.com/twitter.com URL → `@name` |
| `source_url` | same | http(s) URL |
| `signal` | same | ≤1000 chars |
| `signal_date` | same | `YYYY-MM-DD`; `YYYY-MM` / `YYYY` kept as given; never in the future |
| `why_fit`, `outreach_angle` | same | ≤1000 chars |

Outcomes per row:

- **Invalid value** (bad URL, future or malformed date, company page instead of a
  profile): the row is rejected and reported in the import summary; nothing is written.
- **Missing `signal`, `signal_date`, or `source_url`**, or a `signal_date` with only
  month/year precision: the person is captured and flagged `needs_review`, with the
  reason in `review_reason`. Nothing is silently dropped and no date is invented.
- **Missing both email and LinkedIn URL**: captured and flagged for review.
- A possible same-name/company match is held for review rather than merged.

Columns outside the contract are still kept as extra master columns and in source
history. Imported values fill blanks only; your manual edits always win.

Import fingerprints use a frozen mapping (`src/gtm/fingerprint.py`), so editing
`config/gtm.yaml` never makes already-imported files look new.

Arbitrary campaign text must be mapped separately rather than passed as an objective: objectives are `consulting_client` or `remote_job`.

```sh
uv run gtm import grok path/to/daily.csv --dry-run
uv run gtm import grok --all --dry-run
uv run gtm import grok --all
uv run gtm import history
uv run gtm import review
uv run gtm import retry-failed
```

Dry-run now checks the local master and predicts creates/updates; there is no separate CRM preview. It writes no prospect/state/snapshot data. Exit 0 means complete, exit 2 means review/invalid/failed rows, and exit 1 means a command/configuration/local filesystem failure. A newly captured weak identity appears in `gtm import review` even though its row imported successfully.

Each original CSV stays untouched. SHA-256 content, source, frozen fingerprint version and folder segment determine file identity, so renaming a completed CSV does not import again. New files can update existing people. Successful row checkpoints skip on rerun; crash recovery reconciles row observation keys in the master. Keep source history columns intact. Raw imports do not create opportunity stages unless requested by explicit `create_opportunity`/`opportunity_stage` fields; normally set stages in the master after reviewing leads.

Optional macOS scheduling:

```sh
uv run gtm scheduler install --dry-run
uv run gtm scheduler install
uv run gtm scheduler status
uv run gtm scheduler uninstall
```

The LaunchAgent lives under `~/Library/LaunchAgents/`, uses the virtual environment's absolute Python path, loads `.env` through the app, and logs under `data/state/logs/`. It runs at load and every 900 seconds without an interactive shell. No API keys are embedded. The Mac must be awake and the external volume mounted; missed runs process retained files next time. Do not keep the master open with unsaved spreadsheet edits while scheduling is enabled. Installation requires an explicit command; previews and tests never activate a real agent.

## Daily founder and VP files

Put all daily exports into `founders/` or `VPs/` under the Grok inbox. Use dated filenames; originals need not be moved or removed. The `--all` command scans both recursively, including new files on future runs. Hidden folders, symlink directories, and temporary files are excluded. Root-level CSVs remain supported.

The `segments` column in the master is inferred from the folder; no input column is required. An exact matched person can have both labels without two master records. File checkpoints distinguish the two segments so identical CSV content placed in each still adds both labels. Repeated content within the same segment skips. Snapshot retries retain the original folder's segment. Existing master CSVs without the new column load normally; the next real change adds it. Rows imported earlier from the inbox root keep an empty segment until a later categorized observation matches them.
