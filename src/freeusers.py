"""
Who signed in to the free server, so we can write to them later.

Why a row at all
----------------
The free server has never known who its callers are. Fair use counts a
digest of an address and a user agent; nothing in it is a person. Matan's
call on 2026-09-09 was to add an OPTIONAL sign-in and keep the addresses, so
that people who already use the free tier can be told about the paid one by
email instead of only by a line in a refusal.

So exactly one row per Google account, holding exactly what that use needs:

    google_sub        the account id -- the key, and the only stable identifier
    email             the address the upsell list is built from
    first_seen        when they first signed in (the consent moment)
    last_seen         the last time a signed-in request arrived
    call_count        backend searches this account has spent, all time
    client_kind       the MCP client they approved (Claude Code, Cursor, ...)
    resend_state      whether the address reached a Resend audience, and how
    email_opt_out     they pressed unsubscribe. Nothing is ever sent again.
    unsubscribe_token what makes the one-click link work without a session
    welcome_sent_at   the "once per user, ever" guard on the welcome note
    cap_note_sent_at  the "once per 30 days" guard on the cap note

plus `free_mcp_user_days`, one row per account per UTC day, which is what
lets the 08:33Z read say how many signed-in users there were and what they
spent -- and, with its `cap_hits` column, what branch D of the cap note is
evaluated over -- and `free_mcp_user_tools`, the same shape with the tool
name in the key, which is the only thing that can answer "does this account
search flights or hotels" and therefore which of the two URLs the cap note
prints.

**This table IS the mailing list.** Resend's plan caps this account at three
audiences/segments and all three are taken, so there is no `free-mcp-signins`
audience to add anyone to (see `src/maillist.py`). Consent, opt-out and send
history live here; the transactional sends go out through Resend's
`POST /emails`, which needs none of that.

No RapidAPI key, no search history, no ad data. The free server stores no
secret of the user's at all -- which is why it needs no `MCP_KEY_MASTER`
equivalent for encryption, and why nothing in this module is encrypted:
there is nothing here that a leak would turn into spend.

`last_seen` and `call_count` are BEST EFFORT and are a floor, not a count.
They are written from a background task after the tool call has already been
answered, and a serverless instance that freezes between the response and
the write simply loses that increment. The exact per-client numbers are the
Upstash fair-use counters; these two exist so a future email can say "you
last used it in March" without a join.

Storage is Neon Postgres, reached exactly the way the paid server reaches it
(`mcp_server_paid/src/keystore.py`): asyncpg imported lazily inside the first
call that touches it, one connection per operation, pointed at the POOLED
endpoint. Table in `migrations/001_free_mcp_signin.sql`.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class UserStoreError(RuntimeError):
    """Storage failed. Never surfaced to a caller as "your request is bad"."""


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _date(epoch: float):
    """The UTC calendar date, as a `date`.

    asyncpg binds a Postgres `date` column from a `datetime.date`; the
    `YYYY-MM-DD` string `utc_day` returns is the key the in-memory store and
    the log lines use, and passing that here would make asyncpg guess.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def utc_day(epoch: float) -> str:
    """`YYYY-MM-DD` in UTC -- the day key both stores agree on.

    UTC because the fair-use day cap rolls over at 00:00 UTC, and a usage row
    on a different clock would disagree with the counter that refused the
    call it is describing.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def utc_days_back(epoch: float, days: int) -> list[str]:
    """The last `days` UTC day keys, today first.

    Branch D of the cap note counts DISTINCT days with a refusal inside a
    rolling week, so the window is a list of day keys and not a range: the
    day rows are keyed by the same `YYYY-MM-DD` string the counter that
    produced the refusal rolls over on.
    """
    return [utc_day(epoch - i * 86400.0) for i in range(max(1, days))]


def window_days(hours: int, now: float | None = None) -> int:
    """How many UTC days an `hours` window ending at `now` touches.

    The usage rows are keyed by UTC day, so an hours-shaped question can
    only be answered in whole days -- and `hours=24` asked at 12:00Z touches
    TWO of them, which is why this counts calendar days rather than
    dividing by 24. Widening is the safe direction: a number that includes a
    few hours too many is a visible over-count, while a narrow window
    silently drops this morning's searches and turns an active account into
    a day-2 note candidate.
    """
    moment = now if now is not None else time.time()
    span = max(1, int(hours)) * 3600.0
    return (_date(moment) - _date(moment - span)).days + 1


def utc_month_prefix(epoch: float) -> str:
    """`YYYY-MM` in UTC -- the calendar month the month cap is counted over."""
    return time.strftime("%Y-%m", time.gmtime(epoch))


def days_left_in_month(epoch: float) -> int:
    """Whole days of this UTC month AFTER today.

    Branch M only fires when there is still a month left to spend: telling
    somebody on the 30th that they are two thirds through the month is not
    an offer, it is a fact they cannot act on.
    """
    import calendar  # noqa: PLC0415 -- one call site

    now = time.gmtime(epoch)
    last = calendar.monthrange(now.tm_year, now.tm_mon)[1]
    return max(0, last - now.tm_mday)


#: Which of the two paid URLs a cap note prints. Substring rather than a
#: fixed set on purpose: a hotel tool added later is a hotel tool, and the
#: failure mode of guessing wrong here is one link in one email, while the
#: failure mode of a stale set is a hotel user being sent to flights.
def is_hotel_tool(tool: str) -> bool:
    return "hotel" in (tool or "").lower()


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value or 0.0)


#: `resend_state` values. `pending` is what a row starts as; the background
#: task moves it to one of the other three and never moves it back, so a
#: retry can be told from a first attempt and an address that opted out is
#: never posted again (Resend's POST /contacts UPSERTS and resets
#: `unsubscribed` to false -- see the module header of `maillist.py`).
RESEND_PENDING = "pending"
RESEND_ADDED = "added"
RESEND_SKIPPED = "skipped"
RESEND_FAILED = "failed"

#: How many bytes of randomness go into an unsubscribe token. It has to be
#: unguessable -- the link takes no session and no confirmation click, which
#: is what "one-click unsubscribe" means and what the List-Unsubscribe-Post
#: header promises -- and it must not be derivable from the address, or an
#: address is enough to unsubscribe a stranger.
UNSUBSCRIBE_TOKEN_BYTES = 24


#: The cap note's frequency guards, from the sequence file. Named here
#: rather than inlined in the SQL because the tests assert on them and
#: because a reader who wants to know "how often can this thing mail me"
#: should find the answer in one place.
CAP_NOTE_MIN_GAP_DAYS = 30
#: Never inside 72 hours of the welcome: two notes in three days from a
#: server somebody just signed in to reads as a drip, and the sequence file
#: is explicit that there is no drip.
CAP_NOTE_WELCOME_GAP_HOURS = 72
#: Lane-wide, per UTC day. A spike in sign-ins must not turn into a mail
#: blast; the overflow simply waits for the next day.
CAP_NOTE_LANE_DAY_CAP = 20

#: The day-2 activation note (`state/gtm/research/outreach-dm-craft-pass9-2026-09-11.md`
#: §2.2). It is a FOURTH touch on a sequence that bans drips, and it earns
#: that only by going to the one state with an unfinished job: signed in,
#: never searched. Two days is the floor because the sources agree the
#: useful window is the first three days and a note on day 1 lands before
#: the person has had an evening to try it.
DAY2_MIN_HOURS = 48
#: Lane-wide, per run, for BOTH scheduled notes. Same number and same
#: reason as the cap note's: a spike in sign-ins must never turn into a mail
#: blast, and the overflow waits for tomorrow's run.
SEQUENCE_LANE_DAY_CAP = 20

#: `sent_emails.kind`. The table's unique key is (user_sub, kind), so the
#: kind IS the window: `day2` is once per account ever, and the cap note
#: carries the UTC day it belongs to, which makes "at most one cap note per
#: account per day" a database constraint instead of a code path that has to
#: remember to check. The 30-day spacing between cap notes is a separate,
#: older guard on `free_mcp_users.cap_note_sent_at` and both are enforced.
DAY2_KIND = "day2"
CAP_KIND_PREFIX = "cap:"
#: The day-7 note (`state/gtm/email/FREE-SIGNIN-UPSELL-2026-09-17.md` (b)).
#: Once per account ever, so the kind carries no window.
DAY7_KIND = "day7"
#: The dormant note, (c). Also once ever, and it is the LAST automated note
#: that address gets: welcome, day-2, dormant, and then the lane is closed.
DORMANT_KIND = "dormant14"
#: The welcome, in the ledger. `welcome_sent_at` on the user row is a
#: CLAIM -- it is stamped before the Resend call, because that is what
#: stops two instances racing one first sign-in from sending twice -- so a
#: stamp on its own does not say the note went out. This row does: it
#: carries Resend's message id and is written only AFTER Resend accepted
#: the note, so "stamped, no ledger id" is precisely the lost-send case
#: the retry exists for, and the daily read can count it.
WELCOME_KIND = "welcome"
#: The welcome RETRY's own claim. One row, so one retry, ever.
WELCOME_RETRY_KIND = "welcome:retry"
#: The cap note's retry claim, per lost note: `cap-retry:<the UTC day the
#: lost note was claimed on>`. Same rule as the welcome -- `cap_note_sent_at`
#: is a claim, the ledger row is the proof -- and the same bound: one retry
#: per lost note, never a daily re-send.
CAP_RETRY_PREFIX = "cap-retry:"

#: How far back the cap retry looks. A cap note is about what happened to
#: somebody TODAY; three days later that sentence is stale and the right
#: move is to let the next cap fire a fresh one.
CAP_RETRY_MAX_DAYS = 3

#: The day-7 note's window and floor. Seven days because that is what the
#: copy says ("in your first week"), ten because a note that turns up on
#: day 20 describing "your first week" reads as automation. Ten searches is
#: the floor: it is the ">= 10" bucket the 2026-09-17 read found six real
#: accounts in, and below it the note's own opening sentence is false.
DAY7_MIN_HOURS = 168
DAY7_MAX_HOURS = 240
DAY7_MIN_SEARCHES = 10

#: The dormant note's window. Fourteen days is the copy ("you signed in two
#: weeks ago"); seventeen is the ceiling, so a backlog can never fire at
#: once and nobody gets a "two weeks ago" note a month late. One search
#: still counts as dormant -- they connected, tried it once and stopped,
#: which is the same question.
DORMANT_MIN_HOURS = 336
DORMANT_MAX_HOURS = 408
DORMANT_MAX_SEARCHES = 1

#: The welcome retry's window. Ten minutes so a retry can never race the
#: sign-in callback's own attempt. Fourteen days so a person who signed in
#: a month ago and never heard from us is not welcomed to a server they
#: have forgotten about.
WELCOME_RETRY_MIN_MINUTES = 10
WELCOME_RETRY_MAX_DAYS = 14


def cap_kind(epoch: float) -> str:
    """`cap:YYYY-MM-DD` for the UTC day `epoch` falls in."""
    return CAP_KIND_PREFIX + utc_day(epoch)


def cap_retry_kind(epoch: float) -> str:
    """`cap-retry:YYYY-MM-DD` for the day the LOST note was claimed on."""
    return CAP_RETRY_PREFIX + utc_day(epoch)


#: When a welcome stamp with no ledger row counts as a LOST send rather
#: than as a send from before the ledger existed. Unset (the shipped
#: default) means only a NULL stamp is ever retried, which is the safe
#: reading: every welcome sent before this code shipped is stamped and has
#: no ledger row, and retrying those would mail a second welcome to people
#: who already got one. Set it to the deploy moment -- epoch seconds or
#: `YYYY-MM-DD` -- once the ledger is live, and lost sends after that
#: moment become retryable too.
WELCOME_LEDGER_SINCE_ENV = "FREE_WELCOME_LEDGER_SINCE"


def welcome_ledger_since() -> float:
    """The cutover as an epoch, or 0.0 when it is not configured."""
    raw = (os.environ.get(WELCOME_LEDGER_SINCE_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return (
            datetime.strptime(raw, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        logger.warning(
            "%s is neither an epoch nor YYYY-MM-DD; treating it as unset",
            WELCOME_LEDGER_SINCE_ENV,
        )
        return 0.0


def new_unsubscribe_token() -> str:
    import secrets  # noqa: PLC0415 -- one call site

    return secrets.token_urlsafe(UNSUBSCRIBE_TOKEN_BYTES)


@dataclass(frozen=True)
class CapSignals:
    """Everything the cap note's triggers are decided on, in one read.

    One query set rather than four calls, because this is evaluated in a
    background task after a tool call has already been answered and every
    extra Neon round trip is a chance for the instance to freeze first.

    Read `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` (b) for what
    each number gates. In short: `cap_days_7 >= 2` is branch D,
    `month_searches >= 60% of the month cap` with `days_left_in_month >= 5`
    is branch M, and `active_days_ever >= 2` is required by both -- one
    afternoon of a script hammering the server is not a user, it is a test.
    """

    cap_days_7: int = 0
    active_days_ever: int = 0
    month_searches: int = 0
    #: Month-to-date searches we could attribute to a tool at all, and the
    #: hotel share of them. Both 0 on an account that only used the server
    #: before `free_mcp_user_tools` existed, which is why the default is
    #: flights rather than "no link".
    month_tool_searches: int = 0
    month_hotel_searches: int = 0
    days_left_in_month: int = 0

    @property
    def hotel_majority(self) -> bool:
        """True when hotels are the MAJORITY of this month's attributed use.

        Strictly more than half, so a 50/50 account gets flights -- the
        larger listing, and the one every printed guide already points at.
        """
        return (
            self.month_tool_searches > 0
            and self.month_hotel_searches * 2 > self.month_tool_searches
        )


@dataclass(frozen=True)
class SequenceCandidate:
    """One account a scheduled note would go to, and everything to render it.

    Returned by `day2_candidates` / `cap_candidates`, which do the whole
    selection in ONE query rather than handing back subs to look up one at a
    time: the job runs on a serverless function with a 300 s ceiling and a
    per-user round trip to Neon is 20 connections for 20 emails.

    Selection is not the same thing as permission to send. The claim
    (`claim_sent_email`, and for the cap note `claim_cap_note` as well) is
    what makes a send happen at most once, because two runs of the job can
    overlap and a SELECT is not a lock.
    """

    google_sub: str
    email: str
    unsubscribe_token: str = ""
    first_seen: float = 0.0
    #: Month-to-date, for the cap note's hero card. 0 for a day-2 candidate,
    #: who by definition has never searched.
    month_searches: int = 0
    month_tool_searches: int = 0
    month_hotel_searches: int = 0
    #: The day-7 note's one number: searches in the seven days after this
    #: account first signed in, which is what the copy claims ("you've run
    #: N searches in your first week") and not the all-time counter.
    first_week_searches: int = 0
    #: Set by a selector that cannot use the run's single ledger kind --
    #: the cap retry, whose claim names the day the LOST note was claimed
    #: on, so one lost note gets exactly one retry and never a daily one.
    ledger_kind: str = ""

    @property
    def hotel_majority(self) -> bool:
        """Strictly more than half of this month's attributed searches.

        A 50/50 account gets flights, and so does an account we cannot
        attribute at all -- flights is the larger listing and the one every
        printed guide points at, so it is right more often than a coin flip.
        """
        return (
            self.month_tool_searches > 0
            and self.month_hotel_searches * 2 > self.month_tool_searches
        )


@dataclass(frozen=True)
class FreeUser:
    google_sub: str
    email: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    call_count: int = 0
    client_kind: str = ""
    resend_state: str = RESEND_PENDING
    email_opt_out: bool = False
    unsubscribe_token: str = ""
    welcome_sent_at: float | None = None
    cap_note_sent_at: float | None = None


#: How many user rows `signin_records` will ever read in one call. The
#: sign-in list is in the low tens today, so this is not a paging concern
#: yet -- it is a promise that a route behind a cron can never turn into an
#: unbounded read of a growing table. The reader is told when it bites
#: (`truncated` in the `/metrics/signins` body), because a total computed
#: over a silently clipped set is worse than no total.
SIGNIN_STATS_MAX_ROWS = 5000


@dataclass(frozen=True)
class SigninRecord:
    """One signed-in account, as the reporting surface sees it.

    Deliberately NOT `FreeUser`. That row is what the sign-in path reads and
    writes; this is a join of it with the day rows and the send ledger, and
    it carries the two numbers the daily read cannot get any other way: what
    this account spent inside the window, and how often the cap refused it.

    `calls_total` and `calls_window` come from two different places on
    purpose -- the user row's running counter and the per-day rows -- so a
    disagreement between them is visible rather than averaged away. Both are
    written best-effort by the same background task, which is exactly why
    the route also reports the telemetry totals beside them: those come off
    the call path and are the independent system the day-2 gate needs.
    """

    google_sub: str
    email: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    #: The user row's all-time counter (`free_mcp_users.call_count`).
    calls_total: int = 0
    #: Summed from `free_mcp_user_days` over the window's UTC days.
    calls_window: int = 0
    #: `free_mcp_user_days.cap_hits` over the same days.
    cap_hits_window: int = 0
    client_kind: str = ""
    welcome_sent_at: float | None = None
    #: From the `sent_emails` ledger, kind `day2`. There is no column for it
    #: on the user row: the ledger is where every note after the first two
    #: lives (see `migrations/003_sent_emails.sql`).
    day2_sent_at: float | None = None
    cap_note_sent_at: float | None = None
    opted_out: bool = False
    #: Every `sent_emails.kind` this account has a row for, cap days
    #: included (`cap:2026-09-18`). What `/metrics/signins` counts per
    #: kind: one ledger read rather than a column per note, so a fifth
    #: note needs no migration and no new field here.
    kinds_sent: tuple[str, ...] = ()
    #: Resend's id for the welcome, from the ledger. Empty means the note
    #: was CLAIMED (`welcome_sent_at` is set) but never confirmed sent --
    #: either a lost send, or a welcome from before the ledger existed.
    welcome_resend_id: str = ""


class FreeUserStore(Protocol):
    available: bool

    async def ping(self, timeout: float = 0.0) -> bool: ...

    async def upsert(
        self,
        google_sub: str,
        email: str,
        *,
        client_kind: str = "",
        now: float | None = None,
    ) -> bool: ...

    async def get(self, google_sub: str) -> FreeUser | None: ...

    async def signin_records(
        self,
        now: float | None = None,
        *,
        hours: int = 24,
        limit: int = SIGNIN_STATS_MAX_ROWS,
    ) -> list[SigninRecord]: ...

    async def touch(
        self,
        google_sub: str,
        spent: int = 1,
        now: float | None = None,
        tool: str = "",
    ) -> None: ...

    async def note_cap_hit(
        self, google_sub: str, now: float | None = None
    ) -> None: ...

    async def cap_signals(
        self, google_sub: str, now: float | None = None
    ) -> CapSignals: ...

    async def claim_cap_note(
        self, google_sub: str, now: float | None = None
    ) -> bool: ...

    async def release_cap_note(self, google_sub: str) -> None: ...

    async def day2_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        min_hours: int = DAY2_MIN_HOURS,
        max_hours: int | None = None,
    ) -> list[SequenceCandidate]: ...

    async def cap_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]: ...

    async def day7_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]: ...

    async def dormant_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]: ...

    async def welcome_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        ledger_since: float = 0.0,
    ) -> list[SequenceCandidate]: ...

    async def cap_retry_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]: ...

    async def claim_sent_email(
        self, google_sub: str, kind: str, now: float | None = None
    ) -> bool: ...

    async def finish_sent_email(
        self, google_sub: str, kind: str, resend_id: str
    ) -> None: ...

    async def release_sent_email(self, google_sub: str, kind: str) -> None: ...

    async def set_resend_state(self, google_sub: str, state: str) -> None: ...

    async def mark_welcome_sent(self, google_sub: str) -> bool: ...

    async def release_welcome(self, google_sub: str) -> None: ...

    async def opt_out(self, token: str) -> bool: ...

    async def delete(self, google_sub: str) -> bool: ...


class NullFreeUserStore:
    """What a deployment with no DATABASE_URL gets.

    Reads answer "nobody"; `upsert` reports that the row is not new, so the
    Resend hand-off never fires against a store that did not record anything.
    Nothing here raises: a free server that cannot reach Postgres must still
    serve flights, and the sign-in feature is simply not registered (see
    `oauth.build_free_oauth`).
    """

    available = False

    async def ping(self, timeout: float = 0.0) -> bool:
        """Nothing to reach. `available` is False, so `/health` reports this
        as "not configured" rather than as an outage."""
        return False

    async def upsert(
        self,
        google_sub: str,
        email: str,
        *,
        client_kind: str = "",
        now: float | None = None,
    ) -> bool:
        return False

    async def get(self, google_sub: str) -> FreeUser | None:
        return None

    async def signin_records(
        self,
        now: float | None = None,
        *,
        hours: int = 24,
        limit: int = SIGNIN_STATS_MAX_ROWS,
    ) -> list[SigninRecord]:
        # Nobody has signed in, because nothing recorded anyone. The route
        # never gets here -- `available` is False and it answers 503 first --
        # but an empty list is the honest value either way.
        return []

    async def touch(
        self,
        google_sub: str,
        spent: int = 1,
        now: float | None = None,
        tool: str = "",
    ) -> None:
        return None

    async def note_cap_hit(self, google_sub: str, now: float | None = None) -> None:
        return None

    async def cap_signals(
        self, google_sub: str, now: float | None = None
    ) -> CapSignals:
        return CapSignals()

    async def claim_cap_note(self, google_sub: str, now: float | None = None) -> bool:
        return False

    async def release_cap_note(self, google_sub: str) -> None:
        return None

    async def day2_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        min_hours: int = DAY2_MIN_HOURS,
        max_hours: int | None = None,
    ) -> list[SequenceCandidate]:
        return []

    async def cap_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        return []

    async def day7_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        return []

    async def dormant_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        return []

    async def welcome_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        ledger_since: float = 0.0,
    ) -> list[SequenceCandidate]:
        return []

    async def cap_retry_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        return []

    async def claim_sent_email(
        self, google_sub: str, kind: str, now: float | None = None
    ) -> bool:
        # Never "yes". A store that records nothing cannot promise that a
        # second run will see the row, and the whole value of the claim is
        # that promise.
        return False

    async def finish_sent_email(
        self, google_sub: str, kind: str, resend_id: str
    ) -> None:
        return None

    async def release_sent_email(self, google_sub: str, kind: str) -> None:
        return None

    async def set_resend_state(self, google_sub: str, state: str) -> None:
        return None

    async def mark_welcome_sent(self, google_sub: str) -> bool:
        return False

    async def release_welcome(self, google_sub: str) -> None:
        return None

    async def opt_out(self, token: str) -> bool:
        return False

    async def delete(self, google_sub: str) -> bool:
        return False


class MemoryFreeUserStore:
    """In-process, for tests and `python -m src` on a laptop."""

    available = True

    async def ping(self, timeout: float = 0.0) -> bool:
        return True

    def __init__(self) -> None:
        self.rows: dict[str, FreeUser] = {}
        #: (google_sub, "YYYY-MM-DD") -> backend searches. The same shape the
        #: `free_mcp_user_days` table has, so a test that reads it reads the
        #: thing production writes.
        self.days: dict[tuple[str, str], int] = {}
        #: (google_sub, "YYYY-MM-DD") -> times the day cap refused a call.
        #: `free_mcp_user_days.cap_hits`.
        self.cap_hits: dict[tuple[str, str], int] = {}
        #: (google_sub, "YYYY-MM-DD", tool) -> backend searches.
        #: `free_mcp_user_tools`.
        self.tools: dict[tuple[str, str, str], int] = {}
        #: (google_sub, kind) -> {"sent_at", "resend_id"}. `sent_emails`,
        #: whose unique (user_sub, kind) is the only thing standing between
        #: two overlapping runs of the daily job and two copies of the same
        #: note.
        self.sent: dict[tuple[str, str], dict] = {}

    async def upsert(
        self,
        google_sub: str,
        email: str,
        *,
        client_kind: str = "",
        now: float | None = None,
    ) -> bool:
        stamp = now if now is not None else time.time()
        existing = self.rows.get(google_sub)
        if existing is None:
            self.rows[google_sub] = FreeUser(
                google_sub=google_sub,
                email=email,
                first_seen=stamp,
                last_seen=stamp,
                call_count=0,
                client_kind=client_kind,
                resend_state=RESEND_PENDING,
                unsubscribe_token=new_unsubscribe_token(),
            )
            return True
        from dataclasses import replace  # noqa: PLC0415 -- one call site

        self.rows[google_sub] = replace(
            existing,
            # A changed address replaces the old one; an empty one never
            # overwrites a good one, because an unverified Google email
            # arrives as "" (see webauth.GoogleWebAuth.finish).
            email=email or existing.email,
            last_seen=stamp,
            client_kind=client_kind or existing.client_kind,
        )
        return False

    async def get(self, google_sub: str) -> FreeUser | None:
        return self.rows.get(google_sub)

    async def signin_records(
        self,
        now: float | None = None,
        *,
        hours: int = 24,
        limit: int = SIGNIN_STATS_MAX_ROWS,
    ) -> list[SigninRecord]:
        stamp = now if now is not None else time.time()
        window = set(utc_days_back(stamp, window_days(hours, stamp)))
        rows = sorted(
            self.rows.values(), key=lambda row: row.first_seen, reverse=True
        )
        out: list[SigninRecord] = []
        for row in rows[: max(0, limit)]:
            spent = sum(
                n
                for (sub, day), n in self.days.items()
                if sub == row.google_sub and day in window
            )
            refusals = sum(
                n
                for (sub, day), n in self.cap_hits.items()
                if sub == row.google_sub and day in window
            )
            sent = self.sent.get((row.google_sub, DAY2_KIND))
            kinds = tuple(
                sorted(kind for (sub, kind) in self.sent if sub == row.google_sub)
            )
            welcome_id = str(
                (self.sent.get((row.google_sub, WELCOME_KIND)) or {}).get(
                    "resend_id", ""
                )
            )
            out.append(
                SigninRecord(
                    google_sub=row.google_sub,
                    email=row.email,
                    first_seen=row.first_seen,
                    last_seen=row.last_seen,
                    calls_total=row.call_count,
                    calls_window=spent,
                    cap_hits_window=refusals,
                    client_kind=row.client_kind,
                    welcome_sent_at=row.welcome_sent_at,
                    day2_sent_at=(sent or {}).get("sent_at"),
                    cap_note_sent_at=row.cap_note_sent_at,
                    opted_out=row.email_opt_out,
                    kinds_sent=kinds,
                    welcome_resend_id=welcome_id,
                )
            )
        return out

    async def touch(
        self,
        google_sub: str,
        spent: int = 1,
        now: float | None = None,
        tool: str = "",
    ) -> None:
        row = self.rows.get(google_sub)
        if row is None:
            return
        from dataclasses import replace  # noqa: PLC0415

        stamp = now if now is not None else time.time()
        self.rows[google_sub] = replace(
            row, last_seen=stamp, call_count=row.call_count + max(0, spent)
        )
        day = utc_day(stamp)
        key = (google_sub, day)
        self.days[key] = self.days.get(key, 0) + max(0, spent)
        if tool:
            tkey = (google_sub, day, tool)
            self.tools[tkey] = self.tools.get(tkey, 0) + max(0, spent)

    async def note_cap_hit(self, google_sub: str, now: float | None = None) -> None:
        if google_sub not in self.rows:
            return
        stamp = now if now is not None else time.time()
        key = (google_sub, utc_day(stamp))
        self.cap_hits[key] = self.cap_hits.get(key, 0) + 1
        self.days.setdefault(key, 0)

    async def cap_signals(
        self, google_sub: str, now: float | None = None
    ) -> CapSignals:
        stamp = now if now is not None else time.time()
        week = set(utc_days_back(stamp, 7))
        month = utc_month_prefix(stamp)
        cap_days = sum(
            1
            for (sub, day), hits in self.cap_hits.items()
            if sub == google_sub and day in week and hits > 0
        )
        active = sum(
            1
            for (sub, _day), spent in self.days.items()
            if sub == google_sub and spent > 0
        )
        month_searches = sum(
            spent
            for (sub, day), spent in self.days.items()
            if sub == google_sub and day.startswith(month)
        )
        attributed = 0
        hotels = 0
        for (sub, day, tool), spent in self.tools.items():
            if sub != google_sub or not day.startswith(month):
                continue
            attributed += spent
            if is_hotel_tool(tool):
                hotels += spent
        return CapSignals(
            cap_days_7=cap_days,
            active_days_ever=active,
            month_searches=month_searches,
            month_tool_searches=attributed,
            month_hotel_searches=hotels,
            days_left_in_month=days_left_in_month(stamp),
        )

    async def claim_cap_note(
        self, google_sub: str, now: float | None = None
    ) -> bool:
        from dataclasses import replace  # noqa: PLC0415

        stamp = now if now is not None else time.time()
        row = self.rows.get(google_sub)
        if row is None or not row.email or row.email_opt_out:
            return False
        if (
            row.cap_note_sent_at is not None
            and stamp - row.cap_note_sent_at < CAP_NOTE_MIN_GAP_DAYS * 86400
        ):
            return False
        if (
            row.welcome_sent_at is not None
            and stamp - row.welcome_sent_at < CAP_NOTE_WELCOME_GAP_HOURS * 3600
        ):
            return False
        today = utc_day(stamp)
        sent_today = sum(
            1
            for other in self.rows.values()
            if other.cap_note_sent_at is not None
            and utc_day(other.cap_note_sent_at) == today
        )
        if sent_today >= CAP_NOTE_LANE_DAY_CAP:
            return False
        self.rows[google_sub] = replace(row, cap_note_sent_at=stamp)
        return True

    async def release_cap_note(self, google_sub: str) -> None:
        from dataclasses import replace  # noqa: PLC0415

        row = self.rows.get(google_sub)
        if row is not None:
            self.rows[google_sub] = replace(row, cap_note_sent_at=None)

    # ── the scheduled notes ──────────────────────────────────────────────

    def _mailable(self, row: FreeUser) -> bool:
        return bool(row.email) and not row.email_opt_out

    async def day2_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        min_hours: int = DAY2_MIN_HOURS,
        max_hours: int | None = None,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            age_hours = (stamp - row.first_seen) / 3600.0
            if age_hours < min_hours:
                continue
            if max_hours is not None and age_hours > max_hours:
                continue
            if row.call_count > 0:
                continue
            if any(
                spent > 0 for (other, _day), spent in self.days.items() if other == sub
            ):
                continue
            if (sub, DAY2_KIND) in self.sent:
                continue
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                )
            )
        # Newest first: if a backlog ever builds up, the people who signed
        # in this week are the ones the note is actually about.
        out.sort(key=lambda c: c.first_seen, reverse=True)
        return out[: max(0, limit)]

    async def cap_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        today = utc_day(stamp)
        kind = cap_kind(stamp)
        month = utc_month_prefix(stamp)
        gap = stamp - CAP_NOTE_MIN_GAP_DAYS * 86400.0
        after_welcome = stamp - CAP_NOTE_WELCOME_GAP_HOURS * 3600.0
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            if self.cap_hits.get((sub, today), 0) <= 0:
                continue
            if (sub, kind) in self.sent:
                continue
            if row.cap_note_sent_at is not None and row.cap_note_sent_at >= gap:
                continue
            if row.welcome_sent_at is not None and row.welcome_sent_at >= after_welcome:
                continue
            month_searches = sum(
                spent
                for (other, day), spent in self.days.items()
                if other == sub and day.startswith(month)
            )
            attributed = 0
            hotels = 0
            for (other, day, tool), spent in self.tools.items():
                if other != sub or not day.startswith(month):
                    continue
                attributed += spent
                if is_hotel_tool(tool):
                    hotels += spent
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                    month_searches=month_searches,
                    month_tool_searches=attributed,
                    month_hotel_searches=hotels,
                )
            )
        out.sort(key=lambda c: c.month_searches, reverse=True)
        return out[: max(0, limit)]

    def _first_week(self, sub: str, first_seen: float) -> int:
        """Searches in the seven days that follow this account's sign-in.

        The day rows and not `call_count`: the counter is all-time, and the
        day-7 note's opening sentence is a claim about the first week
        specifically. A number we cannot source from the days it covers is
        not printed (CLAUDE.md rule 1).
        """
        start = utc_day(first_seen)
        end = utc_day(first_seen + DAY7_MIN_HOURS * 3600.0)
        return sum(
            spent
            for (other, day), spent in self.days.items()
            if other == sub and start <= day <= end
        )

    async def day7_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            age_hours = (stamp - row.first_seen) / 3600.0
            if age_hours < DAY7_MIN_HOURS or age_hours > DAY7_MAX_HOURS:
                continue
            if (sub, DAY7_KIND) in self.sent:
                continue
            # Never refused. Somebody who hit the cap is the cap note's
            # person, and telling them about the allowance they already ran
            # out of is the wrong note.
            if any(
                hits > 0 for (other, _day), hits in self.cap_hits.items()
                if other == sub
            ):
                continue
            first_week = self._first_week(sub, row.first_seen)
            if first_week < DAY7_MIN_SEARCHES:
                continue
            month = utc_month_prefix(stamp)
            attributed = 0
            hotels = 0
            for (other, day, tool), spent in self.tools.items():
                if other != sub or not day.startswith(month):
                    continue
                attributed += spent
                if is_hotel_tool(tool):
                    hotels += spent
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                    month_tool_searches=attributed,
                    month_hotel_searches=hotels,
                    first_week_searches=first_week,
                )
            )
        out.sort(key=lambda c: c.first_week_searches, reverse=True)
        return out[: max(0, limit)]

    async def dormant_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            age_hours = (stamp - row.first_seen) / 3600.0
            if age_hours < DORMANT_MIN_HOURS or age_hours > DORMANT_MAX_HOURS:
                continue
            if (sub, DORMANT_KIND) in self.sent:
                continue
            if row.call_count > DORMANT_MAX_SEARCHES:
                continue
            # Both counters, the same belt-and-braces the day-2 note uses:
            # either can be short by an increment a frozen instance lost,
            # and "you went quiet" is the one sentence an active user must
            # never be sent.
            spent = sum(
                n for (other, _day), n in self.days.items() if other == sub
            )
            if spent > DORMANT_MAX_SEARCHES:
                continue
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                )
            )
        out.sort(key=lambda c: c.first_seen, reverse=True)
        return out[: max(0, limit)]

    def _welcome_confirmed(self, sub: str) -> bool:
        """Did Resend accept this account's welcome? The ledger says.

        The stamp on the user row is only the CLAIM -- it goes on before
        the send -- so it cannot answer this. The ledger row carries the
        message id and is written after the 2xx.
        """
        return bool((self.sent.get((sub, WELCOME_KIND)) or {}).get("resend_id"))

    async def welcome_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        ledger_since: float = 0.0,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        floor = stamp - WELCOME_RETRY_MIN_MINUTES * 60.0
        ceiling = stamp - WELCOME_RETRY_MAX_DAYS * 86400.0
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            if row.welcome_sent_at is not None:
                # Stamped. That is a claim, not a receipt -- but a stamp
                # from before the ledger existed has no row and never
                # will, so it only counts as lost past the cutover.
                if not ledger_since or row.welcome_sent_at < ledger_since:
                    continue
                if self._welcome_confirmed(sub):
                    continue
            if row.first_seen > floor or row.first_seen < ceiling:
                continue
            if (sub, WELCOME_RETRY_KIND) in self.sent:
                continue
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                )
            )
        out.sort(key=lambda c: c.first_seen, reverse=True)
        return out[: max(0, limit)]

    async def cap_retry_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        stamp = now if now is not None else time.time()
        floor = stamp - CAP_RETRY_MAX_DAYS * 86400.0
        month = utc_month_prefix(stamp)
        out: list[SequenceCandidate] = []
        for sub, row in self.rows.items():
            if not self._mailable(row):
                continue
            claimed_at = row.cap_note_sent_at
            if claimed_at is None or claimed_at < floor:
                continue
            # Confirmed by the ledger? Then it went out and there is
            # nothing to retry. Both paths write that row: the job claims
            # `cap:<day>` up front and stamps the id on the 2xx, and the
            # inline path writes the same row after its own 2xx.
            if (self.sent.get((sub, cap_kind(claimed_at))) or {}).get("resend_id"):
                continue
            retry = cap_retry_kind(claimed_at)
            if (sub, retry) in self.sent:
                continue
            month_searches = sum(
                spent
                for (other, day), spent in self.days.items()
                if other == sub and day.startswith(month)
            )
            attributed = 0
            hotels = 0
            for (other, day, tool), spent in self.tools.items():
                if other != sub or not day.startswith(month):
                    continue
                attributed += spent
                if is_hotel_tool(tool):
                    hotels += spent
            out.append(
                SequenceCandidate(
                    google_sub=sub,
                    email=row.email,
                    unsubscribe_token=row.unsubscribe_token,
                    first_seen=row.first_seen,
                    month_searches=month_searches,
                    month_tool_searches=attributed,
                    month_hotel_searches=hotels,
                    ledger_kind=retry,
                )
            )
        out.sort(key=lambda c: c.month_searches, reverse=True)
        return out[: max(0, limit)]

    async def claim_sent_email(
        self, google_sub: str, kind: str, now: float | None = None
    ) -> bool:
        key = (google_sub, kind)
        if key in self.sent:
            return False
        self.sent[key] = {
            "sent_at": now if now is not None else time.time(),
            "resend_id": "",
        }
        return True

    async def finish_sent_email(
        self, google_sub: str, kind: str, resend_id: str
    ) -> None:
        row = self.sent.get((google_sub, kind))
        if row is not None:
            row["resend_id"] = resend_id or ""

    async def release_sent_email(self, google_sub: str, kind: str) -> None:
        self.sent.pop((google_sub, kind), None)

    async def set_resend_state(self, google_sub: str, state: str) -> None:
        row = self.rows.get(google_sub)
        if row is None:
            return
        from dataclasses import replace  # noqa: PLC0415

        self.rows[google_sub] = replace(row, resend_state=state)

    async def mark_welcome_sent(self, google_sub: str) -> bool:
        row = self.rows.get(google_sub)
        if row is None or row.welcome_sent_at is not None:
            return False
        from dataclasses import replace  # noqa: PLC0415

        self.rows[google_sub] = replace(row, welcome_sent_at=time.time())
        return True

    async def release_welcome(self, google_sub: str) -> None:
        from dataclasses import replace  # noqa: PLC0415

        row = self.rows.get(google_sub)
        if row is not None:
            self.rows[google_sub] = replace(row, welcome_sent_at=None)

    async def opt_out(self, token: str) -> bool:
        from dataclasses import replace  # noqa: PLC0415

        for sub, row in self.rows.items():
            if row.unsubscribe_token and row.unsubscribe_token == token:
                self.rows[sub] = replace(row, email_opt_out=True)
                return True
        return False

    async def delete(self, google_sub: str) -> bool:
        for key in [k for k in self.days if k[0] == google_sub]:
            del self.days[key]
        for key in [k for k in self.cap_hits if k[0] == google_sub]:
            del self.cap_hits[key]
        for key in [k for k in self.tools if k[0] == google_sub]:
            del self.tools[key]
        for key in [k for k in self.sent if k[0] == google_sub]:
            del self.sent[key]
        return self.rows.pop(google_sub, None) is not None


# ── Postgres ─────────────────────────────────────────────────────────────

#: The insert is the whole "is this a first sign-in" test. `xmax = 0` is the
#: standard Postgres way to ask whether a row from an upsert was INSERTed or
#: UPDATEd, and doing it in one statement is what makes the Resend hand-off
#: fire exactly once even when two sign-ins race: only one of them gets the
#: insert. A SELECT-then-INSERT here would add the address twice.
_UPSERT = """
INSERT INTO free_mcp_users (google_sub, email, first_seen, last_seen,
                            client_kind, unsubscribe_token)
