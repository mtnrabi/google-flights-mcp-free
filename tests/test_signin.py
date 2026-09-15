"""
The free server's Google sign-in: the endpoints, the caps, the list.

What this file pins, and why each one is here rather than assumed:

1. **The three doors.** `/mcp` is the URL we publish and behaves the way the
   rest of the market behaves -- a credential is served, none is served on
   the taster allowance, and the challenge arrives when that runs out.
   `/mcp/oauth` always challenges, for printed guides and for clients whose
   auth mode is fixed when a server is added. `/mcp/key` never challenges,
   for a gateway that probes with no credential.
2. **Who gets a 401 and who gets a 200.** A direct caller past the taster cap
   gets 401 + `WWW-Authenticate`, which is what turns a refusal into a Sign
   in button. A pooled gateway caller gets the in-band `rate_limited` JSON --
   Smithery proxies this server for its users with no credential of theirs,
   and a mid-session upstream 401 there is a broken integration, not a
   prompt (state/gtm/research/smithery-oauth-2026-09-09.md).
3. **The exact directions.** Both refusals carry text that names the number
   that stopped the call and the one action that lifts it. A refusal a reader
   cannot act on is the same as an outage.
4. **Per-user caps.** A signed-in caller's key is their Google account and
   nothing else: not their address, not their user agent, not their session.
   Two signed-in users cannot spend each other's allowance, and moving
   machine does not reset one.
5. **The header cannot be forged.** The identity header is the whole basis
   for the larger allowance, so an inbound copy is stripped on every path.
6. **The list.** A row per account, written once, with the Resend rules that
   cannot be got wrong: never re-add an unsubscribed address, never send the
   welcome twice, never let either fail a sign-in.

    python -m pytest mcp_server/tests/test_signin.py -q
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.anon_gate import AnonCapMiddleware, anon_cap_middleware
from src.fair_use import (
    KIND_DIRECT,
    KIND_GATEWAY_POOLED,
    KIND_SIGNED_IN,
    SIGNED_IN_HEADER,
    FairUseState,
    anon_directions,
    caps_for,
    client_key,
    identify,
    rate_limited_result,
    signin_directions,
    upgrade_block,
    upgrade_tail,
)
from src.freeusers import (
    RESEND_SKIPPED,
    MemoryFreeUserStore,
    NullFreeUserStore,
    new_unsubscribe_token,
    utc_day,
)
from src.hard_limit import hard_limit_middleware
from src.maillist import (
    MailConfig,
    remember_signin,
    send_welcome,
    sync_contact,
    unsubscribe_url,
)
from src.oauth import (
    IDENTITY_HEADERS,
    MCP_OAUTH_PATH,
    MCP_PATH,
    OAuthResourceGate,
    OAuthSupport,
    anon_mode,
    strip_identity_headers,
)
from src.oauthstore import MemoryOAuthStore
from src.server import build_server
from src.settings import load_settings
from src.telemetry import CallRecord
from src.webauth import GoogleWebAuth, build_web_auth, derive_secret, signing_master

ORIGIN = "https://mcp.test.invalid"

DIRECT_HEADERS = {
    "x-forwarded-for": "198.51.100.7",
    "user-agent": "python-httpx/0.27",
}
DIRECT_KEY = client_key(DIRECT_HEADERS)

# claude.ai's published egress. `policy.ANTHROPIC_OUTBOUND_RANGES`.
POOLED_HEADERS = {
    "x-real-ip": "160.79.104.7",
    "x-forwarded-for": "160.79.104.7",
    "user-agent": "Claude-User/1.0",
}


def _auth() -> GoogleWebAuth:
    master = signing_master("test-client-secret")
    return GoogleWebAuth(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri=f"{ORIGIN}/connect/callback",
        session_secret=derive_secret(master, b"fp-free-session-v1"),
    )


def _support(users=None) -> OAuthSupport:
    return OAuthSupport(
        store=MemoryOAuthStore(),
        auth=_auth(),
        origin=ORIGIN,
        product="travel",
        users=users if users is not None else MemoryFreeUserStore(),
        mail=MailConfig(api_key="", unsubscribe_base=ORIGIN),
    )


# ── the two doors ────────────────────────────────────────────────────────


async def _drive(gate, path, headers=None, method="POST", query=b""):
    """Run one request through an ASGI gate and collect what it sent."""
    sent: list = []

    async def send(message):
        sent.append(message)

    await gate(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query,
            "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
        },
        None,
        send,
    )
    return sent


def _echo(seen: dict):
    async def app(scope, receive, send):
        seen["path"] = scope["path"]
        seen["headers"] = {
            k.decode().lower(): v.decode() for k, v in scope.get("headers") or []
        }
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


def _status(sent):
    return sent[0]["status"]


def _headers_of(sent):
    return {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}


def _body_of(sent):
    return json.loads(sent[1]["body"])


class TestTheEndpoints:
    """One handler, two paths, and one answer to "no credential": 401.

    Since 2026-09-09 the free server has no anonymous tier at all (Matan:
    "for free MCP - let's make all of them go through oauth in the plain
    /mcp"). `/mcp` is the URL we publish and it challenges; `/mcp/oauth` is
    the same behaviour under the name in old guides and in connectors added
    before the change, so nobody has to re-add a server.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [MCP_PATH, MCP_OAUTH_PATH])
    async def test_a_credential_less_call_is_challenged(self, path):
        sent = await _drive(OAuthResourceGate(_echo({}), _support()), path)
        assert _status(sent) == 401
        header = _headers_of(sent)["www-authenticate"]
        assert 'resource_metadata="' in header
        # The path-scoped document for the path that was challenged: a client
        # follows this URL literally.
        assert header.endswith(f'oauth-protected-resource{path}"')

    @pytest.mark.asyncio
    async def test_the_401_body_carries_the_plain_directions(self):
        """The header is what a client acts on. The body is the only thing a
        script, a log or a person running curl will ever read."""
        sent = await _drive(OAuthResourceGate(_echo({}), _support()), MCP_PATH)
        body = _body_of(sent)
        assert body["jsonrpc"] == "2.0"
        assert body["id"] is None
        assert body["error"]["message"] == signin_directions()
        assert "Sign in with Google" in body["error"]["message"]
        assert "free-trial.flightpowers.com/mcp" in body["error"]["message"]
        assert "flights.flightpowers.com/mcp" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_a_get_gets_the_oauth_error_object_not_json_rpc(self):
        """A GET probe is not a JSON-RPC call, and answering one with a
        JSON-RPC envelope would be a shape nothing asked for."""
        sent = await _drive(
            OAuthResourceGate(_echo({}), _support()), MCP_PATH, method="GET"
        )
        body = _body_of(sent)
        assert body["error"] == "invalid_request"
        assert body["error_description"] == signin_directions()

    @pytest.mark.asyncio
    async def test_an_unknown_bearer_is_refused_on_both_paths(self):
        for path in (MCP_PATH, MCP_OAUTH_PATH):
            sent = await _drive(
                OAuthResourceGate(_echo({}), _support()),
                path,
                {"authorization": "Bearer fpo_nope"},
            )
            assert _status(sent) == 401, path
            assert "invalid_token" in _headers_of(sent)["www-authenticate"]

    @pytest.mark.asyncio
    async def test_a_valid_token_reaches_the_tools_from_either_path(self):
        """A user who signed in through the printed alias must not be
        second-class on the primary path, or the other way round."""
        support = _support()
        from src.oauth import ACCESS_TOKEN_PREFIX, mint
        from src.oauthstore import TokenRecord, hash_secret

        token = mint(ACCESS_TOKEN_PREFIX)
        await support.store.put_token(
            TokenRecord(
                token_hash=hash_secret(token),
                kind="access",
                client_id="fpcl_x",
                user_sub="google-sub-1",
                provider="google",
                scope="flightpowers:free-search",
                resource=f"{ORIGIN}{MCP_PATH}",
                expires_at=time.time() + 600,
                user_email="a@example.com",
            )
        )
        for path in (MCP_PATH, MCP_OAUTH_PATH):
            seen: dict = {}
            sent = await _drive(
                OAuthResourceGate(_echo(seen), support),
                path,
                {"authorization": f"Bearer {token}"},
            )
            assert _status(sent) == 200, path
            # Rewritten to the real route, so it is the same tool registry
            # and the same sponsored card, not a second copy.
            assert seen["path"] == MCP_PATH
            assert seen["headers"][SIGNED_IN_HEADER] == "google-sub-1"
            assert seen["headers"]["x-fp-oauth-email"] == "a@example.com"

    @pytest.mark.asyncio
    async def test_no_signin_configured_leaves_mcp_open(self):
        """The other rollback, and the state of every deployment until ops
        sets the three variables: `/mcp` behaves exactly as it did before
        this feature existed, and `/mcp/oauth` is a 404."""
        gate = OAuthResourceGate(_echo({}), None)
        assert _status(await _drive(gate, MCP_PATH)) == 200
        assert _status(await _drive(gate, MCP_OAUTH_PATH)) == 404


