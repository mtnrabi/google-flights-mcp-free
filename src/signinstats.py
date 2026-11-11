"""What `GET /metrics/signins` answers, and nothing else.

Why this exists
---------------
On 2026-09-09 the free server started requiring a Google sign-in on every
call. From that day the funnel had a step nothing could read: the 04:31Z
daily read could see total traffic collapse and could not say whether one
person had signed in or forty, nor whether any of them had ever searched.
The read on 2026-09-11 recorded it plainly -- `/metrics/signins`,
`/metrics/users`, `/metrics/free-users` and `/metrics/signin` all 404.

That gap is not only a reporting nuisance. The day-2 activation note goes to
accounts that signed in and never searched, and until a SECOND system can
confirm "never searched", the note would be sent on one system's say-so --
the store that records the sign-in is the same store that records the usage,
so a dropped best-effort write looks exactly like an inactive user. So the
route reports the store's per-account numbers AND the telemetry totals for
the same window side by side. When they disagree, the day-2 gate is the
thing that must not fire.

What is deliberately NOT here
-----------------------------
Raw email addresses and unsubscribe tokens. This body gets pasted into a
daily read file that is committed to the state repo and sometimes into
Slack, so the address is masked to one character each side of the local
part -- enough to recognise a person you already know, not enough to mail a
stranger. `sequence.mask_email` prints three leading characters because a
dry run of a send list is reviewed by a human deciding whether to send;
this is a number surface that gets archived, and it reveals less on
purpose.

Everything below is pure: records in, JSON-ready dict out. The store read
lives in `freeusers.signin_records`, and the route in `server.py` is the
only thing that knows about tokens and status codes.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

from .freeusers import (
    CAP_KIND_PREFIX,
    DAY2_KIND,
    DAY7_KIND,
    DORMANT_KIND,
    SigninRecord,
    WELCOME_RETRY_MIN_MINUTES,
    window_days,
)

#: The send kinds the daily read counts, in ladder order. `cap` collapses
#: every `cap:<UTC day>` row, and `welcome` is counted off the user row's
#: stamp rather than the ledger, because the sign-in callback sends most of
#: them and writes no ledger row at all.
SENT_KINDS = ("welcome", DAY2_KIND, "cap", DAY7_KIND, DORMANT_KIND)

#: Calls-per-user buckets, in order, as `(label, low, high_inclusive)`.
#: `high` of None means open-ended. 50 is the boundary because it is the
#: free server's daily cap (lowered from 150 on 2026-09-15): an account in
#: the top bucket spent its whole allowance, which is the one segment worth
#: a paid-server conversation.
CALL_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0", 0, 0),
    ("1-9", 1, 9),
    ("10-49", 10, 49),
    ("50+", 50, None),
)

#: Newest sign-ins listed one by one. Twenty is what fits in a daily read
#: without the file becoming a user dump; the totals above it cover the rest.
RECENT_LIMIT = 20


def mask_email(email: str) -> str:
    """`r***d@gmail.com`. Never the address itself.

    One character each end of the local part. A one-character local keeps
    its single character (`r***@…`) rather than being padded into something
    that looks like two, and anything without an `@` -- including the empty
    string a Google account with an unverified address arrives with --
    answers `""`, because inventing a shape for "no address" would make an
    empty row look like a real one.
    """
    address = (email or "").strip()
    if "@" not in address:
        return ""
    local, domain = address.split("@", 1)
    if not local or not domain:
        return ""
    if len(local) == 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def bucket_for(calls: int) -> str:
    """Which `CALL_BUCKETS` label a per-user call count falls in."""
    count = max(0, int(calls or 0))
    for label, low, high in CALL_BUCKETS:
        if count >= low and (high is None or count <= high):
            return label
    return CALL_BUCKETS[-1][0]  # pragma: no cover - the last bucket is open


def _welcome_state(
    record: SigninRecord, now: float, ledger_since: float = 0.0
) -> str:
    """`sent`, `failed`, `unconfirmed` or `too_soon` for one account.

    The distinction this makes is the whole point of the ledger:

    * a ledger id means Resend accepted the note -- `sent`;
    * NO stamp at all, more than ten minutes after the sign-in, means the
      callback never even claimed it -- `failed`, and it is the retry's
      queue. Ten minutes is the same floor `welcome_candidates` uses:
      inside it the callback may still be running;
    * a stamp with no ledger id is a CLAIM with no receipt. After the
      ledger cutover that is a lost send -- `failed`. Before it (or with
      no cutover configured) it is simply a welcome from the days when
      nothing wrote receipts, so it is `unconfirmed` and NOT mailed
      again: re-welcoming somebody who already got one is worse than a
      number we cannot prove.
    """
    if not record.email or record.opted_out:
        return "sent" if record.welcome_sent_at else "too_soon"
    if record.welcome_resend_id:
        return "sent"
    if record.welcome_sent_at:
        if ledger_since and record.welcome_sent_at >= ledger_since:
            return "failed"
        return "unconfirmed"
    if (now - record.first_seen) >= WELCOME_RETRY_MIN_MINUTES * 60.0:
        return "failed"
    return "too_soon"


def sent_by_kind(records: Sequence[SigninRecord]) -> dict[str, int]:
    """ACCOUNTS that got each note, not messages. One per account per kind.

    Counted off the send ledger (`sent_emails.kind`) for everything the
    daily job sends, and off the user row for the two notes that also have
    a column of their own -- the welcome, which the sign-in callback sends
    without a ledger row, and the cap note, whose inline path claims
    `cap_note_sent_at` and never writes to the ledger either. Missing that
    union would report zero cap notes on the day the inline path sent one.
    """
    counts = {kind: 0 for kind in SENT_KINDS}
    for record in records:
        kinds = set(record.kinds_sent)
        if record.welcome_sent_at:
            counts["welcome"] += 1
        if DAY2_KIND in kinds or record.day2_sent_at:
            counts[DAY2_KIND] += 1
        if record.cap_note_sent_at or any(
            kind.startswith(CAP_KIND_PREFIX) for kind in kinds
        ):
            counts["cap"] += 1
        if DAY7_KIND in kinds:
            counts[DAY7_KIND] += 1
        if DORMANT_KIND in kinds:
            counts[DORMANT_KIND] += 1
    return counts


def _stamp(epoch: float | None) -> str | None:
    """`YYYY-MM-DDTHH:MM:SSZ`, or null. Same clock as every other route."""
    if not epoch:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def build_stats(
    records: Sequence[SigninRecord] | Iterable[SigninRecord],
    *,
    hours: int = 24,
    now: float | None = None,
    calls: dict[str, Any] | None = None,
    limit: int | None = None,
    recent_limit: int = RECENT_LIMIT,
    #: `freeusers.welcome_ledger_since()`. 0.0 -- the shipped default --
    #: means a stamp with no receipt is `unconfirmed` rather than lost,
    #: which is what keeps the retry off the welcomes that predate the
    #: ledger. Passed in rather than read here so this stays pure.
    ledger_since: float = 0.0,
) -> dict[str, Any]:
    """The whole response body, from rows the store already fetched.

    `calls` is `telemetry.call_series(hours)` (or None when it could not be
    read): its `totals` are the independent, call-path view of the same
    window, and they are reported as their own keys rather than merged into
    the per-user numbers, because the entire point is that the two can
    disagree.

    `limit` is the row cap the store was given. When exactly that many rows
    came back the answer is marked `truncated`, since every total below is
    then a total over the newest N accounts and not over the list.
    """
    moment = now if now is not None else time.time()
    rows = list(records)
    days = window_days(hours, moment)
    cutoff = moment - max(1, int(hours)) * 3600.0

    users = len(rows)
    with_calls = sum(1 for r in rows if r.calls_total > 0)
    totals = {
        "users": users,
        "opted_out": sum(1 for r in rows if r.opted_out),
        "welcome_sent": sum(1 for r in rows if r.welcome_sent_at),
        "day2_sent": sum(1 for r in rows if r.day2_sent_at),
        "cap_sent": sum(1 for r in rows if r.cap_note_sent_at),
        # All-time, off the user row's running counter: "has this account
        # EVER searched" is the day-2 note's whole eligibility question, and
        # it is not a question about the window.
        "users_with_calls": with_calls,
        "users_zero_calls": users - with_calls,
    }
    totals["sent_by_kind"] = sent_by_kind(rows)
    # The number the 2026-09-17 read had to reconstruct by hand. Three of
    # nine real sign-ins sat in it between 09-13 and 09-16 and nothing
    # reported it. `welcome_failed` is exactly the welcome retry's queue
    # (`free-sequence-run.sh welcome --send`); a value that stays non-zero
    # after a retry run means Resend is refusing those addresses and a
    # human has to look. `welcome_unconfirmed` is the older population we
    # deliberately do NOT mail again -- stamped before the ledger existed,
    # so there is no receipt and no way to tell; see `_welcome_state`.
    states = [_welcome_state(r, moment, ledger_since) for r in rows]
    totals["welcome_failed"] = states.count("failed")
    totals["welcome_unconfirmed"] = states.count("unconfirmed")

    window: dict[str, Any] = {
        "hours": hours,
        # Whole UTC days, because the usage rows are keyed by day. Said out
        # loud so nobody reads a 24 h label as an exact 24 h count.
        "days_read": days,
        "from": _stamp(cutoff),
        "to": _stamp(moment),
        # Exact: a sign-in is a timestamp, not a day row.
        "new_signins": sum(1 for r in rows if r.first_seen >= cutoff),
        "active_users": sum(1 for r in rows if r.calls_window > 0),
        "user_searches": sum(r.calls_window for r in rows),
        "cap_hits": sum(r.cap_hits_window for r in rows),
    }
    call_totals = (calls or {}).get("totals") or {}
    # The second system. Server-wide rather than per account -- telemetry
    # holds no identity -- which is exactly what makes it independent of the
    # rows above.
    window["tool_calls"] = int(call_totals.get("tool_calls") or 0)
    window["backend_calls"] = int(call_totals.get("backend_calls") or 0)
    if calls is None:
        window["calls_source"] = "unavailable"

    distribution = {label: 0 for label, _low, _high in CALL_BUCKETS}
    for row in rows:
        distribution[bucket_for(row.calls_window)] += 1

    recent = [
        {
            "email": mask_email(r.email),
            "client_kind": r.client_kind,
            "first_seen": _stamp(r.first_seen),
            "last_seen": _stamp(r.last_seen),
            "calls_total": r.calls_total,
            "calls_window": r.calls_window,
            "welcome_sent_at": _stamp(r.welcome_sent_at),
            "welcome_state": _welcome_state(r, moment, ledger_since),
            "day2_sent_at": _stamp(r.day2_sent_at),
            "cap_note_sent_at": _stamp(r.cap_note_sent_at),
            "opted_out": r.opted_out,
        }
        for r in sorted(rows, key=lambda r: r.first_seen, reverse=True)[
            : max(0, recent_limit)
        ]
    ]

    body: dict[str, Any] = {
        "hours": hours,
        "totals": totals,
        "window": window,
        "distribution": distribution,
        "recent": recent,
        "notes": [
            "Per-user numbers come from free_mcp_users / free_mcp_user_days, "
            "which are written best-effort after a tool call is answered, so "
            "they are a floor. window.tool_calls and window.backend_calls "
            "come from the call counters instead and are the independent "
            "view -- if a user shows zero calls while those are non-zero, do "
            "not send the day-2 note on this data alone.",
            "distribution and the per-user window numbers cover whole UTC "
            "days (window.days_read of them); new_signins is exact.",
            "totals.sent_by_kind counts ACCOUNTS that received each note, "
            "not messages. totals.welcome_failed is the welcome retry's "
            "queue: no stamp ten minutes after sign-in, or a stamp with no "
            "Resend receipt after FREE_WELCOME_LEDGER_SINCE. "
            "totals.welcome_unconfirmed is stamped-with-no-receipt from "
            "before that cutover -- not retried, because re-welcoming "
            "somebody who already got one is the worse error.",
            "Addresses are masked. The unmasked list is in Neon and nowhere "
            "in this response.",
        ],
    }
    if limit is not None and users >= limit:
        body["truncated"] = True
        body["notes"].append(
            f"Only the {limit} newest accounts were read; every total above "
            "covers that subset, not the whole table."
        )
    return body
