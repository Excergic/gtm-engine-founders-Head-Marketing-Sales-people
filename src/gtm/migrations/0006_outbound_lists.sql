-- Named lists of golden people kept apart from the main ready list, each for its own
-- campaign. The first is 'old-employer-email': everything ready except that the verified
-- email is at a company other than their current one (enrichment often returns a new
-- leader's previous employer). The score step keeps each list current: people are added
-- while they qualify and get removed_at when they no longer do. Rows are never deleted.

create table outbound.list_members (
    list_name   text not null check (list_name in ('old-employer-email')),
    prospect_id uuid not null references golden.prospects on delete restrict,
    email       text,
    company     text,
    reason      text not null,
    added_at    timestamptz not null default now(),
    removed_at  timestamptz,
    primary key (list_name, prospect_id)
);
create index on outbound.list_members (list_name) where removed_at is null;

grant select on outbound.list_members to gtm_reader;
grant select, insert, update on outbound.list_members to gtm_pipeline, gtm_outbound;
