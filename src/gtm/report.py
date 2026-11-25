"""One-page results report: what was sent, what came back, and today's new leads.

Plain SQL in, one self-contained HTML file out. No JavaScript, no LLM, no server.
"""

from __future__ import annotations

import datetime as dt
from html import escape

FUNNEL_SQL = """
select
  (select count(*) from golden.prospects)                                              as tracked,
  (select count(*) from golden.prospects where lifecycle_stage = 'ready')              as ready,
  (select count(*) from outbound.messages where status = 'sent' and channel = 'email') as emails_sent,
  (select count(*) from outbound.events where event_type = 'connection_requested')     as invites_sent,
  (select count(*) from outbound.events where event_type = 'connection_accepted')      as invites_accepted,
  (select count(*) from outbound.events where event_type in ('reply', 'positive_reply', 'unsubscribe'))
                                                                                        as replies,
  (select count(*) from outbound.events where event_type = 'positive_reply')           as positive_replies,
  (select count(*) from outbound.events where event_type = 'meeting_booked')           as meetings,
  (select count(*) from outbound.events where event_type = 'interview')                as interviews,
  (select count(*) from outbound.events where event_type in ('offer', 'won', 'hired')) as wins
"""

DAILY_SQL = """
with days as (
  select generate_series(current_date - %(days)s + 1, current_date, interval '1 day')::date as day
)
select d.day,
  (select count(*) from ops.state_transitions t where t.to_state = 'ready' and t.at::date = d.day) as new_leads,
  (select count(*) from outbound.messages m
     where m.status = 'sent' and m.channel = 'email' and m.sent_at::date = d.day)                 as emails,
  (select count(*) from outbound.events e
     where e.event_type = 'connection_requested' and e.occurred_at::date = d.day)                 as invites,
  (select count(*) from outbound.events e
     where e.event_type in ('reply', 'positive_reply', 'unsubscribe') and e.occurred_at::date = d.day)
                                                                                                    as replies,
  (select count(*) from outbound.events e
     where e.event_type in ('meeting_booked', 'interview') and e.occurred_at::date = d.day)       as meetings
from days d
order by d.day desc
"""

TODAY_LEADS_SQL = """
select p.name, p.title, p.company, p.signal, p.signal_date
from ops.state_transitions t
join golden.prospects p using (prospect_id)
where t.to_state = 'ready' and t.at::date = %(day)s
order by p.lead_score desc nulls last, p.company
limit 50
"""

REPLIES_SQL = """
select e.occurred_at::date as day, e.event_type, p.name, p.title, p.company
from outbound.events e
join golden.prospects p using (prospect_id)
where e.event_type in ('reply', 'positive_reply', 'unsubscribe', 'meeting_booked', 'interview',
                       'offer', 'won', 'hired', 'connection_accepted')
order by e.occurred_at desc
limit 25
"""

EVENT_LABELS = {
    "reply": "Replied",
    "positive_reply": "Positive reply",
    "unsubscribe": "Replied: not interested",
    "meeting_booked": "Meeting booked",
    "interview": "Interview",
    "offer": "Offer",
    "won": "Client won",
    "hired": "Hired",
    "connection_accepted": "Accepted LinkedIn invite",
}


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def collect(conn, today: dt.date, days: int = 14) -> dict:
    """Read everything the page shows. Read-only."""
    return {
        "today": today,
        "funnel": _rows(conn, FUNNEL_SQL)[0],
        "daily": _rows(conn, DAILY_SQL, {"days": days}),
        "today_leads": _rows(conn, TODAY_LEADS_SQL, {"day": today}),
        "replies": _rows(conn, REPLIES_SQL),
    }


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _who(row: dict, show_names: bool) -> str:
    """Person label. Names stay hidden unless asked, since the page is shown to outsiders."""
    role = escape(f"{row.get('title') or ''}, {row.get('company') or ''}".strip(", "))
    if show_names and row.get("name"):
        return f"{escape(row['name'])} <span class='muted'>({role})</span>"
    return role


