"""Counter stores -- especially the durable one, since the spend guard
is only meaningful on serverless when it is shared across instances."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.stores import (
    FAIR_USE_DAY_TTL_SECONDS,
    FAIR_USE_MONTH_TTL_SECONDS,
    RETENTION_HOURS,
    ROUTE_KEYS_PER_DAY,
    ROUTE_WINDOW_DAYS,
    MemoryCounterStore,
    RedisCounterStore,
    build_counter_store,
    fair_use_keys,
)


class TestMemoryStore:
    @pytest.mark.asyncio
    async def test_accumulates_totals_and_tiers(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump("llm_host", {"tool_calls": 1, "backend_calls": 15}, 15, now)
        await store.bump("unknown", {"tool_calls": 1, "backend_calls": 4}, 4, now)

        snap = await store.snapshot()
        assert snap["totals"]["backend_calls"] == 19
        assert snap["by_tier"]["llm_host"]["backend_calls"] == 15
        assert snap["by_tier"]["unknown"]["backend_calls"] == 4

    @pytest.mark.asyncio
    async def test_window_excludes_old_buckets(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump("t", {"backend_calls": 5}, 5, now - 90000)
        await store.bump("t", {"backend_calls": 7}, 7, now)
        assert await store.backend_calls_in_window(now) == 7

    @pytest.mark.asyncio
    async def test_declares_itself_not_durable(self):
        # This flag is what makes the server warn instead of silently
        # pretending the budget guard is armed on serverless.
        assert MemoryCounterStore().durable is False


class _StubUpstash(BaseHTTPRequestHandler):
    """Minimal Upstash REST pipeline endpoint."""

    state: dict[str, int] = {}
    # Hashes are a separate keyspace here for the same reason they are in
    # Redis: the per-tool and per-caller breakdowns are HINCRBY/HGETALL, not
    # INCRBY/MGET, because the reader has to enumerate members it cannot know
    # the names of in advance.
    hashes: dict[str, dict[str, str]] = {}
    commands: list[list] = []
    # One entry per HTTP request, so a test can assert that a write which
    # should cost one round trip costs one round trip.
    batches: list[list] = []
    fail: bool = False

    def do_POST(self):  # noqa: N802
        if _StubUpstash.fail:
            self.send_response(500)
            self.end_headers()
            return
        length = int(self.headers.get("content-length", 0))
        commands = json.loads(self.rfile.read(length) or b"[]")
        _StubUpstash.commands.extend(commands)
        _StubUpstash.batches.append(commands)
        results = []
        for command in commands:
            verb = command[0].upper()
            if verb == "INCRBY":
                key, value = command[1], int(command[2])
                _StubUpstash.state[key] = _StubUpstash.state.get(key, 0) + value
                results.append({"result": _StubUpstash.state[key]})
            elif verb == "MGET":
                results.append(
                    {"result": [_StubUpstash.state.get(k) for k in command[1:]]}
                )
            elif verb == "HINCRBY":
                key, field, value = command[1], command[2], int(command[3])
                bucket = _StubUpstash.hashes.setdefault(key, {})
                bucket[field] = str(int(bucket.get(field, 0)) + value)
                results.append({"result": int(bucket[field])})
            elif verb == "HSET":
                bucket = _StubUpstash.hashes.setdefault(command[1], {})
                pairs = command[2:]
                for i in range(0, len(pairs) - 1, 2):
                    bucket[pairs[i]] = pairs[i + 1]
                results.append({"result": len(pairs) // 2})
            elif verb == "HSETNX":
                bucket = _StubUpstash.hashes.setdefault(command[1], {})
                if command[2] in bucket:
                    results.append({"result": 0})
                else:
                    bucket[command[2]] = command[3]
                    results.append({"result": 1})
            elif verb == "HLEN":
                results.append(
                    {"result": len(_StubUpstash.hashes.get(command[1]) or {})}
                )
            elif verb == "HGETALL":
                # Upstash answers a hash as a flat [field, value, ...] array.
                flat = []
                for field, value in (
                    _StubUpstash.hashes.get(command[1]) or {}
                ).items():
                    flat.extend([field, value])
                results.append({"result": flat})
            else:
                results.append({"result": "OK"})
        body = json.dumps(results).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def upstash():
    _StubUpstash.state = {}
    _StubUpstash.hashes = {}
    _StubUpstash.commands = []
    _StubUpstash.batches = []
    _StubUpstash.fail = False
    server = HTTPServer(("127.0.0.1", 0), _StubUpstash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class TestRedisStore:
    @pytest.mark.asyncio
    async def test_increments_totals_and_per_tier(self, upstash):
        store = RedisCounterStore(upstash, "token")
        await store.bump("llm_host", {"tool_calls": 1, "backend_calls": 15}, 15, time.time())

        verbs = [c[0] for c in _StubUpstash.commands]
        assert "INCRBY" in verbs
        keys = [c[1] for c in _StubUpstash.commands if c[0] == "INCRBY"]
        assert any("total:backend_calls" in k for k in keys)
        assert any("tier:llm_host:backend_calls" in k for k in keys)

    @pytest.mark.asyncio
    async def test_window_buckets_get_a_ttl(self, upstash):
        # Without EXPIRE the window keys accumulate forever.
        store = RedisCounterStore(upstash, "token")
        await store.bump("t", {"backend_calls": 3}, 3, time.time())
        assert any(c[0] == "EXPIRE" for c in _StubUpstash.commands)

    @pytest.mark.asyncio
    async def test_window_sums_across_hour_buckets(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump("t", {"backend_calls": 4}, 4, now)
        await store.bump("t", {"backend_calls": 6}, 6, now - 3600)
        assert await store.backend_calls_in_window(now) == 10

    @pytest.mark.asyncio
    async def test_window_ignores_buckets_older_than_24h(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump("t", {"backend_calls": 99}, 99, now - 90000)
        await store.bump("t", {"backend_calls": 2}, 2, now)
        assert await store.backend_calls_in_window(now) == 2

    @pytest.mark.asyncio
    async def test_snapshot_reads_totals(self, upstash):
        store = RedisCounterStore(upstash, "token")
        await store.bump("t", {"tool_calls": 1, "backend_calls": 5}, 5, time.time())
        snap = await store.snapshot()
        assert snap["totals"]["backend_calls"] == 5
        assert snap["totals"]["tool_calls"] == 1

    @pytest.mark.asyncio
    async def test_store_outage_never_raises(self, upstash):
        # A counter outage must not become an outage of flight search.
        _StubUpstash.fail = True
        store = RedisCounterStore(upstash, "token")
        await store.bump("t", {"backend_calls": 1}, 1, time.time())
        assert await store.backend_calls_in_window(time.time()) == 0
        assert await store.snapshot() == {"totals": {}, "by_tier": {}}
        assert store.degraded, "an outage must be admitted, not hidden"

    @pytest.mark.asyncio
    async def test_unreachable_host_never_raises(self):
        store = RedisCounterStore("http://127.0.0.1:1", "token")
        await store.bump("t", {"backend_calls": 1}, 1, time.time())
        assert await store.backend_calls_in_window(time.time()) == 0


class TestStoreSelection:
    def test_memory_when_no_credentials(self, monkeypatch):
        for name in (
            "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
            "KV_REST_API_URL", "KV_REST_API_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        assert isinstance(build_counter_store(), MemoryCounterStore)

    def test_redis_from_upstash_vars(self, monkeypatch):
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
        store = build_counter_store()
        assert isinstance(store, RedisCounterStore)
        assert store.durable is True

    def test_redis_from_legacy_kv_vars(self, monkeypatch):
        # Stores migrated off the retired Vercel KV carry KV_REST_API_*.
        for name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("KV_REST_API_URL", "https://y.upstash.io")
        monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
        assert isinstance(build_counter_store(), RedisCounterStore)

    def test_strips_quotes_from_credentials(self, monkeypatch):
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", '"https://z.upstash.io"')
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", '"tok"')
        store = build_counter_store()
        assert isinstance(store, RedisCounterStore)
        assert store._url == "https://z.upstash.io"


class TestCallSeries:
    """Per-hour time series behind /metrics/calls."""

    @pytest.mark.asyncio
    async def test_memory_series_buckets_by_hour(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump("t", {"tool_calls": 1, "backend_calls": 5}, 5, now)
        await store.bump("t", {"tool_calls": 1, "backend_calls": 3}, 3, now)
        await store.bump("t", {"tool_calls": 1, "backend_calls": 7}, 7, now - 3600)

        series = await store.call_series(now, 3)
        assert [b["hour"] for b in series] == sorted(b["hour"] for b in series), \
            "oldest first"
        assert series[-1]["backend_calls"] == 8
        assert series[-1]["tool_calls"] == 2
        assert series[-2]["backend_calls"] == 7

    @pytest.mark.asyncio
    async def test_memory_series_pads_empty_hours(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump("t", {"tool_calls": 1, "backend_calls": 2}, 2, now)
        series = await store.call_series(now, 5)
        assert len(series) == 5
        assert sum(b["backend_calls"] for b in series) == 2

    @pytest.mark.asyncio
    async def test_budget_window_still_agrees_with_the_series(self):
        # The spend guard and the series read the same buckets, so they must
        # never disagree.
        store = MemoryCounterStore()
        now = time.time()
        for h in range(5):
            await store.bump("t", {"tool_calls": 1, "backend_calls": 2}, 2, now - h * 3600)
        series = await store.call_series(now, 24)
        assert sum(b["backend_calls"] for b in series) == 10
        assert await store.backend_calls_in_window(now) == 10

    @pytest.mark.asyncio
    async def test_redis_series_reads_hourly_keys(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump("t", {"tool_calls": 1, "backend_calls": 4}, 4, now)
        await store.bump("t", {"tool_calls": 1, "backend_calls": 6}, 6, now - 3600)

        series = await store.call_series(now, 2)
        assert len(series) == 2
        assert series[0]["backend_calls"] == 6
        assert series[1]["backend_calls"] == 4
        assert series[1]["tool_calls"] == 1

    @pytest.mark.asyncio
    async def test_redis_series_survives_an_outage(self, upstash):
        _StubUpstash.fail = True
        store = RedisCounterStore(upstash, "token")
        series = await store.call_series(time.time(), 3)
        assert len(series) == 3
        assert all(b["backend_calls"] == 0 for b in series)

    @pytest.mark.asyncio
    async def test_long_range_is_chunked_not_one_giant_mget(self, upstash):
        store = RedisCounterStore(upstash, "token")
        await store.call_series(time.time(), 500)
        mgets = [c for c in _StubUpstash.commands if c[0].upper() == "MGET"]
        assert mgets, "expected MGET calls"
        assert all(len(c) - 1 <= 200 for c in mgets), "MGET chunks must stay bounded"

    @pytest.mark.asyncio
    async def test_range_is_capped(self, upstash):
        store = RedisCounterStore(upstash, "token")
        series = await store.call_series(time.time(), 100000)
        assert len(series) <= RETENTION_HOURS


class TestRedisPerTier:
    """The per-tier breakdown was written but never read back — that gap is
    exactly the data monitor mode exists to collect."""

    @pytest.mark.asyncio
    async def test_snapshot_returns_by_tier(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "llm_host", {"tool_calls": 1, "backend_calls": 10, "ad_eligible_calls": 1}, 10, now
        )
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 4, "ad_eligible_calls": 0}, 4, now
        )

        snap = await store.snapshot()
        assert snap["totals"]["backend_calls"] == 14
        assert snap["by_tier"]["llm_host"]["backend_calls"] == 10
        assert snap["by_tier"]["llm_host"]["ad_eligible_calls"] == 1
        assert snap["by_tier"]["unknown"]["backend_calls"] == 4
        assert "ad_eligible_calls" not in snap["by_tier"]["unknown"]

    @pytest.mark.asyncio
    async def test_tiers_with_no_traffic_are_omitted(self, upstash):
        store = RedisCounterStore(upstash, "token")
        await store.bump("llm_host", {"tool_calls": 1}, 0, time.time())
        snap = await store.snapshot()
        assert set(snap["by_tier"]) == {"llm_host"}

    @pytest.mark.asyncio
    async def test_every_policy_tier_is_readable(self, upstash):
        # Guards against a tier being added in policy.py and silently missing
        # from the metrics.
        from src.stores import KNOWN_TIERS

        store = RedisCounterStore(upstash, "token")
        for tier in KNOWN_TIERS:
            await store.bump(tier, {"tool_calls": 1}, 1, time.time())
        snap = await store.snapshot()
        assert set(snap["by_tier"]) == set(KNOWN_TIERS)

    @pytest.mark.asyncio
    async def test_by_tier_empty_on_outage_not_raising(self, upstash):
        _StubUpstash.fail = True
        store = RedisCounterStore(upstash, "token")
        snap = await store.snapshot()
        assert snap["by_tier"] == {}


class TestDurableBreakdowns:
    """Per-tool and per-caller hourly counters, on the store that survives a
    cold start. The in-process equivalents are covered in test_attribution.py;
    what matters here is that the Redis shape round-trips, because that is the
    one that actually holds the production numbers.
    """

    @pytest.mark.asyncio
    async def test_tool_and_caller_hashes_round_trip(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        # In call order, which is the order production writes in: `last` is
        # the most recent write, not a running max.
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 6}, 6, now - 3600,
            tool="search_oneway_flights", fingerprint="batch0000001",
        )
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 6}, 6, now,
            tool="search_oneway_flights", fingerprint="batch0000001",
        )
        await store.bump(
            "llm_host", {"tool_calls": 1, "backend_calls": 1}, 1, now,
            tool="search_hotels", fingerprint="person000001",
        )

        by_tool = await store.tool_series(now, 24)
        assert by_tool["search_oneway_flights"] == {
            "tool_calls": 2, "backend_calls": 12
        }
        assert by_tool["search_hotels"] == {"tool_calls": 1, "backend_calls": 1}

        clients = await store.top_clients(now, 24, 20)
        assert [c["fingerprint"] for c in clients] == [
            "batch0000001", "person000001"
        ]
        assert clients[0]["backend_calls"] == 12
        assert clients[0]["active_hours"] == 2
        assert clients[0]["tier"] == "unknown"
        assert clients[0]["first_seen"] < clients[0]["last_seen"]
        assert clients[1]["tier"] == "llm_host"

    @pytest.mark.asyncio
    async def test_first_seen_is_not_overwritten_by_later_calls(self, upstash):
        """HSETNX, not HSET. Otherwise every row says the client appeared just
        now and the breakdown cannot tell a batch job from a newcomer."""
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, now - 72 * 3600,
            fingerprint="batch0000001",
        )
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, now,
            fingerprint="batch0000001",
        )
        first = _StubUpstash.hashes["mcpads:fp:batch0000001"]["first"]
        assert int(first) == int(now - 72 * 3600)

    @pytest.mark.asyncio
    async def test_the_breakdown_keys_expire_like_every_other_bucket(self, upstash):
        """One retention number, in one place. An unbounded fingerprint
        keyspace with no TTL is how a counter store turns into a bill."""
        store = RedisCounterStore(upstash, "token")
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, time.time(),
            tool="search_hotels", fingerprint="person000001",
        )
        expired = {
            c[1]: int(c[2]) for c in _StubUpstash.commands if c[0] == "EXPIRE"
        }
        assert any(":tool:" in key for key in expired)
        assert any(":fp:" in key for key in expired)
        assert set(expired.values()) == {RETENTION_HOURS * 3600}

    @pytest.mark.asyncio
    async def test_tool_series_by_day_round_trips_with_the_daily_keys(
        self, upstash
    ):
        """The per-tool counters read like the route histogram: per UTC day,
        not per hour, so a multi-week `by_tool` window is a handful of
        HGETALLs instead of hundreds of them."""
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        yesterday = now - 86400
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 10}, 10, yesterday,
            tool="search_oneway_flights",
        )
        await store.bump(
            "unknown", {"tool_calls": 1, "backend_calls": 5}, 5, now,
            tool="search_oneway_flights",
        )
        await store.bump(
            "llm_host", {"tool_calls": 1, "backend_calls": 1}, 1, now,
            tool="search_hotels",
        )

        # A 1-day window only sees today's writes.
        today_only = await store.tool_series_by_day(now, 1)
        assert today_only["search_oneway_flights"] == {
            "tool_calls": 1, "backend_calls": 5
        }
        assert "search_hotels" in today_only

        # A 2-day window sums across the UTC-day boundary.
        both_days = await store.tool_series_by_day(now, 2)
        assert both_days["search_oneway_flights"] == {
            "tool_calls": 2, "backend_calls": 15
        }
        assert both_days["search_hotels"] == {"tool_calls": 1, "backend_calls": 1}

        day = time.strftime("%Y%m%d", time.gmtime(now))
        assert set(_StubUpstash.hashes[f"mcpads:d:{day}:tool:t"]) == {
            "search_oneway_flights", "search_hotels"
        }
        expired = {
            c[1]: int(c[2]) for c in _StubUpstash.commands if c[0] == "EXPIRE"
        }
        assert any(key == f"mcpads:d:{day}:tool:t" for key in expired)
        assert any(key == f"mcpads:d:{day}:tool:b" for key in expired)
        assert set(expired.values()) == {RETENTION_HOURS * 3600}

    @pytest.mark.asyncio
    async def test_route_hash_round_trips_with_the_daily_keys(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        for _ in range(2):
            await store.bump(
                "unknown", {"tool_calls": 1, "backend_calls": 15}, 15, now,
                tool="search_oneway_flights", fingerprint="batch0000001",
                route="batch0000001|LHR|JFK,BOS", combinations=28,
            )
        await store.bump(
            "llm_host", {"tool_calls": 1, "backend_calls": 1}, 1, now,
            tool="search_hotels", fingerprint="person000001",
            route="person000001|-|rome", combinations=1,
        )

        routes = await store.top_routes(now, ROUTE_WINDOW_DAYS, 20)
        assert routes[0] == {
            "fingerprint": "batch0000001",
            "from": "LHR",
            "to": "JFK,BOS",
            "tool_calls": 2,
            "combos": 56,
            "backend_calls": 30,
        }
        assert routes[1]["to"] == "rome" and routes[1]["from"] is None
        expired = {
            c[1]: int(c[2]) for c in _StubUpstash.commands if c[0] == "EXPIRE"
        }
        assert any(":route:" in key for key in expired)
        assert set(expired.values()) == {RETENTION_HOURS * 3600}

    @pytest.mark.asyncio
    async def test_the_route_hash_is_capped_per_day(self, upstash):
        """An origin x destination sweep must not write an unbounded hash into
        a store nobody is watching."""
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        for index in range(ROUTE_KEYS_PER_DAY + 50):
            await store.bump(
                "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, now,
                fingerprint="batch0000001",
                route=f"batch0000001|LHR|X{index:04d}", combinations=1,
            )
        day = time.strftime("%Y%m%d", time.gmtime(now))
        assert len(_StubUpstash.hashes[f"mcpads:d:{day}:route:t"]) == (
            ROUTE_KEYS_PER_DAY
        )

    @pytest.mark.asyncio
    async def test_a_second_instance_learns_the_cap_from_hlen(self, upstash):
        """The cap is enforced from a process-local count, and serverless runs
        many processes. Each bump asks for HLEN in the same pipeline, so a
        fresh instance overshoots by one route and then stops -- not by another
        whole 500."""
        first = RedisCounterStore(upstash, "token")
        now = time.time()
        for index in range(ROUTE_KEYS_PER_DAY):
            await first.bump(
                "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, now,
                fingerprint="batch0000001",
                route=f"batch0000001|LHR|X{index:04d}", combinations=1,
            )

        second = RedisCounterStore(upstash, "token")
        for suffix in ("NEW1", "NEW2", "NEW3"):
            await second.bump(
                "unknown", {"tool_calls": 1, "backend_calls": 1}, 1, now,
                fingerprint="batch0000001",
                route=f"batch0000001|LHR|{suffix}", combinations=1,
            )

        day = time.strftime("%Y%m%d", time.gmtime(now))
        members = _StubUpstash.hashes[f"mcpads:d:{day}:route:t"]
        assert len(members) == ROUTE_KEYS_PER_DAY + 1
        assert "batch0000001|LHR|NEW1" in members
        assert "batch0000001|LHR|NEW2" not in members

    @pytest.mark.asyncio
    async def test_an_outage_returns_empty_breakdowns_rather_than_raising(
        self, upstash
    ):
        _StubUpstash.fail = True
        store = RedisCounterStore(upstash, "token")
        assert await store.tool_series(time.time(), 24) == {}
        assert await store.tool_series_by_day(time.time(), 2) == {}
        assert await store.top_clients(time.time(), 24, 20) == []
        assert await store.top_routes(time.time(), ROUTE_WINDOW_DAYS, 20) == []


class TestFairUseCounters:
    """The per-client caps, in the store that makes them real.

    On serverless these numbers only mean something shared: per-instance
    counters mean a client gets a fresh 150 on every cold start, which is not
    a cap, it is a delay.
    """

    @pytest.mark.asyncio
    async def test_a_tool_call_writes_both_windows_in_one_round_trip(self, upstash):
        # One round trip, not two. The fair-use write rides in the same
        # pipeline as the counters, the breakdowns and the route histogram;
        # the only extra request fair use adds anywhere is the read that
        # decides whether the call happens at all.
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown",
            {"tool_calls": 1, "backend_calls": 12},
            12,
            now,
            tool="search_oneway_flights",
            fair_use_key="abc123abc123",
        )
        assert len(_StubUpstash.batches) == 1

        day_key, month_key = fair_use_keys("abc123abc123", now)
        assert _StubUpstash.state[day_key] == 12
        assert _StubUpstash.state[month_key] == 12

    @pytest.mark.asyncio
    async def test_each_window_gets_a_ttl_that_outlives_it(self, upstash):
        # Without these the keyspace grows one key per client per day forever.
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown", {"backend_calls": 3}, 3, now, fair_use_key="abc123abc123"
        )
        day_key, month_key = fair_use_keys("abc123abc123", now)
        expires = {
            command[1]: int(command[2])
            for command in _StubUpstash.commands
            if command[0].upper() == "EXPIRE"
        }
        assert expires[day_key] == FAIR_USE_DAY_TTL_SECONDS
        assert expires[month_key] == FAIR_USE_MONTH_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_the_usage_read_is_one_command(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown", {"backend_calls": 40}, 40, now, fair_use_key="abc123abc123"
        )
        _StubUpstash.batches.clear()
        assert await store.fair_use_usage("abc123abc123", now) == (40, 40)
        assert len(_StubUpstash.batches) == 1

    @pytest.mark.asyncio
    async def test_clients_do_not_share_an_allowance(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown", {"backend_calls": 40}, 40, now, fair_use_key="aaaaaaaaaaaa"
        )
        assert await store.fair_use_usage("bbbbbbbbbbbb", now) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_refusal_is_counted_without_spending_an_allowance(self, upstash):
        store = RedisCounterStore(upstash, "token")
        now = time.time()
        await store.bump(
            "unknown",
            {"tool_calls": 1},
            0,
            now,
            fair_use_key="abc123abc123",
            fair_use_blocked=True,
        )
        assert await store.fair_use_usage("abc123abc123", now) == (0, 0)
        assert await store.fair_use_blocked_today(now) == 1

    @pytest.mark.asyncio
    async def test_an_outage_reads_as_zero_so_nobody_is_refused(self, upstash):
        # Fail open. The alternative turns a store blip into a refusal for
        # every caller, and the rolling daily budget is still the real
        # ceiling on spend.
        store = RedisCounterStore(upstash, "token")
        _StubUpstash.fail = True
        assert await store.fair_use_usage("abc123abc123", time.time()) == (0, 0)
