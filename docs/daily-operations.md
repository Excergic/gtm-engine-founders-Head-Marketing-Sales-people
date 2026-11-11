# Daily operations

```sh
uv run gtm import grok --all
uv run gtm daily
```

Open `data/prospects.csv` in Excel or Numbers. Edit notes, dates, DNC, and pipeline stages directly. Preserve IDs, headers, and the JSON history columns. Save as UTF-8 CSV, then close it before CLI imports/manual updates. Use ISO dates like `2026-11-20`; date-only and naive timestamp values are interpreted as UTC. Future explicit follow-up dates appear once reached. The CLI keeps pre-write backups in `data/backups/`; take your own backups after important spreadsheet edits too.

```sh
uv run gtm people
uv run gtm update-status PERSON_ID connection_sent
uv run gtm update-status PERSON_ID connected
uv run gtm log-activity PERSON_ID --notes 'Sent a personal introduction' --followup 2026-11-20
uv run gtm update-status PERSON_ID replied --notes 'Asked for details'
uv run gtm log-activity PERSON_ID --notes 'Requested no further contact' --do-not-contact
uv run gtm followups
uv run gtm complete-action ACTION_ID
```

Pending requests generate a review after seven days; connected people generate a first-conversation reminder; replies generate a respond reminder. An explicit next-follow-up date takes precedence. These are deterministic views, not messages. Completed action IDs persist across reimports. Changing a trigger date can intentionally generate a new action. DNC and closed people are excluded. Dates and statuses must reflect things you actually observed on LinkedIn.

The simplified pipeline uses one consulting stage and one job stage per person, each independently editable. It does not represent several simultaneous deals of the same objective for one person.

```sh
uv run gtm opportunities --person-id PERSON_ID --objective consulting_client --stage qualified
uv run gtm opportunities --person-id PERSON_ID --objective remote_job --stage role_researched
uv run gtm opportunities
```

`daily` reports actual local people, reported domains, acquisition sources, review flags, active stages and top ten due actions. A person from two sources counts once in total people and once in each relevant source category. `daily` does not import files automatically. No cloud service is involved.

Account research, technical-role scoring, and observed connection/reply events are documented in [scoring and research](scoring-and-research.md). Use `gtm event PERSON_ID connection_sent`, `connection_accepted`, `outreach_sent`, `reply`, or `positive_reply` to maintain relationship history; these never send messages.
