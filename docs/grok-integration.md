# Grok CSV integration

Grok saves founder CSVs into `data/incoming/grok/founders/` and VP CSVs into
`data/incoming/grok/VPs/`. The folder sets the person's `segment`. These files are
**inputs only**: `gtm db import` (or the daily `gtm pipeline`) copies each one unchanged to
`data/raw/YYYY-MM-DD/`, stores every row in `raw.grok_rows`, and registers the people in the
warehouse. Nothing is ever written back to a CSV; the warehouse is the only record.

Publish finished files with a `.tmp` -> `.csv` rename. A file modified in the last
60 seconds is treated as still being written and is picked up on the next run.

## The Grok contract

Every Grok CSV has these 13 columns (see [grok-brief.md](grok-brief.md)). Each row is
validated by the Pydantic `Prospect` model in `src/gtm/models.py`; the header mapping
lives in `config/gtm.yaml` and is checked against that model when config loads.

| Grok column | Model field | Validation |
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

Outcomes per row (stored in `ops.raw_row_outcomes`, one per raw row):

| Outcome | When |
|---|---|
| `registered` | A new person, placed in staging, silver or golden by their data |
| `merged` | Same LinkedIn URL or email as an existing person: blanks filled, segment added, newer signal appended |
| `duplicate` | The identical 13 values were already imported |
| `held_for_review` | Same name and company as someone else, but no shared identifier: `gtm db review` |
| `invalid` | A value failed validation (bad URL, malformed or future date, company page instead of a profile) |

Rows missing `signal`, `signal_date` or `source_url`, or with only month/year precision,
are kept and flagged `review` in raw; no date is invented. A malformed file (broken quoting,
duplicate headers) is reported and skipped; the other files still import.

```sh
uv run gtm db import            # new files -> raw -> staging / silver / golden
uv run gtm db review            # possible duplicates held for a decision
uv run gtm db resolve RAW_ROW_ID merge|new|reject
```

A file is identified by its SHA-256 and folder, so re-dropping or renaming the same file
never imports it twice.
