"""The MCP structured-output contract: `outputSchema`, `structuredContent`, `isError`.

These tools always returned a JSON object and always carried a
`search_status` field. What they did not do was say so in the protocol. That
gap is what this file closes, and what it now guards.

Two spec mechanisms are involved, both read from the current revision,
**2026-07-28** (structured output itself landed in 2025-06-18, "Add support
for structured tool output", and is unchanged in substance since):

* `outputSchema` on the tool, `structuredContent` on the result.
  "Servers **MUST** provide structured results that conform to this schema.
  Clients **SHOULD** validate structured results against this schema."

* `isError: true` for a failed call. The spec puts "API failures" under
  Tool Execution Errors and says clients "**SHOULD** provide tool execution
  errors to language models to enable self-correction". Nothing in the spec
  obliges a host to show `structuredContent` to the model at all -- so a
  failure that lives only in the payload is a failure the model may never
  learn about. `search_status: "degraded"` was exactly that.

The backwards-compatibility duplicate is not optional for us:

  "For backwards compatibility, a tool that returns structured content
   SHOULD also return the serialized JSON in a TextContent block."

We have callers on clients that predate structured content, so the text
block is load-bearing rather than ceremonial.

The ad surface is the reason this server's schema is permissive rather than
tight. `LuluAdsMiddleware` adds a `sponsored` key to `structured_content`
*after* the tool returns, and rewrites the serialized text block to match.
Nothing in this package puts it there, so nothing here would have thought to
declare it -- and `additionalProperties: false` would have turned every
ad-carrying result into a validation failure on exactly the clients that
validate, with no test failing and no error logged. That case is
`test_an_ad_carrying_result_still_conforms`.
"""

import json
import threading
from http.server import HTTPServer

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator

from src import lambda_client as lambda_client_module
from src.output_schema import (
    FLIGHTS_OUTPUT_SCHEMA,
    HOTELS_OUTPUT_SCHEMA,
    SEARCH_STATUS_VALUES,
)
from src.server import build_server
from src.settings import load_settings
from tests.test_ads import _StubAds
from tests.test_server import _call, _call_result

FLIGHT_TOOLS = ("search_oneway_flights", "search_roundtrip_flights")
HOTEL_TOOLS = ("search_hotels", "find_hotel_by_name")

# The schema FastMCP infers from a bare `-> dict[str, Any]` annotation. It is
# a valid schema and tells a client nothing, which is the state this work
# replaced; if a tool ever falls back to it, the declaration was dropped.
INFERRED_PLACEHOLDER = {"type": "object", "additionalProperties": True}