class TestTheForgedHeader:
    """The injected identity is the entire basis for the larger allowance."""

    def test_every_identity_header_is_stripped_on_the_way_in(self):
        scope = {
            "type": "http",
            "headers": [
                (name.encode(), b"attacker") for name in IDENTITY_HEADERS
            ]
            + [(b"user-agent", b"curl/8")],
        }
        cleaned = strip_identity_headers(scope)
        names = {k.decode().lower() for k, _ in cleaned["headers"]}
        assert names == {"user-agent"}

    @pytest.mark.asyncio
    async def test_a_caller_cannot_hand_themselves_an_account(self):
        """The strip happens before the challenge, so a forged header does
        not even buy an anonymous caller a 200 -- they still get the 401."""
        sent = await _drive(
            OAuthResourceGate(_echo({}), _support()),
            MCP_PATH,
            {SIGNED_IN_HEADER: "somebody-elses-sub"},
        )
        assert _status(sent) == 401


# ── who is counted against what ──────────────────────────────────────────


class TestPerUserCaps:
    def test_a_signed_in_key_is_the_account_and_nothing_else(self):
        """Not the address, not the user agent, not the session. That is what
        makes the allowance follow the person rather than the connection."""
        one = identify({SIGNED_IN_HEADER: "sub-1", **DIRECT_HEADERS})
        again = identify(
            {
                SIGNED_IN_HEADER: "sub-1",
                "x-forwarded-for": "203.0.113.9",
                "user-agent": "Cursor/1.2",
                "mcp-session-id": "another-session",
            }
        )
        assert one.kind == KIND_SIGNED_IN
        assert one.key == again.key

    def test_two_accounts_never_share_a_counter(self):
        first = identify({SIGNED_IN_HEADER: "sub-1", **DIRECT_HEADERS})
        second = identify({SIGNED_IN_HEADER: "sub-2", **DIRECT_HEADERS})
        assert first.key != second.key

    def test_a_signed_in_caller_behind_a_gateway_is_still_their_own(self):
        """The case sign-in exists for: everyone behind claude.ai arrives with
        the same address, and before this they shared one counter."""
        pooled = identify(POOLED_HEADERS, gateway_networks=())
        signed = identify({SIGNED_IN_HEADER: "sub-1", **POOLED_HEADERS})
        assert signed.kind == KIND_SIGNED_IN
        assert signed.key != pooled.key

    def test_the_signed_in_tier_gets_the_real_allowance(self):
        assert caps_for(
            KIND_SIGNED_IN,
            day_cap=150,
            month_cap=2000,
            anon_day_cap=10,
            anon_month_cap=0,
        ) == (150, 2000)

    def test_the_anonymous_tier_gets_the_taster(self):
        assert caps_for(
            KIND_DIRECT,
            day_cap=150,
            month_cap=2000,
            anon_day_cap=10,
            anon_month_cap=0,
        ) == (10, 0)

    def test_a_pooled_gateway_keeps_its_own_pair(self):
        """Smithery calls us with no credential of its users', so there is
        nobody on that connection who could sign in. Starving all of them to
        push one of them at a button they cannot press is not a growth move."""
        assert caps_for(
            KIND_GATEWAY_POOLED,
            day_cap=150,
            month_cap=2000,
            gateway_day_cap=1500,
            gateway_month_cap=10000,
            anon_day_cap=10,
            anon_month_cap=0,
        ) == (1500, 10000)

    def test_the_defaults_ship_as_ten_a_day(self, monkeypatch):
        monkeypatch.delenv("FREE_ANON_DAILY_CAP", raising=False)
        monkeypatch.delenv("FREE_ANON_MONTHLY_CAP", raising=False)
        settings = load_settings()
        assert settings.anon_day_cap == 10
        assert settings.anon_month_cap == 0
        assert settings.fair_use_day_cap == 150
        assert settings.fair_use_month_cap == 2000
        assert settings.fair_use_gateway_day_cap == 1500


