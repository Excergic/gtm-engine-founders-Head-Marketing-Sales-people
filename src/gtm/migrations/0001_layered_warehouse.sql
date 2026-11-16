-- Layered warehouse: raw -> staging -> silver -> golden, plus core, outbound and ops.
-- Spec: docs/architecture.md, "Data storage and CRM model". Do not edit once applied;
-- add a new numbered migration instead.

create schema if not exists raw;
create schema if not exists core;
create schema if not exists staging;
create schema if not exists silver;
create schema if not exists golden;
create schema if not exists outbound;
create schema if not exists ops;

-- ---------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------

create function ops.reject_change() returns trigger language plpgsql as $$
begin
    raise exception '%.% is append-only (% not allowed)', tg_table_schema, tg_table_name, tg_op
        using errcode = 'P0001';
end $$;

create function ops.touch_updated_at() returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end $$;

-- ---------------------------------------------------------------------------
-- ops: runs first, since every other table can reference a run
-- ---------------------------------------------------------------------------

create table ops.pipeline_runs (
    run_id        uuid primary key default gen_random_uuid(),
    job           text not null check (job in ('import', 'enrich', 'promote', 'research', 'score',
                                               'draft', 'reply_sync', 'booking', 'migration',
                                               'stuck_report')),
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null default 'running'
                  check (status in ('running', 'succeeded', 'failed', 'partial')),
    rows_in       integer not null default 0 check (rows_in >= 0),
    rows_ok       integer not null default 0 check (rows_ok >= 0),
    rows_failed   integer not null default 0 check (rows_failed >= 0),
    error_summary text,
    details       jsonb not null default '{}'
);

-- ---------------------------------------------------------------------------
-- raw: every input exactly as received. Append-only.
-- ---------------------------------------------------------------------------

create table raw.files (
    file_id       uuid primary key default gen_random_uuid(),
    source        text not null check (source in ('grok', 'linkedin')),
    segment       text check (segment in ('founders', 'VPs')),
    original_path text not null,
    sha256        text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes    bigint not null check (size_bytes >= 0),
    storage_path  text,
    received_at   timestamptz not null default now(),
    run_id        uuid references ops.pipeline_runs,
    unique (sha256, source, segment)
);

-- The 13 Grok columns exactly as the bot writes them, all text, no cleaning.
create table raw.grok_rows (
    raw_row_id        bigint generated always as identity primary key,
    file_id           uuid not null references raw.files,
    row_number        integer not null check (row_number >= 1),
    segment           text check (segment in ('founders', 'VPs')),
    name              text,
    title             text,
    company           text,
    company_website   text,
    country           text,
    city              text,
    linkedin_url      text,
    x_handle          text,
    source_url        text,
    signal            text,
    signal_date       text,
    why_fit           text,
    outreach_angle    text,
    extra_columns     jsonb not null default '{}',
    ingested_at       timestamptz not null default now(),
    validation_status text not null check (validation_status in ('valid', 'invalid', 'review')),
    validation_errors jsonb not null default '[]',
    unique (file_id, row_number)
);

-- LinkedIn exports have their own headers; keep the row as received.
create table raw.linkedin_rows (
    raw_row_id        bigint generated always as identity primary key,
    file_id           uuid not null references raw.files,
    row_number        integer not null check (row_number >= 1),
    data              jsonb not null,
    ingested_at       timestamptz not null default now(),
    validation_status text not null check (validation_status in ('valid', 'invalid', 'review')),
    validation_errors jsonb not null default '[]',
    unique (file_id, row_number)
);

-- ---------------------------------------------------------------------------
-- core: identity registry and data that exists before a person is golden
-- ---------------------------------------------------------------------------

create table core.prospect_registry (
    prospect_id      uuid primary key default gen_random_uuid(),
    layer            text not null default 'staging'
                     check (layer in ('staging', 'silver', 'golden')),
    -- Identity keys mirrored from whichever layer table holds the person, so they are
    -- unique across all three layers.
    email            text unique check (email = lower(email)),
    linkedin_url     text unique,
    first_raw_row_id bigint references raw.grok_rows,
    created_at       timestamptz not null default now(),
    promoted_at      timestamptz,
    unique (prospect_id, layer)
);