VALUES ($1, $2, $3, $3, $4, $5)
ON CONFLICT (google_sub) DO UPDATE
   SET email = COALESCE(NULLIF(EXCLUDED.email, ''), free_mcp_users.email),
       last_seen = EXCLUDED.last_seen,
       client_kind = COALESCE(NULLIF(EXCLUDED.client_kind, ''),
                              free_mcp_users.client_kind)
RETURNING (xmax = 0) AS inserted
"""

_SELECT = """
SELECT google_sub, email, first_seen, last_seen, call_count, client_kind,
       resend_state, email_opt_out, unsubscribe_token, welcome_sent_at,
       cap_note_sent_at
  FROM free_mcp_users
 WHERE google_sub = $1
"""

_TOUCH = """
UPDATE free_mcp_users
   SET last_seen = $2, call_count = call_count + $3
 WHERE google_sub = $1
"""

_SET_RESEND = "UPDATE free_mcp_users SET resend_state = $2 WHERE google_sub = $1"

#: The "once per user, ever" guard on the welcome note, as ONE statement.
#: `WHERE welcome_sent_at IS NULL` plus the affected-row count is what makes
#: it a guard: two instances racing the same first sign-in both run this and
#: exactly one of them is told it won. Checking with a SELECT and then
#: stamping would send the note twice.
_MARK_WELCOME = """
UPDATE free_mcp_users SET welcome_sent_at = now()
 WHERE google_sub = $1 AND welcome_sent_at IS NULL
