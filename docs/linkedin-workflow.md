# LinkedIn workflow

Capture prospects manually or import authorized CSV exports. The tool never logs into LinkedIn, scrapes profiles, reads inboxes, sends connection requests, or sends messages.

```sh
uv run gtm add-prospect --name 'Alex Example' --company 'Example Co' \
  --linkedin-url 'https://www.linkedin.com/in/alex-example'
uv run gtm import linkedin path/to/prepared.csv --dry-run
uv run gtm import linkedin path/to/prepared.csv
uv run gtm people
uv run gtm update-status PERSON_ID connection_sent
uv run gtm update-status PERSON_ID connected
uv run gtm log-activity PERSON_ID --notes 'Sent introduction' --followup 2026-11-20
uv run gtm followups
```

Observe acceptances/replies yourself and update the status in the master CSV or CLI. Supported statuses: discovered, connection_sent, connection_pending, connected, conversation_started, replied, meeting_booked, no_response, closed. LinkedIn URLs normalize to exact `/in/` profile URLs; name/company alone never auto-merges. People without an email and URL may still be captured with a review flag. Imported source data remains in the master history; current manual statuses take precedence over later exports.
