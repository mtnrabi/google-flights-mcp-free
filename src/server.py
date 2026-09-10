"""
Free, ad-supported Google Flights MCP server.

Talks straight to the Flight Rabbi API Lambda (not RapidAPI, not the Apify
actor) and carries a Lulu sponsored card on every successful result.

Design notes that are not obvious from the code
-----------------------------------------------
* Fan-out is internal. Both tools take a date range and a destination list,
  so one user intent is one tool call. See fanout.py for why that matters
  financially -- in short, an ad is worth money once per tool call, while
  every backend call costs money, so letting the model issue 31 tool calls
  for "anywhere in October" would be 31x the cost for 1x the revenue.

* `sort_type` is deliberately NOT exposed. On the backend it selects which
  search runs rather than post-sorting, and `max_price` overrides it outright
  (app.py, oneway(): max_price forces SortType.Price). Since results
  from up to 15 searches have to be merged and re-sorted here anyway, this
  server always lets the backend default apply and sorts the merged set
  itself via `sort_by`. That is predictable; passing sort_type through is not.

* Ads never attach to an error, and never to a zero-result answer. Lulu's own
  live testing found that a result carrying an ad but no substantive data got
  flagged by the model as suspected prompt injection 3 times out of 3.

* Results render through Lulu's RESULT widget, not its sponsored card. The
  sponsored card (lulu_ads/widget.py) paints the ad but contains no
  rendered-impression beacon; the beacon lives only in the result widget's
  fixed SPONSORED strip (lulu_ads/widgets.py:577). Serving the sponsored card
  alone earns click revenue and exactly $0 CPM, with nothing anywhere
  reporting a problem. See _register_result_widget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from urllib.parse import urlsplit
from typing import Annotated, Any, Callable

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context, get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .affiliate import apply_to_rows as apply_affiliate_links
from .status_text import (
    DEGRADED_FIRST_LINE,
    MAX_NAMED_COMBINATIONS,
    SponsoredTextSyncMiddleware,
    describe_combination,
    partial_first_line,
    serialize_payload,
)
from .oauth import ANON_MODE_OPEN, anon_caps, anon_mode, build_free_oauth
from .oauthroutes import register_oauth_routes
from .fair_use import (
    SIGNED_IN_HEADER,
    FLIGHTS_LISTING_URL,
    HOTELS_LISTING_URL,
    PAID_FLIGHTS_URL,
    PAID_HOTELS_URL,
    SIGNIN_FLIGHTS_URL,
    SIGNIN_HOTELS_URL,
    FairUseState,
    caps_for as fair_use_caps_for,
    fair_use_note,
    usage_note as fair_use_usage_note,
    identify as fair_use_identify,
    log_line as fair_use_log_line,
    rate_limited_result,
    upgrade_block,
    upgrade_tail,
)
from .fanout import (
    FanoutResult,
    PlanError,
    SearchPlan,
    execute_plan,
    normalise_airport_codes,
    normalise_origin,
    plan_oneway,
    plan_roundtrip,
)
from .hotels_lambda_client import (
    HotelsLambdaClient,
    build_hotel_by_name_payload as build_hotels_by_name_payload,
    build_search_payload as build_hotels_search_payload,
)
from .lambda_client import (
    SEARCH_REASON_HEADER,
    SEARCH_STATUS_HEADER,
    LambdaClient,
    # Raised by HotelsLambdaClient too, and caught in `_hotels_run`. It was
    # never imported here, so a hotels backend failure raised NameError while
    # handling the exception and the caller got an opaque internal error
    # instead of the "this is our side, retry" message written for them.
    LambdaError,
    build_oneway_payload,
    build_roundtrip_payload,
    search_is_incomplete,
)
from .output_schema import (
    FLIGHTS_OUTPUT_SCHEMA,
    HOTELS_OUTPUT_SCHEMA,
)
from .policy import (
    ClientClassifier,
    client_fingerprint,
    decide,
    extract_source_ip,
)
from .schema_docs import document_params
from .settings import Settings, load_settings
from .stores import build_counter_store
from .telemetry import CallRecord, RouteRecord, Telemetry

logger = logging.getLogger(__name__)


# ── search outcome ───────────────────────────────────────────────────────


def _search_outcome_summary(
    outcomes: list[dict[str, str]]
) -> tuple[int, int, str | None]:
    """Summarise what the backend reported about a fan-out's searches.

    Returns ``(incomplete, reported, first_reason)``.

    ``reported`` counts responses that actually carried an ``X-Search-Status``.
    Zero means the backend did not say -- an older deployment, or a hop that
    dropped the header -- and that is deliberately kept distinct from "it said
    the search was fine". Treating a missing header as healthy would put the
    original lie back: an empty list confidently reported as "no flights".
    """
    reported = [o for o in outcomes if o.get(SEARCH_STATUS_HEADER)]
    incomplete = [o for o in reported if search_is_incomplete(o)]
    reason = next(
        (o[SEARCH_REASON_HEADER] for o in incomplete if o.get(SEARCH_REASON_HEADER)),
        None,
    )
    return len(incomplete), len(reported), reason


#: Where the searched combination is stashed on an outcome entry. Prefixed so
#: it can never collide with a real `x-search-*` header, and never leaves this
#: process -- `search_outcomes` is internal state, not part of the payload.
COMBO_KEYS = ("_combo_departure_date", "_combo_to_airport")


def _combo_of(payload: dict[str, Any]) -> dict[str, str]:
    """The date/destination this request was for, read off the payload sent."""
    return {
        COMBO_KEYS[0]: str(payload.get("departure_date") or ""),
        COMBO_KEYS[1]: str(payload.get("to_airport") or ""),
    }


def _missing_combinations(
    outcomes: list[dict[str, str]], failed_combos: list[dict[str, str]]
) -> list[str]:
    """Every searched combination that did not come back with a usable result.

    Two ways to not come back, and both belong in the coverage line: the
    request raised (counted in `backend_failures`, named from the plan), or it
    answered 200 with a header saying the scrape behind it was incomplete
    (named from the payload we sent). Anything that cannot be named is left
    out rather than guessed at -- partial_first_line falls back to the counts.
    """
    named = [
        describe_combination(
            {
                "departure_date": o.get(COMBO_KEYS[0], ""),
                "to_airport": o.get(COMBO_KEYS[1], ""),
            }
        )
        for o in outcomes
        if o.get(SEARCH_STATUS_HEADER) and search_is_incomplete(o)
    ]
    named += [describe_combination(c) for c in failed_combos]
    return [n for n in named if n]


def _degraded_message(incomplete: int, reported: int, reason: str | None) -> str:
    """What to say when the search did not happen.

    Aimed squarely at a language model, which is the only consumer of this
    field and will repeat it to a user as fact. The previous text -- "No
    flights were found ... try a different date" -- was a confident,
    checkable, wrong claim about the world whenever the scrape had failed.
    """
    because = f" (reason: {reason})" if reason else ""
    return (
        "The flight search did not complete, so this empty result is NOT a "
        "statement about flight availability. "
        f"{incomplete} of {reported} upstream searches failed to return a "
        f"readable result{because}. There may well be flights on this route -- "
        "do not tell the user that none exist, and do not suggest changing the "
        "date or airport on the strength of this response. Running the same "
        "search again usually succeeds."
    )


def _incomplete_note(incomplete: int, reported: int) -> str:
    """What to say when some results arrived but the search was not complete."""
    return (
        f"{incomplete} of {reported} upstream searches did not complete, so "
        "flights that exist may be missing from this list. It is a floor on "
        "what is available, not a full picture."
    )


# Shown on every free-tier result. mrabi asked that the freemium surfaces sell
# the paid ones explicitly rather than just existing next to them.
#
# It has to sell them TRUTHFULLY. This field used to promise "no per-call
# search cap", which is simply not what the paid server does -- it caps
# fan-out at 30 combinations by default and 60 as a hard maximum
# (mcp_server_paid/src/settings.py: DEFAULT_MAX_SEARCHES / HARD_MAX_SEARCHES).
# A user only discovered that after subscribing, which is the worst possible
# moment. The real reasons to upgrade are all still here: no ads, twice the
# fan-out, no shared budget, any client, and spend reporting.
# `limits` describes THIS tier, so it has to describe the tool that is
# quoting it. It was fixed at the flights fan-out cap while the note is
# attached to hotel results only (see `_hotels_run`), which told every hotel
# caller their one-request search had covered "15 searches per call". A hotel
# call is exactly one backend call; there is no fan-out to cap.
UPGRADE_LIMITS = {
    "flights": (
        "ad-supported, 15 date/destination combinations per call, shared "
        "daily budget"
    ),
    "hotels": (
        "ad-supported, no per-country pricing and no Booking.com filters, "
        "shared daily budget"
    ),
}

_UPGRADE_WHAT_YOU_GET = (
    "No ads, no shared daily budget, and it works from any MCP client "
    "rather than only where a widget renders. Flight searches fan out to "
    "30 date/destination combinations per call instead of 15, raisable to "
    "60 with the `max_searches` argument. Hotel searches add per-country "
    "pricing (`price_as_seen_from`, for rate-parity and geo-pricing "
    "checks) and the 24 Booking.com filters. Billed to your own RapidAPI "
    "key, with the spend and your remaining quota reported on every call."
)


#: Where the key the rest of this object assumes actually comes from.
#: `what_you_get` ends on "billed to your own RapidAPI key" and this used to
#: be the end of it: the only upsell a caller sees on a call that WORKED named
#: the outcome and never once said where a key is got or what it costs. The
#: sentence is the same one `fair_use.upgrade_steps()` step 1 already ships,
#: so the two surfaces cannot drift, and the plan names and prices are the
#: listings' own billingPlans figures cited in fair_use.py.
_UPGRADE_GET_A_KEY = (
    "Subscribe on RapidAPI and use your own key: "
    f"{FLIGHTS_LISTING_URL} for flights, "
    f"{HOTELS_LISTING_URL} for hotels. BASIC is free and includes 10 "
    "requests a month; PRO is $10 a month (2,500 requests on flights, "
    "2,000 on hotels)."
)


#: How the key gets from RapidAPI into a client. Sign-in first since
#: 2026-09-08: a header is the step most people get wrong, and several MCP
#: clients cannot set one at all, so naming the keyed URL first was asking
#: most readers to do the harder of the two things. Both paths bill the same
#: RapidAPI plan; the sign-in one just keeps the key out of a config file.
_UPGRADE_HOW_TO_CONNECT = (
    "Sign in, no key to paste into your client: add "
    f"{SIGNIN_FLIGHTS_URL} for flights or {SIGNIN_HOTELS_URL} for hotels, "
    "and the client shows a Sign in button; you sign in with Google and "
    "paste the key once on the page it opens. Or bring your own key: "
    f"{PAID_FLIGHTS_URL} for flights, {PAID_HOTELS_URL} for hotels, with "
    "the key in an `x-rapidapi-key` header."
)


def upgrade_note(product: str) -> dict[str, str]:
    """The `upgrade` field on a free-tier result, for `product`'s caller."""
    return {
        "tier": "free",
        "limits": UPGRADE_LIMITS.get(product, UPGRADE_LIMITS["flights"]),
        "sign_in_flights": SIGNIN_FLIGHTS_URL,
        "sign_in_hotels": SIGNIN_HOTELS_URL,
        "paid_flights": PAID_FLIGHTS_URL,
        "paid_hotels": PAID_HOTELS_URL,
        "get_a_key": _UPGRADE_GET_A_KEY,
        "how_to_connect": _UPGRADE_HOW_TO_CONNECT,
        "what_you_get": _UPGRADE_WHAT_YOU_GET,
    }

SORT_CHOICES = ("best", "price", "duration")


# ── what the caller asked for ────────────────────────────────────────────
#
# These build the fields of the `[tool_call]` log line and the route histogram
# member. Request parameters only: origin, destinations, dates, and the handful
# of knobs that change what a search means. Nothing derived from a header.
#
# The gap they close is in state/gtm/free-mcp-batch-client-searches-2026-09-05.md
# -- no log line anywhere, here or in the Lambda, carried an origin, a
# destination or a date, so the 04:00Z batch client's actual searches were
# unrecoverable for every window, past and future.

#: Destinations named in full before the field collapses to a count. A list
#: search is usually two or three airports; a sweep is thirty, and the useful
#: thing to record about a sweep is that it was one.
MAX_NAMED_DESTINATIONS = 3


