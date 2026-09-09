"""Per-client fair use: the caps, the refusal, and the upgrade path.

On 2026-09-05 one client had made 3,270 of this server's 4,078 backend Lambda
calls in 30 hours (``state/gtm/free-mcp-batch-client-2026-09-05.md``) -- a
scheduled job, ~$90 a month of proxy bandwidth, one free user who had never
been shown that a paid server exists. The daily spend budget could not help:
it is a total, so the heaviest caller spends it and everyone else gets a
degraded server.

So there is now a per-client cap on BACKEND calls -- 150 a UTC day, 2,000 a
calendar month -- and this file pins the four things that make it worth
shipping rather than one more counter nobody reads:

1. it counts what costs money (backend calls), not what a caller sees (tool
   calls), so one call over a two-week range moves it by fourteen;
2. it is keyed WITHOUT the session id, because the client it exists for opens
   a new session per request and a session-keyed counter never reaches two;
3. a refused call comes back in the tool's normal result shape with
   ``retry: false`` and an actionable ``upgrade`` object, not as an exception
   a model will simply retry;
4. the upgrade instructions are in the server instructions and in every tool
   description, so a model knows about them before it is ever refused.

    python -m pytest mcp_server/tests -q
"""

import logging
import time

import pytest
from fastmcp import Client

from src import fair_use
from src import lambda_client as lambda_client_module
from src import server as server_module
from src.fair_use import (
    DOCS_URL,
    FLIGHTS_LISTING_URL,
    HOTELS_LISTING_URL,
    PAID_FLIGHTS_URL,
    PAID_HOTELS_URL,
    SIGNIN_FLIGHTS_URL,
    SIGNIN_HOTELS_URL,
    WARN_AT,
    FairUseState,
    client_key,
    fair_use_note,
    log_line,
    rate_limited_result,
    upgrade_block,
    upgrade_tail,
)
from src.hotels_lambda_client import HotelsLambdaClient
from src.policy import client_fingerprint
from src.server import build_instructions, build_server
from src.settings import load_settings
from src.stores import (
    FAIR_USE_DAY_TTL_SECONDS,
    FAIR_USE_MONTH_TTL_SECONDS,
    MemoryCounterStore,
    fair_use_blocked_key,
    fair_use_keys,
)
from src.telemetry import CallRecord, Telemetry

DAY = 86400

HEADERS = {
    "x-forwarded-for": "203.0.113.9, 70.0.0.1",
    "user-agent": "python-httpx/0.27.0",
    "mcp-session-id": "session-one",
}


def _state(**overrides):
    base = dict(
        key="abc123abc123",
        used_today=0,
        used_month=0,
        day_cap=150,
        month_cap=2000,
    )
    base.update(overrides)
    return FairUseState(**base)


# ── the key ──────────────────────────────────────────────────────────────


class TestTheClientKey:
    def test_the_session_id_is_not_in_it(self):
        """The whole reason for a second digest.

        The client this cap exists for opened 75 sessions in two minutes. A
        counter keyed on the reporting fingerprint, which includes
        mcp-session-id, would have reset on every single request and the cap
        would never have fired once.
        """
        one = client_key(HEADERS)
        two = client_key({**HEADERS, "mcp-session-id": "session-two"})
        assert one == two
        assert client_fingerprint(HEADERS) != client_fingerprint(
            {**HEADERS, "mcp-session-id": "session-two"}
        )

    def test_it_is_not_the_reporting_fingerprint(self):
        # Different values for the same caller, on purpose: /metrics keeps
        # reading the fingerprint it always read.
        assert client_key(HEADERS) != client_fingerprint(HEADERS)

    def test_only_the_original_client_ip_counts(self):
        """Proxy hops move; the leftmost entry is the caller."""
        assert client_key(HEADERS) == client_key(
            {**HEADERS, "x-forwarded-for": "203.0.113.9, 198.51.100.4"}
        )

    def test_a_different_caller_is_a_different_key(self):
        assert client_key(HEADERS) != client_key(
            {**HEADERS, "x-forwarded-for": "198.51.100.7"}
        )

    def test_no_headers_is_no_key(self):
        # stdio, a test, a direct local connection. A caller we cannot
        # identify is not counted and, critically, not refused.
        assert client_key({}) is None
        assert client_key({"mcp-session-id": "only-a-session"}) is None

    def test_it_is_one_way_and_short(self):
        key = client_key(HEADERS)
        assert len(key) == 12
        assert "203.0.113.9" not in key and "httpx" not in key


