"""Signal-based outbound: pick who to email today, store the drafts, record what was sent.

Nothing here sends anything. The flow is:

1. ``pick``: the best ready people (fresh signal, verified email, never messaged), split
   between founders and VPs, one person per company. Each comes with the signal the email
   must cite and an offer chosen from that signal: companies hiring for GTM engineering
   work get the *role* offer, everyone else (funding, launch, new leader) the *project* one.
2. ``save_drafts``: drafts written from those records are checked against the house style
   and stored in ``outbound.messages`` as ``draft``.
3. The drafts are placed in the human's Gmail as drafts (``link_draft`` keeps the Gmail
   thread). The human reads each one and presses send.
4. ``mark_sent``: only after a send is confirmed in Gmail. The message becomes ``sent``,
   the signal is marked used, the person moves to ``in_sequence`` and a follow-up task
   is created. A draft never counts as contact.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from .icp import OLD_EMPLOYER_LIST, email_matches_company

SEQUENCE = "signal-v1"
FOLLOW_UP_AFTER = timedelta(days=4)

# A company hiring for the work itself gets the role offer.
ROLE_WORDS = ("gtm engineer", "growth engineer", "revops", "rev ops", "revenue operations",
              "marketing ops", "marketing operations", "sales ops", "sales operations",
              "gtm ops", "go-to-market engineer", "automation engineer")  # fmt: skip
HIRING_WORDS = ("hiring", "hire", "job post", "open role", "opening", "posted", "recruiting")

BANNED = ("unlock", "leverage", "game-changer", "game changer", "dive into", "synergy")


def offer_for(signal: str | None) -> str:
    text = (signal or "").lower()
    if any(w in text for w in ROLE_WORDS) and any(w in text for w in HIRING_WORDS):
        return "role"
    return "project"


PICK_SQL = """  -- {audience} is filled from AUDIENCE, never from input
with latest as (
    select p.*, s.tier as score_tier, s.total as score_total,
           sig.signal_id, sig.signal as sig_text, sig.signal_date as sig_date,
           sig.source_url as sig_source, sig.why_fit as sig_why, sig.outreach_angle as sig_angle
    from golden.prospects p
    join lateral (select tier, total from core.score_snapshots s
                  where s.prospect_id = p.prospect_id and s.model_version = %(version)s
                  order by snapshot_date desc limit 1) s on true
    join lateral (select * from core.signals g
                  where g.prospect_id = p.prospect_id and g.used_in_message_id is null
                  order by g.signal_date desc nulls last, g.first_seen_at desc limit 1) sig on true
    where {audience} and p.lead_status = 'open' and not p.do_not_contact
      and p.email_status = 'verified' and p.unsubscribed_at is null
      and not exists (select 1 from outbound.messages m
                      where m.prospect_id = p.prospect_id and m.status <> 'cancelled')
      and not exists (select 1 from outbound.messages m join golden.prospects o using (prospect_id)
                      where lower(o.company) = lower(p.company) and m.status <> 'cancelled')
)
select * from latest
order by score_tier, score_total desc, sig_date desc nulls last, prospect_id
"""


# Who each campaign may pick from. The main campaign takes the ready list; a named list
# (kept current by the score step) feeds its own campaign.
AUDIENCE = {
    None: "p.lifecycle_stage = 'ready'",
    OLD_EMPLOYER_LIST: (
        "p.lifecycle_stage = 'golden' and exists (select 1 from outbound.list_members l"
        f" where l.list_name = '{OLD_EMPLOYER_LIST}' and l.prospect_id = p.prospect_id"
        " and l.removed_at is null)"
    ),
}


def pick(conn: psycopg.Connection, version: str, per_segment: int = 25,
         list_name: str | None = None) -> dict:  # fmt: skip
    """Up to ``per_segment`` founders and ``per_segment`` VPs, best first, one per company.
    Main campaign: ready people; anyone whose email domain does not look like their company
    is held (the score step already keeps them out of ready). ``list_name``: that list's
    members instead, without the domain check, since the list exists because of it."""
    if list_name not in AUDIENCE:
        raise ValueError(f"unknown list {list_name!r}; lists: {[k for k in AUDIENCE if k]}")
    cur = conn.execute(PICK_SQL.format(audience=AUDIENCE[list_name]), {"version": version})
    cols = [c.name for c in cur.description]
    taken: dict[str, list[dict]] = {"founders": [], "VPs": []}
    companies: set[str] = set()
    held: list[dict] = []
    for row in cur.fetchall():
        p = dict(zip(cols, row, strict=True))
        seg = next((s for s in (p["segment"] or []) if s in taken), None)
        company = (p["company"] or "").strip().lower()
        if not seg or not company or company in companies or len(taken[seg]) >= per_segment:
            continue
        if list_name is None and not email_matches_company(p["email"], p["company"]):
            held.append({"name": p["name"], "company": p["company"], "email": p["email"],
                         "reason": "email domain does not match the company"})  # fmt: skip
            continue
        companies.add(company)
        taken[seg].append({
            "prospect_id": str(p["prospect_id"]), "segment": seg, "name": p["name"],
            "first_name": (p["name"] or "").split()[0], "title": p["title"],
            "company": p["company"], "email": p["email"], "tier": p["score_tier"],
            "score": p["score_total"], "signal_id": str(p["signal_id"]),
            "signal": p["sig_text"] or p["signal"],
            "signal_date": str(p["sig_date"] or p["signal_date"]),
            "source_url": p["sig_source"] or p["source_url"],
            "why_fit": p["sig_why"] or p["why_fit"],
            "outreach_angle": p["sig_angle"] or p["outreach_angle"],
            "offer": offer_for(p["sig_text"] or p["signal"]),
            "list": list_name or "ready",
        })  # fmt: skip
    return {"picked": taken["founders"] + taken["VPs"], "held": held}


def style_problems(draft: dict) -> list[str]:
    """House style: no em dashes, banned words, 'Most people' openers; short; names the
    company; carries an opt-out."""
    subject, body = draft.get("subject") or "", draft.get("body") or ""
    text = f"{subject}\n{body}"
    problems = []
    if "—" in text or "–" in text:
        problems.append("em/en dash")
    problems += [f"banned word: {w}" for w in BANNED if w in text.lower()]
    if re.search(r"(^|[.!?]\s+)most people", text, re.IGNORECASE):
        problems.append("sentence starts with 'Most people'")
    if not subject or len(subject) > 60:
        problems.append("subject missing or over 60 characters")
    words = len(body.split())
    if not 40 <= words <= 160:
        problems.append(f"body is {words} words (40-160)")
    if draft.get("company") and draft["company"].lower() not in body.lower():
        problems.append("body does not name the company")
    if "[" in text or "]" in text:
        problems.append("unfilled placeholder")
    if "not relevant" not in body.lower() and "won't email" not in body.lower():
        problems.append("no opt-out line")
    return problems


def save_drafts(conn: psycopg.Connection, drafts: list[dict]) -> dict:
    """Store drafts that pass the style check; skip people who already have a message."""
    saved, rejected, skipped = [], [], []
    for d in drafts:
        problems = style_problems(d)
        if problems:
            rejected.append({"prospect_id": d.get("prospect_id"), "problems": problems})
            continue
        with conn.transaction():
            exists = conn.execute(
                "select 1 from outbound.messages where prospect_id = %s and status <> 'cancelled'",
                (d["prospect_id"],),
            ).fetchone()
            if exists:
                skipped.append(d["prospect_id"])
                continue
            mid = conn.execute(
                "insert into outbound.messages (prospect_id, signal_id, channel, sequence_name,"
                " step, angle, template_version, subject, body)"
                " values (%s, %s, 'email', %s, 1, %s, %s, %s, %s) returning message_id",
                (d["prospect_id"], d["signal_id"], SEQUENCE, d.get("offer"), SEQUENCE,
                 d["subject"], d["body"]),
            ).fetchone()[0]  # fmt: skip
        saved.append({"prospect_id": d["prospect_id"], "message_id": str(mid)})
    return {"saved": saved, "rejected": rejected, "skipped_existing": skipped}


def link_draft(conn: psycopg.Connection, message_id: str, thread_id: str) -> None:
    """Remember the Gmail thread a draft lives in, so a send can be confirmed later."""
    conn.execute("update outbound.messages set thread_id = %s where message_id = %s"
                 " and status = 'draft'", (thread_id, message_id))  # fmt: skip


def mark_sent(conn: psycopg.Connection, message_id: str, *, sent_at: datetime | None = None,
              external_id: str | None = None) -> dict:  # fmt: skip
    """Record a confirmed send. Pressing send in Gmail is the human's approval."""
    sent_at = sent_at or datetime.now(timezone.utc)
    with conn.transaction():
        row = conn.execute(
            "update outbound.messages set status = 'sent', approved_at = coalesce(approved_at, %s),"
            " sent_at = %s, external_message_id = coalesce(%s, external_message_id)"
            " where message_id = %s and status in ('draft', 'approved')"
            " returning prospect_id, signal_id",
            (sent_at, sent_at, external_id, message_id),
        ).fetchone()
        if not row:
            return {"message_id": message_id, "changed": False}
        prospect_id, signal_id = row
        conn.execute("update core.signals set used_in_message_id = %s where signal_id = %s",
                     (message_id, signal_id))  # fmt: skip
        touches = conn.execute("select touches_count from golden.prospects where prospect_id = %s",
                               (prospect_id,)).fetchone()[0]  # fmt: skip
        changes = {"lead_status": "in_sequence", "touches_count": touches + 1,
                   "last_contacted_at": sent_at.isoformat(),
                   "next_followup_at": (sent_at + FOLLOW_UP_AFTER).isoformat()}  # fmt: skip
        conn.execute("select ops.update_prospect(%s, %s, null, 'human', 'email sent')",
                     (prospect_id, Jsonb(changes)))  # fmt: skip
        conn.execute("select ops.transition(%s, 'in_sequence', null, 'human', 'email sent')",
                     (prospect_id,))  # fmt: skip
        conn.execute(
            "insert into outbound.tasks (prospect_id, type, due_at, rule)"
            " values (%s, 'follow_up', %s, %s)",
            (prospect_id, sent_at + FOLLOW_UP_AFTER, f"{SEQUENCE}: no reply after 4 days"),
        )
    return {"message_id": message_id, "changed": True}


