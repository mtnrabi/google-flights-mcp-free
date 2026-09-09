"""
Counter storage, behind an interface, because the deployment target decides
which one is correct.

Running as a container, an in-process dict is fine: one process, one set of
counters, and the JSONL log on disk to rebuild from after a restart.

Running on Vercel, both of those assumptions are false:

* The filesystem is read-only apart from `/tmp`, which is per-instance
  scratch with no durability guarantee. A log file written there is not a
  log, it is a temporary buffer that silently disappears.
* Fluid compute shares one instance across concurrent invocations and scales
  instances up and down freely. In-process counters therefore fragment across
  instances and reset on recycle. They do not error -- they just return a
  number that is smaller than the truth, which is the worst way for a spend
  guard to fail.

So the daily backend-call budget is only enforceable with a shared store.
`MemoryCounterStore.durable` is False and the server says so out loud at
startup rather than pretending the guard is armed.

Upstash is reached over its REST API rather than the Redis wire protocol on
purpose: serverless invocations are short-lived and a connection-pooling
client is the wrong shape for them, plus it keeps `redis` out of the
dependency list.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from .fair_use import GATEWAY_KINDS
from .policy import TIER_LLM_HOST, TIER_LOCAL_CLIENT, TIER_UNKNOWN

logger = logging.getLogger(__name__)

# Imported from policy rather than re-listed, so adding a tier there cannot
# silently drop it from /metrics. Redis has no cheap "list my keys", so the
# per-tier read has to know what to ask for.
KNOWN_TIERS = (TIER_LLM_HOST, TIER_LOCAL_CLIENT, TIER_UNKNOWN)

WINDOW_HOURS = 24

# Hourly buckets are retained far longer than the 24h spend window needs,
# because they are also the time series behind /metrics/calls. 35 days covers
# a monthly reconciliation against Lulu's numbers with room to spare.
RETENTION_HOURS = 35 * 24
BUCKET_TTL_SECONDS = RETENTION_HOURS * 3600

# Cap on how far back a single query may reach, so one request cannot ask for
# an unbounded number of keys.
MAX_QUERY_HOURS = RETENTION_HOURS

KEY_PREFIX = "mcpads"

# Per-hour counters. `b` = backend (Lambda) calls, `t` = MCP tool calls.
FIELD_BACKEND = "b"
FIELD_TOOL = "t"

# Per-hour breakdowns, one Redis HASH each rather than one key per member.
#
# A hash, not a key per tool, because the read has to enumerate the members and
# Redis has no cheap "list my keys". A hardcoded list of tool names would have
# worked for tools and silently dropped the fifth one somebody adds; it could
# never have worked for client fingerprints, whose set is unbounded and unknown
# in advance. One shape for both is one shape to reason about.
#
#   mcpads:h:<YYYYMMDDHH>:tool:t   {tool_name -> tool calls}
#   mcpads:h:<YYYYMMDDHH>:tool:b   {tool_name -> backend calls}
#   mcpads:h:<YYYYMMDDHH>:fp:t     {fingerprint -> tool calls}
#   mcpads:h:<YYYYMMDDHH>:fp:b     {fingerprint -> backend calls}
#
# Same TTL as the existing hourly keys, so retention is one number in one place.
GROUP_TOOL = "tool"
GROUP_FINGERPRINT = "fp"

# The route histogram. Per DAY, not per hour: the question it answers is "what
# was that 04:00Z client actually searching for", and a day is the natural unit
# for a scheduled job. One hash per counted field, same shape as the hourly
# breakdowns above:
#
#   mcpads:d:<YYYYMMDD>:route:t   {<fp>|<from>|<to> -> tool calls}
#   mcpads:d:<YYYYMMDD>:route:c   {<fp>|<from>|<to> -> requested combinations}
#   mcpads:d:<YYYYMMDD>:route:b   {<fp>|<from>|<to> -> backend calls}
#
# `c` (combinations) is carried here and nowhere else because a route's cost is
# not its tool-call count: one call for a 15-date range is fifteen searches.
GROUP_ROUTE = "route"
FIELD_COMBOS = "c"

# The member is `<fingerprint>|<origin>|<destination>`. Nothing else -- dates
# and currency stay on the log line, because putting them in the key would make
# the member set unbounded and blow the cap below on the first date sweep.
ROUTE_SEPARATOR = "|"

# Hard cap on distinct routes stored per day. A batch client sweeping every
# origin x destination pair could otherwise write an unbounded hash into a store
# nobody is watching. Past the cap, routes already being counted keep counting
# and new ones are dropped -- the heavy hitters, which is what the histogram is
# for, are the ones already in it.
ROUTE_KEYS_PER_DAY = 500

# How many rows /metrics/calls names, and over how many days. Two days rather
# than one so a 24h read always contains a whole 04:00Z burst regardless of when
# it is read.
TOP_ROUTES = 20
ROUTE_WINDOW_DAYS = 2

# Fair-use counters, one per client per window. Plain counters rather than
# hash members, because unlike the histograms above these are READ on the hot
# path -- a tool call asks for exactly its own caller's two numbers, which is
# one MGET, not an HGETALL of every client that called today.
#
#   mcpads:fu:d:<YYYYMMDD>:<client_key>   backend calls, this UTC day
#   mcpads:fu:m:<YYYYMM>:<client_key>     backend calls, this calendar month
#   mcpads:fu:blocked:<YYYYMMDD>          tool calls refused today, all clients
#
# The client key is NOT the fingerprint above: it is sha256(x-forwarded-for +
# user-agent) with no session id, because the client this cap exists for opens
# a new session per request. See fair_use.py.
FAIR_USE_PREFIX = f"{KEY_PREFIX}:fu"

# Two days, not one: a counter only has to outlive the UTC day it belongs to,
# plus enough slack that a call landing either side of midnight still finds it.
FAIR_USE_DAY_TTL_SECONDS = 2 * 86400

# 35 days covers any calendar month plus the same slack, and matches the
# retention the hourly buckets already use.
FAIR_USE_MONTH_TTL_SECONDS = 35 * 86400

# Refusals per client over a ROLLING hour, for the hard escalation:
#
#   mcpads:fu:r:<YYYYMMDDHHm>:<client_key>   soft refusals in one 10-min slot
#   mcpads:fu:hard:<YYYYMMDD>                429s issued today, all clients
#
# Rolling rather than a fixed clock hour, because a fixed hour is escapable by
# arithmetic: a caller refused 19 times at 10:59 starts again from zero at
# 11:00, and a loop running at 20 calls a minute crosses that boundary sixty
# times a day. Six ten-minute slots, summed, give a window between 50 and 60
# minutes wide -- never MORE than an hour, so nothing is ever blocked on a
# refusal older than the window claims -- for one MGET of six keys.
#
# The slot label is the hour label plus the tens-of-minutes digit, so slots
# sort and expire the same way every other key here does.
FAIR_USE_REFUSAL_SLOT_SECONDS = 600
FAIR_USE_REFUSAL_SLOTS = 6

# Two hours: one window plus the same kind of slack the day counter has, so a
# slot is still readable while it is still inside somebody's rolling hour and
# is gone shortly after.
FAIR_USE_REFUSAL_TTL_SECONDS = 2 * 3600


# Per-fingerprint metadata, outside the hourly buckets so "first seen" can
# predate the window being asked about: mcpads:fp:<fingerprint> ->
# {first, last, tier}. The fingerprint is a truncated hash; the IP, user agent
# and session id it was derived from are never written anywhere.
FP_META_PREFIX = f"{KEY_PREFIX}:fp"

# How many callers /metrics/calls names. The question this answers is "is the
# traffic one scheduled client or many people" -- 79.3% of all backend calls to
# date landed in a single 04:00Z hour -- and twenty rows settles it.
TOP_CLIENTS = 20

# The window the client breakdown covers, regardless of how many hours of the
# call series were asked for. A day is what makes "who is calling us right now"
# readable; a 35-day merge of unbounded fingerprint sets is not.
CLIENT_WINDOW_HOURS = 24


def _hour_bucket(ts: float) -> str:
    return time.strftime("%Y%m%d%H", time.gmtime(ts))


def _bucket_range(now: float, hours: int) -> list[str]:
    """Hour labels from oldest to newest, inclusive of the current hour."""
    hours = max(1, min(int(hours), MAX_QUERY_HOURS))
    return [_hour_bucket(now - h * 3600) for h in range(hours - 1, -1, -1)]


def _day_bucket(ts: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts))


def _month_bucket(ts: float) -> str:
    return time.strftime("%Y%m", time.gmtime(ts))


def fair_use_keys(client_key: str, ts: float) -> tuple[str, str]:
    """The day and month counter keys for one client, in that order.

    One function so the writer and the reader can never disagree about the
    shape -- the failure mode of two literals is a cap that counts into a key
    nobody reads and therefore never fires.
    """
    return (
        f"{FAIR_USE_PREFIX}:d:{_day_bucket(ts)}:{client_key}",
        f"{FAIR_USE_PREFIX}:m:{_month_bucket(ts)}:{client_key}",
    )


def fair_use_blocked_key(ts: float) -> str:
    return f"{FAIR_USE_PREFIX}:blocked:{_day_bucket(ts)}"


def _refusal_slot(ts: float) -> str:
    """`YYYYMMDDHHm`, where m is the tens-of-minutes digit (0-5)."""
    moment = time.gmtime(ts)
    return time.strftime("%Y%m%d%H", moment) + str(moment.tm_min // 10)


def fair_use_refusal_key(client_key: str, ts: float) -> str:
    """The slot one refusal at `ts` is counted into."""
    return f"{FAIR_USE_PREFIX}:r:{_refusal_slot(ts)}:{client_key}"


def fair_use_refusal_keys(client_key: str, now: float) -> list[str]:
    """Every slot covering this client's rolling hour, newest first.

    Six slots of ten minutes: the current one, which is between 0 and 10
    minutes old, plus the five before it. The sum therefore covers at least
    the last 50 minutes and never more than the last 60 -- it can understate a
    burst that straddles a slot edge, and it can never block a caller on a
    refusal that is over an hour old, which is the direction to be wrong in.
    """
    return [
        fair_use_refusal_key(
            client_key, now - slot * FAIR_USE_REFUSAL_SLOT_SECONDS
        )
        for slot in range(FAIR_USE_REFUSAL_SLOTS)
    ]


def fair_use_hard_key(ts: float) -> str:
    """429s issued today, all clients. The only trace a hard block leaves."""
    return f"{FAIR_USE_PREFIX}:hard:{_day_bucket(ts)}"


#: One hash per UTC day, `<kind>:<field>` -> count, where field is one of
#: FIELD_TOOL / FIELD_BACKEND / FIELD_BLOCKED. A hash rather than three
#: counters per kind because it is read whole, once, by /metrics/calls and
#: never on the hot path, and because the set of kinds is fixed and tiny.
#:
#: This exists so a refusal can be read. "The cap blocked 12 calls today" is
#: two different facts: twelve refusals of a runaway script is the feature,
#: twelve refusals of Claude users sharing one pooled key is a regression, and
#: nothing else this server records can tell them apart.
FAIR_USE_KIND_FIELDS = (FIELD_TOOL, FIELD_BACKEND)
FIELD_BLOCKED = "x"


def fair_use_kind_key(ts: float) -> str:
    return f"{FAIR_USE_PREFIX}:kind:{_day_bucket(ts)}"


def _day_range(now: float, days: int) -> list[str]:
    """UTC day labels from oldest to newest, inclusive of today."""
    days = max(1, min(int(days), RETENTION_HOURS // 24))
    return [_day_bucket(now - d * 86400) for d in range(days - 1, -1, -1)]


def _recent_buckets(now: float) -> list[str]:
    return [_hour_bucket(now - hours * 3600) for hours in range(WINDOW_HOURS)]


class CounterStore(Protocol):
    """Totals, per-tier counters, and a rolling 24h backend-call window."""

    durable: bool

    async def bump(
        self,
        tier: str,
        fields: dict[str, int],
        backend_calls: int,
        ts: float,
        tool: str | None = None,
        fingerprint: str | None = None,
        route: str | None = None,
        combinations: int = 0,
        fair_use_key: str | None = None,
        fair_use_blocked: bool = False,
    ) -> None: ...

    async def backend_calls_in_window(self, now: float) -> int: ...

    async def fair_use_usage(
        self, client_key: str, now: float
    ) -> tuple[int, int]: ...

    async def fair_use_refusals(self, client_key: str, now: float) -> int: ...

    async def record_hard_block(self, client_key: str, now: float) -> None: ...

    async def fair_use_hard_today(self, now: float) -> int: ...

    async def fair_use_blocked_today(self, now: float) -> int: ...

    async def fair_use_kind_counts(
        self, now: float
    ) -> dict[str, dict[str, int]]: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]: ...

    async def tool_series(self, now: float, hours: int) -> dict[str, dict[str, int]]: ...

    async def tool_series_by_day(
        self, now: float, days: int
    ) -> dict[str, dict[str, int]]: ...

    async def top_clients(
        self, now: float, hours: int, limit: int
    ) -> list[dict[str, Any]]: ...

    async def top_routes(
        self, now: float, days: int, limit: int
    ) -> list[dict[str, Any]]: ...


class MemoryCounterStore:
    """In-process counters. Correct for a container, wrong for serverless."""

    durable = False

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        self._by_tier: dict[str, dict[str, int]] = {}
        self._buckets: dict[str, dict[str, int]] = {}
        # (hour, group) -> member -> {t, b}. Mirrors the Redis hashes so the
        # two stores answer the same questions with the same shape.
        self._groups: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
        self._fp_meta: dict[str, dict[str, Any]] = {}
        # day -> member -> {t, c, b}, capped at ROUTE_KEYS_PER_DAY members.
        self._routes: dict[str, dict[str, dict[str, int]]] = {}
        # day -> tool -> {t, b}. Mirrors the Redis daily tool hash so a
        # multi-day `by_tool` window is answered the same way in both stores.
        self._tool_days: dict[str, dict[str, dict[str, int]]] = {}
        # Fair use: key -> backend calls, mirroring the Redis keyspace exactly
        # so a test against this store is a test of the same arithmetic.
        self._fair_use: dict[str, int] = {}
        # day key -> "<kind>:<field>" -> count. Same shape as the Redis hash.
        self._fair_use_kinds: dict[str, dict[str, int]] = {}

    def _apply(
        self,
        tier: str,
        fields: dict[str, int],
        backend_calls: int,
        ts: float,
        tool: str | None,
        fingerprint: str | None,
        route: str | None = None,
        combinations: int = 0,
        fair_use_key: str | None = None,
        fair_use_kind: str | None = None,
        fair_use_blocked: bool = False,
    ) -> None:
        if fair_use_key and backend_calls > 0:
            for key in fair_use_keys(fair_use_key, ts):
                self._fair_use[key] = self._fair_use.get(key, 0) + backend_calls
        if fair_use_blocked:
            key = fair_use_blocked_key(ts)
            self._fair_use[key] = self._fair_use.get(key, 0) + 1
            if fair_use_key and fair_use_kind not in GATEWAY_KINDS:
                slot = fair_use_refusal_key(fair_use_key, ts)
                self._fair_use[slot] = self._fair_use.get(slot, 0) + 1
        if fair_use_kind:
            # Counted for every identified call, blocked or not: the split is
            # only readable against the traffic it came out of.
            members = self._fair_use_kinds.setdefault(fair_use_kind_key(ts), {})
            tool_calls = int(fields.get("tool_calls") or 0)
            for field, amount in (
                (FIELD_TOOL, tool_calls),
                (FIELD_BACKEND, backend_calls),
                (FIELD_BLOCKED, 1 if fair_use_blocked else 0),
            ):
                if amount:
                    member = f"{fair_use_kind}:{field}"
                    members[member] = members.get(member, 0) + amount
        for key, value in fields.items():
            if not value:
                continue
            self._totals[key] = self._totals.get(key, 0) + value
            bucket = self._by_tier.setdefault(tier, {})
            bucket[key] = bucket.get(key, 0) + value
        slot = _hour_bucket(ts)
        hour = self._buckets.setdefault(slot, {FIELD_BACKEND: 0, FIELD_TOOL: 0})
        tool_calls = int(fields.get("tool_calls") or 0)
        hour[FIELD_BACKEND] += backend_calls
        hour[FIELD_TOOL] += tool_calls
        for group, member in (
            (GROUP_TOOL, tool),
            (GROUP_FINGERPRINT, fingerprint),
        ):
            if not member:
                continue
            counts = self._groups.setdefault((slot, group), {}).setdefault(
                member, {FIELD_TOOL: 0, FIELD_BACKEND: 0}
            )
            counts[FIELD_TOOL] += tool_calls
            counts[FIELD_BACKEND] += backend_calls
        if tool:
            day_counts = self._tool_days.setdefault(_day_bucket(ts), {}).setdefault(
                tool, {FIELD_TOOL: 0, FIELD_BACKEND: 0}
            )
            day_counts[FIELD_TOOL] += tool_calls
            day_counts[FIELD_BACKEND] += backend_calls
        if fingerprint:
            meta = self._fp_meta.setdefault(
                fingerprint, {"first": ts, "last": ts, "tier": tier}
            )
            meta["first"] = min(meta["first"], ts)
            meta["last"] = max(meta["last"], ts)
            meta["tier"] = tier
        if route and (tool_calls or combinations or backend_calls):
            members = self._routes.setdefault(_day_bucket(ts), {})
            counts = members.get(route)
            if counts is None and len(members) < ROUTE_KEYS_PER_DAY:
                counts = members.setdefault(
                    route, {FIELD_TOOL: 0, FIELD_COMBOS: 0, FIELD_BACKEND: 0}
                )
            if counts is not None:
                counts[FIELD_TOOL] += tool_calls
                counts[FIELD_COMBOS] += combinations
                counts[FIELD_BACKEND] += backend_calls

    async def bump(
        self,
        tier: str,
        fields: dict[str, int],
        backend_calls: int,
        ts: float,
        tool: str | None = None,
        fingerprint: str | None = None,
        route: str | None = None,
        combinations: int = 0,
        fair_use_key: str | None = None,
        fair_use_kind: str | None = None,
        fair_use_blocked: bool = False,
    ) -> None:
        self._apply(
            tier,
            fields,
            backend_calls,
            ts,
            tool,
            fingerprint,
            route,
            combinations,
            fair_use_key,
            fair_use_kind,
            fair_use_blocked,
        )

    async def fair_use_usage(self, client_key: str, now: float) -> tuple[int, int]:
        day_key, month_key = fair_use_keys(client_key, now)
        return (
            int(self._fair_use.get(day_key) or 0),
            int(self._fair_use.get(month_key) or 0),
        )

    async def fair_use_refusals(self, client_key: str, now: float) -> int:
        return sum(
            int(self._fair_use.get(key) or 0)
            for key in fair_use_refusal_keys(client_key, now)
        )

    async def record_hard_block(self, client_key: str, now: float) -> None:
        """Count one 429. Deliberately does NOT touch the refusal slots.

        If a hard block re-armed its own trigger, the window would never
        decay and a caller that fixed its loop an hour ago would still be
        refused. The escalation has to be able to end on its own.
        """
        key = fair_use_hard_key(now)
        self._fair_use[key] = self._fair_use.get(key, 0) + 1

    async def fair_use_hard_today(self, now: float) -> int:
        return int(self._fair_use.get(fair_use_hard_key(now)) or 0)

    async def fair_use_blocked_today(self, now: float) -> int:
        return int(self._fair_use.get(fair_use_blocked_key(now)) or 0)

    async def fair_use_kind_counts(
        self, now: float
    ) -> dict[str, dict[str, int]]:
        return _kind_rows(self._fair_use_kinds.get(fair_use_kind_key(now)) or {})

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]:
        out = []
        for label in _bucket_range(now, hours):
            hour = self._buckets.get(label) or {}
            out.append(
                {
                    "hour": label,
                    "tool_calls": int(hour.get(FIELD_TOOL) or 0),
                    "backend_calls": int(hour.get(FIELD_BACKEND) or 0),
                }
            )
        return out

    def _merge_group(
        self, now: float, hours: int, group: str
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for label in _bucket_range(now, hours):
            for member, counts in (self._groups.get((label, group)) or {}).items():
                row = merged.setdefault(
                    member, {"tool_calls": 0, "backend_calls": 0, "hours": []}
                )
                row["tool_calls"] += int(counts.get(FIELD_TOOL) or 0)
                row["backend_calls"] += int(counts.get(FIELD_BACKEND) or 0)
                row["hours"].append(label)
        return merged

    async def tool_series(self, now: float, hours: int) -> dict[str, dict[str, int]]:
        return {
            tool: {
                "tool_calls": row["tool_calls"],
                "backend_calls": row["backend_calls"],
            }
            for tool, row in sorted(
                self._merge_group(now, hours, GROUP_TOOL).items(),
                key=lambda item: -item[1]["backend_calls"],
            )
        }

    async def tool_series_by_day(
        self, now: float, days: int
    ) -> dict[str, dict[str, int]]:
        merged: dict[str, dict[str, int]] = {}
        for label in _day_range(now, days):
            for member, counts in (self._tool_days.get(label) or {}).items():
                row = merged.setdefault(
                    member, {"tool_calls": 0, "backend_calls": 0}
                )
                row["tool_calls"] += int(counts.get(FIELD_TOOL) or 0)
                row["backend_calls"] += int(counts.get(FIELD_BACKEND) or 0)
        return dict(
            sorted(merged.items(), key=lambda item: -item[1]["backend_calls"])
        )

    async def top_clients(
        self, now: float, hours: int, limit: int
    ) -> list[dict[str, Any]]:
        merged = self._merge_group(now, hours, GROUP_FINGERPRINT)
        ranked = sorted(
            merged.items(),
            key=lambda item: (-item[1]["backend_calls"], -item[1]["tool_calls"]),
        )[:limit]
        return [
            _client_row(fp, row, self._fp_meta.get(fp) or {}) for fp, row in ranked
        ]

    async def top_routes(
        self, now: float, days: int, limit: int
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, int]] = {}
        for label in _day_range(now, days):
            for member, counts in (self._routes.get(label) or {}).items():
                row = merged.setdefault(
                    member, {"tool_calls": 0, "combinations": 0, "backend_calls": 0}
                )
                row["tool_calls"] += int(counts.get(FIELD_TOOL) or 0)
                row["combinations"] += int(counts.get(FIELD_COMBOS) or 0)
                row["backend_calls"] += int(counts.get(FIELD_BACKEND) or 0)
        return [
            _route_row(member, row)
            for member, row in sorted(
                merged.items(),
                key=lambda item: (
                    -item[1]["backend_calls"],
                    -item[1]["combinations"],
                ),
            )[:limit]
        ]

    async def backend_calls_in_window(self, now: float) -> int:
        wanted = set(_recent_buckets(now))
        # Drop anything past the retention horizon so this dict cannot grow
        # forever. Buckets inside retention are kept -- they back the series.
        oldest = _hour_bucket(now - RETENTION_HOURS * 3600)
        for slot in list(self._buckets):
            if slot < oldest:
                del self._buckets[slot]
        for slot, group in list(self._groups):
            if slot < oldest:
                del self._groups[(slot, group)]
        oldest_day = _day_bucket(now - RETENTION_HOURS * 3600)
        for day in list(self._routes):
            if day < oldest_day:
                del self._routes[day]
        for day in list(self._tool_days):
            if day < oldest_day:
                del self._tool_days[day]
        return sum(
            int((self._buckets.get(slot) or {}).get(FIELD_BACKEND) or 0)
            for slot in wanted
        )

    async def snapshot(self) -> dict[str, Any]:
        return {
            "totals": dict(self._totals),
            "by_tier": {t: dict(c) for t, c in self._by_tier.items()},
        }

    def seed(
        self,
        tier: str,
        fields: dict[str, int],
        backend_calls: int,
        ts: float,
        tool: str | None = None,
        fingerprint: str | None = None,
        route: str | None = None,
        combinations: int = 0,
    ) -> None:
        """Synchronous replay path, used when rebuilding from a log file."""
        self._apply(
            tier, fields, backend_calls, ts, tool, fingerprint, route, combinations
        )


def _kind_rows(pairs: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Turn one `<kind>:<field>` hash into {kind: {tool_calls, ...}}.

    Every kind that has any traffic gets a full row, zeros included, so a
    reader can compare `blocked` against `tool_calls` for the same kind
    without checking whether a field happened to be written.
    """
    rows: dict[str, dict[str, int]] = {}
    for member, raw in pairs.items():
        kind, _, field = str(member).rpartition(":")
        if not kind:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        row = rows.setdefault(
            kind, {"tool_calls": 0, "backend_calls": 0, "blocked": 0}
        )
        if field == FIELD_TOOL:
            row["tool_calls"] += value
        elif field == FIELD_BACKEND:
            row["backend_calls"] += value
        elif field == FIELD_BLOCKED:
            row["blocked"] += value
    return rows


