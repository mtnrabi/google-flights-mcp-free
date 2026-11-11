"""`passengers`: what the client sends versus what the Lambda gets.

Same helper and same rules as the paid server (mcp_server_paid/src/
passengers.py, tests/test_passengers.py there carry the mapping table). This
file checks the free server wires it in: a counts list reaches the Lambda as
codes, a codes list unchanged, and a 422 on every combination is reported as
a refusal rather than "temporarily unavailable". The telemetry line keeps
`pax=` as the client sent it (test_attribution.py asserts `2/1/0`).
"""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import lambda_client as lambda_client_module
from src.passengers import normalise_passengers
from src.server import build_server
from src.settings import load_settings


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


async def _call(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


class TestMappingIsShared:
    @pytest.mark.parametrize(
        "sent, forwarded",
        [
            ([2, 1, 0], [1, 1, 2]),
            ([1, 0, 0], [1]),
            ([2], [1, 1]),
            ([1, 2], [1, 2]),
            ([1, 1, 2], [1, 1, 2]),
        ],
    )
    def test_table(self, sent, forwarded):
        assert normalise_passengers(sent) == forwarded

    @pytest.mark.parametrize("sent", [[], [0, 1], [1, 3, 3], [1] * 10])
    def test_unrecognised_is_forwarded_unchanged(self, sent):
        assert normalise_passengers(sent) is sent


class TestToolsForwardCodes:
    @pytest.mark.asyncio
    async def test_oneway_counts_list_reaches_the_lambda_as_codes(
        self, server, recording_backend
    ):
        await _call(server, "search_oneway_flights", {
            "from_airport": "LHR",
            "to_airport": "DXB",
            "departure_date": "2026-12-15",
            "passengers": [2, 1, 0],
        })
        assert recording_backend
        assert recording_backend[0]["passengers"] == [1, 1, 2]

    @pytest.mark.asyncio
    async def test_oneway_codes_list_is_forwarded_as_sent(
        self, server, recording_backend
    ):
        await _call(server, "search_oneway_flights", {
            "from_airport": "LHR",
            "to_airport": "DXB",
            "departure_date": "2026-12-15",
            "passengers": [1, 2],
        })
        assert recording_backend[0]["passengers"] == [1, 2]

    @pytest.mark.asyncio
    async def test_oneway_omitted_stays_omitted(self, server, recording_backend):
        await _call(server, "search_oneway_flights", {
            "from_airport": "LHR",
            "to_airport": "DXB",
            "departure_date": "2026-12-15",
        })
        assert "passengers" not in recording_backend[0]

    @pytest.mark.asyncio
    async def test_roundtrip_counts_list_reaches_the_lambda_as_codes(
        self, server, recording_backend
    ):
        await _call(server, "search_roundtrip_flights", {
            "from_airport": "LHR",
            "to_airport": "DXB",
            "departure_date": "2026-12-15",
            "return_date": "2026-12-22",
            "passengers": [2],
        })
        assert recording_backend[0]["passengers"] == [1, 1]

    @pytest.mark.asyncio
    async def test_a_bare_integer_is_refused_before_any_backend_call(
        self, server, recording_backend
    ):
        with pytest.raises(ToolError, match="passengers"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "LHR",
                "to_airport": "DXB",
                "departure_date": "2026-12-15",
                "passengers": 2,
            })
        assert recording_backend == []


class TestRefusalIsNotAnOutage:
    @pytest.mark.asyncio
    async def test_lambda_422_reads_as_a_refusal_with_the_detail(
        self, server, monkeypatch
    ):
        async def reject(self, endpoint, payload, **_kwargs):
            raise lambda_client_module.LambdaError(
                "oneway search failed -- HTTP 422: passengers - [1, 3, 3] is "
                "not a valid passenger list; each infant on lap (3) needs its "
                "own adult (1)"
            )

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", reject)
        with pytest.raises(ToolError) as exc:
            await _call(server, "search_oneway_flights", {
                "from_airport": "LHR",
                "to_airport": "DXB",
                "departure_date": "2026-12-15",
                "passengers": [1, 3, 3],
            })
        text = str(exc.value)
        assert "refused" in text
        assert "temporarily unavailable" not in text
        assert "each infant on lap (3) needs its own adult" in text

    @pytest.mark.asyncio
    async def test_lambda_502_is_still_an_outage(self, server, monkeypatch):
        async def fail(self, endpoint, payload, **_kwargs):
            raise lambda_client_module.LambdaError("Lambda returned 502")

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", fail)
        with pytest.raises(ToolError, match="temporarily unavailable"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "LHR",
                "to_airport": "DXB",
                "departure_date": "2026-12-15",
            })


class TestSchemaTextStaysHonest:
    @pytest.mark.parametrize(
        "name", ["search_oneway_flights", "search_roundtrip_flights"]
    )
    def test_description_names_both_shapes(self, monkeypatch, tmp_path, name):
        from tests.test_param_descriptions import tools_for

        tool = next(t for t in tools_for(monkeypatch, tmp_path, False) if t["name"] == name)
        text = tool["inputSchema"]["properties"]["passengers"]["description"]
        assert "One entry per traveller" in text
        assert "[2, 1, 0]" in text
        assert "converted to codes" in text
        assert "counts as" not in text
