"""The first line of the text block, pinned per status -- ads included.

Same change as the paid server (mcp_server_paid/tests/test_loud_status.py),
plus the one thing only this server has: a sponsored card on the same result.

u/lulu_dev, r/mcp comment p7exhmy, 2026-09-02: "make the degraded case read as
alarming in plain language, not just present as a quiet field ... Schema for
the clients that read it, loud natural language for the ones that don't."

`partial` is the case that matters most. `degraded` carries `isError: true`,
so a spec-following host already has something to flag. A partial result has
real rows and `isError` false on purpose, because those rows ARE usable --
which leaves a model every reason to read the rows it got as the whole answer
while part of the requested range was never scraped.

The ad-ordering tests are the other half. A sponsored card sitting in front of
"this search did not complete" would be the exact failure this change exists
to prevent, and the Lulu SDK reserves the right to rewrite the text block
under us -- so both the ordering and the ad/JSON sync are asserted on the
rendered content, not reasoned about.
"""

import json
import threading
from http.server import HTTPServer

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src.server import build_server
from src.settings import load_settings
from src.status_text import DEGRADED_FIRST_LINE, MAX_NAMED_COMBINATIONS
from tests.test_ads import _StubAds
from tests.test_structured_output import ONE_FLIGHT, ONEWAY_ARGS, server  # noqa: F401
from tests.test_server import _call_result

RANGE_ARGS = {
    "from_airport": "TLV",
    "to_airport": "CMB",
    "departure_date_from": "2026-10-14",
    "departure_date_to": "2026-10-16",
}


def _row(date):
    return dict(ONE_FLIGHT[0], buy_link=f"https://book/{date}")


def _stub_per_date(monkeypatch, behaviour):
    """A backend that answers differently per requested departure_date.

    `behaviour` maps a date to "ok" | "partial" | "degraded" | "boom".
    Anything not listed answers a healthy single row.
    """

    async def search(self, endpoint, payload, *, outcome_sink=None, **_kwargs):
        date = payload["departure_date"]
        kind = behaviour.get(date, "ok")
        if kind == "boom":
            raise lambda_client_module.LambdaError("upstream exploded")
        if outcome_sink is not None:
            outcome_sink.append({"x-search-status": "ok" if kind == "ok" else kind})
        return [] if kind in ("degraded", "partial") else [_row(date)]

    monkeypatch.setattr(lambda_client_module.LambdaClient, "search", search)


def _first_line(result):
    return result.content[0].text


def _json_block(result):
    return json.loads(result.content[1].text)


class TestDegradedReadsAsAlarming:
    @pytest.mark.asyncio
    async def test_the_first_line_is_prose_not_json(self, server, monkeypatch):
        _stub_per_date(monkeypatch, {"2026-10-14": "degraded"})
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert _first_line(result) == DEGRADED_FIRST_LINE
        assert _first_line(result).startswith("WARNING: this search did not complete.")

    def test_the_degraded_line_contains_no_digits(self):
        """Rule 1: nothing interpolated, so no invented metric can drift in."""
        assert not any(ch.isdigit() for ch in DEGRADED_FIRST_LINE)

    @pytest.mark.asyncio
    async def test_the_json_still_follows_and_still_matches(self, server, monkeypatch):
        _stub_per_date(monkeypatch, {"2026-10-14": "degraded"})
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 2
        assert _json_block(result) == result.structured_content
        assert result.structured_content["search_status"] == "degraded"
        assert result.is_error

    @pytest.mark.asyncio
    async def test_the_roundtrip_tool_says_the_same_thing(self, server, monkeypatch):
        _stub_per_date(monkeypatch, {"2026-10-14": "degraded"})
        result = await _call_result(
            server,
            "search_roundtrip_flights",
            dict(ONEWAY_ARGS, return_date="2026-10-21"),
        )
        assert _first_line(result) == DEGRADED_FIRST_LINE


