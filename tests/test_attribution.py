"""Who called, and for which tool -- the two questions the counters could not answer.

On 2026-09-04 the durable counters said this server had made 31,239 backend
Lambda calls since launch, and 79.3% of them landed inside a single 04:00Z hour
(``state/gtm/free-mcp-usage-2026-09-04.md``). Both facts were exact and neither
was actionable, because the counters are keyed by *tier* alone:

* which tool caused a backend call was recorded only on the ephemeral
  ``MCP_CALL`` stdout line, which Vercel Hobby keeps for about an hour;
* nothing counted callers at all, so "one scheduled client" was an inference
  from the shape of a time series rather than a measurement;
* and on the Lambda side, our calls arrived at the same function URL as
  RapidAPI Hub traffic carrying the same two headers, so no CloudWatch query
  could separate them for any window, past or future.

Three things close it, and this file pins all three:

1. every backend call carries ``X-FP-Source`` and ``X-FP-Tool``;
2. hourly counters are kept per tool and per client fingerprint;
3. ``/metrics/calls`` returns both.

Observation only. Nothing here blocks, caps, or changes a search -- see
``TestNothingIsEnforced``.

    python -m pytest mcp_server/tests -q
"""

import logging
import time

import httpx
import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.hotels_lambda_client import HotelsLambdaClient
from src.lambda_client import LambdaClient
from src.policy import TIER_LLM_HOST, TIER_UNKNOWN, client_fingerprint
from src.server import build_server
from src.settings import DEFAULT_SOURCE, load_settings
from src.stores import (
    CLIENT_WINDOW_HOURS,
    ROUTE_KEYS_PER_DAY,
    ROUTE_WINDOW_DAYS,
    TOP_CLIENTS,
    TOP_ROUTES,
    MemoryCounterStore,
)
from src.telemetry import TOOL_CALL_PREFIX, CallRecord, RouteRecord, Telemetry


def _record(**overrides):
    base = dict(
        timestamp=time.time(),
        tool="search_oneway_flights",
        tier=TIER_UNKNOWN,
        client_name="claude-ai",
        source_ip="1.2.3.4",
        widget_capable=True,
        requested_combinations=1,
        backend_calls=4,
        backend_failures=0,
        results_returned=10,
        duration_ms=120,
        truncated=False,
        allowed=True,
        decision_reason="monitor",
        ad_eligible=True,
    )
    base.update(overrides)
    return CallRecord(**base)


def _route(**overrides):
    base = dict(
        origin="LHR",
        destination="JFK,BOS",
        dates="2026-10-01..2026-10-14",
        currency="EUR",
        passengers="1",
        combinations=28,
    )
    base.update(overrides)
    return RouteRecord(**base)


def _tool_call_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(TOOL_CALL_PREFIX)
    ]


def _fields(line: str) -> dict[str, str]:
    head, _, rest = line.partition(" ")
    assert head == TOOL_CALL_PREFIX
    return dict(part.split("=", 1) for part in rest.split(" "))


# --- 1. the headers on the wire ----------------------------------------------


