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
        up = out.structured_content["upgrade"]
        assert up["paid_hotels"] == "https://hotels.flightpowers.com/mcp"
        # One hostname for the paid flights server everywhere on this
        # server. Both names resolve to the same deployment since #415, but
        # a caller handed two addresses in one conversation reads them as
        # two products.
        assert up["paid_flights"] == "https://flights.flightpowers.com/mcp"
        # Sign-in first since 2026-09-08. Both fields are present; the
        # sign-in pair is what `how_to_connect` names first.
        assert up["sign_in_flights"] == "https://flights.flightpowers.com/mcp/oauth"
        assert up["sign_in_hotels"] == "https://hotels.flightpowers.com/mcp/oauth"
        connect = up["how_to_connect"]
        assert connect.index("/mcp/oauth") < connect.index("x-rapidapi-key")
        assert "sign in with Google" in connect
        assert "No ads" in up["what_you_get"]
        assert out.structured_content["result_count"] == 1

    @pytest.mark.asyncio
    async def test_the_upgrade_note_says_where_a_key_comes_from(
        self, hotels_server, stub_hotels
    ):
        """This is the only upsell a caller sees on a call that WORKED.

        It told them to bring their own RapidAPI key and never said where a
        key comes from or what it costs -- both facts live twenty lines away
        in `fair_use.upgrade_steps()`, which only runs at 80% or on a
        refusal. A next action, and its price, on the one always-on surface.
        """
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        key = out.structured_content["upgrade"]["get_a_key"]
        assert "https://rapidapi.com/mtnrabi/api/google-flights-live-api" in key
        assert "https://rapidapi.com/mtnrabi/api/booking-live-api" in key
        # The listings' own billingPlans figures, cited in fair_use.py.
        assert "BASIC is free and includes 10 requests a month" in key
        assert "PRO is $10 a month" in key

    def test_the_get_a_key_sentence_cannot_drift_from_upgrade_steps(self):
        """Two surfaces, one sentence. `upgrade_steps()[0]` is the version a
        caller sees at 80% or on a refusal; `get_a_key` is the version they
        see on a call that worked. If the prices ever disagree, one of them
        is lying to somebody."""
        from src.fair_use import upgrade_steps
        from src.server import _UPGRADE_GET_A_KEY

        priced = (
            "BASIC is free and includes 10 requests a month; PRO is $10 a "
            "month (2,500 requests on flights, 2,000 on hotels)."
        )
        assert priced in _UPGRADE_GET_A_KEY
        assert priced in upgrade_steps()[0]

    @pytest.mark.asyncio
    async def test_the_upgrade_note_states_the_paid_cap_it_used_to_deny(
        self, hotels_server, stub_hotels
    ):
        """It promised "no per-call search cap". The paid server caps fan-out
        at 30 by default, 60 hard (mcp_server_paid/src/settings.py), so the
        sentence that converts a free user was false and they only found out
        after subscribing. The honest reasons -- ad-free, twice the fan-out,
        per-country hotel pricing, spend reporting -- are all still here."""
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        pitch = out.structured_content["upgrade"]["what_you_get"]
        assert "no per-call search cap" not in pitch
        assert "30" in pitch and "60" in pitch
        assert "max_searches" in pitch
        assert "price_as_seen_from" in pitch

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
        assert out.structured_content["result_count"] == 0
        assert "message" in out.structured_content


class TestWidgetMapping:
    def test_every_column_path_resolves_against_a_live_shaped_row(self):
        """A renamed backend field silently empties a column rather than
        failing, so the paths are resolved here against a real response
        shape."""
        for col in server_module.HOTELS_WIDGET_MAPPING["columns"]:
            if col is server_module.BOOK_COLUMN:
                # `book_label` is added by this server, not sent by the
                # backend, so it is asserted end-to-end below instead.
                continue
            assert col["path"] in PROPERTY, col["path"]

    @pytest.mark.asyncio
    async def test_the_book_column_carries_the_property_link(
        self, hotels_server, stub_hotels
    ):
        """A hotels row clicks through to Booking.com and now says so.

        Same shape as the flights table: the cell is a short label, the URL
        stays in `link` where the model already reads it, and `rowLink` does
        the opening.
        """
        assert server_module.BOOK_COLUMN in (
            server_module.HOTELS_WIDGET_MAPPING["columns"]
        )
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        row = out.structured_content["results"][0]
        assert row[server_module.BOOK_COLUMN["path"]] == "Book →"
        assert row["link"] == PROPERTY["link"], "the URL must not be renamed"
        assert "book_label" not in PROPERTY, (
            "the backend fixture was mutated -- rows must be copied, not "
            "labelled in place"
        )

    def test_row_link_points_at_the_property_url(self):
        """Each row clicks through to the property. `link` is the field the
        hotels backend already emits (verified live 2026-08-18), so this
        needs no new API field."""
        assert server_module.HOTELS_WIDGET_MAPPING["rowLink"] == "link"
        assert PROPERTY[server_module.HOTELS_WIDGET_MAPPING["rowLink"]]

    @pytest.mark.asyncio
    async def test_a_property_without_a_link_still_renders(
        self, hotels_server, stub_hotels
    ):
        """Backend rows pass through verbatim, so a property with no `link`
        has to keep its columns and its place in the table -- the widget
        simply attaches no click handler to it."""
        stub_hotels[:] = [{k: v for k, v in PROPERTY.items() if k != "link"}]
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        assert out.structured_content["result_count"] == 1
        row = out.structured_content["results"][0]
        assert row.get(server_module.HOTELS_WIDGET_MAPPING["rowLink"]) is None
        for col in server_module.HOTELS_WIDGET_MAPPING["columns"]:
            if col is server_module.BOOK_COLUMN:
                # Nothing to open, so nothing to offer: the Book cell is
                # blank rather than a button that does nothing.
                assert row.get(col["path"]) is None
                continue
            assert row.get(col["path"]) is not None, col["path"]