# ── the words ────────────────────────────────────────────────────────────


class TestTheExactDirections:
    def test_it_names_the_number_the_action_and_the_fallback(self):
        text = anon_directions(10, 150, 2000)
        assert "10 a day" in text
        assert "150 a day and 2,000 a month" in text
        assert "Sign in with Google" in text
        assert "https://free-trial.flightpowers.com/mcp/oauth" in text
        assert "https://flights.flightpowers.com/mcp" in text
        assert "https://hotels.flightpowers.com/mcp" in text

    def test_the_pooled_version_does_not_talk_about_their_client(self):
        """A pooled caller is behind a gateway we are not talking to, so
        "your client should offer a Sign in button" is advice about
        somebody else's software."""
        text = anon_directions(1500, 150, 2000, pooled=True)
        assert "shared connection" in text
        assert "add https://free-trial.flightpowers.com/mcp" in text
        assert "Sign in button" not in text

    def test_the_refusal_carries_them(self):
        state = FairUseState(
            key="k",
            used_today=10,
            used_month=10,
            day_cap=10,
            month_cap=0,
            kind=KIND_DIRECT,
            signed_in_day_cap=150,
            signed_in_month_cap=2000,
        )
        payload = rate_limited_result(state)
        assert payload["search_status"] == "rate_limited"
        assert payload["retry"] is False
        assert "Sign in with Google" in payload["message"]
        assert payload["fair_use"]["signed_in"] is False
        assert "what_to_do" in payload["fair_use"]
        assert payload["upgrade"]["sign_in_free_url"].endswith("/mcp/oauth")

    def test_a_signed_in_refusal_does_not_tell_them_to_sign_in(self):
        state = FairUseState(
            key="k",
            used_today=150,
            used_month=400,
            day_cap=150,
            month_cap=2000,
            kind=KIND_SIGNED_IN,
        )
        payload = rate_limited_result(state)
        assert "Sign in with Google" not in payload["message"]
        assert "sign_in_free" not in payload["upgrade"]
        assert payload["fair_use"]["signed_in"] is True

    def test_the_tool_text_leads_with_the_sign_in(self):
        """What ships: free, sign in with Google, your own allowance,
        ad-supported. No anonymous tier is described, because there is
        none to describe."""
        tail = upgrade_tail(150, 2000)
        assert "free and ad-supported" in tail
        assert "sign in with Google" in tail
        assert "150 backend searches a day and 2,000 a calendar month" in tail
        assert "has not signed in" not in tail

    def test_the_rollback_tier_is_described_only_when_it_exists(self):
        """Under `FREE_ANON_MODE=open` there is an anonymous allowance again,
        and a model that is not told about it discovers it by hitting it."""
        tail = upgrade_tail(150, 2000, 10)
        assert "has not signed in gets 10 searches a day" in tail


