"""
Stay22 affiliate wrapping on hotel booking links.

Two properties carry the whole feature and both are tested end to end through
a real tool call rather than only against the helper:

1. **Off is byte-for-byte off.** `STAY22_AID` unset is the shipped default, so
   a regression there is a regression on production, not on a future config.
2. **The redirect is built the way Stay22 actually reads it.** Three details
   were verified against live redirects on 2026-09-06 and every one of them
   fails *silently* -- a wrong link still 302s to Booking.com and still looks
   like it worked:
     * the parameter is `link`, not `url` (`url=` loses the property);
     * the target is percent-encoded in full, so a Booking.com URL's own
       query string cannot end the parameter at its first `&`;
     * the stay dates travel as Stay22's own `checkin`/`checkout` rather than
       inside the target.

The widget is covered here too, because `rowLink` resolves `link` off the row:
there is no separate widget payload to assert against, so the test that the
widget opens the wrapped URL *is* the test that the row's `link` is wrapped.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp import Client

from src.affiliate import (
    LINK_NOTE,
    STAY22_ALLEZ_URL,
    apply_to_rows,
    booking_label,
    is_booking_url,
    rewrite_booking_query,
    wrap_url,
)
from src.hotels_lambda_client import HotelsLambdaClient
from src.server import HOTELS_WIDGET_MAPPING, build_server
from src.settings import (
    DEFAULT_STAY22_BOOKING_AID,
    DEFAULT_STAY22_CAMPAIGN,
    load_settings,
)

# The live-verified property shape (fields checked against the hotels Lambda
# 2026-08-18). Kept local rather than imported from test_hotels_free: `tests`
# is not a package, and a cross-file fixture import is a rootdir accident
# waiting to happen.
PROPERTY = {
    "name": "Hotel Leone",
    "price_string": "US$273",
    "price": 273,
    "review_score": 8.2,
    "room_type": "Superior Double",
    "link": "https://www.booking.com/hotel/it/leone.html",
}

# The account's real Stay22 affiliate id, confirmed live: a redirect built
# with it comes back as `label=flightpowers-<campaign>`. Not the LetMeAllez
# script id -- that one is `6a9d8516c805ed7ccdb27f57`, and sent as `aid` it
# credits Stay22 instead of us. See LMA_ID below.
AID = "flightpowers"

# The page-script id from the hub. Present in this file only so the test that
# it is NOT interchangeable with the aid has something to name.
LMA_ID = "6a9d8516c805ed7ccdb27f57"

# A real-shaped Booking.com property URL: absolute, with its own query string
# carrying Booking's generic affiliate id and the stay dates. The query is the
# part that breaks under lazy encoding.
BOOKING_URL = (
    "https://www.booking.com/hotel/it/leone.html"
    "?aid=304142&checkin=2026-10-01&checkout=2026-10-04&lang=en-us"
)

# What should end up inside `link=`: the property page, without the query.
BARE_BOOKING_URL = "https://www.booking.com/hotel/it/leone.html"

# Stay22's own Booking.com affiliate account, confirmed live 2026-09-06
# (`&aid=1607597` on a redirected stay22.com/allez/booking response). Not
# ours -- Stay22 is the party with the Booking.com partnership.
BOOKING_AID = "1607597"


@pytest.fixture
def stub_hotels(monkeypatch):
    """Replace HotelsLambdaClient.search; the list it returns is the result."""
    rows: list = [dict(PROPERTY)]

    async def fake_search(self, endpoint, payload):
        return [dict(row) for row in rows]

    monkeypatch.setattr(HotelsLambdaClient, "search", fake_search, raising=True)
    return rows


@pytest.fixture
def stay22_server(tmp_path, monkeypatch):
    """A hotels server with the affiliate wrap switched ON."""
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    monkeypatch.setenv("STAY22_AID", AID)
    return build_server(load_settings())


@pytest.fixture
def plain_server(tmp_path, monkeypatch):
    """The same server with STAY22_AID unset -- the shipped default."""
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
    monkeypatch.setenv("HOTELS_AUTH", "secret")
    monkeypatch.delenv("STAY22_AID", raising=False)
    return build_server(load_settings())


def parts(url: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}", parse_qs(parsed.query)


class TestSettings:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("STAY22_AID", raising=False)
        assert load_settings().stay22_aid == ""

    def test_campaign_defaults_to_free_mcp(self, monkeypatch):
        monkeypatch.delenv("STAY22_CAMPAIGN", raising=False)
        assert load_settings().stay22_campaign == DEFAULT_STAY22_CAMPAIGN == "free-mcp"

    def test_quoted_value_is_unwrapped(self, monkeypatch):
        """Both existing .env files in this repo quote their values."""
        monkeypatch.setenv("STAY22_AID", f'"{AID}"')
        assert load_settings().stay22_aid == AID

    def test_booking_aid_defaults_to_stay22s_own_account(self, monkeypatch):
        monkeypatch.delenv("STAY22_BOOKING_AID", raising=False)
        assert load_settings().stay22_booking_aid == DEFAULT_STAY22_BOOKING_AID == "1607597"


class TestWrapUrl:
    def test_target_is_fully_encoded(self):
        """The target's own `&` and `=` must not become parameters of the
        redirect. This is the assertion that catches `quote(url)` written
        without `safe=""`."""
        base, query = parts(wrap_url(BOOKING_URL, aid=AID, campaign="free-mcp"))
        assert base == STAY22_ALLEZ_URL
        # `link`, not `url`. Stay22 ignores `url=` and drops the redirect to
        # booking.com's root, which loses the property and still looks fine.
        assert "url" not in query
        assert query["link"] == [BARE_BOOKING_URL]
        assert query["aid"] == [AID]
        assert query["campaign"] == ["free-mcp"]
        # Booking's own generic aid must not have leaked into the redirect's
        # parameters, where it would sit next to (or override) ours.
        assert query["aid"] == [AID]
        assert "304142" not in str(query)
        # The dates survive, as Stay22's own parameters rather than inside
        # the target -- a target carrying its own query came back as a
        # search-results page instead of the priced property.
        assert query["checkin"] == ["2026-10-01"]
        assert query["checkout"] == ["2026-10-04"]

    def test_a_target_with_no_query_is_passed_through_whole(self):
        _, query = parts(wrap_url(BARE_BOOKING_URL, aid=AID))
        assert query["link"] == [BARE_BOOKING_URL]
        assert "checkin" not in query

    def test_the_encoded_target_contains_no_bare_delimiters(self):
        """The `safe=""` assertion, stated directly: everything after
        `link=` is one opaque value."""
        wrapped = wrap_url(BOOKING_URL, aid=AID, campaign="free-mcp")
        tail = wrapped.split("link=", 1)[1]
        assert "&" not in tail and "?" not in tail and "=" not in tail

    def test_the_lma_script_id_is_not_the_affiliate_id(self):
        """Sent as `aid`, the LetMeAllez script id came back as
        `label=stay22-...` on a live redirect -- Stay22 validates the value
        rather than echoing it, so a wrong id credits them, not us. Nothing
        in this module may treat the two as interchangeable."""
        assert AID != LMA_ID
        _, query = parts(wrap_url(BOOKING_URL, aid=LMA_ID))
        # It is still passed through verbatim; the point is that the caller
        # has to supply the right one, and this module does not guess.
        assert query["aid"] == [LMA_ID]

    def test_empty_aid_returns_the_url_untouched(self):
        assert wrap_url(BOOKING_URL, aid="", campaign="free-mcp") == BOOKING_URL

    def test_campaign_is_optional(self):
        """`campaign` becomes the `label` suffix (`label=<aid>-<campaign>`).
        Without it the label is the bare aid, which still attributes."""
        _, query = parts(wrap_url(BOOKING_URL, aid=AID))
        assert "campaign" not in query
        assert query["link"] == [BARE_BOOKING_URL]

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            None,
            123,
            "/hotel/it/leone.html",
            "javascript:alert(1)",
            "mailto:someone@example.com",
        ],
    )
    def test_non_http_targets_pass_through(self, value):
        assert wrap_url(value, aid=AID) == value  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "already",
        [
            "https://www.stay22.com/allez/booking?aid=x&link=https%3A%2F%2Fb.test",
            "https://embed.stay22.com/allez?aid=x",
        ],
    )
    def test_a_stay22_url_is_never_double_wrapped(self, already):
        """Nesting a redirect inside a redirect loses the inner attribution."""
        assert wrap_url(already, aid=AID) == already

    def test_wrapping_is_idempotent(self):
        once = wrap_url(BOOKING_URL, aid=AID, campaign="free-mcp")
        assert wrap_url(once, aid=AID, campaign="free-mcp") == once


class TestBookingLabel:
    def test_matches_what_stay22s_own_redirect_produces(self):
        """label=<aid>-<campaign> is what a live /allez/booking redirect
        wrote onto the Booking.com URL (state/gtm/stay22-2026-09-06.md
        ss4) -- `link` and `booking_url` must agree on it."""
        assert booking_label(AID, "free-mcp") == "flightpowers-free-mcp"

    def test_no_campaign_is_the_bare_aid(self):
        assert booking_label(AID) == AID
        assert booking_label(AID, "") == AID


class TestIsBookingUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.booking.com/hotel/it/leone.html",
            "http://booking.com/hotel/it/leone.html",
            "https://www.booking.com/hotel/it/leone.html?aid=304142",
        ],
    )
    def test_booking_urls(self, url):
        assert is_booking_url(url) is True

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.stay22.com/allez/booking?aid=x&link=y",
            "https://example.com/hotel",
            "",
            None,
            123,
            "javascript:alert(1)",
        ],
    )
    def test_non_booking_values(self, value):
        assert is_booking_url(value) is False


class TestRewriteBookingQuery:
    def test_aid_and_label_replaced_other_params_kept(self):
        out = rewrite_booking_query(BOOKING_URL, booking_aid=BOOKING_AID, label="flightpowers-free-mcp")
        _, query = parts(out)
        assert query["aid"] == [BOOKING_AID]
        assert query["label"] == ["flightpowers-free-mcp"]
        assert "304142" not in out
        # Every other param the backend/Booking sent is untouched.
        assert query["checkin"] == ["2026-10-01"]
        assert query["checkout"] == ["2026-10-04"]
        assert query["lang"] == ["en-us"]

    def test_an_existing_label_is_replaced_not_duplicated(self):
        url = BOOKING_URL + "&label=booking-desktop-general"
        out = rewrite_booking_query(url, booking_aid=BOOKING_AID, label="flightpowers-free-mcp")
        _, query = parts(out)
        assert query["label"] == ["flightpowers-free-mcp"]

    def test_empty_booking_aid_is_off(self):
        assert rewrite_booking_query(BOOKING_URL, booking_aid="", label="flightpowers-free-mcp") == BOOKING_URL

    def test_non_booking_url_is_untouched(self):
        other = "https://www.expedia.com/hotel/x?aid=1"
        assert rewrite_booking_query(other, booking_aid=BOOKING_AID, label="x") == other

    def test_a_url_with_no_query_still_gets_aid_and_label(self):
        out = rewrite_booking_query(BARE_BOOKING_URL, booking_aid=BOOKING_AID, label="flightpowers-free-mcp")
        _, query = parts(out)
        assert query["aid"] == [BOOKING_AID]
        assert query["label"] == ["flightpowers-free-mcp"]


class TestApplyToRows:
    def test_off_leaves_the_rows_identical(self):
        rows = [dict(PROPERTY)]
        assert apply_to_rows(rows, aid="") == rows

    def test_on_rewrites_link_and_keeps_the_original(self):
        rows = [{**PROPERTY, "link": BOOKING_URL}]
        out = apply_to_rows(rows, aid=AID, campaign="free-mcp")
        assert out[0]["booking_url"] == BOOKING_URL
        assert out[0]["link_note"] == LINK_NOTE == "affiliate link (Stay22)"
        assert out[0]["link"].startswith(STAY22_ALLEZ_URL)
        # Everything else is the backend's, untouched.
        assert out[0]["name"] == PROPERTY["name"]
        assert out[0]["price_string"] == PROPERTY["price_string"]

    def test_the_input_rows_are_not_mutated(self):
        """Rows belong to the caller; in these tests they are shared
        module-level fixtures, and rewriting one in place makes later tests
        pass for reasons that depend on execution order."""
        rows = [{**PROPERTY, "link": BOOKING_URL}]
        apply_to_rows(rows, aid=AID)
        assert rows[0]["link"] == BOOKING_URL
        assert "booking_url" not in rows[0]

    def test_a_row_without_a_link_is_untouched(self):
        rows = [{"name": "No link"}]
        assert apply_to_rows(rows, aid=AID) == rows

    def test_a_backends_own_booking_url_is_not_overwritten(self):
        rows = [{**PROPERTY, "booking_url": "https://theirs.test/x"}]
        assert apply_to_rows(rows, aid=AID) == rows

    def test_non_dict_rows_survive(self):
        rows = ["not a row", {**PROPERTY, "link": BOOKING_URL}]
        out = apply_to_rows(rows, aid=AID)  # type: ignore[arg-type]
        assert out[0] == "not a row"
        assert out[1]["link"].startswith(STAY22_ALLEZ_URL)

    def test_booking_aid_set_rewrites_booking_url_too(self):
        """Both fields end up attributed to us, under the same label --
        `link` via Stay22's redirect, `booking_url` directly."""
        rows = [{**PROPERTY, "link": BOOKING_URL}]
        out = apply_to_rows(rows, aid=AID, campaign="free-mcp", booking_aid=BOOKING_AID)
        _, booking_query = parts(out[0]["booking_url"])
        assert booking_query["aid"] == [BOOKING_AID]
        assert booking_query["label"] == ["flightpowers-free-mcp"]
        # Booking's own generic aid is gone, not merely shadowed.
        assert "304142" not in out[0]["booking_url"]
        # Everything else the backend sent survives untouched.
        assert booking_query["checkin"] == ["2026-10-01"]
        assert booking_query["checkout"] == ["2026-10-04"]
        assert booking_query["lang"] == ["en-us"]
        # `link` is still the Stay22 redirect, unaffected by the booking_aid wrap.
        assert out[0]["link"].startswith(STAY22_ALLEZ_URL)

    def test_booking_aid_unset_leaves_booking_url_as_the_bare_original(self):
        """The shipped-until-now behaviour: `booking_aid` empty (the
        parameter's own default) means `booking_url` is exactly the
        original link, generic aid and all."""
        rows = [{**PROPERTY, "link": BOOKING_URL}]
        out = apply_to_rows(rows, aid=AID, campaign="free-mcp")
        assert out[0]["booking_url"] == BOOKING_URL

    def test_stay22_disabled_ignores_booking_aid_entirely(self):
        """`aid` (STAY22_AID) is the master switch. If it is off, nothing
        is wrapped at all, no matter what booking_aid says."""
        rows = [dict(PROPERTY)]
        assert apply_to_rows(rows, aid="", booking_aid=BOOKING_AID) == rows


