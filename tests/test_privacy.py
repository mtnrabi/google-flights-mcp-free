"""
`/privacy` on the free server: served to anyone, on every hostname, and every
page that used to link flightpowers.com/privacy now links it.

Until 2026-10-22 `/privacy` answered 404 here and the server linked the
marketing site's page, which says nothing about sign-ins, counters, emails,
Lulu or Stay22 (state/gtm/research/data-sale-2026-10-22/legal.md §2a).

    python -m pytest mcp_server/tests/test_privacy.py -q
"""

from __future__ import annotations

import httpx
from starlette.applications import Starlette

from src import pages
from src.landing import LandingPage, landing_html
from src.privacy import COUNTER_DAYS, privacy_html
from src.stores import (
    BUCKET_TTL_SECONDS,
    FAIR_USE_MONTH_TTL_SECONDS,
    RETENTION_HOURS,
)

from test_landing import BROWSER, CLIENT_JSON, CURL, NEW, OLD, stack  # noqa: F401


def _client(asgi, base_url: str = NEW) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
        base_url=base_url,
    )


class TestServed:
    async def test_privacy_answers_200_html_to_anyone_on_every_host(self, stack):  # noqa: F811
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            for host in (NEW, OLD):
                async with _client(app, host) as client:
                    for headers in (BROWSER, CURL, CLIENT_JSON):
                        for path in ("/privacy", "/privacy/"):
                            response = await client.get(path, headers=headers)
                            assert response.status_code == 200, (host, path)
                            assert response.headers["content-type"].startswith(
                                "text/html"
                            )
                            assert "FlightPowers free MCP: privacy" in response.text

    async def test_head_privacy_has_no_body(self, stack):  # noqa: F811
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.head("/privacy", headers=BROWSER)
        assert response.status_code == 200
        assert response.content == b""

    async def test_post_privacy_is_not_the_page(self, stack):  # noqa: F811
        """Only GET/HEAD are the page; anything else reaches the app as before."""
        app, _, _ = stack
        async with app.router.lifespan_context(app):
            async with _client(app) as client:
                response = await client.post("/privacy", headers=CURL)
        assert "FlightPowers free MCP: privacy" not in response.text

    def test_the_layer_does_not_need_the_gate_for_privacy(self):
        """A bare LandingPage answers /privacy without touching the app."""

        class _Settings:
            public_url = f"{NEW}/mcp"

            def public_url_for_host(self, host):
                return self.public_url

        async def _boom(scope, receive, send):  # pragma: no cover - must not run
            raise AssertionError("the wrapped app must not see /privacy")

        app = Starlette()
        app.mount("/", LandingPage(_boom, _Settings()))
        import asyncio

        async def go():
            async with _client(app) as client:
                return await client.get("/privacy")

        assert asyncio.run(go()).status_code == 200


class TestLinks:
    def test_every_page_links_the_free_hosts_own_privacy_page(self):
        assert pages.PRIVACY_URL == "https://free-trial.flightpowers.com/privacy"
        assert pages.PRIVACY_URL in pages.footer()
        assert pages.PRIVACY_URL in landing_html(f"{NEW}/mcp")
        assert "https://flightpowers.com/privacy\"" not in landing_html(f"{NEW}/mcp")


class TestContent:
    def test_it_names_what_the_server_keeps_and_who_gets_it(self):
        html = privacy_html()
        for fact in (
            "Google account id and email address",
            "which AI app you approved",
            "scrambled code",
            "your IP address itself",
            "the IP address it registered from",
            "Zoho Mail",
            "GitHub",
            "number of guests",
            "Lulu",
            "only the name of the tool you used",
            "Stay22",
            "Resend",
            "unsubscribe",
            'href="/connect"',
            "Failed flight searches",
            "no automatic deletion yet",
            "We do not sell your personal data",
            "app@flightpowers.com",
            "not legal advice",
        ):
            assert fact in html, fact

    def test_the_counter_retention_is_the_stores_own_number(self):
        assert COUNTER_DAYS * 24 == RETENTION_HOURS
        assert BUCKET_TTL_SECONDS == COUNTER_DAYS * 86400
        assert FAIR_USE_MONTH_TTL_SECONDS == COUNTER_DAYS * 86400
        assert f"Counters for {COUNTER_DAYS} days" in privacy_html()

    def test_it_points_to_the_other_policies(self):
        html = privacy_html()
        for url in (
            "https://flights.flightpowers.com/privacy",
            "https://hotels.flightpowers.com/privacy",
            "https://flightpowers.com/privacy",
        ):
            assert url in html

    def test_plain_page(self):
        html = privacy_html()
        for tag in ("<script", "<img", "<iframe"):
            assert tag not in html
        assert "—" not in html  # no em dash in outward copy