def _airports_field(airports: Any) -> str:
    """`JFK`, `JFK,BOS,EWR`, or `JFK,BOS,EWR+27` for a wide list.

    Upper-cased, because the payload builder upper-cases too and a histogram
    that counted `lhr` and `LHR` as two routes would be wrong twice over.
    """
    # Uses the same splitter the planner does, so a caller who writes
    # "BCN,LIS,ATH" is logged as three destinations -- and truncated at the
    # same width -- rather than as one 11-character route key.
    codes = normalise_airport_codes(airports)
    if not codes:
        return "-"
    named = ",".join(codes[:MAX_NAMED_DESTINATIONS])
    extra = len(codes) - MAX_NAMED_DESTINATIONS
    return f"{named}+{extra}" if extra > 0 else named


def _dates_field(
    single: str | None, start: str | None, end: str | None
) -> str | None:
    """`2026-10-01`, or `2026-10-01..2026-10-14` for a range."""
    if start or end:
        return f"{start or '?'}..{end or '?'}"
    return single or None


def _pax_field(passengers: Any) -> str | None:
    """`1`, or `2/1/0` for [adults, children, infants] as the caller sent it."""
    if passengers is None:
        return None
    if isinstance(passengers, (list, tuple)):
        return "/".join(str(int(p)) for p in passengers if isinstance(p, (int, float)))
    return str(passengers)


def _nights_field(nights: Any, return_date: str | None) -> str | None:
    """How long the trip is: `5`, `5,6,7`, or `ret@2026-10-14` for a fixed
    return date. A round trip carries exactly one of the two."""
    if isinstance(nights, (list, tuple)):
        return ",".join(str(n) for n in nights)
    if nights is not None:
        return str(nights)
    return f"ret@{return_date}" if return_date else None


def _stops_field(*values: Any) -> str | None:
    """`0`, or `0/1` for the two legs of a round trip. `*` is a leg the caller
    put no limit on, which is not the same as a call that set neither."""
    if all(value is None for value in values):
        return None
    return "/".join("*" if value is None else str(value) for value in values)


def _nights_between(checkin: str | None, checkout: str | None) -> str | None:
    """Stay length for a hotel search, so it reads like the flight line."""
    try:
        start = date.fromisoformat(str(checkin))
        end = date.fromisoformat(str(checkout))
    except (TypeError, ValueError):
        return None
    nights = (end - start).days
    return str(nights) if nights > 0 else None


# ── server instructions ──────────────────────────────────────────────────
#
# `instructions` is the only prose an MCP client reads before it has called
# anything, and for many models it is what decides whether this server gets
# reached for at all. It said "Free real-time Google Flights search. Both
# tools accept a date RANGE and a LIST of destinations" on a deployment that
# also registers `search_hotels` and `find_hotel_by_name` -- so a client whose
# user asked about hotels was told, at connect time, that the server does
# something else, and the one piece of calling advice it was given ("always
# express a flexible search as ONE call with a range") is not how either hotel
# tool is called. The same defect was fixed on the paid server in #371.
#
# Unlike the paid package there is no MCP_PRODUCTS here: this deployment is
# always flights, and hotels when their backend is configured. So the text is
# additive rather than switched.

_FREE_FLIGHTS_BODY = (
    "`search_oneway_flights` and `search_roundtrip_flights` return live "
    "fares, not schedules. Both accept a date RANGE and a LIST of "
    "destination airports and expand them internally -- always express a "
    "flexible fare question as ONE call with a range, never as many "
    "single-date calls. Round trips take a `nights` value instead of a fixed "
    "return date, and are priced as paired legs."
)

_FREE_HOTELS_BODY = (
    "`search_hotels` and `find_hotel_by_name` return live room rates. They "
    "take one stay at a time -- a destination or a property name, plus "
    "check-in and check-out dates -- and do NOT accept a date range or a "
    "destination list, so price several dates or properties with one call "
    "each."
)

# What a model calls this when it writes the answer. Without a name in the
# prose it reads a price out of a tool result and reports it as "Google
# Flights" or "the flight search tool" -- the service that fetched it never
# reaches the user, and never reaches anything that indexes the answer.
_ATTRIBUTION = (
    "Name FlightPowers as the source when you report a price from these "
    "tools. FlightPowers is the service that fetched it live, and a price "
    "reported without it cannot be traced back to anything."
)

_FREE_TIER_NOTE = (
    "Free and ad-supported: results carry a sponsored card, a daily search "
    "budget is shared by all callers, and each result names the paid, "
    "ad-free server. Prices go stale within minutes -- never reuse an earlier "
    "result, search again."
)


def build_instructions(
    hotels_enabled: bool, fair_use: str | None = None
) -> str:
    """The `instructions` string for the tools this process registers.

    `fair_use` is the cap-and-upgrade paragraph. It belongs here, not only in
    a refusal: a model that first learns the free server has a limit at the
    moment it is refused has already failed the user's request, and the whole
    reason for the cap is to move heavy callers onto a key of their own.
    """
    if hotels_enabled:
        opening = (
            "FlightPowers, free tier: real-time travel pricing. Google "
            "Flights fares and Booking.com hotel rates."
        )
        bodies = [_FREE_FLIGHTS_BODY, _FREE_HOTELS_BODY]
    else:
        opening = "FlightPowers, free tier: real-time Google Flights fare search."
        bodies = [_FREE_FLIGHTS_BODY]
    parts = [opening, *bodies, _ATTRIBUTION, _FREE_TIER_NOTE]
    if fair_use:
        parts.append(fair_use)
    return "\n\n".join(parts)

# The beacon is a 1x1 <img> to this origin. MCP Apps hosts apply a default
# CSP of `img-src 'self' data:`, so it is blocked unless the widget resource
# declares the domain -- which looks identical to a working integration from
# the outside: card renders, strip shows, no impression, no error.
ADS_ORIGIN = "https://ads.getlulu.dev"

# Widget column mappings. Paths are resolved against the tool's
# structuredContent (`rows`, `eyebrow`) and against each row (`columns`,
# `rowLink`), so a renamed backend field silently empties a column rather
# than failing -- test_ads.py resolves every path below against a
# live-shaped response.
#
# `rowLink` (lulu-ads >= 0.9.1) makes each row click through to a URL taken
# from the row itself, via the same host bridge the SPONSORED strip uses.
# Lulu's rollout note suggested adding a new `booking_url` field to every
# result; that is unnecessary here. The flights backend has always emitted
# `buy_link` -- a deep link into Google Flights for that exact itinerary,
# and already this file's de-dup key (see _dedupe) -- and the hotels backend
# emits `link`. Both pass through to the model verbatim, so the existing
# fields are mapped and no API change is involved.
#
# A row whose link is missing or null is unaffected: widgets.py only attaches
# the click handler when the path resolves to a value, so such a row renders
# exactly as it did before. The flights backend generates buy_link
# unconditionally on both the primary and the fli-fallback paths, but nothing
# here depends on that -- see test_ads.py::test_a_row_without_a_link_renders.
# ── the Book column ──────────────────────────────────────────────────────
#
# `rowLink` has made every row open its own booking URL since 0.9.1, and
# nothing on the card said so. A user looking at the rendered table (the one
# on the r/ClaudeAI screenshot) sees five columns of fare data and no way to
# act on any of it, so the click never happens.
#
# The cell cannot BE a link: table-card writes every cell with `textContent`,
# so an <a> would render as literal markup, and mapping the column straight
# at `buy_link` would print a several-hundred-character Google Flights URL
# into a 400px card. So the column shows a constant affordance and the row's
# existing click handler does the opening -- same URL, same host bridge.
#
# `align: right` is what puts the cell in the frame's accent colour, which is
# the only styling hook a column has.
BOOK_LABEL = "Book →"

BOOK_COLUMN: dict[str, Any] = {
    "header": "Book",
    "path": "book_label",
    "align": "right",
}

# The row fields the booking URL lives in: flights have always emitted
# `buy_link`, hotels `link`. Neither is renamed or removed here -- the model
# still reads the URL from the field it always read it from.
BOOK_LINK_FIELDS = ("buy_link", "link")