def lists(conn: psycopg.Connection) -> list[dict]:
    """Each outbound list: active members, ever removed, and how many were messaged."""
    cur = conn.execute(
        "select l.list_name, count(*) filter (where l.removed_at is null) as active,"
        " count(*) filter (where l.removed_at is not null) as removed,"
        " count(m.message_id) as messaged from outbound.list_members l"
        " left join outbound.messages m on m.prospect_id = l.prospect_id"
        "  and m.status <> 'cancelled' group by 1 order by 1"
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def list_members(conn: psycopg.Connection, name: str) -> list[dict]:
    cur = conn.execute(
        "select p.name, p.title, l.company, l.email, p.linkedin_url, p.signal_date::text,"
        " p.signal, l.added_at::date::text as added from outbound.list_members l"
        " join golden.prospects p using (prospect_id)"
        " where l.list_name = %s and l.removed_at is null order by p.signal_date desc nulls last",
        (name,),
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def drafts(conn: psycopg.Connection, status: str | None = None) -> list[dict]:
    cur = conn.execute(
        "select m.message_id::text, m.status, m.angle as offer, p.name, p.company, p.email,"
        " m.subject, m.thread_id, m.sent_at from outbound.messages m"
        " join golden.prospects p using (prospect_id)"
        " where %s::text is null or m.status = %s order by m.created_at",
        (status, status),
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