def _client_row(
    fingerprint: str, counts: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    """One row of the caller breakdown.

    ``first_seen`` can predate the window: it comes from the fingerprint's own
    metadata, not from the buckets that were merged, which is what distinguishes
    a long-running scheduled client from one that appeared this morning.
    """

    def _iso(value: Any) -> str | None:
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
        except (TypeError, ValueError):
            return None

    hours = sorted(counts.get("hours") or [])
    return {
        "fingerprint": fingerprint,
        "tool_calls": int(counts.get("tool_calls") or 0),
        "backend_calls": int(counts.get("backend_calls") or 0),
        "active_hours": len(hours),
        "first_seen": _iso(meta.get("first")),
        "last_seen": _iso(meta.get("last")),
        "tier": meta.get("tier") or TIER_UNKNOWN,
    }


def _route_row(member: str, counts: dict[str, int]) -> dict[str, Any]:
    """One row of the route histogram.

    The member is unpacked back into its three parts rather than returned raw,
    so a reader never has to know the encoding to read the answer. `combinations`
    is the cost-shaped number: a single tool call for a 15-date range is fifteen
    searches, and the tool-call count hides that.
    """
    parts = member.split(ROUTE_SEPARATOR)
    parts += [""] * (3 - len(parts))
    # "-" is the absent-value sentinel the member is built with (a hotel search
    # has no origin, a stdio caller has no fingerprint). It reads as a value in
    # a log line and as noise in JSON, so it comes back out as null.
    fingerprint, origin, destination = (
        None if part in ("", "-") else part for part in parts[:3]
    )
    return {
        "fingerprint": fingerprint,
        "from": origin,
        "to": destination,
        "tool_calls": int(counts.get("tool_calls") or 0),
        "combos": int(counts.get("combinations") or 0),
        "backend_calls": int(counts.get("backend_calls") or 0),
    }


# Fields worth a per-tier breakdown as well as a global total.
TRACKED_FIELDS = (
    "tool_calls",
    "backend_calls",
    "backend_failures",
    "results_returned",
    "ad_eligible_calls",
    "blocked_calls",
    "truncated_calls",
    "errored_calls",
)


class RedisCounterStore:
    """Upstash Redis over its REST API. Shared across instances, so the
    budget guard actually means something on serverless.

    Every failure is logged and swallowed. Losing a counter must never fail
    a user's flight search, and a store outage must not become an outage of
    the product.
    """

    durable = True

    def __init__(
        self,
        url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._client = client
        # Deferred on purpose. Constructing an httpx.AsyncClient costs ~24 ms
        # of CPU on a cold start (it pulls in httpcore, h11, h2, socksio and
        # certifi and builds the transport), and a cold start that only
        # answers `initialize` or `tools/list` never touches the store. The
        # factory hands back the same process-wide client the tools use, so
        # connection reuse is unchanged once a real request arrives.
        self._client_factory = client_factory
        self._degraded = False
        # Best-effort, process-local view of how full today's route hash is, so
        # the cap can be enforced at the write site without a read round trip.
        # Every bump that writes a route also asks for HLEN in the same
        # pipeline, so instances converge on the real size instead of each
        # counting to 500 on its own.
        self._route_len: dict[str, int] = {}
        self._route_seen: dict[str, set[str]] = {}

    def _resolve_client(self) -> httpx.AsyncClient | None:
        if self._client is None and self._client_factory is not None:
            self._client = self._client_factory()
        return self._client

    @property
    def degraded(self) -> bool:
        """True once a call has failed, so /metrics can admit the numbers
        may be short rather than reporting them as authoritative."""
        return self._degraded

    async def _pipeline(self, commands: list[list[Any]]) -> list[Any] | None:
        if not commands:
            return []
        payload = [[str(part) for part in command] for command in commands]
        try:
            client = self._resolve_client() or httpx.AsyncClient(timeout=5.0)
            response = await client.post(
                f"{self._url}/pipeline",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5.0,
            )
            if self._client is None:
                await client.aclose()
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            self._degraded = True
            logger.warning("counter store write failed (%s)", exc)
            return None

    def _forget_old_route_days(self, keep: str) -> None:
        """Keep the local cap cache to the current day and yesterday."""
        for day in list(self._route_len):
            if day < keep:
                self._route_len.pop(day, None)
                self._route_seen.pop(day, None)

    async def bump(
        self,
        tier: str,
        fields: dict[str, int],
        backend_calls: int,
        ts: float,
        tool: str | None = None,
        fingerprint: str | None = None,
        route: str | None = None,
        combinations: int = 0,
        fair_use_key: str | None = None,
        fair_use_kind: str | None = None,
        fair_use_blocked: bool = False,
    ) -> None:
        commands: list[list[Any]] = []

        # Fair use rides in the same pipeline as everything else: one round
        # trip per tool call, not two. The only extra round trip fair use adds
        # anywhere is the read before the call (fair_use_usage), which cannot
        # be folded into anything because its answer decides whether the call
        # happens at all.
        if fair_use_key and backend_calls > 0:
            day_key, month_key = fair_use_keys(fair_use_key, ts)
            for key, ttl in (
                (day_key, FAIR_USE_DAY_TTL_SECONDS),
                (month_key, FAIR_USE_MONTH_TTL_SECONDS),
            ):
                commands.append(["INCRBY", key, backend_calls])
                # EXPIRE every time, not EXPIRE-if-new: a key that somehow
                # lost its TTL would otherwise live forever, and refreshing a
                # fixed-length TTL on a key that is replaced daily costs
                # nothing.
                commands.append(["EXPIRE", key, ttl])
        if fair_use_blocked:
            blocked_key = fair_use_blocked_key(ts)
            commands.append(["INCRBY", blocked_key, 1])
            commands.append(["EXPIRE", blocked_key, FAIR_USE_DAY_TTL_SECONDS])
            # The rolling-hour refusal counter behind the hard escalation.
            # Written only for a key that stands for ONE caller: a pooled
            # gateway key is an unknown number of real people, and counting
            # their refusals together would eventually 429 the whole gateway.
            # The exemption lives here, at the write site, because this is
            # where the kind is known accurately -- the middleware that reads
            # the counter runs before the OpenAI feed is guaranteed loaded,
            # and `gateway_pooled` and `direct` are the same digest.
            if fair_use_key and fair_use_kind not in GATEWAY_KINDS:
                slot_key = fair_use_refusal_key(fair_use_key, ts)
                commands.append(["INCRBY", slot_key, 1])
                commands.append(
                    ["EXPIRE", slot_key, FAIR_USE_REFUSAL_TTL_SECONDS]
                )
        if fair_use_kind:
            # Same pipeline again. One hash per day with at most four kinds x
            # three fields in it, so it is bounded by construction and needs no
            # cap of the kind the route histogram has.
            kind_key = fair_use_kind_key(ts)
            for field, amount in (
                (FIELD_TOOL, int(fields.get("tool_calls") or 0)),
                (FIELD_BACKEND, backend_calls),
                (FIELD_BLOCKED, 1 if fair_use_blocked else 0),
            ):
                if amount:
                    commands.append(
                        ["HINCRBY", kind_key, f"{fair_use_kind}:{field}", amount]
                    )
            commands.append(["EXPIRE", kind_key, FAIR_USE_DAY_TTL_SECONDS])

        for key, value in fields.items():
            if not value:
                continue
            commands.append(["INCRBY", f"{KEY_PREFIX}:total:{key}", value])
            commands.append(["INCRBY", f"{KEY_PREFIX}:tier:{tier}:{key}", value])
        slot = _hour_bucket(ts)
        day = _day_bucket(ts)
        tool_calls = int(fields.get("tool_calls") or 0)
        for field, amount in ((FIELD_BACKEND, backend_calls), (FIELD_TOOL, tool_calls)):
            if not amount:
                continue
            key = f"{KEY_PREFIX}:h:{slot}:{field}"
            commands.append(["INCRBY", key, amount])
            commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])

        # The breakdowns. Same pipeline, so they cost one more command each and
        # not one more round trip, and the whole write stays best-effort: a
        # store outage loses counters and never fails a search.
        for group, member in (
            (GROUP_TOOL, tool),
            (GROUP_FINGERPRINT, fingerprint),
        ):
            if not member:
                continue
            for field, amount in (
                (FIELD_TOOL, tool_calls),
                (FIELD_BACKEND, backend_calls),
            ):
                if not amount:
                    continue
                key = f"{KEY_PREFIX}:h:{slot}:{group}:{field}"
                commands.append(["HINCRBY", key, member, amount])
                commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])

        # The per-day per-tool histogram: same shape as the hourly one just
        # above, but keyed by UTC day like the route histogram, so a window
        # wider than a day or two (up to the full 35-day retention) can be
        # read as `days` HGETALLs instead of `hours` of them -- a 35-day
        # window is 840 hourly keys but only 35 daily ones. This is what lets
        # /metrics/calls answer "flights vs hotels since <window>" instead of
        # only the last ~24h the hourly breakdown practically allows.
        if tool:
            for field, amount in (
                (FIELD_TOOL, tool_calls),
                (FIELD_BACKEND, backend_calls),
            ):
                if not amount:
                    continue
                key = f"{KEY_PREFIX}:d:{day}:{GROUP_TOOL}:{field}"
                commands.append(["HINCRBY", key, tool, amount])
                commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])

        if fingerprint:
            meta_key = f"{FP_META_PREFIX}:{fingerprint}"
            # HSETNX so the first sighting is not overwritten by every later
            # one; `last` and `tier` are meant to move. `last` is the most
            # recent write rather than a running max, which is the same thing
            # while calls arrive in time order and is not worth a read-modify-
            # write to make exact -- it is a reporting field.
            commands.append(["HSETNX", meta_key, "first", int(ts)])
            commands.append(["HSET", meta_key, "last", int(ts), "tier", tier])
            commands.append(["EXPIRE", meta_key, BUCKET_TTL_SECONDS])

        # The per-day route histogram. Same pipeline again, and capped: a new
        # member is only written while the day is known to be under
        # ROUTE_KEYS_PER_DAY, while a member already being counted keeps
        # counting forever.
        hlen_at: int | None = None
        if route and (tool_calls or combinations or backend_calls):
            seen = self._route_seen.setdefault(day, set())
            known = route in seen
            if known or self._route_len.get(day, 0) < ROUTE_KEYS_PER_DAY:
                if not known:
                    seen.add(route)
                    self._route_len[day] = self._route_len.get(day, 0) + 1
                for field, amount in (
                    (FIELD_TOOL, tool_calls),
                    (FIELD_COMBOS, combinations),
                    (FIELD_BACKEND, backend_calls),
                ):
                    if not amount:
                        continue
                    key = f"{KEY_PREFIX}:d:{day}:{GROUP_ROUTE}:{field}"
                    commands.append(["HINCRBY", key, route, amount])
                    commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])
                hlen_at = len(commands)
                commands.append(
                    ["HLEN", f"{KEY_PREFIX}:d:{day}:{GROUP_ROUTE}:{FIELD_TOOL}"]
                )
            self._forget_old_route_days(_day_bucket(ts - 86400))

        result = await self._pipeline(commands)

        if hlen_at is not None and result:
            try:
                actual = int(result[hlen_at].get("result") or 0)
            except (AttributeError, IndexError, TypeError, ValueError):
                actual = 0
            # max(): the local count is ahead of Redis for the writes still in
            # flight, and under-counting is what lets the hash grow past the cap.
            self._route_len[day] = max(self._route_len.get(day, 0), actual)

    async def _hgetall_many(self, keys: list[str]) -> list[dict[str, str]]:
        """HGETALL over several keys, chunked like `_mget_ints`.

        Upstash returns a hash as a flat [field, value, field, value] array.
        """
        out: list[dict[str, str]] = []
        CHUNK = 50
        for start in range(0, len(keys), CHUNK):
            chunk = keys[start : start + CHUNK]
            result = await self._pipeline([["HGETALL", key] for key in chunk])
            for index in range(len(chunk)):
                flat: list[Any] = []
                if result:
                    try:
                        flat = result[index].get("result") or []
                    except (AttributeError, IndexError):
                        flat = []
                if isinstance(flat, dict):
                    out.append({str(k): str(v) for k, v in flat.items()})
                    continue
                pairs = {}
                for i in range(0, len(flat) - 1, 2):
                    pairs[str(flat[i])] = str(flat[i + 1])
                out.append(pairs)
        return out

    async def _merge_group(
        self, now: float, hours: int, group: str
    ) -> dict[str, dict[str, Any]]:
        labels = _bucket_range(now, hours)
        merged: dict[str, dict[str, Any]] = {}
        for field, name in ((FIELD_TOOL, "tool_calls"), (FIELD_BACKEND, "backend_calls")):
            hashes = await self._hgetall_many(
                [f"{KEY_PREFIX}:h:{s}:{group}:{field}" for s in labels]
            )
            for index, pairs in enumerate(hashes):
                for member, raw in pairs.items():
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        continue
                    row = merged.setdefault(
                        member, {"tool_calls": 0, "backend_calls": 0, "hours": []}
                    )
                    row[name] += value
                    if labels[index] not in row["hours"]:
                        row["hours"].append(labels[index])
        return merged

    async def tool_series(self, now: float, hours: int) -> dict[str, dict[str, int]]:
        merged = await self._merge_group(now, hours, GROUP_TOOL)
        return {
            tool: {
                "tool_calls": row["tool_calls"],
                "backend_calls": row["backend_calls"],
            }
            for tool, row in sorted(
                merged.items(), key=lambda item: -item[1]["backend_calls"]
            )
        }

    async def top_clients(
        self, now: float, hours: int, limit: int
    ) -> list[dict[str, Any]]:
        merged = await self._merge_group(now, hours, GROUP_FINGERPRINT)
        ranked = sorted(
            merged.items(),
            key=lambda item: (-item[1]["backend_calls"], -item[1]["tool_calls"]),
        )[:limit]
        if not ranked:
            return []
        # Only the rows that survived the cut are looked up, so an hour with
        # thousands of distinct fingerprints still costs one extra chunked read.
        metas = await self._hgetall_many(
            [f"{FP_META_PREFIX}:{fp}" for fp, _ in ranked]
        )
        return [
            _client_row(fp, row, metas[index] if index < len(metas) else {})
            for index, (fp, row) in enumerate(ranked)
        ]

    async def tool_series_by_day(
        self, now: float, days: int
    ) -> dict[str, dict[str, int]]:
        """Same answer as `tool_series`, read from the daily keys instead of
        the hourly ones -- `days` HGETALLs per field instead of `hours` of
        them, so a multi-week window is cheap enough to actually serve."""
        labels = _day_range(now, days)
        merged: dict[str, dict[str, int]] = {}
        for field, name in ((FIELD_TOOL, "tool_calls"), (FIELD_BACKEND, "backend_calls")):
            hashes = await self._hgetall_many(
                [f"{KEY_PREFIX}:d:{d}:{GROUP_TOOL}:{field}" for d in labels]
            )
            for pairs in hashes:
                for member, raw in pairs.items():
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        continue
                    row = merged.setdefault(
                        member, {"tool_calls": 0, "backend_calls": 0}
                    )
                    row[name] += value
        return dict(
            sorted(merged.items(), key=lambda item: -item[1]["backend_calls"])
        )

    async def top_routes(
        self, now: float, days: int, limit: int
    ) -> list[dict[str, Any]]:
        labels = _day_range(now, days)
        merged: dict[str, dict[str, int]] = {}
        for field, name in (
            (FIELD_TOOL, "tool_calls"),
            (FIELD_COMBOS, "combinations"),
            (FIELD_BACKEND, "backend_calls"),
        ):
            hashes = await self._hgetall_many(
                [f"{KEY_PREFIX}:d:{d}:{GROUP_ROUTE}:{field}" for d in labels]
            )
            for pairs in hashes:
                for member, raw in pairs.items():
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        continue
                    row = merged.setdefault(
                        member,
                        {"tool_calls": 0, "combinations": 0, "backend_calls": 0},
                    )
                    row[name] += value
        return [
            _route_row(member, row)
            for member, row in sorted(
                merged.items(),
                key=lambda item: (
                    -item[1]["backend_calls"],
                    -item[1]["combinations"],
                ),
            )[:limit]
        ]

    async def _mget_ints(self, keys: list[str]) -> list[int]:
        """MGET in chunks. A 35-day query is 840 keys; one giant MGET is rude."""
        values: list[int] = []
        CHUNK = 200
        for start in range(0, len(keys), CHUNK):
            chunk = keys[start : start + CHUNK]
            result = await self._pipeline([["MGET", *chunk]])
            raw = []
            if result:
                try:
                    raw = result[0].get("result") or []
                except (AttributeError, IndexError):
                    raw = []
            for i in range(len(chunk)):
                try:
                    values.append(int(raw[i]))
                except (IndexError, TypeError, ValueError):
                    values.append(0)
        return values

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]:
        labels = _bucket_range(now, hours)
        backend = await self._mget_ints(
            [f"{KEY_PREFIX}:h:{s}:{FIELD_BACKEND}" for s in labels]
        )
        tool = await self._mget_ints(
            [f"{KEY_PREFIX}:h:{s}:{FIELD_TOOL}" for s in labels]
        )
        return [
            {"hour": label, "tool_calls": tool[i], "backend_calls": backend[i]}
            for i, label in enumerate(labels)
        ]

    async def backend_calls_in_window(self, now: float) -> int:
        keys = [
            f"{KEY_PREFIX}:h:{slot}:{FIELD_BACKEND}" for slot in _recent_buckets(now)
        ]
        return sum(await self._mget_ints(keys))

    async def fair_use_usage(self, client_key: str, now: float) -> tuple[int, int]:
        """This client's backend calls today and this month. One MGET.

        A store outage returns (0, 0), which fails OPEN -- nobody is refused
        on a number we could not read. The alternative fails a paying-shaped
        request on an infrastructure blip, and the daily spend budget is still
        underneath as the real ceiling.
        """
        day_key, month_key = fair_use_keys(client_key, now)
        values = await self._mget_ints([day_key, month_key])
        values += [0, 0]
        return values[0], values[1]

    async def fair_use_kind_counts(
        self, now: float
    ) -> dict[str, dict[str, int]]:
        hashes = await self._hgetall_many([fair_use_kind_key(now)])
        return _kind_rows(hashes[0] if hashes else {})

    async def fair_use_refusals(self, client_key: str, now: float) -> int:
        """Soft refusals for this client in the rolling hour. One MGET.

        Fails open through `_mget_ints`, which returns zeros on an outage:
        the escalation is a defence against a loop, not a spend control, and
        the day and month caps underneath are untouched by a store blip.
        """
        return sum(
            await self._mget_ints(fair_use_refusal_keys(client_key, now))
        )

    async def record_hard_block(self, client_key: str, now: float) -> None:
        """Count one 429. Never touches the refusal slots -- see the memory
        store's copy of this method for why the window must be allowed to
        decay while a blocked caller is still hammering."""
        key = fair_use_hard_key(now)
        await self._pipeline(
            [
                ["INCRBY", key, 1],
                ["EXPIRE", key, FAIR_USE_DAY_TTL_SECONDS],
            ]
        )

    async def fair_use_hard_today(self, now: float) -> int:
        values = await self._mget_ints([fair_use_hard_key(now)])
        return values[0] if values else 0

    async def fair_use_blocked_today(self, now: float) -> int:
        values = await self._mget_ints([fair_use_blocked_key(now)])
        return values[0] if values else 0

    async def snapshot(self) -> dict[str, Any]:
        total_values = await self._mget_ints(
            [f"{KEY_PREFIX}:total:{f}" for f in TRACKED_FIELDS]
        )
        totals = {
            field: value
            for field, value in zip(TRACKED_FIELDS, total_values)
            if value
        }

        # Per-tier is the breakdown that answers "who is calling, and what
        # share of them are on a surface that can render an ad" -- the whole
        # point of monitor mode. It was being written and never read.
        tier_keys = [
            f"{KEY_PREFIX}:tier:{tier}:{field}"
            for tier in KNOWN_TIERS
            for field in TRACKED_FIELDS
        ]
        tier_values = await self._mget_ints(tier_keys)
        by_tier: dict[str, dict[str, int]] = {}
        for t, tier in enumerate(KNOWN_TIERS):
            offset = t * len(TRACKED_FIELDS)
            counts = {
                field: tier_values[offset + f]
                for f, field in enumerate(TRACKED_FIELDS)
                if tier_values[offset + f]
            }
            if counts:
                by_tier[tier] = counts

        return {"totals": totals, "by_tier": by_tier}


def build_counter_store(
    client: httpx.AsyncClient | None = None,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> CounterStore:
    """Pick a store from the environment.

    Recognises both credential names: Vercel's Upstash marketplace
    integration injects UPSTASH_REDIS_REST_*, while stores migrated from the
    retired Vercel KV carry KV_REST_API_*.
    """
    url = (
        os.environ.get("UPSTASH_REDIS_REST_URL")
        or os.environ.get("KV_REST_API_URL")
        or ""
    ).strip().strip('"').strip("'")
    token = (
        os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        or os.environ.get("KV_REST_API_TOKEN")
        or ""
    ).strip().strip('"').strip("'")

    if url and token:
        logger.info("using Upstash Redis counter store (durable, shared)")
        return RedisCounterStore(
            url, token, client=client, client_factory=client_factory
        )

    logger.info("using in-process counter store (not shared between instances)")
    return MemoryCounterStore()