ONEWAY_ARGS = {
    "from_airport": "TLV",
    "to_airport": "CMB",
    "departure_date": "2026-10-14",
}
ROUNDTRIP_ARGS = dict(ONEWAY_ARGS, return_date="2026-10-21")
ONE_FLIGHT = [
    {
        "buy_link": "https://book/x",
        "price_as_number": 412,
        "duration_seconds": 30000,
        "airline": "Delta",
    }
]


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Ads off, monitor mode, hotels configured.

    Declared here rather than imported: `server` lives in test_server.py,
    which is a module and not a conftest, so its fixtures are not shared.
    The hotels backend is configured because the hotel tools are only
    registered when it is, and their schemas are asserted on below.
    """
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    return build_server(load_settings())


def _stub_search(monkeypatch, rows, status=None, reason=None):
    """Stand in for LambdaClient.search, including the outcome headers it reads."""

    async def search(self, endpoint, payload, *, outcome_sink=None, **_kwargs):
        if outcome_sink is not None:
            entry = {}
            if status:
                entry["x-search-status"] = status
            if reason:
                entry["x-search-reason"] = reason
            outcome_sink.append(entry)
        return list(rows)

    monkeypatch.setattr(lambda_client_module.LambdaClient, "search", search)


def _tools(server):
    import anyio

    async def _list():
        return await server._list_tools()

    return {
        t.name: json.loads(t.to_mcp_tool().model_dump_json())
        for t in anyio.run(_list)
    }


def _check(payload, schema):
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda e: e.path
    )
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)


class TestTheSchemaIsDeclaredAndReal:
    """`tools/list` is where a client learns the result shape."""

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_every_tool_declares_an_output_schema(self, server, name):
        schema = _tools(server)[name].get("outputSchema")
        assert schema, f"{name} declares no outputSchema"
        assert schema != INFERRED_PLACEHOLDER, (
            f"{name} fell back to the schema FastMCP infers from "
            "`-> dict[str, Any]`, which declares nothing"
        )
        assert schema.get("properties"), f"{name} declares no properties"

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_every_declared_schema_is_itself_valid(self, server, name):
        # A malformed schema is not a test failure at import time -- FastMCP
        # accepts any object schema -- so it would ship and only break the
        # clients that actually validate.
        Draft202012Validator.check_schema(_tools(server)[name]["outputSchema"])

    @pytest.mark.parametrize("name", FLIGHT_TOOLS)
    def test_flight_tools_publish_the_search_status_vocabulary(self, server, name):
        status = _tools(server)[name]["outputSchema"]["properties"]["search_status"]
        assert status["enum"] == list(SEARCH_STATUS_VALUES)
        assert set(status["enum"]) == {
            "ok",
            "empty",
            "partial",
            "degraded",
            # The free server's per-client fair-use refusal. A status value
            # rather than an error because there is nothing here to retry.
            "rate_limited",
        }
        # The whole point of publishing it: a client can be told what an
        # empty array means without reading our documentation.
        assert "empty" in status["description"]

    @pytest.mark.parametrize("name", HOTEL_TOOLS)
    def test_hotel_tools_claim_only_the_status_they_can_produce(self, server, name):
        # The hotels backend sends no X-Search-Status header, so a hotel
        # result has no honest search vocabulary to publish. The one value
        # here is not the backend's: `rate_limited` is this server refusing
        # the call on its own fair-use counters, on a path that never reaches
        # a backend at all.
        status = _tools(server)[name]["outputSchema"]["properties"]["search_status"]
        assert status["enum"] == ["rate_limited"]

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_unknown_keys_are_permitted(self, server, name):
        # See the module docstring: the ad middleware appends `sponsored`
        # after we return, so this is a revenue assertion, not a style one.
        assert _tools(server)[name]["outputSchema"]["additionalProperties"] is True

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_only_results_is_required(self, server, name):
        # `results` is the one key every exit path carries -- the search, the
        # zero-result answer and the `blocked` refusal alike.
        assert _tools(server)[name]["outputSchema"]["required"] == ["results"]

    def test_the_widget_mapping_paths_are_declared(self, server):
        """The result widget resolves dot-paths against `structuredContent`.

        A schema that renamed or dropped one of these would take the
        rendered-impression beacon with it, which is the whole CPM.
        """
        props = _tools(server)["search_oneway_flights"]["outputSchema"]["properties"]
        assert "results" in props
        assert "search_coverage" in props
        assert "destinations_searched" in props["search_coverage"]["properties"]
        assert "sponsored" in props, "the injected ad field is undeclared"
        # The eyebrow moved off `search_coverage` onto its own computed
        # field when the fare band had to share that one line with it.
        assert "widget_eyebrow" in props
        assert "fare_band" in props


class TestDegradedIsAnError:
    """The design decision this file exists to pin down.

    A degraded search means every combination failed: there is no data, and
    an empty list is not an answer. That is a tool execution error, and the
    spec's channel for one is `isError`, which clients SHOULD pass to the
    model. `search_status` alone depended on a host choosing to surface
    `structuredContent`, which the spec never requires.
    """

    @pytest.mark.asyncio
    async def test_a_degraded_search_sets_is_error(self, server, monkeypatch):
        _stub_search(monkeypatch, [], status="degraded", reason="blocked_page")
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_a_degraded_search_keeps_its_payload(self, server, monkeypatch):
        """`isError` is a flag, not a reason to throw the result away.

        The coverage and the explanation are what stop a model saying "there
        are no flights", so losing them to a bare ToolError would undo the
        reason the header is read at all.
        """
        _stub_search(monkeypatch, [], status="degraded", reason="blocked_page")
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        payload = result.structured_content
        assert payload is not None, "the error dropped its structured content"
        assert payload["search_status"] == "degraded"
        assert payload["results"] == []
        assert payload["search_coverage"]["searched_combinations"] == 1
        assert "blocked_page" in payload["message"]

    @pytest.mark.asyncio
    async def test_a_degraded_error_still_serializes_json_into_a_text_block(
        self, server, monkeypatch
    ):
        """The backwards-compatibility duplicate, on the error path too.

        Callers on clients that predate structured content read `content[]`
        and nothing else.

        Since 2026-09-02 the JSON is the *second* block: the first is the
        plain-language warning (tests/test_loud_status.py). Hand-building the
        blocks is what makes the ad middleware decline to rewrite them, which
        is why SponsoredTextSyncMiddleware exists -- but a degraded result
        carries no ad at all, so on this path there is nothing to sync and
        the duplicate is simply the last block instead of the only one.
        """
        _stub_search(monkeypatch, [], status="degraded")
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 2
        assert not result.content[0].text.startswith("{"), "prose first"
        assert json.loads(result.content[-1].text) == result.structured_content

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_behaves_the_same_way(self, server, monkeypatch):
        _stub_search(monkeypatch, [], status="degraded")
        result = await _call_result(server, "search_roundtrip_flights", ROUNDTRIP_ARGS)
        assert result.is_error
        assert result.structured_content["search_status"] == "degraded"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,rows", [("ok", ONE_FLIGHT), ("empty", []), ("partial", ONE_FLIGHT)]
    )
    async def test_nothing_else_is_flagged_as_an_error(
        self, server, monkeypatch, status, rows
    ):
        """Only `degraded` is a failure.

        `empty` is a true negative -- the search ran and Google has nothing --
        and `partial` carries results the caller can use. Flagging either
        would throw away a real answer over a caveat, and would train a model
        to treat "no flights on that date" as a fault to retry.
        """
        _stub_search(monkeypatch, rows, status=status)
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert not result.is_error
        assert result.structured_content["search_status"] == status

    @pytest.mark.asyncio
    async def test_a_silent_backend_is_not_an_error(self, server, monkeypatch):
        # No header at all: a backend that predates it, or a hop that dropped
        # it. We do not know the search failed, so we must not claim it did.
        _stub_search(monkeypatch, [])
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert not result.is_error
        assert "search_status" not in result.structured_content


class TestEveryExitPathConformsToItsDeclaredSchema:
    """The spec's MUST, checked against real payloads rather than by eye."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,rows", [("ok", ONE_FLIGHT), ("empty", []), ("partial", ONE_FLIGHT)]
    )
    async def test_flight_search_results(self, server, monkeypatch, status, rows):
        _stub_search(monkeypatch, rows, status=status)
        _check(
            await _call(server, "search_oneway_flights", ONEWAY_ARGS),
            FLIGHTS_OUTPUT_SCHEMA,
        )

    @pytest.mark.asyncio
    async def test_a_degraded_result(self, server, monkeypatch):
        _stub_search(monkeypatch, [], status="degraded")
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        _check(result.structured_content, FLIGHTS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_a_blocked_refusal(self, tmp_path, monkeypatch):
        """The free tier's refusal is data, not an error, and it carries no
        `result_count` -- which is why `required` stops at `results`."""
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "false")
        monkeypatch.setenv("ENFORCEMENT_MODE", "enforce")
        monkeypatch.setenv("BLOCKED_TIERS", "unknown")
        blocking = build_server(load_settings())
        _stub_search(monkeypatch, ONE_FLIGHT)
        out = await _call(blocking, "search_oneway_flights", ONEWAY_ARGS)
        assert out["blocked"] is True
        _check(out, FLIGHTS_OUTPUT_SCHEMA)