"""

_OPT_OUT = """
UPDATE free_mcp_users SET email_opt_out = true
 WHERE unsubscribe_token = $1 AND unsubscribe_token <> ''
"""

#: One row per account per UTC day. `ON CONFLICT ... searches + EXCLUDED` so
#: the write is a single statement whatever else is happening: this runs from
#: a best-effort background task and a read-modify-write would lose counts
#: under any concurrency at all.
_BUMP_DAY = """
INSERT INTO free_mcp_user_days (google_sub, day, searches)
VALUES ($1, $2, $3)
ON CONFLICT (google_sub, day) DO UPDATE
   SET searches = free_mcp_user_days.searches + EXCLUDED.searches
"""

#: The per-tool day row. Only written when the caller knows which tool
#: spent the searches, which is every signed-in tool call and nothing else.
#: Its only consumer is the cap note's choice of URL.
_BUMP_TOOL = """
INSERT INTO free_mcp_user_tools (google_sub, day, tool, searches)
VALUES ($1, $2, $3, $4)
ON CONFLICT (google_sub, day, tool) DO UPDATE
   SET searches = free_mcp_user_tools.searches + EXCLUDED.searches
"""

#: A refusal, not a search: `searches` is untouched, because the call this
#: describes spent nothing. Branch D counts DAYS with a hit, so the exact
#: number matters less than the row existing.
_BUMP_CAP_HIT = """
INSERT INTO free_mcp_user_days (google_sub, day, searches, cap_hits)
VALUES ($1, $2, 0, 1)
ON CONFLICT (google_sub, day) DO UPDATE
   SET cap_hits = free_mcp_user_days.cap_hits + 1
