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
from typing import Any, Protocol

import httpx

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


def _hour_bucket(ts: float) -> str:
    return time.strftime("%Y%m%d%H", time.gmtime(ts))


def _bucket_range(now: float, hours: int) -> list[str]:
    """Hour labels from oldest to newest, inclusive of the current hour."""
    hours = max(1, min(int(hours), MAX_QUERY_HOURS))
    return [_hour_bucket(now - h * 3600) for h in range(hours - 1, -1, -1)]


def _recent_buckets(now: float) -> list[str]:
    return [_hour_bucket(now - hours * 3600) for hours in range(WINDOW_HOURS)]


class CounterStore(Protocol):
    """Totals, per-tier counters, and a rolling 24h backend-call window."""

    durable: bool

    async def bump(
        self, tier: str, fields: dict[str, int], backend_calls: int, ts: float
    ) -> None: ...

    async def backend_calls_in_window(self, now: float) -> int: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def call_series(self, now: float, hours: int) -> list[dict[str, Any]]: ...


class MemoryCounterStore:
    """In-process counters. Correct for a container, wrong for serverless."""

    durable = False

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        self._by_tier: dict[str, dict[str, int]] = {}
        self._buckets: dict[str, dict[str, int]] = {}

    async def bump(
        self, tier: str, fields: dict[str, int], backend_calls: int, ts: float
    ) -> None:
        for key, value in fields.items():
            if not value:
                continue
            self._totals[key] = self._totals.get(key, 0) + value
            bucket = self._by_tier.setdefault(tier, {})
            bucket[key] = bucket.get(key, 0) + value
        slot = _hour_bucket(ts)
        hour = self._buckets.setdefault(slot, {FIELD_BACKEND: 0, FIELD_TOOL: 0})
        hour[FIELD_BACKEND] += backend_calls
        hour[FIELD_TOOL] += int(fields.get("tool_calls") or 0)

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

    async def backend_calls_in_window(self, now: float) -> int:
        wanted = set(_recent_buckets(now))
        # Drop anything past the retention horizon so this dict cannot grow
        # forever. Buckets inside retention are kept -- they back the series.
        oldest = _hour_bucket(now - RETENTION_HOURS * 3600)
        for slot in list(self._buckets):
            if slot < oldest:
                del self._buckets[slot]
        return sum(
            int((self._buckets.get(slot) or {}).get(FIELD_BACKEND) or 0)
            for slot in wanted
        )

    async def snapshot(self) -> dict[str, Any]:
        return {
            "totals": dict(self._totals),
            "by_tier": {t: dict(c) for t, c in self._by_tier.items()},
        }

    def seed(self, tier: str, fields: dict[str, int], backend_calls: int, ts: float) -> None:
        """Synchronous replay path, used when rebuilding from a log file."""
        for key, value in fields.items():
            if not value:
                continue
            self._totals[key] = self._totals.get(key, 0) + value
            bucket = self._by_tier.setdefault(tier, {})
            bucket[key] = bucket.get(key, 0) + value
        slot = _hour_bucket(ts)
        hour = self._buckets.setdefault(slot, {FIELD_BACKEND: 0, FIELD_TOOL: 0})
        hour[FIELD_BACKEND] += backend_calls
        hour[FIELD_TOOL] += int(fields.get("tool_calls") or 0)


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

    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._client = client
        self._degraded = False

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
            client = self._client or httpx.AsyncClient(timeout=5.0)
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

    async def bump(
        self, tier: str, fields: dict[str, int], backend_calls: int, ts: float
    ) -> None:
        commands: list[list[Any]] = []
        for key, value in fields.items():
            if not value:
                continue
            commands.append(["INCRBY", f"{KEY_PREFIX}:total:{key}", value])
            commands.append(["INCRBY", f"{KEY_PREFIX}:tier:{tier}:{key}", value])
        slot = _hour_bucket(ts)
        tool_calls = int(fields.get("tool_calls") or 0)
        for field, amount in ((FIELD_BACKEND, backend_calls), (FIELD_TOOL, tool_calls)):
            if not amount:
                continue
            key = f"{KEY_PREFIX}:h:{slot}:{field}"
            commands.append(["INCRBY", key, amount])
            commands.append(["EXPIRE", key, BUCKET_TTL_SECONDS])
        await self._pipeline(commands)

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


def build_counter_store(client: httpx.AsyncClient | None = None) -> CounterStore:
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
        return RedisCounterStore(url, token, client=client)

    logger.info("using in-process counter store (not shared between instances)")
    return MemoryCounterStore()