class TestTheModeSwitch:
    def test_it_defaults_to_challenge(self, monkeypatch):
        monkeypatch.delenv("FREE_ANON_MODE", raising=False)
        assert anon_mode() == "challenge"

    def test_a_typo_fails_towards_sign_in(self, monkeypatch):
        """The safe direction now that sign-in is the product: a misspelling
        costs a sign-in prompt, not an anonymous free-for-all."""
        monkeypatch.setenv("FREE_ANON_MODE", "opne")
        assert anon_mode() == "challenge"

    def test_open_is_the_rollback_and_is_opt_in(self, monkeypatch):
        monkeypatch.setenv("FREE_ANON_MODE", "open")
        assert anon_mode() == "open"

    @pytest.mark.asyncio
    async def test_open_serves_a_credential_less_caller_again(self, monkeypatch):
        """One env var, no deploy: `/mcp` answers anonymously again, capped
        by `anon_gate.AnonCapMiddleware` at FREE_ANON_DAILY_CAP."""
        monkeypatch.setenv("FREE_ANON_MODE", "open")
        seen: dict = {}
        sent = await _drive(OAuthResourceGate(_echo(seen), _support()), MCP_PATH)
        assert _status(sent) == 200
        assert seen["path"] == MCP_PATH

    @pytest.mark.asyncio
    async def test_open_does_not_reopen_the_alias(self, monkeypatch):
        """`/mcp/oauth` means sign-in, whatever the mode. A rollback that
        silently turned the always-challenge URL into an open one would
        strand every client that added it expecting a Sign in button."""
        monkeypatch.setenv("FREE_ANON_MODE", "open")
        sent = await _drive(
            OAuthResourceGate(_echo({}), _support()), MCP_OAUTH_PATH
        )
        assert _status(sent) == 401


