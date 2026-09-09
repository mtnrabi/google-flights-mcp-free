"""Fair use behind a gateway: who shares a counter, and who must not.

The 2026-09-05 cap keys on the forwarded IP plus the user agent. That is
right for the caller it was written for -- a scheduled script with its own
address -- and wrong in the opposite direction for everyone reached through a
remote host. A gateway terminates the end user's connection and opens its own
to us, so every user behind it arrives with the same forwarded IP and the same
user agent: one key, one 150-a-day allowance, shared between strangers.

The evidence, read 2026-09-05:

* Anthropic, support.anthropic.com/en/articles/11175166: "Claude connects to
  your remote MCP server from Anthropic's cloud infrastructure, rather than
  from your local device. This is true across every Claude client." The
  address is the published 160.79.104.0/21 -- every Claude user on earth.
* OpenAI, developers.openai.com/api/docs/guides/ip-addresses: "An IP allowlist
  identifies traffic from an OpenAI-operated network, not a specific user or
  workspace." 262 prefixes, refreshed from the feed policy.py already reads.
* Smithery, smithery.ai/docs/build/session-config: "Smithery Gateway passes
  through all query parameters and headers to your upstream server" -- the
  user's own saved configuration, per user, on every request. That blob is the
  only thing in a proxied request that separates two people.
* MCP spec, Streamable HTTP session management: the SERVER assigns
  `MCP-Session-Id`. Behind a proxy the server's client is the gateway, so the
  id belongs to the gateway's connection, not to a person.

So this file pins four things:

1. the abuser -- a direct script, no config, stateless sessions -- still lands
   on `direct` and is still capped at 150, exactly as before;
2. a caller from a published LLM-host range is NOT held to 150, and its
   counter is split by session where a session exists;
3. a per-user configuration value beats everything, because it is the only
   signal that survives a proxy;
4. nobody can talk their way into the higher caps by typing a header.

    python -m pytest mcp_server/tests/test_fair_use_gateway.py -q
"""

import ipaddress
import logging
import time

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src import server as server_module
from src.fair_use import (
    KIND_CONFIG,
    KIND_DIRECT,
    KIND_GATEWAY_POOLED,
    KIND_GATEWAY_SESSION,
    caps_for,
    client_key,
    config_blob,
    gateway_egress,
    identify,
    log_line,
    trusted_addresses,
)
from src.hotels_lambda_client import HotelsLambdaClient
from src.policy import ClientClassifier
from src.server import build_server
from src.settings import load_settings
from src.stores import MemoryCounterStore, fair_use_kind_key
from src.telemetry import CallRecord, Telemetry

# An address inside Anthropic's published outbound range. Not a real host --
# what matters is only that it is inside the /21.
ANTHROPIC_IP = "160.79.104.7"
ANTHROPIC_NETS = [ipaddress.ip_network("160.79.104.0/21")]

# The batch client's shape: its own address, a script user agent, and -- the
# detail that made a session-keyed counter useless -- no session id at all,
# because it opens a fresh stateless request every time.
ABUSER = {
    "x-forwarded-for": "203.0.113.9",
    "user-agent": "python-httpx/0.27.0",
}


def _gateway_headers(session: str | None = "gw-session-1") -> dict[str, str]:
    """What a request proxied by an LLM host looks like at our edge.

    The forwarded address is the gateway's, because it is the gateway that
    connected to us; our own edge appends nothing else in this fixture, so the
    leftmost and rightmost entries are the same value.
    """
    headers = {
        "x-forwarded-for": ANTHROPIC_IP,
        "user-agent": "claude-user",
    }
    if session:
        headers["mcp-session-id"] = session
    return headers


# ── which branch a request takes ─────────────────────────────────────────


