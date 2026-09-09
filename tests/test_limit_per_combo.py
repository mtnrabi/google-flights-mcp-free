"""`limit` must never hide a destination that was searched and answered.

The bug, found 2026-09-06 while building a guide page off one real call:

    search_oneway_flights(from_airport="BER",
                          to_airport=["BCN","LIS","ATH","IST","CDG"],
                          departure_date_from="2026-10-06",
                          departure_date_to="2026-10-08",
                          sort_by="price", limit=50)

Fifteen combinations ran, all fifteen answered, and the response carried
nothing for Lisbon -- because `limit` was a slice off one globally
price-sorted list and the 50th cheapest row was cheaper than every Lisbon
fare. `search_coverage.destinations_searched` still listed LIS. No error, no
flag, a response that reads as complete. The same class of mistake as
reading a bare `200 []` as "no flights".

The fix reserves each answering combination its own cheapest row before the
rest of `limit` is filled by price, so `results` is still ranked by price and
still `limit` long, and no searched combination silently disappears from it.
When `limit` is smaller than the number of answering combinations there is no
selection that can show them all, and the coverage says which are missing
rather than looking complete.
"""

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.server import (
    MAX_AUTO_LIMIT,
    _effective_limit,
    _note_hidden_combinations,
    _select_rows,
    build_server,
)
from src.settings import load_settings

# The real search from the guide, with one destination made the expensive one.
DESTINATIONS = ["BCN", "LIS", "ATH", "IST", "CDG"]
DATES = ["2026-10-06", "2026-10-07", "2026-10-08"]
# Lisbon is the priciest by a wide margin -- that is the whole scenario. The
# others are roughly the fares the real call returned.
BASE_PRICE = {"CDG": 43, "BCN": 70, "ATH": 86, "IST": 105, "LIS": 900}
ROWS_PER_COMBO = 4
TOTAL_COMBOS = len(DESTINATIONS) * len(DATES)
TOTAL_ROWS = TOTAL_COMBOS * ROWS_PER_COMBO


@pytest.fixture
def priced_backend(monkeypatch):
    """Four rows per combination, priced by destination."""

    async def fake_search(self, endpoint, payload, **_kwargs):
        dest = payload["to_airport"]
        day = payload["departure_date"]
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

    monkeypatch.setattr(
        lambda_client_module.LambdaClient, "search", fake_search, raising=True
    )


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


ONEWAY_ARGS = {
    "from_airport": "BER",
    "to_airport": DESTINATIONS,
    "departure_date_from": DATES[0],
    "departure_date_to": DATES[-1],
    "sort_by": "price",
}