class TestBackendCallsAreTagged:
    @pytest.mark.asyncio
    async def test_flight_searches_name_the_server_and_the_tool(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=[])

        async with LambdaClient(
            "https://lambda.test.invalid",
            "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            tool="search_roundtrip_flights",
        ) as client:
            await client.search("roundtrip", {"from_airport": "TLV"})

        assert seen["x-fp-source"] == DEFAULT_SOURCE == "lulu"
        assert seen["x-fp-tool"] == "search_roundtrip_flights"
        # The auth header is what it always was; this is additive.
        assert seen["x-rapidapi-proxy-secret"] == "secret"

    @pytest.mark.asyncio
    async def test_hotel_searches_are_tagged_the_same_way(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"properties": []})

        async with HotelsLambdaClient(
            "https://hotels.test.invalid",
            "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            tool="find_hotel_by_name",
        ) as client:
            await client.search("hotel_by_name", {"hotel_name": "Artemide"})

        assert seen["x-fp-source"] == "lulu"
        assert seen["x-fp-tool"] == "find_hotel_by_name"

    @pytest.mark.asyncio
    async def test_a_different_deployment_can_call_itself_something_else(self):
        """The paid server and api_proxy run the same shape of code; the header
        is a value, not a constant, so one directory does not have to lie."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=[])

        async with LambdaClient(
            "https://lambda.test.invalid",
            "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            source="api-front",
        ) as client:
            await client.search("oneway", {})

        assert seen["x-fp-source"] == "api-front"
        # No tool named -- an HTTP front has none. Absent, not empty.
        assert "x-fp-tool" not in seen


# --- 2. the fingerprint ------------------------------------------------------


class TestClientFingerprint:
    HEADERS = {
        "x-forwarded-for": "160.79.104.10, 10.0.0.1",
        "user-agent": "python-httpx/0.27",
        "mcp-session-id": "abc123",
    }

    def test_is_stable_for_the_same_caller(self):
        assert client_fingerprint(self.HEADERS) == client_fingerprint(self.HEADERS)

    def test_ignores_proxy_hops_after_the_client(self):
        """The leftmost X-Forwarded-For entry is the caller; the hops behind it
        change without the caller changing."""
        other = dict(self.HEADERS, **{"x-forwarded-for": "160.79.104.10, 10.9.9.9"})
        assert client_fingerprint(other) == client_fingerprint(self.HEADERS)

    def test_a_different_caller_gets_a_different_id(self):
        other = dict(self.HEADERS, **{"x-forwarded-for": "203.0.113.7"})
        assert client_fingerprint(other) != client_fingerprint(self.HEADERS)

    def test_is_one_way_and_carries_none_of_the_inputs(self):
        """An IP and a user agent are the caller's data. We store the digest and
        nothing else, so /metrics/calls can name a client without holding one."""
        fingerprint = client_fingerprint(self.HEADERS)
        assert len(fingerprint) == 12
        assert all(c in "0123456789abcdef" for c in fingerprint)
        for value in ("160.79.104.10", "python-httpx", "abc123", "10.0.0.1"):
            assert value not in fingerprint

    def test_no_identifying_headers_means_no_caller(self):
        """stdio, tests, a direct local connection. Better than counting every
        one of them as the same very busy client."""
        assert client_fingerprint({}) is None
        assert client_fingerprint({"accept": "application/json"}) is None


# --- 3. the counters, and what /metrics/calls returns ------------------------


class TestPerToolCounters:
    @pytest.mark.asyncio
    async def test_backend_calls_are_split_by_tool(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 12}, 12, now,
            tool="search_oneway_flights",
        )
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 3}, 3, now,
            tool="search_hotels",
        )
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 5}, 5, now - 3600,
            tool="search_oneway_flights",
        )

        by_tool = await store.tool_series(now, 24)
        assert by_tool["search_oneway_flights"] == {
            "tool_calls": 2, "backend_calls": 17
        }
        assert by_tool["search_hotels"] == {"tool_calls": 1, "backend_calls": 3}
        # Ordered by cost, because that is the column the answer is read from.
        assert list(by_tool) == ["search_oneway_flights", "search_hotels"]

    @pytest.mark.asyncio
    async def test_an_untagged_call_still_counts_in_the_totals(self):
        """Adding a breakdown must not be able to lose a call from the number
        the spend guard reads."""
        store = MemoryCounterStore()
        now = time.time()
        await store.bump(TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 7}, 7, now)

        assert await store.backend_calls_in_window(now) == 7
        assert await store.tool_series(now, 24) == {}
        assert await store.tool_series_by_day(now, 1) == {}

    @pytest.mark.asyncio
    async def test_by_day_counters_aggregate_across_utc_days(self):
        """Unlike the hourly breakdown, this is what `/metrics/calls` reads
        for `by_tool` -- it has to sum across day boundaries, not just hours
        inside one day, to answer a window wider than ~24h."""
        store = MemoryCounterStore()
        now = time.time()
        yesterday = now - 86400
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 10}, 10, yesterday,
            tool="search_oneway_flights",
        )
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 5}, 5, now,
            tool="search_oneway_flights",
        )
        await store.bump(
            TIER_LLM_HOST, {"tool_calls": 1, "backend_calls": 1}, 1, now,
            tool="search_hotels",
        )

        assert await store.tool_series_by_day(now, 1) == {
            "search_oneway_flights": {"tool_calls": 1, "backend_calls": 5},
            "search_hotels": {"tool_calls": 1, "backend_calls": 1},
        }
        assert await store.tool_series_by_day(now, 2) == {
            "search_oneway_flights": {"tool_calls": 2, "backend_calls": 15},
            "search_hotels": {"tool_calls": 1, "backend_calls": 1},
        }


class TestClientBreakdown:
    @pytest.mark.asyncio
    async def test_names_the_heaviest_callers_with_when_they_appeared(self):
        store = MemoryCounterStore()
        now = time.time()
        # A scheduled batch: many backend calls, one hour a day, seen for days.
        for hours_ago in (0, 24, 48):
            await store.bump(
                TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 500}, 500,
                now - hours_ago * 3600, tool="search_oneway_flights",
                fingerprint="batch0000001",
            )
        # A person: a couple of calls, this hour.
        await store.bump(
            TIER_LLM_HOST, {"tool_calls": 1, "backend_calls": 3}, 3, now,
            tool="search_hotels", fingerprint="person000001",
        )

        clients = await store.top_clients(now, CLIENT_WINDOW_HOURS, TOP_CLIENTS)
        assert [c["fingerprint"] for c in clients] == [
            "batch0000001", "person000001"
        ]

        batch = clients[0]
        assert batch["backend_calls"] == 500  # inside the 24h window
        assert batch["tier"] == TIER_UNKNOWN
        # first_seen reaches past the window -- that is what distinguishes a
        # long-running scheduled client from one that showed up this morning.
        assert batch["first_seen"] < batch["last_seen"]
        assert clients[1]["tier"] == TIER_LLM_HOST

    @pytest.mark.asyncio
    async def test_the_list_is_capped(self):
        store = MemoryCounterStore()
        now = time.time()
        for index in range(TOP_CLIENTS + 5):
            await store.bump(
                TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": index + 1},
                index + 1, now, fingerprint=f"fp{index:010d}",
            )
        clients = await store.top_clients(now, CLIENT_WINDOW_HOURS, TOP_CLIENTS)
        assert len(clients) == TOP_CLIENTS
        assert clients[0]["backend_calls"] == TOP_CLIENTS + 5


class TestMetricsCallsPayload:
    @pytest.mark.asyncio
    async def test_the_series_carries_both_breakdowns(self):
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        await telemetry.record(_record(fingerprint="batch0000001"))
        await telemetry.record(
            _record(tool="search_hotels", backend_calls=1, fingerprint="person000001")
        )

        series = await telemetry.call_series(24)
        assert series["totals"]["backend_calls"] == 5
        assert series["by_tool"]["search_oneway_flights"]["backend_calls"] == 4
        assert series["by_tool"]["search_hotels"]["backend_calls"] == 1
        # The default (hours=24, what a caller gets with no `hours=` at all)
        # still reads as "the last day" -- one UTC day bucket.
        assert series["by_tool_window"]["days"] == 1

        top = series["top_clients"]
        assert top["window_hours"] == 24 and top["limit"] == 20
        assert {c["fingerprint"] for c in top["clients"]} == {
            "batch0000001", "person000001"
        }
        # The note has to say what a fingerprint is, because the number is
        # useless to a reader who cannot tell whether it identifies a person.
        assert "sha256" in top["note"]

    @pytest.mark.asyncio
    async def test_by_tool_answers_a_window_wider_than_a_day(self):
        """The gap this closes: `by_tool` used to be stuck at ~24h no matter
        what `hours=` asked for, so "flights vs hotels since release" was
        unanswerable. A 48h request must reach back a full UTC day further."""
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        now = time.time()
        await telemetry.record(_record(timestamp=now - 86400))
        await telemetry.record(_record(tool="search_hotels", backend_calls=1))

        series = await telemetry.call_series(48)
        assert series["by_tool_window"]["days"] == 2
        assert series["by_tool"]["search_oneway_flights"]["backend_calls"] == 4
        assert series["by_tool"]["search_hotels"]["backend_calls"] == 1

    @pytest.mark.asyncio
    async def test_by_tool_window_days_is_capped_at_retention(self):
        from src.stores import RETENTION_HOURS

        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        series = await telemetry.call_series(RETENTION_HOURS * 10)
        assert series["by_tool_window"]["days"] == RETENTION_HOURS // 24

    @pytest.mark.asyncio
    async def test_the_stdout_line_carries_the_fingerprint_too(self):
        """So a log pull and /metrics/calls name the same caller the same way."""
        assert _record(fingerprint="batch0000001").to_json()["fingerprint"] == (
            "batch0000001"
        )


# --- 5. what they were searching for -----------------------------------------
#
# The gap this closes is measured in state/gtm/free-mcp-batch-client-searches-
# 2026-09-05.md: on 2026-09-05 the 04:00Z client was hitting the backend at
# ~45 calls/sec and NO log line, here or in the Lambda, carried an origin, a
# destination or a date. Volume and cost were exact; what was being searched
# was unrecoverable for every window, past and future.


class TestToolCallLogLine:
    @pytest.mark.asyncio
    async def test_one_line_per_call_names_the_search(self, caplog):
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            await telemetry.record(
                _record(fingerprint="batch0000001", route=_route())
            )

        lines = _tool_call_lines(caplog)
        assert len(lines) == 1
        assert _fields(lines[0]) == {
            "fp": "batch0000001",
            "tool": "search_oneway_flights",
            "from": "LHR",
            "to": "JFK,BOS",
            "dates": "2026-10-01..2026-10-14",
            "combos": "28",
            "nights": "-",
            "currency": "EUR",
            "pax": "1",
            "stops": "-",
        }

    @pytest.mark.asyncio
    async def test_it_carries_no_ip_no_user_agent_no_session_id(self, caplog):
        """Request parameters only. The fingerprint is the one caller-derived
        value and it is already a one-way digest of exactly those three."""
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            await telemetry.record(
                _record(
                    fingerprint="batch0000001",
                    source_ip="203.0.113.7",
                    client_name="claude-ai",
                    route=_route(),
                )
            )

        line = _tool_call_lines(caplog)[0]
        assert "203.0.113.7" not in line
        assert "claude-ai" not in line

    @pytest.mark.asyncio
    async def test_a_call_with_no_route_writes_no_line(self, caplog):
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            await telemetry.record(_record(fingerprint="batch0000001"))
        assert _tool_call_lines(caplog) == []

    def test_a_free_text_field_cannot_forge_another(self):
        """A hotel destination is text the caller typed. It must not be able to
        add a field to the line or a segment to the histogram key."""
        line = _route(
            origin=None,
            destination="Tokyo Shibuya | fp=deadbeef stops=0",
            combinations=1,
        ).log_line("person000001", "search_hotels")

        fields = _fields(line)
        assert fields["fp"] == "person000001"
        assert fields["stops"] == "-"
        assert " " not in fields["to"]
        assert "|" not in fields["to"]
        assert fields["from"] == "-"

    def test_the_histogram_key_is_fingerprint_plus_route(self):
        assert _route().member("batch0000001") == "batch0000001|LHR|JFK,BOS"
        # No fingerprint (stdio, a test) still keys a route rather than
        # dropping it, so the histogram never silently loses traffic.
        assert _route().member(None) == "-|LHR|JFK,BOS"


class TestRouteHistogram:
    @pytest.mark.asyncio
    async def test_routes_accumulate_per_day_with_their_cost(self):
        store = MemoryCounterStore()
        now = time.time()
        for _ in range(3):
            await store.bump(
                TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 15}, 15, now,
                tool="search_oneway_flights", fingerprint="batch0000001",
                route="batch0000001|LHR|JFK,BOS", combinations=28,
            )
        await store.bump(
            TIER_LLM_HOST, {"tool_calls": 1, "backend_calls": 1}, 1, now,
            tool="search_hotels", fingerprint="person000001",
            route="person000001|-|rome", combinations=1,
        )

        routes = await store.top_routes(now, ROUTE_WINDOW_DAYS, TOP_ROUTES)
        assert routes[0] == {
            "fingerprint": "batch0000001",
            "from": "LHR",
            "to": "JFK,BOS",
            "tool_calls": 3,
            # 28 requested combinations per call is the cost a tool_calls of 3
            # hides -- and it is the number the fan-out is capped against.
            "combos": 84,
            "backend_calls": 45,
        }
        assert routes[1]["to"] == "rome" and routes[1]["from"] is None
        # Ranked by backend calls, which is what a route costs.
        assert [r["backend_calls"] for r in routes] == [45, 1]

    @pytest.mark.asyncio
    async def test_a_days_routes_are_capped(self):
        """A sweep over every origin x destination pair must not write an
        unbounded hash. Routes already being counted keep counting."""
        store = MemoryCounterStore()
        now = time.time()
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 1}, 1, now,
            fingerprint="batch0000001", route="batch0000001|LHR|JFK",
            combinations=1,
        )
        for index in range(ROUTE_KEYS_PER_DAY + 50):
            await store.bump(
                TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 1}, 1, now,
                fingerprint="batch0000001",
                route=f"batch0000001|LHR|X{index:04d}", combinations=1,
            )
        # The early route keeps counting past the cap.
        await store.bump(
            TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 9}, 9, now,
            fingerprint="batch0000001", route="batch0000001|LHR|JFK",
            combinations=9,
        )

        day = store._routes[time.strftime("%Y%m%d", time.gmtime(now))]
        assert len(day) == ROUTE_KEYS_PER_DAY
        routes = await store.top_routes(now, ROUTE_WINDOW_DAYS, TOP_ROUTES)
        assert routes[0]["to"] == "JFK" and routes[0]["backend_calls"] == 10

    @pytest.mark.asyncio
    async def test_an_unrouted_call_still_counts_in_the_totals(self):
        store = MemoryCounterStore()
        now = time.time()
        await store.bump(TIER_UNKNOWN, {"tool_calls": 1, "backend_calls": 7}, 7, now)

        assert await store.backend_calls_in_window(now) == 7
        assert await store.top_routes(now, ROUTE_WINDOW_DAYS, TOP_ROUTES) == []

    @pytest.mark.asyncio
    async def test_metrics_calls_carries_top_routes(self):
        telemetry = Telemetry(store=MemoryCounterStore(), stdout=False)
        await telemetry.record(
            _record(fingerprint="batch0000001", route=_route())
        )

        section = (await telemetry.call_series(24))["top_routes"]
        assert section["window_days"] == ROUTE_WINDOW_DAYS
        assert section["limit"] == TOP_ROUTES
        assert section["routes"] == [
            {
                "fingerprint": "batch0000001",
                "from": "LHR",
                "to": "JFK,BOS",
                "tool_calls": 1,
                "combos": 28,
                "backend_calls": 4,
            }
        ]


class TestRouteLineOnRealToolCalls:
    """Through the actual tools, because the fields are built at the call site
    and a signature that drifts would otherwise log `-` forever."""

    @pytest.fixture
    def stub_backend(self, monkeypatch):
        async def fake_search(self, endpoint, payload, **_kwargs):
            return [{
                "buy_link": "https://book/x",
                "price_as_number": 400,
                "duration_seconds": 30000,
                "airline": "Delta",
            }]

        monkeypatch.setattr(
            lambda_client_module.LambdaClient, "search", fake_search, raising=True
        )

    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "false")
        monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
        monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
        monkeypatch.setenv("HOTELS_AUTH", "secret")
        return build_server(load_settings())

    @pytest.mark.asyncio
    async def test_a_oneway_range_logs_its_route_and_its_cost(
        self, server, stub_backend, caplog
    ):
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            async with Client(server) as client:
                await client.call_tool("search_oneway_flights", {
                    "from_airport": "lhr",
                    "to_airport": ["JFK", "BOS"],
                    "departure_date_from": "2026-10-01",
                    "departure_date_to": "2026-10-14",
                    "currency": "eur",
                    "max_stops": 0,
                    "passengers": [2, 1, 0],
                })

        fields = _fields(_tool_call_lines(caplog)[0])
        assert fields["tool"] == "search_oneway_flights"
        assert fields["from"] == "LHR" and fields["to"] == "JFK,BOS"
        assert fields["dates"] == "2026-10-01..2026-10-14"
        # 14 dates x 2 destinations, which is what the call actually asked for
        # even though the free-tier cap runs only 15 of them.
        assert fields["combos"] == "28"
        assert fields["currency"] == "eur"
        assert fields["pax"] == "2/1/0"
        assert fields["stops"] == "0"

    @pytest.mark.asyncio
    async def test_a_wide_destination_list_collapses_to_a_count(
        self, server, stub_backend, caplog
    ):
        """A thirty-airport sweep is one fact -- that it was a sweep. Naming
        every airport would make the histogram key unbounded."""
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            async with Client(server) as client:
                await client.call_tool("search_oneway_flights", {
                    "from_airport": "TLV",
                    "to_airport": [f"X{i:02d}" for i in range(30)],
                    "departure_date": "2026-10-14",
                })

        assert _fields(_tool_call_lines(caplog)[0])["to"] == "X00,X01,X02+27"

    @pytest.mark.asyncio
    async def test_a_roundtrip_logs_nights_and_both_legs_stops(
        self, server, stub_backend, caplog
    ):
        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            async with Client(server) as client:
                await client.call_tool("search_roundtrip_flights", {
                    "from_airport": "TLV",
                    "to_airport": "FCO",
                    "departure_date": "2026-05-01",
                    "nights": [5, 6, 7],
                    "max_departure_stops": 0,
                    "max_return_stops": 1,
                })

        fields = _fields(_tool_call_lines(caplog)[0])
        assert fields["nights"] == "5,6,7"
        assert fields["stops"] == "0/1"
        assert fields["combos"] == "3"

    @pytest.mark.asyncio
    async def test_a_hotel_search_logs_the_destination_and_the_stay(
        self, server, monkeypatch, caplog
    ):
        async def fake_search(self, endpoint, payload):
            return [{"name": "Hotel Leone", "price": 273}]

        monkeypatch.setattr(
            HotelsLambdaClient, "search", fake_search, raising=True
        )

        with caplog.at_level(logging.INFO, logger="src.telemetry"):
            async with Client(server) as client:
                await client.call_tool("search_hotels", {
                    "destination": "Tokyo Shibuya",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-05",
                    "adults": 2,
                })

        fields = _fields(_tool_call_lines(caplog)[0])
        assert fields["tool"] == "search_hotels"
        assert fields["from"] == "-"
        assert fields["to"] == "Tokyo_Shibuya"
        assert fields["dates"] == "2026-10-01..2026-10-05"
        assert fields["nights"] == "4"
        assert fields["pax"] == "2/0"
        assert fields["combos"] == "1"


# --- and none of it is a gate ------------------------------------------------


class TestNothingIsEnforced:
    @pytest.mark.asyncio
    async def test_a_store_that_cannot_answer_does_not_fail_the_series(self):
        """Counters are not the product. A breakdown that cannot be read comes
        back empty; it never raises into a caller's flight search."""

        class Broken(MemoryCounterStore):
            async def tool_series(self, now, hours):
                raise RuntimeError("store is down")

            async def tool_series_by_day(self, now, days):
                raise RuntimeError("store is down")

            async def top_clients(self, now, hours, limit):
                raise RuntimeError("store is down")

            async def top_routes(self, now, days, limit):
                raise RuntimeError("store is down")

        telemetry = Telemetry(store=Broken(), stdout=False)
        await telemetry.record(_record(fingerprint="batch0000001", route=_route()))

        series = await telemetry.call_series(24)
        assert series["by_tool"] == {}
        assert series["top_clients"]["clients"] == []
        assert series["top_routes"]["routes"] == []
        assert series["totals"]["backend_calls"] == 4

    def test_a_fingerprint_gates_nothing(self):
        """Grep-able intent: no fingerprint is read anywhere a decision is made.

        MCP has no client attestation (spec 2026-07-28), and neither an IP nor a
        user agent survives a proxy intact -- rule 8 in the agent CLAUDE.md
        exists because a transport-path classification was nearly used as a
        capability one. This is a reporting id and must stay one.
        """
        import inspect

        from src.policy import ClientClassifier, decide

        assert "fingerprint" not in inspect.getsource(decide)
        assert "fingerprint" not in inspect.getsource(ClientClassifier)

    def test_a_route_gates_nothing_either(self):
        """Same rule for the search parameters: recorded, never read by a
        decision. A cap that varied by destination would be a product change
        smuggled in as telemetry."""
        import inspect

        from src.policy import ClientClassifier, decide

        for source in (inspect.getsource(decide), inspect.getsource(ClientClassifier)):
            assert "route" not in source
            assert "destination" not in source
