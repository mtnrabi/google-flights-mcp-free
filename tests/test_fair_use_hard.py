"""The hard escalation: 429 + Retry-After for a caller that ignores refusals.

The soft refusal shipped on 2026-09-05 is a 200 whose body says
`search_status: "rate_limited"`, `retry: false`. That is the right answer for
a model. It is worth nothing to a script, and on 2026-09-06 we measured
exactly how little: the 04:00Z batch client spent its 150 backend calls at
04:04Z and then made 486 more tool calls before 04:28Z -- about 20 a minute --
because nothing in its code reads a response body
(state/gtm/free-mcp-batch-client-searches-2026-09-06.md).

So this file pins the escalation agreed with grok that morning: first hit
stays a soft structured result, sustained hammering earns an HTTP status code.

1. the counting -- refusals per client over a ROLLING hour, so a caller
   cannot reset the count by waiting for the top of the hour;
2. the trigger -- FAIR_USE_HARD_AFTER refusals, default 20, and not one fewer;
3. the response -- 429, `Retry-After` in seconds until the UTC day rolls over
   and never more than an hour, and a tiny body carrying the same `upgrade`
   object the soft refusal carries;
4. who is exempt -- pooled gateway keys, which stand for an unknown number of
   real people and must never be 429ed as one;
5. how it ends -- the window decays while the caller is still hammering, and
   the counters reset with the hour and with the day.

    python -m pytest mcp_server/tests/test_fair_use_hard.py -q
"""

import calendar
import json
import logging
import time
from dataclasses import replace

import httpx
import pytest

from src.fair_use import (
    GATEWAY_KINDS,
    KIND_DIRECT,
    KIND_GATEWAY_POOLED,
    MAX_RETRY_AFTER_SECONDS,
    PAID_FLIGHTS_URL,
    SIGNIN_FLIGHTS_URL,
    client_key,
    hard_block_body,
    hard_block_message,
    hard_log_line,
    retry_after_seconds,
    upgrade_block,
)
from src.hard_limit import DEFAULT_MCP_PATH, HardLimitMiddleware, hard_limit_middleware
from src.policy import ClientClassifier
from src.server import build_server
from src.settings import load_settings
from src.stores import (
    FAIR_USE_REFUSAL_SLOTS,
    FAIR_USE_REFUSAL_SLOT_SECONDS,
    FAIR_USE_REFUSAL_TTL_SECONDS,
    MemoryCounterStore,
    RedisCounterStore,
    fair_use_hard_key,
    fair_use_refusal_key,
    fair_use_refusal_keys,
)
from src.telemetry import CallRecord, Telemetry

from test_stores import _StubUpstash, upstash  # noqa: F401 - fixture reuse

HOUR = 3600
DAY = 86400

# One caller: a script with its own address, which is what `direct` means.
BATCH_HEADERS = {
    "x-forwarded-for": "198.51.100.7",
    "user-agent": "python-httpx/0.27",
}
BATCH_KEY = client_key(BATCH_HEADERS)


# ── Retry-After ──────────────────────────────────────────────────────────


class TestRetryAfter:
    """Seconds until the daily allowance rolls over, clamped to an hour."""

    def _at(self, hour, minute=0, second=0):
        return calendar.timegm((2026, 9, 6, hour, minute, second, 0, 0, 0))

    def test_it_counts_down_to_midnight_utc(self):
        assert retry_after_seconds(self._at(23, 45)) == 900
        assert retry_after_seconds(self._at(23, 0)) == 3600

    def test_it_is_never_longer_than_an_hour(self):
        """A five-figure Retry-After is a number a scheduler rounds up into
        'never come back', and the day cap is not the only reason a block
        lifts -- raising FAIR_USE_DAY_CAP takes effect immediately."""
        assert retry_after_seconds(self._at(5, 0)) == MAX_RETRY_AFTER_SECONDS
        assert retry_after_seconds(self._at(0, 0, 1)) == MAX_RETRY_AFTER_SECONDS
        assert MAX_RETRY_AFTER_SECONDS == 3600

    def test_it_is_never_zero(self):
        """0 reads as 'retry immediately', which is the opposite of the point."""
        assert retry_after_seconds(self._at(23, 59, 59)) == 1
        assert retry_after_seconds(self._at(23, 59, 60)) >= 1

    def test_the_maximum_is_a_parameter_not_a_literal(self):
        assert retry_after_seconds(self._at(5, 0), maximum=120) == 120


