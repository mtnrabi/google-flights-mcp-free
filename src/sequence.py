"""The two SCHEDULED notes: the day-2 activation note and the cap-day note.

This is the job behind `POST /email/run-sequence`. It is not a new lane --
the copy, the triggers and the suppression rules are
`state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` and
`state/gtm/research/outreach-dm-craft-pass9-2026-09-11.md` §2.2 -- it is the
part that was missing: something that can look at everybody once a day
instead of only at the person who happens to be making a request.

Why a job and not more inline sends
-----------------------------------
The welcome fires from the sign-in callback and the cap note fires from the
background task of a tool call, because both describe something the caller
just did. The day-2 note cannot work that way: it goes to the accounts that
made NO request, so there is no request to hang it off. And a cap note
evaluated only on a capped call is evaluated only while the person is still
hammering a cap, which is the worst moment to write to them.

So: one route, called by the 09:00Z session cron, dry-run by default.

Three properties, and each one is a test
----------------------------------------
1. **Dry run sends nothing and needs no flag.** It is the thing the manager
   runs first and the thing the cron runs when nobody is sure. It returns
   the list it would mail, so "who would get this" is answerable without
   betting an address on the answer.
2. **A flag off is not a send that gets discarded.** `FREE_SIGNIN_DAY2` and
   `FREE_SIGNIN_CAPNOTE` are checked before the first Resend call, and the
   response says so.
3. **The claim comes before the send.** `sent_emails` (migration 003) has a
   unique key on (user_sub, kind) and the claim is an
   `INSERT ... ON CONFLICT DO NOTHING`; two overlapping runs of the job both
   run it and exactly one wins. A REFUSED releases the claim, a timeout
   never does -- Resend may have taken the message and a duplicate is the
   failure that costs a sending domain.

The copy is not written here
----------------------------
`src/email_templates/notes.py` holds the exact approved bytes of both
notes, HTML and text twin, generated from the renders in the api-growth
repo. This module fills five placeholders and nothing else. Substitution is
`str.replace`, never `str.format`: the HTML is full of CSS braces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .email_templates import ladder
from .email_templates.names import first_name_from_email
from .email_templates.notes import (
    DAY2_HTML,
    DAY2_SUBJECT,
    DAY2_TEXT,
)
from .freeusers import (
    CAP_KIND_PREFIX,
    DAY2_KIND,
    DAY2_MIN_HOURS,
    DAY7_KIND,
    DORMANT_KIND,
    SEQUENCE_LANE_DAY_CAP,
    WELCOME_KIND,
    WELCOME_RETRY_KIND,
    SequenceCandidate,
    cap_kind,
    welcome_ledger_since,
)
from .giftfare import gift_fare_bracket
from .maillist import (
    FROM_ADDRESS,
    REFUSED,
    SENT,
    MailConfig,
    post_email,
    record_sent,
    unsubscribe_url,
    welcome_parts,
)

logger = logging.getLogger(__name__)

#: The two kinds the route accepts. `cap` is what a caller asks for; the
#: ledger stores `cap:<UTC day>` -- see `freeusers.cap_kind`.
KIND_DAY2 = "day2"
KIND_CAP = "cap"
#: The two notes added on 2026-09-17 from the upsell read: ten or more
#: searches in the first week and never capped (day-7), and two weeks with
#: one search or none (dormant). Both OFF by default, each behind its own
#: flag, same claim-before-send and same lane ceiling as the first two.
KIND_DAY7 = "day7"
KIND_DORMANT = "dormant14"
#: Not a new note: the WELCOME, re-sent to an account whose first attempt
#: never landed. Three of nine real sign-ins between 09-13 and 09-16 never
#: got one -- the flag was on, the address was there, and nothing retried.
#: See `freeusers.welcome_candidates` for what "never landed" means.
KIND_WELCOME = "welcome"
#: The cap note's retry, on the same rule as the welcome's: a note that
#: was CLAIMED (`cap_note_sent_at`) and has no confirmed ledger row for
#: that day never reached anybody. Bounded to three days and to one retry
#: per lost note.
KIND_CAP_RETRY = "cap-retry"
KINDS = (
    KIND_DAY2,
    KIND_CAP,
    KIND_DAY7,
    KIND_DORMANT,
    KIND_WELCOME,
    KIND_CAP_RETRY,
)

#: A fourth outcome next to maillist's SENT / REFUSED / UNKNOWN: somebody
#: else already holds the claim for this account and kind -- another run of
#: the job, or, for the cap note, the inline path. Not a failure and not a
#: send; it is the guard doing its job, and it is counted separately so a
#: run that mailed nobody says WHY.
ALREADY_CLAIMED = "already_claimed"

#: Which paid listing a note's one button points at. The table itself is
#: `ladder.CONNECT_PRODUCTS`, so the inline path and this job cannot end up
#: pointing at two different URLs.
CAP_PRODUCTS = ladder.CONNECT_PRODUCTS


@dataclass(frozen=True)
class Note:
    """One rendered message, ready for `POST /emails`."""

    subject: str
    text: str
    html: str


def mask_email(email: str) -> str:
    """`ale***@gmail.com`. What a dry run prints.

    The route is behind `METRICS_TOKEN`, so the caller is the manager -- but
    a dry run is pasted into Slack and committed into session notes, and a
    list of addresses is not something that belongs in either. Three
    characters plus the domain is enough to recognise a person you already
    know and not enough to mail a stranger.
    """
    address = (email or "").strip()
    if "@" not in address:
        return ""
    local, domain = address.split("@", 1)
    return f"{local[:3]}***@{domain}"


def _fill(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def cap_product(candidate: SequenceCandidate) -> str:
    """`hotels` or `flights`. Flights whenever we cannot tell.

    Not a coin flip: flights is the larger listing and the one every printed
    guide already points at, so an account with no attributed searches --
    everyone who used the server before `free_mcp_user_tools` existed --
    gets the link that is right more often.
    """
    return "hotels" if candidate.hotel_majority else "flights"


def render_day2(candidate: SequenceCandidate, config: MailConfig) -> Note:
    values = {
        "unsubscribe_url": unsubscribe_url(config, candidate.unsubscribe_token),
        "first_name": first_name_from_email(candidate.email),
    }
    return Note(
        subject=DAY2_SUBJECT,
        text=_fill(DAY2_TEXT, values),
        html=_fill(DAY2_HTML, values),
    )


def render_cap(
    candidate: SequenceCandidate,
    config: MailConfig,
    *,
    gift_fare: str = "",
    day_cap: int = 0,
    month_cap: int = 0,
) -> Note:
    """The cap note, branch D.

    Branch D and not a choice, because this job's candidate filter IS
    branch D: `cap_candidates` selects the accounts the day cap refused
    TODAY. The monthly branch is only ever reached from the inline path,
    which knows the month-to-date signals. Until 2026-09-17 this job
    rendered a fixed text whose first line was "you hit the free daily cap
    twice this week" -- untrue for the first-time capper the filter
    actually selects, which is why the copy now says *today*.
    """
    product = cap_product(candidate)
    url, requests = CAP_PRODUCTS[product]
    parts = ladder.cap_note(
        unsubscribe_url=unsubscribe_url(config, candidate.unsubscribe_token),
        connect_url=url,
        pro_requests=requests,
        basic_requests=ladder.BASIC_REQUESTS[product],
        branch=ladder.BRANCH_DAILY,
        first_name=first_name_from_email(candidate.email),
        # A flight fare in a hotels note is a non sequitur, so the bracket
        # is dropped there -- the upsell file says so explicitly.
        gift_fare=gift_fare if product == "flights" else "",
        day_cap=day_cap,
        month_searches=candidate.month_searches,
        month_cap=month_cap,
    )
    return Note(subject=parts.subject, text=parts.text, html=parts.html)


def render_day7(
    candidate: SequenceCandidate, config: MailConfig, *, day_cap: int = 0
) -> Note:
    parts = ladder.day7_note(
        unsubscribe_url=unsubscribe_url(config, candidate.unsubscribe_token),
        connect_url=CAP_PRODUCTS[cap_product(candidate)][0],
        searches=candidate.first_week_searches,
        first_name=first_name_from_email(candidate.email),
        day_cap=day_cap,
    )
    return Note(subject=parts.subject, text=parts.text, html=parts.html)


def render_dormant(candidate: SequenceCandidate, config: MailConfig) -> Note:
    parts = ladder.dormant_note(
        unsubscribe_url=unsubscribe_url(config, candidate.unsubscribe_token),
        first_name=first_name_from_email(candidate.email),
    )
    return Note(subject=parts.subject, text=parts.text, html=parts.html)


def render_welcome(
    candidate: SequenceCandidate,
    config: MailConfig,
    *,
    day_cap: int = 0,
    month_cap: int = 0,
) -> Note:
    """The welcome note, byte for byte what the sign-in callback sends."""
    subject, text, html = welcome_parts(
        day_cap,
        month_cap,
        unsubscribe_url(config, candidate.unsubscribe_token),
    )
    return Note(subject=subject, text=text, html=html)


def _payload(candidate: SequenceCandidate, note: Note, link: str) -> dict:
    return {
        "from": FROM_ADDRESS,
        "reply_to": FROM_ADDRESS,
        "to": [candidate.email],
        "subject": note.subject,
        "text": note.text,
        "html": note.html,
        # One-click unsubscribe. `{{{RESEND_UNSUBSCRIBE_URL}}}` is a
        # BROADCAST variable and would go out literally here, so both the
        # header and the footer link point at our own route.
        "headers": {
            "List-Unsubscribe": f"<{link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ledger_kind_for(kind: str, moment: float) -> str:
    """The `sent_emails.kind` a send of `kind` claims.

    The kind IS the window, which is what makes "never twice" a database
    constraint rather than a code path somebody has to remember: `day2`,
    `day7`, `dormant14` and `welcome:retry` are once per account ever, and
    the cap note carries its UTC day.
    """
    if kind == KIND_DAY2:
        return DAY2_KIND
    if kind == KIND_DAY7:
        return DAY7_KIND
    if kind == KIND_DORMANT:
        return DORMANT_KIND
    if kind == KIND_WELCOME:
        return WELCOME_RETRY_KIND
    if kind == KIND_CAP_RETRY:
        # A placeholder: every cap-retry candidate carries its OWN ledger
        # kind, naming the day the lost note was claimed on, so one lost
        # note is retried exactly once. See `freeusers.cap_retry_kind`.
        return KIND_CAP_RETRY
    return cap_kind(moment)


def _flag_on(kind: str, config: MailConfig) -> bool:
    """One switch per note, and never a shared one.

    Four sends that can be turned on together will be turned on together
    by accident. The welcome retry rides the welcome's own flag: it is the
    same note, and a deployment that has the welcome off has not asked for
    one to be re-sent either.
    """
    return {
        KIND_DAY2: config.day2,
        KIND_CAP: config.cap_note,
        KIND_DAY7: config.day7,
        KIND_DORMANT: config.dormant,
        KIND_WELCOME: config.welcome,
        KIND_CAP_RETRY: config.cap_note,
    }.get(kind, False)


def render(
    kind: str,
    candidate: SequenceCandidate,
    config: MailConfig,
    *,
    gift_fare: str = "",
    day_cap: int = 0,
    month_cap: int = 0,
) -> Note:
    if kind == KIND_DAY2:
        return render_day2(candidate, config)
    if kind == KIND_DAY7:
        return render_day7(candidate, config, day_cap=day_cap)
    if kind == KIND_DORMANT:
        return render_dormant(candidate, config)
    if kind == KIND_WELCOME:
        return render_welcome(
            candidate, config, day_cap=day_cap, month_cap=month_cap
        )
    # KIND_CAP and KIND_CAP_RETRY are the same note; the retry is a
    # second attempt at a message somebody was already meant to get.
    return render_cap(
        candidate,
        config,
        gift_fare=gift_fare,
        day_cap=day_cap,
        month_cap=month_cap,
    )


async def run_sequence(
    store,
    config: MailConfig,
    *,
    kind: str,
    dry_run: bool = True,
    limit: int = SEQUENCE_LANE_DAY_CAP,
    now: float | None = None,
    min_hours: int = DAY2_MIN_HOURS,
    max_hours: int | None = None,
    #: The live caps, for the copy. Passed in by the route from settings
    #: rather than read here, so a note can never quote a cap this module
    #: guessed (CLAUDE.md rule 1).
    day_cap: int = 0,
    month_cap: int = 0,
    #: The welcome ledger's cutover (`freeusers.welcome_ledger_since`).
    #: None reads the environment, which is what the route does; a caller
    #: that passes a number owns the decision instead.
    ledger_since: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Select, and -- unless this is a dry run -- send. Never raises.

    Returns the response body of `POST /email/run-sequence`: the selection
    either way, and the outcome when something was actually sent.

    `dry_run` defaults to True at every layer that has a default, including
    the route. A job that mails strangers should take an explicit sentence
    to make it mail anybody.
    """
    moment = now if now is not None else time.time()
    limit = max(0, min(int(limit), SEQUENCE_LANE_DAY_CAP))
    body: dict = {
        "kind": kind,
        "dry_run": bool(dry_run),
        "limit": limit,
        "at": _iso(moment),
        "eligible": 0,
        "candidates": [],
        "sent": 0,
        "refused": 0,
        "unknown": 0,
        "already_claimed": 0,
        "resend_ids": [],
        "reason": "",
    }
    if kind not in KINDS:
        body["reason"] = "unknown_kind"
        return body

    try:
        if kind == KIND_DAY2:
            candidates = await store.day2_candidates(
                moment, limit=limit, min_hours=min_hours, max_hours=max_hours
            )
        elif kind == KIND_DAY7:
            candidates = await store.day7_candidates(moment, limit=limit)
        elif kind == KIND_DORMANT:
            candidates = await store.dormant_candidates(moment, limit=limit)
        elif kind == KIND_WELCOME:
            candidates = await store.welcome_candidates(
                moment,
                limit=limit,
                ledger_since=(
                    welcome_ledger_since() if ledger_since is None else ledger_since
                ),
            )
        elif kind == KIND_CAP_RETRY:
            candidates = await store.cap_retry_candidates(moment, limit=limit)
        else:
            candidates = await store.cap_candidates(moment, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a mail job never 500s the server
        logger.warning("could not select %s candidates: %s", kind, exc)
        body["reason"] = "store_unavailable"
        return body

    body["eligible"] = len(candidates)
    body["candidates"] = [
        {
            "sub": candidate.google_sub[:8],
            "email": mask_email(candidate.email),
            "first_seen": _iso(candidate.first_seen),
            "month_searches": candidate.month_searches,
            "first_week_searches": candidate.first_week_searches,
            "product": (
                cap_product(candidate)
                if kind in (KIND_CAP, KIND_CAP_RETRY, KIND_DAY7)
                else ""
            ),
            "ledger_kind": candidate.ledger_kind,
        }
        for candidate in candidates
    ]

    # Every reason NOT to send, decided once, before the loop, so the
    # response says which one it was instead of reporting a silent zero.
    if dry_run:
        body["reason"] = "dry_run"
        return body
    if not config.can_send:
        body["reason"] = "no_api_key"
        return body
    if not _flag_on(kind, config):
        body["reason"] = "flag_off"
        return body

    ledger_kind = ledger_kind_for(kind, moment)
    body["ledger_kind"] = ledger_kind
    # ONE fare lookup per run, not one per recipient: the bracket is the
    # same sentence in every copy of the note, and twenty calls to price
    # the same round trip would be twenty searches billed to our own plan.
    # Empty on any failure, which drops the bracket (CLAUDE.md rule 1).
    wants_fare = kind in (KIND_CAP, KIND_CAP_RETRY)
    gift_fare = await gift_fare_bracket() if wants_fare else ""
    if wants_fare:
        body["gift_fare"] = bool(gift_fare)
    for candidate in candidates:
        outcome, message_id = await _send_one(
            store,
            config,
            candidate,
            kind,
            # The cap retry names its own claim, per lost note.
            candidate.ledger_kind or ledger_kind,
            moment,
            client,
            gift_fare=gift_fare,
            day_cap=day_cap,
            month_cap=month_cap,
        )
        if outcome == SENT:
            body["sent"] += 1
            if message_id:
                body["resend_ids"].append(message_id)
        elif outcome == REFUSED:
            body["refused"] += 1
        elif outcome == ALREADY_CLAIMED:
            body["already_claimed"] += 1
        else:
            body["unknown"] += 1
    return body


async def _send_one(
    store,
    config: MailConfig,
    candidate: SequenceCandidate,
    kind: str,
    ledger_kind: str,
    moment: float,
    client: httpx.AsyncClient | None,
    *,
    gift_fare: str = "",
    day_cap: int = 0,
    month_cap: int = 0,
) -> tuple[str, str]:
    """Claim, render, send, record. One account. Never raises.

    Returns the outcome and Resend's message id (empty unless SENT).

    The order is the whole point and it is the same order the welcome and
    the cap note already use: CLAIM first, because the row count of an
    `INSERT ... ON CONFLICT DO NOTHING` is the only thing that can tell two
    concurrent runs apart, and "we did not see an error afterwards" re-sends
    the note on every retry of the cron.
    """
    try:
        claimed = await store.claim_sent_email(
            candidate.google_sub, ledger_kind, moment
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not claim the %s note: %s", kind, exc)
        return ALREADY_CLAIMED, ""
    if not claimed:
        return ALREADY_CLAIMED, ""

    # The cap note ALSO goes through the older claim on the user row, which
    # carries the 30-day spacing, the 72-hour gap after the welcome and the
    # lane-wide daily ceiling. Two guards rather than one because the
    # inline path (`maillist.maybe_send_cap_note`) claims the same row, and
    # this job must not be able to send a second copy behind its back.
    # The cap note ALSO claims the user row, which carries the 30-day
    # spacing, the 72-hour gap after the welcome and the lane's daily
    # ceiling. The RETRY does not: that row is already stamped -- being
    # stamped with nothing delivered is the whole reason the retry
    # selected this account -- and re-claiming it would fail on its own
    # 30-day guard and drop the send.
    if kind == KIND_CAP:
        try:
            if not await store.claim_cap_note(candidate.google_sub, moment):
                await store.release_sent_email(candidate.google_sub, ledger_kind)
                return ALREADY_CLAIMED, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not claim the cap note row: %s", exc)
            await _release(store, candidate.google_sub, ledger_kind)
            return ALREADY_CLAIMED, ""

    # The welcome retry ALSO claims the once-ever stamp on the user row,
    # the same `UPDATE ... WHERE welcome_sent_at IS NULL` the sign-in
    # callback uses. Two guards, because the callback is still running for
    # new sign-ins and this job must not be able to send a second copy
    # behind its back.
    if kind == KIND_WELCOME:
        try:
            if not await store.mark_welcome_sent(candidate.google_sub):
                await _release(store, candidate.google_sub, ledger_kind)
                return ALREADY_CLAIMED, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not claim the welcome: %s", exc)
            await _release(store, candidate.google_sub, ledger_kind)
            return ALREADY_CLAIMED, ""

    note = render(
        kind,
        candidate,
        config,
        gift_fare=gift_fare,
        day_cap=day_cap,
        month_cap=month_cap,
    )
    link = unsubscribe_url(config, candidate.unsubscribe_token)
    outcome, message_id = await post_email(
        _payload(candidate, note, link),
        config,
        label=f"{kind} note",
        client=client,
    )
    if outcome == SENT:
        try:
            await store.finish_sent_email(
                candidate.google_sub, ledger_kind, message_id
            )
        except Exception as exc:  # noqa: BLE001 - the send already happened
            logger.warning("could not record the Resend id: %s", exc)
        # The welcome's receipt lives under its own kind, not the retry's,
        # because that is the row `welcome_candidates` and the daily read
        # both ask about -- a retry that lands must make this account stop
        # looking lost. Same for the cap note's day row.
        if kind == KIND_WELCOME:
            await record_sent(
                store, candidate.google_sub, WELCOME_KIND, message_id
            )
        elif kind == KIND_CAP_RETRY:
            # Under the LOST note's own day, never today's: the claim we
            # are making good on is `cap:<that day>`, and writing today's
            # instead would suppress a genuine new cap note this evening.
            await record_sent(
                store,
                candidate.google_sub,
                CAP_KIND_PREFIX + ledger_kind.split(":", 1)[-1],
                message_id,
            )
        return SENT, message_id
    if outcome == REFUSED:
        # Resend answered and declined, so nothing is in flight and the
        # claim is a lie. A timeout is NOT released: the message may have
        # gone, and a second copy is worse than none.
        if kind == KIND_WELCOME:
            # The stamp comes off -- the note did not go, and
            # `/metrics/signins` must keep counting this account as one
            # the welcome never reached. The LEDGER row stays: the retry
            # is one attempt, ever, and an address Resend refuses today it
            # will refuse tomorrow. A third attempt is a human's call.
            try:
                await store.release_welcome(candidate.google_sub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not release the welcome stamp: %s", exc)
            return REFUSED, ""
        await _release(store, candidate.google_sub, ledger_kind)
        if kind == KIND_CAP:
            try:
                await store.release_cap_note(candidate.google_sub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not release the cap-note claim: %s", exc)
        return REFUSED, ""
    return outcome, ""


async def _release(store, google_sub: str, ledger_kind: str) -> None:
    try:
        await store.release_sent_email(google_sub, ledger_kind)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not release the send claim: %s", exc)
