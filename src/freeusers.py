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

_DELETE_TOOLS = "DELETE FROM free_mcp_user_tools WHERE google_sub = $1"

_DELETE_DAYS = "DELETE FROM free_mcp_user_days WHERE google_sub = $1"

_DELETE = "DELETE FROM free_mcp_users WHERE google_sub = $1"


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