# ── the body and the log line ────────────────────────────────────────────


class TestTheHardBody:
    @pytest.fixture
    def body(self):
        return hard_block_body(900, 150, 2000)

    def test_it_is_the_four_documented_keys(self, body):
        assert set(body) == {"message", "error", "retry_after", "upgrade"}
        assert body["error"] == "rate_limited"
        assert body["retry_after"] == 900

    def test_message_is_the_first_key(self, body):
        """A script that logs the raw dict repr, or a viewer that only shows
        the first field of a JSON object, still has to see this one."""
        assert next(iter(body)) == "message"

    def test_the_message_is_one_plain_sentence_with_the_cap_and_the_wait(
        self, body
    ):
        message = body["message"]
        assert "150 searches/day" in message
        assert "900s" in message
        assert PAID_FLIGHTS_URL in message
        assert "flightpowers.com" in message
        assert "—" not in message, "no em-dash: a period or comma instead"

    def test_hard_block_message_matches_the_body(self):
        assert hard_block_message(900, 150) == (
            "Free fair-use reached (150 searches/day). Retry after 900s, or "
            "use the paid server (BASIC is free): sign in at "
            f"{SIGNIN_FLIGHTS_URL}, or bring your own RapidAPI key at "
            f"{PAID_FLIGHTS_URL}, details: "
            "https://flightpowers.com/"
            "?utm_source=free-mcp&utm_medium=429&utm_campaign=upgrade"
        )

    def test_the_sign_in_url_comes_before_the_keyed_one(self):
        """2026-09-08: the paid server has a Google sign-in, so the header a
        reader of a raw log would otherwise have to configure is now the
        second-best path, not the only one. Order carries the recommendation
        here -- there is no other formatting in a one-line log message."""
        message = hard_block_message(900, 150)
        assert message.index(SIGNIN_FLIGHTS_URL) < message.index(
            f"key at {PAID_FLIGHTS_URL}"
        )

    def test_the_error_matches_the_soft_refusals_status(self, body):
        """`rate_limited` is already the vocabulary a caller sees in a 200
        body. Two spellings of one condition would be one more thing for a
        client to special-case."""
        assert body["error"] == "rate_limited"

    def test_it_carries_the_compact_upgrade_object(self, body):
        """The COMPACT shape (2026-09-09), not the anonymous one.

        A caller in this state is looping and is not reading the body -- that
        is how it got here -- and `message` already carries the whole story
        in one line. The anonymous shape adds `do_this_first` and
        `sign_in_free`, three more strings for a reader who is not there,
        which is what pushed this body past the size the next test pins."""
        assert body["upgrade"] == upgrade_block(150, 2000, signed_in=True)
        assert "do_this_first" not in body["upgrade"]
        assert body["upgrade"]["paid_server"].startswith("https://")

    def test_it_is_small(self):
        """A caller in this state is not reading bodies -- that is how it got
        here. The body is for the human who eventually opens a log."""
        assert len(json.dumps(hard_block_body(900, 150, 2000))) < 2500

    def test_the_log_line_names_the_client_the_count_and_the_wait(self):
        line = hard_log_line("29f0c3237a6e", 21, 900)
        assert line == (
            "[fair_use] action=hard client=29f0c3237a6e "
            "refusals=21 retry_after=900"
        )

    def test_the_log_line_shares_the_prefix_with_warn_and_block(self):
        """One grep has to find all three actions, or a runbook needs three."""
        assert hard_log_line("abc", 1, 1).startswith("[fair_use] action=")


# ── the rolling-hour counter ─────────────────────────────────────────────