class TestTheBranches:
    def test_a_direct_script_is_unchanged(self):
        """Branch (c). The caller the cap was written for, still keyed as it
        was, still at the ordinary caps -- and identical to what the old
        `client_key` produced, so nobody's allowance is quietly reset by this
        change landing."""
        identity = identify(ABUSER, {}, gateway_networks=ANTHROPIC_NETS)
        assert identity.kind == KIND_DIRECT
        assert identity.key == client_key(ABUSER)
        assert identity.pooled is False
        assert caps_for(
            identity.kind,
            day_cap=150,
            month_cap=2000,
            gateway_day_cap=1500,
            gateway_month_cap=10000,
        ) == (150, 2000)

    def test_a_gateway_with_a_session_is_split_by_session(self):
        """Branch (b). Two chats through the same host are two counters."""
        one = identify(
            _gateway_headers("session-a"), {}, gateway_networks=ANTHROPIC_NETS
        )
        two = identify(
            _gateway_headers("session-b"), {}, gateway_networks=ANTHROPIC_NETS
        )
        assert one.kind == two.kind == KIND_GATEWAY_SESSION
        assert one.key != two.key
        assert one.pooled is True

    def test_a_gateway_without_a_session_says_so(self):
        """Branch (b) with nothing to split on. One shared counter, named as
        one, rather than a per-user counter we cannot actually produce."""
        identity = identify(
            _gateway_headers(None), {}, gateway_networks=ANTHROPIC_NETS
        )
        assert identity.kind == KIND_GATEWAY_POOLED
        assert identity.pooled is True

    def test_both_gateway_kinds_get_the_higher_caps(self):
        for kind in (KIND_GATEWAY_SESSION, KIND_GATEWAY_POOLED):
            assert caps_for(
                kind,
                day_cap=150,
                month_cap=2000,
                gateway_day_cap=1500,
                gateway_month_cap=10000,
            ) == (1500, 10000)

    def test_a_per_user_config_beats_the_gateway_branch(self):
        """Branch (a). Smithery injects the user's saved configuration on
        every request, so two users behind one gateway are two keys -- and
        because that identifies a person, they get the ordinary caps."""
        one = identify(
            _gateway_headers("shared"),
            {"config": "eyJyYXBpZEFwaUtleSI6ImFhYSJ9"},
            gateway_networks=ANTHROPIC_NETS,
        )
        two = identify(
            _gateway_headers("shared"),
            {"config": "eyJyYXBpZEFwaUtleSI6ImJiYiJ9"},
            gateway_networks=ANTHROPIC_NETS,
        )
        assert one.kind == two.kind == KIND_CONFIG
        assert one.key != two.key
        assert one.pooled is False
        assert caps_for(
            one.kind,
            day_cap=150,
            month_cap=2000,
            gateway_day_cap=1500,
            gateway_month_cap=10000,
        ) == (150, 2000)

    def test_dot_notation_config_is_the_same_thing(self):
        """Smithery's current gateway passes declared config fields through as
        plain query parameters rather than one base64 blob."""
        identity = identify(
            _gateway_headers(),
            {"config.rapidApiKey": "aaa"},
            gateway_networks=ANTHROPIC_NETS,
        )
        assert identity.kind == KIND_CONFIG

    def test_an_extra_identity_param_can_be_named(self):
        plain = identify(_gateway_headers(), {"profile": "user-7"})
        named = identify(
            _gateway_headers(),
            {"profile": "user-7"},
            identity_params=("profile",),
        )
        assert plain.kind != KIND_CONFIG
        assert named.kind == KIND_CONFIG

    def test_an_empty_config_is_not_an_identity(self):
        """A gateway that always appends the parameter, and a server that
        declares no schema, produce the same blob for everybody. Treating
        that as 'all different users' would switch the cap off for all
        gateway traffic at once."""
        for blob in ("", "{}", "e30=", "e30"):
            identity = identify(
                _gateway_headers(), {"config": blob},
                gateway_networks=ANTHROPIC_NETS,
            )
            assert identity.kind == KIND_GATEWAY_SESSION, blob

    def test_a_request_with_nothing_to_key_on_is_not_counted(self):
        assert identify({}, {}) is None

    def test_a_gateway_recognised_only_by_peer_still_gets_a_counter(self):
        """No headers at all, but the socket peer is in a known range. Keyed
        on the address our edge saw, not dropped: a caller we can see is a
        caller we can count."""
        identity = identify(
            {}, {}, peer=ANTHROPIC_IP, gateway_networks=ANTHROPIC_NETS
        )
        assert identity.kind == KIND_GATEWAY_POOLED


# ── the higher caps cannot be claimed ────────────────────────────────────