class TestTheServerDescribesWhatItActuallyOffers:
    """The fourth and fifth instances of one copy-paste defect.

    Flights wording keeps leaking into strings a HOTELS caller reads. It has
    now been found in the paid server's `instructions` and `SIGNUP_URL` (#371),
    in its keyless reply ("billed to the caller's own Google Flights API
    subscription"), and here. These assert the invariant rather than the
    sentence: whatever a hotels caller is handed must describe the hotel tools.
    """

    @pytest.mark.asyncio
    async def test_instructions_name_the_hotel_tools_when_they_are_registered(
        self, hotels_server
    ):
        """`instructions` reaches every client before it calls anything, so a
        server that introduces itself as "Free real-time Google Flights
        search" steers models away from a hotel search it can serve."""
        async with Client(hotels_server) as client:
            instructions = client.initialize_result.instructions or ""
            names = {t.name for t in await client.list_tools()}

        assert "search_hotels" in names
        assert "search_hotels" in instructions
        assert "find_hotel_by_name" in instructions
        assert "hotel rates" in instructions

    @pytest.mark.asyncio
    async def test_instructions_do_not_give_hotel_tools_the_flight_calling_rule(
        self, hotels_server
    ):
        """"Both tools accept a date RANGE and a LIST of destinations" was
        advice for two tools on a server that registers four, and it is not
        how either hotel tool is called."""
        async with Client(hotels_server) as client:
            instructions = client.initialize_result.instructions or ""
        assert "Both tools accept a date RANGE" not in instructions
        assert "do NOT accept a date range" in instructions

    @pytest.mark.asyncio
    async def test_instructions_stay_flights_only_without_a_hotels_backend(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("HOTELS_LAMBDA_URL", raising=False)
        monkeypatch.delenv("HOTELS_AUTH", raising=False)
        monkeypatch.setenv("ADS_ENABLED", "false")
        mcp = build_server(load_settings())
        async with Client(mcp) as client:
            instructions = client.initialize_result.instructions or ""
        assert "search_hotels" not in instructions

    @pytest.mark.asyncio
    async def test_the_upgrade_note_on_a_hotel_result_states_hotel_limits(
        self, hotels_server, stub_hotels
    ):
        """`upgrade` is attached to hotel results only, and its `limits` field
        was the flights fan-out cap -- so every hotel caller was told their
        single-request search was capped at "15 searches per call". A hotel
        call is exactly one backend call."""
        async with Client(hotels_server) as client:
            out = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-11-10",
                    "checkout_date": "2026-11-13",
                },
            )
        limits = out.structured_content["upgrade"]["limits"]
        assert "15 searches per call" not in limits
        assert "shared daily budget" in limits
        assert "Booking.com filters" in limits

    @pytest.mark.asyncio
    async def test_a_backend_failure_reaches_the_message_written_for_it(
        self, hotels_server, monkeypatch
    ):
        """`LambdaError` was caught in `_hotels_run` but never imported into
        server.py, so the handler raised NameError while handling the
        exception and the caller got an opaque internal error instead."""

        async def fail(self, endpoint, payload):
            raise LambdaError("upstream said no")

        monkeypatch.setattr(HotelsLambdaClient, "search", fail, raising=True)
        async with Client(hotels_server) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "search_hotels",
                    {
                        "destination": "Rome",
                        "checkin_date": "2026-11-10",
                        "checkout_date": "2026-11-13",
                    },
                )
        assert "This is our side" in str(exc.value)
        assert "NameError" not in str(exc.value)