"""

#: Every cap-note trigger in ONE round trip. This runs from a best-effort
#: background task on a serverless instance that may be frozen at any point,
#: so five separate queries would be five chances to lose the answer -- and
#: four more Neon connections on the hottest path in the module.
_SIGNALS = """
SELECT
  (SELECT count(*) FROM free_mcp_user_days d
    WHERE d.google_sub = $1 AND d.cap_hits > 0 AND d.day >= $2) AS cap_days_7,
  (SELECT count(*) FROM free_mcp_user_days d
    WHERE d.google_sub = $1 AND d.searches > 0) AS active_days,
  (SELECT COALESCE(sum(d.searches), 0) FROM free_mcp_user_days d
    WHERE d.google_sub = $1 AND d.day >= $3) AS month_searches,
  (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
    WHERE t.google_sub = $1 AND t.day >= $3) AS month_tools,
  (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
    WHERE t.google_sub = $1 AND t.day >= $3
      AND t.tool ILIKE '%hotel%') AS month_hotels
"""

#: The cap note's guard, as ONE statement, for the same reason the welcome's
#: is: a SELECT then an UPDATE sends the note twice when two instances race.
#: Every frequency rule in the sequence file is a conjunct here -- 30 days
#: between notes, 72 hours after the welcome, a lane-wide daily ceiling, an
#: address, and no opt-out -- so there is exactly one place to read what can
#: stop a send and exactly one place a new rule has to be added.
_CLAIM_CAP_NOTE = """
UPDATE free_mcp_users SET cap_note_sent_at = $2
 WHERE google_sub = $1
   AND email <> ''
   AND email_opt_out = false
   AND (cap_note_sent_at IS NULL OR cap_note_sent_at < $3)
   AND (welcome_sent_at IS NULL OR welcome_sent_at < $4)
   AND (SELECT count(*) FROM free_mcp_users u
          WHERE u.cap_note_sent_at >= $5) < $6