create table core.companies (
    company_id                uuid primary key default gen_random_uuid(),
    name                      text not null,
    domain                    text unique check (domain = lower(domain)),
    website                   text,
    linkedin_company_url      text,
    industry                  text,
    sub_vertical              text,
    business_model            text,
    employee_count            integer check (employee_count >= 0),
    employee_range            text,
    funding_stage             text,
    total_funding_usd         numeric check (total_funding_usd >= 0),
    last_funding_date         date,
    last_funding_amount_usd   numeric check (last_funding_amount_usd >= 0),
    investors                 text[],
    hq_country                text,
    hq_city                   text,
    crm_tool                  text,
    marketing_automation_tool text,
    sales_engagement_tool     text,
    data_tools                text[],
    hiring_gtm_roles          text[],
    open_gtm_roles_count      integer check (open_gtm_roles_count >= 0),
    careers_url               text,
    icp_fit                   text check (icp_fit in ('strong', 'moderate', 'weak')),
    account_tier              text check (account_tier in ('tier_1', 'tier_2', 'tier_3')),
    account_score             numeric check (account_score between 0 and 100),
    lifecycle_stage           text not null default 'target'
                              check (lifecycle_stage in ('target', 'engaged', 'opportunity',
                                                         'customer', 'lost', 'nurture')),
    owner                     text,
    original_source           text,
    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now()
);

create trigger companies_updated_at before update on core.companies
    for each row execute function ops.touch_updated_at();

-- ---------------------------------------------------------------------------
-- staging / silver / golden prospects: one identical column set.
-- The 13 Grok columns come first, with the same names and order.
-- ---------------------------------------------------------------------------

do $$
declare
    lyr text;
    layer_rule text;
