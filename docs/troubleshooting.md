# Troubleshooting

```sh
uv run gtm health
uv run gtm import history
uv run gtm import review
uv run gtm import retry-failed
```

No API keys, company verification fields, setup command, or network troubleshooting is necessary. All records are local. `health` can run before the first import and reports that no master exists.

- **Review flag on a captured person:** missing email and LinkedIn URL. Capture is allowed; research the correct identifier and update the master when confirmed. Do not invent it.
- **Possible name/company duplicate:** the importer holds the source row. Review both observations manually; it will not merge uncertain identities. For a genuinely distinct person, add a reliable identifier to the master/input when known. For a confirmed already-captured duplicate with no reliable identifier, keep it held and use the existing master row; no automatic merge or deletion occurs.
- **Conflicting email/LinkedIn identities:** inspect the master and the retained original source. Correct only confirmed errors and retry. Existing master values are preserved.
- **Invalid row:** history/review shows row number and field validation. Successful rows stay committed. Correct producer data or publish a corrected new CSV and import it. Historical snapshots remain immutable for audit.
- **File deferred:** it is empty, changed during the stability window, or disappeared. Let Grok finish. Prefer atomic rename to publish finished files.
- **Another GTM writer is running:** let the existing process finish. The advisory lock releases when its process exits; do not delete state. Use one project/state path on one Mac.
- **Invalid master headers/JSON/dates or duplicate IDs:** stop imports, back up the damaged file, and restore an intact master from `data/backups/`. Keep all columns and original IDs. Never import the master as an input CSV.
- **Unsaved spreadsheet changes disappeared:** Excel/Numbers do not share the CLI writer lock. Pause/uninstall scheduling while editing, save and close the spreadsheet, then import. Backups may contain the previous version.
- **Disk full/permission error:** fix disk access/space and retry. Atomic replacement retains the old master if writing fails; original inputs and successful rows are retained.
- **Scheduler is not running:** check `gtm scheduler status`, logs in `data/state/logs`, macOS Background Items, whether the Mac is awake, and whether the external volume/project is mounted. Reinstall explicitly if paths changed. `install` starts a real import immediately; `--dry-run` only previews.

If you delete the master intentionally, keep a copy first. Restore it from backup rather than treating import history as a contact database. Source observation hashes allow safe replay after missing row checkpoints, but manual deletions and loss of history cannot be fully reconstructed from SQLite alone.

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests
```

## Agent research returns invalid values in accounts.0

Older agent schemas allowed an unrestricted finding string, while application rules required different vocabularies for intent and remote eligibility. The agent could return `unknown` for `contract_remote`, causing an account-level validation failure. Updated schemas constrain each finding with an enum; legacy remote `unknown` is normalized to `uncertain`. Other unsupported values still fail. Errors now identify the finding and allowed values without printing rejected data.

Reuse the saved run instead of paying for the same research again:

```sh
uv run gtm research resume RUN_ID --dry-run
uv run gtm research resume RUN_ID
```

Find the run ID under `data/research/runs/` or in the rejection message. The resume command does not invoke an agent or change the original result. It verifies current goals, domain coverage and evidence before updating research/derived scores. See [research recovery](scoring-and-research.md#recovering-a-saved-agent-result).