class TestTheAdSurfaceStillConforms:
    """`sponsored` is injected after we return. The schema has to allow it."""

    @pytest.fixture
    def ads_endpoint(self):
        _StubAds.requests = []
        httpd = HTTPServer(("127.0.0.1", 0), _StubAds)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_port}"
        httpd.shutdown()

    @pytest.fixture
    def ad_server(self, ads_endpoint, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "true")
        monkeypatch.setenv("LULU_ADS_PUBLISHER_ID", "pub_test")
        monkeypatch.setenv("LULU_ADS_API_KEY", "lk_test")
        monkeypatch.setenv("LULU_ADS_BASE_URL", ads_endpoint)
        return build_server(load_settings())

    @pytest.mark.asyncio
    async def test_an_ad_carrying_result_still_conforms(self, ad_server, monkeypatch):
        _stub_search(monkeypatch, ONE_FLIGHT, status="ok")
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", ONEWAY_ARGS)
        payload = result.structured_content
        assert payload.get("sponsored"), "no sponsored field -- no revenue"
        # The assertion that matters: the ad the SDK appended does not make
        # the result violate the schema we now publish.
        _check(payload, FLIGHTS_OUTPUT_SCHEMA)

    @pytest.mark.asyncio
    async def test_the_ad_and_the_text_block_stay_in_sync(self, ad_server, monkeypatch):
        # The middleware only rewrites content[0] when it is the single
        # auto-generated TextContent. This is what proves we still hand it one.
        _stub_search(monkeypatch, ONE_FLIGHT, status="ok")
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == result.structured_content
        assert json.loads(result.content[0].text)["sponsored"]

    @pytest.mark.asyncio
    async def test_a_degraded_search_carries_no_ad(self, ad_server, monkeypatch):
        """No revenue moves with this change.

        The middleware skips any result whose `is_error` is set, and a
        degraded search has zero rows, which the existing
        `_has_no_substantive_data` suppression already covered. So the ad was
        never there to lose.
        """
        _stub_search(monkeypatch, [], status="degraded")
        async with Client(ad_server) as client:
            result = await client.call_tool(
                "search_oneway_flights", ONEWAY_ARGS, raise_on_error=False
            )
        assert result.is_error
        assert (result.structured_content or {}).get("sponsored") is None
        blob = json.dumps([c.model_dump(mode="json") for c in result.content])
        assert "getlulu.dev/c/" not in blob, "never advertise on an error"


