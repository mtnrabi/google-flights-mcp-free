"""Every destination the caller asked for has an entry, empty or not.

Where this came from: the 2026-09-06 fix stopped `limit` from silently
dropping a searched destination out of `results`, and reported what it could
not show in `search_coverage.truncated` plus a note. A reader of our
2026-09-08 r/AI_Agents post made the point that a boolean is one more field a
model can read and ignore -- the stronger fix is a response SHAPE that has
nowhere to hide the hole. One entry per requested destination, and per
requested date on a multi-date search, so an empty entry is something the
model has to walk past on its way to the answer rather than an absence it
never notices.

The four ways a destination can have no rows are four different facts:

    no_flights    -- searched, answered, Google has nothing
    search_failed -- searched and the search errored; nothing is known
    not_in_limit  -- searched, found flights, none fitted in `limit`
    not_searched  -- never searched; the fan-out cap sampled it away

Told apart here, because "no flights to Lisbon" and "we never looked at
Lisbon" are the same silence in a response that only carries `results`.
"""

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.fanout import SearchPlan
from src.server import _by_destination, build_server
from src.settings import load_settings

DESTINATIONS = ["BCN", "LIS", "ATH", "IST", "CDG"]
DATES = ["2026-10-06", "2026-10-07", "2026-10-08"]
# Lisbon is the priciest by a wide margin -- the original scenario, where a
# small `limit` swallowed it whole.
BASE_PRICE = {"CDG": 43, "BCN": 70, "ATH": 86, "IST": 105, "LIS": 900}
ROWS_PER_COMBO = 4
TOTAL_COMBOS = len(DESTINATIONS) * len(DATES)

ONEWAY_ARGS = {
    "from_airport": "BER",
    "to_airport": DESTINATIONS,
    "departure_date_from": DATES[0],
    "departure_date_to": DATES[-1],
    "sort_by": "price",
}


def _rows(dest: str, day: str) -> list[dict]:
    base = BASE_PRICE[dest]
    return [
        {
            "buy_link": f"https://book/{dest}/{day}/{offset}",
            "price": f"${base + offset}",
            "price_as_number": base + offset,
            "duration_seconds": 10000 + offset,
            "to_airport": dest,
            "departure_date": day,
            "airline": "Test Air",
        }
        for offset in range(ROWS_PER_COMBO)
    ]


def _backend(monkeypatch, *, empty: set[str] = frozenset(), failing: set[str] = frozenset()):
    async def fake_search(self, endpoint, payload, **_kwargs):
        dest = payload["to_airport"]
        if dest in failing:
            raise RuntimeError(f"upstream refused {dest}")
        if dest in empty:
            return []
        return _rows(dest, payload["departure_date"])

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )


@pytest.fixture
def priced_backend(monkeypatch):
    _backend(monkeypatch)


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