class TestNobodyTalksTheirWayIn:
    def test_a_forged_leftmost_forwarded_for_does_not_work(self):
        """Our edge appends the real peer after whatever the caller sent, so
        the leftmost entry is the caller's own text. Gating on it would let
        anyone have the pooled caps for the price of one header."""
        forged = {
            "x-forwarded-for": f"{ANTHROPIC_IP}, 203.0.113.9",
            "user-agent": "python-httpx/0.27.0",
            "mcp-session-id": "rotates-every-call",
        }
        identity = identify(forged, {}, gateway_networks=ANTHROPIC_NETS)
        assert identity.kind == KIND_DIRECT

    def test_the_trusted_addresses_exclude_the_leftmost_entry(self):
        addresses = trusted_addresses(
            {"x-forwarded-for": "1.2.3.4, 5.6.7.8", "x-real-ip": "9.9.9.9"},
            "10.0.0.1",
        )
        assert "1.2.3.4" not in addresses
        assert addresses == ["5.6.7.8", "9.9.9.9", "10.0.0.1"]

    def test_a_user_agent_alone_proves_nothing_by_default(self):
        """Smithery publishes no egress range and tells origins to allowlist
        its user agent instead. That is a forgeable signal, so it is opt-in."""
        headers = dict(ABUSER, **{"user-agent": "SmitheryBot/1.0"})
        assert identify(headers, {}).kind == KIND_DIRECT
        assert (
            identify(headers, {}, gateway_user_agents=("smithery",)).kind
            == KIND_GATEWAY_POOLED
        )

    def test_a_gateways_own_api_key_never_identifies_a_user(self):
        """Rule 6: `?api_key=` on a gateway URL is the GATEWAY's key, the same
        value for every user behind it. Naming it as an identity parameter
        must not turn one shared counter into a per-user one."""
        assert config_blob({"api_key": "smithery-key"}, ("api_key",)) == ""

    def test_no_configured_ranges_means_no_gateway_branch(self):
        assert gateway_egress([ANTHROPIC_IP], []) is False

    def test_an_unparseable_address_is_not_a_match(self):
        assert gateway_egress(["not-an-ip", None, ""], ANTHROPIC_NETS) is False


class TestTheRangesComeFromPolicy:
    def test_anthropics_published_range_is_included(self):
        nets = ClientClassifier().gateway_networks()
        assert ipaddress.ip_address(ANTHROPIC_IP) in nets[0]

    def test_an_extra_cidr_can_be_configured(self):
        nets = ClientClassifier().gateway_networks(("198.51.100.0/24",))
        assert any(
            ipaddress.ip_address("198.51.100.5") in net for net in nets
        )

    def test_an_unparseable_cidr_is_dropped_not_fatal(self):
        before = len(ClientClassifier().gateway_networks())
        after = ClientClassifier().gateway_networks(("nonsense",))
        assert len(after) == before


# ── what a reader of the logs and metrics sees ───────────────────────────


class TestItIsReadable:
    def test_the_log_line_names_the_branch(self):
        from src.fair_use import FairUseState

        line = log_line(
            FairUseState(
                key="abc123abc123",
                used_today=1500,
                used_month=1500,
                day_cap=1500,
                month_cap=10000,
                kind=KIND_GATEWAY_POOLED,
            ),
            "block",
        )
        assert "kind=gateway_pooled" in line
        # Still nothing derived from the caller beyond the one-way key.
        assert ANTHROPIC_IP not in line and "claude-user" not in line

    @pytest.mark.asyncio
    async def test_metrics_splits_today_by_kind(self):
        """`fair_use_blocked_today: 12` means two opposite things depending on
        which kind it fired against, and this is the only field that says
        which."""
        store = MemoryCounterStore()
        telemetry = Telemetry(
            store=store,
            log_path=None,
            fair_use_enabled=True,
            fair_use_day_cap=150,
            fair_use_month_cap=2000,
            fair_use_gateway_day_cap=1500,
            fair_use_gateway_month_cap=10000,
        )
        now = time.time()
        base = dict(
            timestamp=now,
            tool="search_oneway_flights",
            tier="unknown",
            client_name=None,
            source_ip=None,
            widget_capable=False,
            requested_combinations=1,
            backend_failures=0,
            results_returned=1,
            duration_ms=10,
            truncated=False,
            allowed=True,
            decision_reason="monitor",
            ad_eligible=False,
        )
        await telemetry.record(
            CallRecord(
                **base,
                backend_calls=12,
                fair_use_key="aaaaaaaaaaaa",
                fair_use_kind=KIND_DIRECT,
            )
        )
        await telemetry.record(
            CallRecord(
                **{**base, "error": "fair_use_blocked"},
                backend_calls=0,
                fair_use_key="aaaaaaaaaaaa",
                fair_use_kind=KIND_DIRECT,
                fair_use_blocked=True,
            )
        )
        await telemetry.record(
            CallRecord(
                **base,
                backend_calls=3,
                fair_use_key="bbbbbbbbbbbb",
                fair_use_kind=KIND_GATEWAY_SESSION,
            )
        )

        series = await telemetry.call_series(hours=2)
        by_kind = series["fair_use"]["by_key_kind"]
        assert by_kind[KIND_DIRECT] == {
            "tool_calls": 2,
            "backend_calls": 12,
            "blocked": 1,
        }
        assert by_kind[KIND_GATEWAY_SESSION] == {
            "tool_calls": 1,
            "backend_calls": 3,
            "blocked": 0,
        }
        assert series["fair_use"]["gateway_day_cap"] == 1500
        assert series["fair_use"]["gateway_month_cap"] == 10000
        assert fair_use_kind_key(now) in store._fair_use_kinds

    @pytest.mark.asyncio
    async def test_an_unidentified_call_adds_no_kind_row(self):
        store = MemoryCounterStore()
        telemetry = Telemetry(store=store, log_path=None, fair_use_enabled=True)
        await telemetry.record(
            CallRecord(
                timestamp=time.time(),
                tool="search_oneway_flights",
                tier="unknown",
                client_name=None,
                source_ip=None,
                widget_capable=False,
                requested_combinations=1,
                backend_calls=1,
                backend_failures=0,
                results_returned=1,
                duration_ms=10,
                truncated=False,
                allowed=True,
                decision_reason="monitor",
                ad_eligible=False,
            )
        )
        assert await store.fair_use_kind_counts(time.time()) == {}