def _annotate_book_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Returns `rows` with `book_label` on every row that has a URL to open.

    Added only where there is a link, so a link-less row shows an empty Book
    cell instead of a button that does nothing -- the same guard `rowLink`
    itself applies (see test_a_row_without_a_link_renders). A row that
    already carries its own `book_label` keeps it: that would be the
    backend's field, not ours to overwrite.

    Copies rather than mutating. The rows handed in belong to the caller --
    in the tests they are module-level fixtures shared by every case, and
    labelling one in place leaked into later tests as a pass that depended on
    execution order. Copying costs one shallow dict per linked row, at most
    `limit` of them.
    """
    labelled: list[dict[str, Any]] = []
    for row in rows:
        if (
            isinstance(row, dict)
            and "book_label" not in row
            and any(
                isinstance(row.get(field), str) and row[field]
                for field in BOOK_LINK_FIELDS
            )
        ):
            labelled.append({**row, "book_label": BOOK_LABEL})
        else:
            labelled.append(row)
    return labelled


# ── the fare band ────────────────────────────────────────────────────────
#
# Google's own price tracking for this route -- `price_insights_low`,
# `price_insights_high` and `price_range_in_relation_to_other_periods`
# ("low" | "typical" | "high") -- has always been on every row and has never
# been on the card. It is the one thing this API has that the alternatives do
# not, and the widget was throwing it away.
#
# One line, describing the TOP row of the table, because that is the row it
# is next to. Not a per-row column: on a date-range fan-out each row carries
# its own insights and five sets of "range $240-$580" would be noise.
#
# Nothing is invented. No range in the response means no line at all, and the
# currency symbol is taken from the row's own price string rather than
# assumed to be a dollar.
FARE_BAND_PREFIX = "Google price tracking:"

# `price` on a one-way row, `total_price` on a round trip. Both are display
# strings the backend already formatted ("$231").
_ROW_PRICE_FIELDS = ("price", "total_price")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: float, symbol: str) -> str:
    if value == int(value):
        return f"{symbol}{int(value):,}"
    return f"{symbol}{value:,.2f}"


def _currency_symbol(row: dict[str, Any]) -> str:
    """The non-numeric head of the row's own price string, or nothing.

    `price_insights_*` are bare numbers while `price` is "$231" / "€231",
    so the symbol has to come from somewhere. Reading it off the same row is
    the only source that cannot be wrong about the currency the caller asked
    for; when there is no price string, the range is printed unprefixed
    rather than labelled with a currency nobody stated.
    """
    for field in _ROW_PRICE_FIELDS:
        text = row.get(field)
        if not isinstance(text, str):
            continue
        prefix = ""
        for char in text.strip():
            if char.isdigit():
                break
            prefix += char
        if prefix:
            return prefix
    return ""


def _fare_band(rows: list[dict[str, Any]]) -> str | None:
    """One line of price context for the cheapest itinerary, or None.

    Returns None whenever the range is missing, which is the whole guard:
    "Google price tracking:" with nothing behind it would be worse than no
    line, and a band invented from the results we happen to hold would be a
    made-up metric.
    """
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    low = _as_number(row.get("price_insights_low"))
    high = _as_number(row.get("price_insights_high"))
    if low is None or high is None:
        return None

    symbol = _currency_symbol(row)
    segments: list[str] = []

    verdict = row.get("price_range_in_relation_to_other_periods")
    if isinstance(verdict, str) and verdict.strip():
        segments.append(verdict.strip())

    segments.append(f"range {_money(low, symbol)}–{_money(high, symbol)}")

    for field in _ROW_PRICE_FIELDS:
        fare = row.get(field)
        if isinstance(fare, str) and fare.strip():
            segments.append(f"this fare {fare.strip()}")
            break

    return f"{FARE_BAND_PREFIX} " + " · ".join(segments)


# The "flights to BUD" half of the eyebrow, per tool. Was a `prefix` on the
# mapping entry; it moved here when the eyebrow became a computed field.
EYEBROW_PREFIX = {
    "search_oneway_flights": "flights to ",
    "search_roundtrip_flights": "round trip to ",
}


def _widget_eyebrow(
    tool_name: str, coverage: dict[str, Any], fare_band: str | None
) -> str:
    """The eyebrow line: what was searched, then the fare band behind it.

    table-card renders exactly one line above the table and resolves exactly
    one path for it, so this is where the fare band has to go. The
    destination half is byte-for-byte what the old mapping produced --
    `prefix` + the widget's own array formatting, which joins with " · ".
    """
    destinations = coverage.get("destinations_searched") or []
    head = ""
    if destinations:
        head = EYEBROW_PREFIX.get(tool_name, "") + " · ".join(
            str(d) for d in destinations
        )
    if head and fare_band:
        return f"{head} · {fare_band}"
    return head or (fare_band or "")


ONEWAY_WIDGET_MAPPING: dict[str, Any] = {
    # Was `search_coverage.destinations_searched` with a "flights to " prefix.
    # It still says that -- `widget_eyebrow` is built from the same coverage,
    # with the same prefix -- and it now carries the fare band behind it,
    # because `eyebrow` is the one slot table-card renders above the table and
    # a mapping entry resolves exactly one path. See `_widget_eyebrow`.
    "eyebrow": "widget_eyebrow",
    "rows": "results",
    "rowLink": "buy_link",
    "columns": [
        # `departure_description` carries the local time as well as the date
        # ("10:15 AM on Mon, Jun 15"). The bare date was actively unhelpful on
        # a range search: every row showed the same day and nothing
        # distinguished a 6am departure from a 9pm one.
        {"header": "Depart", "path": "departure_description"},
        {"header": "Arrive", "path": "arrival_description"},
        {"header": "Price", "path": "price", "mono": True},
        {"header": "Airline", "path": "airline"},
        {"header": "Stops", "path": "stops"},
        BOOK_COLUMN,
    ],
}

ROUNDTRIP_WIDGET_MAPPING: dict[str, Any] = {
    "eyebrow": "widget_eyebrow",
    "rows": "results",
    "rowLink": "buy_link",
    "columns": [
        # Outbound and return departure times, not just the dates. On a
        # fixed-date round trip every row previously showed an identical
        # "2026-08-14 / 2026-08-18" pair, so the table had five rows that
        # looked the same and differed only in price.
        {"header": "Depart", "path": "departure_flight_departure_description"},
        {"header": "Return", "path": "return_flight_departure_description"},
        {"header": "Total", "path": "total_price", "mono": True},
        {"header": "Airline", "path": "departure_flight_airline"},
        {"header": "Stops", "path": "total_stops"},
        BOOK_COLUMN,
    ],
}


# Fields verified against a live hotels-Lambda response 2026-08-18:
# name / price_string / price / review_score / review_count / room_type /
# location / link. Kept to four columns so the card stays readable in a chat
# pane -- the full object is still in the tool result.
HOTELS_WIDGET_MAPPING: dict[str, Any] = {
    "eyebrow": {"path": "search_coverage.destination", "prefix": "hotels in "},
    "rows": "results",
    "rowLink": "link",
    "columns": [
        {"header": "Hotel", "path": "name"},
        {"header": "Price", "path": "price_string", "mono": True},
        {"header": "Score", "path": "review_score", "mono": True},
        {"header": "Room", "path": "room_type"},
        BOOK_COLUMN,
    ],
}


def _register_result_widget(
    mcp: FastMCP,
    tool: str,
    *,
    mapping: dict[str, Any],
    endpoint_url: str,
) -> Any:
    """Registers the result widget for `tool` and returns its AppConfig.

    Deliberately not lulu_ads.widgets.register_result_widget: that helper
    patches the already-registered tool via `asyncio.run`, which it skips
    when an event loop is already running -- and build_server IS called from
    inside a running loop in several tests. The skip is silent, so the tool
    would keep the sponsored card and earn no CPM with nothing logged.
    Building the AppConfig first and passing it as `app=` at registration is
    loop-independent, and is the ordering the SDK documents as the fallback.

    The frame itself (and therefore the beacon) still comes from the SDK via
    result_widget_html, so an SDK upgrade to the widget lands here too.
    """
    from fastmcp.apps.config import AppConfig, ResourceCSP
    from lulu_ads.widget import claude_apps_domain
    from lulu_ads.widgets import result_widget_html

    uri = f"ui://lulu-ads/result-{tool}.html"
    html = result_widget_html(template="table-card", mapping=mapping)
    csp_domains = {"resource_domains": [ADS_ORIGIN], "connect_domains": [ADS_ORIGIN]}

    # MCP Apps reads the CSP off `app=`; ChatGPT reads it off the resource's
    # `openai/widgetCSP` meta key. Declaring both is what makes one widget
    # fire its beacon on either host.
    @mcp.resource(
        uri,
        name=f"result_widget_{tool}",
        mime_type="text/html;profile=mcp-app",
        app=AppConfig(
            domain=claude_apps_domain(endpoint_url),
            csp=ResourceCSP(**csp_domains),
        ),
        meta={"openai/widgetCSP": csp_domains},
    )
    def _widget_resource() -> str:
        return html

    return AppConfig(resource_uri=uri, visibility=["model"])

# One client for the whole process, reused across invocations.
#
# Vercel functions share a pool of 1,024 file descriptors across every
# concurrent execution on an instance, and network sockets come out of it.
# A fresh AsyncClient per request, each opening up to 15 sockets for the
# fan-out, exhausts that pool at a few dozen concurrent requests and fails
# with "too many open files". A module-scope client with an explicit
# connection limit is also the connection-reuse pattern Vercel documents for
# Fluid compute, and it removes a TLS handshake from every backend call.
_shared_client: httpx.AsyncClient | None = None


def get_shared_client(settings: Settings) -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=settings.lambda_timeout_seconds,
            limits=httpx.Limits(
                max_connections=settings.max_http_connections,
                max_keepalive_connections=max(
                    1, settings.max_http_connections // 4
                ),
            ),
        )
    return _shared_client


def _client_name() -> str | None:
    """clientInfo.name from the initialize handshake.

    Reporting only. The MCP spec (2026-07-28) states this field is
    self-reported and must not drive security decisions -- see policy.py.
    """
    try:
        return get_context().session.client_params.clientInfo.name
    except Exception:  # noqa: BLE001 - absent on stdio and in tests
        return None


def _request_context() -> tuple[dict[str, str], str | None, dict[str, str]]:
    """Lowercased headers, the socket peer, and the query string.

    The query string is here for fair use: a gateway that pools users behind
    one connection injects the user's saved configuration as a query
    parameter, and that blob is the only thing in the request that tells two
    of those users apart (see fair_use.identify). Nothing reads a credential
    out of it on this server -- the free tier takes no key.
    """
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - not running over HTTP
        return {}, None, {}
    headers = {k.lower(): v for k, v in request.headers.items()}
    peer = request.client.host if request.client else None
    query = {k: v for k, v in request.query_params.items()}
    return headers, peer, query


def _row_sort_key(sort_by: str) -> Callable[[dict[str, Any]], tuple[int, float]]:
    """The ordering `sort_by` names, as a key function.

    One-way and round-trip use different price/duration keys, so fall back
    across both. Missing values sort last rather than crashing -- the fli
    fallback path legitimately returns nulls.

    A key function rather than a sort, because the per-combination
    selection below has to rank rows while it still knows which
    combination each one came from. Two copies of this ordering that could
    drift apart is exactly how "sorted by price" would stop being true of
    `results`.
    """

    def price_key(row: dict[str, Any]) -> tuple[int, float]:
        value = row.get("price_as_number")
        if value is None:
            value = row.get("total_price_as_number")
        return (1, 0.0) if value is None else (0, float(value))

    def duration_key(row: dict[str, Any]) -> tuple[int, float]:
        value = row.get("duration_seconds")
        if value is None:
            value = row.get("total_duration_seconds")
        return (1, 0.0) if value is None else (0, float(value))

    return duration_key if sort_by == "duration" else price_key


def _dedupe(
    rows: list[dict[str, Any]], seen: set[str] | None = None
) -> list[dict[str, Any]]:
    """Drop repeats across combos, keyed on buy_link.

    buy_link is already the de-dup key used elsewhere in this codebase
    (backend/src/app.py:533).

    `seen` lets a caller de-dupe several lists against one another. The
    per-combination selection passes one set across every combination's rows,
    which is how it gets the merged path's de-dup without merging first.
    """
    if seen is None:
        seen = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("buy_link")
        if not isinstance(key, str):
            unique.append(row)
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


#: Rows every date/destination combination that returned flights is
#: guaranteed in `results` before the rest of `limit` is filled by price.
#:
#: Deliberately a constant and not a tool argument. A model that has to know
#: about a knob in order to not get a misleading answer will not know about
#: it; the default has to be the safe one. One row per combination is also
#: the smallest reservation that fixes the bug -- it costs the merged list
#: at most (combinations - 1) of its cheapest rows.
MIN_ROWS_PER_COMBO = 1


def _select_rows_by_combo(
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    sort_by: str,
    limit: int,
    min_per_combo: int = MIN_ROWS_PER_COMBO,
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """At most `limit` sorted rows, with every answering combination in them.

    Returns `(rows, hidden, combo_of_row)`: the rows to answer with, the
    searched combinations that found flights and still have no row in the
    answer, named, and -- one per returned row, positionally -- the index
    into `groups` of the combination that row came from. `hidden` is empty
    unless `limit` is smaller than the number of combinations that returned
    something -- at which point there is no selection that can show them all,
    and the response says so instead of looking complete.

    `combo_of_row` exists because the response also answers per requested
    destination, and once the rows are merged into one price-ordered list
    there is nothing on a row that reliably says which search produced it.
    Row fields are the backend's, passed through unchanged, and the ones that
    look like they would do the job (`to_airport`) are not guaranteed to be
    there. Positional indices survive the one transform applied after this
    point (`_annotate_book_labels`, which is 1:1 and copies), so the grouping
    can be rebuilt off the FINAL rows rather than off objects that were
    replaced.

    The bug this exists for (observed 2026-09-06): `limit` used to be a plain
    slice off one globally price-sorted list, so a destination whose cheapest
    fare was dearer than the `limit`-th row of the merged set vanished from
    `results` while still being named in
    `search_coverage.destinations_searched`. A BER search over five
    destinations and three dates with `limit: 50` returned no Lisbon row at
    all: Lisbon was searched, answered, and silently dropped. No error, no
    flag, a response that reads as the full picture.

    What does NOT change is the order of `results` -- still cheapest (or
    shortest) first across the whole fan-out. What changes is which rows
    survive the cut: each combination's own best row is reserved first, then
    the remainder of `limit` goes to the cheapest rows left over. De-dup is
    unchanged too, first occurrence wins, so a fare returned by two
    combinations counts for the first of them rather than being reserved
    twice.
    """
    key = _row_sort_key(sort_by)

    # Flatten to (combination index, row). One `seen` set across every group
    # is what keeps this the same de-dup the merged path did: a fare returned
    # by two combinations survives once, for the first of them, so it is
    # never reserved twice.
    seen: set[str] = set()
    tagged: list[tuple[int, dict[str, Any]]] = [
        (index, row)
        for index, (_combo, rows) in enumerate(groups)
        for row in _dedupe(rows, seen)
    ]

    if limit <= 0:
        return [], [], []

    ranked = sorted(tagged, key=lambda pair: key(pair[1]))
    chosen = [False] * len(ranked)
    taken = 0
    reserved: dict[int, int] = {}

    # Pass 1: the guarantee. Walking `ranked` rather than the groups means the
    # row reserved for a combination is that combination's cheapest, and that
    # the combinations reached first are the cheapest ones -- so when `limit`
    # is too small to reach them all, what gets shown is still the best of
    # what was found.
    for position, (index, _row) in enumerate(ranked):
        if taken >= limit:
            break
        if reserved.get(index, 0) >= min_per_combo:
            continue
        reserved[index] = reserved.get(index, 0) + 1
        chosen[position] = True
        taken += 1

    # Pass 2: fill what is left of `limit` by price, as before.
    for position in range(len(ranked)):
        if taken >= limit:
            break
        if chosen[position]:
            continue
        chosen[position] = True
        taken += 1

    selected = [ranked[i][1] for i in range(len(ranked)) if chosen[i]]
    combo_of_row = [ranked[i][0] for i in range(len(ranked)) if chosen[i]]
    shown = {ranked[i][0] for i in range(len(ranked)) if chosen[i]}
    answered = {index for index, _row in tagged}
    hidden = [
        describe_combination(groups[index][0])
        for index in sorted(answered - shown)
    ]
    return selected, [name for name in hidden if name], combo_of_row


def _select_rows(
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    sort_by: str,
    limit: int,
    min_per_combo: int = MIN_ROWS_PER_COMBO,
) -> tuple[list[dict[str, Any]], list[str]]:
    """`_select_rows_by_combo` without the per-row combination indices."""
    rows, hidden, _combos = _select_rows_by_combo(
        groups, sort_by, limit, min_per_combo
    )
    return rows, hidden


def _append_coverage_note(coverage: dict[str, Any], note: str | None) -> None:
    """Add one sentence to `search_coverage.note`, keeping what is there."""
    if not note:
        return
    existing = coverage.get("note")
    coverage["note"] = f"{existing} {note}" if existing else note


def _note_hidden_combinations(
    coverage: dict[str, Any], hidden: list[str], limit: int
) -> None:
    """Say, in `search_coverage`, that `limit` hid whole combinations.

    Mutates the coverage dict in place. `truncated` already means "this
    answer does not cover everything that was asked for" -- it was set when
    the fan-out cap dropped searches, and a `limit` that drops entire
    answered searches is the same claim about the same field. The note names
    the combinations, because "some are missing" sends a caller back to
    re-run the search while "2026-10-08 to LIS is missing" does not.
    """
    if not hidden:
        return
    coverage["truncated"] = True
    named = hidden
    if len(named) > MAX_NAMED_COMBINATIONS:
        named = named[:MAX_NAMED_COMBINATIONS] + [
            f"and {len(hidden) - MAX_NAMED_COMBINATIONS} more"
        ]
    note = (
        f"`limit` was {limit}, which is fewer than the number of "
        "date/destination combinations that returned flights, so "
        f"{len(hidden)} of them have no row in `results`: "
        f"{', '.join(named)}. Those searches ran and found flights; the "
        "answer simply had no room for them. Raise `limit` (roughly "
        "rows-per-combination x dates x destinations) to see them."
    )
    _append_coverage_note(coverage, note)


#: The ceiling on the automatic `limit` raise below.
#:
#: Deliberately larger than either server's hard fan-out cap (15 free, 60
#: paid), so the raise always reaches every combination a call can possibly
#: search. It exists so that raising a fan-out cap later cannot silently turn
#: one tool call into an unbounded response.
MAX_AUTO_LIMIT = 60


def _effective_limit(limit: int, combinations: int) -> tuple[int, str | None]:
    """`limit`, raised to cover every combination this call will search.

    Returns `(limit, note)`; `note` is None when nothing was changed.

    A `limit` below the number of date/destination combinations cannot show
    them all, and what it drops is not "some extra rows" -- it is whole
    searches that ran and answered. The per-combination floor in
    `_select_rows` makes that visible instead of silent, but visible is the
    consolation prize. The fix a caller actually wants is to not lose the
    searches at all, and the server knows the number of combinations before
    it knows anything else, so it can simply ask for enough rows.

    Raised, never lowered: an explicit `limit: 200` is left alone. The raise
    is capped at MAX_AUTO_LIMIT so a caller cannot turn a small `limit` into
    an unbounded response by widening the fan-out.

    Deliberately not an error. Rejecting the call would be the strictest
    reading of "catch it before the model sees anything", and it would fail a
    search over a default argument the model never chose -- `limit` defaults
    to 10 and five destinations over three dates is fifteen combinations, so
    the common flexible search would start erroring. The response says what
    was done in `search_coverage.note`.
    """
    if combinations <= 0 or limit >= combinations:
        return limit, None
    raised = min(combinations, MAX_AUTO_LIMIT)
    if raised <= limit:
        return limit, None
    note = (
        f"`limit` was {limit}, fewer than the {combinations} date/destination "
        f"combinations this search covers, so it was raised to {raised} before "
        "the search ran. Below that, whole combinations -- searches that ran "
        "and found flights -- would have had no row in `results`. Pass a "
        "larger `limit` to see more than one fare per combination."
    )
    return raised, note


def _row_price(row: dict[str, Any]) -> float | None:
    """The fare on a row, one-way or round-trip, or None."""
    for field_name in ("price_as_number", "total_price_as_number"):
        value = row.get(field_name)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _cheapest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The lowest-priced of `rows`, or None. Unpriced rows sort last."""
    if not rows:
        return None
    return min(rows, key=_row_sort_key("price"))


