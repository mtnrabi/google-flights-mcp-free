"""Counter stores -- especially the durable one, since the spend guard
is only meaningful on serverless when it is shared across instances."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.stores import (
    RETENTION_HOURS,
    MemoryCounterStore,
    RedisCounterStore,
    build_counter_store,
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
    commands: list[list] = []
    fail: bool = False

    def do_POST(self):  # noqa: N802
        if _StubUpstash.fail:
            self.send_response(500)
            self.end_headers()
            return
        length = int(self.headers.get("content-length", 0))
        commands = json.loads(self.rfile.read(length) or b"[]")
        _StubUpstash.commands.extend(commands)
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
    _StubUpstash.commands = []
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