"""

#: Rolled back only when Resend told us in so many words that it did not
#: take the message. A timeout is never rolled back: Resend may have
#: accepted it and a second copy is worse than a missing one.
_RELEASE_CAP_NOTE = (
    "UPDATE free_mcp_users SET cap_note_sent_at = NULL WHERE google_sub = $1"
)

_RELEASE_WELCOME = (
    "UPDATE free_mcp_users SET welcome_sent_at = NULL WHERE google_sub = $1"
)

#: The day-2 note's whole selection, in one statement.
#:
#: "Never searched" is asserted TWICE and deliberately: `call_count` is the
#: cheap counter on the user row and the day rows are what the daily read
#: and the cap note are computed from. Both are written from the same
#: best-effort background task, so either can be short by an increment a
#: frozen instance lost -- but a row that searched is nearly always non-zero
#: in at least one of them, and the cost of the belt is one index scan while
#: the cost of being wrong is telling an active user they have never used
#: the thing.
#:
#: `first_seen <= $1` is the 48-hour floor. `$2` is the optional ceiling
#: (pass 9 scopes the note to a 48-96 h window); NULL means no ceiling, and
#: then the ORDER BY is what stops a first run from mailing the whole
#: backlog: newest sign-ins first, 20 of them.
_DAY2_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen
  FROM free_mcp_users u
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND u.first_seen <= $1
   AND ($2::timestamptz IS NULL OR u.first_seen >= $2)
   AND u.call_count = 0
   AND NOT EXISTS (SELECT 1 FROM free_mcp_user_days d
                    WHERE d.google_sub = u.google_sub AND d.searches > 0)
   AND NOT EXISTS (SELECT 1 FROM sent_emails s
                    WHERE s.user_sub = u.google_sub AND s.kind = $3)
 ORDER BY u.first_seen DESC
 LIMIT $4
"""

