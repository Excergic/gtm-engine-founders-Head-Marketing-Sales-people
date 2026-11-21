# Signal-based outbound

Nothing in this repo sends email. Drafts go into Gmail and the human presses send.

```sh
uv run gtm outbound pick --per-segment 25   # best ready founders + VPs, one per company
uv run gtm outbound pick --list old-employer-email  # the separate campaign
uv run gtm outbound save drafts.json        # style-checked drafts -> outbound.messages (draft)
uv run gtm outbound link MESSAGE_ID THREAD  # remember the Gmail draft's thread
uv run gtm outbound sent MESSAGE_ID         # only after the send is seen in Gmail
uv run gtm outbound list --status draft
```

**Pick.** Ready people only (golden, verified email, fresh signal, tier 1/2, open, not
DNC), never messaged, ordered by tier, score, then signal date. One person per company.
The offer comes from the signal: a company hiring a GTM engineer / RevOps / growth engineer
gets the *role* offer, everyone else the *project* offer.

**Old-employer emails get their own list.** The enrichment waterfall verifies that an
address delivers, not that it is the current job, so newly hired CROs and CMOs often come
back with their previous employer's address. The ready gate blocks an email whose domain
does not look like the company (Grok leaves `company_website` blank, so the domain is
compared with the company name). Golden people for whom that is the *only* blocker are kept
in `outbound.list_members` as `old-employer-email`, updated on every score run: added
while they qualify, `removed_at` stamped once they don't (a current email found, contacted,
signal stale). Nothing is deleted.

```sh
uv run gtm outbound lists                                   # counts per list
uv run gtm outbound lists old-employer-email                # who is on it
uv run gtm outbound pick --list old-employer-email          # picks for that campaign
```

The MCP server's `campaign_list` tool shows the same list.

**Drafts** must pass `style_problems`: no em/en dashes, no banned words, no sentence
starting "Most people", subject 60 characters or less, 40–160 words, names the company,
no `[placeholders]`, and an opt-out line.

**Sent.** Claude checks Gmail (`in:sent`) for the stored thread ids and runs
`gtm outbound sent` for each one actually sent. That marks the message `sent`, the signal
used, the person `in_sequence` (so the ready gate leaves them alone), and adds a follow-up
task four days later. A draft never counts as contact.
