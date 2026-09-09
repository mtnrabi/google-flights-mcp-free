"""Lambda client: payload shape and the sort_type null trap."""

import asyncio

import httpx
import pytest

from src.lambda_client import (
    LambdaClient,
    LambdaError,
    build_oneway_payload,
    build_roundtrip_payload,
    read_search_status,
    search_is_incomplete,
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

    def test_use_fallback_none_is_omitted_so_the_backend_can_escalate(self):
        """Tri-state upstream: absent != false.

        Absent lets the backend escalate to the fallback client once, after
        every retry for a combination has failed; `false` forbids the fallback
        outright, escalation included. Sending `false` by default -- which is
        what these tools did -- opted our own users out of the reliability fix.
        """
        oneway = build_oneway_payload(
            departure_date="2026-10-14", from_airport="TLV", to_airport="CMB",
        )
        roundtrip = build_roundtrip_payload(
            departure_date="2026-05-01", return_date="2026-05-08",
            from_airport="TLV", to_airport="FCO",
        )
        assert "use_fallback" not in oneway
        assert "use_fallback" not in roundtrip

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


class TestSearchStatus:
    """`X-Search-Status` is the only thing that separates "Google has no
    flights" from "the scrape was blocked". Both arrive as HTTP 200 with `[]`.
    """

    def test_reads_the_search_headers_and_nothing_else(self):
        got = read_search_status(
            httpx.Response(
                200,
                headers={
                    "X-Search-Status": "degraded",
                    "X-Search-Reason": "blocked_page",
                    "content-type": "application/json",
                },
            )
        )
        assert got == {
            "x-search-status": "degraded",
            "x-search-reason": "blocked_page",
        }

    def test_absent_headers_are_empty_not_healthy(self):
        """An empty dict must read as "the backend did not say", never as "the
        search was fine" -- that assumption is the bug being fixed."""
        assert read_search_status(httpx.Response(200)) == {}

    def test_degraded_and_partial_are_incomplete(self):
        assert search_is_incomplete({"x-search-status": "degraded"})
        assert search_is_incomplete({"x-search-status": "partial"})
        assert not search_is_incomplete({"x-search-status": "empty"})
        assert not search_is_incomplete({})

    def test_the_sink_gets_one_entry_per_answered_request(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            status = "degraded" if calls["n"] == 1 else "ok"
            return httpx.Response(200, json=[], headers={"X-Search-Status": status})

        async def run():
            sink: list[dict[str, str]] = []
            client = LambdaClient(
                "https://lambda.test",
                "secret",
                timeout_seconds=5.0,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            async with client:
                await client.search("oneway", {}, outcome_sink=sink)
                await client.search("oneway", {}, outcome_sink=sink)
            return sink

        sink = asyncio.run(run())
        assert [entry["x-search-status"] for entry in sink] == ["degraded", "ok"]


class TestTimeoutAlignment:
    """The read timeout cannot give up on an answer the function can still send.

    This server calls the search Lambda directly. The property used to be stated
    as a ceiling -- "cannot outlive the function", with the default pinned at or
    below ``Timeout: 45`` -- and that inverted rule became a live bug on
    2026-08-27, when the deployed ``Timeout`` was raised to 60 and the 45.0 here
    did not move. A caller in that state abandons searches that were about to
    succeed; measured through the RapidAPI edge the same day, 2 of 46 requests
    answered successfully at 48.7s and 59.7s.

    So the constraint is a floor, and the direction matters: below the callee's
    ``Timeout`` discards real answers, above it only costs latency on a request
    that has already failed. Before 45 the default was 105, from
    ``backend/src/constants.py``'s deleted LAMBDA_REQUEST_TIMEOUT_SECONDS (90)
    plus a router's +15 -- a number describing a function that never existed.
    Both mistakes have the same cure: derive the wait from one declared
    ``Timeout`` instead of restating a literal.
    """

    #: The deployed ``flyMyGApi`` ``Timeout``, read from the live configuration
    #: on 2026-08-27 (it was 45 on 2026-08-25).
    DEPLOYED_FUNCTION_TIMEOUT_SECONDS = 60.0

    def test_the_code_agrees_with_the_deployed_function_timeout(self):
        from src import settings as settings_mod

        assert (settings_mod.UPSTREAM_FUNCTION_TIMEOUT_SECONDS
                == self.DEPLOYED_FUNCTION_TIMEOUT_SECONDS)

    def test_the_default_outlives_the_function(self):
        from src.settings import load_settings

        assert (load_settings().lambda_timeout_seconds
                > self.DEPLOYED_FUNCTION_TIMEOUT_SECONDS)

    def test_the_default_is_derived_rather_than_restated(self):
        from src import settings as settings_mod
        from src.settings import load_settings

        assert (load_settings().lambda_timeout_seconds
                == settings_mod.UPSTREAM_FUNCTION_TIMEOUT_SECONDS
                + settings_mod.UPSTREAM_RELAY_MARGIN_SECONDS)

    def test_the_client_defaults_match_the_settings_default(self):
        """A client built without settings must not be more patient.

        Both clients carry their own default for direct use, and a divergence
        there is exactly how 105 came to be written down in three places.
        """
        import inspect

        from src.hotels_lambda_client import HotelsLambdaClient
        from src.settings import load_settings

        expected = load_settings().lambda_timeout_seconds
        for client in (LambdaClient, HotelsLambdaClient):
            default = inspect.signature(
                client.__init__).parameters["timeout_seconds"].default
            assert default == expected, client.__name__

    def test_it_is_still_tunable_without_a_deploy(self, monkeypatch):
        from src.settings import load_settings

        monkeypatch.setenv("LAMBDA_TIMEOUT_SECONDS", "20")
        assert load_settings().lambda_timeout_seconds == 20.0
