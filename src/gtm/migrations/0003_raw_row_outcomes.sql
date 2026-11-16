-- What happened to every raw row, so nothing silently drops out. Rows held for review
-- form the review queue; a human resolves them (merge into the candidate, register as a
-- new person, or reject).

create table ops.raw_row_outcomes (
    raw_row_id            bigint primary key references raw.grok_rows,
    outcome               text not null
                          check (outcome in ('registered', 'merged', 'duplicate',
                                             'held_for_review', 'invalid', 'migrated')),
    prospect_id           uuid references core.prospect_registry,
    candidate_prospect_id uuid references core.prospect_registry,
    reason                text,
    run_id                uuid references ops.pipeline_runs,
    decided_at            timestamptz not null default now(),
    resolution            text check (resolution in ('merged', 'new', 'rejected')),
    resolved_by           text,
    resolved_at           timestamptz,
    check (outcome <> 'held_for_review' or candidate_prospect_id is not null),
    check ((resolution is null) = (resolved_at is null)),
    check (resolution is null or outcome = 'held_for_review')
);
create index on ops.raw_row_outcomes (outcome) where resolution is null;

-- Only the resolution fields may change after a decision is recorded.
create function ops.outcomes_guard() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE'
       or (to_jsonb(new) - array['resolution', 'resolved_by', 'resolved_at', 'prospect_id'])
          <> (to_jsonb(old) - array['resolution', 'resolved_by', 'resolved_at', 'prospect_id'])
    then
        raise exception 'ops.raw_row_outcomes: only the resolution can change'
            using errcode = 'P0001';
    end if;
    return new;
end $$;
create trigger raw_row_outcomes_guard before update or delete on ops.raw_row_outcomes
    for each row execute function ops.outcomes_guard();

-- Rows already tied to a person by the initial load.
insert into ops.raw_row_outcomes (raw_row_id, outcome, prospect_id, reason)
select g.raw_row_id, 'migrated', coalesce(r.prospect_id, s.prospect_id),
       'linked during the initial load from prospects.csv'
  from raw.grok_rows g
  left join core.prospect_registry r on r.first_raw_row_id = g.raw_row_id
  left join lateral (select prospect_id from core.signals s
                      where s.raw_row_id = g.raw_row_id limit 1) s on true
 where r.prospect_id is not null or s.prospect_id is not null;

grant select, insert, update on ops.raw_row_outcomes to gtm_pipeline;
grant select on ops.raw_row_outcomes to gtm_reader;