def render(data: dict, show_names: bool = False) -> str:
    f = data["funnel"]
    touched = f["emails_sent"] + f["invites_sent"]
    cards = [
        ("Leads tracked", f["tracked"], "verified email and LinkedIn"),
        ("Ready to contact", f["ready"], "fresh signal and ICP fit"),
        ("Emails sent", f["emails_sent"], ""),
        ("LinkedIn invites", f["invites_sent"], f"{f['invites_accepted']} accepted"),
        ("Replies", f["replies"], f"{_pct(f['replies'], touched)} of touches"),
        ("Positive replies", f["positive_replies"], ""),
        ("Meetings booked", f["meetings"], ""),
        ("Interviews", f["interviews"], ""),
    ]
    card_html = "".join(
        f"<div class='card'><div class='num'>{v}</div><div class='lbl'>{escape(k)}</div>"
        f"<div class='muted'>{escape(s)}</div></div>"
        for k, v, s in cards
    )

    daily_rows = "".join(
        f"<tr><td>{d['day']:%a %d %b}</td><td>{d['new_leads']}</td><td>{d['emails']}</td>"
        f"<td>{d['invites']}</td><td>{d['replies']}</td><td>{d['meetings']}</td></tr>"
        for d in data["daily"]
    )

    leads = data["today_leads"]
    lead_rows = (
        "".join(
            f"<tr><td>{_who(r, show_names)}</td><td>{escape(r['signal'] or '')}</td>"
            f"<td>{r['signal_date'] or ''}</td></tr>"
            for r in leads
        )
        or "<tr><td colspan='3' class='muted'>No new leads moved to ready today.</td></tr>"
    )

    reply_rows = (
        "".join(
            f"<tr><td>{r['day']:%d %b}</td><td>{escape(EVENT_LABELS.get(r['event_type'], r['event_type']))}</td>"
            f"<td>{_who(r, show_names)}</td></tr>"
            for r in data["replies"]
        )
        or "<tr><td colspan='3' class='muted'>No replies recorded yet.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outbound Results</title>
<style>
:root {{ --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e3dd; --card:#fff; --accent:#1f5f4a; }}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{ --bg:#161615; --fg:#ecebe6; --muted:#9a9993; --line:#2e2e2b; --card:#1f1f1d; --accent:#6fc2a2; }}
}}
:root[data-theme="dark"] {{ --bg:#161615; --fg:#ecebe6; --muted:#9a9993; --line:#2e2e2b; --card:#1f1f1d; --accent:#6fc2a2; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ margin:0 0 4px; font-size:24px; }}
h2 {{ margin:32px 0 8px; font-size:17px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.how {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin:16px 0 0; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:12px; margin-top:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.num {{ font-size:28px; font-weight:650; color:var(--accent); font-variant-numeric:tabular-nums; }}
.lbl {{ font-weight:600; }}
.table {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:13px; color:var(--muted); font-weight:600; }}
td {{ font-variant-numeric:tabular-nums; }}
</style></head>
<body><main>
<h1>Outbound results</h1>
<div class="muted">As of {data["today"]:%d %b %Y}. Every number below is counted from the log, not estimated.</div>
<p class="how"><b>How it works:</b> every day the system finds companies with a buying signal (new funding,
a new sales or marketing leader, a GTM hiring post), checks the person's email and LinkedIn, and keeps
only people who fit and whose signal is under 30 days old. Those are the <i>ready</i> leads. Each message
is written to that signal, approved by a person, then sent. Every send, reply and meeting is logged.</p>
<div class="grid">{card_html}</div>

<h2>Last {len(data["daily"])} days</h2>
<div class="table"><table>
<tr><th>Day</th><th>New leads</th><th>Emails</th><th>LinkedIn invites</th><th>Replies</th><th>Meetings / interviews</th></tr>
{daily_rows}
</table></div>

<h2>Today's new leads ({len(leads)})</h2>
<div class="table"><table>
<tr><th>Who</th><th>Why now (signal)</th><th>Signal date</th></tr>
{lead_rows}
</table></div>

<h2>Recent replies and results</h2>
<div class="table"><table>
<tr><th>Day</th><th>What happened</th><th>Who</th></tr>
{reply_rows}
</table></div>
</main></body></html>
"""