class TestPartialNamesWhatIsMissing:
    @pytest.mark.asyncio
    async def test_the_coverage_line_names_the_missing_combinations(
        self, server, monkeypatch
    ):
        _stub_per_date(monkeypatch, {"2026-10-15": "partial"})
        result = await _call_result(server, "search_oneway_flights", RANGE_ARGS)
        line = _first_line(result)
        assert line.startswith("COVERAGE WARNING: 2 of 3 searches completed.")
        assert "2026-10-15 to CMB" in line
        assert "2026-10-14" not in line and "2026-10-16" not in line
        assert "floor on what is available" in line

    @pytest.mark.asyncio
    async def test_the_counts_come_from_the_real_outcome(self, server, monkeypatch):
        _stub_per_date(
            monkeypatch, {"2026-10-14": "partial", "2026-10-16": "partial"}
        )
        result = await _call_result(server, "search_oneway_flights", RANGE_ARGS)
        attempted = _json_block(result)["search_coverage"]["searched_combinations"]
        assert attempted == 3
        assert _first_line(result).startswith(
            f"COVERAGE WARNING: 1 of {attempted} searches completed."
        )
        assert "2026-10-14 to CMB" in _first_line(result)
        assert "2026-10-16 to CMB" in _first_line(result)

    @pytest.mark.asyncio
    async def test_partial_is_still_not_an_error_and_keeps_its_rows(
        self, server, monkeypatch
    ):
        _stub_per_date(monkeypatch, {"2026-10-15": "partial"})
        result = await _call_result(server, "search_oneway_flights", RANGE_ARGS)
        assert not result.is_error
        assert result.structured_content["search_status"] == "partial"
        assert result.structured_content["result_count"] == 2
        assert _json_block(result) == result.structured_content

    @pytest.mark.asyncio
    async def test_a_request_that_raised_is_named_too(self, server, monkeypatch):
        """A raise never produces an `X-Search-Status`, so `search_status` reads
        "ok" while a third of the range is missing. That result already calls
        itself partial in the payload, so it is not the clean result the `ok`
        path exists to protect -- and it is exactly what a model reads as the
        whole answer.
        """
        _stub_per_date(monkeypatch, {"2026-10-16": "boom"})
        result = await _call_result(server, "search_oneway_flights", RANGE_ARGS)
        line = _first_line(result)
        assert line.startswith("COVERAGE WARNING: 2 of 3 searches completed.")
        assert "2026-10-16 to CMB" in line
        assert not result.is_error

    def test_a_long_missing_list_is_counted_rather_than_recited(self):
        from src.status_text import partial_first_line

        missing = [f"2026-10-{day:02d} to CMB" for day in range(1, 13)]
        line = partial_first_line(completed=3, attempted=15, missing=missing)
        assert f"and {12 - MAX_NAMED_COMBINATIONS} more" in line
        assert line.count("2026-10-") == MAX_NAMED_COMBINATIONS

    def test_it_falls_back_to_counts_when_nothing_can_be_named(self):
        from src.status_text import partial_first_line

        line = partial_first_line(completed=9, attempted=12, missing=[])
        assert line.startswith("COVERAGE WARNING: 9 of 12 searches completed.")
        assert "Nothing came back for" not in line


class TestACleanResultIsLeftAlone:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["ok", "empty"])
    async def test_no_prose_line_is_added(self, server, monkeypatch, status):
        from tests.test_structured_output import _stub_search

        _stub_search(
            monkeypatch, ONE_FLIGHT if status == "ok" else [], status=status
        )
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == result.structured_content
        assert result.structured_content["search_status"] == status
        assert "WARNING" not in result.content[0].text


