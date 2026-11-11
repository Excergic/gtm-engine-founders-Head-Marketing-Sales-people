# Moving to the local tracker

The Attio integration has been removed from application code, dependencies, configuration examples, and CLI commands. The local `.env` has its Attio entries removed without printing them. The connected external workspace, its records/schema, your account, and any separately registered MCP connection are untouched. To revoke the former token, use the service's integration settings yourself if desired; no remote token revocation is performed by this project.

Raw inbox CSVs and all previous managed snapshots remain on disk. The old `data/state/gtm.sqlite` is not deleted. New local imports use `data/state/local.sqlite` by default and a separate fingerprint namespace, so previous remote success checkpoints cannot incorrectly suppress local capture. Historical files created by a different backend stay in the data folders and are not automatically treated as new import jobs.

Reimport retained original files to populate `data/prospects.csv`. The new tracker needs no verification columns. Data stored only remotely is not automatically downloaded: export it to CSV and use the appropriate local source import. Check aliases in `config/gtm.yaml` first, and preview before importing. Remote activity/task/custom JSON history is not automatically migrated. Keep any remote export as an untouched backup.

The simpler model is one person per master row, with company details, source history, consulting stage and job stage. There is no independent company record, remote task mirror, enrichment adapter, or multi-deal pipeline. You can still use the macOS scheduler; the same command now imports into the local CSV. No scheduler is installed or activated during migration.