def _telemetry(store=None, **kwargs):
    options = dict(
        fair_use_enabled=True,
        fair_use_day_cap=150,
        fair_use_month_cap=2000,
        fair_use_hard_after=20,
    )
    options.update(kwargs)
    return Telemetry(store=store or MemoryCounterStore(), stdout=False, **options)


def _refusal(ts=None, key=BATCH_KEY, kind=KIND_DIRECT):
    return CallRecord(
        timestamp=time.time() if ts is None else ts,
        tool="search_oneway_flights",
        tier="unknown",
        client_name=None,
        source_ip="198.51.100.7",
        widget_capable=False,
        requested_combinations=0,
        backend_calls=0,
        backend_failures=0,
        results_returned=0,
        duration_ms=1,
        truncated=False,
        allowed=True,
        decision_reason="monitor",
        ad_eligible=False,
        error="fair_use_blocked",
        fair_use_key=key,
        fair_use_kind=kind,
        fair_use_blocked=True,
    )


class TestTheRollingWindow:
    @pytest.mark.asyncio
    async def test_a_soft_refusal_is_counted(self):
        telemetry = _telemetry()
        await telemetry.record(_refusal())
        assert await telemetry.fair_use_refusals(BATCH_KEY) == 1

    @pytest.mark.asyncio
    async def test_it_counts_only_this_client(self):
        telemetry = _telemetry()
        for _ in range(5):
            await telemetry.record(_refusal(key="ffffffffffff"))
        assert await telemetry.fair_use_refusals(BATCH_KEY) == 0

    @pytest.mark.asyncio
    async def test_it_sums_across_slots_inside_the_hour(self):
        """The point of the rolling window: a burst spread over the hour is
        one count, not one count per ten-minute slot."""
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        now = time.time()
        for minutes in (0, 12, 25, 41, 49):
            await telemetry.record(_refusal(ts=now - minutes * 60))
        assert await store.fair_use_refusals(BATCH_KEY, now) == 5

    @pytest.mark.asyncio
    async def test_a_refusal_older_than_the_window_has_rolled_off(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        now = time.time()
        await telemetry.record(_refusal(ts=now - 70 * 60))
        assert await store.fair_use_refusals(BATCH_KEY, now) == 0

    @pytest.mark.asyncio
    async def test_the_window_never_reaches_back_more_than_an_hour(self):
        """It may understate by up to ten minutes and must never overstate:
        blocking on a refusal that is over an hour old is the one direction
        that punishes a caller who already backed off."""
        span = FAIR_USE_REFUSAL_SLOTS * FAIR_USE_REFUSAL_SLOT_SECONDS
        assert span == HOUR
        now = time.time()
        oldest = fair_use_refusal_keys(BATCH_KEY, now)[-1]
        assert oldest == fair_use_refusal_key(BATCH_KEY, now - HOUR + 600)

    @pytest.mark.asyncio
    async def test_the_count_does_not_reset_at_the_top_of_the_hour(self):
        """A fixed clock hour would be escapable by arithmetic: 19 refusals at
        10:59 and a clean slate at 11:00, sixty times a day."""
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        # 10:55 and 11:05 on the same day: two clock hours, one rolling hour.
        base = calendar.timegm((2026, 9, 6, 10, 55, 0, 0, 0, 0))
        await telemetry.record(_refusal(ts=base))
        await telemetry.record(_refusal(ts=base + 10 * 60))
        assert await store.fair_use_refusals(BATCH_KEY, base + 10 * 60) == 2

    @pytest.mark.asyncio
    async def test_a_call_that_was_served_is_not_a_refusal(self):
        telemetry = _telemetry()
        served = _refusal()
        await telemetry.record(
            replace(served, fair_use_blocked=False, backend_calls=12, error=None)
        )
        assert await telemetry.fair_use_refusals(BATCH_KEY) == 0

    @pytest.mark.asyncio
    async def test_a_pooled_gateway_key_collects_no_refusals(self):
        """The exemption is enforced where the kind is known accurately.

        `gateway_pooled` and `direct` are the SAME digest for the same caller
        -- only the kind separates them -- and the middleware runs before the
        OpenAI egress feed is guaranteed loaded. Refusing to write the counter
        at all is what makes the exemption independent of that race.
        """
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        for kind in sorted(GATEWAY_KINDS):
            for _ in range(30):
                await telemetry.record(_refusal(kind=kind))
        assert await store.fair_use_refusals(BATCH_KEY, time.time()) == 0
        # It is still counted as a refusal everywhere else, so the block is
        # still visible on /metrics.
        assert await telemetry.fair_use_blocked_today() == 60

    @pytest.mark.asyncio
    async def test_a_store_outage_fails_open(self):
        class Broken(MemoryCounterStore):
            async def fair_use_refusals(self, client_key, now):
                raise RuntimeError("upstash is down")

        assert await _telemetry(Broken()).fair_use_refusals(BATCH_KEY) == 0


class TestTheHardCounter:
    @pytest.mark.asyncio
    async def test_a_hard_block_is_counted_for_the_day(self):
        telemetry = _telemetry()
        await telemetry.record_hard_block(BATCH_KEY)
        await telemetry.record_hard_block(BATCH_KEY)
        assert await telemetry.fair_use_hard_today() == 2

    @pytest.mark.asyncio
    async def test_a_hard_block_does_not_re_arm_the_window(self):
        """If serving a 429 counted a refusal, a caller hammering through the
        block would keep its own window topped up forever and could never come
        back, however long ago it fixed its loop."""
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        for _ in range(50):
            await telemetry.record_hard_block(BATCH_KEY)
        assert await store.fair_use_refusals(BATCH_KEY, time.time()) == 0

    @pytest.mark.asyncio
    async def test_it_resets_with_the_utc_day(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.record_hard_block(BATCH_KEY, now - DAY)
        assert await store.fair_use_hard_today(now) == 0

    @pytest.mark.asyncio
    async def test_recording_never_raises(self):
        class Broken(MemoryCounterStore):
            async def record_hard_block(self, client_key, now):
                raise RuntimeError("upstash is down")

        await _telemetry(Broken()).record_hard_block(BATCH_KEY)


class TestTheRedisKeyShapes:
    def test_the_slot_key_carries_the_ten_minute_window(self):
        # 04:27 UTC is the third ten-minute slot of hour 04, so the label is
        # the hour label plus the digit 2.
        ts = calendar.timegm((2026, 9, 6, 4, 27, 0, 0, 0, 0))
        assert fair_use_refusal_key(BATCH_KEY, ts) == (
            f"mcpads:fu:r:20260906042:{BATCH_KEY}"
        )

    def test_the_hard_key_is_per_day_and_not_per_client(self):
        ts = time.time()
        assert fair_use_hard_key(ts) == (
            f"mcpads:fu:hard:{time.strftime('%Y%m%d', time.gmtime(ts))}"
        )

    def test_the_refusal_ttl_outlives_the_window_and_no_more(self):
        assert FAIR_USE_REFUSAL_TTL_SECONDS == 2 * HOUR

    @pytest.mark.asyncio
    async def test_the_refusal_rides_the_existing_pipeline(self, upstash):
        """One round trip per tool call, still. The refusal counter is two
        more commands in the batch that was already being sent."""
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown",
            {"tool_calls": 1, "blocked_calls": 1},
            0,
            now,
            fair_use_key=BATCH_KEY,
            fair_use_kind=KIND_DIRECT,
            fair_use_blocked=True,
        )
        assert len(_StubUpstash.batches) == 1
        assert await store.fair_use_refusals(BATCH_KEY, now) == 1
        expire = [
            c
            for c in _StubUpstash.commands
            if c[0] == "EXPIRE" and c[1] == fair_use_refusal_key(BATCH_KEY, now)
        ]
        assert expire and int(expire[0][2]) == FAIR_USE_REFUSAL_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_redis_exempts_a_pooled_key_too(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "llm_host",
            {"tool_calls": 1, "blocked_calls": 1},
            0,
            now,
            fair_use_key=BATCH_KEY,
            fair_use_kind=KIND_GATEWAY_POOLED,
            fair_use_blocked=True,
        )
        assert await store.fair_use_refusals(BATCH_KEY, now) == 0
        assert await store.fair_use_blocked_today(now) == 1

    @pytest.mark.asyncio
    async def test_the_hard_counter_round_trips(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.record_hard_block(BATCH_KEY, now)
        await store.record_hard_block(BATCH_KEY, now)
        assert await store.fair_use_hard_today(now) == 2

    @pytest.mark.asyncio
    async def test_reads_fail_open_on_an_outage(self, upstash):
        store = RedisCounterStore(upstash, "token")
        _StubUpstash.fail = True
        assert await store.fair_use_refusals(BATCH_KEY, time.time()) == 0
        assert await store.fair_use_hard_today(time.time()) == 0
        await store.record_hard_block(BATCH_KEY, time.time())


# ── the middleware ───────────────────────────────────────────────────────


class _Downstream:
    """Stands in for the MCP app. Records that it was reached."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _scope(path="/mcp", method="POST", headers=None, query=b"", peer="198.51.100.7"):
    raw = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in (BATCH_HEADERS if headers is None else headers).items()
    ]
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": raw,
        "client": (peer, 51234),
    }


async def _drive(middleware, scope):
    """Run one request through the middleware and collect the response."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body") or b"" for m in sent if m["type"] == "http.response.body"
    )
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]
    }
    return start["status"], headers, body