#: The cap note's selection: the day cap refused this account TODAY, and we
#: have not already written to it today. The two older frequency guards --
#: 30 days between cap notes, never inside 72 hours of the welcome -- are
#: conjuncts here as well as in `_CLAIM_CAP_NOTE`, so that a dry run reports
#: the same set the real run would send to. The claim is still what makes it
#: safe; this only makes it honest.
_CAP_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen,
       (SELECT COALESCE(sum(d2.searches), 0) FROM free_mcp_user_days d2
         WHERE d2.google_sub = u.google_sub AND d2.day >= $2) AS month_searches,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $2) AS month_tools,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $2
           AND t.tool ILIKE '%hotel%') AS month_hotels
  FROM free_mcp_users u
  JOIN free_mcp_user_days d
    ON d.google_sub = u.google_sub AND d.day = $1 AND d.cap_hits > 0
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND (u.cap_note_sent_at IS NULL OR u.cap_note_sent_at < $3)
   AND (u.welcome_sent_at IS NULL OR u.welcome_sent_at < $4)
   AND NOT EXISTS (SELECT 1 FROM sent_emails s
                    WHERE s.user_sub = u.google_sub AND s.kind = $5)
 ORDER BY month_searches DESC
 LIMIT $6
"""

#: The day-7 note's selection. `$1`/`$2` are the 7-day floor and the 10-day
#: ceiling on `first_seen`; the sum is over the seven days FOLLOWING the
#: sign-in, because the copy's number is "your first week" and not an
#: all-time counter. `NOT EXISTS` on a cap hit is the other half of the
#: trigger: somebody the cap refused belongs to the cap note, not here.
_DAY7_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen,
       (SELECT COALESCE(sum(d.searches), 0) FROM free_mcp_user_days d
         WHERE d.google_sub = u.google_sub
           AND d.day >= u.first_seen::date
           AND d.day <= (u.first_seen + interval '7 days')::date)
         AS first_week_searches,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $5) AS month_tools,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $5
           AND t.tool ILIKE '%hotel%') AS month_hotels
  FROM free_mcp_users u
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND u.first_seen <= $1
   AND u.first_seen >= $2
   AND NOT EXISTS (SELECT 1 FROM free_mcp_user_days c
                    WHERE c.google_sub = u.google_sub AND c.cap_hits > 0)
   AND NOT EXISTS (SELECT 1 FROM sent_emails s
                    WHERE s.user_sub = u.google_sub AND s.kind = $3)
   AND (SELECT COALESCE(sum(d.searches), 0) FROM free_mcp_user_days d
         WHERE d.google_sub = u.google_sub
           AND d.day >= u.first_seen::date
           AND d.day <= (u.first_seen + interval '7 days')::date) >= $6
 ORDER BY first_week_searches DESC
 LIMIT $4
"""

#: The dormant note's selection. Signed in 14 to 17 days ago and one
#: search or none -- asserted on BOTH counters, the same belt-and-braces
#: the day-2 note uses, because "you went quiet" is the one sentence an
#: active account must never be sent.
_DORMANT_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen
  FROM free_mcp_users u
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND u.first_seen <= $1
   AND u.first_seen >= $2
   AND u.call_count <= $5
   AND COALESCE((SELECT sum(d.searches) FROM free_mcp_user_days d
                  WHERE d.google_sub = u.google_sub), 0) <= $5
   AND NOT EXISTS (SELECT 1 FROM sent_emails s
                    WHERE s.user_sub = u.google_sub AND s.kind = $3)
 ORDER BY u.first_seen DESC
 LIMIT $4
"""

#: The welcome RETRY's selection: an account whose welcome never got
#: stamped. `welcome_sent_at IS NULL` is the whole condition that matters --
#: the sign-in callback stamps it BEFORE it calls Resend, so a NULL here
#: means the callback did not get as far as the claim, which is exactly
#: what a frozen serverless instance leaves behind. Ten minutes old so a
#: retry can never race the callback, fourteen days at most so nobody is
#: welcomed to a server they signed in to last month.
_WELCOME_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen
  FROM free_mcp_users u
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND u.first_seen <= $1
   AND u.first_seen >= $2
   AND NOT EXISTS (SELECT 1 FROM sent_emails s
                    WHERE s.user_sub = u.google_sub AND s.kind = $3)
   AND (
        u.welcome_sent_at IS NULL
        OR ($5::timestamptz IS NOT NULL
            AND u.welcome_sent_at >= $5
            AND NOT EXISTS (SELECT 1 FROM sent_emails w
                             WHERE w.user_sub = u.google_sub
                               AND w.kind = $6
                               AND w.resend_id <> ''))
       )
 ORDER BY u.first_seen DESC
 LIMIT $4
"""

