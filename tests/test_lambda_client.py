"""Lambda client: payload shape and the sort_type null trap."""

import asyncio

import httpx
import pytest

from src.lambda_client import (
    LambdaClient,
    LambdaError,
    build_oneway_payload,
    build_roundtrip_payload,
)


class TestPayloadBuilding:
    def test_omits_none_rather_than_sending_null(self):
        # RoundtripAPI.sort_type is a bare enum -- an explicit null is a 422.
        payload = build_roundtrip_payload(
            departure_date="2026-05-01", return_date="2026-05-08",
            from_airport="TLV", to_airport="FCO",
        )
        assert "sort_type" not in payload
        assert not any(v is None for v in payload.values())

    def test_oneway_omits_none_too(self):
        payload = build_oneway_payload(
            departure_date="2026-10-14", from_airport="TLV", to_airport="CMB",
        )
        assert set(payload) == {"departure_date", "from_airport", "to_airport"}

    def test_keeps_falsey_values_that_are_meaningful(self):
        # max_stops=0 means non-stop only and must survive the None filter.
        payload = build_oneway_payload(
            departure_date="2026-10-14", from_airport="TLV", to_airport="CMB",
            max_stops=0, use_fallback=False, use_ext_proxy=False,
        )
        assert payload["max_stops"] == 0
        assert payload["use_fallback"] is False
        assert payload["use_ext_proxy"] is False

    def test_passes_through_filters(self):
        payload = build_oneway_payload(
            departure_date="2026-10-14", from_airport="TLV", to_airport="CMB",
            airline_codes=["LY"], currency="eur", limit=5, passengers=[2, 1, 0],
        )
        assert payload["airline_codes"] == ["LY"]
        assert payload["currency"] == "eur"
        assert payload["passengers"] == [2, 1, 0]


def _client(handler) -> LambdaClient:
    transport = httpx.MockTransport(handler)
    return LambdaClient(
        "https://lambda.example.com",
        "secret",
        timeout_seconds=5,
        client=httpx.AsyncClient(transport=transport),
    )


class TestSearch:
    def test_sends_the_auth_header_the_backend_checks(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["secret"] = request.headers.get("X-RapidAPI-Proxy-Secret")
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])

        async def run():
            async with _client(handler) as c:
                await c.search("oneway", {"departure_date": "2026-10-14"})

        asyncio.run(run())
        assert seen["secret"] == "secret"
        assert seen["url"].endswith("/api/google_flights/oneway/v1")

    def test_empty_array_is_a_result_not_an_error(self):
        async def run():
            async with _client(lambda r: httpx.Response(200, json=[])) as c:
                return await c.search("oneway", {})

        assert asyncio.run(run()) == []

    def test_parses_result_rows(self):
        rows = [{"price_as_number": 412, "buy_link": "https://x"}]

        async def run():
            async with _client(lambda r: httpx.Response(200, json=rows)) as c:
                return await c.search("oneway", {})

        assert asyncio.run(run()) == rows

    def test_403_is_not_retried(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(403, text="Auth header missing")

        async def run():
            async with _client(handler) as c:
                await c.search("oneway", {})

        with pytest.raises(LambdaError, match="403"):
            asyncio.run(run())
        assert attempts["n"] == 1, "a bad secret is deterministic; do not retry it"

    def test_422_is_not_retried(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(422, text="validation error")

        async def run():
            async with _client(handler) as c:
                await c.search("roundtrip", {})

        with pytest.raises(LambdaError):
            asyncio.run(run())
        assert attempts["n"] == 1

    def test_502_is_retried_then_succeeds(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(502, text="Bad Gateway")
            return httpx.Response(200, json=[{"ok": True}])

        async def run():
            async with _client(handler) as c:
                return await c.search("oneway", {})

        assert asyncio.run(run()) == [{"ok": True}]
        assert attempts["n"] == 2

    def test_gives_up_after_max_attempts(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(503, text="unavailable")

        async def run():
            async with _client(handler) as c:
                await c.search("oneway", {})

        with pytest.raises(LambdaError, match="after 3 attempts"):
            asyncio.run(run())
        assert attempts["n"] == 3

    def test_non_json_body_is_an_error_not_a_crash(self):
        async def run():
            async with _client(lambda r: httpx.Response(200, text="<html>")) as c:
                await c.search("oneway", {})

        with pytest.raises(LambdaError, match="non-JSON"):
            asyncio.run(run())