#: Why a requested destination (or one of its dates) has no rows in
#: `results`. Every value is a fact about OUR pipeline, not a guess about
#: the route:
#:
#:   ok            -- it has rows in `results`
#:   no_flights    -- searched, answered, and Google had no itineraries
#:   search_failed -- searched and the search errored; nothing is known
#:   not_in_limit  -- searched, found flights, and no row fit in `limit`
#:   not_searched  -- never searched; the fan-out cap sampled it away
#:
#: Declared as an enum in src/output_schema.py (DESTINATION_REASONS); add a
#: value there and here together or a validating client rejects the response.


def _combo_key(combo: dict[str, str]) -> tuple[str, str]:
    return (
        str(combo.get("to_airport") or ""),
        str(combo.get("departure_date") or ""),
    )


def _by_destination(
    plan: SearchPlan,
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    failed_combos: list[dict[str, str]],
    rows: list[dict[str, Any]],
    combo_of_row: list[int],
) -> dict[str, dict[str, Any]]:
    """One entry per REQUESTED destination, present whether or not it has rows.

    Why this exists (a reader of the 2026-09-08 r/AI_Agents post made the
    point, and he is right): `search_coverage.truncated` is one more boolean
    in a response, and a boolean is a thing a model can read and not act on.
    A fixed shape is not. If every destination the caller asked for has an
    entry, then a destination with an empty `rows` array is a hole the model
    has to look at to summarise the answer at all -- it cannot skip what is
    sitting in the structure it is reading.

    So the keys here are the destinations from the REQUEST, in request order,
    not the destinations that came back. A destination the fan-out cap never
    searched, one whose searches all failed, and one Google genuinely has no
    flights for are three different facts, and each gets its own `reason`
    rather than all three arriving as absence.

    `rows` are the rows for that destination that are in `results` -- the same
    objects, the same order, no second set of data and nothing hidden here
    that is not in the answer. `cheapest` is the lowest-priced of them, which
    on a `sort_by: "duration"` search is the cheapest of what was selected
    rather than of everything found; the entry is a view of the answer, not a
    second search.

    `dates` appears only on multi-date searches, for the same reason as the
    destination keys: on "cheapest week in October", a date that was sampled
    away is exactly the thing a reader assumes was checked.
    """
    requested_keys = [
        _combo_key(c) for c in (plan.requested_combos or plan.combos)
    ]
    executed_keys = {_combo_key(c) for c in plan.combos}
    failed_keys = {_combo_key(c) for c in failed_combos}
    # Rows the BACKEND returned per combination, before `limit` cut anything.
    # The difference between this and the selected rows is what separates
    # "found nothing" from "found something that did not fit".
    returned: dict[tuple[str, str], int] = {}
    for combo, combo_rows in groups:
        key = _combo_key(combo)
        returned[key] = returned.get(key, 0) + len(combo_rows)

    # Rows in the ANSWER per combination, mapped positionally: `rows` has been
    # through `_annotate_book_labels`, which copies, so matching on object
    # identity would find nothing.
    # Carries the position so a destination spread over several dates can be
    # put back into `results` order without comparing row dicts for equality
    # -- two identical fares on two dates are equal and are not the same row.
    selected: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for position, (row, index) in enumerate(zip(rows, combo_of_row)):
        if 0 <= index < len(groups):
            selected.setdefault(_combo_key(groups[index][0]), []).append(
                (position, row)
            )

    def reason_for(keys: list[tuple[str, str]], row_count: int) -> tuple[bool, str]:
        searched = any(key in executed_keys for key in keys)
        if row_count:
            return searched, "ok"
        if not searched:
            return False, "not_searched"
        if any(returned.get(key, 0) for key in keys):
            return True, "not_in_limit"
        if any(key in failed_keys for key in keys):
            return True, "search_failed"
        return True, "no_flights"

    dates_requested = plan.requested_departure_dates
    multi_date = len(dates_requested) > 1

    out: dict[str, dict[str, Any]] = {}
    for destination in plan.requested_destinations:
        keys = [key for key in requested_keys if key[0] == destination]
        dest_rows = [
            row
            for _position, row in sorted(
                pair for key in keys for pair in selected.get(key, [])
            )
        ]
        searched, reason = reason_for(keys, len(dest_rows))
        entry: dict[str, Any] = {
            "rows": dest_rows,
            "cheapest": _cheapest_row(dest_rows),
            "searched": searched,
            "reason": reason,
        }
        if multi_date:
            per_date: dict[str, dict[str, Any]] = {}
            for day in dates_requested:
                key = (destination, day)
                if key not in keys:
                    # Never requested for this destination -- a round trip
                    # whose return date fell before this departure date, for
                    # one. Claiming it as a hole would be inventing one.
                    continue
                day_rows = [row for _position, row in selected.get(key, [])]
                day_searched, day_reason = reason_for([key], len(day_rows))
                cheapest = _cheapest_row(day_rows)
                per_date[day] = {
                    "searched": day_searched,
                    "reason": day_reason,
                    "row_count": len(day_rows),
                    "cheapest_price": (
                        _row_price(cheapest) if cheapest is not None else None
                    ),
                }
            entry["dates"] = per_date
        out[destination] = entry
    return out


def _has_no_substantive_data(result: Any) -> bool:
    """Ad-suppression predicate handed to Lulu's middleware.

    True means "attach no sponsored card". Used for zero-result and blocked
    answers, which are not errors but carry nothing for an ad to sit beside.
    """
    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict):
        return False
    if payload.get("blocked"):
        return True
    results = payload.get("results")
    return isinstance(results, list) and len(results) == 0