#: The cap note's retry. Same shape as the welcome's and the same reason:
#: `cap_note_sent_at` is the CLAIM, written before the send, so a claim
#: with no confirmed ledger row for that day is a note somebody was
#: supposed to get and did not. Bounded to three days, and to one retry
#: per lost note by the `cap-retry:<day>` row.
_CAP_RETRY_CANDIDATES = """
SELECT u.google_sub, u.email, u.unsubscribe_token, u.first_seen,
       u.cap_note_sent_at,
       (SELECT COALESCE(sum(d2.searches), 0) FROM free_mcp_user_days d2
         WHERE d2.google_sub = u.google_sub AND d2.day >= $2) AS month_searches,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $2) AS month_tools,
       (SELECT COALESCE(sum(t.searches), 0) FROM free_mcp_user_tools t
         WHERE t.google_sub = u.google_sub AND t.day >= $2
           AND t.tool ILIKE '%hotel%') AS month_hotels
  FROM free_mcp_users u
 WHERE u.email <> ''
   AND u.email_opt_out = false
   AND u.cap_note_sent_at IS NOT NULL
   AND u.cap_note_sent_at >= $1
   AND NOT EXISTS (
         SELECT 1 FROM sent_emails s
          WHERE s.user_sub = u.google_sub
            AND s.kind = 'cap:' || to_char(u.cap_note_sent_at AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD')
            AND s.resend_id <> '')
   AND NOT EXISTS (
         SELECT 1 FROM sent_emails r
          WHERE r.user_sub = u.google_sub
            AND r.kind = 'cap-retry:' || to_char(
                  u.cap_note_sent_at AT TIME ZONE 'UTC', 'YYYY-MM-DD'))
 ORDER BY month_searches DESC
 LIMIT $3
"""

#: The claim. `ON CONFLICT DO NOTHING` plus the affected-row count is the
#: whole idempotency story for both notes: two overlapping runs of the job
#: both execute this and exactly one is told it won. Inserted BEFORE the
#: Resend call, never after -- "we did not see an error" is not a guard.
_CLAIM_SENT_EMAIL = """
INSERT INTO sent_emails (user_sub, kind, sent_at)
VALUES ($1, $2, $3)
ON CONFLICT (user_sub, kind) DO NOTHING
"""

_FINISH_SENT_EMAIL = """
UPDATE sent_emails SET resend_id = $3 WHERE user_sub = $1 AND kind = $2
"""

#: Deleted only when Resend answered and said no. A timeout keeps the row:
#: the message may have gone out, and a second copy of an unasked-for email
#: is the failure that costs a sending domain.
_RELEASE_SENT_EMAIL = "DELETE FROM sent_emails WHERE user_sub = $1 AND kind = $2"

_DELETE_SENT_EMAILS = "DELETE FROM sent_emails WHERE user_sub = $1"

_DELETE_TOOLS = "DELETE FROM free_mcp_user_tools WHERE google_sub = $1"

_DELETE_DAYS = "DELETE FROM free_mcp_user_days WHERE google_sub = $1"

_DELETE = "DELETE FROM free_mcp_users WHERE google_sub = $1"


#: The reporting read behind `GET /metrics/signins`.
#:
#: One query, not one per user: the route runs on a serverless function and
#: a per-account round trip to Neon is one connection per signed-in user.
#: The window sum is a grouped subquery rather than a correlated one so the
#: day rows are scanned once, and `sent_emails` is joined on the literal
#: day-2 kind because that ledger holds every note kind and only this one
#: is a per-account, once-ever fact.
#:
#: `ORDER BY first_seen DESC` with a LIMIT means a clipped read keeps the
#: NEWEST accounts, which is what the `recent` block wants; the route says
#: `truncated` when the limit bites so a total over a clipped set is never
#: read as a total.
_SIGNIN_RECORDS = """
SELECT u.google_sub, u.email, u.first_seen, u.last_seen, u.call_count,
       u.client_kind, u.email_opt_out, u.welcome_sent_at, u.cap_note_sent_at,
       s.sent_at AS day2_sent_at,
       COALESCE(k.kinds, ARRAY[]::text[]) AS kinds_sent,
       COALESCE(wl.resend_id, '') AS welcome_resend_id,
       COALESCE(w.searches, 0) AS calls_window,
       COALESCE(w.cap_hits, 0) AS cap_hits_window
  FROM free_mcp_users u
  LEFT JOIN sent_emails s
    ON s.user_sub = u.google_sub AND s.kind = $1
  LEFT JOIN (SELECT s2.user_sub, array_agg(s2.kind) AS kinds
               FROM sent_emails s2
              GROUP BY s2.user_sub) k
    ON k.user_sub = u.google_sub
  LEFT JOIN sent_emails wl
    ON wl.user_sub = u.google_sub AND wl.kind = $4
  LEFT JOIN (SELECT d.google_sub,
                    sum(d.searches) AS searches,
                    sum(d.cap_hits) AS cap_hits
               FROM free_mcp_user_days d
              WHERE d.day >= $2
              GROUP BY d.google_sub) w
    ON w.google_sub = u.google_sub
 ORDER BY u.first_seen DESC
 LIMIT $3
"""