class TestTheLisbonCase:
    @pytest.mark.asyncio
    async def test_every_searched_destination_has_a_row(
        self, server, priced_backend
    ):
        """The regression itself. `limit` is 20 of 60 rows, and the twenty
        cheapest are all Paris and Barcelona -- so before the fix Athens,
        Istanbul and Lisbon were all searched, all answered, and all absent.
        """
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        assert data["result_count"] == 20
        shown = {row["to_airport"] for row in data["results"]}
        assert shown == set(DESTINATIONS)
        assert set(data["search_coverage"]["destinations_searched"]) == shown, (
            "a destination named in the coverage and missing from the results "
            "is exactly the bug"
        )

    @pytest.mark.asyncio
    async def test_the_expensive_destination_gets_one_row_per_date(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        lisbon = [row for row in data["results"] if row["to_airport"] == "LIS"]
        assert len(lisbon) == len(DATES)
        assert {row["departure_date"] for row in lisbon} == set(DATES)

    @pytest.mark.asyncio
    async def test_results_are_still_sorted_by_price(self, server, priced_backend):
        """The guarantee reserves rows; it does not reorder them."""
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        prices = [row["price_as_number"] for row in data["results"]]
        assert prices == sorted(prices)
        assert prices[0] == BASE_PRICE["CDG"], "the cheapest fare still leads"

    @pytest.mark.asyncio
    async def test_the_rest_of_limit_still_goes_to_the_cheapest(
        self, server, priced_backend
    ):
        """One row per combination is a floor, not a quota. Fifteen rows are
        reserved and the other five go to the cheapest fares left, which are
        Paris's -- so Paris ends up with more rows than anyone else."""
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        counts: dict[str, int] = {}
        for row in data["results"]:
            counts[row["to_airport"]] = counts.get(row["to_airport"], 0) + 1
        assert counts["CDG"] == 8
        assert counts["LIS"] == 3

    @pytest.mark.asyncio
    async def test_a_generous_limit_returns_everything(self, server, priced_backend):
        data = await _call(
            server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": TOTAL_ROWS}
        )

        assert data["result_count"] == TOTAL_ROWS
        assert data["search_coverage"]["truncated"] is False
        assert "note" not in data["search_coverage"]

    @pytest.mark.asyncio
    async def test_roundtrip_has_the_same_guarantee(self, server, priced_backend):
        """`search_roundtrip_flights` merges and slices through the same
        code path, so it had the same bug."""
        data = await _call(server, "search_roundtrip_flights", {
            "from_airport": "BER",
            "to_airport": DESTINATIONS,
            "departure_date_from": DATES[0],
            "departure_date_to": DATES[-1],
            "nights": 3,
            "sort_by": "price",
            "limit": 20,
        })

        assert {row["to_airport"] for row in data["results"]} == set(DESTINATIONS)


class TestLimitSmallerThanTheFanout:
    """A `limit` that cannot cover the fan-out is fixed before the search.

    The per-combination floor made a too-small `limit` visible instead of
    silent, and visible was as far as it went: fifteen searches ran, four
    rows came back, and the response explained which eleven combinations had
    no room. A reader of the 2026-09-08 r/AI_Agents post put the obvious
    question -- why is that discovered in the response at all? The number of
    combinations is known before a single search runs.

    So it is not discovered any more. `limit` is raised to cover the fan-out
    up front and `search_coverage.note` says so. Nothing is hidden, so
    nothing is reported hidden; the naming path below is exercised directly
    in TestSelectRows, where a caller-side floor can still bite.
    """

    @pytest.mark.asyncio
    async def test_limit_is_raised_to_cover_every_combination(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        assert data["result_count"] == TOTAL_COMBOS
        assert {row["to_airport"] for row in data["results"]} == set(DESTINATIONS)

    @pytest.mark.asyncio
    async def test_the_note_says_what_was_done_and_why(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        note = data["search_coverage"]["note"]
        assert "`limit` was 4" in note
        assert f"raised to {TOTAL_COMBOS}" in note

    @pytest.mark.asyncio
    async def test_nothing_is_hidden_so_nothing_is_called_truncated(
        self, server, priced_backend
    ):
        """`truncated` means the answer does not cover the request. After the
        raise it does, and saying otherwise would be the mirror image of the
        original bug."""
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 4})

        assert data["search_coverage"]["truncated"] is False
        assert "have no row" not in data["search_coverage"]["note"]

    @pytest.mark.asyncio
    async def test_a_limit_that_already_covers_the_fanout_is_left_alone(
        self, server, priced_backend
    ):
        data = await _call(server, "search_oneway_flights", {**ONEWAY_ARGS, "limit": 20})

        assert data["result_count"] == 20
        assert "note" not in data["search_coverage"]

    def test_the_raise_never_lowers_an_explicit_limit(self):
        assert _effective_limit(200, TOTAL_COMBOS) == (200, None)

    def test_the_raise_is_bounded(self):
        """A caller cannot turn `limit: 10` into an unbounded response by
        widening the fan-out."""
        raised, note = _effective_limit(10, 500)

        assert raised == MAX_AUTO_LIMIT
        assert f"raised to {MAX_AUTO_LIMIT}" in note

    def test_a_single_combination_needs_no_raise(self):
        assert _effective_limit(1, 1) == (1, None)


class TestSelectRows:
    """The selection on its own, away from the server."""

    @staticmethod
    def _group(dest: str, *prices: int):
        return (
            {"departure_date": "2026-10-06", "to_airport": dest},
            [
                {
                    "buy_link": f"https://book/{dest}/{price}",
                    "price_as_number": price,
                    "to_airport": dest,
                }
                for price in prices
            ],
        )

    def test_no_groups_is_no_rows(self):
        assert _select_rows([], "price", 10) == ([], [])

    def test_a_combination_that_found_nothing_is_not_reported_hidden(self):
        groups = [
            self._group("CDG", 40, 50),
            ({"departure_date": "2026-10-06", "to_airport": "LIS"}, []),
        ]
        rows, hidden = _select_rows(groups, "price", 1)
        assert [row["price_as_number"] for row in rows] == [40]
        assert hidden == [], "an empty search has no row to hide"

    def test_a_fare_returned_by_two_combinations_counts_once(self):
        shared = {
            "buy_link": "https://book/same",
            "price_as_number": 40,
            "to_airport": "CDG",
        }
        groups = [
            ({"departure_date": "2026-10-06", "to_airport": "CDG"}, [shared]),
            ({"departure_date": "2026-10-07", "to_airport": "CDG"}, [dict(shared)]),
        ]
        rows, hidden = _select_rows(groups, "price", 10)
        assert len(rows) == 1
        assert hidden == [], (
            "the second combination has no row of its own left to show, and "
            "its fare is on the answer under the first -- not a silent drop"
        )

    def test_zero_limit_returns_nothing_and_claims_nothing(self):
        assert _select_rows([self._group("CDG", 40)], "price", 0) == ([], [])

    def test_duration_sort_reserves_the_shortest_per_combination(self):
        groups = [
            (
                {"departure_date": "2026-10-06", "to_airport": "CDG"},
                [
                    {"buy_link": "a", "duration_seconds": 100},
                    {"buy_link": "b", "duration_seconds": 200},
                ],
            ),
            (
                {"departure_date": "2026-10-06", "to_airport": "LIS"},
                [{"buy_link": "c", "duration_seconds": 900}],
            ),
        ]
        rows, hidden = _select_rows(groups, "duration", 2)
        assert [row["buy_link"] for row in rows] == ["a", "c"]
        assert hidden == []

    def test_a_limit_below_the_answering_combinations_names_them(self):
        """The tool no longer reaches this -- the raise happens first -- but
        the floor is a general function and one row per combination is not
        free. Kept because it is the last line of defence if a future caller
        selects rows with a limit of its own."""
        groups = [
            self._group("CDG", 40),
            self._group("BCN", 70),
            self._group("LIS", 900),
        ]
        rows, hidden = _select_rows(groups, "price", 2)

        assert [row["to_airport"] for row in rows] == ["CDG", "BCN"]
        assert hidden == ["2026-10-06 to LIS"]

    def test_the_note_for_hidden_combinations_still_reads_correctly(self):
        coverage = {}
        _note_hidden_combinations(coverage, ["2026-10-06 to LIS"], 2)

        assert coverage["truncated"] is True
        assert "`limit` was 2" in coverage["note"]
        assert "1 of them have no row" in coverage["note"]
        assert "2026-10-06 to LIS" in coverage["note"]

    def test_rows_with_no_price_still_sort_last_and_still_show(self):
        groups = [
            self._group("CDG", 40),
            (
                {"departure_date": "2026-10-06", "to_airport": "LIS"},
                [{"buy_link": "https://book/null", "price_as_number": None}],
            ),
        ]
        rows, hidden = _select_rows(groups, "price", 2)
        assert [row["buy_link"] for row in rows] == [
            "https://book/CDG/40",
            "https://book/null",
        ]
        assert hidden == []
