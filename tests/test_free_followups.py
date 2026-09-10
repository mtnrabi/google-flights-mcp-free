"""Follow-ups from the 2026-09-09 free-server sign-in tests.

Seven findings from two live runs against production, each one a thing that
was true and invisible:

1. `/health` said `{"status": "ok"}` for the 25 minutes in which every
   `/oauth/register` answered 503 and no MCP client could sign in -- because
   it reported that sign-in was CONFIGURED and never asked whether it
   WORKED. It probes both stores now, and degrades.
2. `FREE_ANON_MODE=open` was set as the incident stopgap and appeared to do
   nothing. Two separate reasons, and this file pins the one that is ours:
   the cap gate refused `initialize` and `tools/list`, not just searches, so
   a caller whose day counter was already past the taster cap could not even
   connect and the rollback looked inert. (The other reason is the platform:
   a Vercel env var reaches only the NEXT deployment, which is why `/health`
   now reports `anon_mode` from the running process.)
3. Three sentences in the served pages still promised an "open endpoint" that
   "works without an account and always will". There is no such endpoint.
4. The `/mcp/oauth` protected-resource document named `/mcp` as its
   `resource`, which RFC 9728 3.3 invites a strict client to reject.
5. A successful search carried no usage number at all until 80% of a cap, so
   a signed-in user could not see where they stood for their first 119
   searches and the model had nothing to nudge with.
6. The consent page's "this page removes your account and your address
   entirely" reads as though visiting the page deletes you.
7. An anonymous `initialize` was answered 401 `rate_limited`. A cap must
   never take away a client's ability to connect.

    python -m pytest mcp_server/tests/test_free_followups.py -q
"""

import json
import time

import httpx
import pytest
from fastmcp import Client
from starlette.applications import Starlette

from src import lambda_client as lambda_client_module
from src import server as server_module
from src.anon_gate import anon_cap_middleware
from src.fair_use import (
    KIND_SIGNED_IN,
    SIGNED_IN_EMAIL_HEADER,
    SIGNED_IN_HEADER,
    FairUseState,
    identify,
    usage_note,
)
from src.freeusers import MemoryFreeUserStore, NullFreeUserStore
from src.hard_limit import hard_limit_middleware
from src.hotels_lambda_client import HotelsLambdaClient
from src.oauth import (
    MCP_OAUTH_PATH,
    MCP_PATH,
    OAuthResourceGate,
    OAuthSupport,
    anon_caps,
    anon_mode,
    consent_html,
)
from src.oauthstore import MemoryOAuthStore, NullOAuthStore, OAuthStoreError
from src.oauthroutes import signed_in_html, signed_out_html
from src.server import build_server
from src.settings import load_settings
from src.telemetry import CallRecord

DIRECT_HEADERS = {"user-agent": "curl/8.5.0"}
PEER = "198.51.100.7"


# ── the app, built the way api/index.py builds it ────────────────────────