# ── the arithmetic ───────────────────────────────────────────────────────


class TestCapArithmetic:
    def test_remaining_is_the_tighter_of_the_two(self):
        assert _state(used_today=100, used_month=1000).remaining == 50
        assert _state(used_today=10, used_month=1995).remaining == 5

    def test_the_day_cap_blocks(self):
        state = _state(used_today=150, used_month=200)
        assert state.blocked is True
        assert state.limit_reached == "day"

    def test_the_month_cap_blocks_even_on_a_quiet_day(self):
        """The cap that actually protects the paid plan.

        150/day for a month is 4,500, and RapidAPI PRO is 2,500 requests for
        $10. Free must not beat the cheapest paid plan, which is the month
        cap's entire job.
        """
        state = _state(used_today=3, used_month=2000)
        assert state.blocked is True
        assert state.limit_reached == "month"

    def test_month_is_reported_before_day_when_both_are_spent(self):
        # The expensive news first: a day cap rolls over tonight, a month cap
        # does not.
        assert _state(used_today=150, used_month=2000).limit_reached == "month"

    def test_under_both_caps_nothing_is_blocked(self):
        state = _state(used_today=149, used_month=1999)
        assert state.blocked is False
        assert state.remaining == 1

    def test_a_cap_of_zero_is_off_not_exhausted(self):
        # Otherwise unsetting one cap would silently refuse everybody.
        assert _state(day_cap=0, used_today=9999).blocked is False
        assert _state(day_cap=0, used_month=10, month_cap=2000).remaining == 1990
        assert _state(day_cap=0, month_cap=0).blocked is False

    def test_after_counts_what_the_call_spent(self):
        spent = _state(used_today=10, used_month=40).after(12)
        assert (spent.used_today, spent.used_month) == (22, 52)

    def test_the_warning_starts_at_eighty_percent_of_the_day_cap(self):
        assert WARN_AT == 0.8
        assert _state(used_today=119).warning is False
        assert _state(used_today=120).warning is True

    def test_the_warning_also_starts_at_eighty_percent_of_the_month(self):
        assert _state(used_today=1, used_month=1599).warning is False
        assert _state(used_today=1, used_month=1600).warning is True


# ── the counters ─────────────────────────────────────────────────────────


def _telemetry(store=None):
    return Telemetry(
        store=store or MemoryCounterStore(),
        stdout=False,
        fair_use_enabled=True,
        fair_use_day_cap=150,
        fair_use_month_cap=2000,
    )


def _call(**overrides):
    base = dict(
        timestamp=time.time(),
        tool="search_oneway_flights",
        tier="unknown",
        client_name="claude-ai",
        source_ip="203.0.113.9",
        widget_capable=True,
        requested_combinations=12,
        backend_calls=12,
        backend_failures=0,
        results_returned=10,
        duration_ms=100,
        truncated=False,
        allowed=True,
        decision_reason="monitor",
        ad_eligible=True,
        fair_use_key="abc123abc123",
    )
    base.update(overrides)
    return CallRecord(**base)