class TestBothToolsThroughTheServer:
    """The wrap has to be in `_hotels_run`, which both tools share. A change
    that put it in one tool would pass every helper test above."""

    @pytest.mark.parametrize(
        "tool,args",
        [
            (
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            ),
            (
                "find_hotel_by_name",
                {
                    "hotel_name": "Hotel Leone",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_links_are_wrapped(self, stay22_server, stub_hotels, tool, args):
        stub_hotels[:] = [{**PROPERTY, "link": BOOKING_URL}]
        async with Client(stay22_server) as client:
            result = await client.call_tool(tool, args)
        row = result.structured_content["results"][0]
        base, query = parts(row["link"])
        assert base == STAY22_ALLEZ_URL
        assert query["link"] == [BARE_BOOKING_URL]
        assert query["aid"] == [AID]
        assert query["campaign"] == ["free-mcp"]
        assert query["checkin"] == ["2026-10-01"]
        # booking_url is attributed too, under STAY22_BOOKING_AID's default
        # (Stay22's own Booking.com account) -- see TestApplyToRows.
        _, booking_query = parts(row["booking_url"])
        assert booking_query["aid"] == [DEFAULT_STAY22_BOOKING_AID]
        assert booking_query["label"] == ["flightpowers-free-mcp"]
        assert booking_query["checkin"] == ["2026-10-01"]
        assert row["link_note"] == LINK_NOTE

    @pytest.mark.parametrize(
        "tool,args",
        [
            (
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            ),
            (
                "find_hotel_by_name",
                {
                    "hotel_name": "Hotel Leone",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_off_is_byte_for_byte_off(
        self, plain_server, stub_hotels, tool, args
    ):
        """The shipped default. No new keys, no rewritten link."""
        stub_hotels[:] = [{**PROPERTY, "link": BOOKING_URL}]
        async with Client(plain_server) as client:
            result = await client.call_tool(tool, args)
        row = result.structured_content["results"][0]
        assert row["link"] == BOOKING_URL
        assert "booking_url" not in row
        assert "link_note" not in row

    @pytest.mark.asyncio
    async def test_the_widget_row_link_opens_the_wrapped_url(
        self, stay22_server, stub_hotels
    ):
        """`rowLink` resolves `link` off the row that goes out in
        structuredContent, so the widget's click target is whatever `link`
        holds. Asserting the mapping still points there keeps a future rename
        from silently un-attributing every click."""
        assert HOTELS_WIDGET_MAPPING["rowLink"] == "link"
        assert HOTELS_WIDGET_MAPPING["rows"] == "results"

        stub_hotels[:] = [{**PROPERTY, "link": BOOKING_URL}]
        async with Client(stay22_server) as client:
            result = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            )
        rows = result.structured_content[HOTELS_WIDGET_MAPPING["rows"]]
        assert rows[0][HOTELS_WIDGET_MAPPING["rowLink"]].startswith(STAY22_ALLEZ_URL)

    @pytest.mark.asyncio
    async def test_the_book_column_still_appears_on_a_wrapped_row(
        self, stay22_server, stub_hotels
    ):
        """`_annotate_book_labels` runs AFTER the wrap and keys off `link`.
        If the wrap ever emptied or renamed that field, the Book cell would go
        blank and the row would become unclickable."""
        stub_hotels[:] = [{**PROPERTY, "link": BOOKING_URL}]
        async with Client(stay22_server) as client:
            result = await client.call_tool(
                "search_hotels",
                {
                    "destination": "Rome",
                    "checkin_date": "2026-10-01",
                    "checkout_date": "2026-10-04",
                },
            )
        assert result.structured_content["results"][0]["book_label"] == "Book →"


class TestFlightsAreNeverWrapped:
    def test_buy_link_is_not_a_field_this_module_touches(self):
        """Flights carry `buy_link`, a Google Flights itinerary deep link.
        Stay22 does not monetise it and wrapping it would break the Book
        column on both flight tools."""
        rows = [{"buy_link": "https://www.google.com/travel/flights/x"}]
        assert apply_to_rows(rows, aid=AID) == rows