begin
    foreach lyr in array array['staging', 'silver', 'golden'] loop
        layer_rule := case lyr
            -- Neither a verified email nor a LinkedIn URL yet.
            when 'staging' then
                'email_status not in (''verified'', ''bounced'') and linkedin_url is null'
            -- Exactly one of the two.
            when 'silver' then
                '(email_status = ''verified'') <> (linkedin_url is not null)'
                ' and email_status <> ''bounced'''
            -- Both. A later bounce keeps the person golden but blocks email sends.
            when 'golden' then
                'email is not null and email_status in (''verified'', ''bounced'')'
                ' and linkedin_url is not null'
        end;

        execute format($t$
            create table %1$I.prospects (
                prospect_id         uuid primary key,
                layer               text not null default %2$L check (layer = %2$L),
                -- Grok contract
                name                text not null check (length(name) between 2 and 200),
                title               text not null check (length(title) between 1 and 200),
                company             text not null check (length(company) between 1 and 200),
                company_website     text check (company_website ~ '^https?://'),
                country             text check (length(country) <= 100),
                city                text check (length(city) <= 100),
                linkedin_url        text check (linkedin_url ~ '^https://www\.linkedin\.com/in/[a-z0-9_%%-]+$'),
                x_handle            text check (x_handle ~ '^@[A-Za-z0-9_]{1,15}$'),
                source_url          text check (source_url ~ '^https?://'),
                signal              text check (length(signal) <= 1000),
                signal_date         text check (signal_date ~ '^\d{4}(-\d{2}(-\d{2})?)?$'),
                why_fit             text check (length(why_fit) <= 1000),
                outreach_angle      text check (length(outreach_angle) <= 1000),
                -- Added by enrichment, scoring and CRM work
                company_id          uuid references core.companies,
                segment             text[] not null default '{}'
                                    check (segment <@ array['founders', 'VPs']),
                email               text check (email = lower(email) and email like '%%_@_%%'),
                email_status        text not null default 'unknown'
                                    check (email_status in ('verified', 'risky', 'invalid',
                                                            'bounced', 'unknown')),
                email_source        text,
                domain              text check (domain = lower(domain)),
                seniority           text check (seniority in ('founder', 'c_suite', 'vp', 'head')),
                function            text check (function in ('founder', 'marketing', 'content',
                                                             'sales', 'revenue')),
                timezone            text,
                lifecycle_stage     text not null default 'scraped'
                                    check (lifecycle_stage in (
                                        'scraped', 'researched', 'enriched', 'silver_waiting',
                                        'needs_manual', 'golden', 'ready', 'in_sequence',
                                        'replied', 'unresponsive', 'meeting_booked',
                                        'deal_open', 'won', 'closed_lost', 'nurture')),
                lead_status         text not null default 'open'
                                    check (lead_status in ('open', 'in_sequence', 'replied',
                                                           'unresponsive', 'bad_fit')),
                connection_status   text not null default 'none'
                                    check (connection_status in ('none', 'requested', 'connected')),
                reply_status        text not null default 'none'
                                    check (reply_status in ('none', 'positive', 'neutral',
                                                            'negative', 'ooo')),
                touches_count       integer not null default 0 check (touches_count >= 0),
                last_contacted_at   timestamptz,
                last_replied_at     timestamptz,
                next_followup_at    timestamptz,
                lead_score          numeric check (lead_score between 0 and 100),
                tier                text check (tier in ('tier_1', 'tier_2', 'tier_3')),
                score_model_version text,
                consulting_stage    text,
                job_stage           text,
                do_not_contact      boolean not null default false,
                unsubscribed_at     timestamptz,
                bounce_count        integer not null default 0 check (bounce_count >= 0),
                owner               text,
                notes               text,
                original_source     text,
                created_at          timestamptz not null default now(),
                updated_at          timestamptz not null default now(),
                constraint email_needs_address check (email is not null or email_status = 'unknown'),
                constraint layer_requirements check (%3$s),
                -- The row may only live in the layer the registry says. Deferred so a
                -- promotion can move the row and update the registry in one transaction.
                constraint in_registered_layer foreign key (prospect_id, layer)
                    references core.prospect_registry (prospect_id, layer)
                    on update restrict on delete restrict
                    deferrable initially deferred
            )$t$, lyr, lyr, layer_rule);

        execute format('create index on %I.prospects (company_id)', lyr);
        execute format('create index on %I.prospects (lifecycle_stage)', lyr);
        execute format(
            'create trigger prospects_updated_at before update on %I.prospects'
            ' for each row execute function ops.touch_updated_at()', lyr);
    end loop;
end $$;

