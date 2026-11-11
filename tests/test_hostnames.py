"""
One deployment, two public hostnames, and everything host-sensitive per host.

WHY THIS FILE EXISTS
--------------------
On 2026-09-15 the free server's public hostname changed from
`google-flights-lulu.flightpowers.com` to `free-trial.flightpowers.com`
(Matan: "keep the old one for old users, but update everywhere for the new
one"). An MCP connector URL is saved inside the client and is never re-typed,
so the six accounts that signed in on the old hostname -- and the Smithery
listing, and every Claude Code config -- have to keep working on it forever
while everything new advertises the new one.

That makes four values PER REQUEST rather than per deployment, because each
is compared byte-for-byte against the host the client used:

1. the Lulu widget's `_meta.ui.domain`, which claude.ai recomputes as
   `sha256(<connector URL>)` and refuses the frame on a mismatch (the
   2026-09-15 incident: every search drew "Unable to reach FlightPowers" and
   CPM was structurally zero);
2. the OAuth issuer and its authorization/token/registration endpoints;
3. the RFC 9728 `resource_metadata` URL in the 401 challenge, which a client
   follows literally;
4. the Google `redirect_uri`, which Google compares literally.

And one value that must NOT move: every sentence a PERSON reads keeps naming
the canonical new hostname, so an old user learns it instead of having it
kept from them.

    python -m pytest mcp_server/tests/test_hostnames.py -q
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from src.freeusers import MemoryFreeUserStore
from src.maillist import MailConfig
from src.oauth import MCP_OAUTH_PATH, MCP_PATH, OAuthResourceGate, OAuthSupport
from src.oauthstore import MemoryOAuthStore
from src.server import build_server
from src.settings import load_settings
from src.webauth import GoogleWebAuth, derive_secret, signing_master
from src.widget_domain import HostWidgetDomainMiddleware

NEW = "https://free-trial.flightpowers.com"
OLD = "https://google-flights-lulu.flightpowers.com"
STRANGER = "https://mcp-lulu-git-preview.vercel.app"

NEW_HOST = "free-trial.flightpowers.com"
OLD_HOST = "google-flights-lulu.flightpowers.com"


def _two_hostnames(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
    monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", f"{OLD}/mcp")


def _expected_domain(connector_url: str) -> str:
    """The hash claude.ai computes, written out rather than imported.

    Deliberately not `lulu_ads.widget.claude_apps_domain`: this states the
    external contract, and a test that borrows the implementation can only
    ever agree with it.
    """
    return (
        hashlib.sha256(connector_url.encode()).hexdigest()[:32]
        + ".claudemcpcontent.com"
    )


# ── parsing the alias list ───────────────────────────────────────────────


class TestAliasParsing:
    """`MCP_PUBLIC_URL_ALIASES` is corrected the same way #487 corrects the
    canonical value. A bare origin or a trailing slash there would hash to a
    different domain, which is the exact silent failure #487 exists for."""

    def test_a_bare_alias_origin_is_given_the_mcp_path(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
        monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", OLD)
        assert load_settings().public_url_aliases == (f"{OLD}/mcp",)

    def test_a_trailing_slash_on_an_alias_is_dropped(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
        monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", f"{OLD}/mcp/")
        assert load_settings().public_url_aliases == (f"{OLD}/mcp",)

    def test_several_aliases_and_stray_whitespace(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
        monkeypatch.setenv(
            "MCP_PUBLIC_URL_ALIASES",
            f" {OLD}/mcp , https://lulu.flightpowers.com/mcp ,",
        )
        assert load_settings().public_url_aliases == (
            f"{OLD}/mcp",
            "https://lulu.flightpowers.com/mcp",
        )

    def test_the_canonical_host_is_not_repeated_as_an_alias(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
        monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", f"{NEW}/mcp,{OLD}/mcp")
        settings = load_settings()
        assert settings.public_url_aliases == (f"{OLD}/mcp",)
        assert settings.public_hosts == (NEW_HOST, OLD_HOST)

    def test_no_aliases_is_the_normal_state(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", f"{NEW}/mcp")
        monkeypatch.delenv("MCP_PUBLIC_URL_ALIASES", raising=False)
        settings = load_settings()
        assert settings.public_url_aliases == ()
        assert settings.public_url_for_host(OLD_HOST) == f"{NEW}/mcp"


class TestResolvingTheHost:
    def test_each_hostname_resolves_to_its_own_connector_url(self, monkeypatch):
        _two_hostnames(monkeypatch)
        settings = load_settings()
        assert settings.public_url_for_host(NEW_HOST) == f"{NEW}/mcp"
        assert settings.public_url_for_host(OLD_HOST) == f"{OLD}/mcp"

    def test_the_match_is_case_insensitive_and_survives_a_default_port(
        self, monkeypatch
    ):
        """A client may or may not spell out `:443` on an https `Host`."""
        _two_hostnames(monkeypatch)
        settings = load_settings()
        assert settings.public_url_for_host(OLD_HOST.upper()) == f"{OLD}/mcp"
        assert settings.public_url_for_host(f"{OLD_HOST}:443") == f"{OLD}/mcp"

    def test_a_different_port_is_a_different_server(self, monkeypatch):
        """Only the scheme's DEFAULT port is ignored. Stripping every port
        would make a dev server on :9000 answer as the one published on
        :8000, which is a different process."""
        monkeypatch.setenv("MCP_PUBLIC_URL", "http://localhost:8000/mcp")
        monkeypatch.setenv("MCP_PUBLIC_URL_ALIASES", f"{OLD}/mcp")
        settings = load_settings()
        assert settings.public_url_for_host("localhost:8000") == (
            "http://localhost:8000/mcp"
        )
        # Unknown -> canonical, which here IS localhost:8000; so assert on the
        # matcher itself, where "no match" is visible.
        from src.settings import host_matches

        assert host_matches("http://localhost:8000/mcp", "localhost:8000")
        assert not host_matches("http://localhost:8000/mcp", "localhost:9000")
        assert not host_matches("http://localhost:8000/mcp", "localhost")
        assert host_matches("http://localhost/mcp", "localhost:80")
        assert not host_matches(f"{OLD}/mcp", f"{OLD_HOST}:8443")

    def test_an_unknown_host_falls_back_to_the_canonical_url(self, monkeypatch):
        """A preview deployment, a probe with no Host, a hostname somebody
        pointed at us. The canonical answer is what was correct before
        aliases existed, so falling back to it can only be a no-op."""
        _two_hostnames(monkeypatch)
        settings = load_settings()
        assert settings.public_url_for_host("mcp-lulu.vercel.app") == f"{NEW}/mcp"
        assert settings.public_url_for_host("") == f"{NEW}/mcp"


# ── the widget domain ────────────────────────────────────────────────────


class TestWidgetDomainPerHost:
    """claude.ai validates `ui.domain` against the URL THE USER ADDED. A
    single baked-in domain would put one of the two populations back into
    "Unable to reach FlightPowers" with $0 CPM behind it."""

    def test_the_hash_follows_the_host(self, monkeypatch):
        _two_hostnames(monkeypatch)
        middleware = HostWidgetDomainMiddleware(load_settings())
        assert middleware.domain_for_host(NEW_HOST) == _expected_domain(f"{NEW}/mcp")
        assert middleware.domain_for_host(OLD_HOST) == _expected_domain(f"{OLD}/mcp")

    def test_an_unknown_host_gets_the_canonical_hash(self, monkeypatch):
        _two_hostnames(monkeypatch)
        middleware = HostWidgetDomainMiddleware(load_settings())
        assert middleware.domain_for_host("elsewhere.invalid") == _expected_domain(
            f"{NEW}/mcp"
        )

    def test_the_two_hashes_are_not_the_same_string(self, monkeypatch):
        """Guards the test above from passing for the wrong reason."""
        _two_hostnames(monkeypatch)
        middleware = HostWidgetDomainMiddleware(load_settings())
        assert middleware.domain_for_host(NEW_HOST) != middleware.domain_for_host(
            OLD_HOST
        )

    def test_meta_that_is_not_ours_is_left_alone(self, monkeypatch):
        """Only a `*.claudemcpcontent.com` value was produced by
        `claude_apps_domain`. Anything else in the registry is somebody
        else's and is handed back byte-for-byte."""
        _two_hostnames(monkeypatch)
        middleware = HostWidgetDomainMiddleware(load_settings())
        for meta in (
            None,
            {},
            {"ui": {}},
            {"ui": {"domain": "example.com"}},
            {"fastmcp": {"tags": []}},
        ):
            assert middleware._retarget(meta, "x.claudemcpcontent.com") is None

    def _ad_server(self, monkeypatch):
        _two_hostnames(monkeypatch)
        monkeypatch.setenv("ADS_ENABLED", "true")
        monkeypatch.setenv("LULU_ADS_PUBLISHER_ID", "pub_test")
        monkeypatch.setenv("LULU_ADS_API_KEY", "lk_test")
        monkeypatch.setenv("FREE_ANON_MODE", "open")
        return build_server(load_settings()).http_app(stateless_http=True)

    @pytest.mark.asyncio
    async def test_a_resources_list_over_http_carries_that_hosts_domain(
        self, monkeypatch
    ):
        """End to end, through the real transport, because the Host header is
        the input and an in-memory client does not have one."""
        app = self._ad_server(monkeypatch)
        for origin in (NEW, OLD):
            widgets = {
                uri: meta
                for uri, meta in (await _resources_list(app, origin)).items()
                if uri.startswith("ui://lulu-ads/")
            }
            assert widgets, "no sponsored widgets registered -- nothing to check"
            expected = _expected_domain(f"{origin}/mcp")
            for uri, meta in widgets.items():
                assert meta["ui"]["domain"] == expected, (
                    f"{uri} on {origin}: claude.ai validates ui.domain against "
                    f"the connector URL the user added."
                )

    @pytest.mark.asyncio
    async def test_a_resources_read_over_http_carries_that_hosts_domain(
        self, monkeypatch
    ):
        """THE call that matters, and the one that was wrong.

        `resources/read` carries the same `_meta.ui.domain` and it is the
        response claude.ai validates before it draws the frame. fastmcp hands
        `on_read_resource` a `ResourceResult`, not a list; the first version
        of this middleware early-returned on anything that was not a list, so
        `resources/list` was rewritten and the read was not -- an alias
        caller was served the canonical domain and kept the red "Unable to
        reach FlightPowers" chip, which is the whole failure this change
        exists to end.
        """
        app = self._ad_server(monkeypatch)
        for origin in (NEW, OLD):
            uris = [
                uri
                for uri in (await _resources_list(app, origin))
                if uri.startswith("ui://lulu-ads/")
            ]
            assert uris, "no sponsored widgets registered -- nothing to check"
            expected = _expected_domain(f"{origin}/mcp")
            for uri in uris:
                for block in await _resources_read(app, origin, uri):
                    assert block["ui"]["domain"] == expected, (
                        f"{uri} read on {origin}: claude.ai validates "
                        f"ui.domain on the READ, not just the listing."
                    )


async def _resources_list(app, origin: str) -> dict[str, dict]:
    """`resources/list` over HTTP with `Host` set by the base URL."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
    assert response.status_code == 200, response.text
    payload = _payload(response.text)
    return {r["uri"]: (r.get("_meta") or {}) for r in payload["result"]["resources"]}


async def _resources_read(app, origin: str, uri: str) -> list[dict]:
    """The `_meta` of every content block of one `resources/read`."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": uri},
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
    assert response.status_code == 200, response.text
    contents = _payload(response.text)["result"]["contents"]
    assert contents, f"{uri} read back with no content blocks"
    return [block.get("_meta") or {} for block in contents]


def _payload(body: str) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in {body!r}")


# ── the OAuth identity of each hostname ──────────────────────────────────


def _support() -> OAuthSupport:
    master = signing_master("test-client-secret")
    return OAuthSupport(
        store=MemoryOAuthStore(),
        auth=GoogleWebAuth(
            client_id="test-client-id",
            client_secret="test-client-secret",
            redirect_uri=f"{NEW}/connect/callback",
            session_secret=derive_secret(master, b"fp-free-session-v1"),
        ),
        origin=NEW,
        product="travel",
        users=MemoryFreeUserStore(),
        mail=MailConfig(api_key="", unsubscribe_base=NEW),
        origins=(NEW, OLD),
    )


class TestOAuthPerHost:
    def test_the_issuer_and_every_endpoint_follow_the_host(self):
        for origin in (NEW, OLD):
            metadata = _support().for_host(
                origin.split("://", 1)[1]
            ).authorization_server_metadata()
            assert metadata["issuer"] == origin
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
                "revocation_endpoint",
            ):
                assert metadata[key].startswith(origin + "/"), (key, origin)

    def test_the_protected_resource_document_follows_the_host(self):
        document = _support().for_host(OLD_HOST).protected_resource_metadata(MCP_PATH)
        assert document["resource"] == f"{OLD}{MCP_PATH}"
        assert document["authorization_servers"] == [OLD]

    def test_the_google_redirect_uri_follows_the_host(self):
        """Google compares it literally; both are registered in the Cloud
        Console. Sending a user who started on the old hostname back to the
        new one is `redirect_uri_mismatch`."""
        assert _support().for_host(OLD_HOST).auth.redirect_uri == (
            f"{OLD}/connect/callback"
        )
        assert _support().for_host(NEW_HOST).auth.redirect_uri == (
            f"{NEW}/connect/callback"
        )

    def test_the_session_secret_is_shared_so_a_cookie_still_reads(self):
        support = _support()
        assert support.for_host(OLD_HOST).auth.session_secret == (
            support.auth.session_secret
        )

    def test_an_unknown_host_gets_the_canonical_instance(self):
        support = _support()
        assert support.for_host("preview.vercel.app") is support
        assert support.for_host("") is support

    def test_the_variant_is_built_once_and_reused(self):
        support = _support()
        assert support.for_host(OLD_HOST) is support.for_host(OLD_HOST)

    def test_the_variant_knows_every_hostname_too(self):
        """So `resource_matches` gives the same answer whichever host the
        request arrived on."""
        assert _support().for_host(OLD_HOST).known_origins() == (NEW, OLD)

    def test_a_token_named_for_either_hostname_is_accepted(self):
        """The six accounts that signed in before the rename hold tokens whose
        `resource` names the old host. Treating those as a different resource
        would log them out for a rename."""
        support = _support()
        for instance in (support, support.for_host(OLD_HOST)):
            assert instance.resource_matches(f"{OLD}{MCP_PATH}")
            assert instance.resource_matches(f"{NEW}{MCP_PATH}")
            assert instance.resource_matches(f"{OLD}{MCP_OAUTH_PATH}")
            assert not instance.resource_matches(f"{STRANGER}{MCP_PATH}")


class TestTheChallengeFollowsTheHost:
    """A client reads `resource_metadata` out of the 401 and fetches it
    literally. Pointing it at the other hostname sends it off to an
    authorization server it never connected to."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin,host", [(NEW, NEW_HOST), (OLD, OLD_HOST)])
    async def test_the_401_names_the_host_that_was_called(self, origin, host):
        sent = await _drive(OAuthResourceGate(_echo(), _support()), MCP_PATH, host)
        assert sent[0]["status"] == 401
        header = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}[
            "www-authenticate"
        ]
        assert f'resource_metadata="{origin}' in header
        assert header.endswith(f'oauth-protected-resource{MCP_PATH}"')

    @pytest.mark.asyncio
    async def test_the_body_still_names_the_canonical_hostname(self):
        """The header is machine-read and is per host. The BODY is the only
        thing a person or a script ever sees, and it teaches the new name."""
        sent = await _drive(OAuthResourceGate(_echo(), _support()), MCP_PATH, OLD_HOST)
        message = json.loads(sent[1]["body"])["error"]["message"]
        assert "free-trial.flightpowers.com/mcp" in message
        assert OLD_HOST not in message

    @pytest.mark.asyncio
    async def test_an_unknown_host_is_challenged_with_the_canonical_one(self):
        sent = await _drive(
            OAuthResourceGate(_echo(), _support()), MCP_PATH, "preview.vercel.app"
        )
        header = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}[
            "www-authenticate"
        ]
        assert f'resource_metadata="{NEW}' in header


def _echo():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


async def _drive(gate, path, host):
    sent: list = []

    async def send(message):
        sent.append(message)

    await gate(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"host", host.encode())],
        },
        None,
        send,
    )
    return sent


# ── /health ──────────────────────────────────────────────────────────────


class TestHealthReportsTheRequestHost:
    """`/health` is how a deploy is checked, and after this change it has to
    be checked once per hostname. Reporting the canonical endpoint twice
    would make the second check meaningless."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [NEW, OLD])
    async def test_it_names_the_endpoint_of_the_host_it_was_asked_on(
        self, origin, monkeypatch
    ):
        _two_hostnames(monkeypatch)
        app = build_server(load_settings()).http_app(stateless_http=True)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url=origin
            ) as client:
                body = (await client.get("/health?stores=0")).json()
        assert body["public_url"] == f"{origin}/mcp"
        if body["signin_enabled"]:
            assert body["signin_endpoint"] == f"{origin}{MCP_PATH}"


# ── what a person reads does not move ────────────────────────────────────


class TestTheCopyNamesTheNewHostname:
    def test_the_directions_and_the_cap_message_teach_free_trial(self):
        from src.fair_use import anon_directions, signin_directions

        for text in (
            signin_directions(),
            anon_directions(10, 150, 2000),
            anon_directions(1500, 150, 2000, pooled=True),
        ):
            assert "free-trial.flightpowers.com" in text
            assert OLD_HOST not in text


class TestTheAnonRollbackChallengeFollowsTheHost:
    """`FREE_ANON_MODE=open` is the rollback path, and it sends its own 401.

    It is the hardest place for a wrong hostname to be noticed -- nobody is
    looking at the rollback until they need it -- so it is pinned here.
    """

    def test_the_gate_asks_the_per_host_support(self):
        from src.anon_gate import AnonCapMiddleware

        support = _support()
        gate = AnonCapMiddleware(
            _echo(),
            settings=load_settings(),
            telemetry=None,
            classifier=None,
            challenge=lambda host="", **_: support.for_host(host).challenge_header(),
        )
        assert NEW in gate._challenge_for(NEW_HOST)
        assert OLD in gate._challenge_for(OLD_HOST)
        assert NEW in gate._challenge_for("preview.vercel.app")

    def test_a_zero_argument_challenge_still_works(self):
        """Nothing in the tree passes one, but several tests build one, and
        the signature is read rather than guessed at from a TypeError."""
        from src.anon_gate import AnonCapMiddleware

        gate = AnonCapMiddleware(
            _echo(),
            settings=load_settings(),
            telemetry=None,
            classifier=None,
            challenge=lambda: 'Bearer resource_metadata="x"',
        )
        assert gate._challenge_for(OLD_HOST) == 'Bearer resource_metadata="x"'

    def test_a_typeerror_from_inside_the_callable_is_not_swallowed(self):
        """This used to be `try: f(host=...) except TypeError: f()`, which
        cannot tell a bad signature from a real bug inside the callable -- so
        it hid the bug AND called it a second time. The signature is read
        once now, so a genuine TypeError propagates and is called once."""
        from src.anon_gate import AnonCapMiddleware

        calls = []

        def broken(host: str = "") -> str:
            calls.append(host)
            raise TypeError("a real bug inside the challenge")

        gate = AnonCapMiddleware(
            _echo(),
            settings=load_settings(),
            telemetry=None,
            classifier=None,
            challenge=broken,
        )
        with pytest.raises(TypeError, match="a real bug"):
            gate._challenge_for(OLD_HOST)
        assert calls == [OLD_HOST]


class TestKnownHostsAreNotRedirectedAway:
    """`_wrong_host` used to send every non-canonical hostname to the
    canonical origin. With two published hostnames that would bounce an old
    connector's user off the host their client is talking to, mid sign-in,
    and hand them a token for an issuer they never asked about. A hostname we
    publish now serves itself; anything else still gets the 302."""

    def _app(self, monkeypatch):
        from fastmcp import FastMCP

        from src.oauthroutes import register_oauth_routes

        _two_hostnames(monkeypatch)
        mcp = FastMCP("hostnames-test")
        register_oauth_routes(mcp, _support(), load_settings())
        return mcp.http_app(stateless_http=True)

    async def _get(self, app, origin, path):
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url=origin, follow_redirects=False
            ) as client:
                return await client.get(path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [NEW, OLD])
    async def test_a_published_hostname_serves_the_connect_page(
        self, origin, monkeypatch
    ):
        response = await self._get(self._app(monkeypatch), origin, "/connect")
        assert response.status_code == 200
        assert "Sign in" in response.text

    @pytest.mark.asyncio
    async def test_an_unpublished_hostname_is_still_sent_home(self, monkeypatch):
        response = await self._get(self._app(monkeypatch), STRANGER, "/connect")
        assert response.status_code == 302
        assert response.headers["location"] == f"{NEW}/connect"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [NEW, OLD])
    async def test_the_authorization_server_metadata_is_that_hosts(
        self, origin, monkeypatch
    ):
        response = await self._get(
            self._app(monkeypatch), origin, "/.well-known/oauth-authorization-server"
        )
        assert response.status_code == 200
        document = response.json()
        assert document["issuer"] == origin
        assert document["token_endpoint"].startswith(origin + "/")
        # A cache in front of this must not serve one hostname's document to
        # the other: the body IS the host.
        assert "Host" in response.headers.get("vary", "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [NEW, OLD])
    async def test_the_protected_resource_document_is_that_hosts(
        self, origin, monkeypatch
    ):
        response = await self._get(
            self._app(monkeypatch),
            origin,
            f"/.well-known/oauth-protected-resource{MCP_PATH}",
        )
        assert response.json()["resource"] == f"{origin}{MCP_PATH}"

    @pytest.mark.asyncio
    async def test_the_printed_url_stays_canonical_on_the_old_hostname(
        self, monkeypatch
    ):
        """The page a person reads teaches the new name even when they got
        there on the old one."""
        response = await self._get(self._app(monkeypatch), OLD, "/connect")
        assert f"{NEW}{MCP_OAUTH_PATH}" in response.text
        assert OLD_HOST not in response.text
