"""One search, three ways of writing the destination list.

Verified live on 2026-09-06, against main after #444:

    free  search_oneway_flights(to_airport="BCN,LIS,ATH")  -> fanned out, 3 searches
    paid  search_oneway_flights(to_airport="BCN,LIS,ATH")  -> ToolError,
        "Not valid airport codes: BCN,LIS,ATH. Use three-letter IATA codes"

Same tool name, same argument name, same intent, opposite outcome. A model
that had learned this server's shape failed on the paid one, and the refusal
named the string the model had just written as the problem -- so the retry
wrote it again.

This file is the free-server half of the fix; the paid half is
mcp_server_paid/tests/test_airport_shapes.py and the two servers now share the
same splitter (`src/fanout.py`, kept byte-comparable between the packages).
What this server does NOT do is reject a code it dislikes: it never has, the
Lambda is mrabi's own cost rather than a caller's billed request, and a
several-shapes change is not the place to start refusing searches. The paid
server's guard is unchanged -- see mcp_server_paid/tests/test_airport_validation.py.
"""

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.fanout import (
    PlanError,
    normalise_airport_codes,
    normalise_destinations,
    normalise_origin,
    plan_oneway,
    plan_roundtrip,
    split_airport_codes,
)
from src.server import _airports_field, build_server
from src.settings import load_settings


class TestSplitAirportCodes:
    """The one helper both servers normalise through."""

    def test_a_single_code(self):
        assert split_airport_codes("BCN") == ["BCN"]

    def test_a_comma_separated_string(self):
        assert split_airport_codes("BCN,LIS,ATH") == ["BCN", "LIS", "ATH"]

    def test_commas_with_spaces(self):
        assert split_airport_codes("BCN, LIS,  ATH") == ["BCN", "LIS", "ATH"]

    def test_spaces_alone(self):
        assert split_airport_codes("BCN LIS ATH") == ["BCN", "LIS", "ATH"]

    def test_semicolons_and_pipes(self):
        assert split_airport_codes("BCN;LIS|ATH") == ["BCN", "LIS", "ATH"]

    def test_a_json_list(self):
        assert split_airport_codes(["BCN", "LIS"]) == ["BCN", "LIS"]

    def test_a_list_whose_elements_are_themselves_strings_of_codes(self):
        assert split_airport_codes(["BCN,LIS", "ATH"]) == ["BCN", "LIS", "ATH"]

    def test_case_is_preserved_here(self):
        assert split_airport_codes("bcn,Lis") == ["bcn", "Lis"]

    def test_nothing_is_nothing(self):
        assert split_airport_codes(None) == []
        assert split_airport_codes("") == []
        assert split_airport_codes("   ") == []
        assert split_airport_codes([]) == []

    def test_a_phrase_is_not_split_on_its_spaces(self):
        """"Tel Aviv" is one value, not the two values TEL and AVIV.

        Whitespace separates only when every piece already looks like a code.
        This server passes an unrecognised value through to the Lambda rather
        than refusing it, so silently turning one bad value into two would
        change what gets searched, not just what an error says.
        """
        assert split_airport_codes("Tel Aviv") == ["Tel Aviv"]
        assert split_airport_codes("Tel Aviv (TLV)") == ["Tel Aviv (TLV)"]


class TestNormaliseAirportCodes:
    def test_upper_cased(self):
        assert normalise_airport_codes("bcn,lis") == ["BCN", "LIS"]

    def test_mixed_case_and_padding(self):
        assert normalise_airport_codes(" bcn , LiS ") == ["BCN", "LIS"]

    def test_duplicates_drop_and_order_is_the_callers(self):
        assert normalise_airport_codes("LIS,bcn,LIS,ATH") == ["LIS", "BCN", "ATH"]

    def test_a_code_this_server_does_not_judge(self):
        """No validation here: `X00` is what test_attribution's sweep sends."""
        assert normalise_airport_codes("X00,X01") == ["X00", "X01"]


class TestOriginShapes:
    def test_a_plain_code(self):
        assert normalise_origin("tlv") == "TLV"

    def test_padded(self):
        assert normalise_origin("  TLV ") == "TLV"

    def test_the_same_code_twice_is_still_one_origin(self):
        assert normalise_origin("TLV,tlv") == "TLV"

    def test_two_origins_are_refused_by_name(self):
        """Before this the string was upper-cased and sent whole, and the
        Lambda answered `200 []` -- which reads as "no flights"."""
        with pytest.raises(PlanError) as exc:
            normalise_origin("TLV,JFK")
        assert "one origin airport per search" in str(exc.value)
        assert "TLV, JFK" in str(exc.value)

    def test_a_missing_origin_is_named(self):
        with pytest.raises(PlanError):
            normalise_origin("")


class TestDestinationShapes:
    def test_empty_still_raises(self):
        with pytest.raises(PlanError):
            normalise_destinations("")
        with pytest.raises(PlanError):
            normalise_destinations([])

    def test_the_planner_takes_every_shape(self):
        as_string = plan_oneway(
            from_airport="TLV",
            to_airport="BCN,LIS,ATH",
            departure_date="2026-10-14",
            cap=15,
        )
        as_spaces = plan_oneway(
            from_airport="TLV",
            to_airport="bcn lis ath",
            departure_date="2026-10-14",
            cap=15,
        )
        as_list = plan_oneway(
            from_airport="TLV",
            to_airport=["BCN", "LIS", "ATH"],
            departure_date="2026-10-14",
            cap=15,
        )
        assert as_string.combos == as_list.combos == as_spaces.combos
        assert [c["to_airport"] for c in as_list.combos] == ["BCN", "LIS", "ATH"]

    def test_the_roundtrip_planner_too(self):
        plan = plan_roundtrip(
            from_airport="tlv",
            to_airport="bcn, lis",
            departure_date="2026-10-14",
            nights=5,
            cap=15,
        )
        assert [c["to_airport"] for c in plan.combos] == ["BCN", "LIS"]

    def test_a_multi_origin_plan_is_refused(self):
        with pytest.raises(PlanError):
            plan_oneway(
                from_airport="TLV,JFK",
                to_airport="BCN",
                departure_date="2026-10-14",
                cap=15,
            )

    def test_the_cap_is_not_touched(self):
        """A comma string is planned exactly like the list it means -- the
        fan-out cap still bites at the same point."""
        plan = plan_oneway(
            from_airport="TLV",
            to_airport=",".join(f"D{n:02d}" for n in range(20)),
            departure_date="2026-10-14",
            cap=15,
        )
        assert plan.requested_combinations == 20
        assert plan.executed_combinations == 15
        assert plan.truncated


