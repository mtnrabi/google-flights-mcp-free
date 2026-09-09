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

    async def fake_search(self, endpoint, payload, **_kwargs):
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


async def _call_result(server, tool, args):
    """The whole CallToolResult, errors included.

    `_call` cannot reach a degraded search any more: that result now carries
    `isError: true`, and the client raises on it by default. Anything
    asserting on the error flag, or on a payload that rides alongside it,
    has to look at the result rather than just the structured content.
    """
    async with Client(server) as client:
        return await client.call_tool(tool, args, raise_on_error=False)


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

        async def flaky(self, endpoint, payload, **_kwargs):
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
        async def always_fail(self, endpoint, payload, **_kwargs):
            raise lambda_client_module.LambdaError("Lambda returned 502")

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", always_fail)
        with pytest.raises(Exception, match="temporarily unavailable"):
            await _call(server, "search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })

    @pytest.mark.asyncio
    async def test_empty_results_explain_themselves(self, server, monkeypatch):
        async def empty(self, endpoint, payload, **_kwargs):
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
        async def empty(self, endpoint, payload, **_kwargs):
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


class TestSearchStatusIsBelieved:
    """The backend answers a failed scrape and a genuine empty with the same
    HTTP 200 and the same `[]`, and says which in `X-Search-Status`. Until that
    header was read, the tool answered both with "No flights were found ... try
    a different date" -- a confident, checkable, wrong claim about the world
    that a model repeats to the user as fact.

    The stubs write into `outcome_sink` because that is exactly what the real
    LambdaClient does with the headers it reads off the response.
    """

    @staticmethod
    def _stub(rows, status=None, reason=None):
        async def search(self, endpoint, payload, *, outcome_sink=None, **_kwargs):
            if outcome_sink is not None:
                entry = {}
                if status:
                    entry["x-search-status"] = status
                if reason:
                    entry["x-search-reason"] = reason
                outcome_sink.append(entry)
            return list(rows)

        return search

    @pytest.mark.asyncio
    async def test_a_degraded_search_is_not_reported_as_no_flights(
        self, server, monkeypatch
    ):
        monkeypatch.setattr(
            lambda_client_module.LambdaClient,
            "search",
            self._stub([], status="degraded", reason="blocked_page"),
        )
        result = await _call_result(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        # The failure is on the result itself, not only inside the payload.
        assert result.is_error
        data = result.structured_content
        assert data["result_count"] == 0
        assert data["search_status"] == "degraded"
        assert "No flights were found" not in data["message"]
        assert "did not complete" in data["message"]
        assert "blocked_page" in data["message"]

    @pytest.mark.asyncio
    async def test_a_genuine_empty_still_says_no_flights(self, server, monkeypatch):
        monkeypatch.setattr(
            lambda_client_module.LambdaClient, "search", self._stub([], status="empty")
        )
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert data["search_status"] == "empty"
        assert "No flights were found" in data["message"]

    @pytest.mark.asyncio
    async def test_a_silent_backend_does_not_get_the_benefit_of_the_doubt(
        self, server, monkeypatch
    ):
        monkeypatch.setattr(
            lambda_client_module.LambdaClient, "search", self._stub([])
        )
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert "search_status" not in data
        assert "No flights were found" in data["message"]

    @pytest.mark.asyncio
    async def test_the_degraded_message_tells_the_model_what_not_to_say(
        self, server, monkeypatch
    ):
        monkeypatch.setattr(
            lambda_client_module.LambdaClient,
            "search",
            self._stub([], status="degraded"),
        )
        result = await _call_result(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert result.is_error
        data = result.structured_content
        assert "do not tell the user that none exist" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_a_healthy_search_is_labelled_ok(self, server, monkeypatch):
        row = {
            "buy_link": "https://book/1",
            "price_as_number": 400,
            "duration_seconds": 30000,
            "airline": "Delta",
        }
        monkeypatch.setattr(
            lambda_client_module.LambdaClient, "search", self._stub([row], status="ok")
        )
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert data["search_status"] == "ok"
        assert "partial" not in data

    @pytest.mark.asyncio
    async def test_a_degraded_date_in_a_range_is_not_hidden_by_the_others(
        self, server, monkeypatch
    ):
        """A fan-out is many independent searches. Recording one outcome per
        request, last writer wins, would let a healthy date bury a failed one.
        """
        seen = {"n": 0}
        row = {
            "buy_link": "https://book/1",
            "price_as_number": 400,
            "duration_seconds": 30000,
            "airline": "Delta",
        }

        async def search(self, endpoint, payload, *, outcome_sink=None, **_kwargs):
            seen["n"] += 1
            degraded = seen["n"] == 1
            if outcome_sink is not None:
                outcome_sink.append(
                    {"x-search-status": "degraded" if degraded else "ok"}
                )
            return [] if degraded else [row]

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", search)
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date_from": "2026-10-01",
            "departure_date_to": "2026-10-03",
        })
        assert data["result_count"] > 0
        assert data["search_status"] == "partial"
        assert "did not complete" in data["partial"]


class TestFallbackDefault:
    """`use_fallback` is tri-state upstream, and the default has to be absent.

    The backend reads the field as: true = run the fallback client inline on
    every attempt; false = never, last-resort escalation included; absent =
    escalate to it once, only after every retry for a combination has failed.
    These tools used to declare `use_fallback: bool = False`, which sent an
    explicit `false` on every call and excluded our own users from the
    escalation -- the exact opposite of the intent.
    """

    @pytest.mark.asyncio
    async def test_default_call_omits_the_key_entirely(self, server, stub_backend):
        await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert stub_backend
        assert "use_fallback" not in stub_backend[0][1]

    @pytest.mark.asyncio
    async def test_roundtrip_default_omits_it_too(self, server, stub_backend):
        await _call(server, "search_roundtrip_flights", {
            "from_airport": "TLV", "to_airport": "FCO",
            "departure_date": "2026-05-01", "return_date": "2026-05-08",
        })
        assert stub_backend
        assert "use_fallback" not in stub_backend[0][1]

    @pytest.mark.asyncio
    async def test_an_explicit_choice_still_reaches_the_backend(
        self, server, stub_backend
    ):
        """Absent is only the default; a caller who states a value keeps it."""
        for requested in (True, False):
            stub_backend.clear()
            await _call(server, "search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
                "use_fallback": requested,
            })
            assert stub_backend[0][1]["use_fallback"] is requested

    @pytest.mark.asyncio
    async def test_the_schema_offers_null_and_defaults_to_it(self, server):
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}

        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            schema = tools[name].inputSchema
            prop = schema["properties"]["use_fallback"]
            assert prop["default"] is None
            assert {"type": "null"} in prop["anyOf"]
            assert "use_fallback" not in schema.get("required", [])

    @pytest.mark.asyncio
    async def test_the_model_is_told_what_the_flag_actually_does(self, server):
        """FastMCP does not lift a docstring `Args:` entry into the schema, and
        these tools pass an explicit `description=` which overrides the
        docstring anyway -- so a parameter the model must reason about has to
        carry a `Field(description=...)`. The old text ("Wait longer on hard
        routes") was also simply wrong: it does not wait, it re-runs the search
        through a different client."""
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}

        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            text = tools[name].inputSchema["properties"]["use_fallback"]["description"]
            assert "Wait longer" not in text
            assert "Leave unset" in text
            assert "can time out" in text

    @pytest.mark.asyncio
    async def test_an_empty_result_does_not_recommend_the_inline_path(
        self, server, monkeypatch
    ):
        """Inline `true` hangs until the function's ``Timeout``, so the
        message a model reads on an empty result must not point at it."""
        async def empty(self, endpoint, payload, **_kwargs):
            return []

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", empty)
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "TLV", "to_airport": "CMB",
            "departure_date": "2026-10-14",
        })
        assert data["result_count"] == 0
        assert "set use_fallback to true" not in data["message"]