-- Mirror identity keys into the registry so email and LinkedIn URL are unique across
-- all three layers (the registry's unique constraints do the enforcing).
create function core.sync_identity() returns trigger language plpgsql as $$
begin
    update core.prospect_registry
       set email = new.email, linkedin_url = new.linkedin_url
     where prospect_id = new.prospect_id
       and (email is distinct from new.email or linkedin_url is distinct from new.linkedin_url);
    return new;
end $$;

-- lifecycle_stage may change only through ops.transition(), which records the change.
create function core.guard_lifecycle() returns trigger language plpgsql as $$
begin
    if new.lifecycle_stage is distinct from old.lifecycle_stage
       and coalesce(current_setting('gtm.in_transition', true), '') <> 'on' then
        raise exception 'lifecycle_stage changes must go through ops.transition()'
            using errcode = 'P0001';
    end if;
    return new;
end $$;

do $$
declare lyr text;
begin
    foreach lyr in array array['staging', 'silver', 'golden'] loop
        execute format(
            'create trigger prospects_identity after insert or update of email, linkedin_url'
            ' on %I.prospects for each row execute function core.sync_identity()', lyr);
        execute format(
            'create trigger prospects_lifecycle before update of lifecycle_stage'
            ' on %I.prospects for each row execute function core.guard_lifecycle()', lyr);
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- core: signals, enrichment, research, scores (attach to the registry)
-- ---------------------------------------------------------------------------

create table raw.provider_responses (
    response_id uuid primary key default gen_random_uuid(),
    provider    text not null,
    prospect_id uuid references core.prospect_registry,
    company_id  uuid references core.companies,
    request     jsonb not null,
    response    jsonb not null,
    run_id      uuid references ops.pipeline_runs,
    received_at timestamptz not null default now()
);

create table core.signals (
    signal_id          uuid primary key default gen_random_uuid(),
    prospect_id        uuid references core.prospect_registry,
    company_id         uuid references core.companies,
    raw_row_id         bigint references raw.grok_rows,
    -- Grok names
    signal             text not null check (length(signal) <= 1000),
    signal_date        text check (signal_date ~ '^\d{4}(-\d{2}(-\d{2})?)?$'),
    source_url         text check (source_url ~ '^https?://'),
    why_fit            text check (length(why_fit) <= 1000),
    outreach_angle     text check (length(outreach_angle) <= 1000),
    signal_type        text not null default 'other'
                       check (signal_type in ('funding', 'new_role', 'job_post', 'launch',
                                              'pain_post', 'expansion', 'other')),
    source             text not null check (source in ('grok', 'linkedin', 'job_board', 'manual')),
    strength           text check (strength in ('strong', 'moderate', 'weak')),
    first_seen_at      timestamptz not null default now(),
    expires_at         timestamptz,
    used_in_message_id uuid,
    check (prospect_id is not null or company_id is not null)
);
create index on core.signals (prospect_id);
create index on core.signals (company_id);

-- Signals are append-only, except recording which message used them.
create function core.signals_append_only() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE'
       or (to_jsonb(new) - 'used_in_message_id') <> (to_jsonb(old) - 'used_in_message_id') then
        raise exception 'core.signals is append-only (only used_in_message_id may change)'
            using errcode = 'P0001';
    end if;
    return new;
end $$;
create trigger signals_append_only before update or delete on core.signals
    for each row execute function core.signals_append_only();

create table core.enrichment (
    enrichment_id uuid primary key default gen_random_uuid(),
    prospect_id   uuid references core.prospect_registry,
    company_id    uuid references core.companies,
    field         text not null,
    value         text not null,
    provider      text not null,
    confidence    numeric check (confidence between 0 and 1),
    found_at      timestamptz not null default now(),
    response_id   uuid references raw.provider_responses,
    run_id        uuid references ops.pipeline_runs,
    check ((prospect_id is null) <> (company_id is null))
);
create index on core.enrichment (prospect_id);

create table ops.ai_outputs (
    ai_output_id   uuid primary key default gen_random_uuid(),
    task           text not null check (task in ('research', 'angle', 'draft', 'classify_reply',
                                                 'score')),
    entity_type    text not null check (entity_type in ('prospect', 'company', 'message')),
    entity_id      uuid not null,
    prospect_id    uuid references core.prospect_registry,
    company_id     uuid references core.companies,
    model          text not null,
    prompt_version text not null,
    input_hash     text not null,
    output         jsonb not null,
    sources        text[] not null default '{}',
    run_id         uuid references ops.pipeline_runs,
    created_at     timestamptz not null default now()
);
create index on ops.ai_outputs (prospect_id);

create table core.person_research (
    research_id           uuid primary key default gen_random_uuid(),
    prospect_id           uuid not null references core.prospect_registry,
    researched_at         timestamptz not null default now(),
    recent_posts          jsonb not null default '[]',
    talks_podcasts        jsonb not null default '[]',
    role_history          jsonb not null default '[]',
    stated_priorities     jsonb not null default '[]',
    personalization_hooks jsonb not null default '[]',
    sources               text[] not null check (cardinality(sources) > 0),
    confidence            numeric check (confidence between 0 and 1),
    ai_output_id          uuid references ops.ai_outputs
);
create index on core.person_research (prospect_id);

create table core.account_research (
    research_id   uuid primary key default gen_random_uuid(),
    company_id    uuid not null references core.companies,
    researched_at timestamptz not null default now(),
    findings      jsonb not null,
    sources       text[] not null default '{}',
    status        text not null,
    profile_hash  text,
    ai_output_id  uuid references ops.ai_outputs
);
create index on core.account_research (company_id);

create table core.score_snapshots (
    snapshot_id      uuid primary key default gen_random_uuid(),
    prospect_id      uuid not null references core.prospect_registry,
    snapshot_date    date not null default current_date,
    account_fit      numeric,
    lead_fit         numeric,
    intent           numeric,
    engagement       numeric,
    total            numeric check (total between 0 and 100),
    tier             text check (tier in ('tier_1', 'tier_2', 'tier_3')),
    model_version    text not null,
    reasons          jsonb not null default '[]',
    unique (prospect_id, snapshot_date, model_version)
);

-- ---------------------------------------------------------------------------
-- outbound: every row belongs to a golden prospect
-- ---------------------------------------------------------------------------

create table outbound.deals (
    deal_id                uuid primary key default gen_random_uuid(),
    company_id             uuid references core.companies,
    primary_prospect_id    uuid not null references golden.prospects on delete restrict,
    pipeline               text not null check (pipeline in ('consulting', 'job')),
    stage                  text not null,
    engagement_type        text check (engagement_type in ('full_time', 'contract', 'project')),
    role_title             text,
    job_post_url           text,
    amount                 numeric check (amount >= 0),
    currency               text check (currency ~ '^[A-Z]{3}$'),
    rate_type              text check (rate_type in ('salary', 'hourly', 'fixed')),
    expected_close_date    date,
    closed_at              timestamptz,
    lost_reason            text,
    next_step              text,
    created_from_signal_id uuid references core.signals,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create trigger deals_updated_at before update on outbound.deals
    for each row execute function ops.touch_updated_at();

create table outbound.messages (
    message_id          uuid primary key default gen_random_uuid(),
    prospect_id         uuid not null references golden.prospects on delete restrict,
    signal_id           uuid not null references core.signals,
    deal_id             uuid references outbound.deals,
    ai_output_id        uuid references ops.ai_outputs,
    channel             text not null check (channel in ('email', 'linkedin')),
    sequence_name       text,
    step                integer check (step between 1 and 3),
    angle               text,
    template_version    text,
    subject             text,
    body                text not null,
    status              text not null default 'draft'
                        check (status in ('draft', 'approved', 'sent', 'bounced', 'cancelled')),
    approved_at         timestamptz,
    sent_at             timestamptz,
    external_message_id text unique,
    thread_id           text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    check (status not in ('approved', 'sent', 'bounced') or approved_at is not null),
    check (status not in ('sent', 'bounced') or sent_at is not null)
);
create index on outbound.messages (prospect_id);
create trigger messages_updated_at before update on outbound.messages
    for each row execute function ops.touch_updated_at();

alter table core.signals
    add constraint signals_used_in_message_fk
    foreign key (used_in_message_id) references outbound.messages;

create table outbound.events (
    event_id    uuid primary key default gen_random_uuid(),
    prospect_id uuid not null references golden.prospects on delete restrict,
    deal_id     uuid references outbound.deals,
    message_id  uuid references outbound.messages,
    event_type  text not null
                check (event_type in ('connection_requested', 'connection_accepted', 'reply',
                                      'positive_reply', 'meeting_booked', 'meeting_held',
                                      'interview', 'offer', 'hired', 'won', 'lost',
                                      'unsubscribe', 'bounce', 'note')),
    occurred_at timestamptz not null,
    recorded_by text not null check (recorded_by in ('human', 'sync')),
    notes       text,
    created_at  timestamptz not null default now()
);
create index on outbound.events (prospect_id);
create trigger events_append_only before update or delete on outbound.events
    for each row execute function ops.reject_change();

create table outbound.tasks (
    task_id      uuid primary key default gen_random_uuid(),
    prospect_id  uuid not null references golden.prospects on delete restrict,
    deal_id      uuid references outbound.deals,
    type         text not null,
    due_at       timestamptz not null,
    status       text not null default 'open' check (status in ('open', 'done', 'cancelled')),
    completed_at timestamptz,
    rule         text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    check (status <> 'done' or completed_at is not null)
);
create index on outbound.tasks (prospect_id, status);
create trigger tasks_updated_at before update on outbound.tasks
    for each row execute function ops.touch_updated_at();

-- ---------------------------------------------------------------------------
-- ops: transitions, and the only two ways a person moves
-- ---------------------------------------------------------------------------

create table ops.state_transitions (
    transition_id bigint generated always as identity primary key,
    prospect_id   uuid not null references core.prospect_registry,
    from_layer    text check (from_layer in ('staging', 'silver', 'golden')),
    to_layer      text check (to_layer in ('staging', 'silver', 'golden')),
    from_state    text,
    to_state      text,
    at            timestamptz not null default now(),
    run_id        uuid references ops.pipeline_runs,
    actor         text not null check (actor in ('system', 'human')),
    reason        text not null,
    check (from_layer is distinct from to_layer or from_state is distinct from to_state)
);
create index on ops.state_transitions (prospect_id, at);

-- Append-only tables
create trigger files_append_only before update or delete on raw.files
    for each row execute function ops.reject_change();
create trigger grok_rows_append_only before update or delete on raw.grok_rows
    for each row execute function ops.reject_change();
create trigger linkedin_rows_append_only before update or delete on raw.linkedin_rows
    for each row execute function ops.reject_change();
create trigger provider_responses_append_only before update or delete on raw.provider_responses
    for each row execute function ops.reject_change();
create trigger state_transitions_append_only before update or delete on ops.state_transitions
    for each row execute function ops.reject_change();
create trigger ai_outputs_append_only before update or delete on ops.ai_outputs
    for each row execute function ops.reject_change();

-- Which layer a prospect row belongs in, from its contact data alone.
create function ops.layer_for(p_row jsonb) returns text language sql immutable as $$
    select case
        when has_email and has_linkedin then 'golden'
        when has_email or has_linkedin then 'silver'
        else 'staging'
    end
    from (select p_row->>'email' is not null and p_row->>'email_status' = 'verified' as has_email,
                 p_row->>'linkedin_url' is not null as has_linkedin) s
$$;

-- Comma-separated columns of a layer's prospects table that p_row supplies. Unknown
-- keys are refused rather than silently dropped.
create function ops.prospect_columns(p_layer text, p_row jsonb) returns text
language plpgsql stable as $$
declare
    unknown text;
    cols text;
begin
    select string_agg(k, ', ') into unknown
      from jsonb_object_keys(p_row) k
     where not exists (select from pg_attribute
                        where attrelid = format('%I.prospects', p_layer)::regclass
                          and attname = k and attnum > 0 and not attisdropped);
    if unknown is not null then
        raise exception 'unknown prospect columns: %', unknown using errcode = 'P0001';
    end if;
    select string_agg(quote_ident(attname), ', ' order by attnum) into cols
      from pg_attribute
     where attrelid = format('%I.prospects', p_layer)::regclass
       and attnum > 0 and not attisdropped and p_row ? attname::text;
    return cols;
end $$;

-- Register a new person in the layer their data qualifies for, and record it.
create function ops.register_prospect(
    p_row jsonb, p_run uuid, p_actor text, p_reason text, p_raw_row_id bigint default null
) returns uuid language plpgsql as $$
declare
    pid uuid := coalesce((p_row->>'prospect_id')::uuid, gen_random_uuid());
    target text := ops.layer_for(p_row);
begin
    if p_row ? 'lifecycle_stage' and p_row->>'lifecycle_stage' <> 'scraped' then
        raise exception 'new prospects start as scraped; use ops.transition() afterwards'
            using errcode = 'P0001';
    end if;
    insert into core.prospect_registry (prospect_id, layer, first_raw_row_id)
    values (pid, target, p_raw_row_id);
    p_row := p_row || jsonb_build_object('prospect_id', pid, 'layer', target);
    execute format(
        'insert into %1$I.prospects (%2$s) select %2$s'
        ' from jsonb_populate_record(null::%1$I.prospects, $1)',
        target, ops.prospect_columns(target, p_row))
      using p_row;
    insert into ops.state_transitions (prospect_id, from_layer, to_layer, from_state, to_state,
                                       run_id, actor, reason)
    values (pid, null, target, null, 'scraped', p_run, p_actor, p_reason);
    return pid;
end $$;

-- Apply changes to a prospect (for example, enrichment found a LinkedIn URL) and move
-- them up to the layer the result qualifies for, in one transaction. Never moves down:
-- a golden person stays golden (a bounce is recorded in email_status instead), and a
-- change that would drop someone a layer is refused. Returns the resulting layer.
create function ops.update_prospect(
    p_prospect uuid, p_changes jsonb, p_run uuid, p_actor text, p_reason text
) returns text language plpgsql as $$
declare
    cur text;
    target text;
    merged jsonb;
    cols text;
    rank_of constant jsonb := '{"staging": 0, "silver": 1, "golden": 2}';
begin
    if p_changes ?| array['prospect_id', 'layer', 'lifecycle_stage', 'created_at'] then
        raise exception 'prospect_id, layer, lifecycle_stage and created_at cannot be set here'
            using errcode = 'P0001';
    end if;
    select layer into cur from core.prospect_registry
     where prospect_id = p_prospect for update;
    if cur is null then
        raise exception 'unknown prospect %', p_prospect using errcode = 'P0002';
    end if;
    perform ops.prospect_columns(cur, p_changes);
    execute format('select to_jsonb(p) from %I.prospects p where prospect_id = $1', cur)
       into merged using p_prospect;
    merged := merged || p_changes;
    target := case when cur = 'golden' then 'golden' else ops.layer_for(merged) end;
    if (rank_of->>target)::int < (rank_of->>cur)::int then
        raise exception 'change would move prospect % down from % to %', p_prospect, cur, target
            using errcode = 'P0001';
    end if;

    if target = cur then
        select string_agg(quote_ident(attname), ', ' order by attnum) into cols
          from pg_attribute
         where attrelid = format('%I.prospects', cur)::regclass and attnum > 0
           and not attisdropped and attname <> 'prospect_id';
        execute format(
            'update %1$I.prospects t set (%2$s) = (select %2$s from jsonb_populate_record(t, $2))'
            ' where prospect_id = $1', cur, cols)
          using p_prospect, p_changes;
        return cur;
    end if;

    execute format('delete from %I.prospects where prospect_id = $1', cur) using p_prospect;
    update core.prospect_registry set layer = target, promoted_at = now()
     where prospect_id = p_prospect;
    execute format(
        'insert into %I.prospects select * from jsonb_populate_record(null::%I.prospects, $1)',
        target, target)
      using merged || jsonb_build_object('layer', target, 'updated_at', now());
    insert into ops.state_transitions (prospect_id, from_layer, to_layer, from_state, to_state,
                                       run_id, actor, reason)
    values (p_prospect, cur, target, merged->>'lifecycle_stage', merged->>'lifecycle_stage',
            p_run, p_actor, p_reason);
    return target;
end $$;

-- Change a person's lifecycle stage and record it.
create function ops.transition(
    p_prospect uuid, p_to_state text, p_run uuid, p_actor text, p_reason text
) returns void language plpgsql as $$
declare
    cur_layer text;
    cur_state text;
begin
    select layer into cur_layer from core.prospect_registry
     where prospect_id = p_prospect for update;
    if cur_layer is null then
        raise exception 'unknown prospect %', p_prospect using errcode = 'P0002';
    end if;
    execute format('select lifecycle_stage from %I.prospects where prospect_id = $1', cur_layer)
       into cur_state using p_prospect;
    if cur_state = p_to_state then
        return;
    end if;
    perform set_config('gtm.in_transition', 'on', true);
    execute format('update %I.prospects set lifecycle_stage = $2 where prospect_id = $1',
                   cur_layer) using p_prospect, p_to_state;
    perform set_config('gtm.in_transition', 'off', true);
    insert into ops.state_transitions (prospect_id, from_layer, to_layer, from_state, to_state,
                                       run_id, actor, reason)
    values (p_prospect, cur_layer, cur_layer, cur_state, p_to_state, p_run, p_actor, p_reason);
end $$;

-- One view across layers for reporting and the stuck report.
create view core.all_prospects as
    select * from staging.prospects
    union all select * from silver.prospects
    union all select * from golden.prospects;
