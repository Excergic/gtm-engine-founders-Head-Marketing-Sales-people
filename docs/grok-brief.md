# Grok daily lead brief

Paste this as the standing instruction for the scheduled Grok task. It is the input
contract for `gtm import grok`; keep it versioned with the code.

## Prompt

> Find **25 founders** and **25 GTM leaders** at B2B SaaS companies who show a
> fresh buying or hiring signal for GTM engineering work. Return two CSV files.
>
> **Segment A — founders** (file `founders/YYYY-MM-DD_founders.csv`)
> - Title: Founder, Co-founder, CEO, Co-founder & CEO. Skip CTO-only founders unless
>   they are the only founder.
> - Company: B2B SaaS, roughly seed to Series B, 5–200 employees.
>
> **Segment B — GTM leaders** (file `VPs/YYYY-MM-DD_vps.csv`)
> - Head-level or C-suite in **marketing, content, or sales**. Accept: CMO, CRO,
>   Chief Growth/Content/Commercial Officer, VP or Head of Marketing, Sales, Growth,
>   Demand Generation, Content, Revenue, or GTM.
> - Do **not** include individual contributors, managers below Head level,
>   Directors, account executives/SDRs, or investors (VCs, partners at funds).
> - Aim for a rough mix: about half marketing/content, half sales/revenue.
>
> **Signals (must be within the last 30 days):** new funding, new marketing, content,
> or sales leader hired, job post for GTM Engineer / Growth Engineer / Marketing Ops / RevOps,
> product launch, public post about pipeline, outbound, or tooling problems.
>
> **Required for every row:** `linkedin_url` (the person's /in/ profile, not the
> company page) and `company_website`. If you cannot find both, drop the person and
> find another. Never invent URLs, emails, or handles.
>
> **Exclude** anyone already returned in a previous run.
>
> **Columns, exactly in this order:**
> `name,title,company,company_website,country,city,linkedin_url,x_handle,source_url,signal,signal_date,why_fit,outreach_angle`
>
> - `signal_date`: exact ISO date `YYYY-MM-DD` of the signal event (announcement,
>   post, or job-post date). Month-only or year-only dates get flagged for review.
> - `source_url`: the page that proves the signal.
> - `why_fit`: one sentence on why they need technical GTM help.
> - `outreach_angle`: one sentence, specific to the signal, no generic flattery.

## File rules

- Name files `YYYY-MM-DD_founders.csv` / `YYYY-MM-DD_vps.csv`. If a run returns fewer
  than 25, use the suffix `_partial` (e.g. `2026-11-06_vps_partial.csv`).
- Save into `data/incoming/grok/founders/` or `data/incoming/grok/VPs/` only.
- Write to `NAME.csv.tmp`, then rename to `NAME.csv`, so the scheduler never reads a
  half-written file.
- Re-dropping an identical file is harmless: imports are fingerprinted by content.