class TestTheWarningComesBeforeTheSponsoredCard:
    """The ad must never sit in front of the warning.

    A partial result is the one that carries both: real rows, so the ad
    middleware attaches a card, and an incomplete range, so it carries a
    coverage line. A degraded result never has an ad (is_error short-circuits
    the SDK), so this is the only shape where the ordering can go wrong.
    """

    @pytest.fixture
    def ads_endpoint(self):
        """A local stand-in for the Lulu ads server. Same as the one in
        test_structured_output.py; fixtures on a test class are not shared
        across modules, and a conftest-level one would change that file."""
        _StubAds.requests = []
        httpd = HTTPServer(("127.0.0.1", 0), _StubAds)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_port}"
        httpd.shutdown()

    @pytest.fixture
    def ad_server(self, ads_endpoint, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "true")
        monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
        monkeypatch.setenv("LULU_ADS_PUBLISHER_ID", "pub_test")
        monkeypatch.setenv("LULU_ADS_API_KEY", "lk_test")
        monkeypatch.setenv("LULU_ADS_BASE_URL", ads_endpoint)
        return build_server(load_settings())

    @pytest.mark.asyncio
    async def test_the_coverage_line_is_still_the_first_thing_read(
        self, ad_server, monkeypatch
    ):
        _stub_per_date(monkeypatch, {"2026-10-15": "partial"})
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", RANGE_ARGS)
        assert result.structured_content.get("sponsored"), "no ad -- no revenue"
        assert _first_line(result).startswith("COVERAGE WARNING:")
        # Nothing sponsored may appear before the warning, in any block.
        rendered = [c.text for c in result.content if hasattr(c, "text")]
        assert "getlulu" not in rendered[0].lower()
        assert "sponsored" not in rendered[0].lower()

    @pytest.mark.asyncio
    async def test_the_ad_reaches_the_json_block_that_now_follows_the_warning(
        self, ad_server, monkeypatch
    ):
        """The SDK declines to sync a multi-block result, so we do it.

        `LuluAdsMiddleware` only rewrites `content[0]` when it is the single
        auto-generated text block, and leaves anything richer alone rather
        than destroying real content. A warning line makes this result
        exactly that -- so without SponsoredTextSyncMiddleware the JSON block
        would still be the pre-ad one, and the client that reads `content[]`
        rather than `structuredContent` would render no card.
        """
        _stub_per_date(monkeypatch, {"2026-10-15": "partial"})
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", RANGE_ARGS)
        assert _json_block(result) == result.structured_content
        assert _json_block(result)["sponsored"], "the ad went stale in content[]"

    @pytest.mark.asyncio
    async def test_a_clean_result_is_still_synced_by_the_sdk_itself(
        self, ad_server, monkeypatch
    ):
        """The common path is untouched: one block, rewritten by the SDK."""
        from tests.test_structured_output import _stub_search

        _stub_search(monkeypatch, ONE_FLIGHT, status="ok")
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 1
        assert json.loads(result.content[0].text)["sponsored"]

    @pytest.mark.asyncio
    async def test_a_degraded_search_still_carries_no_ad(
        self, ad_server, monkeypatch
    ):
        _stub_per_date(monkeypatch, {"2026-10-14": "degraded"})
        async with Client(ad_server) as client:
            result = await client.call_tool(
                "search_oneway_flights", ONEWAY_ARGS, raise_on_error=False
            )
        assert result.is_error
        assert (result.structured_content or {}).get("sponsored") is None
        blob = json.dumps([c.model_dump(mode="json") for c in result.content])
        assert "getlulu.dev/c/" not in blob, "never advertise on an error"


class TestTheSerializerMatchesFastMCPs:
    """Our JSON block must be byte-identical to the one FastMCP builds."""

    @pytest.mark.asyncio
    async def test_our_block_and_fastmcps_agree_on_the_same_payload(
        self, server, monkeypatch
    ):
        from src.status_text import serialize_payload
        from tests.test_structured_output import _stub_search

        _stub_search(monkeypatch, ONE_FLIGHT, status="ok")
        result = await _call_result(server, "search_oneway_flights", ONEWAY_ARGS)
        assert len(result.content) == 1
        assert result.content[0].text == serialize_payload(result.structured_content)