# ── end to end, through the tools ────────────────────────────────────────


@pytest.fixture
def backend_calls(monkeypatch):
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


def _server(monkeypatch, **env):
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    monkeypatch.setenv("LOG_PATH", "")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return build_server(load_settings())


def _as_caller(monkeypatch, headers, peer, query=None):
    monkeypatch.setattr(
        server_module,
        "_request_context",
        lambda: (dict(headers), peer, dict(query or {})),
    )


async def _spend(server, key, backend_calls, kind):
    await server.telemetry.record(
        CallRecord(
            timestamp=time.time(),
            tool="search_oneway_flights",
            tier="unknown",
            client_name=None,
            source_ip=None,
            widget_capable=False,
            requested_combinations=backend_calls,
            backend_calls=backend_calls,
            backend_failures=0,
            results_returned=1,
            duration_ms=10,
            truncated=False,
            allowed=True,
            decision_reason="monitor",
            ad_eligible=False,
            fair_use_key=key,
            fair_use_kind=kind,
        )
    )


SEARCH = {
    "from_airport": "TLV",
    "to_airport": "FCO",
    "departure_date": "2026-10-14",
}


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_the_abuser_is_still_capped_at_150(
        self, monkeypatch, backend_calls, caplog
    ):
        """The whole point of the 2026-09-05 cap, unchanged: a direct script
        with rotating sessions and no configuration still hits 150 a day."""
        _as_caller(monkeypatch, ABUSER, "203.0.113.9")
        server = _server(monkeypatch)
        key = client_key(ABUSER)
        await _spend(server, key, 150, KIND_DIRECT)
        with caplog.at_level(logging.INFO, logger="src.server"):
            async with Client(server) as client:
                result = await client.call_tool("search_oneway_flights", SEARCH)
        assert backend_calls == []
        assert result.structured_content["search_status"] == "rate_limited"
        assert any(
            "[fair_use] action=block" in r.getMessage()
            and "kind=direct" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_rotating_the_session_does_not_help_a_direct_caller(
        self, monkeypatch, backend_calls
    ):
        """Stateless request after stateless request, each with a brand new
        session id: the key does not move, because a direct key has no session
        in it. This is the property the batch client would otherwise use to
        reset its counter 460 times a morning."""
        server = _server(monkeypatch)
        await _spend(server, client_key(ABUSER), 150, KIND_DIRECT)
        for index in range(3):
            _as_caller(
                monkeypatch,
                dict(ABUSER, **{"mcp-session-id": f"fresh-{index}"}),
                "203.0.113.9",
            )
            async with Client(server) as client:
                result = await client.call_tool("search_oneway_flights", SEARCH)
            assert result.structured_content["search_status"] == "rate_limited"
        assert backend_calls == []

    @pytest.mark.asyncio
    async def test_a_claude_user_is_not_refused_at_the_direct_cap(
        self, monkeypatch, backend_calls
    ):
        """The regression this branch exists to prevent. 150 backend calls
        already spent on this gateway's pooled counter, and the next real user
        behind it still gets served, because a pooled key is held to the
        gateway caps."""
        headers = _gateway_headers(None)
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch)
        identity = identify(
            headers,
            {},
            peer=ANTHROPIC_IP,
            gateway_networks=ANTHROPIC_NETS,
        )
        await _spend(server, identity.key, 150, KIND_GATEWAY_POOLED)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", SEARCH)
        assert len(backend_calls) == 1
        assert result.structured_content["result_count"] == 1
        assert "fair_use" not in result.structured_content

    @pytest.mark.asyncio
    async def test_a_pooled_gateway_is_still_bounded(
        self, monkeypatch, backend_calls, caplog
    ):
        """Higher is not unlimited. At the gateway day cap the pooled counter
        refuses, in the same shape, with the branch named on the log line so a
        reader knows to raise FAIR_USE_GATEWAY_DAY_CAP rather than shrug."""
        headers = _gateway_headers(None)
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch)
        identity = identify(
            headers, {}, peer=ANTHROPIC_IP, gateway_networks=ANTHROPIC_NETS
        )
        await _spend(server, identity.key, 1500, KIND_GATEWAY_POOLED)
        with caplog.at_level(logging.INFO, logger="src.server"):
            async with Client(server) as client:
                result = await client.call_tool("search_oneway_flights", SEARCH)
        assert backend_calls == []
        assert result.structured_content["search_status"] == "rate_limited"
        assert result.structured_content["fair_use"]["day_cap"] == 1500
        assert any(
            "kind=gateway_pooled" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_one_chat_hitting_the_cap_does_not_refuse_the_next(
        self, monkeypatch, backend_calls
    ):
        """Branch (b) with sessions: a heavy conversation spends its own
        allowance, and a different conversation through the same host is
        unaffected."""
        server = _server(monkeypatch)
        heavy = _gateway_headers("session-heavy")
        light = _gateway_headers("session-light")
        heavy_id = identify(
            heavy, {}, peer=ANTHROPIC_IP, gateway_networks=ANTHROPIC_NETS
        )
        await _spend(server, heavy_id.key, 1500, KIND_GATEWAY_SESSION)

        _as_caller(monkeypatch, heavy, ANTHROPIC_IP)
        async with Client(server) as client:
            blocked = await client.call_tool("search_oneway_flights", SEARCH)
        assert blocked.structured_content["search_status"] == "rate_limited"

        _as_caller(monkeypatch, light, ANTHROPIC_IP)
        async with Client(server) as client:
            served = await client.call_tool("search_oneway_flights", SEARCH)
        assert served.structured_content["result_count"] == 1
        assert len(backend_calls) == 1

    @pytest.mark.asyncio
    async def test_the_gateway_caps_come_from_the_environment(
        self, monkeypatch, backend_calls
    ):
        headers = _gateway_headers(None)
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch, FAIR_USE_GATEWAY_DAY_CAP="40")
        identity = identify(
            headers, {}, peer=ANTHROPIC_IP, gateway_networks=ANTHROPIC_NETS
        )
        await _spend(server, identity.key, 40, KIND_GATEWAY_POOLED)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", SEARCH)
        assert result.structured_content["search_status"] == "rate_limited"
        assert result.structured_content["fair_use"]["day_cap"] == 40

    @pytest.mark.asyncio
    async def test_a_hotel_search_takes_the_same_branch(
        self, monkeypatch, backend_calls
    ):
        """Both call sites, or the split is only half done -- a hotel search
        is one backend call on the same counter."""
        headers = _gateway_headers(None)
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch)
        identity = identify(
            headers, {}, peer=ANTHROPIC_IP, gateway_networks=ANTHROPIC_NETS
        )
        await _spend(server, identity.key, 150, KIND_GATEWAY_POOLED)
        async with Client(server) as client:
            result = await client.call_tool("search_hotels", {
                "destination": "Rome",
                "checkin_date": "2026-10-14",
                "checkout_date": "2026-10-16",
            })
        assert len(backend_calls) == 1
        assert result.structured_content.get("search_status") != "rate_limited"

    @pytest.mark.asyncio
    async def test_the_kind_reaches_the_metrics_through_a_real_call(
        self, monkeypatch, backend_calls
    ):
        headers = _gateway_headers("session-metrics")
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch)
        async with Client(server) as client:
            await client.call_tool("search_oneway_flights", SEARCH)
        series = await server.telemetry.call_series(hours=2)
        assert (
            series["fair_use"]["by_key_kind"][KIND_GATEWAY_SESSION][
                "backend_calls"
            ]
            == 1
        )

    @pytest.mark.asyncio
    async def test_the_off_switch_still_switches_everything_off(
        self, monkeypatch, backend_calls
    ):
        headers = _gateway_headers(None)
        _as_caller(monkeypatch, headers, ANTHROPIC_IP)
        server = _server(monkeypatch, FAIR_USE_ENABLED="0")
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", SEARCH)
        assert result.structured_content["result_count"] == 1
        series = await server.telemetry.call_series(hours=2)
        assert series["fair_use"]["by_key_kind"] == {}
