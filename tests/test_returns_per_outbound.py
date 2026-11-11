"""`returns_per_outbound` on search_roundtrip_flights (backend flight_rabbi #525).

Same argument and bounds as the paid server (tests/test_returns_per_outbound.py
there). Omitted from the body when not given: PRE_CHANGE_BODY below was
recorded from origin/main at 3b41ba7 (before this change) with the same call,
so the default path is checked against the old code path, not against itself.
"""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import lambda_client as lambda_client_module
from src.server import build_server
from src.settings import load_settings

ARGS = {
    "from_airport": "LHR",
    "to_airport": "DXB",
    "departure_date": "2026-12-15",
    "return_date": "2026-12-22",
    "passengers": [2, 1, 0],
    "max_return_stops": 1,
    "seat_type": 3,
    "currency": "eur",
}

PRE_CHANGE_BODY = {
    "currency": "eur",
    "departure_date": "2026-12-15",
    "from_airport": "LHR",
    "limit": 10,
    "max_return_stops": 1,
    "passengers": [1, 1, 2],
    "return_date": "2026-12-22",
    "seat_type": 3,
    "to_airport": "DXB",
}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    return build_server(load_settings())


@pytest.fixture
def recording_backend(monkeypatch):
    payloads: list[dict] = []

    async def fake_search(self, endpoint, payload, **_kwargs):
        payloads.append(dict(payload))
        return [{
            "buy_link": "https://book/x",
            "price_as_number": 400,
            "duration_seconds": 30000,
            "airline": "Delta",
            "departure_date": payload.get("departure_date", "2026-12-15"),
        }]

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )
    return payloads


async def _call(server, args):
    async with Client(server) as client:
        return await client.call_tool("search_roundtrip_flights", args)


@pytest.mark.asyncio
async def test_default_body_is_unchanged_and_the_argument_only_adds_its_key(
    server, recording_backend
):
    await _call(server, ARGS)
    await _call(server, {**ARGS, "returns_per_outbound": 5})

    without, with_arg = recording_backend
    assert without == PRE_CHANGE_BODY
    assert with_arg == {**PRE_CHANGE_BODY, "returns_per_outbound": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 2, 5])
async def test_in_range_values_are_forwarded(server, recording_backend, value):
    await _call(server, {**ARGS, "returns_per_outbound": value})
    assert recording_backend[0]["returns_per_outbound"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 6, -1, 50])
async def test_out_of_range_is_refused_before_any_search(
    server, recording_backend, value
):
    with pytest.raises(ToolError, match="returns_per_outbound must be a whole number from 1 to 5"):
        await _call(server, {**ARGS, "returns_per_outbound": value})
    assert recording_backend == []


@pytest.mark.asyncio
async def test_a_fraction_is_refused_before_any_search(server, recording_backend):
    with pytest.raises(ToolError, match="returns_per_outbound"):
        await _call(server, {**ARGS, "returns_per_outbound": 2.5})
    assert recording_backend == []


def test_schema_documents_it_on_the_round_trip_tool_only(monkeypatch, tmp_path):
    from tests.test_param_descriptions import tools_for

    tools = {t["name"]: t for t in tools_for(monkeypatch, tmp_path, False)}
    prop = tools["search_roundtrip_flights"]["inputSchema"]["properties"][
        "returns_per_outbound"
    ]
    assert "1 to 5 (default 2)" in prop["description"]
    assert (
        "Each date/destination combination searched still returns at most 10 "
        "trips; at 5 they are picked from up to 50 pairings instead of 20."
    ) in prop["description"]
    assert "returns_per_outbound" not in tools["search_oneway_flights"]["inputSchema"]["properties"]
    assert "returns_per_outbound" not in tools["search_roundtrip_flights"]["inputSchema"].get(
        "required", []
    )