class TestHotelResultsConform:
    @pytest.mark.asyncio
    async def test_a_hotel_search_result(self, server, monkeypatch):
        from src.hotels_lambda_client import HotelsLambdaClient

        async def fake(self, endpoint, payload):
            return [{"name": "Hotel Roma", "price_string": "$120"}]

        monkeypatch.setattr(HotelsLambdaClient, "search", fake, raising=True)
        out = await _call(
            server,
            "search_hotels",
            {
                "destination": "Rome",
                "checkin_date": "2026-10-14",
                "checkout_date": "2026-10-16",
            },
        )
        assert out["upgrade"], "the free tier always pitches the paid server"
        _check(out, HOTELS_OUTPUT_SCHEMA)


def _type_arrays(node, path="$"):
    """Every place a schema declares `"type": [...]` instead of `anyOf`."""
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), list):
            found.append(path)
        for key, value in node.items():
            found += _type_arrays(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _type_arrays(value, f"{path}[{i}]")
    return found


class TestOutputSchemaPortability:
    """A nullable field says `anyOf`, never a type ARRAY.

    Both forms are valid JSON Schema and mean the same thing, but the MCP
    Inspector flags a type array, and several client-side validators and code
    generators read only its first entry, which turns a legitimate null into
    what looks like a schema violation. This is a declaration change only:
    the wire format is byte for byte what it was.
    """

    def test_no_schema_declares_a_type_array(self):
        assert _type_arrays(FLIGHTS_OUTPUT_SCHEMA) == []
        assert _type_arrays(HOTELS_OUTPUT_SCHEMA) == []

    def test_cheapest_is_an_object_or_null(self):
        entry = FLIGHTS_OUTPUT_SCHEMA["properties"]["by_destination"][
            "additionalProperties"
        ]["properties"]
        assert entry["cheapest"]["anyOf"] == [
            {"type": "object", "additionalProperties": True},
            {"type": "null"},
        ]

    def test_cheapest_price_is_a_number_or_null(self):
        dates = FLIGHTS_OUTPUT_SCHEMA["properties"]["by_destination"][
            "additionalProperties"
        ]["properties"]["dates"]["additionalProperties"]["properties"]
        assert dates["cheapest_price"]["anyOf"] == [
            {"type": "number"},
            {"type": "null"},
        ]

    def test_the_hotel_stay_dates_are_a_string_or_null(self):
        coverage = HOTELS_OUTPUT_SCHEMA["properties"]["search_coverage"][
            "properties"
        ]
        for field in ("checkin_date", "checkout_date"):
            assert coverage[field]["anyOf"] == [
                {"type": "string"},
                {"type": "null"},
            ]

    @pytest.mark.parametrize("name", FLIGHT_TOOLS + HOTEL_TOOLS)
    def test_the_served_schema_is_the_same(self, server, name):
        """The schema a client actually reads, off `tools/list`."""
        assert _type_arrays(_tools(server)[name]["outputSchema"]) == []
