"""End-to-end tool behaviour with the backend stubbed out."""

import json

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.server import build_server
from src.settings import load_settings


@pytest.fixture
def stub_backend(monkeypatch):
    """Replace LambdaClient.search and record every payload it receives."""
    calls: list[tuple[str, dict]] = []

    async def fake_search(self, endpoint, payload):
        calls.append((endpoint, payload))
        return [
            {
                "buy_link": f"https://book/{payload['departure_date']}",
                "price_as_number": 400 + len(calls),
                "duration_seconds": 30000 - len(calls),
                "airline": "Delta",
            }
        ]

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )
    return calls


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    return build_server(load_settings())


async def _call(server, tool, args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
        return result.structured_content


class TestOneway:
    @pytest.mark.asyncio
    async def test_single_date_makes_one_backend_call(self, server, stub_backend):
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert len(stub_backend) == 1
        assert data["result_count"] == 1
        assert data["search_coverage"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_the_thirty_call_prompt_is_capped_at_fifteen(
        self, server, stub_backend
    ):
        # "oneway from TLV to Sri Lanka anywhere in October" as ONE tool call.
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-31",
        })
        assert len(stub_backend) == 15, "the cap is the whole point"
        coverage = data["search_coverage"]
        assert coverage["requested_combinations"] == 31
        assert coverage["searched_combinations"] == 15
        assert coverage["truncated"] is True
        assert "note" in coverage

    @pytest.mark.asyncio
    async def test_truncation_is_reported_not_silent(self, server, stub_backend):
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-31",
        })
        note = data["search_coverage"]["note"]
        assert "31" in note and "15" in note
        assert "spread evenly" in note

    @pytest.mark.asyncio
    async def test_never_sends_a_null_sort_type(self, server, stub_backend):
        await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        _, payload = stub_backend[0]
        assert "sort_type" not in payload
        assert not any(v is None for v in payload.values())

    @pytest.mark.asyncio
    async def test_uppercases_airport_codes(self, server, stub_backend):
        await _call(server, "search_oneway_flights", {
            "from_airport": "tlv", "to_airport": "cmb",
            "departure_date": "2026-10-14",
        })
        _, payload = stub_backend[0]
        assert payload["from_airport"] == "TLV"
        assert payload["to_airport"] == "CMB"

    @pytest.mark.asyncio
    async def test_sorts_merged_results_by_price(self, server, stub_backend):
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-05",
            "sort_by": "price",
        })
        prices = [r["price_as_number"] for r in data["results"]]
        assert prices == sorted(prices)

    @pytest.mark.asyncio
    async def test_rejects_a_bad_sort_by(self, server, stub_backend):
        with pytest.raises(Exception, match="sort_by"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14", "sort_by": "cheapest",
            })

    @pytest.mark.asyncio
    async def test_multiple_destinations_in_one_call(self, server, stub_backend):
        await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": ["CMB", "DXB"],
            "departure_date": "2026-10-14",
        })
        assert {p["to_airport"] for _, p in stub_backend} == {"CMB", "DXB"}


class TestRoundtrip:
    @pytest.mark.asyncio
    async def test_nights_expands_into_return_dates(self, server, stub_backend):
        await _call(server, "search_roundtrip_flights", {
            "from_airport": "TLV", "to_airport": "FCO",
            "departure_date": "2026-05-01", "nights": [5, 7],
        })
        returns = sorted(p["return_date"] for _, p in stub_backend)
        assert returns == ["2026-05-06", "2026-05-08"]

    @pytest.mark.asyncio
    async def test_capped_like_oneway(self, server, stub_backend):
        await _call(server, "search_roundtrip_flights", {
            "from_airport": "TLV", "to_airport": "FCO",
            "departure_date_from": "2026-05-01",
            "departure_date_to": "2026-05-31",
            "nights": [5, 6, 7],
        })
        assert len(stub_backend) == 15


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_partial_failure_still_returns_results(
        self, server, monkeypatch, tmp_path
    ):
        state = {"n": 0}

        async def flaky(self, endpoint, payload):
            state["n"] += 1
            if state["n"] % 2 == 0:
                raise lambda_client_module.LambdaError("Lambda returned 502")
            return [{"buy_link": f"x{state['n']}", "price_as_number": 100}]

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", flaky)
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-05",
        })
        assert data["result_count"] > 0
        assert "partial" in data

    @pytest.mark.asyncio
    async def test_total_failure_is_an_error_not_an_empty_answer(
        self, server, monkeypatch
    ):
        async def always_fail(self, endpoint, payload):
            raise lambda_client_module.LambdaError("Lambda returned 502")

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", always_fail)
        with pytest.raises(Exception, match="temporarily unavailable"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })

    @pytest.mark.asyncio
    async def test_empty_results_explain_themselves(self, server, monkeypatch):
        async def empty(self, endpoint, payload):
            return []

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", empty)
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert data["result_count"] == 0
        assert "message" in data

    @pytest.mark.asyncio
    async def test_bad_date_is_a_clean_tool_error(self, server, stub_backend):
        with pytest.raises(Exception, match="ISO date"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "next tuesday",
            })
        assert stub_backend == [], "must not hit the backend on a bad request"


class TestTelemetryWiring:
    @pytest.mark.asyncio
    async def test_every_call_is_logged(self, server, stub_backend, tmp_path):
        await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-31",
        })
        snapshot = await server.telemetry.snapshot()
        assert snapshot["totals"]["tool_calls"] == 1
        assert snapshot["totals"]["backend_calls"] == 15
        assert snapshot["backend_calls_per_tool_call"] == 15.0

    @pytest.mark.asyncio
    async def test_zero_result_calls_are_not_ad_eligible(self, server, monkeypatch):
        async def empty(self, endpoint, payload):
            return []

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", empty)
        await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        # Lulu's own testing: an ad with no substantive data beside it gets
        # flagged by the model as suspected prompt injection.
        snap = await server.telemetry.snapshot()
        assert snap["totals"].get("ad_eligible_calls", 0) == 0
