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

plus `free_mcp_user_days`, one row per account per UTC day, which is what
lets the 08:33Z read say how many signed-in users there were and what they
spent -- and what the phase-2 cap note's triggers are evaluated over.

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


def utc_day(epoch: float) -> str:
    """`YYYY-MM-DD` in UTC -- the day key both stores agree on.

    UTC because the fair-use day cap rolls over at 00:00 UTC, and a usage row
    on a different clock would disagree with the counter that refused the
    call it is describing.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


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


def new_unsubscribe_token() -> str:
    import secrets  # noqa: PLC0415 -- one call site

    return secrets.token_urlsafe(UNSUBSCRIBE_TOKEN_BYTES)


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
        self, google_sub: str, spent: int = 1, now: float | None = None
    ) -> None: ...

    async def set_resend_state(self, google_sub: str, state: str) -> None: ...

    async def mark_welcome_sent(self, google_sub: str) -> bool: ...

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
        self, google_sub: str, spent: int = 1, now: float | None = None
    ) -> None:
        return None

    async def set_resend_state(self, google_sub: str, state: str) -> None:
        return None

    async def mark_welcome_sent(self, google_sub: str) -> bool:
        return False

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
        self, google_sub: str, spent: int = 1, now: float | None = None
    ) -> None:
        row = self.rows.get(google_sub)
        if row is None:
            return
        from dataclasses import replace  # noqa: PLC0415

        stamp = now if now is not None else time.time()
        self.rows[google_sub] = replace(
            row, last_seen=stamp, call_count=row.call_count + max(0, spent)
        )
        key = (google_sub, utc_day(stamp))
        self.days[key] = self.days.get(key, 0) + max(0, spent)

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
       resend_state, email_opt_out, unsubscribe_token, welcome_sent_at
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
        )

    async def touch(
        self, google_sub: str, spent: int = 1, now: float | None = None
    ) -> None:
        moment = now if now is not None else time.time()
        stamp = _dt(moment)
        day = utc_day(moment)

        async def go(conn):
            # Two statements, one connection: this is the hottest write in
            # the module (it runs once per signed-in tool call) and opening a
            # second Neon connection for the day row would double its cost.
            await conn.execute(_TOUCH, google_sub, stamp, max(0, spent))
            await conn.execute(_BUMP_DAY, google_sub, day, max(0, spent))

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
