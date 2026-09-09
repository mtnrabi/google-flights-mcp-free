"""
Call logging, counters, and the backend-spend guard.

Two jobs.

1. Answer "how many calls came through the MCP, and how many Lambda requests
   did they cause?" Every tool call emits one JSON line to stdout prefixed
   `MCP_CALL `, which is the only logging channel that works everywhere --
   container, Vercel, or local. Vercel captures stdout automatically, so that
   line is the record of truth. A file sink is layered on top when a writable
   LOG_PATH is configured, which is the container case.

2. Stop the free channel running up an unbounded backend bill. Revenue is per
   rendered ad (per tool call); cost is per backend call. The ratio between
   them is surfaced as `backend_calls_per_tool_call` rather than left to be
   reconstructed later, because it is the number that decides whether this
   channel survives past the POC.

The counters live behind a CounterStore (see stores.py) because a single
process holding them in a dict is only correct when there is a single
process. On serverless there is not, and the guard silently under-counts
unless a shared store is configured.

Vercel note: runtime logs cap at 256 lines and 1 MB per request, so this
emits exactly one line per tool call, never one per backend call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from .stores import (
    CLIENT_WINDOW_HOURS,
    RETENTION_HOURS,
    ROUTE_SEPARATOR,
    ROUTE_WINDOW_DAYS,
    TOP_CLIENTS,
    TOP_ROUTES,
    CounterStore,
    MemoryCounterStore,
)

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 60 * 60
STDOUT_PREFIX = "MCP_CALL "

# The one-line-per-tool-call record of WHAT was searched.
#
# `MCP_CALL` already answered "how many calls, by whom, at what cost". It could
# never answer "what were they searching for", and neither could the backend:
# the Lambda logs `[source] source=lulu tool=... endpoint=...` and nothing else,
# by design (see state/gtm/free-mcp-batch-client-searches-2026-09-05.md). So for
# the client burning ~45 backend calls a second at 04:00Z every morning, the
# origin, the destinations and the date range were not recoverable from any log
# anywhere. This line is that gap closed.
#
# REQUEST PARAMETERS ONLY. No IP, no user agent, no session id, no header of any
# kind -- the fingerprint is already the one-way digest of those three
# (policy.client_fingerprint) and it is the only caller-derived value here.
TOOL_CALL_PREFIX = "[tool_call]"

# Field width caps. A hotel destination is free text a caller typed, so it is
# bounded before it reaches a log line or, worse, a Redis hash member.
MAX_FIELD = 24
MAX_DESTINATION = 48


def _clean(value: Any, limit: int = MAX_FIELD) -> str:
    """One log field: no spaces, no separators, bounded length.

    Whitespace becomes `_` and the separators the line and the histogram member
    are built from are stripped, so no caller-supplied string can forge a field
    boundary in either.
    """
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"
    for bad in (ROUTE_SEPARATOR, "=", "\n", "\r", "\t"):
        text = text.replace(bad, "")
    text = "_".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "~"
    return text or "-"


@dataclass
class RouteRecord:
    """What one tool call asked for. Built by the tool, carried on CallRecord."""

    origin: str | None = None
    destination: str | None = None
    dates: str | None = None
    nights: str | None = None
    currency: str | None = None
    passengers: str | None = None
    stops: str | None = None
    #: Date x destination combinations the plan expanded to -- the cost of the
    #: call, which its tool-call count of 1 does not show.
    combinations: int = 0

    @property
    def origin_field(self) -> str:
        return _clean(self.origin)

    @property
    def destination_field(self) -> str:
        return _clean(self.destination, MAX_DESTINATION)

    def member(self, fingerprint: str | None) -> str:
        """The route histogram key: caller + where they were flying."""
        return ROUTE_SEPARATOR.join(
            (_clean(fingerprint), self.origin_field, self.destination_field)
        )

    def log_line(self, fingerprint: str | None, tool: str) -> str:
        return (
            f"{TOOL_CALL_PREFIX} fp={_clean(fingerprint)} tool={_clean(tool, 40)} "
            f"from={self.origin_field} to={self.destination_field} "
            f"dates={_clean(self.dates)} combos={max(0, int(self.combinations))} "
            f"nights={_clean(self.nights)} currency={_clean(self.currency)} "
            f"pax={_clean(self.passengers)} stops={_clean(self.stops)}"
        )


@dataclass
class CallRecord:
    """One tool call, as logged."""

    timestamp: float
    tool: str
    tier: str
    client_name: str | None
    source_ip: str | None
    widget_capable: bool
    requested_combinations: int
    backend_calls: int
    backend_failures: int
    results_returned: int
    duration_ms: int
    truncated: bool
    allowed: bool
    decision_reason: str
    ad_eligible: bool
    error: str | None = None
    #: A 12-hex digest of the caller's forwarded IP, user agent and MCP session
    #: id -- see `policy.client_fingerprint`. Stable enough to say "these 3,270
    #: backend calls were one client", and one-way, so none of the three values
    #: it was derived from is stored or logged. None when the request carried
    #: none of them (stdio, tests).
    fingerprint: str | None = None
    #: The search parameters this call was for. None for a call that has none
    #: (a test, a tool that does not search), in which case no `[tool_call]`
    #: line is written and no route is counted.
    route: RouteRecord | None = None
    #: The fair-use client key -- sha256(x-forwarded-for + user-agent)[:12],
    #: NOT `fingerprint`, which also mixes in the session id and would reset on
    #: every request for the client the cap exists for. See fair_use.py. None
    #: when the request carried neither header, in which case nothing is
    #: counted against any cap.
    fair_use_key: str | None = None
    #: How that key was chosen -- `direct`, `config`, `gateway_session` or
    #: `gateway_pooled` (fair_use.ALL_KINDS). Counted per day so a block can be
    #: read: a `direct` block is the cap doing its job, a `gateway_pooled`
    #: block is real users behind one host refusing each other and means the
    #: gateway cap is set too low.
    fair_use_kind: str | None = None
    #: True when this call was refused by the fair-use cap. It spent zero
    #: backend calls, so it adds nothing to either allowance; it increments the
    #: refusal counter /metrics/calls reports.
    fair_use_blocked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": round(self.timestamp, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "tool": self.tool,
            "tier": self.tier,
            "client_name": self.client_name,
            "source_ip": self.source_ip,
            "widget_capable": self.widget_capable,
            "requested_combinations": self.requested_combinations,
            # The Lambda request count for this call. Sum this field across
            # MCP_CALL lines to get total backend requests for any period.
            "backend_calls": self.backend_calls,
            "backend_failures": self.backend_failures,
            "results_returned": self.results_returned,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "allowed": self.allowed,
            "decision_reason": self.decision_reason,
            "ad_eligible": self.ad_eligible,
            "error": self.error,
            "fingerprint": self.fingerprint,
            "fair_use_client": self.fair_use_key,
            "fair_use_kind": self.fair_use_kind,
            "fair_use_blocked": self.fair_use_blocked,
        }

    def counter_fields(self) -> dict[str, int]:
        return {
            "tool_calls": 1,
            "backend_calls": self.backend_calls,
            "backend_failures": self.backend_failures,
            "results_returned": self.results_returned,
            "ad_eligible_calls": 1 if self.ad_eligible else 0,
            "blocked_calls": 0 if self.allowed else 1,
            "truncated_calls": 1 if self.truncated else 0,
            "errored_calls": 1 if self.error else 0,
        }


class Telemetry:
    def __init__(
        self,
        store: CounterStore | None = None,
        daily_budget: int = 0,
        degrade_at: float = 0.8,
        log_path: str | None = None,
        stdout: bool = True,
        fair_use_enabled: bool = False,
        fair_use_day_cap: int = 0,
        fair_use_month_cap: int = 0,
        fair_use_gateway_day_cap: int = 0,
        fair_use_gateway_month_cap: int = 0,
        fair_use_hard_after: int = 0,
    ) -> None:
        self._store: CounterStore = store or MemoryCounterStore()
        self._daily_budget = max(0, daily_budget)
        self._degrade_at = min(max(degrade_at, 0.0), 1.0)
        self._fair_use_enabled = fair_use_enabled
        self._fair_use_day_cap = max(0, fair_use_day_cap)
        self._fair_use_month_cap = max(0, fair_use_month_cap)
        self._fair_use_gateway_day_cap = max(0, fair_use_gateway_day_cap)
        self._fair_use_gateway_month_cap = max(0, fair_use_gateway_month_cap)
        self._fair_use_hard_after = max(0, fair_use_hard_after)
        self._stdout = stdout
        self._started_at = time.time()
        self._log_path = self._prepare_file_sink(log_path)
        if self._log_path:
            self._replay_log()

    # ── sinks ────────────────────────────────────────────────────────────

    def _prepare_file_sink(self, log_path: str | None) -> str | None:
        """Enable the file sink only if the path is genuinely writable.

        On Vercel everything outside /tmp is read-only, and /tmp itself has
        no durability guarantee. Rather than append into a void, the file
        sink turns itself off and stdout carries the record.
        """
        if not log_path:
            return None
        try:
            directory = os.path.dirname(os.path.abspath(log_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(log_path, "a", encoding="utf-8"):
                pass
            return log_path
        except OSError as exc:
            logger.info(
                "file log sink disabled (%s is not writable: %s); "
                "stdout MCP_CALL lines remain the record",
                log_path,
                exc,
            )
            return None

    @property
    def file_sink_enabled(self) -> bool:
        return self._log_path is not None

    @property
    def durable_counters(self) -> bool:
        return getattr(self._store, "durable", False)

    def _emit(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        if self._stdout:
            # stdout, not the logging module: MCP over stdio would own stdout,
            # but this server only ever runs over HTTP. Vercel maps stdout to
            # info-level runtime logs.
            print(STDOUT_PREFIX + line, file=sys.stdout, flush=True)
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                logger.warning("could not append to %s: %s", self._log_path, exc)

    # ── recording ────────────────────────────────────────────────────────

    async def record(self, record: CallRecord) -> None:
        payload = record.to_json()
        self._emit(payload)
        route = record.route
        if route is not None:
            # logger, not the MCP_CALL stdout channel: this is a human-readable
            # grep target, and keeping it off that line means nothing parsing
            # MCP_CALL has to learn a new shape.
            logger.info("%s", route.log_line(record.fingerprint, record.tool))
        try:
            await self._store.bump(
                record.tier,
                record.counter_fields(),
                record.backend_calls,
                record.timestamp,
                tool=record.tool,
                fingerprint=record.fingerprint,
                route=route.member(record.fingerprint) if route else None,
                combinations=route.combinations if route else 0,
                fair_use_key=record.fair_use_key,
                fair_use_kind=record.fair_use_kind,
                fair_use_blocked=record.fair_use_blocked,
            )
        except Exception as exc:  # noqa: BLE001 - counters are not the product
            logger.warning("counter update failed: %s", exc)

    # ── fair use ─────────────────────────────────────────────────────────

    @property
    def fair_use_enabled(self) -> bool:
        return self._fair_use_enabled

    async def fair_use_usage(self, client_key: str) -> tuple[int, int]:
        """This client's backend calls today and this month.

        Fails open: a store that will not answer returns (0, 0) and nobody is
        refused on a number we could not read. The rolling daily budget is
        still underneath as the real ceiling on spend.
        """
        try:
            return await self._store.fair_use_usage(client_key, time.time())
        except Exception as exc:  # noqa: BLE001 - never fail a search
            logger.warning("could not read fair-use counters: %s", exc)
            return 0, 0

    async def fair_use_blocked_today(self) -> int:
        try:
            return await self._store.fair_use_blocked_today(time.time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read fair-use blocked count: %s", exc)
            return 0

    @property
    def fair_use_hard_after(self) -> int:
        """Refusals in a rolling hour before a client gets a 429. 0 is off."""
        return self._fair_use_hard_after

    async def fair_use_refusals(self, client_key: str) -> int:
        """Soft refusals this client collected in the rolling hour.

        Fails open at 0, like every other read here: a store outage must not
        start 429ing people, and it must not fail a search either.
        """
        try:
            return await self._store.fair_use_refusals(client_key, time.time())
        except Exception as exc:  # noqa: BLE001 - never fail a request
            logger.warning("could not read fair-use refusals: %s", exc)
            return 0

    async def record_hard_block(self, client_key: str) -> None:
        """Count one 429. Best effort -- the response goes out either way."""
        try:
            await self._store.record_hard_block(client_key, time.time())
        except Exception as exc:  # noqa: BLE001 - counters are not the product
            logger.warning("could not record a hard block: %s", exc)

    async def fair_use_hard_today(self) -> int:
        try:
            return await self._store.fair_use_hard_today(time.time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read fair-use hard count: %s", exc)
            return 0

    async def fair_use_kind_counts(self) -> dict[str, dict[str, int]]:
        """Today's tool calls, backend calls and blocks per key kind.

        The number that says whether the gateway split is working. Without it
        `fair_use_blocked_today: 12` is unreadable -- twelve refusals of a
        runaway script and twelve Claude users refusing each other look
        identical, and only one of them is the cap doing what it was built for.
        """
        try:
            return await self._store.fair_use_kind_counts(time.time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read fair-use kind counts: %s", exc)
            return {}

    # ── budget ───────────────────────────────────────────────────────────

    async def backend_calls_in_window(self) -> int:
        try:
            return await self._store.backend_calls_in_window(time.time())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read budget window: %s", exc)
            return 0

    async def budget_state(self) -> dict[str, Any]:
        used = await self.backend_calls_in_window()
        if self._daily_budget <= 0:
            return {
                "enabled": False,
                "used_24h": used,
                "budget": None,
                "remaining": None,
                "degraded": False,
                "exhausted": False,
                "enforceable": self.durable_counters,
            }
        return {
            "enabled": True,
            "used_24h": used,
            "budget": self._daily_budget,
            "remaining": max(0, self._daily_budget - used),
            "degraded": used >= self._daily_budget * self._degrade_at,
            "exhausted": used >= self._daily_budget,
            # False means counters are per-instance, so the real spend is at
            # least this and probably higher. Stated rather than implied.
            "enforceable": self.durable_counters,
        }

    async def cap_for_budget(self, requested_cap: int) -> tuple[int, str | None]:
        """Shrink the per-call cap as the daily budget runs down.

        Degrading beats refusing: a single-date answer is still useful, and a
        hard failure at 80% of budget would look like an outage to every user
        for the rest of the day.
        """
        state = await self.budget_state()
        if not state["enabled"]:
            return requested_cap, None
        if state["exhausted"]:
            return 1, (
                f"daily backend-call budget of {self._daily_budget} is spent; "
                "serving a single search per request until the window rolls over"
            )
        if state["degraded"]:
            reduced = max(1, requested_cap // 3)
            return reduced, (
                f"{state['used_24h']} of {self._daily_budget} daily backend calls "
                f"used; fan-out reduced to {reduced} to protect the budget"
            )
        return requested_cap, None

    async def call_series(self, hours: int = 24) -> dict[str, Any]:
        """Per-hour call counts over the last `hours`, oldest first.

        This is the answer to "how many calls in a given period". It reads the
        same hourly buckets the spend guard uses, so the numbers agree by
        construction rather than by coincidence.

        Only meaningful with a durable store: per-instance counters reset on
        every cold start, so `durable` is reported alongside the data rather
        than leaving a caller to assume the zeros are real.
        """
        now = time.time()
        try:
            buckets = await self._store.call_series(now, hours)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read call series: %s", exc)
            buckets = []

        # The two breakdowns the totals could not give. "31,239 backend calls"
        # was never the hard question -- "which tool, and was it one client or
        # many" was, and until these existed neither was answerable for any
        # period, past or future.
        #
        # Read from the daily tool counters, not the hourly ones `hours`
        # would naively suggest: a 35-day window is 840 hourly HGETALLs but
        # only 35 daily ones, and the daily keys are what actually survive a
        # wide window instead of quietly degrading to whatever the last day
        # happens to hold. `hours` still decides how wide the window is --
        # rounded up to whole UTC days, capped at the retention horizon -- so
        # the default (no `hours=` given) keeps behaving like "the last day".
        tool_days = max(1, min(-(-hours // 24), RETENTION_HOURS // 24))
        try:
            by_tool = await self._store.tool_series_by_day(now, tool_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read tool series: %s", exc)
            by_tool = {}
        try:
            top_clients = await self._store.top_clients(
                now, CLIENT_WINDOW_HOURS, TOP_CLIENTS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read client breakdown: %s", exc)
            top_clients = []
        try:
            top_routes = await self._store.top_routes(
                now, ROUTE_WINDOW_DAYS, TOP_ROUTES
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read route breakdown: %s", exc)
            top_routes = []

        # How often the fair-use cap actually fired today. Without it, the
        # only evidence a cap is doing anything is an absence -- traffic that
        # did not happen -- which is unreadable on any dashboard.
        blocked_today = await self.fair_use_blocked_today()
        by_key_kind = await self.fair_use_kind_counts()
        hard_today = await self.fair_use_hard_today()

        return {
            "hours": hours,
            "from": time.strftime(
                "%Y-%m-%dT%H:00:00Z", time.gmtime(now - (hours - 1) * 3600)
            ),
            "to": time.strftime("%Y-%m-%dT%H:59:59Z", time.gmtime(now)),
            "totals": {
                "tool_calls": sum(b["tool_calls"] for b in buckets),
                "backend_calls": sum(b["backend_calls"] for b in buckets),
            },
            "buckets": buckets,
            "by_tool": by_tool,
            "by_tool_window": {
                "days": tool_days,
                "note": (
                    "by_tool is aggregated from per-UTC-day counters, not "
                    "the hourly buckets above, so it always covers whole "
                    "UTC days -- `days` is how many, rounded up from "
                    "`hours` (min 1, capped at the 35-day retention window)."
                ),
            },
            "top_clients": {
                "window_hours": CLIENT_WINDOW_HOURS,
                "limit": TOP_CLIENTS,
                "clients": top_clients,
                "note": (
                    "fingerprint is sha256(x-forwarded-for + user-agent + "
                    "mcp-session-id) truncated to 12 hex characters. It is "
                    "one-way and none of those three values is stored or "
                    "logged. Ranked by backend_calls, which is the cost."
                ),
            },
            "top_routes": {
                "window_days": ROUTE_WINDOW_DAYS,
                "limit": TOP_ROUTES,
                "routes": top_routes,
                "note": (
                    "What each caller was searching for, from request "
                    "parameters only. `combos` is date x destination "
                    "combinations requested, which is the cost a tool-call "
                    "count of 1 hides. Dates, currency and passengers are on "
                    "the [tool_call] log line, not here -- putting them in the "
                    "key would make the set unbounded. Capped at 500 routes "
                    "per UTC day."
                ),
            },
            # The flat number, named exactly as it is asked for in the runbook
            # so a grep or a jq path finds it without knowing the nesting.
            "fair_use_blocked_today": blocked_today,
            "fair_use": {
                "enabled": self._fair_use_enabled,
                "day_cap": self._fair_use_day_cap,
                "month_cap": self._fair_use_month_cap,
                "gateway_day_cap": self._fair_use_gateway_day_cap,
                "gateway_month_cap": self._fair_use_gateway_month_cap,
                "blocked_today": blocked_today,
                # The escalation. `hard_today` counts 429s, which are the
                # calls that never reached a tool at all -- so they are NOT in
                # blocked_today, in by_key_kind, or in any of the call series
                # above. A day where hard_today climbs and blocked_today has
                # gone flat is the escalation working: the loop is being told
                # to back off in the only language it reads.
                "hard_after": self._fair_use_hard_after,
                "hard_today": hard_today,
                "by_key_kind": by_key_kind,
                "kind_note": (
                    "by_key_kind splits today's traffic by how the counting "
                    "key was chosen. `direct` is IP + user agent, the "
                    "ordinary caps. `config` is a gateway-injected per-user "
                    "configuration blob, also the ordinary caps because it "
                    "identifies one person. `gateway_session` and "
                    "`gateway_pooled` are callers arriving from a published "
                    "LLM-host or gateway egress range, where one connection "
                    "carries an unknown number of real users -- those get the "
                    "higher gateway caps. Blocks under a gateway kind are the "
                    "ones to act on: they mean real users refused each other."
                ),
                "note": (
                    "blocked_today counts tool calls refused since 00:00 UTC "
                    "because one client had spent its fair-use allowance. The "
                    "caps are per client and count BACKEND calls, not tool "
                    "calls. The client key is sha256(x-forwarded-for + "
                    "user-agent) truncated to 12 hex and is NOT the "
                    "fingerprint in top_clients, which also mixes in the "
                    "session id. Refusals are named on the [fair_use] log "
                    "line, by client key only. hard_today counts HTTP 429s "
                    "issued to clients that ignored the soft refusal "
                    f"{self._fair_use_hard_after} times in a rolling hour "
                    "(FAIR_USE_HARD_AFTER, 0 disables); those requests never "
                    "reach a tool, so they are counted here and nowhere else. "
                    "Pooled gateway keys are exempt from the escalation."
                ),
            },
            "durable": self.durable_counters,
            "note": (
                "backend_calls is the Lambda request count; tool_calls is MCP "
                "tool invocations. Buckets are UTC hours, oldest first."
                if self.durable_counters
                else "NOT DURABLE - no shared store configured, so these counts "
                "cover only the process that answered this request and reset on "
                "every cold start. Configure UPSTASH_REDIS_REST_URL / _TOKEN."
            ),
        }

    # ── reporting ────────────────────────────────────────────────────────

    async def snapshot(self) -> dict[str, Any]:
        try:
            counters = await self._store.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read counters: %s", exc)
            counters = {"totals": {}, "by_tier": {}}

        totals = counters.get("totals", {})
        by_tier = counters.get("by_tier", {})

        tool_calls = totals.get("tool_calls", 0)
        backend_calls = totals.get("backend_calls", 0)
        ratio = round(backend_calls / tool_calls, 2) if tool_calls else 0.0

        for counts in by_tier.values():
            calls = counts.get("tool_calls", 0)
            counts["backend_calls_per_tool_call"] = (
                round(counts.get("backend_calls", 0) / calls, 2) if calls else 0.0
            )

        notes = [
            "ad_eligible_calls counts slots this server attached, NOT rendered "
            "impressions. The render beacon fires from inside the Lulu widget "
            "frame directly to ads.getlulu.dev and never reaches this process. "
            "Reconcile these counts against Lulu's reported rendered impressions "
            "to get a true render rate.",
            "backend_calls is the Lambda request count. Summing the "
            "backend_calls field across MCP_CALL stdout lines gives the same "
            "number for any time range.",
        ]
        if not self.durable_counters:
            notes.append(
                "COUNTERS ARE NOT DURABLE: no shared store is configured, so "
                "these totals cover this process only and reset when it "
                "recycles. On serverless the real numbers are higher than "
                "shown, and DAILY_BACKEND_CALL_BUDGET cannot be enforced. "
                "Configure UPSTASH_REDIS_REST_URL / _TOKEN to fix both."
            )
        if getattr(self._store, "degraded", False):
            notes.append(
                "The counter store returned an error recently; totals may be "
                "short by the writes that failed."
            )

        return {
            "uptime_seconds": int(time.time() - self._started_at),
            "totals": totals,
            # Every backend call is cost; every tool call is at most one
            # rendered ad. This ratio is the post-POC viability number.
            "backend_calls_per_tool_call": ratio,
            "by_tier": by_tier,
            "budget": await self.budget_state(),
            "durable_counters": self.durable_counters,
            "file_sink_enabled": self.file_sink_enabled,
            "notes": notes,
        }

    # ── restart recovery (container only) ────────────────────────────────

    def _replay_log(self) -> None:
        """Rebuild in-process counters from the log file.

        Only meaningful for MemoryCounterStore behind a real file: a shared
        store is already authoritative, and replaying into it would
        double-count every record on every restart.
        """
        if self.durable_counters or not isinstance(self._store, MemoryCounterStore):
            return
        assert self._log_path is not None
        if not os.path.exists(self._log_path):
            return
        replayed = 0
        try:
            with open(self._log_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(STDOUT_PREFIX):
                        line = line[len(STDOUT_PREFIX):]
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._store.seed(
                        payload.get("tier") or "unknown",
                        _fields_from_payload(payload),
                        int(payload.get("backend_calls") or 0),
                        float(payload.get("ts") or 0),
                        tool=payload.get("tool"),
                        fingerprint=payload.get("fingerprint"),
                    )
                    replayed += 1
        except OSError as exc:
            logger.warning("could not replay %s: %s", self._log_path, exc)
            return
        if replayed:
            logger.info("replayed %d call records from %s", replayed, self._log_path)


def _fields_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "tool_calls": 1,
        "backend_calls": int(payload.get("backend_calls") or 0),
        "backend_failures": int(payload.get("backend_failures") or 0),
        "results_returned": int(payload.get("results_returned") or 0),
        "ad_eligible_calls": 1 if payload.get("ad_eligible") else 0,
        "blocked_calls": 0 if payload.get("allowed", True) else 1,
        "truncated_calls": 1 if payload.get("truncated") else 0,
        "errored_calls": 1 if payload.get("error") else 0,
    }
