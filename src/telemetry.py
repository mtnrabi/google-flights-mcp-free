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

from .stores import CounterStore, MemoryCounterStore

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 60 * 60
STDOUT_PREFIX = "MCP_CALL "


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
    ) -> None:
        self._store: CounterStore = store or MemoryCounterStore()
        self._daily_budget = max(0, daily_budget)
        self._degrade_at = min(max(degrade_at, 0.0), 1.0)
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
        try:
            await self._store.bump(
                record.tier,
                record.counter_fields(),
                record.backend_calls,
                record.timestamp,
            )
        except Exception as exc:  # noqa: BLE001 - counters are not the product
            logger.warning("counter update failed: %s", exc)

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