# ── over the wire ────────────────────────────────────────────────────────


@pytest.fixture
def wired(monkeypatch):
    """The real app, built the way api/index.py builds it, with sign-in on."""
    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.delenv("FREE_ANON_MODE", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.test/neon")
    monkeypatch.setenv("ADS_ENABLED", "false")
    server = build_server(load_settings())
    inner = server.http_app(
        stateless_http=True,
        middleware=list(hard_limit_middleware(server))
        + list(anon_cap_middleware(server)),
    )
    from starlette.applications import Starlette

    app = Starlette(lifespan=inner.lifespan)
    app.mount("/", OAuthResourceGate(inner, getattr(server, "fp_oauth", None)))
    return server, app


#: What a caller sends when they want something that costs a backend search.
#: The probe of choice for "is this challenged": a `tools/list` is read-only
#: discovery and is served to anybody (src/discovery.py).
A_SEARCH = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "search_oneway_flights",
        "arguments": {
            "from_airport": "TLV",
            "to_airport": "FCO",
            "departure_date": "2026-10-14",
        },
    },
}


async def _post(app, path, headers, peer="198.51.100.7", message=None):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                path,
                json=message or {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    **headers,
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )


def _rpc_result(response):
    """The JSON-RPC `result`, whether the transport answered JSON or SSE."""
    text = response.text.strip()
    if text.startswith("{"):
        return json.loads(text)["result"]
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())["result"]
    raise AssertionError(f"no JSON-RPC payload in {text!r}")


async def _get(app, path, base_url="http://testserver"):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url=base_url
        ) as client:
            return await client.get(path)


def server_oauth(app):
    """The OAuthSupport behind the mounted gate."""
    return app.routes[-1].app.support