class TestTheFixedShape:
    """5 destinations x 3 dates, one priciest, a small `limit`."""

    @pytest.mark.asyncio
    async def test_every_requested_destination_has_an_entry(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        assert list(data["by_destination"]) == DESTINATIONS, (
            "request order, not the order results came back in"
        )

    @pytest.mark.asyncio
    async def test_the_expensive_destination_is_not_an_empty_entry(
        self, server, priced_backend
    ):
        """The regression, read off the new shape: LIS is the one a `limit`
        of 4 used to drop entirely."""
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        lisbon = data["by_destination"]["LIS"]
        assert lisbon["searched"] is True
        assert lisbon["reason"] == "ok"
        assert lisbon["rows"], "searched, answered, and it has rows"
        assert lisbon["cheapest"]["price_as_number"] == BASE_PRICE["LIS"]

    @pytest.mark.asyncio
    async def test_entry_rows_are_the_rows_in_results(self, server, priced_backend):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        regrouped = [
            row
            for entry in data["by_destination"].values()
            for row in entry["rows"]
        ]
        assert len(regrouped) == data["result_count"]
        assert {row["buy_link"] for row in regrouped} == {
            row["buy_link"] for row in data["results"]
        }

    @pytest.mark.asyncio
    async def test_rows_carry_the_book_label_results_rows_carry(
        self, server, priced_backend
    ):
        """`_annotate_book_labels` copies rather than mutating, so a grouping
        built from the pre-annotation objects would quietly ship rows the
        widget cannot render a Book cell for."""
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        for entry in data["by_destination"].values():
            for row in entry["rows"]:
                assert "book_label" in row

    @pytest.mark.asyncio
    async def test_cheapest_is_the_cheapest_of_that_destination(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 60})

        for dest, entry in data["by_destination"].items():
            prices = [row["price_as_number"] for row in entry["rows"]]
            assert entry["cheapest"]["price_as_number"] == min(prices)
            assert min(prices) == BASE_PRICE[dest]

    @pytest.mark.asyncio
    async def test_roundtrip_answers_in_the_same_shape(self, server, priced_backend):
        data = await _call(server, "search_roundtrip_flights", {
            "from_airport": "BER",
            "to_airport": DESTINATIONS,
            "departure_date_from": DATES[0],
            "departure_date_to": DATES[-1],
            "nights": 3,
            "sort_by": "price",
            "limit": 4,
        })

        assert list(data["by_destination"]) == DESTINATIONS


class TestTheHoles:
    @pytest.mark.asyncio
    async def test_a_destination_with_no_flights_is_present_and_says_so(
        self, server, monkeypatch
    ):
        _backend(monkeypatch, empty={"LIS"})
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        lisbon = data["by_destination"]["LIS"]
        assert lisbon["rows"] == []
        assert lisbon["cheapest"] is None
        assert lisbon["searched"] is True
        assert lisbon["reason"] == "no_flights"
        assert "LIS" not in {row["to_airport"] for row in data["results"]}, (
            "the point: results alone cannot tell this from 'never looked'"
        )

    @pytest.mark.asyncio
    async def test_a_destination_whose_searches_failed_says_so(
        self, server, monkeypatch
    ):
        _backend(monkeypatch, failing={"ATH"})
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        athens = data["by_destination"]["ATH"]
        assert athens["rows"] == []
        assert athens["searched"] is True
        assert athens["reason"] == "search_failed", (
            "an errored search knows nothing about the route -- reporting it "
            "as 'no flights' is the 200-empty-list mistake again"
        )

    @pytest.mark.asyncio
    async def test_an_empty_and_a_failed_destination_are_told_apart(
        self, server, monkeypatch
    ):
        _backend(monkeypatch, empty={"LIS"}, failing={"ATH"})
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        assert data["by_destination"]["LIS"]["reason"] == "no_flights"
        assert data["by_destination"]["ATH"]["reason"] == "search_failed"


class TestPerDate:
    @pytest.mark.asyncio
    async def test_a_multi_date_search_breaks_each_destination_down_by_date(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        for entry in data["by_destination"].values():
            assert list(entry["dates"]) == DATES

    @pytest.mark.asyncio
    async def test_a_date_entry_carries_its_own_cheapest_price(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 60})

        for dest, entry in data["by_destination"].items():
            for day, day_entry in entry["dates"].items():
                assert day_entry["searched"] is True
                assert day_entry["reason"] == "ok"
                assert day_entry["row_count"] == ROWS_PER_COMBO
                assert day_entry["cheapest_price"] == BASE_PRICE[dest]

    @pytest.mark.asyncio
    async def test_a_single_date_search_has_no_date_breakdown(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {
            "from_airport": "BER",
            "to_airport": DESTINATIONS,
            "departure_date": DATES[0],
            "sort_by": "price",
            "limit": 20,
        })

        for entry in data["by_destination"].values():
            assert "dates" not in entry, "one date is not a breakdown"


