# Architecture

A small modular Python CLI: Pydantic validates and normalizes input, YAML maps CSV headers, Typer exposes operations, and standard-library CSV/SQLite/locking provide persistence. Core ingestion/tracking has no outbound HTTP calls or external services. An optional, explicitly invoked Codex subprocess can research public web sources; it returns structured evidence to the local application.

`Grok / authorized LinkedIn CSV → normalize → match → atomic master CSV → daily follow-up view`

The primary data lives in `data/prospects.csv`. One row represents one person; company name/domain stay on that row. `sources` and `objectives` are pipe-separated sets; `original_source` retains first acquisition. Consulting and remote-job stages are independent columns. Activity history and source observations are JSON lists in protected columns, not separate databases. Extra source fields are also projected as ordinary columns for spreadsheet use. New observations fill blank fields and append notes; existing manual edits and nonempty identity fields win.

Match by exact normalized email or LinkedIn URL. If identifiers disagree or identify multiple rows, hold for manual review. Name plus company/domain only suggests a duplicate and never auto-merges. A brand-new person with no strong identity is captured with `needs_review=true`; repeat ingestion of the exact row cannot duplicate them because observation fingerprints are saved in the same master write. Syntax validation does not verify emails or company ownership. No domain-verification input is required for local capture.

Every importer/manual CLI mutation holds a local advisory writer lock associated with its SQLite state path. Use one project/state path and one Mac; external spreadsheet applications do not honor this lock. Save and close the spreadsheet before writes. The whole master is rewritten through a private temporary file, flush/fsync, and atomic replace. Before each replacement the old master is backed up in `data/backups/`. Unknown user-added columns are retained. Invalid headers, dates, identities, JSON history, or duplicate IDs stop ingestion instead of destroying the file.

SQLite stores fingerprints, row outcomes, run history, snapshot references, and audit metadata only. It has no prospect table. Each row commits to CSV before its operational checkpoint; after a crash, the observation in the master prevents another create. Successful rows survive subsequent row failures. Originals remain in the inbox; managed snapshots move through processing, processed, and failed directories. The existing stable-file detection, snapshot recovery, and LaunchAgent logic are retained.

Follow-up actions are derived from actual dates/statuses in the master. A stable hash of person/rule/trigger date identifies each action; completion IDs are stored in the same master. Seven-day pending reviews, first-conversation reminders, reply reminders, and explicit due dates are supported. DNC/closed people generate no outreach actions. These reminders do not imply a message was sent. This local version intentionally omits CRM schema management, remote synchronization, enrichment, and separate multi-contact opportunity entities.

## Research boundary

Account-level JSON research receipts are local source evidence, not another CRM. The master CSV still owns contact data and manual events. Fixed Python policy computes numeric scores from AI-assisted, cited findings; no per-row model-generated weights. Full-time/contract eligibility are independent. Missing evidence stays unscored, and profile changes invalidate cached judgments. Derived score columns refresh on writes and daily views calculate freshness again. See [research/scoring](scoring-and-research.md).
