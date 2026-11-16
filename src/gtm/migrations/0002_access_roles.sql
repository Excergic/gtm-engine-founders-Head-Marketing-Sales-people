-- Access roles (NOLOGIN groups). Login users are granted one of these later.
-- gtm_outbound cannot see raw, staging or silver at all, so outbound can only ever use
-- golden prospects.

do $$
declare r text;
begin
    foreach r in array array['gtm_reader', 'gtm_pipeline', 'gtm_outbound'] loop
        if not exists (select from pg_roles where rolname = r) then
            execute format('create role %I nologin', r);
        end if;
    end loop;
end $$;

-- Read-only reporting (dashboards, read-only Claude connection).
grant usage on schema raw, core, staging, silver, golden, outbound, ops to gtm_reader;
grant select on all tables in schema raw, core, staging, silver, golden, outbound, ops
    to gtm_reader;

-- Import, enrichment, research, scoring and promotion jobs.
grant usage on schema raw, core, staging, silver, golden, ops to gtm_pipeline;
grant select, insert on all tables in schema raw to gtm_pipeline;
grant select, insert, update on all tables in schema core, ops to gtm_pipeline;
grant select, insert, update, delete on staging.prospects, silver.prospects to gtm_pipeline;
grant select, insert, update on golden.prospects to gtm_pipeline;
grant select on outbound.messages to gtm_pipeline;

-- Drafting, sending, reply sync and booking jobs.
grant usage on schema golden, outbound, core, ops to gtm_outbound;
grant select, update on golden.prospects to gtm_outbound;
grant select on core.companies, core.signals, core.person_research, core.account_research,
    core.score_snapshots to gtm_outbound;
grant update (used_in_message_id) on core.signals to gtm_outbound;
grant select, insert, update on outbound.messages, outbound.deals, outbound.tasks
    to gtm_outbound;
grant select, insert on outbound.events to gtm_outbound;
grant select, insert on ops.ai_outputs, ops.state_transitions, ops.pipeline_runs
    to gtm_outbound;
grant update on ops.pipeline_runs to gtm_outbound;
grant execute on function ops.transition(uuid, text, uuid, text, text) to gtm_outbound;

-- Moving people runs with the owner's rights, so jobs need only EXECUTE, and the
-- functions are the only path for layer and lifecycle changes.
alter function ops.register_prospect(jsonb, uuid, text, text, bigint)
    security definer set search_path = pg_catalog, pg_temp;
alter function ops.update_prospect(uuid, jsonb, uuid, text, text)
    security definer set search_path = pg_catalog, pg_temp;
alter function ops.transition(uuid, text, uuid, text, text)
    security definer set search_path = pg_catalog, pg_temp;
revoke execute on function ops.register_prospect(jsonb, uuid, text, text, bigint),
    ops.update_prospect(uuid, jsonb, uuid, text, text),
    ops.transition(uuid, text, uuid, text, text) from public;
grant execute on function ops.register_prospect(jsonb, uuid, text, text, bigint),
    ops.update_prospect(uuid, jsonb, uuid, text, text),
    ops.transition(uuid, text, uuid, text, text) to gtm_pipeline;