class TestTheRouteFieldInTheLog:
    """Rule 11: every backend caller is attributable and counted. A comma
    string used to be logged as one 11-character route key."""

    def test_a_comma_string_is_three_destinations(self):
        assert _airports_field("BCN,LIS,ATH") == "BCN,LIS,ATH"

    def test_it_upper_cases_and_truncates_like_a_list(self):
        assert _airports_field("bcn lis ath ist cdg") == "BCN,LIS,ATH+2"

    def test_a_list_is_unchanged(self):
        assert _airports_field(["jfk", "BOS"]) == "JFK,BOS"

    def test_nothing_is_a_dash(self):
        assert _airports_field("") == "-"
        assert _airports_field(None) == "-"


@pytest.fixture
def recorded(monkeypatch):
    """Every payload the fan-out sends, in plan order."""
    seen: list[dict] = []

    async def fake_search(self, endpoint, payload, **_kwargs):
        seen.append(payload)
        return []

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )
    return seen


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    return build_server(load_settings())


async def _call(server, tool, args):
    async with Client(server) as client:
        await client.call_tool(tool, args)


BASE = {"from_airport": "TLV", "departure_date": "2026-10-14"}


class TestTheToolsThemselves:
    @pytest.mark.asyncio
    async def test_a_comma_string_fans_out(self, server, recorded):
        await _call(server, "search_oneway_flights", {**BASE, "to_airport": "BCN,LIS,ATH"})
        assert [p["to_airport"] for p in recorded] == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_a_space_separated_string_fans_out(self, server, recorded):
        await _call(server, "search_oneway_flights", {**BASE, "to_airport": "BCN LIS ATH"})
        assert [p["to_airport"] for p in recorded] == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_a_list_still_fans_out(self, server, recorded):
        await _call(
            server, "search_oneway_flights", {**BASE, "to_airport": ["BCN", "LIS", "ATH"]}
        )
        assert [p["to_airport"] for p in recorded] == ["BCN", "LIS", "ATH"]

    @pytest.mark.asyncio
    async def test_one_code_is_one_search(self, server, recorded):
        await _call(server, "search_oneway_flights", {**BASE, "to_airport": "BCN"})
        assert [p["to_airport"] for p in recorded] == ["BCN"]

    @pytest.mark.asyncio
    async def test_mixed_case_reaches_the_backend_upper_cased(self, server, recorded):
        await _call(server, "search_oneway_flights", {**BASE, "to_airport": "bcn, Lis"})
        assert [p["to_airport"] for p in recorded] == ["BCN", "LIS"]

    @pytest.mark.asyncio
    async def test_the_origin_is_normalised_too(self, server, recorded):
        await _call(
            server,
            "search_oneway_flights",
            {"from_airport": "  tlv ", "to_airport": "BCN",
             "departure_date": "2026-10-14"},
        )
        assert recorded[0]["from_airport"] == "TLV"

    @pytest.mark.asyncio
    async def test_two_origins_are_refused_before_any_backend_call(
        self, server, recorded
    ):
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc:
            await _call(
                server,
                "search_oneway_flights",
                {"from_airport": "TLV,JFK", "to_airport": "BCN",
                 "departure_date": "2026-10-14"},
            )
        assert "one origin airport per search" in str(exc.value)
        assert recorded == []

    @pytest.mark.asyncio
    async def test_an_unrecognised_destination_is_still_searched_as_written(
        self, server, recorded
    ):
        """This server does not judge codes; it must also not mangle one.
        `Tel Aviv` goes to the Lambda as one value, upper-cased, not as two."""
        await _call(server, "search_oneway_flights", {**BASE, "to_airport": "Tel Aviv"})
        assert [p["to_airport"] for p in recorded] == ["TEL AVIV"]

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_behaves_the_same(self, server, recorded):
        await _call(
            server,
            "search_roundtrip_flights",
            {"from_airport": "tlv", "to_airport": "bcn;lis",
             "departure_date": "2026-10-14", "nights": 5},
        )
        assert [p["to_airport"] for p in recorded] == ["BCN", "LIS"]
        assert {p["from_airport"] for p in recorded} == {"TLV"}


class TestTheSchemaSaysSo:
    """A model that cannot see the shapes in the schema keeps guessing."""

    @pytest.mark.asyncio
    async def test_both_tools_document_all_three_shapes(self, server):
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            described = tools[name].inputSchema["properties"]["to_airport"][
                "description"
            ]
            assert "commas" in described
            assert "list" in described

    @pytest.mark.asyncio
    async def test_the_origin_stays_a_single_string_in_the_schema(self, server):
        """Deliberate: the fan-out is planned over dates and destinations
        only, so advertising `from_airport` as string-or-array would invite a
        call the server has to refuse."""
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            spec = tools[name].inputSchema["properties"]["from_airport"]
            assert spec.get("type") == "string", spec
