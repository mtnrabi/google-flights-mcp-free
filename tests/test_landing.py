"""
The landing page: a person's browser gets a page, every client gets what it
got before, byte for byte.

WHY THIS FILE EXISTS
--------------------
The URL in every Reddit post, `https://free-trial.flightpowers.com/mcp`,
answered a 404 on `/` and a 401 JSON on `/mcp` to a reader's browser
(Matan, 2026-09-28, after clicking it from a post). The fix is one ASGI
layer above the OAuth gate (src/landing.py). The danger in that fix is the
other direction: an MCP client, a directory probe or Smithery's release scan
being answered HTML where it was owed the 401 + OAuth metadata that makes a
client show its Sign in button. So half of this file asserts the page and
the other half drives the same requests through the gate with and without
the layer and compares the answers.

    python -m pytest mcp_server/tests/test_landing.py -q
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette

from src.anon_gate import anon_cap_middleware
from src.hard_limit import hard_limit_middleware
from src.landing import LandingPage, is_browser_navigation, landing_html
from src.oauth import OAuthResourceGate
from src.server import build_server
from src.settings import load_settings

NEW = "https://free-trial.flightpowers.com"
OLD = "https://google-flights-lulu.flightpowers.com"
STRANGER = "https://mcp-lulu-git-preview.vercel.app"

#: What Chrome sends when a person opens a URL.
BROWSER = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0",
}
#: The streamable-HTTP transport's own GET (the SSE stream).
CLIENT_SSE = {"accept": "text/event-stream"}
#: A script or a probe asking for JSON.
CLIENT_JSON = {"accept": "application/json"}
#: A bare curl.
CURL = {"accept": "*/*", "user-agent": "curl/8.5.0"}
#: The transport's POST headers.
CLIENT_POST = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "landing-test", "version": "1.0"},
    },
}
A_SEARCH = {
    "jsonrpc": "2.0",
    "id": 2,
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


@pytest.fixture
def stack(monkeypatch):
    """The real app the way api/index.py builds it, sign-in on, two hosts.

    Returns the outer Starlette app (carrying the lifespan), the bare gate
    and the gate wrapped in the landing layer, so a test can drive the same
    request through both and compare.
    """
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
    monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", f"{OLD}/mcp")
    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.delenv("FREE_ANON_MODE", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.test/neon")
    monkeypatch.setenv("ADS_ENABLED", "false")
    settings = load_settings()
    server = build_server(settings)
    inner = server.http_app(
        stateless_http=True,
        middleware=list(hard_limit_middleware(server))
        + list(anon_cap_middleware(server)),
    )
    gate = OAuthResourceGate(inner, getattr(server, "fp_oauth", None))
    assert gate.support is not None, "sign-in must be configured for this suite"
    landing = LandingPage(gate, settings)
    app = Starlette(lifespan=inner.lifespan)
    app.mount("/", landing)
    return app, gate, landing


def _client(asgi, base_url: str = NEW) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
        base_url=base_url,
    )


def _box(url: str) -> str:
    """The copy box carrying `url`: a readonly textarea, so the whole URL
    is visible on a phone."""
    return f'aria-label="MCP connector URL">{url}</textarea>'


def _is_html(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("text/html")


# ── the page ─────────────────────────────────────────────────────────────


class TestThePage:
    async def test_root_serves_the_page_to_anyone(self, stack):
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                for headers in (BROWSER, CURL):
                    response = await client.get("/", headers=headers)
                    assert response.status_code == 200, response.text
                    assert _is_html(response)
                    assert _box(f"{NEW}/mcp") in response.text
                    assert "<title>FlightPowers free MCP</title>" in response.text

    async def test_head_root_answers_with_headers_and_no_body(self, stack):
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.head("/", headers=BROWSER)
        assert response.status_code == 200
        assert _is_html(response)
        assert response.content == b""

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/oauth"])
    async def test_a_browser_on_the_connector_url_gets_the_page(self, stack, path):
        """The click from a Reddit post. The same request from an MCP client
        is the 401 -- see TestClientsAreUntouched."""
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.get(path, headers=BROWSER)
        assert response.status_code == 200, response.text
        assert _is_html(response)
        assert _box(f"{NEW}/mcp") in response.text
        # The same path answers JSON to a client, so caches must key on Accept.
        assert response.headers.get("vary") == "Accept"
        assert response.headers.get("cache-control") == "no-store"

    async def test_the_copy_box_names_the_host_the_reader_arrived_on(self, stack):
        """Canonical host -> canonical URL; the alias host -> the alias URL
        plus a line teaching the canonical one; a host we do not publish (a
        preview) -> canonical, never itself."""
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app, NEW) as client:
                on_new = (await client.get("/", headers=BROWSER)).text
            async with _client(app, OLD) as client:
                on_old = (await client.get("/mcp", headers=BROWSER)).text
            async with _client(app, STRANGER) as client:
                on_stranger = (await client.get("/", headers=BROWSER)).text

        assert _box(f"{NEW}/mcp") in on_new
        assert "older hostname" not in on_new

        assert _box(f"{OLD}/mcp") in on_old
        assert "older hostname" in on_old
        assert f"{NEW}/mcp" in on_old, "an alias reader is told the canonical URL"

        assert _box(f"{NEW}/mcp") in on_stranger
        assert STRANGER not in on_stranger, "a preview must never advertise itself"

    def test_the_page_is_plain_and_carries_only_published_facts(self):
        html = landing_html(f"{NEW}/mcp", canonical_url=f"{NEW}/mcp")
        # No JavaScript, no tracking, nothing fetched from anywhere.
        for tag in ("<script", "<img", "<link", "<iframe"):
            assert tag not in html
        # No em dash (outward copy rule), no invented numbers: every figure
        # below is the one in the server's own instructions and listings.
        assert "—" not in html
        for fact in (
            "50 searches a day",
            "250 a month",
            "$10 key",
            "2,500 searches",
            "https://rapidapi.com/mtnrabi/api/google-flights-live-api",
            "https://rapidapi.com/mtnrabi/api/booking-live-api",
            "https://flights.flightpowers.com/mcp",
            "https://hotels.flightpowers.com/mcp",
            'href="https://flightpowers.com"',
            "Add custom connector",
            "Sign in with Google",
            "claude mcp add --transport http",
            "mcpServers",
        ):
            assert fact in html, fact
        assert '<meta name="viewport"' in html, "mobile-friendly"

    def test_the_alias_note_only_appears_on_an_alias(self):
        assert "older hostname" not in landing_html(
            f"{NEW}/mcp", canonical_url=f"{NEW}/mcp"
        )
        assert "older hostname" in landing_html(
            f"{OLD}/mcp", canonical_url=f"{NEW}/mcp"
        )


# ── who counts as a browser ──────────────────────────────────────────────


def _scope(method: str = "GET", **headers: str) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


class TestIsBrowserNavigation:
    def test_a_browser_get_is(self):
        assert is_browser_navigation(_scope(**BROWSER))
        assert is_browser_navigation(_scope("HEAD", **BROWSER))

    @pytest.mark.parametrize(
        "headers",
        [
            CLIENT_SSE,
            CLIENT_JSON,
            CURL,
            {},
            {"accept": "text/html, text/event-stream"},
            {"accept": "text/html", "mcp-session-id": "abc"},
            {"accept": "text/html", "mcp-protocol-version": "2025-06-18"},
            {"accept": "text/html", "authorization": "Bearer fpo_x"},
        ],
    )
    def test_anything_a_client_sends_is_not(self, headers):
        assert not is_browser_navigation(_scope(**headers))

    def test_a_post_never_is(self):
        assert not is_browser_navigation(_scope("POST", **BROWSER))

    def test_only_http_scopes(self):
        assert not is_browser_navigation({"type": "lifespan"})


# ── every client answer is unchanged ─────────────────────────────────────


#: (method, path, headers, json body) for every request shape a client, a
#: probe or a script sends to the connector URL.
CLIENT_REQUESTS = [
    ("GET", "/mcp", CLIENT_SSE, None),
    ("GET", "/mcp", CLIENT_JSON, None),
    ("GET", "/mcp", CURL, None),
    ("GET", "/mcp", {}, None),
    ("GET", "/mcp", {**BROWSER, "mcp-session-id": "abc"}, None),
    ("GET", "/mcp", {**BROWSER, "authorization": "Bearer fpo_unknown"}, None),
    ("GET", "/mcp/oauth", CLIENT_SSE, None),
    ("GET", "/mcp/oauth", CURL, None),
    ("POST", "/mcp", CLIENT_POST, INITIALIZE),
    ("POST", "/mcp", CLIENT_POST, A_SEARCH),
    ("POST", "/mcp", BROWSER, INITIALIZE),
    ("GET", "/.well-known/oauth-protected-resource/mcp", BROWSER, None),
    ("GET", "/health?stores=0", BROWSER, None),
]


class TestClientsAreUntouched:
    @pytest.mark.parametrize("method,path,headers,body", CLIENT_REQUESTS)
    async def test_the_same_request_gets_the_same_answer_with_and_without_the_layer(
        self, stack, method, path, headers, body
    ):
        """Status, content type, the OAuth challenge header and the body,
        compared between the bare gate and the gate under the landing layer.
        Not one byte of any of these may change."""
        app, gate, landing = stack
        async with app.router.lifespan_context(app):
            async with _client(gate) as bare, _client(landing) as wrapped:
                a = await bare.request(method, path, headers=headers, json=body)
                b = await wrapped.request(method, path, headers=headers, json=body)
        assert (a.status_code, a.content) == (b.status_code, b.content)
        for name in ("content-type", "www-authenticate"):
            assert a.headers.get(name) == b.headers.get(name), name
        assert not _is_html(b), "a client request was answered HTML"

    async def test_the_credential_less_get_is_still_the_401_with_the_challenge(
        self, stack
    ):
        """The exact contract clients and Smithery's probe rely on."""
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                for headers in (CLIENT_SSE, CLIENT_JSON, CURL):
                    response = await client.get("/mcp", headers=headers)
                    assert response.status_code == 401, headers
                    assert response.headers["content-type"] == "application/json"
                    assert "resource_metadata=" in response.headers["www-authenticate"]
                    assert response.json()["error"] == "invalid_request"

    async def test_initialize_still_answers_through_the_layer(self, stack):
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.post("/mcp", json=INITIALIZE, headers=CLIENT_POST)
        assert response.status_code == 200, response.text
        assert "serverInfo" in response.text

    async def test_a_search_without_a_token_is_still_challenged(self, stack):
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.post("/mcp", json=A_SEARCH, headers=CLIENT_POST)
        assert response.status_code == 401
        payload = json.loads(response.content)
        assert payload["error"]["data"]["reason"] == "sign_in_required"


# ── through the Vercel entrypoint ────────────────────────────────────────


class TestEntrypoint:
    """api/index.py, driven as Vercel drives it. conftest.py's canonical URL
    is `https://mcp.test.invalid/mcp` and `testserver` is not a published
    host, so the page names the canonical URL."""

    async def test_root_and_browser_mcp_serve_the_page(self):
        import api.index as entrypoint

        app = entrypoint.app
        async with app.router.lifespan_context(app):
            async with _client(app, "http://testserver") as client:
                root = await client.get("/", headers=BROWSER)
                mcp = await client.get("/mcp", headers=BROWSER)
                sse = await client.get("/mcp", headers=CLIENT_SSE)
        assert root.status_code == 200 and _is_html(root)
        assert "https://mcp.test.invalid/mcp" in root.text
        assert mcp.status_code == 200 and _is_html(mcp)
        assert not _is_html(sse), "the transport's own GET must not get HTML"
