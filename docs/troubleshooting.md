# Troubleshooting

Start with the pipeline's own record:

```sh
uv run gtm schedule status                     # installed? last exit code?
tail -50 data/logs/pipeline.log                # what the last run printed
uv run gtm sql "select job, status, started_at, error_summary from ops.pipeline_runs order by started_at desc limit 10"
```

Or ask Claude in this folder ("what failed?"); the read-only `gtm-warehouse` MCP server
answers from the warehouse.

| Symptom | What to do |
|---|---|
| A pipeline step shows `failed` | The other steps still ran. Read the step's `error` in `ops.pipeline_runs.details`, fix the cause, run `gtm pipeline` again; every step is safe to rerun. |
| `enrich` shows `skipped: credits below ...` | Top up Deepline (`deepline billing balance`). The next run continues. |
| Lookups failed (timeouts, 5xx) | They are in the retry queue; the next `gtm pipeline` or `gtm enrich run --retry-failed` retries them. `failures` in the MCP server lists them with the real error. |
| Pipeline says another run is in progress | A run is marked running. If it really crashed, it is marked failed after 6 hours, or close it in `ops.pipeline_runs`. |
| `cannot connect to the warehouse` | Check `DATABASE_URL` in `.env` and that the Neon project is up. Connections retry with backoff, so this means a real outage. |
| A file was not imported | Check `raw` problems in the import output: it was malformed or still being written (modified in the last 60 seconds). |
| Possible duplicate held | `gtm db review`, then `gtm db resolve RAW_ROW_ID merge|new|reject`. Nothing is merged on a guess. |
| Scheduled run did not happen | The Mac was off, or the external volume was not mounted. launchd runs a missed job when the Mac wakes. Reinstall with `gtm schedule install` if the project moved. |

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests
```
