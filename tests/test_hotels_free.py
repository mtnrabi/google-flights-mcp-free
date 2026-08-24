"""
Hotel tools on the free, ad-supported server.

Different economics from the paid servers: every call here is billed to mrabi
rather than to the caller's RapidAPI key, so the budget check and the
telemetry record that feeds it are the tests that matter most. A hotel search
that does not count against the budget would be free forever and the check
would never fire.
"""

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import src.server as server_module
from src.hotels_lambda_client import (
    HotelsLambdaClient,
    build_search_payload,
)
from src.lambda_client import LambdaError
from src.server import build_server
from src.settings import load_settings

PROPERTY = {
    "name": "Hotel Leone",
    "price_string": "US$273",
    "price": 273,
    "review_score": 8.2,
    "room_type": "Superior Double",
    "link": "https://www.booking.com/hotel/it/leone.html",
}

HOTEL_ENV = {
    "hotels_lambda_url": "https://hotels.test",
    "hotels_auth": "secret",
}


@pytest.fixture
def hotels_server(tmp_path, monkeypatch):
    """A server with the hotels backend configured and stubbed."""
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    return build_server(load_settings())


@pytest.fixture
def stub_hotels(monkeypatch):
    """Replace HotelsLambdaClient.search; the list it returns is the result."""
    rows: list = [PROPERTY]

    async def fake_search(self, endpoint, payload):
        return list(rows)

    monkeypatch.setattr(HotelsLambdaClient, "search", fake_search, raising=True)
    return rows


class TestClientNormalisation:
    @pytest.mark.asyncio
    async def test_search_unwraps_properties(self):
        """`/search` answers with an object carrying `properties`."""

        def handler(_r):
            return httpx.Response(200, json={"properties": [PROPERTY]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsLambdaClient("https://h.test", "s", client=http) as c:
                rows = await c.search("search", {})
        assert rows == [PROPERTY]

    @pytest.mark.asyncio
    async def test_single_object_becomes_a_list(self):
        """`/hotel_by_name` answers with one property. A model should not have
        to branch on which endpoint it called."""

        def handler(_r):
            return httpx.Response(200, json=PROPERTY)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsLambdaClient("https://h.test", "s", client=http) as c:
                rows = await c.search("hotel_by_name", {})
        assert rows == [PROPERTY]

    @pytest.mark.asyncio
    async def test_sends_the_proxy_secret(self):
        seen = {}

        def handler(request: httpx.Request):
            seen.update(request.headers)
            return httpx.Response(200, json={"properties": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsLambdaClient("https://h.test", "sek", client=http) as c:
                await c.search("search", {})
        assert seen["x-rapidapi-proxy-secret"] == "sek"

    @pytest.mark.asyncio
    async def test_403_is_not_retried(self):
        """A 403 is a wrong proxy secret -- our config, not a blip. Retrying
        just delays a clear error."""
        calls = []

        def handler(_r):
            calls.append(1)
            return httpx.Response(403, text="invalid or missing secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with HotelsLambdaClient("https://h.test", "s", client=http) as c:
                with pytest.raises(LambdaError):
                    await c.search("search", {})
        assert len(calls) == 1

    def test_none_values_are_dropped(self):
        got = build_search_payload(
            destination="Rome",
            checkin_date="2026-11-10",
            checkout_date="2026-11-13",
            adults=None,
        )
        assert "adults" not in got


class TestRegistration:
    @pytest.mark.asyncio
    async def test_hotels_absent_without_config(self, monkeypatch, tmp_path):
        """Registering tools that can only 500 is worse than not offering
        them."""
        monkeypatch.delenv("HOTELS_LAMBDA_URL", raising=False)
        monkeypatch.delenv("HOTELS_AUTH", raising=False)
        monkeypatch.setenv("ADS_ENABLED", "false")
        mcp = build_server(load_settings())
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert "search_hotels" not in names

    @pytest.mark.asyncio
    async def test_hotels_present_when_configured(self, hotels_server):
        async with Client(hotels_server) as client:
            names = {t.name for t in await client.list_tools()}
        assert {"search_hotels", "find_hotel_by_name"} <= names

    @pytest.mark.asyncio
    async def test_result_carries_the_upgrade_pitch(
        self, hotels_server, stub_hotels
    ):
        """mrabi asked that the free surfaces sell the paid ones explicitly."""
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        up = out.data["upgrade"]
        assert up["paid_hotels"] == "https://hotels.flightpowers.com/mcp"
        assert "No ads" in up["what_you_get"]
        assert out.data["result_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_result_explains_rather_than_erroring(
        self, hotels_server, stub_hotels
    ):
        stub_hotels.clear()
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Nowhere",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        assert out.data["result_count"] == 0
        assert "message" in out.data


class TestWidgetMapping:
    def test_every_column_path_resolves_against_a_live_shaped_row(self):
        """A renamed backend field silently empties a column rather than
        failing, so the paths are resolved here against a real response
        shape."""
        for col in server_module.HOTELS_WIDGET_MAPPING["columns"]:
            assert col["path"] in PROPERTY, col["path"]