class TestByDestinationDirectly:
    """The cases the fan-out cap produces, built by hand.

    A destination sampled away needs a plan whose cap bit, and the free
    server's cap is decided by policy and the daily budget rather than by a
    tool argument -- so it is constructed here instead of provoked through a
    tool call.
    """

    @staticmethod
    def _plan(executed: list[tuple[str, str]]) -> SearchPlan:
        requested = [
            {"departure_date": day, "to_airport": dest}
            for day in DATES[:2]
            for dest in ["CDG", "LIS"]
        ]
        return SearchPlan(
            endpoint="oneway",
            combos=[
                {"departure_date": day, "to_airport": dest}
                for dest, day in executed
            ],
            requested_combinations=len(requested),
            cap=len(executed),
            requested_combos=requested,
        )

    def test_a_destination_the_cap_dropped_is_not_searched(self):
        plan = self._plan([("CDG", DATES[0]), ("CDG", DATES[1])])
        row = {"buy_link": "a", "price_as_number": 40}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [row]),
            ({"departure_date": DATES[1], "to_airport": "CDG"}, []),
        ]

        by_dest = _by_destination(plan, groups, [], [row], [0])

        assert by_dest["LIS"]["searched"] is False
        assert by_dest["LIS"]["reason"] == "not_searched"
        assert by_dest["LIS"]["rows"] == []
        assert by_dest["CDG"]["reason"] == "ok"

    def test_a_date_the_cap_dropped_is_not_searched(self):
        plan = self._plan([("CDG", DATES[0]), ("LIS", DATES[0])])
        row = {"buy_link": "a", "price_as_number": 40}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [row]),
            ({"departure_date": DATES[0], "to_airport": "LIS"}, []),
        ]

        by_dest = _by_destination(plan, groups, [], [row], [0])

        assert by_dest["CDG"]["dates"][DATES[0]]["reason"] == "ok"
        assert by_dest["CDG"]["dates"][DATES[1]]["reason"] == "not_searched"
        assert by_dest["CDG"]["dates"][DATES[1]]["searched"] is False
        assert by_dest["LIS"]["dates"][DATES[0]]["reason"] == "no_flights"
        assert by_dest["LIS"]["searched"] is True, (
            "one of its dates ran -- 'searched' is about the destination"
        )

    def test_a_destination_that_found_flights_and_missed_the_limit(self):
        """`not_in_limit` is unreachable through the tool now that `limit` is
        raised up front, and it is still the honest answer if a selection
        ever runs with a limit of its own."""
        plan = self._plan([("CDG", DATES[0]), ("LIS", DATES[0])])
        cheap = {"buy_link": "a", "price_as_number": 40}
        dear = {"buy_link": "b", "price_as_number": 900}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [cheap]),
            ({"departure_date": DATES[0], "to_airport": "LIS"}, [dear]),
        ]

        by_dest = _by_destination(plan, groups, [], [cheap], [0])

        assert by_dest["LIS"]["reason"] == "not_in_limit"
        assert by_dest["LIS"]["searched"] is True

    def test_a_failed_combination_is_not_reported_as_empty(self):
        plan = self._plan([("CDG", DATES[0]), ("LIS", DATES[0])])
        row = {"buy_link": "a", "price_as_number": 40}
        failed = {"departure_date": DATES[0], "to_airport": "LIS"}
        groups = [({"departure_date": DATES[0], "to_airport": "CDG"}, [row])]

        by_dest = _by_destination(plan, groups, [failed], [row], [0])

        assert by_dest["LIS"]["reason"] == "search_failed"

    def test_rows_stay_in_results_order_within_a_destination(self):
        plan = self._plan([("CDG", DATES[0]), ("CDG", DATES[1])])
        first = {"buy_link": "a", "price_as_number": 40}
        second = {"buy_link": "b", "price_as_number": 50}
        groups = [
            ({"departure_date": DATES[0], "to_airport": "CDG"}, [second]),
            ({"departure_date": DATES[1], "to_airport": "CDG"}, [first]),
        ]

        by_dest = _by_destination(plan, groups, [], [first, second], [1, 0])

        assert [row["buy_link"] for row in by_dest["CDG"]["rows"]] == ["a", "b"]
