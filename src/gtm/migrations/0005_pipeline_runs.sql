-- The daily pipeline (gtm pipeline) records one run of its own, with each step's
-- outcome; the steps keep writing their own import/enrich/score runs as before.

alter table ops.pipeline_runs drop constraint pipeline_runs_job_check;
alter table ops.pipeline_runs add constraint pipeline_runs_job_check
    check (job in ('import', 'enrich', 'promote', 'research', 'score', 'draft', 'reply_sync',
                   'booking', 'migration', 'stuck_report', 'pipeline'));