def _middleware(app, telemetry, **overrides):
    settings = replace(load_settings(), **overrides)
    return HardLimitMiddleware(
        app,
        settings=settings,
        telemetry=telemetry,
        classifier=ClientClassifier(),
    )


async def _seed(telemetry, count, **kwargs):
    for _ in range(count):
        await telemetry.record(_refusal(**kwargs))


class TestSoftFirst:
    @pytest.mark.asyncio
    async def test_a_clean_caller_passes_straight_through(self):
        app = _Downstream()
        status, _, _ = await _drive(_middleware(app, _telemetry()), _scope())
        assert status == 200
        assert app.calls == 1

    @pytest.mark.asyncio
    async def test_the_first_refusal_does_not_escalate(self):
        """The whole shape agreed on 2026-09-06: first hit is a soft,
        structured 200 a model can read and act on."""
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 1)
        status, _, _ = await _drive(_middleware(app, telemetry), _scope())
        assert status == 200
        assert app.calls == 1

    @pytest.mark.asyncio
    async def test_nineteen_refusals_still_pass_through(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 19)
        status, _, _ = await _drive(_middleware(app, telemetry), _scope())
        assert status == 200
        assert app.calls == 1


class TestHardAfterTwenty:
    @pytest.mark.asyncio
    async def test_the_twentieth_refusal_arms_the_429(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 20)
        status, headers, body = await _drive(_middleware(app, telemetry), _scope())
        assert status == 429
        assert app.calls == 0, "a hard-blocked request must not reach the MCP app"
        payload = json.loads(body)
        assert payload["error"] == "rate_limited"
        assert payload["retry_after"] == int(headers["retry-after"])
        assert payload["upgrade"]["paid_server"]
        assert next(iter(payload)) == "message"
        assert payload["message"] == hard_block_message(
            payload["retry_after"], 150
        )

    @pytest.mark.asyncio
    async def test_the_retry_after_header_is_delta_seconds_within_bounds(self):
        telemetry = _telemetry()
        await _seed(telemetry, 25)
        _, headers, _ = await _drive(
            _middleware(_Downstream(), telemetry), _scope()
        )
        retry_after = int(headers["retry-after"])
        assert 1 <= retry_after <= MAX_RETRY_AFTER_SECONDS
        assert retry_after == retry_after_seconds(time.time())

    @pytest.mark.asyncio
    async def test_the_429_is_not_cacheable(self):
        """An intermediary pinning a 429 would outlive the block itself."""
        telemetry = _telemetry()
        await _seed(telemetry, 20)
        _, headers, _ = await _drive(
            _middleware(_Downstream(), telemetry), _scope()
        )
        assert headers["cache-control"] == "no-store"
        assert headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_it_logs_one_hard_line_per_429(self, caplog):
        telemetry = _telemetry()
        await _seed(telemetry, 20)
        with caplog.at_level(logging.INFO, logger="src.hard_limit"):
            await _drive(_middleware(_Downstream(), telemetry), _scope())
        lines = [r.getMessage() for r in caplog.records if "action=hard" in r.getMessage()]
        assert len(lines) == 1
        assert f"client={BATCH_KEY}" in lines[0]
        assert "refusals=20" in lines[0]
        assert "retry_after=" in lines[0]

    @pytest.mark.asyncio
    async def test_the_plain_sentence_is_the_first_line_of_that_log(self, caplog):
        """A script that only ever prints a log record verbatim, and never
        parses `action=hard client=...`, still gets the human sentence -- it
        has to be the FIRST thing in the record, not appended after."""
        telemetry = _telemetry()
        await _seed(telemetry, 20)
        with caplog.at_level(logging.INFO, logger="src.hard_limit"):
            await _drive(_middleware(_Downstream(), telemetry), _scope())
        record = next(
            r for r in caplog.records if "action=hard" in r.getMessage()
        )
        first_line = record.getMessage().splitlines()[0]
        assert first_line.startswith("Free fair-use reached")
        assert "150 searches/day" in first_line
        assert PAID_FLIGHTS_URL in first_line

    @pytest.mark.asyncio
    async def test_the_429_is_counted_and_the_window_is_not_re_armed(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        await _seed(telemetry, 20)
        middleware = _middleware(_Downstream(), telemetry)
        for _ in range(5):
            await _drive(middleware, _scope())
        assert await telemetry.fair_use_hard_today() == 5
        assert await store.fair_use_refusals(BATCH_KEY, time.time()) == 20

    @pytest.mark.asyncio
    async def test_the_threshold_is_configurable(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 3)
        status, _, _ = await _drive(
            _middleware(app, telemetry, fair_use_hard_after=3), _scope()
        )
        assert status == 429

    @pytest.mark.asyncio
    async def test_it_lifts_once_the_window_has_rolled_off(self):
        """The escalation has to be able to end on its own, or a caller that
        fixed its loop is banned until the process forgets."""
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        old = time.time() - 70 * 60
        await _seed(telemetry, 30, ts=old)
        app = _Downstream()
        status, _, _ = await _drive(_middleware(app, telemetry), _scope())
        assert status == 200
        assert app.calls == 1


class TestWhoIsExempt:
    @pytest.mark.asyncio
    async def test_a_pooled_gateway_caller_is_never_hard_blocked(self):
        """Claude.ai reaches every server on earth from one published range.
        429ing that key would 429 every Claude user of this server at once."""
        headers = {
            "x-forwarded-for": f"198.51.100.7, 160.79.104.9",
            "user-agent": "python-httpx/0.27",
        }
        telemetry = _telemetry()
        # Seed against the digest a direct caller with these base parts would
        # have -- which is the same digest gateway_pooled produces.
        await _seed(telemetry, 40, key=client_key(headers))
        app = _Downstream()
        status, _, _ = await _drive(
            _middleware(app, telemetry), _scope(headers=headers, peer="160.79.104.9")
        )
        assert status == 200
        assert app.calls == 1

    @pytest.mark.asyncio
    async def test_a_caller_with_no_headers_at_all_is_never_hard_blocked(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        status, _, _ = await _drive(
            _middleware(app, telemetry), _scope(headers={}, peer=None)
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_health_and_metrics_answer_for_everyone(self):
        """A monitor must not be told 429 because somebody else is looping."""
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        middleware = _middleware(_Downstream(), telemetry)
        for path in ("/health", "/metrics", "/metrics/calls"):
            status, _, _ = await _drive(middleware, _scope(path=path, method="GET"))
            assert status == 200, path

    @pytest.mark.asyncio
    async def test_a_get_to_the_mcp_path_is_untouched(self):
        """GET /mcp is the SSE reconnect leg of the transport, not a call."""
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        status, _, _ = await _drive(
            _middleware(_Downstream(), telemetry), _scope(method="GET")
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_the_trailing_slash_spelling_is_covered(self):
        """`/mcp/` answers via a 307, so leaving it out would be a way round."""
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        status, _, _ = await _drive(
            _middleware(_Downstream(), telemetry), _scope(path="/mcp/")
        )
        assert status == 429


class TestTheOffSwitches:
    @pytest.mark.asyncio
    async def test_hard_after_zero_leaves_the_soft_refusal_alone(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        status, _, _ = await _drive(
            _middleware(app, telemetry, fair_use_hard_after=0), _scope()
        )
        assert status == 200
        assert app.calls == 1

    @pytest.mark.asyncio
    async def test_fair_use_disabled_disables_this_too(self):
        app = _Downstream()
        telemetry = _telemetry()
        await _seed(telemetry, 40)
        status, _, _ = await _drive(
            _middleware(app, telemetry, fair_use_enabled=False), _scope()
        )
        assert status == 200

    def test_a_disabled_escalation_installs_no_middleware_at_all(self, monkeypatch):
        monkeypatch.setenv("FAIR_USE_HARD_AFTER", "0")
        assert list(hard_limit_middleware(build_server(load_settings()))) == []

    def test_an_enabled_escalation_installs_exactly_one(self, monkeypatch):
        monkeypatch.setenv("FAIR_USE_HARD_AFTER", "20")
        installed = list(hard_limit_middleware(build_server(load_settings())))
        assert len(installed) == 1
        assert installed[0].cls is HardLimitMiddleware

    def test_the_default_is_twenty(self, monkeypatch):
        monkeypatch.delenv("FAIR_USE_HARD_AFTER", raising=False)
        assert load_settings().fair_use_hard_after == 20


# ── end to end, over real HTTP ───────────────────────────────────────────


@pytest.fixture
def hard_app(monkeypatch):
    """The real server app with the middleware installed, as Vercel builds it."""
    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.setenv("FAIR_USE_HARD_AFTER", "20")
    server = build_server(load_settings())
    app = server.http_app(
        stateless_http=True, middleware=list(hard_limit_middleware(server))
    )
    return server, app


class TestOverTheWire:
    @pytest.mark.asyncio
    async def test_a_looping_caller_gets_429_before_the_session_manager(
        self, hard_app
    ):
        server, app = hard_app
        await _seed(server.telemetry, 20)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    DEFAULT_MCP_PATH,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        **BATCH_HEADERS,
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        assert response.json()["error"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_an_ordinary_caller_still_initialises(self, hard_app):
        _, app = hard_app
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    DEFAULT_MCP_PATH,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "hard-limit-test", "version": "1"},
                        },
                    },
                    headers={
                        **BATCH_HEADERS,
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_still_answers_a_hard_blocked_caller(self, hard_app):
        server, app = hard_app
        await _seed(server.telemetry, 40)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/health", headers=BATCH_HEADERS)
        assert response.status_code == 200


# ── reporting ────────────────────────────────────────────────────────────


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_calls_carries_hard_today_and_the_threshold(self):
        telemetry = _telemetry()
        await telemetry.record_hard_block(BATCH_KEY)
        await telemetry.record_hard_block(BATCH_KEY)
        series = await telemetry.call_series(24)
        assert series["fair_use"]["hard_today"] == 2
        assert series["fair_use"]["hard_after"] == 20

    @pytest.mark.asyncio
    async def test_hard_blocks_are_not_double_counted_as_soft_refusals(self):
        """They never reached a tool, so they are in neither the call series
        nor blocked_today. A day where hard_today climbs and blocked_today has
        gone flat is the escalation working."""
        telemetry = _telemetry()
        await telemetry.record_hard_block(BATCH_KEY)
        series = await telemetry.call_series(24)
        assert series["fair_use"]["hard_today"] == 1
        assert series["fair_use_blocked_today"] == 0
        assert series["totals"]["tool_calls"] == 0

    @pytest.mark.asyncio
    async def test_the_note_says_where_hard_blocks_are_counted(self):
        note = (await _telemetry().call_series(24))["fair_use"]["note"]
        assert "hard_today" in note
        assert "FAIR_USE_HARD_AFTER" in note