class PostgresFreeUserStore:
    available = True

    def __init__(self, dsn: str, connect_timeout: float = 8.0) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    async def _connect(self):
        import asyncpg  # noqa: PLC0415 -- lazy on purpose, see module header

        return await asyncpg.connect(self._dsn, timeout=self._connect_timeout)

    async def _run(self, fn):
        try:
            conn = await self._connect()
        except Exception as exc:  # noqa: BLE001 - asyncpg raises many shapes
            raise UserStoreError(f"could not reach the user store: {exc}") from exc
        try:
            return await fn(conn)
        except UserStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserStoreError(f"user store operation failed: {exc}") from exc
        finally:
            await conn.close()

    async def ping(self, timeout: float = 0.0) -> bool:
        """One `SELECT 1`. Raises `UserStoreError` if the store is unreachable.

        The sign-in feature reads TWO stores -- this one and
        `oauthstore` -- and on 2026-09-09 one of them was dead while
        `/health` reported `ok`. Probing both is the point: a missing driver
        or a bad DATABASE_URL breaks them together, but a migration that ran
        on one database and not the other breaks exactly one.
        """
        limit = timeout if timeout and timeout > 0 else self._connect_timeout
        original, self._connect_timeout = self._connect_timeout, limit
        try:
            await self._run(lambda conn: conn.fetchval("SELECT 1"))
        finally:
            self._connect_timeout = original
        return True

    async def upsert(
        self,
        google_sub: str,
        email: str,
        *,
        client_kind: str = "",
        now: float | None = None,
    ) -> bool:
        stamp = _dt(now if now is not None else time.time())

        token = new_unsubscribe_token()

        async def go(conn):
            return await conn.fetchval(
                _UPSERT, google_sub, email or "", stamp, client_kind or "", token
            )

        return bool(await self._run(go))

    async def get(self, google_sub: str) -> FreeUser | None:
        async def go(conn):
            return await conn.fetchrow(_SELECT, google_sub)

        row = await self._run(go)
        if row is None:
            return None
        return FreeUser(
            google_sub=row["google_sub"],
            email=row["email"] or "",
            first_seen=_epoch(row["first_seen"]),
            last_seen=_epoch(row["last_seen"]),
            call_count=int(row["call_count"] or 0),
            client_kind=row["client_kind"] or "",
            resend_state=row["resend_state"] or RESEND_PENDING,
            email_opt_out=bool(row["email_opt_out"]),
            unsubscribe_token=row["unsubscribe_token"] or "",
            welcome_sent_at=(
                _epoch(row["welcome_sent_at"])
                if row["welcome_sent_at"] is not None
                else None
            ),
            cap_note_sent_at=(
                _epoch(row["cap_note_sent_at"])
                if row["cap_note_sent_at"] is not None
                else None
            ),
        )

    async def signin_records(
        self,
        now: float | None = None,
        *,
        hours: int = 24,
        limit: int = SIGNIN_STATS_MAX_ROWS,
    ) -> list[SigninRecord]:
        moment = now if now is not None else time.time()
        first_day = _date(moment - max(1, int(hours)) * 3600.0)

        async def go(conn):
            return await conn.fetch(
                _SIGNIN_RECORDS, DAY2_KIND, first_day, max(0, limit), WELCOME_KIND
            )

        return [
            SigninRecord(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                first_seen=_epoch(row["first_seen"]),
                last_seen=_epoch(row["last_seen"]),
                calls_total=int(row["call_count"] or 0),
                calls_window=int(row["calls_window"] or 0),
                cap_hits_window=int(row["cap_hits_window"] or 0),
                client_kind=row["client_kind"] or "",
                welcome_sent_at=(
                    _epoch(row["welcome_sent_at"])
                    if row["welcome_sent_at"] is not None
                    else None
                ),
                day2_sent_at=(
                    _epoch(row["day2_sent_at"])
                    if row["day2_sent_at"] is not None
                    else None
                ),
                cap_note_sent_at=(
                    _epoch(row["cap_note_sent_at"])
                    if row["cap_note_sent_at"] is not None
                    else None
                ),
                opted_out=bool(row["email_opt_out"]),
                kinds_sent=tuple(row["kinds_sent"] or ()),
                welcome_resend_id=row["welcome_resend_id"] or "",
            )
            for row in await self._run(go)
        ]

    async def touch(
        self,
        google_sub: str,
        spent: int = 1,
        now: float | None = None,
        tool: str = "",
    ) -> None:
        moment = now if now is not None else time.time()
        stamp = _dt(moment)
        day = _date(moment)
        amount = max(0, spent)

        async def go(conn):
            # Two statements -- three with a tool -- on ONE connection: this
            # is the hottest write in the module (it runs once per signed-in
            # tool call) and opening a second Neon connection for the day row
            # would double its cost.
            await conn.execute(_TOUCH, google_sub, stamp, amount)
            await conn.execute(_BUMP_DAY, google_sub, day, amount)
            if tool and amount:
                await conn.execute(_BUMP_TOOL, google_sub, day, tool, amount)

        await self._run(go)

    async def note_cap_hit(self, google_sub: str, now: float | None = None) -> None:
        """Record that the day cap refused this account today.

        Branch D of the cap note is "a scan stopped before it finished",
        twice in a week. Nothing else in the system remembers that: the
        Upstash counter knows the number but not the person, and the day row
        counts searches, which a refusal is not.
        """
        day = _date(now if now is not None else time.time())

        async def go(conn):
            await conn.execute(_BUMP_CAP_HIT, google_sub, day)

        await self._run(go)

    async def cap_signals(
        self, google_sub: str, now: float | None = None
    ) -> CapSignals:
        moment = now if now is not None else time.time()
        week_start = _date(moment - 6 * 86400.0)
        month_start = _date(moment).replace(day=1)

        async def go(conn):
            return await conn.fetchrow(_SIGNALS, google_sub, week_start, month_start)

        row = await self._run(go)
        if row is None:  # pragma: no cover - the query always returns one row
            return CapSignals(days_left_in_month=days_left_in_month(moment))
        return CapSignals(
            cap_days_7=int(row["cap_days_7"] or 0),
            active_days_ever=int(row["active_days"] or 0),
            month_searches=int(row["month_searches"] or 0),
            month_tool_searches=int(row["month_tools"] or 0),
            month_hotel_searches=int(row["month_hotels"] or 0),
            days_left_in_month=days_left_in_month(moment),
        )

    async def claim_cap_note(
        self, google_sub: str, now: float | None = None
    ) -> bool:
        moment = now if now is not None else time.time()
        stamp = _dt(moment)
        since = _dt(moment - CAP_NOTE_MIN_GAP_DAYS * 86400.0)
        after_welcome = _dt(moment - CAP_NOTE_WELCOME_GAP_HOURS * 3600.0)
        midnight = _dt(moment).replace(hour=0, minute=0, second=0, microsecond=0)

        async def go(conn):
            return await conn.execute(
                _CLAIM_CAP_NOTE,
                google_sub,
                stamp,
                since,
                after_welcome,
                midnight,
                CAP_NOTE_LANE_DAY_CAP,
            )

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def release_cap_note(self, google_sub: str) -> None:
        async def go(conn):
            await conn.execute(_RELEASE_CAP_NOTE, google_sub)

        await self._run(go)

    async def day2_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        min_hours: int = DAY2_MIN_HOURS,
        max_hours: int | None = None,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        floor = _dt(moment - min_hours * 3600.0)
        ceiling = _dt(moment - max_hours * 3600.0) if max_hours else None

        async def go(conn):
            return await conn.fetch(
                _DAY2_CANDIDATES, floor, ceiling, DAY2_KIND, max(0, limit)
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
            )
            for row in await self._run(go)
        ]

    async def cap_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        today = _date(moment)
        month_start = today.replace(day=1)
        since = _dt(moment - CAP_NOTE_MIN_GAP_DAYS * 86400.0)
        after_welcome = _dt(moment - CAP_NOTE_WELCOME_GAP_HOURS * 3600.0)
        kind = cap_kind(moment)

        async def go(conn):
            return await conn.fetch(
                _CAP_CANDIDATES,
                today,
                month_start,
                since,
                after_welcome,
                kind,
                max(0, limit),
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
                month_searches=int(row["month_searches"] or 0),
                month_tool_searches=int(row["month_tools"] or 0),
                month_hotel_searches=int(row["month_hotels"] or 0),
            )
            for row in await self._run(go)
        ]

    async def day7_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        floor = _dt(moment - DAY7_MIN_HOURS * 3600.0)
        ceiling = _dt(moment - DAY7_MAX_HOURS * 3600.0)
        month_start = _date(moment).replace(day=1)

        async def go(conn):
            return await conn.fetch(
                _DAY7_CANDIDATES,
                floor,
                ceiling,
                DAY7_KIND,
                max(0, limit),
                month_start,
                DAY7_MIN_SEARCHES,
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
                first_week_searches=int(row["first_week_searches"] or 0),
                month_tool_searches=int(row["month_tools"] or 0),
                month_hotel_searches=int(row["month_hotels"] or 0),
            )
            for row in await self._run(go)
        ]

    async def dormant_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        floor = _dt(moment - DORMANT_MIN_HOURS * 3600.0)
        ceiling = _dt(moment - DORMANT_MAX_HOURS * 3600.0)

        async def go(conn):
            return await conn.fetch(
                _DORMANT_CANDIDATES,
                floor,
                ceiling,
                DORMANT_KIND,
                max(0, limit),
                DORMANT_MAX_SEARCHES,
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
            )
            for row in await self._run(go)
        ]

    async def welcome_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
        ledger_since: float = 0.0,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        floor = _dt(moment - WELCOME_RETRY_MIN_MINUTES * 60.0)
        ceiling = _dt(moment - WELCOME_RETRY_MAX_DAYS * 86400.0)
        since = _dt(ledger_since) if ledger_since else None

        async def go(conn):
            return await conn.fetch(
                _WELCOME_CANDIDATES,
                floor,
                ceiling,
                WELCOME_RETRY_KIND,
                max(0, limit),
                since,
                WELCOME_KIND,
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
            )
            for row in await self._run(go)
        ]

    async def cap_retry_candidates(
        self,
        now: float | None = None,
        *,
        limit: int = SEQUENCE_LANE_DAY_CAP,
    ) -> list[SequenceCandidate]:
        moment = now if now is not None else time.time()
        floor = _dt(moment - CAP_RETRY_MAX_DAYS * 86400.0)
        month_start = _date(moment).replace(day=1)

        async def go(conn):
            return await conn.fetch(
                _CAP_RETRY_CANDIDATES, floor, month_start, max(0, limit)
            )

        return [
            SequenceCandidate(
                google_sub=row["google_sub"],
                email=row["email"] or "",
                unsubscribe_token=row["unsubscribe_token"] or "",
                first_seen=_epoch(row["first_seen"]),
                month_searches=int(row["month_searches"] or 0),
                month_tool_searches=int(row["month_tools"] or 0),
                month_hotel_searches=int(row["month_hotels"] or 0),
                ledger_kind=cap_retry_kind(_epoch(row["cap_note_sent_at"])),
            )
            for row in await self._run(go)
        ]

    async def claim_sent_email(
        self, google_sub: str, kind: str, now: float | None = None
    ) -> bool:
        stamp = _dt(now if now is not None else time.time())

        async def go(conn):
            return await conn.execute(_CLAIM_SENT_EMAIL, google_sub, kind, stamp)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def finish_sent_email(
        self, google_sub: str, kind: str, resend_id: str
    ) -> None:
        async def go(conn):
            await conn.execute(_FINISH_SENT_EMAIL, google_sub, kind, resend_id or "")

        await self._run(go)

    async def release_sent_email(self, google_sub: str, kind: str) -> None:
        async def go(conn):
            await conn.execute(_RELEASE_SENT_EMAIL, google_sub, kind)

        await self._run(go)

    async def set_resend_state(self, google_sub: str, state: str) -> None:
        async def go(conn):
            await conn.execute(_SET_RESEND, google_sub, state)

        await self._run(go)

    async def mark_welcome_sent(self, google_sub: str) -> bool:
        async def go(conn):
            return await conn.execute(_MARK_WELCOME, google_sub)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def release_welcome(self, google_sub: str) -> None:
        async def go(conn):
            await conn.execute(_RELEASE_WELCOME, google_sub)

        await self._run(go)

    async def opt_out(self, token: str) -> bool:
        if not token:
            return False

        async def go(conn):
            return await conn.execute(_OPT_OUT, token)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")

    async def delete(self, google_sub: str) -> bool:
        async def go(conn):
            await conn.execute(_DELETE_DAYS, google_sub)
            await conn.execute(_DELETE_TOOLS, google_sub)
            await conn.execute(_DELETE_SENT_EMAILS, google_sub)
            return await conn.execute(_DELETE, google_sub)

        status = await self._run(go)
        return str(status).rsplit(" ", 1)[-1].strip() not in ("", "0")


def build_user_store(dsn: str | None = None) -> FreeUserStore:
    """The store this deployment should use, or a NullFreeUserStore.

    Never raises for a missing DATABASE_URL: that is the normal state of this
    server today, and it must boot and serve anonymous callers exactly as it
    does now.
    """
    dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
    if not dsn:
        return NullFreeUserStore()
    return PostgresFreeUserStore(dsn)