class TestCountersThroughThePipeline:
    @pytest.mark.asyncio
    async def test_it_counts_backend_calls_not_tool_calls(self):
        """The distinction the whole design rests on.

        Two tool calls that expanded to 12 backend searches each are 24
        against the allowance, not 2. Counting tool calls would leave the
        exact fan-out that caused this uncapped.
        """
        telemetry = _telemetry()
        await telemetry.record(_call(backend_calls=12))
        await telemetry.record(_call(backend_calls=12))
        assert await telemetry.fair_use_usage("abc123abc123") == (24, 24)

    @pytest.mark.asyncio
    async def test_each_client_has_its_own_allowance(self):
        telemetry = _telemetry()
        await telemetry.record(_call(fair_use_key="aaaaaaaaaaaa", backend_calls=30))
        await telemetry.record(_call(fair_use_key="bbbbbbbbbbbb", backend_calls=4))
        assert await telemetry.fair_use_usage("aaaaaaaaaaaa") == (30, 30)
        assert await telemetry.fair_use_usage("bbbbbbbbbbbb") == (4, 4)

    @pytest.mark.asyncio
    async def test_an_unidentified_caller_is_not_counted(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        await telemetry.record(_call(fair_use_key=None, backend_calls=12))
        # Nothing was written at all, so nobody's allowance moved.
        assert store._fair_use == {}

    @pytest.mark.asyncio
    async def test_the_day_rolls_over_and_the_month_does_not(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        now = time.time()
        await telemetry.record(_call(timestamp=now - DAY, backend_calls=140))
        await telemetry.record(_call(timestamp=now, backend_calls=10))
        used_today, used_month = await store.fair_use_usage("abc123abc123", now)
        assert used_today == 10
        assert used_month == 150

    @pytest.mark.asyncio
    async def test_the_month_rolls_over_too(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        now = time.time()
        # 40 days back is always a different calendar month, whatever today is.
        await telemetry.record(_call(timestamp=now - 40 * DAY, backend_calls=1900))
        assert await store.fair_use_usage("abc123abc123", now) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_refusal_spends_nothing_and_is_counted_as_a_refusal(self):
        store = MemoryCounterStore()
        telemetry = _telemetry(store)
        await telemetry.record(
            _call(backend_calls=0, fair_use_blocked=True, error="fair_use_blocked")
        )
        assert await telemetry.fair_use_usage("abc123abc123") == (0, 0)
        assert await telemetry.fair_use_blocked_today() == 1

    @pytest.mark.asyncio
    async def test_metrics_calls_reports_the_refusals(self):
        telemetry = _telemetry()
        await telemetry.record(_call(backend_calls=0, fair_use_blocked=True))
        await telemetry.record(_call(backend_calls=0, fair_use_blocked=True))
        series = await telemetry.call_series(24)
        assert series["fair_use_blocked_today"] == 2
        assert series["fair_use"]["blocked_today"] == 2
        assert series["fair_use"]["day_cap"] == 150
        assert series["fair_use"]["month_cap"] == 2000
        assert series["fair_use"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_a_store_outage_fails_open(self):
        """Nobody is refused on a number we could not read.

        The alternative refuses every caller on an infrastructure blip, and
        the rolling daily budget is still underneath as the real ceiling.
        """

        class Broken(MemoryCounterStore):
            async def fair_use_usage(self, client_key, now):
                raise RuntimeError("upstash is down")

        assert await _telemetry(Broken()).fair_use_usage("abc") == (0, 0)


class TestTheKeyShape:
    def test_the_day_and_month_keys_carry_their_windows(self):
        ts = time.time()
        day_key, month_key = fair_use_keys("abc123abc123", ts)
        assert day_key == (
            f"mcpads:fu:d:{time.strftime('%Y%m%d', time.gmtime(ts))}:abc123abc123"
        )
        assert month_key == (
            f"mcpads:fu:m:{time.strftime('%Y%m', time.gmtime(ts))}:abc123abc123"
        )

    def test_the_refusal_counter_is_per_day_and_not_per_client(self):
        # It answers "is the cap firing at all today", which is a server
        # question, not a caller one.
        ts = time.time()
        assert fair_use_blocked_key(ts) == (
            f"mcpads:fu:blocked:{time.strftime('%Y%m%d', time.gmtime(ts))}"
        )

    def test_the_ttls_outlive_their_windows_and_no_more(self):
        assert FAIR_USE_DAY_TTL_SECONDS == 2 * DAY
        assert FAIR_USE_MONTH_TTL_SECONDS == 35 * DAY


# ── the refusal ──────────────────────────────────────────────────────────


class TestTheBlockShape:
    @pytest.fixture
    def result(self):
        return rate_limited_result(_state(used_today=150, used_month=900))

    def test_it_is_a_result_not_an_exception(self, result):
        # A model handed an exception retries it. There is nothing to retry.
        assert result["results"] == []
        assert result["result_count"] == 0
        assert result["search_status"] == "rate_limited"
        assert result["retry"] is False

    def test_the_message_says_no_search_ran(self, result):
        # The text block is what most models actually see; "no results" alone
        # would be repeated to a user as "there are no flights".
        assert "not run" in result["message"]
        assert "Retrying will not help" in result["message"]

    def test_it_carries_the_counters_it_refused_on(self, result):
        assert result["fair_use"] == {
            "used_today": 150,
            "day_cap": 150,
            "used_month": 900,
            "month_cap": 2000,
            # Which tier the caller is on. Added 2026-09-09: the same object
            # now describes two allowances, and a reader cannot tell 150 a
            # day for one account from 150 a day for everyone on one IP
            # without it.
            "signed_in": False,
            "human": result["fair_use"]["human"],
            "note": result["fair_use"]["note"],
        }

    def test_the_human_string_is_a_plain_one_liner(self, result):
        human = result["fair_use"]["human"]
        assert human == (
            "Free tier: 150 searches a day, 2,000 a month without signing in; "
            "150 used today."
        )

    def test_the_upgrade_object_is_actionable(self, result):
        upgrade = result["upgrade"]
        assert upgrade["why"] == (
            "Free fair-use reached: 150 searches a day and 2,000 a month "
            "for callers who are not signed in. One search counts once per "
            "date and destination combination, so a wide call spends "
            "several. The daily count resets at 00:00 UTC."
        )
        assert upgrade["paid_server"] == PAID_FLIGHTS_URL
        assert upgrade["hotels_server"] == PAID_HOTELS_URL
        assert upgrade["docs"] == DOCS_URL
        assert "utm_source=free-mcp" in upgrade["docs"]

    def test_the_how_is_three_ordered_steps_a_model_can_follow(self, result):
        how = result["upgrade"]["how"]
        assert len(how) == 3
        assert [step[:2] for step in how] == ["1.", "2.", "3."]
        assert FLIGHTS_LISTING_URL in how[0] and HOTELS_LISTING_URL in how[0]
        # Plan names and prices, checked against the live billingPlans payload
        # in state/gtm/PASTE-long-descriptions-2026-09-04.md. Rule 1: no
        # number in outward copy that is not traceable to something live.
        assert "BASIC is free and includes 10 requests a month" in how[0]
        assert "$10 a month" in how[0]
        assert "x-rapidapi-key" in how[1] and "rapidapi_key=" in how[1]
        assert PAID_FLIGHTS_URL in how[1] and PAID_HOTELS_URL in how[1]
        # 2026-09-08: the paid server signs people in, so step 2 leads with
        # the sign-in and keeps the header as the second way. A header is
        # the step most people get wrong and some clients cannot set one.
        assert SIGNIN_FLIGHTS_URL in how[1] and SIGNIN_HOTELS_URL in how[1]
        assert how[1].index("/mcp/oauth") < how[1].index("x-rapidapi-key")

    def test_the_upgrade_block_carries_the_sign_in_urls(self, result):
        upgrade = result["upgrade"]
        assert upgrade["sign_in_flights"] == SIGNIN_FLIGHTS_URL
        assert upgrade["sign_in_hotels"] == SIGNIN_HOTELS_URL

    def test_the_why_follows_the_configured_caps(self):
        # A hardcoded sentence would keep saying 150 after someone changed
        # the env var, which is worse than saying nothing.
        assert "300 searches a day and 5,000 a month" in (
            upgrade_block(300, 5000)["why"]
        )
        # And the tier is named, both ways round.
        assert "not signed in" in upgrade_block(300, 5000)["why"]
        assert "for this account" in (
            upgrade_block(300, 5000, signed_in=True)["why"]
        )

    def test_the_note_does_not_claim_a_search_that_ran_did_not(self):
        """The same object rides on the call that spends the last of the
        allowance, and that call did run."""
        note = fair_use_note(_state(used_today=150).after(0))["note"]
        assert "not run" not in note
        assert "rate_limited" in note


# ── the warning object (not blocked yet) ────────────────────────────────


class TestTheWarningObject:
    """The 80%-of-cap object attached to a normal result, and the `upgrade`
    that now rides beside it (server.py, not this module -- these pin the
    two pieces `fair_use_note` itself owns: `note` and `human`)."""

    def test_the_note_names_the_day_percent_and_points_at_upgrade(self):
        note = fair_use_note(_state(used_today=120))["note"]
        assert note == (
            "Free fair-use is at 80% for today. The `upgrade` steps below "
            "keep this working without a cap on our side, and step 1 is a "
            "free RapidAPI BASIC key."
        )

    def test_the_note_names_the_month_when_the_month_is_tighter(self):
        # A quiet day near the end of a heavy month: today's percentage is
        # nothing to warn about, but the month cap is the one about to stop
        # this client, and the note has to say so, not "today".
        note = fair_use_note(_state(used_today=1, used_month=1800))["note"]
        assert "for this month" in note
        assert "for today" not in note

    def test_the_human_string_matches_the_configured_caps_and_usage(self):
        human = fair_use_note(_state(used_today=120, used_month=1400))["human"]
        assert human == (
            "Free tier: 150 searches a day, 2,000 a month without signing in; "
            "120 used today."
        )

    def test_the_log_line_names_the_client_and_nothing_else(self):
        line = log_line(_state(used_today=150, key="abc123abc123"), "block")
        assert line.startswith("[fair_use] action=block client=abc123abc123")
        assert "used_today=150" in line and "limit=day" in line
        for leaked in ("203.0.113.9", "httpx", "session"):
            assert leaked not in line


# ── through the tools ────────────────────────────────────────────────────


@pytest.fixture
def backend_calls(monkeypatch):
    """Counts what actually reached the Lambda client."""
    calls: list[str] = []

    async def fake_search(self, endpoint, payload, **_kwargs):
        calls.append(endpoint)
        return [{"buy_link": "https://book/x", "price_as_number": 400}]

    async def fake_hotels_search(self, endpoint, payload, **_kwargs):
        calls.append(endpoint)
        return [{"link": "https://book/h", "price": "100"}]

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )
    monkeypatch.setattr(
        HotelsLambdaClient, "search", fake_hotels_search, raising=True
    )
    return calls


@pytest.fixture
def caller(monkeypatch):
    """Give the in-memory client the headers a real HTTP caller would have.

    A direct caller: a peer address outside every published gateway range and
    no configuration blob on the URL, so it takes the `direct` branch and the
    ordinary caps. `tests/test_fair_use_gateway.py` covers the others.
    """
    monkeypatch.setattr(
        server_module,
        "_request_context",
        lambda: (dict(HEADERS), "10.0.0.1", {}),
    )
    return client_key(HEADERS)


def _server(monkeypatch, **env):
    monkeypatch.setenv("ADS_ENABLED", "false")
    # The tests in this file describe an ANONYMOUS direct caller against the
    # ordinary 150/2,000 allowance, which is what every free caller had
    # before 2026-09-09. The taster cap (10 a day, the shipped default) moved
    # that number for anonymous callers, so these set it back explicitly and
    # keep testing the thing they were written to test: the cap machinery,
    # the warning, the overshoot clamp, the month rollover.
    # `TestTheAnonymousTasterCap` covers the new default, and
    # `TestSignedInCallers` covers a caller with an account.
    monkeypatch.setenv("FREE_ANON_DAILY_CAP", env.pop("FREE_ANON_DAILY_CAP", "150"))
    monkeypatch.setenv(
        "FREE_ANON_MONTHLY_CAP", env.pop("FREE_ANON_MONTHLY_CAP", "2000")
    )
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    monkeypatch.setenv("LOG_PATH", "")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return build_server(load_settings())


async def _spend(server, key, backend_calls, ts=None):
    """Put a client at a given point in its allowance."""
    await server.telemetry.record(
        _call(
            fair_use_key=key,
            backend_calls=backend_calls,
            timestamp=ts if ts is not None else time.time(),
        )
    )


class TestThroughTheTools:
    @pytest.mark.asyncio
    async def test_a_caller_under_the_cap_searches_normally(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        assert len(backend_calls) == 1
        assert result.structured_content["result_count"] == 1
        # No warning while there is nothing to warn about.
        assert "fair_use" not in result.structured_content

    @pytest.mark.asyncio
    async def test_the_warning_arrives_before_the_wall(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch)
        await _spend(server, caller, 119)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        # 119 + this call's 1 backend search = 120, which is 80% of 150.
        assert result.structured_content["fair_use"]["used_today"] == 120
        assert result.structured_content["fair_use"]["day_cap"] == 150
        assert "80%" in result.structured_content["fair_use"]["note"]
        assert "120 used today" in result.structured_content["fair_use"]["human"]
        # The warning's note points at `upgrade` instead of repeating it, so
        # the object has to actually be there for that pointer to be true.
        assert result.structured_content["upgrade"]["paid_server"] == PAID_FLIGHTS_URL
        assert len(result.structured_content["upgrade"]["how"]) == 3
        assert result.structured_content["result_count"] == 1

    @pytest.mark.asyncio
    async def test_over_the_cap_nothing_reaches_the_backend(
        self, monkeypatch, backend_calls, caller, caplog
    ):
        server = _server(monkeypatch)
        await _spend(server, caller, 150)
        with caplog.at_level(logging.INFO, logger="src.server"):
            async with Client(server) as client:
                result = await client.call_tool("search_oneway_flights", {
                    "from_airport": "TLV",
                    "to_airport": ["FCO", "CDG"],
                    "departure_date_from": "2026-10-01",
                    "departure_date_to": "2026-10-14",
                })
        assert backend_calls == []
        assert result.structured_content["search_status"] == "rate_limited"
        assert result.structured_content["retry"] is False
        assert result.structured_content["upgrade"]["paid_server"] == PAID_FLIGHTS_URL
        assert any(
            "[fair_use] action=block" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_the_last_call_of_the_day_cannot_overshoot_the_cap(
        self, monkeypatch, backend_calls, caller
    ):
        """A cap that the final fan-out can jump over is not a cap.

        Without the clamp a client at 145 gets a full 15-way fan-out and ends
        the day at 160 -- and a month cap becomes a suggestion, thirty times
        over.
        """
        server = _server(monkeypatch)
        await _spend(server, caller, 145)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date_from": "2026-10-01",
                "departure_date_to": "2026-10-14",
            })
        assert len(backend_calls) == 5
        assert result.structured_content["search_coverage"]["truncated"] is True
        assert result.structured_content["fair_use"]["used_today"] == 150

    @pytest.mark.asyncio
    async def test_the_month_cap_refuses_on_a_quiet_day(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch)
        # Spread across earlier days this month, so today's counter is 0.
        await _spend(server, caller, 1000, ts=time.time() - 2 * DAY)
        await _spend(server, caller, 1000, ts=time.time() - DAY)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        assert backend_calls == []
        assert result.structured_content["search_status"] == "rate_limited"
        assert result.structured_content["fair_use"]["used_today"] == 0
        assert result.structured_content["fair_use"]["used_month"] == 2000

    @pytest.mark.asyncio
    async def test_a_hotel_search_is_refused_in_the_same_shape(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch)
        await _spend(server, caller, 150)
        async with Client(server) as client:
            result = await client.call_tool("search_hotels", {
                "destination": "Rome",
                "checkin_date": "2026-11-18",
                "checkout_date": "2026-11-21",
            })
        assert backend_calls == []
        assert result.structured_content["search_status"] == "rate_limited"
        assert result.structured_content["upgrade"]["hotels_server"] == PAID_HOTELS_URL

    @pytest.mark.asyncio
    async def test_a_hotel_search_spends_exactly_one(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch)
        await _spend(server, caller, 119)
        async with Client(server) as client:
            result = await client.call_tool("search_hotels", {
                "destination": "Rome",
                "checkin_date": "2026-11-18",
                "checkout_date": "2026-11-21",
            })
        assert len(backend_calls) == 1
        assert result.structured_content["fair_use"]["used_today"] == 120
        # The fair-use `upgrade` replaces the generic ad-tier one that a
        # normal hotel result always carries -- the specific path is more
        # useful than the general one once the cap is the live issue.
        upgrade = result.structured_content["upgrade"]
        assert upgrade["hotels_server"] == PAID_HOTELS_URL
        assert "tier" not in upgrade

    @pytest.mark.asyncio
    async def test_a_caller_with_no_headers_is_never_refused(
        self, monkeypatch, backend_calls
    ):
        """No `caller` fixture here: stdio and in-memory clients send nothing.

        Refusing on "no headers at all" would refuse every stdio user on the
        strength of nothing.
        """
        server = _server(monkeypatch)
        await _spend(server, None, 5000)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        assert len(backend_calls) == 1
        assert "search_status" not in result.structured_content or (
            result.structured_content["search_status"] != "rate_limited"
        )

    @pytest.mark.asyncio
    async def test_the_env_switch_turns_the_whole_thing_off(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(monkeypatch, FAIR_USE_ENABLED="0")
        await _spend(server, caller, 5000)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        assert len(backend_calls) == 1
        assert "fair_use" not in result.structured_content
        # And the prose goes with it, so an off switch does not leave the
        # model quoting a limit that is not enforced.
        assert "FAIR USE" not in (server.instructions or "")

    @pytest.mark.asyncio
    async def test_the_caps_come_from_the_environment(
        self, monkeypatch, backend_calls, caller
    ):
        server = _server(
            monkeypatch,
            FAIR_USE_DAY_CAP="10",
            FAIR_USE_MONTH_CAP="40",
            FREE_ANON_DAILY_CAP="10",
            FREE_ANON_MONTHLY_CAP="40",
        )
        await _spend(server, caller, 10)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV",
                "to_airport": "FCO",
                "departure_date": "2026-10-14",
            })
        assert backend_calls == []
        assert result.structured_content["fair_use"]["day_cap"] == 10
        assert (
            "10 searches a day and 40 a month"
            in result.structured_content["upgrade"]["why"]
        )


# ── where the model reads it ─────────────────────────────────────────────


class TestTheUpgradeTextIsWhereAModelWillSeeIt:
    """Being refused is half the job. A model that only learns the paid
    server exists at the moment it is refused has already failed the user's
    request."""

    @pytest.fixture
    def server(self, monkeypatch, backend_calls):
        return _server(monkeypatch)

    def test_the_server_instructions_carry_it(self, server):
        text = server.instructions
        assert "FAIR USE" in text
        assert "150" in text and "2,000" in text
        assert PAID_FLIGHTS_URL in text and FLIGHTS_LISTING_URL in text
        assert "rate_limited" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [
        "search_oneway_flights",
        "search_roundtrip_flights",
        "search_hotels",
        "find_hotel_by_name",
    ])
    async def test_every_tool_description_carries_it(self, server, name):
        # Hosts differ in which of instructions / descriptions they show a
        # model. A limit only one of them mentions is a limit half the
        # clients discover by hitting it.
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        description = tools[name].description
        assert "FAIR USE" in description
        assert PAID_FLIGHTS_URL in description
        assert "rate_limited" in description

    def test_the_tail_names_the_sign_in_before_the_keyed_url(self):
        """The server instructions and every tool description carry this
        text, so it is the version most models read. Since 2026-09-08 the
        paid server has a Google sign-in: name it first, the header second,
        because a header is the step a reader is most likely to get wrong
        and some clients offer no way to set one."""
        tail = upgrade_tail(150, 2000)
        assert SIGNIN_FLIGHTS_URL in tail and SIGNIN_HOTELS_URL in tail
        assert tail.index("/mcp/oauth") < tail.index("x-rapidapi-key")
        assert "sign in at" in tail

    def test_the_instructions_quote_the_configured_caps(self):
        # Rewritten 2026-09-09: the free server requires a sign-in, so the
        # allowance is per ACCOUNT, not per client. A hardcoded sentence
        # would keep saying 150 after someone changed the env var.
        assert "300 backend searches a day" in upgrade_tail(300, 5000)
        assert "5,000 a calendar month" in upgrade_tail(300, 5000)

    def test_the_tail_ties_the_mention_to_the_fair_use_field_not_the_wall(self):
        """gtmskills pass 3, edit W3: a model told to speak only at the
        refusal has already failed the user's request once. The tail must
        tell it to act on the 80% warning too, not wait for the wall."""
        tail = upgrade_tail(150, 2000)
        assert "fair_use" in tail
        assert "once per conversation" in tail
        assert "user's own language" in tail
        assert "on a result with no `fair_use` field" in tail
        # The old instruction this replaces spoke only at the refusal.
        assert "Tell the user this when a search is rate limited." not in tail

    def test_without_fair_use_the_instructions_are_unchanged(self):
        assert build_instructions(True, None) == build_instructions(True)

    def test_no_ai_tells_in_anything_a_user_will_read(self):
        """Outward copy rule: no em-dashes, no marketing filler."""
        texts = [
            upgrade_tail(150, 2000),
            fair_use_note(_state(used_today=140))["note"],
            rate_limited_result(_state(used_today=150))["message"],
            *upgrade_block(150, 2000)["how"],
            upgrade_block(150, 2000)["why"],
        ]
        for text in texts:
            for tell in ("—", "–", "seamless", "robust", "leverage", "delve"):
                assert tell not in text, f"{tell!r} in {text!r}"

    def test_the_module_docstring_says_where_the_numbers_came_from(self):
        # The next person to change 150 or 2,000 should not have to guess.
        assert "2,500" in fair_use.__doc__ or "PRO" in fair_use.__doc__