class TestOverTheWire:
    @pytest.mark.asyncio
    async def test_the_metadata_is_served_for_both_paths(self, wired):
        _, app = wired
        for suffix in ("", MCP_PATH, MCP_OAUTH_PATH):
            got = await _get(app, f"/.well-known/oauth-protected-resource{suffix}")
            assert got.status_code == 200, suffix
            # RFC 9728 3.3: the document names the resource that was asked
            # about. A client challenged on the alias fetches the
            # `/mcp/oauth` document and compares the two literally, so that
            # one has to say `/mcp/oauth`; the bare well-known path and the
            # `/mcp` one say `/mcp`.
            assert got.json()["resource"].endswith(suffix or MCP_PATH), suffix
        # Still ONE audience: every one of those names is accepted for a
        # token, so discovery on either path yields a token that works on
        # both.
        for suffix in ("", MCP_PATH, MCP_OAUTH_PATH):
            named = (
                await _get(app, f"/.well-known/oauth-protected-resource{suffix}")
            ).json()["resource"]
            assert server_oauth(app).resource_matches(named), named
        body = (await _get(app, "/.well-known/oauth-authorization-server")).json()
        assert body["authorization_endpoint"].endswith("/connect/authorize")
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["client_id_metadata_document_supported"] is True

    @pytest.mark.asyncio
    async def test_a_direct_caller_with_no_credential_is_challenged(self, wired):
        """On the call that would spend something. The handshake in front of
        it is served to anybody -- `TestDiscoveryWithoutCredentials`."""
        _, app = wired
        response = await _post(app, MCP_PATH, DIRECT_HEADERS, message=A_SEARCH)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert "Sign in with Google" in response.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_a_gateway_pool_is_challenged_too(self, wired):
        """Deliberate, and the reason it is safe: Smithery's release probe
        sees the 401 and flips its listing into OAuth mode, so its users go
        through our Google sign-in like everyone else. A gateway that cannot
        do MCP authorization stops working until it can."""
        _, app = wired
        response = await _post(
            app, MCP_PATH, POOLED_HEADERS, peer="160.79.104.7", message=A_SEARCH
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_scanner_with_no_credentials_sees_the_whole_menu(self, wired):
        """Glama re-checks every connector HOURLY by opening an MCP
        connection and listing its tools, with no credentials, and marks the
        listing unhealthy on a 401 -- which is what happened to us on
        2026-09-09 (its mail to Matan that evening). Smithery's release scan,
        mcpservers.org and M8ven probe the same way.

        Not just a 200: the reviewable schema. A directory that gets tools
        with no `title` and no annotations ranks the listing down for a
        different reason (Anthropic Directory Policy 5.E).
        """
        _, app = wired
        response = await _post(app, MCP_PATH, DIRECT_HEADERS)
        assert response.status_code == 200, response.text
        assert "www-authenticate" not in response.headers
        tools = _rpc_result(response)["tools"]
        assert {t["name"] for t in tools} >= {
            "search_oneway_flights",
            "search_roundtrip_flights",
        }
        for tool in tools:
            assert tool.get("title"), tool["name"]
            annotations = tool.get("annotations") or {}
            assert annotations.get("title"), tool["name"]
            assert annotations.get("readOnlyHint") is True, tool["name"]
            # A live fare is never idempotent: a host that cached one would
            # quote a stale price to somebody about to book.
            assert annotations.get("idempotentHint") is not True, tool["name"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method",
        [
            "initialize",
            "notifications/initialized",
            "ping",
            "prompts/list",
            "resources/list",
            "resources/templates/list",
        ],
    )
    async def test_the_rest_of_the_read_only_handshake_is_served(self, wired, method):
        _, app = wired
        response = await _post(
            app,
            MCP_PATH,
            DIRECT_HEADERS,
            message={"jsonrpc": "2.0", "id": 1, "method": method},
        )
        assert response.status_code in (200, 202), response.text

    @pytest.mark.asyncio
    async def test_a_batch_hiding_a_tool_call_is_challenged(self, wired):
        """A batch is one HTTP response, so it is served whole or refused
        whole -- and a `tools/call` behind a `tools/list` must not be the way
        through."""
        _, app = wired
        response = await _post(
            app,
            MCP_PATH,
            DIRECT_HEADERS,
            message=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, A_SEARCH],
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_the_alias_challenges_discovery_too(self, wired):
        """`/mcp/oauth` is for a client whose auth mode is fixed when the
        server is ADDED, and for a directory that wants a server which always
        requires auth. The opening does not apply there."""
        _, app = wired
        response = await _post(app, MCP_OAUTH_PATH, DIRECT_HEADERS)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_health_reports_whether_sign_in_is_wired(self, wired):
        """The cheap form: configuration only, no store probe.

        `?stores=0` is what a liveness check uses. The probing form is
        `TestHealthProbesTheStores` in tests/test_free_followups.py, which is
        also where the 2026-09-09 "green while sign-in is dead" case lives.
        """
        _, app = wired
        body = (await _get(app, "/health?stores=0")).json()
        assert body["status"] == "ok"
        assert body["signin_enabled"] is True
        assert body["signin_endpoint"].endswith(MCP_PATH)
        assert body["anon_mode"] == "challenge"

    @pytest.mark.asyncio
    async def test_the_connect_page_states_the_email_before_the_button(self, wired):
        _, app = wired
        # On the canonical origin: an alias hostname is bounced there first,
        # because the session cookie is per host and Google compares
        # `redirect_uri` literally.
        page = await _get(app, "/connect", base_url="https://mcp.test.invalid")
        assert page.status_code == 200
        # A list built from a consent screen that did not say so is the kind
        # of list that produces spam complaints instead of customers.
        assert "product updates" in page.text
        assert "unsubscribe link" in page.text
        assert "/connect/start" in page.text

    @pytest.mark.asyncio
    async def test_the_unsubscribe_route_needs_no_session(self, wired):
        """One click, per RFC 8058, which is what the List-Unsubscribe-Post
        header on every send promises."""
        _, app = wired
        page = await _get(app, "/email/unsubscribe?t=whatever")
        assert page.status_code == 200
        assert "Unsubscribed" in page.text

    @pytest.mark.asyncio
    async def test_the_tool_text_a_client_reads_describes_the_sign_in(self, wired):
        server, _ = wired
        assert "sign in with Google" in (server.instructions or "")
        assert "150 backend searches a day" in (server.instructions or "")
        # And does NOT describe an anonymous allowance nobody can reach.
        assert "has not signed in" not in (server.instructions or "")


# ── the list ─────────────────────────────────────────────────────────────


class TestTheUserRow:
    @pytest.mark.asyncio
    async def test_the_first_sign_in_is_the_one_that_reports_new(self):
        """The whole hand-off hangs off this boolean: it is what makes the
        welcome note fire once per account rather than once per sign-in."""
        store = MemoryFreeUserStore()
        assert await store.upsert("sub-1", "a@example.com") is True
        assert await store.upsert("sub-1", "a@example.com") is False

    @pytest.mark.asyncio
    async def test_an_empty_address_never_overwrites_a_good_one(self):
        """An unverified Google email arrives as "" (webauth.finish)."""
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        await store.upsert("sub-1", "")
        assert (await store.get("sub-1")).email == "a@example.com"

    @pytest.mark.asyncio
    async def test_every_row_gets_an_unguessable_unsubscribe_token(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        await store.upsert("sub-2", "b@example.com")
        first = (await store.get("sub-1")).unsubscribe_token
        second = (await store.get("sub-2")).unsubscribe_token
        assert first and second and first != second
        assert len(first) > 20

    @pytest.mark.asyncio
    async def test_usage_lands_on_the_account_and_on_the_day(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        now = time.time()
        await store.touch("sub-1", 3, now)
        await store.touch("sub-1", 2, now)
        row = await store.get("sub-1")
        assert row.call_count == 5
        assert store.days[("sub-1", utc_day(now))] == 5

    @pytest.mark.asyncio
    async def test_delete_takes_the_row_and_the_days(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        await store.touch("sub-1", 1)
        assert await store.delete("sub-1") is True
        assert await store.get("sub-1") is None
        assert store.days == {}

    @pytest.mark.asyncio
    async def test_the_unsubscribe_token_opts_the_row_out(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        token = (await store.get("sub-1")).unsubscribe_token
        assert await store.opt_out(token) is True
        assert (await store.get("sub-1")).email_opt_out is True
        assert await store.opt_out("not-a-token") is False

    @pytest.mark.asyncio
    async def test_the_welcome_guard_fires_once(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        assert await store.mark_welcome_sent("sub-1") is True
        assert await store.mark_welcome_sent("sub-1") is False

    @pytest.mark.asyncio
    async def test_an_unconfigured_deployment_records_nothing_and_raises_nothing(
        self,
    ):
        store = NullFreeUserStore()
        assert await store.upsert("sub-1", "a@example.com") is False
        assert await store.get("sub-1") is None
        await store.touch("sub-1", 1)


class TestTheMailingList:
    """The rules that cannot be got wrong, because they cannot be undone."""

    def _client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_an_unsubscribed_address_is_never_re_added(self):
        """`POST /contacts` UPSERTS and resets `unsubscribed` to false, so a
        blind POST silently re-subscribes everyone who ever opted out."""
        calls: list[str] = []

        def handler(request):
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(200, json={"unsubscribed": True})
            return httpx.Response(201, json={"id": "should-not-happen"})

        config = MailConfig(api_key="k", audience_id="aud")
        async with self._client(handler) as client:
            state = await sync_contact("gone@example.com", config, client=client)
        assert state == RESEND_SKIPPED
        assert not any(call.startswith("POST") for call in calls)

    @pytest.mark.asyncio
    async def test_an_address_already_there_is_not_refreshed(self):
        calls: list[str] = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"unsubscribed": False})

        config = MailConfig(api_key="k", audience_id="aud")
        async with self._client(handler) as client:
            await sync_contact("there@example.com", config, client=client)
        assert calls == ["GET"]

    @pytest.mark.asyncio
    async def test_a_new_address_is_posted_once(self):
        calls: list[str] = []

        def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(404, json={})
            return httpx.Response(201, json={"id": "c_1"})

        config = MailConfig(api_key="k", audience_id="aud")
        async with self._client(handler) as client:
            state = await sync_contact("new@example.com", config, client=client)
        assert calls == ["GET", "POST"]
        assert state == "added"

    @pytest.mark.asyncio
    async def test_no_audience_configured_means_no_request_at_all(self):
        """Phase 1. Resend's plan caps this account at three audiences and
        all three are taken, so the shipped state writes nothing to Resend."""

        def handler(request):  # pragma: no cover - must never be called
            raise AssertionError("Resend was called with no audience configured")

        config = MailConfig(api_key="k", audience_id="")
        async with self._client(handler) as client:
            assert await sync_contact("a@example.com", config, client=client) == (
                RESEND_SKIPPED
            )

    @pytest.mark.asyncio
    async def test_the_welcome_is_off_unless_someone_turns_it_on(self):
        def handler(request):  # pragma: no cover - must never be called
            raise AssertionError("a welcome went out with the flag off")

        config = MailConfig(api_key="k", welcome=False, unsubscribe_base=ORIGIN)
        async with self._client(handler) as client:
            assert (
                await send_welcome(
                    "a@example.com", "tok", config, day_cap=150, month_cap=2000,
                    client=client,
                )
                is False
            )

    @pytest.mark.asyncio
    async def test_the_welcome_carries_our_own_one_click_unsubscribe(self):
        """`{{{RESEND_UNSUBSCRIBE_URL}}}` only renders inside a broadcast; in
        a transactional send it comes out literally."""
        seen: dict = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "e_1"})

        config = MailConfig(api_key="k", welcome=True, unsubscribe_base=ORIGIN)
        async with self._client(handler) as client:
            assert (
                await send_welcome(
                    "a@example.com", "tok", config, day_cap=150, month_cap=2000,
                    client=client,
                )
                is True
            )
        body = seen["body"]
        link = unsubscribe_url(config, "tok")
        assert link in body["text"]
        assert body["headers"]["List-Unsubscribe"] == f"<{link}>"
        assert body["headers"]["List-Unsubscribe-Post"] == (
            "List-Unsubscribe=One-Click"
        )
        assert "RESEND_UNSUBSCRIBE_URL" not in body["text"]
        assert "app@flightpowers.com" in body["from"]
        assert "150" in body["subject"]

    @pytest.mark.asyncio
    async def test_an_opted_out_row_gets_no_welcome(self):
        def handler(request):  # pragma: no cover - must never be called
            raise AssertionError("a welcome went to an opted-out address")

        store = MemoryFreeUserStore()
        await store.upsert("sub-1", "a@example.com")
        token = (await store.get("sub-1")).unsubscribe_token
        await store.opt_out(token)
        config = MailConfig(api_key="k", welcome=True, unsubscribe_base=ORIGIN)
        await remember_signin(store, "sub-1", "a@example.com", token, config)
        assert (await store.get("sub-1")).welcome_sent_at is None

    @pytest.mark.asyncio
    async def test_the_hand_off_never_raises(self):
        """A mailing list is not a reason to refuse somebody a login."""

        class Exploding:
            async def get(self, sub):
                raise RuntimeError("neon is down")

            async def set_resend_state(self, sub, state):
                raise RuntimeError("still down")

        config = MailConfig(api_key="k", welcome=False)
        assert await remember_signin(
            Exploding(), "sub-1", "a@example.com", "tok", config
        ) == RESEND_SKIPPED