def _build(monkeypatch, **env):
    """The real stack: gate on the outside, hard limit and cap gate inside."""
    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.test/neon")
    monkeypatch.delenv("FREE_ANON_MODE", raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    server = build_server(load_settings())
    inner = server.http_app(
        stateless_http=True,
        middleware=list(hard_limit_middleware(server))
        + list(anon_cap_middleware(server)),
    )
    app = Starlette(lifespan=inner.lifespan)
    app.mount("/", OAuthResourceGate(inner, getattr(server, "fp_oauth", None)))
    return server, app


async def _rpc(app, method, params=None, path=MCP_PATH, headers=None, peer=PEER):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                path,
                json=body,
                headers={
                    **DIRECT_HEADERS,
                    **(headers or {}),
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )


async def _get(app, path):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(PEER, 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)


async def _spend(server, key, backend_calls, kind="direct"):
    await server.telemetry.record(
        CallRecord(
            timestamp=time.time(),
            tool="search_oneway_flights",
            tier="unknown",
            client_name=None,
            source_ip=PEER,
            widget_capable=False,
            requested_combinations=backend_calls,
            backend_calls=backend_calls,
            backend_failures=0,
            results_returned=1,
            duration_ms=1,
            truncated=False,
            allowed=True,
            decision_reason="monitor",
            ad_eligible=False,
            fair_use_key=key,
            fair_use_kind=kind,
        )
    )


def _anon_key():
    """The counting key the cap gate computes for our anonymous caller."""
    return identify(
        {**DIRECT_HEADERS, "x-real-ip": PEER}, {}, peer=PEER
    ).key


# ── 1. /health probes the stores ─────────────────────────────────────────


class _Store:
    """A store whose probe answers however the test wants it to."""

    available = True

    def __init__(self, ok=True):
        self._ok = ok
        self.pings = 0

    async def ping(self, timeout: float = 0.0) -> bool:
        self.pings += 1
        if not self._ok:
            raise OAuthStoreError("could not reach the store: No module named asyncpg")
        return True


def _support(store=None, users=None):
    return OAuthSupport(
        store=store if store is not None else _Store(),
        auth=object(),
        origin="https://free.test",
        product="travel",
        users=users if users is not None else _Store(),
    )


class TestStoreProbes:
    @pytest.mark.asyncio
    async def test_both_stores_are_probed(self):
        store, users = _Store(), _Store()
        got = await _support(store, users).store_health()
        assert got == {"oauth": "ok", "free_users": "ok"}
        # BOTH, every time: they share a DATABASE_URL so they usually fail
        # together, but a migration applied to one database and not the other
        # breaks exactly one, and a single probe would miss it.
        assert (store.pings, users.pings) == (1, 1)

    @pytest.mark.asyncio
    async def test_the_2026_09_09_failure_is_reported(self):
        """The exact shape of the incident: driver missing, store dead."""
        got = await _support(_Store(ok=False), _Store()).store_health()
        assert got == {"oauth": "unreachable", "free_users": "ok"}

    @pytest.mark.asyncio
    async def test_an_unconfigured_store_is_not_an_outage(self):
        got = await _support(NullOAuthStore(), NullFreeUserStore()).store_health()
        assert got == {"oauth": "not_configured", "free_users": "not_configured"}

    @pytest.mark.asyncio
    async def test_a_hanging_store_does_not_hang_health(self):
        """A probe that never returns must not become a request that never
        returns. `/health` answering slowly is how a monitor learns nothing."""

        class Hangs:
            available = True

            async def ping(self, timeout: float = 0.0) -> bool:
                import asyncio

                await asyncio.sleep(30)
                return True

        started = time.monotonic()
        got = await _support(Hangs(), _Store()).store_health(timeout=0.05)
        assert got["oauth"] == "unreachable"
        assert time.monotonic() - started < 5

    @pytest.mark.asyncio
    async def test_the_memory_stores_answer_the_probe(self):
        """Every store implements it, so the suite exercises the same path
        production does rather than a mock of it."""
        assert await MemoryOAuthStore().ping() is True
        assert await MemoryFreeUserStore().ping() is True


class TestHealthProbesTheStores:
    @pytest.mark.asyncio
    async def test_health_degrades_when_sign_in_is_dead(self, monkeypatch):
        """The regression that matters: on 2026-09-09 `/health` was green
        while every sign-in path answered 503, so nothing alerted us."""
        server, app = _build(monkeypatch)
        object.__setattr__(server.fp_oauth, "store", _Store(ok=False))
        object.__setattr__(server.fp_oauth, "users", _Store())
        body = (await _get(app, "/health")).json()
        assert body["status"] == "degraded"
        assert body["signin_store"] == "unreachable"
        assert body["stores"] == {"oauth": "unreachable", "free_users": "ok"}

    @pytest.mark.asyncio
    async def test_health_is_ok_when_both_stores_answer(self, monkeypatch):
        server, app = _build(monkeypatch)
        object.__setattr__(server.fp_oauth, "store", _Store())
        object.__setattr__(server.fp_oauth, "users", _Store())
        body = (await _get(app, "/health")).json()
        assert body["status"] == "ok"
        assert body["signin_store"] == "ok"

    @pytest.mark.asyncio
    async def test_stores_zero_skips_the_probe(self, monkeypatch):
        """A liveness check should not open two database connections."""
        server, app = _build(monkeypatch)
        store = _Store()
        object.__setattr__(server.fp_oauth, "store", store)
        body = (await _get(app, "/health?stores=0")).json()
        assert body["status"] == "ok"
        assert "stores" not in body
        assert store.pings == 0

    @pytest.mark.asyncio
    async def test_health_names_the_running_anon_mode(self, monkeypatch):
        """The other half of the 2026-09-09 rollback failure: a Vercel env var
        reaches only the NEXT deployment, so "I set FREE_ANON_MODE" and "the
        server has FREE_ANON_MODE" are different claims. This is the one curl
        that settles which."""
        _, app = _build(monkeypatch)
        assert (await _get(app, "/health?stores=0")).json()["anon_mode"] == "challenge"
        _, app = _build(monkeypatch, FREE_ANON_MODE="open")
        assert (await _get(app, "/health?stores=0")).json()["anon_mode"] == "open"


# ── 2 and 7. the rollback switch actually rolls back ─────────────────────


class TestTheRollbackSwitch:
    @pytest.mark.asyncio
    async def test_open_serves_an_anonymous_caller(self, monkeypatch):
        """The whole point of the switch: `/mcp` answers with no credential."""
        _, app = _build(monkeypatch, FREE_ANON_MODE="open")
        response = await _rpc(app, "tools/list")
        assert response.status_code == 200
        assert "search_oneway_flights" in response.text

    @pytest.mark.asyncio
    async def test_challenge_is_still_the_default(self, monkeypatch):
        """On the call that spends. `tools/list` is read-only discovery and
        is served in either mode (src/discovery.py), which is what keeps the
        directory health checks green."""
        _, app = _build(monkeypatch)
        response = await _rpc(
            app,
            "tools/call",
            {
                "name": "search_oneway_flights",
                "arguments": {
                    "from_airport": "TLV",
                    "to_airport": "FCO",
                    "departure_date": "2026-10-14",
                },
            },
        )
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert (await _rpc(app, "tools/list")).status_code == 200

    @pytest.mark.asyncio
    async def test_open_serves_a_search_under_the_taster_cap(
        self, monkeypatch, backend_calls
    ):
        _, app = _build(monkeypatch, FREE_ANON_MODE="open")
        response = await _rpc(
            app,
            "tools/call",
            {
                "name": "search_oneway_flights",
                "arguments": {
                    "from_airport": "TLV",
                    "to_airport": "FCO",
                    "departure_date": "2026-10-14",
                },
            },
        )
        assert response.status_code == 200
        assert len(backend_calls) == 1

    @pytest.mark.asyncio
    async def test_the_bug_that_made_the_switch_look_inert(
        self, monkeypatch, backend_calls
    ):
        """THE ROOT CAUSE, pinned.

        At 18:1xZ on 2026-09-09 the switch was flipped during an incident and
        `POST /mcp tools/list` still answered 401 -- so it was written off as
        "no visible effect". The caller's day counter was already far past
        `FREE_ANON_DAILY_CAP` (our own traffic had spent 150 that day under
        the ordinary allowance), and the cap gate refused EVERY POST on
        `/mcp`, `initialize` and `tools/list` included. The rollback had in
        fact taken effect; it just could not be observed, because the one
        call anybody makes to check was the one being refused.

        Now the cap refuses only what spends something.
        """
        server, app = _build(monkeypatch, FREE_ANON_MODE="open")
        await _spend(server, _anon_key(), 150)

        for method, params in (
            ("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            }),
            ("tools/list", None),
        ):
            response = await _rpc(app, method, params)
            assert response.status_code == 200, method

        # And the call that would actually spend a backend search is still
        # refused, with the 401 that makes a client show a Sign in button.
        refused = await _rpc(
            app,
            "tools/call",
            {
                "name": "search_oneway_flights",
                "arguments": {
                    "from_airport": "TLV",
                    "to_airport": "FCO",
                    "departure_date": "2026-10-14",
                },
            },
        )
        assert refused.status_code == 401
        assert "resource_metadata=" in refused.headers["www-authenticate"]
        assert refused.json()["error"]["data"]["search_status"] == "rate_limited"
        assert backend_calls == []

    @pytest.mark.asyncio
    async def test_initialize_is_never_capped_in_challenge_mode_either(
        self, monkeypatch
    ):
        """Two properties, one caller: a client whose day counter is already
        spent, in the shipped mode.

        `initialize` is read-only discovery, so it is SERVED -- neither gate
        may refuse it, or the client cannot connect and a directory health
        check calls the whole server unhealthy (src/discovery.py). The call
        that would spend a search is refused by the OAuth gate, and the
        reason must be `sign_in_required`, never `rate_limited`: a client
        told "you are over a limit" does not show a Sign in button.
        """
        server, app = _build(monkeypatch)
        await _spend(server, _anon_key(), 150)
        started = await _rpc(app, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        })
        assert started.status_code == 200, started.text

        refused = await _rpc(
            app,
            "tools/call",
            {
                "name": "search_oneway_flights",
                "arguments": {
                    "from_airport": "TLV",
                    "to_airport": "FCO",
                    "departure_date": "2026-10-14",
                },
            },
        )
        assert refused.status_code == 401
        assert refused.json()["error"]["data"] == {"reason": "sign_in_required"}

    @pytest.mark.asyncio
    async def test_open_does_not_reopen_the_alias(self, monkeypatch):
        _, app = _build(monkeypatch, FREE_ANON_MODE="open")
        response = await _rpc(app, "tools/list", path=MCP_OAUTH_PATH)
        assert response.status_code == 401


class TestTheAnonTierIsOnlyDescribedWhenItExists:
    """`anon_caps` is the single reader of the mode, so a cap that cannot be
    reached never reaches a page, a tool description or a result."""

    def test_challenge_has_no_anonymous_tier(self, monkeypatch):
        monkeypatch.delenv("FREE_ANON_MODE", raising=False)
        monkeypatch.setenv("FREE_ANON_DAILY_CAP", "10")
        assert anon_caps(load_settings()) == (0, 0)

    def test_open_has_one(self, monkeypatch):
        monkeypatch.setenv("FREE_ANON_MODE", "open")
        monkeypatch.setenv("FREE_ANON_DAILY_CAP", "10")
        monkeypatch.setenv("FREE_ANON_MONTHLY_CAP", "0")
        assert anon_caps(load_settings()) == (10, 0)

    def test_a_typo_still_falls_towards_sign_in(self, monkeypatch):
        monkeypatch.setenv("FREE_ANON_MODE", "Open ")
        assert anon_mode() == "open"
        monkeypatch.setenv("FREE_ANON_MODE", "opne")
        assert anon_caps(load_settings()) == (0, 0)


# ── 3 and 6. the copy ────────────────────────────────────────────────────

#: Every phrase that promises an endpoint this server does not have. Each one
#: was live on 2026-09-09, on a server where all five candidate anonymous
#: paths answered 404.
DEAD_PROMISES = (
    "without an account",
    "with no account at all",
    "open endpoint",
    "needs no account",
    "works without an account and always will",
)


def _pages():
    return {
        "signed_out": signed_out_html("https://free.test/mcp"),
        "signed_in_challenge": signed_in_html(
            email="a@b.test",
            sign_in_url="https://free.test/mcp",
            csrf="x",
            day_cap=150,
            anon_day_cap=0,
        ),
        "consent": consent_html(
            client_name="Claude",
            client_id="fpcl_x",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            email="a@b.test",
            product="travel",
            sealed="s",
            csrf="x",
            day_cap=150,
            anon_day_cap=0,
        ),
    }


class TestTheCopyMatchesTheProduct:
    @pytest.mark.parametrize("name", sorted(_pages()))
    def test_no_page_promises_an_open_endpoint(self, name):
        page = _pages()[name].lower()
        for phrase in DEAD_PROMISES:
            assert phrase not in page, f"{name}: {phrase!r}"

    def test_the_signed_out_page_says_sign_in_is_required(self):
        page = _pages()["signed_out"]
        assert "needs a Google sign-in" in page
        assert "Sign in with Google" in page

    def test_no_anonymous_allowance_is_quoted_in_challenge_mode(self):
        """`anon_day_cap=0` is what `oauth.anon_caps` hands the page unless
        the rollback is on, and a number nobody can reach must not be
        printed."""
        assert "A caller with no account" not in _pages()["signed_in_challenge"]
        assert "150 searches a day" in _pages()["signed_in_challenge"]

    def test_the_rollback_does_put_the_number_back(self):
        page = signed_in_html(
            email="a@b.test",
            sign_in_url="https://free.test/mcp",
            csrf="x",
            day_cap=150,
            anon_day_cap=10,
        )
        assert "A caller with no account shares 10 a day" in page

    def test_the_consent_page_does_not_threaten_to_delete_you(self):
        """It read "Every message carries an unsubscribe link, and this page
        removes your account and your address entirely" -- which says that
        coming back here deletes you, rather than that you may delete
        yourself here."""
        page = " ".join(_pages()["consent"].split())
        assert "this page</a> removes your account" not in page
        assert (
            "You can delete your account and your address any time from "
            '<a href="/connect">your account page</a>.' in page
        )

    def test_the_signed_out_page_does_not_either(self):
        page = _pages()["signed_out"]
        assert "Coming back to this page removes your account" not in page
        assert "delete your account and your address any time" in page


# ── 4. the alias metadata names the alias ────────────────────────────────


class TestProtectedResourceMetadata:
    def test_each_document_names_what_it_was_asked_about(self):
        support = _support()
        assert support.protected_resource_metadata(MCP_PATH)["resource"] == (
            "https://free.test/mcp"
        )
        assert support.protected_resource_metadata(MCP_OAUTH_PATH)["resource"] == (
            "https://free.test/mcp/oauth"
        )

    def test_it_is_still_one_audience(self):
        """Echoing the path does not mint a second audience. Both names pass
        `resource_matches`, which is what `validate_access_token` calls, so a
        token obtained after discovery on the alias is spendable on `/mcp`
        and the other way round."""
        support = _support()
        for path in (MCP_PATH, MCP_OAUTH_PATH):
            named = support.protected_resource_metadata(path)["resource"]
            assert support.resource_matches(named)

    def test_an_unknown_path_falls_back_to_the_canonical_one(self):
        assert _support().protected_resource_metadata("/nonsense")["resource"] == (
            "https://free.test/mcp"
        )

    @pytest.mark.asyncio
    async def test_over_the_wire(self, monkeypatch):
        _, app = _build(monkeypatch)
        got = await _get(app, "/.well-known/oauth-protected-resource/mcp/oauth")
        assert got.status_code == 200
        assert got.json()["resource"].endswith(MCP_OAUTH_PATH)
        got = await _get(app, "/.well-known/oauth-protected-resource")
        assert got.json()["resource"].endswith(MCP_PATH)


# ── 5. every successful result carries the usage block ───────────────────


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


SIGNED_IN = {
    SIGNED_IN_HEADER: "google-sub-12345",
    SIGNED_IN_EMAIL_HEADER: "traveller@example.com",
    "user-agent": "claude-code/1.0",
}


@pytest.fixture
def signed_in_caller(monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_request_context",
        lambda: (dict(SIGNED_IN), "10.0.0.1", {}),
    )
    return identify(SIGNED_IN)


def _tool_server(monkeypatch):
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    monkeypatch.setenv("LOG_PATH", "")
    return build_server(load_settings())


SEARCH = {
    "from_airport": "TLV",
    "to_airport": "FCO",
    "departure_date": "2026-10-14",
}


class TestEverySuccessCarriesTheNumber:
    @pytest.mark.asyncio
    async def test_it_increments_across_two_calls(
        self, monkeypatch, backend_calls, signed_in_caller
    ):
        """1 then 2. Until 2026-09-09 both of these carried nothing at all,
        so a signed-in user's first 119 searches were invisible to them and
        the model had no number to nudge with."""
        server = _tool_server(monkeypatch)
        async with Client(server) as client:
            first = await client.call_tool("search_oneway_flights", SEARCH)
            second = await client.call_tool("search_oneway_flights", SEARCH)

        one = first.structured_content["fair_use"]
        two = second.structured_content["fair_use"]
        assert (one["used_today"], two["used_today"]) == (1, 2)
        assert (one["used_month"], two["used_month"]) == (1, 2)
        assert one["day_cap"] == 150 and one["month_cap"] == 2000
        assert one["signed_in"] is True

    @pytest.mark.asyncio
    async def test_it_names_the_account(
        self, monkeypatch, backend_calls, signed_in_caller
    ):
        """Whose allowance this is. A user with two Google accounts otherwise
        has no way to tell which one their client is spending."""
        server = _tool_server(monkeypatch)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", SEARCH)
        block = result.structured_content["fair_use"]
        assert block["user"] == "traveller@example.com"
        assert block["human"] == (
            "1 of 150 searches today, 1 of 2,000 this month, "
            "on traveller@example.com."
        )

    @pytest.mark.asyncio
    async def test_a_hotel_search_carries_it_too(
        self, monkeypatch, backend_calls, signed_in_caller
    ):
        server = _tool_server(monkeypatch)
        async with Client(server) as client:
            result = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-10-14",
                    "checkout_date": "2026-10-16",
                },
            )
        assert result.structured_content["fair_use"]["used_today"] == 1

    @pytest.mark.asyncio
    async def test_the_warning_shape_still_takes_over_at_eighty_percent(
        self, monkeypatch, backend_calls, signed_in_caller
    ):
        """The compact receipt is the default, not a replacement: past 80% the
        richer block comes back, with the directions and the `upgrade` object
        the upsell depends on."""
        server = _tool_server(monkeypatch)
        await _spend(server, signed_in_caller.key, 130, KIND_SIGNED_IN)
        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", SEARCH)
        block = result.structured_content["fair_use"]
        assert block["used_today"] == 131
        assert "note" in block and block["note"]
        assert block["user"] == "traveller@example.com"
        assert "upgrade" in result.structured_content

    def test_the_sentence_reads_plainly(self):
        state = FairUseState(
            key="k",
            used_today=7,
            used_month=40,
            day_cap=150,
            month_cap=2000,
            kind=KIND_SIGNED_IN,
        )
        assert usage_note(state)["human"] == (
            "7 of 150 searches today, 40 of 2,000 this month for this account."
        )
        assert "note" not in usage_note(state)

    def test_an_anonymous_caller_gets_no_user_field(self):
        state = FairUseState(key="k", used_today=1, used_month=1, day_cap=10,
                             month_cap=0)
        block = usage_note(state)
        assert "user" not in block
        assert block["human"] == "1 of 10 searches today."


