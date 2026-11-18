-- Every time a provider is asked about a person, one row: found, miss, or error.
-- Errors are the retry queue (gtm enrich retry-failed); found/miss let a resumed run
-- skip people already asked, so credits are not spent twice. Append-only.

create table ops.enrichment_attempts (
    attempt_id   bigint generated always as identity primary key,
    prospect_id  uuid not null references core.prospect_registry,
    stage        text not null check (stage in ('linkedin_url', 'email', 'verify')),
    provider     text not null,
    outcome      text not null check (outcome in ('found', 'miss', 'error')),
    error        text,
    retryable    boolean,
    run_id       uuid references ops.pipeline_runs,
    attempted_at timestamptz not null default now(),
    check ((outcome = 'error') = (error is not null))
);
create index on ops.enrichment_attempts (prospect_id, stage, provider, attempted_at desc);
create index on ops.enrichment_attempts (outcome) where outcome = 'error';

create function ops.attempts_append_only() returns trigger language plpgsql as $$
begin
    raise exception 'ops.enrichment_attempts is append-only' using errcode = 'P0001';
end $$;
create trigger enrichment_attempts_append_only before update or delete
    on ops.enrichment_attempts for each row execute function ops.attempts_append_only();

-- The latest attempt per person, stage and provider; open errors are the retry queue.
create view ops.enrichment_latest as
    select distinct on (prospect_id, stage, provider) *
    from ops.enrichment_attempts
    order by prospect_id, stage, provider, attempted_at desc, attempt_id desc;

grant select on ops.enrichment_attempts, ops.enrichment_latest to gtm_reader;
grant select, insert on ops.enrichment_attempts to gtm_pipeline;
grant select on ops.enrichment_latest to gtm_pipeline;