# --- use_fallback -----------------------------------------------------------
# FastMCP 3.4.7 does not turn a docstring `Args:` entry into a JSON-schema
# `description`, and every tool here passes an explicit `description=`, which
# overrides the docstring outright -- so the `Args:` blocks below are read by
# humans only and never reach the model. Anything a model needs in order to set
# a parameter has to travel in `Field(description=...)`, which does show up in
# `tools/list`.
#
# This parameter needs it more than most: the upstream field is tri-state, and
# both wrong values cost something real. `false` opts the caller out of the
# backend's last-resort retry; `true` runs the fallback client inline on every
# attempt, which is the path that hangs until the gateway cuts it off.
USE_FALLBACK_DESCRIPTION = (
    "Leave unset. Switches the search to a second, independent flight data "
    "source instead of the usual Google Flights page read. Unset already "
    "escalates to that source once, automatically, after a search's retries "
    "have failed. true forces it inline on every attempt -- much slower, and it "
    "can time out. false disables it entirely, that automatic retry included."
)


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or load_settings()

    # Read before `instructions` rather than at tool-registration time,
    # because the instructions have to describe the tools this process
    # actually registers. Empty url or secret means no hotel tools at all;
    # registering tools that can only 500 is worse than not offering them.
    hotels_enabled = bool(settings.hotels_lambda_url and settings.hotels_auth)

    # Built once from the configured caps rather than written out as prose,
    # so the number a model is told and the number that refuses it cannot
    # drift apart. It goes in the instructions AND on every tool description:
    # hosts differ in which of the two they show a model, and a limit only
    # one of them mentions is a limit half the clients discover by hitting it.
    fair_use_text = (
        upgrade_tail(
            settings.fair_use_day_cap,
            settings.fair_use_month_cap,
            # Only describe an anonymous tier when one exists. As shipped
            # nothing is served anonymously, so a sentence about a 10-a-day
            # allowance would be a limit the model quotes and no caller can
            # ever hit.
            settings.anon_day_cap if anon_mode() == ANON_MODE_OPEN else 0,
        )
        if settings.fair_use_enabled
        else None
    )

    mcp = FastMCP(
        name="flight-powers-free",
        version="0.1.0",
        instructions=build_instructions(hotels_enabled, fair_use_text),
    )

    def described(description: str) -> str:
        """A tool description with the fair-use and upgrade tail on it."""
        return f"{description}\n\n{fair_use_text}" if fair_use_text else description

    # The shared httpx client is built on first use, not here. Constructing
    # it costs ~24 ms of CPU (httpcore, h11, h2, socksio, certifi, plus the
    # transport), and the majority of invocations on this server are cold
    # starts that only answer initialize / tools/list and never open a
    # socket. Every consumer below asks for it at request time instead, so
    # the process-wide pool and its keep-alives are unchanged.
    telemetry = Telemetry(
        store=build_counter_store(
            client_factory=lambda: get_shared_client(settings)
        ),
        daily_budget=settings.daily_backend_call_budget,
        degrade_at=settings.budget_degrade_at,
        log_path=settings.log_path or None,
        fair_use_enabled=settings.fair_use_enabled,
        fair_use_day_cap=settings.fair_use_day_cap,
        fair_use_month_cap=settings.fair_use_month_cap,
        fair_use_gateway_day_cap=settings.fair_use_gateway_day_cap,
        fair_use_gateway_month_cap=settings.fair_use_gateway_month_cap,
        fair_use_hard_after=settings.fair_use_hard_after,
    )
    if settings.fair_use_enabled and not telemetry.durable_counters:
        logger.warning(
            "fair use is enabled but no shared counter store is configured. "
            "On serverless the per-client counters are per-instance, so the "
            "caps of %d/day and %d/month will not be reached and nothing will "
            "be refused. Set UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN.",
            settings.fair_use_day_cap,
            settings.fair_use_month_cap,
        )
    if settings.daily_backend_call_budget > 0 and not telemetry.durable_counters:
        logger.warning(
            "DAILY_BACKEND_CALL_BUDGET is set but no shared counter store is "
            "configured. On a single always-on process this is fine. On "
            "serverless it is NOT enforceable -- counters are per-instance, so "
            "real spend will exceed the budget silently. Set "
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN."
        )
    def _note_signed_in_usage(headers: dict[str, str], spent: int) -> None:
        """Record a signed-in call against the account, in the background.

        Best effort, and deliberately so: it runs after the tool has already
        produced its answer, it opens a Neon connection, and a serverless
        instance that freezes between the response and the write loses the
        increment. `call_count` and the per-day rows are therefore a FLOOR,
        which the migration says out loud -- the exact per-client numbers are
        the Upstash fair-use counters, and those are what actually refuse a
        call. What this buys is the one thing Upstash cannot answer: WHO,
        with an email address attached, for the daily read and for the
        phase-2 cap note.

        The `sub` comes from the header the OAuth gate injects after a token
        validates and strips from every inbound request, so it cannot be set
        by the caller. If that strip is ever removed, this becomes a way to
        write rows for somebody else's account.
        """
        oauth = getattr(mcp, "fp_oauth", None)
        if oauth is None or oauth.users is None or spent <= 0:
            return
        sub = (headers.get(SIGNED_IN_HEADER) or "").strip()
        if not sub:
            return

        async def go() -> None:
            try:
                await oauth.users.touch(sub, spent)
            except Exception as exc:  # noqa: BLE001 - never fail a tool call
                logger.debug("could not record signed-in usage: %s", exc)

        try:
            asyncio.create_task(go())
        except RuntimeError:  # pragma: no cover - no running loop, e.g. a test
            pass

    classifier = ClientClassifier()

    # ── Lulu wiring ──────────────────────────────────────────────────────
    # Two-step (widget + middleware) rather than the one-line
    # enable_lulu_ads, because only the middleware path accepts
    # is_error_result, which is what keeps ads off empty results. With two
    # tools, the "easy to forget the _meta on a new tool" problem that
    # enable_lulu_ads exists to solve does not apply.
    # Registered before any ads middleware, deliberately: FastMCP builds its
    # chain with `reversed(self.middleware)`, so the first one registered is
    # the outermost and runs LAST on the way out -- the only position from
    # which it can see the ad the SDK just attached. Registered even with ads
    # off, so the two configurations run the same chain and a test with ads
    # disabled is testing the deployed ordering.
    mcp.add_middleware(SponsoredTextSyncMiddleware())

    app_config: Any = None
    widget_configs: dict[str, Any] = {}
    if settings.ads_enabled:
        try:
            from lulu_ads.middleware import LuluAdsMiddleware
            from lulu_ads.widget import register_sponsored_widget

            app_config = register_sponsored_widget(
                mcp,
                # MUST equal the public connector URL exactly. Lulu derives
                # Claude's _meta.ui.domain from this and the widget silently
                # never renders on a mismatch -- $0 CPM with no error.
                endpoint_url=settings.public_url,
                text="Sponsored",
                url="https://getlulu.dev",
            )
            # Kept as the fallback below, not because both are served: if the
            # result widgets fail to register the tools still get an ad card,
            # just one that earns clicks only.
            try:
                widget_configs = {
                    "search_oneway_flights": _register_result_widget(
                        mcp,
                        "search_oneway_flights",
                        mapping=ONEWAY_WIDGET_MAPPING,
                        endpoint_url=settings.public_url,
                    ),
                    "search_roundtrip_flights": _register_result_widget(
                        mcp,
                        "search_roundtrip_flights",
                        mapping=ROUNDTRIP_WIDGET_MAPPING,
                        endpoint_url=settings.public_url,
                    ),
                }
                if hotels_enabled:
                    # Hotel results carry the ad too -- a result widget is the
                    # only surface that fires the rendered-impression beacon,
                    # so a hotel search without one is structurally $0 CPM.
                    for hotel_tool in ("search_hotels", "find_hotel_by_name"):
                        widget_configs[hotel_tool] = _register_result_widget(
                            mcp,
                            hotel_tool,
                            mapping=HOTELS_WIDGET_MAPPING,
                            endpoint_url=settings.public_url,
                        )
            except Exception as exc:  # noqa: BLE001 - degrade, never break search
                widget_configs = {}
                logger.error(
                    "result widgets did not register (%s); falling back to the "
                    "sponsored card, which carries no impression beacon -- "
                    "clicks will still pay, CPM will be zero",
                    exc,
                )
            mcp.add_middleware(
                LuluAdsMiddleware(
                    publisher_id=settings.lulu_publisher_id or None,
                    api_key=settings.lulu_api_key or None,
                    is_error_result=_has_no_substantive_data,
                )
            )
            if not (settings.lulu_publisher_id and settings.lulu_api_key):
                logger.warning(
                    "Lulu credentials are not set -- the SDK is inert, so "
                    "flights still work but no ads will be served and there "
                    "is no CPM. Set LULU_ADS_PUBLISHER_ID and LULU_ADS_API_KEY."
                )
        except Exception as exc:  # noqa: BLE001 - ads must never break search
            logger.error("could not wire Lulu ads (%s); serving without ads", exc)
            app_config = None

    def tool_kwargs(tool_name: str) -> dict[str, Any]:
        """MCP Apps + ChatGPT widget wiring for one tool."""
        cfg = widget_configs.get(tool_name)
        if cfg is not None:
            return {
                "app": cfg,
                "meta": {"openai/outputTemplate": cfg.resource_uri},
            }
        return {"app": app_config} if app_config else {}

    # ── shared execution path ────────────────────────────────────────────

    async def _run(
        tool_name: str,
        plan_builder,
        payload_builder,
        sort_by: str,
        limit: int,
        route: RouteRecord,
    ) -> dict[str, Any] | ToolResult:
        started = time.perf_counter()
        headers, peer, query = _request_context()
        source_ip = extract_source_ip(headers, peer)
        client_name = _client_name()
        fingerprint = client_fingerprint(headers)
        # No-op after the first successful load. Serverless has no awaitable
        # startup hook, so the feed is fetched here instead. It has to happen
        # before the fair-use identity is chosen: the OpenAI feed is one of
        # the ranges that decides whether this caller is pooled.
        await classifier.ensure_openai_ranges()
        # A different digest from the fingerprint on purpose: no session id in
        # it for a direct caller, because the client this cap exists for opens
        # a new session for every request and a counter keyed on the
        # fingerprint would reset before it counted to two. Behind a gateway
        # the trade runs the other way and the session is what separates two
        # people; `identify` picks. See fair_use.py.
        identity = (
            fair_use_identify(
                headers,
                query,
                peer=peer,
                gateway_networks=classifier.gateway_networks(
                    settings.fair_use_gateway_cidrs
                ),
                gateway_user_agents=settings.fair_use_gateway_user_agents,
                identity_params=settings.fair_use_identity_params,
            )
            if settings.fair_use_enabled
            else None
        )
        fu_key = identity.key if identity else None
        fu_kind = identity.kind if identity else None
        tier = classifier.classify(source_ip, client_name)
        decision = decide(
            tier,
            mode=settings.enforcement_mode,
            blocked_tiers=settings.blocked_tiers,
            full_cap=settings.max_backend_calls_per_tool_call,
            openai_ranges_loaded=classifier.openai_ranges_loaded,
        )

        async def log(
            *,
            requested: int,
            calls: int,
            failures: int,
            results: int,
            truncated: bool,
            ad_eligible: bool,
            error: str | None,
            fair_use_blocked: bool = False,
        ) -> None:
            _note_signed_in_usage(headers, calls)
            await telemetry.record(
                CallRecord(
                    timestamp=time.time(),
                    tool=tool_name,
                    tier=tier,
                    client_name=client_name,
                    source_ip=source_ip,
                    widget_capable=decision.widget_capable,
                    requested_combinations=requested,
                    backend_calls=calls,
                    backend_failures=failures,
                    results_returned=results,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    truncated=truncated,
                    allowed=decision.allowed,
                    decision_reason=decision.reason,
                    ad_eligible=ad_eligible,
                    error=error,
                    fingerprint=fingerprint,
                    route=route,
                    fair_use_key=fu_key,
                    fair_use_kind=fu_kind,
                    fair_use_blocked=fair_use_blocked,
                )
            )

        if not decision.allowed:
            await log(
                requested=0,
                calls=0,
                failures=0,
                results=0,
                truncated=False,
                ad_eligible=False,
                error="blocked",
            )
            return {
                "blocked": True,
                "results": [],
                "message": (
                    "This free flight search is available through AI assistants "
                    "that can display the sponsored card that funds it. "
                    f"({decision.reason})"
                ),
            }

        # Fair use. One extra read per tool call, and only for a caller we can
        # actually identify: a request with neither header is not counted and
        # not refused, because refusing on "no headers at all" would refuse
        # every stdio user on the strength of nothing.
        fair_use: FairUseState | None = None
        if identity:
            # `anon_caps`, not `settings.anon_day_cap`: the taster tier only
            # exists under the FREE_ANON_MODE=open rollback, and a cap nobody
            # can reach must not shape the numbers a caller is shown.
            anon_day, anon_month = anon_caps(settings)
            day_cap, month_cap = fair_use_caps_for(
                identity.kind,
                day_cap=settings.fair_use_day_cap,
                month_cap=settings.fair_use_month_cap,
                gateway_day_cap=settings.fair_use_gateway_day_cap,
                gateway_month_cap=settings.fair_use_gateway_month_cap,
                anon_day_cap=anon_day,
                anon_month_cap=anon_month,
            )
            used_today, used_month = await telemetry.fair_use_usage(identity.key)
            fair_use = FairUseState(
                key=identity.key,
                used_today=used_today,
                used_month=used_month,
                day_cap=day_cap,
                month_cap=month_cap,
                kind=identity.kind,
                signed_in_day_cap=settings.fair_use_day_cap,
                signed_in_month_cap=settings.fair_use_month_cap,
            )
            if fair_use.blocked:
                logger.info("%s", fair_use_log_line(fair_use, "block"))
                await log(
                    requested=0,
                    calls=0,
                    failures=0,
                    results=0,
                    truncated=False,
                    ad_eligible=False,
                    error="fair_use_blocked",
                    fair_use_blocked=True,
                )
                return rate_limited_result(fair_use)

        cap, budget_note = await telemetry.cap_for_budget(decision.cap)

        # A cap is only a cap if the last call of the day cannot overshoot it.
        # Without this, a client at 149 of 150 gets a full 15-way fan-out and
        # ends the day at 164 -- which is also how a month cap turns into a
        # suggestion, thirty times over.
        if fair_use is not None:
            cap = min(cap, fair_use.remaining)

        try:
            plan = plan_builder(cap)
        except PlanError as exc:
            await log(
                requested=0,
                calls=0,
                failures=0,
                results=0,
                truncated=False,
                ad_eligible=False,
                error=str(exc),
            )
            raise ToolError(str(exc)) from exc

        # The plan is what turns "one call" into a cost, so the log line and the
        # route histogram carry its number rather than a 1. A blocked call or a
        # rejected plan keeps 0, which is exactly what it spent.
        route.combinations = plan.requested_combinations

        # Up front, before anything is searched: a `limit` that cannot cover
        # the fan-out is a defect in the request, not something to discover in
        # the response. See _effective_limit.
        limit, raised_limit_note = _effective_limit(
            limit, plan.executed_combinations
        )

        if budget_note:
            plan.degraded_reason = budget_note

        # Reuses the process-wide connection pool; LambdaClient does not
        # close a client it was handed.
        # One entry per answered request: that response's `X-Search-*`
        # headers, plus the date/destination it was for under COMBO_KEYS, so
        # the coverage line can NAME what is missing. A list, not a dict --
        # each date and destination combination has its own outcome, and
        # last-writer-wins would hide a failed one behind a healthy one.
        # Internal only; none of this reaches the wire.
        search_outcomes: list[dict[str, str]] = []

        async with LambdaClient(
            settings.base_lambda_url,
            settings.rapid_auth,
            settings.lambda_timeout_seconds,
            client=get_shared_client(settings),
            source=settings.fp_source,
            tool=tool_name,
        ) as client:

            async def run_search(endpoint: str, payload: dict[str, Any]):
                # A private per-call sink, merged into the shared one with the
                # combination attached. The shared list is appended to from
                # many concurrent searches at once, so reading "the entry my
                # call just added" off the end of it is a race; a local list
                # is not.
                sink: list[dict[str, str]] = []
                try:
                    return await client.search(
                        endpoint, payload, outcome_sink=sink
                    )
                finally:
                    for entry in sink:
                        search_outcomes.append({**entry, **_combo_of(payload)})

            outcome: FanoutResult = await execute_plan(
                plan,
                build_payload=payload_builder,
                run_search=run_search,
                max_concurrency=settings.max_concurrent_backend_calls,
            )

        # Every single search failed -- that is an outage, not an empty result.
        if outcome.backend_failures == plan.executed_combinations:
            await log(
                requested=plan.requested_combinations,
                calls=outcome.backend_calls_made,
                failures=outcome.backend_failures,
                results=0,
                truncated=plan.truncated,
                ad_eligible=False,
                error=outcome.first_error,
            )
            raise ToolError(
                f"Flight search is temporarily unavailable ({outcome.first_error})"
            )

        # Not a slice off the merged list: that dropped whole destinations
        # whose cheapest fare fell past `limit` while still naming them in
        # search_coverage. See _select_rows.
        rows, hidden_combos, combo_of_row = _select_rows_by_combo(
            outcome.results_by_combo, sort_by, limit
        )
        ad_eligible = len(rows) > 0

        await log(
            requested=plan.requested_combinations,
            calls=outcome.backend_calls_made,
            failures=outcome.backend_failures,
            results=len(rows),
            truncated=plan.truncated,
            ad_eligible=ad_eligible,
            error=None,
        )

        # Widget affordances, computed after the rows are final (sorted,
        # deduped, capped) because both describe what is actually on the
        # card: the Book cell belongs to a row that has a link, and the fare
        # band describes the row the table opens with.
        rows = _annotate_book_labels(rows)
        coverage = plan.coverage()
        _note_hidden_combinations(coverage, hidden_combos, limit)
        _append_coverage_note(coverage, raised_limit_note)
        fare_band = _fare_band(rows)

        response: dict[str, Any] = {
            "results": rows,
            "result_count": len(rows),
            # One entry per destination the caller ASKED for, empty ones
            # included. See _by_destination.
            "by_destination": _by_destination(
                plan,
                outcome.results_by_combo,
                outcome.failed_combos,
                rows,
                combo_of_row,
            ),
            "search_coverage": coverage,
            "widget_eyebrow": _widget_eyebrow(tool_name, coverage, fare_band),
        }
        if fare_band:
            response["fare_band"] = fare_band
        if outcome.backend_failures:
            response["partial"] = (
                f"{outcome.backend_failures} of {plan.executed_combinations} "
                "searches failed; results cover the rest."
            )

        # A search that answered HTTP 200 with `[]` may still have failed: the
        # backend reports which in `X-Search-Status`. Until this was read, the
        # tool answered a failed scrape with "No flights were found ... try a
        # different date", which a model repeats to the user as fact.
        incomplete, reported, reason = _search_outcome_summary(search_outcomes)
        is_degraded = False
        if reported:
            if not rows:
                is_degraded = bool(incomplete)
                response["search_status"] = "degraded" if incomplete else "empty"
            else:
                response["search_status"] = "partial" if incomplete else "ok"

        if not rows:
            if incomplete:
                response["message"] = _degraded_message(incomplete, reported, reason)
            else:
                response["message"] = (
                    "No flights were found for this search. Google Flights returns "
                    "nothing for some route and date combinations; try a different "
                    "date or a nearby airport. use_fallback: true would re-run this "
                    "through a slower alternate source that can time out, so change "
                    "the date or airport first."
                )
        elif incomplete:
            note = _incomplete_note(incomplete, reported)
            response["partial"] = (
                f"{response['partial']} {note}" if "partial" in response else note
            )

        # The fair-use block. On EVERY successful result since 2026-09-09,
        # not only past 80%: a signed-in user's first 119 searches used to
        # carry no number at all, so nobody -- neither the person nor the
        # model deciding whether to mention the paid server -- could see
        # where they stood until the allowance was nearly gone. The compact
        # receipt (`usage_note`) is the default; from 80% of either cap it is
        # replaced by the richer warning, which brings the directions and the
        # `upgrade` object with it. This call's own spend is already counted
        # either way, so the number is where they stand now and not where
        # they stood a moment ago.
        if fair_use is not None:
            spent = fair_use.after(outcome.backend_calls_made)
            who = identity.email if identity else ""
            response["fair_use"] = fair_use_usage_note(spent, who)
            if spent.warning:
                response["fair_use"] = fair_use_note(spent, who)
                # Same object a refusal carries: a warning that points at an
                # upgrade path which is not actually in the result is just a
                # sentence the model cannot act on.
                response["upgrade"] = upgrade_block(
                    spent.day_cap,
                    spent.month_cap,
                    signed_in=spent.signed_in,
                    signed_in_day_cap=spent.signed_in_day_cap,
                    signed_in_month_cap=spent.signed_in_month_cap,
                )
                logger.info("%s", fair_use_log_line(spent, "warn"))

        # A degraded search is a failed call, and the MCP spec has one way to
        # say so: `isError: true` on the result. Until now the failure was
        # carried by `search_status: "degraded"` inside the payload, which is
        # only as good as the host's willingness to show `structuredContent`
        # to the model -- and the spec requires no host to show it at all. It
        # says the opposite about errors: "Clients SHOULD provide tool
        # execution errors to language models to enable self-correction," and
        # lists "API failures" as exactly that kind of error. Every
        # combination failing IS an API failure, so the flag is the only
        # channel to the model that does not depend on host behaviour.
        #
        # The payload still rides along -- `ToolResult` carries
        # structured_content next to is_error -- so the caller keeps the
        # coverage and the explanation. Passing no `content` is deliberate:
        # FastMCP then derives the text block from the same dict with the same
        # serializer, which is both the backwards-compatible duplicate the
        # spec asks for and the single auto-generated TextContent that
        # LuluAdsMiddleware requires before it will keep content[] in sync
        # with an injected ad.
        #
        # No ad revenue moves. The middleware skips any result whose is_error
        # is set, and a degraded search has zero rows, which
        # `_has_no_substantive_data` already suppressed the ad for.
        #
        # Only `degraded` is an error. `empty` is a true negative and a real
        # answer; `partial` carries results a caller can use. Flagging either
        # would throw away good data over a caveat.
        # ── the first line of the text block ─────────────────────────
        #
        # Everything above puts the outcome in `structuredContent`, where a
        # client that reads `outputSchema` will find it. Most models never see
        # that; they see the text block, which was the serialized JSON and
        # nothing else -- so `"search_status": "degraded"` sat mid-object in
        # the same register as `"currency": "usd"`. Prose first, JSON after.
        # See src/status_text.py for the reasoning and the source.
        #
        # On this server the ad rides on the same result, which is why
        # SponsoredTextSyncMiddleware exists: it re-syncs block 1 with the
        # injected `sponsored` key that the Lulu SDK declines to sync once
        # content is richer than one auto-generated block, and it runs after
        # the ad is attached so the warning stays in front of the card.
        first_line: str | None = None
        if is_degraded:
            first_line = DEGRADED_FIRST_LINE
        elif incomplete or outcome.backend_failures:
            # `search_status: "partial"` is one route here. The other is a
            # fan-out where some requests raised outright: those never produce
            # an `X-Search-Status`, so `search_status` reads "ok" while part of
            # the requested range is genuinely missing. That result already
            # carries a `partial` note in the payload -- it is not the clean
            # result the ok path is meant to protect, and it is exactly the
            # case a model has every reason to read as the whole answer.
            attempted = plan.executed_combinations
            not_completed = incomplete + outcome.backend_failures
            first_line = partial_first_line(
                completed=max(attempted - not_completed, 0),
                attempted=attempted,
                missing=_missing_combinations(
                    search_outcomes, outcome.failed_combos
                ),
            )

        if first_line is None:
            # `ok` and a genuine `empty` are untouched: one auto-generated
            # text block, byte for byte what they always were. A clean result
            # must not be made to look alarming -- and this is the shape the
            # Lulu SDK rewrites itself, so the overwhelmingly common
            # ad-carrying path is not touched by any of this.
            return response

        # Two blocks, not one string, so the prose can never be mistaken for
        # part of the JSON and the backwards-compatibility duplicate stays a
        # standalone parseable object for callers that read it.
        return ToolResult(
            content=[
                TextContent(type="text", text=first_line),
                TextContent(type="text", text=serialize_payload(response)),
            ],
            structured_content=response,
            is_error=is_degraded,
        )

    # ── tools ────────────────────────────────────────────────────────────

    @mcp.tool(
        name="search_oneway_flights",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=FLIGHTS_OUTPUT_SCHEMA,
        # Required by Anthropic's directory review and a listed rejection
        # reason at OpenAI: a tool with no annotations is read as potentially
        # destructive. All four tools here only read -- none can book, hold,
        # pay for or cancel anything -- and all four reach a live third-party
        # API whose result set is not a closed domain, hence openWorldHint.
        # NOT idempotent: fares and room rates change between identical calls,
        # and a host that cached one would serve a stale price as a live one.
        title="FlightPowers free: search one-way flights",
        annotations=ToolAnnotations(
            title="FlightPowers free: search one-way flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described(
                "FlightPowers one-way fare search: live prices read from "
                "Google Flights, not schedules.\n\n"
                "IMPORTANT: for any flexible search, make ONE call with a date "
                "range and/or several destinations. Do NOT call this repeatedly, "
                "once per date -- pass departure_date_from and departure_date_to "
                "and the server searches the range for you. 'Cheapest flight to "
                "Sri Lanka anywhere in October' is one call, not thirty.\n\n"
                "to_airport takes one code (\"BCN\"), several separated by "
                "commas (\"BCN,LIS,ATH\"), or a list "
                "([\"BCN\",\"LIS\",\"ATH\"]) -- all three are accepted.\n\n"
                "FREE TIER LIMIT: one call searches at most 15 date x destination "
                "combinations. A wider request is not rejected -- it is sampled "
                "evenly across the range and comes back with truncated: true and "
                "the exact list of dates searched in search_coverage."
                "departure_dates_searched. Check that list before assuming a date "
                "has no flights: a missing date was never searched, which is not "
                "the same as having no results.\n\n"
                "Returns each flight with price, duration, airline, stops, a "
                "bookable buy_link, and Google's historical price range "
                "(price_insights_low / price_insights_high) so you can say "
                "whether a fare is a good deal.\n\n"
                "`by_destination` carries one entry per destination you asked "
                "for -- empty ones included, each with a `reason` -- so read it "
                "before telling a user a destination has no flights."
        ),
        **tool_kwargs("search_oneway_flights"),
    )
    @document_params
    async def search_oneway_flights(
        from_airport: str,
        to_airport: str | list[str],
        departure_date: str | None = None,
        departure_date_from: str | None = None,
        departure_date_to: str | None = None,
        max_stops: int | None = None,
        airline_codes: list[str] | None = None,
        exclude_airline_codes: list[str] | None = None,
        departure_time_min: int | None = None,
        departure_time_max: int | None = None,
        arrival_time_min: int | None = None,
        arrival_time_max: int | None = None,
        currency: str = "usd",
        max_price: int | None = None,
        seat_type: int | None = None,
        passengers: list[int] | None = None,
        sort_by: str = "best",
        limit: int = 10,
        # Tri-state, matching the backend's own field. None is the default and
        # is dropped from the payload by `_compact`, which is what lets the
        # backend escalate to the fallback client as a last resort. An explicit
        # False would opt our own users out of that; an explicit True runs the
        # fallback inline on every attempt, which is the path that hangs.
        use_fallback: Annotated[
            bool | None, Field(description=USE_FALLBACK_DESCRIPTION)
        ] = None,
    ) -> dict[str, Any] | ToolResult:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV". One origin per
                search; a second one is refused rather than searched.
            to_airport: Destination airport. One IATA code ("BCN"), several
                separated by commas ("BCN,LIS,ATH"), or a list
                (["BCN","LIS","ATH"]) -- every shape is accepted and the
                destinations are compared in the same search.
            departure_date: Single departure date, "YYYY-MM-DD".
            departure_date_from: First date of a departure range.
            departure_date_to: Last date of a departure range.
            max_stops: Maximum stops per flight. 0 means non-stop only.
            airline_codes: Restrict to these airline codes, e.g. ["LY"].
            exclude_airline_codes: Exclude these airline codes.
            departure_time_min: Earliest departure hour, 0-23.
            departure_time_max: Latest departure hour, 0-23.
            arrival_time_min: Earliest arrival hour, 0-23.
            arrival_time_max: Latest arrival hour, 0-23.
            currency: ISO currency code, default "usd".
            max_price: Only return flights at or below this price.
            seat_type: 1 economy, 2 premium economy, 3 business, 4 first.
            passengers: Passenger counts as [adults, children, infants].
            sort_by: "best", "price", or "duration". Applied across all results.
            limit: Maximum flights to return, after merging and sorting.
            use_fallback: See USE_FALLBACK_DESCRIPTION. That text, not this
                line, is what the model actually sees -- see the note there.
        """
        if sort_by not in SORT_CHOICES:
            raise ToolError(f"sort_by must be one of {', '.join(SORT_CHOICES)}")

        def plan_builder(cap: int):
            return plan_oneway(
                from_airport=from_airport,
                to_airport=to_airport,
                departure_date=departure_date,
                departure_date_from=departure_date_from,
                departure_date_to=departure_date_to,
                cap=cap,
            )

        def payload_builder(combo: dict[str, str]) -> dict[str, Any]:
            return build_oneway_payload(
                departure_date=combo["departure_date"],
                from_airport=normalise_origin(from_airport),
                to_airport=combo["to_airport"],
                max_stops=max_stops,
                airline_codes=airline_codes,
                exclude_airline_codes=exclude_airline_codes,
                departure_time_min=departure_time_min,
                departure_time_max=departure_time_max,
                arrival_time_min=arrival_time_min,
                arrival_time_max=arrival_time_max,
                currency=currency,
                max_price=max_price,
                seat_type=seat_type,
                passengers=passengers,
                limit=settings.default_result_limit,
                use_fallback=use_fallback,
            )

        return await _run(
            "search_oneway_flights",
            plan_builder,
            payload_builder,
            sort_by,
            limit,
            RouteRecord(
                origin=_airports_field(from_airport),
                destination=_airports_field(to_airport),
                dates=_dates_field(
                    departure_date, departure_date_from, departure_date_to
                ),
                currency=currency,
                passengers=_pax_field(passengers),
                stops=_stops_field(max_stops),
            ),
        )

    @mcp.tool(
        name="search_roundtrip_flights",
        # Declared, not inferred: see src/output_schema.py.
        output_schema=FLIGHTS_OUTPUT_SCHEMA,
        title="FlightPowers free: search round-trip flights",
        annotations=ToolAnnotations(
            title="FlightPowers free: search round-trip flights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        description=described(
                "FlightPowers round-trip fare search: live prices read from "
                "Google Flights, priced as paired legs rather than two "
                "separate one-ways.\n\n"
                "IMPORTANT: for any flexible search, make ONE call. Pass "
                "departure_date_from / departure_date_to for a departure range, "
                "and `nights` instead of return_date to search trip lengths -- "
                "'5 to 7 nights in Rome sometime in May' is one call.\n\n"
                "to_airport takes one code (\"BCN\"), several separated by "
                "commas (\"BCN,LIS,ATH\"), or a list "
                "([\"BCN\",\"LIS\",\"ATH\"]) -- all three are accepted.\n\n"
                "FREE TIER LIMIT: one call searches at most 15 departure-date x "
                "nights combinations. A wider request is sampled evenly rather "
                "than rejected, and returns truncated: true plus the exact dates "
                "searched in search_coverage.departure_dates_searched. A date "
                "absent from that list was never searched -- which is not the "
                "same as having no flights.\n\n"
                "Returns total price for both legs, per-leg airline, stops and "
                "duration, and a single bookable buy_link covering the trip.\n\n"
                "`by_destination` carries one entry per destination you asked "
                "for -- empty ones included, each with a `reason` -- so read it "
                "before telling a user a destination has no flights."
        ),
        **tool_kwargs("search_roundtrip_flights"),
    )
    @document_params
    async def search_roundtrip_flights(
        from_airport: str,
        to_airport: str | list[str],
        departure_date: str | None = None,
        departure_date_from: str | None = None,
        departure_date_to: str | None = None,
        return_date: str | None = None,
        nights: int | list[int] | None = None,
        max_departure_stops: int | None = None,
        max_return_stops: int | None = None,
        departure_airline_codes: list[str] | None = None,
        return_airline_codes: list[str] | None = None,
        currency: str = "usd",
        max_price: int | None = None,
        seat_type: int | None = None,
        passengers: list[int] | None = None,
        sort_by: str = "best",
        limit: int = 10,
        # Tri-state, matching the backend's own field. None is the default and
        # is dropped from the payload by `_compact`, which is what lets the
        # backend escalate to the fallback client as a last resort. An explicit
        # False would opt our own users out of that; an explicit True runs the
        # fallback inline on every attempt, which is the path that hangs.
        use_fallback: Annotated[
            bool | None, Field(description=USE_FALLBACK_DESCRIPTION)
        ] = None,
    ) -> dict[str, Any] | ToolResult:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV". One origin per
                search; a second one is refused rather than searched.
            to_airport: Destination airport. One IATA code ("BCN"), several
                separated by commas ("BCN,LIS,ATH"), or a list
                (["BCN","LIS","ATH"]) -- every shape is accepted and the
                destinations are compared in the same search.
            departure_date: Single outbound date, "YYYY-MM-DD".
            departure_date_from: First date of an outbound range.
            departure_date_to: Last date of an outbound range.
            return_date: Fixed return date. Use this OR nights, not both.
            nights: Trip length in nights; a number, or a list like [5, 6, 7].
                The return date is derived from each departure date.
            max_departure_stops: Maximum stops on the outbound leg.
            max_return_stops: Maximum stops on the return leg.
            departure_airline_codes: Restrict the outbound leg to these airlines.
            return_airline_codes: Restrict the return leg to these airlines.
            currency: ISO currency code, default "usd".
            max_price: Only return trips at or below this total price.
            seat_type: 1 economy, 2 premium economy, 3 business, 4 first.
            passengers: Passenger counts as [adults, children, infants].
            sort_by: "best", "price", or "duration". Applied across all results.
            limit: Maximum trips to return, after merging and sorting.
            use_fallback: See USE_FALLBACK_DESCRIPTION. That text, not this
                line, is what the model actually sees -- see the note there.
        """
        if sort_by not in SORT_CHOICES:
            raise ToolError(f"sort_by must be one of {', '.join(SORT_CHOICES)}")

        def plan_builder(cap: int):
            return plan_roundtrip(
                from_airport=from_airport,
                to_airport=to_airport,
                departure_date=departure_date,
                departure_date_from=departure_date_from,
                departure_date_to=departure_date_to,
                return_date=return_date,
                nights=nights,
                cap=cap,
            )

        def payload_builder(combo: dict[str, str]) -> dict[str, Any]:
            return build_roundtrip_payload(
                departure_date=combo["departure_date"],
                return_date=combo["return_date"],
                from_airport=normalise_origin(from_airport),
                to_airport=combo["to_airport"],
                max_departure_stops=max_departure_stops,
                max_return_stops=max_return_stops,
                departure_airline_codes=departure_airline_codes,
                return_airline_codes=return_airline_codes,
                currency=currency,
                max_price=max_price,
                seat_type=seat_type,
                passengers=passengers,
                limit=settings.default_result_limit,
                use_fallback=use_fallback,
            )

        return await _run(
            "search_roundtrip_flights",
            plan_builder,
            payload_builder,
            sort_by,
            limit,
            RouteRecord(
                origin=_airports_field(from_airport),
                destination=_airports_field(to_airport),
                dates=_dates_field(
                    departure_date, departure_date_from, departure_date_to
                ),
                nights=_nights_field(nights, return_date),
                currency=currency,
                passengers=_pax_field(passengers),
                stops=_stops_field(max_departure_stops, max_return_stops),
            ),
        )

    # ── hotels ───────────────────────────────────────────────────────────
    # Free tier, so these run against mrabi's own Lambda and every call is his
    # cost -- unlike the paid servers, where the caller's RapidAPI key pays.
    # Registered only when the backend is actually configured.

    if hotels_enabled:

        async def _hotels_run(
            endpoint: str, payload: dict[str, Any], tool: str, route: RouteRecord
        ) -> dict[str, Any]:
            started = time.perf_counter()

            # A hotel search is one backend call and every one is billed to
            # mrabi, so the daily budget is the only thing standing between an
            # automated caller and an unbounded bill. Refuse when it is spent
            # rather than degrade -- there is no smaller version of one call.
            budget = await telemetry.budget_state()
            if budget["enabled"] and budget["exhausted"]:
                raise ToolError(
                    "The daily search budget for the free tier is spent; it "
                    "rolls over within 24 hours. The paid server has no daily "
                    f"cap: sign in at {SIGNIN_HOTELS_URL}, or bring your own "
                    f"RapidAPI key at {PAID_HOTELS_URL}"
                )

            # Same classification path the flight tools use, so hotel calls
            # land in the same by_tier counters rather than a parallel set.
            headers, peer, query = _request_context()
            source_ip = extract_source_ip(headers, peer)
            fingerprint = client_fingerprint(headers)
            await classifier.ensure_openai_ranges()
            identity = (
                fair_use_identify(
                    headers,
                    query,
                    peer=peer,
                    gateway_networks=classifier.gateway_networks(
                        settings.fair_use_gateway_cidrs
                    ),
                    gateway_user_agents=settings.fair_use_gateway_user_agents,
                    identity_params=settings.fair_use_identity_params,
                )
                if settings.fair_use_enabled
                else None
            )
            fu_key = identity.key if identity else None
            fu_kind = identity.kind if identity else None
            tier = classifier.classify(source_ip, _client_name())
            decision = decide(
                tier,
                mode=settings.enforcement_mode,
                blocked_tiers=settings.blocked_tiers,
                full_cap=settings.max_backend_calls_per_tool_call,
                openai_ranges_loaded=classifier.openai_ranges_loaded,
            )
            if not decision.allowed:
                raise ToolError(
                    "This server is limited to hosts where the sponsored card "
                    f"can render ({decision.reason}). The paid server works "
                    f"from anywhere: sign in at {SIGNIN_HOTELS_URL}, or bring "
                    f"your own RapidAPI key at {PAID_HOTELS_URL}"
                )

            # Fair use. A hotel search is exactly one backend call, so there
            # is no fan-out to clamp -- either it fits in the allowance or the
            # caller is told, in the same result shape a search returns, how
            # to keep searching on a key of their own.
            fair_use: FairUseState | None = None
            if identity:
                anon_day, anon_month = anon_caps(settings)
                day_cap, month_cap = fair_use_caps_for(
                    identity.kind,
                    day_cap=settings.fair_use_day_cap,
                    month_cap=settings.fair_use_month_cap,
                    gateway_day_cap=settings.fair_use_gateway_day_cap,
                    gateway_month_cap=settings.fair_use_gateway_month_cap,
                    anon_day_cap=anon_day,
                    anon_month_cap=anon_month,
                )
                used_today, used_month = await telemetry.fair_use_usage(
                    identity.key
                )
                fair_use = FairUseState(
                    key=identity.key,
                    used_today=used_today,
                    used_month=used_month,
                    day_cap=day_cap,
                    month_cap=month_cap,
                    kind=identity.kind,
                    signed_in_day_cap=settings.fair_use_day_cap,
                    signed_in_month_cap=settings.fair_use_month_cap,
                )
                if fair_use.blocked:
                    logger.info("%s", fair_use_log_line(fair_use, "block"))
                    await telemetry.record(
                        CallRecord(
                            timestamp=time.time(),
                            tool=tool,
                            tier=decision.tier,
                            client_name=_client_name(),
                            source_ip=source_ip,
                            widget_capable=decision.widget_capable,
                            requested_combinations=0,
                            backend_calls=0,
                            backend_failures=0,
                            results_returned=0,
                            duration_ms=int(
                                (time.perf_counter() - started) * 1000
                            ),
                            truncated=False,
                            allowed=True,
                            decision_reason=decision.reason,
                            ad_eligible=False,
                            error="fair_use_blocked",
                            fingerprint=fingerprint,
                            route=route,
                            fair_use_key=fu_key,
                            fair_use_kind=fu_kind,
                            fair_use_blocked=True,
                        )
                    )
                    return rate_limited_result(fair_use)

            async with HotelsLambdaClient(
                settings.hotels_lambda_url,
                settings.hotels_auth,
                settings.lambda_timeout_seconds,
                client=get_shared_client(settings),
                source=settings.fp_source,
                tool=tool,
            ) as client:
                try:
                    rows = await client.search(endpoint, payload)
                except LambdaError as exc:
                    logger.error("tool=%s error=%s", tool, exc)
                    raise ToolError(
                        "The hotel search backend did not answer. This is our "
                        "side, not your request -- retrying usually works."
                    ) from exc

            logger.info(
                "tool=%s duration_ms=%d results=%d",
                tool,
                int((time.perf_counter() - started) * 1000),
                len(rows),
            )

            # Affiliate attribution, before the Book labels rather than
            # after: `_annotate_book_labels` decides which rows get a Book
            # cell by looking at `link`, and the widget's `rowLink` resolves
            # the same field -- so the wrap has to be the value already sitting
            # there when either of them reads it. Both hotel tools run through
            # this function, which is why there is one call and not two.
            #
            # A no-op unless STAY22_AID is set. Flights never reach here.
            rows = apply_affiliate_links(
                rows,
                aid=settings.stay22_aid,
                campaign=settings.stay22_campaign,
                booking_aid=settings.stay22_booking_aid,
            )
            rows = _annotate_book_labels(rows)

            response: dict[str, Any] = {
                "results": rows,
                "result_count": len(rows),
                "search_coverage": {
                    "destination": payload.get("destination")
                    or payload.get("hotel_name", ""),
                    "checkin_date": payload.get("checkin_date"),
                    "checkout_date": payload.get("checkout_date"),
                },
            }
            if not rows:
                response["message"] = (
                    "No properties came back for those dates. Try a wider date "
                    "range, a nearby city, or fewer guests."
                )
            response["upgrade"] = upgrade_note("hotels")

            # One hotel search is one backend call, so `spent` is 1. The
            # block rides on every success; the warning shape replaces it
            # from 80% of either cap. See the flights path for why.
            if fair_use is not None:
                spent = fair_use.after(1)
                who = identity.email if identity else ""
                response["fair_use"] = fair_use_usage_note(spent, who)
                if spent.warning:
                    response["fair_use"] = fair_use_note(spent, who)
                    # Overrides the generic ad-tier `upgrade_note` set above:
                    # once the cap is the live issue, the specific fair-use
                    # path is more useful than the general one.
                    response["upgrade"] = upgrade_block(
                    spent.day_cap,
                    spent.month_cap,
                    signed_in=spent.signed_in,
                    signed_in_day_cap=spent.signed_in_day_cap,
                    signed_in_month_cap=spent.signed_in_month_cap,
                )
                    logger.info("%s", fair_use_log_line(spent, "warn"))

            # Recorded with backend_calls=1 so hotel searches actually consume
            # the budget they were just checked against. Without this they
            # would be free forever and the check above would never fire.
            _note_signed_in_usage(headers, 1)
            await telemetry.record(
                CallRecord(
                    timestamp=time.time(),
                    tool=tool,
                    tier=decision.tier,
                    client_name=_client_name(),
                    source_ip=source_ip,
                    widget_capable=decision.widget_capable,
                    requested_combinations=1,
                    backend_calls=1,
                    backend_failures=0,
                    results_returned=len(rows),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    truncated=False,
                    allowed=True,
                    decision_reason=decision.reason,
                    ad_eligible=decision.widget_capable,
                    fingerprint=fingerprint,
                    route=route,
                    fair_use_key=fu_key,
                    fair_use_kind=fu_kind,
                )
            )
            return response

        @mcp.tool(
            name="search_hotels",
            # Declared, not inferred: see src/output_schema.py.
            output_schema=HOTELS_OUTPUT_SCHEMA,
            title="FlightPowers free: search hotels",
            annotations=ToolAnnotations(
                title="FlightPowers free: search hotels",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
            description=described(
                    "FlightPowers hotel search: live Booking.com availability "
                    "and nightly prices for a "
                    "destination and date range. Input: a free-text destination the "
                    "way a person would say it (\"Rome\", \"Tokyo Shibuya\"), plus "
                    "check-in and check-out dates.\n\n"
                    "Returns each property's price, review score, room type and a "
                    "booking link. Rates go stale within minutes -- never reuse an "
                    "earlier result, search again.\n\n"
                    "FREE TIER: this is the ad-supported server. For rate-parity "
                    "pricing by country, the 24 Booking.com filters, no ads and no "
                    "shared daily budget, use the paid server. Sign in with "
                    f"Google at {SIGNIN_HOTELS_URL} and there is no key to "
                    "paste into your client, or bring your own RapidAPI key "
                    f"to {PAID_HOTELS_URL}"
            ),
            **tool_kwargs("search_hotels"),
        )
        @document_params
        async def search_hotels(
            destination: str,
            checkin_date: str,
            checkout_date: str,
            adults: int | None = None,
            children: int | None = None,
            currency: str | None = None,
            budget_per_night: int | None = None,
        ) -> dict[str, Any]:
            """
            Args:
                destination: Where to stay, in free text the way a person would
                    say it, e.g. "Rome" or "Tokyo Shibuya". A city, district,
                    landmark or region all work; no internal location ID is
                    needed.
                checkin_date: First night of the stay, "YYYY-MM-DD".
                checkout_date: Departure morning, "YYYY-MM-DD". Must be after
                    checkin_date.
                adults: Number of adult guests. Defaults to the upstream default
                    when omitted.
                children: Number of children sharing the room.
                currency: ISO currency code for the prices returned, e.g. "usd".
                budget_per_night: Only return properties at or below this
                    nightly price, in `currency`.
            """
            payload = build_hotels_search_payload(
                destination=destination,
                checkin_date=checkin_date,
                checkout_date=checkout_date,
                adults=adults,
                children=children,
                currency=currency,
                budget_per_night=budget_per_night,
            )
            return await _hotels_run(
                "search",
                payload,
                "search_hotels",
                # A hotel search has no origin, so `from` is empty and the
                # destination carries the whole route. One stay per call, so
                # combinations is always 1 -- unlike a flight fan-out.
                RouteRecord(
                    destination=destination,
                    dates=_dates_field(None, checkin_date, checkout_date),
                    nights=_nights_between(checkin_date, checkout_date),
                    currency=currency,
                    passengers=_pax_field([adults or 0, children or 0]),
                    combinations=1,
                ),
            )

        @mcp.tool(
            name="find_hotel_by_name",
            # Declared, not inferred: see src/output_schema.py.
            output_schema=HOTELS_OUTPUT_SCHEMA,
            title="FlightPowers free: find one hotel by name",
            annotations=ToolAnnotations(
                title="FlightPowers free: find one hotel by name",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
            description=described(
                    "FlightPowers single-property lookup: live Booking.com "
                    "availability and pricing for one named property. Input: "
                    "the hotel name a person would type (adding the city helps when "
                    "a chain has many properties) plus check-in and check-out "
                    "dates -- no internal property ID needed.\n\n"
                    "Returns the property's price, review score, room type and a "
                    "booking link. Rates go stale within minutes.\n\n"
                    "FREE TIER: ad-supported. The paid server adds per-country "
                    "pricing, filters, and no ads. Sign in with Google at "
                    f"{SIGNIN_HOTELS_URL} and there is no key to paste into "
                    f"your client, or bring your own RapidAPI key to "
                    f"{PAID_HOTELS_URL}"
            ),
            **tool_kwargs("find_hotel_by_name"),
        )
        @document_params
        async def find_hotel_by_name(
            hotel_name: str,
            checkin_date: str,
            checkout_date: str,
            adults: int | None = None,
            children: int | None = None,
            currency: str | None = None,
        ) -> dict[str, Any]:
            """
            Args:
                hotel_name: The property name a person would type, e.g. "Hotel
                    Artemide". Adding the city ("Hotel Artemide Rome")
                    disambiguates a chain with many properties. No internal
                    property ID is needed.
                checkin_date: First night of the stay, "YYYY-MM-DD".
                checkout_date: Departure morning, "YYYY-MM-DD". Must be after
                    checkin_date.
                adults: Number of adult guests.
                children: Number of children sharing the room.
                currency: ISO currency code for the prices returned, e.g. "usd".
            """
            payload = build_hotels_by_name_payload(
                hotel_name=hotel_name,
                checkin_date=checkin_date,
                checkout_date=checkout_date,
                adults=adults,
                children=children,
                currency=currency,
            )
            return await _hotels_run(
                "hotel_by_name",
                payload,
                "find_hotel_by_name",
                RouteRecord(
                    destination=hotel_name,
                    dates=_dates_field(None, checkin_date, checkout_date),
                    nights=_nights_between(checkin_date, checkout_date),
                    currency=currency,
                    passengers=_pax_field([adults or 0, children or 0]),
                    combinations=1,
                ),
            )

    # ── sign-in (optional, and off unless ops has provisioned it) ────────
    #
    # `MCP_PUBLIC_URL` is the exact URL clients connect to, including the
    # `/mcp` path, so the origin is everything before it -- Google compares
    # `redirect_uri` literally and the session cookie is per host, so this
    # has to be the host the user is actually on.
    #
    # None is the normal state until GOOGLE_OAUTH_CLIENT_ID,
    # GOOGLE_OAUTH_CLIENT_SECRET and DATABASE_URL are all set, and a None
    # here means the server behaves exactly as it did before this feature
    # existed: `/mcp` open, `/mcp/oauth` a 404, no anonymous cap gate.
    # urlsplit, NOT `public_url.split("/mcp")[0]`: that reads the `//` of the
    # scheme as the start of `/mcp` whenever the hostname itself begins with
    # "mcp", and hands back "https:" as the origin. Google compares
    # `redirect_uri` literally, so an origin that is wrong by one character
    # is a sign-in that can never complete.
    _public = urlsplit(settings.public_url)
    site_origin = f"{_public.scheme}://{_public.netloc}" if _public.netloc else ""
    oauth = build_free_oauth(site_origin) if site_origin else None
    if oauth is not None:
        register_oauth_routes(mcp, oauth, settings)

    # ── operational routes ───────────────────────────────────────────────

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Is this deployment actually able to sign anyone in?

        It used to answer `{"status": "ok", "signin_enabled": true}` by
        reading configuration, and on 2026-09-09 it said exactly that for 25
        minutes while every `/oauth/register` answered 503 -- the image had
        shipped without `asyncpg`, so both sign-in stores were unreachable
        and, since sign-in is now the only way into `/mcp`, no MCP client
        could use the server at all. A monitor watching this endpoint saw
        nothing. So health now PROBES rather than reports: one `SELECT 1`
        against each store, concurrently, under a short timeout.

        `signin_store` is the single word to alert on: `ok`, `unreachable`,
        or `not_configured` on a deployment that has no sign-in at all.
        `status` degrades with it, because a green status line next to a
        broken product is what made the last outage invisible.

        `?stores=0` skips the probes for a caller that wants the cheap
        liveness answer (a load balancer, a cold-start check) and does not
        want two database connections on every poll.
        """
        body: dict[str, Any] = {
            "status": "ok",
            "service": "flight-powers-free",
            # The facts a deploy has to be able to check without a browser:
            # is sign-in wired at all, which URL do we tell people to use,
            # and -- since it is an env var whose whole purpose is to be
            # flipped in an incident -- which anonymous mode is this RUNNING
            # deployment in. On Vercel a variable added in the dashboard
            # reaches only the NEXT deployment, so "I set FREE_ANON_MODE" and
            # "the server has FREE_ANON_MODE" are different claims, and this
            # is the one that settles which.
            "signin_enabled": oauth is not None,
            "signin_endpoint": (oauth.resource_url if oauth else None),
            "anon_mode": anon_mode(),
        }
        if oauth is None:
            body["signin_store"] = "not_configured"
            return JSONResponse(body)
        if request.query_params.get("stores", "1").lower() in ("0", "no", "false"):
            return JSONResponse(body)

        stores = await oauth.store_health()
        body["stores"] = stores
        states = set(stores.values())
        if "unreachable" in states:
            body["signin_store"] = "unreachable"
            body["status"] = "degraded"
        elif states == {"ok"}:
            body["signin_store"] = "ok"
        else:
            # not_configured / unknown: nothing is broken, but nothing was
            # proved either. Never report "ok" for a probe that did not run.
            body["signin_store"] = sorted(states)[0]
        return JSONResponse(body)

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(request: Request) -> JSONResponse:
        token = os.environ.get("METRICS_TOKEN", "")
        if token and request.headers.get("x-metrics-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        snapshot = await telemetry.snapshot()
        snapshot["config"] = {
            "enforcement_mode": settings.enforcement_mode,
            "blocked_tiers": sorted(settings.blocked_tiers),
            "max_backend_calls_per_tool_call": (
                settings.max_backend_calls_per_tool_call
            ),
            "ads_configured": bool(
                settings.lulu_publisher_id and settings.lulu_api_key
            ),
            "openai_ranges_loaded": classifier.openai_ranges_loaded,
            "openai_ranges_error": classifier.openai_ranges_error,
            "public_url": settings.public_url,
            # The running deployment's rollback switch, read from the
            # environment on this request. See /health for why it matters.
            "anon_mode": anon_mode(),
            "anon_day_cap": anon_caps(settings)[0],
        }
        return JSONResponse(snapshot)

    @mcp.custom_route("/metrics/calls", methods=["GET"])
    async def metrics_calls(request: Request) -> JSONResponse:
        """Call counts per UTC hour: GET /metrics/calls?hours=24"""
        token = os.environ.get("METRICS_TOKEN", "")
        if token and request.headers.get("x-metrics-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw = request.query_params.get("hours", "24")
        try:
            hours = int(raw)
        except ValueError:
            return JSONResponse(
                {"error": f"hours must be an integer, got {raw!r}"}, status_code=400
            )
        if hours < 1:
            return JSONResponse({"error": "hours must be >= 1"}, status_code=400)
        return JSONResponse(await telemetry.call_series(hours))

    mcp.fp_oauth = oauth  # type: ignore[attr-defined]
    mcp.classifier = classifier  # type: ignore[attr-defined]
    mcp.telemetry = telemetry  # type: ignore[attr-defined]
    mcp.settings_obj = settings  # type: ignore[attr-defined]
    return mcp