# ── the identity carries the email, and only for display ─────────────────


class TestTheSignedInEmail:
    def test_it_rides_on_the_identity(self):
        identity = identify(SIGNED_IN)
        assert identity.kind == KIND_SIGNED_IN
        assert identity.email == "traveller@example.com"

    def test_it_is_not_part_of_the_counting_key(self):
        """Changing the address on a Google account must not hand its owner a
        fresh allowance. The key is the `sub` and nothing else."""
        other = dict(SIGNED_IN, **{SIGNED_IN_EMAIL_HEADER: "renamed@example.com"})
        assert identify(SIGNED_IN).key == identify(other).key

    def test_an_anonymous_caller_has_no_email(self):
        assert identify({**DIRECT_HEADERS, "x-real-ip": PEER}).email == ""


# ── the method sniff replays the body it read ────────────────────────────


class TestTheGateGivesTheBodyBack:
    @pytest.mark.asyncio
    async def test_a_streamed_body_reaches_the_app_intact(self, monkeypatch):
        """The gates now read the JSON-RPC method, which means reading the
        request body. Everything they take off the wire has to be handed
        back, in order, or the app parses a truncated request."""
        seen: dict = {}

        async def app(scope, receive, send):
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body") or b"")
                if not message.get("more_body"):
                    break
            seen["body"] = b"".join(chunks)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).encode()
        parts = [payload[:10], payload[10:]]

        async def receive():
            body = parts.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(parts),
            }

        _, built = _build(monkeypatch, FREE_ANON_MODE="open")
        gate = built.routes[-1].app
        gate.app = app
        scope = {
            "type": "http",
            "method": "POST",
            "path": MCP_PATH,
            "raw_path": MCP_PATH.encode(),
            "headers": [(b"user-agent", b"curl/8.5.0")],
            "client": (PEER, 5000),
            "query_string": b"",
        }
        sent = []

        async def send(message):
            sent.append(message)

        await gate(scope, receive, send)
        assert seen["body"] == payload
        assert sent[0]["status"] == 200
