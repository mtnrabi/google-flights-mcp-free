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
  search runs rather than post-sorting, it is silently dropped for oneway by
  api_lambda.py, and `max_price` overrides it (app.py:255). Since results
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

import logging
import os
import time
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context, get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

from .fanout import (
    FanoutResult,
    PlanError,
    execute_plan,
    plan_oneway,
    plan_roundtrip,
)
from .hotels_lambda_client import (
    HotelsLambdaClient,
    build_hotel_by_name_payload as build_hotels_by_name_payload,
    build_search_payload as build_hotels_search_payload,
)
from .lambda_client import (
    LambdaClient,
    build_oneway_payload,
    build_roundtrip_payload,
)
from .policy import ClientClassifier, decide, extract_source_ip
from .settings import Settings, load_settings
from .stores import build_counter_store
from .telemetry import CallRecord, Telemetry

logger = logging.getLogger(__name__)

# Shown on every free-tier result. mrabi asked that the freemium surfaces sell
# the paid ones explicitly rather than just existing next to them.
UPGRADE_NOTE = {
    "tier": "free",
    "limits": "ad-supported, 15 searches per call, shared daily budget",
    "paid_flights": "https://flights.flightpowers.com/mcp",
    "paid_hotels": "https://hotels.flightpowers.com/mcp",
    "what_you_get": (
        "No ads, no per-call search cap, no shared daily budget, and it works "
        "from any MCP client rather than only where a widget renders. Billed "
        "to your own RapidAPI key, with remaining quota reported on every call."
    ),
}

SORT_CHOICES = ("best", "price", "duration")

# The beacon is a 1x1 <img> to this origin. MCP Apps hosts apply a default
# CSP of `img-src 'self' data:`, so it is blocked unless the widget resource
# declares the domain -- which looks identical to a working integration from
# the outside: card renders, strip shows, no impression, no error.
ADS_ORIGIN = "https://ads.getlulu.dev"

# Widget column mappings. Paths are resolved against the tool's
# structuredContent (`rows`, `eyebrow`) and against each row (`columns`),
# so a renamed backend field silently empties a column rather than failing --
# test_ads.py resolves every path below against a live-shaped response.
ONEWAY_WIDGET_MAPPING: dict[str, Any] = {
    "eyebrow": {
        "path": "search_coverage.destinations_searched",
        "prefix": "flights to ",
    },
    "rows": "results",
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
    ],
}

ROUNDTRIP_WIDGET_MAPPING: dict[str, Any] = {
    "eyebrow": {
        "path": "search_coverage.destinations_searched",
        "prefix": "round trip to ",
    },
    "rows": "results",
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
    ],
}


# Fields verified against a live hotels-Lambda response 2026-08-18:
# name / price_string / price / review_score / review_count / room_type /
# location / link. Kept to four columns so the card stays readable in a chat
# pane -- the full object is still in the tool result.
HOTELS_WIDGET_MAPPING: dict[str, Any] = {
    "eyebrow": {"path": "search_coverage.destination", "prefix": "hotels in "},
    "rows": "results",
    "columns": [
        {"header": "Hotel", "path": "name"},
        {"header": "Price", "path": "price_string", "mono": True},
        {"header": "Score", "path": "review_score", "mono": True},
        {"header": "Room", "path": "room_type"},
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


def _request_context() -> tuple[dict[str, str], str | None]:
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - not running over HTTP
        return {}, None
    headers = {k.lower(): v for k, v in request.headers.items()}
    peer = request.client.host if request.client else None
    return headers, peer


def _sort_results(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    """Sort the merged result set.

    Oneway and roundtrip use different price/duration keys, so fall back
    across both. Missing values sort last rather than crashing -- the fli
    fallback path legitimately returns nulls (fli_fallback.py:231).
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

    if sort_by == "price":
        return sorted(rows, key=price_key)
    if sort_by == "duration":
        return sorted(rows, key=duration_key)
    return sorted(rows, key=price_key)


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats across combos, keyed on buy_link.

    buy_link is already the de-dup key used elsewhere in this codebase
    (backend/src/app.py:533).
    """
    seen: set[str] = set()
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


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or load_settings()

    mcp = FastMCP(
        name="flight-powers-free",
        version="0.1.0",
        instructions=(
            "Free real-time Google Flights search. Both tools accept a date "
            "RANGE and a LIST of destinations and expand them internally -- "
            "always express a flexible search as ONE call with a range, never "
            "as many single-date calls."
        ),
    )

    http_client = get_shared_client(settings)
    telemetry = Telemetry(
        store=build_counter_store(client=http_client),
        daily_budget=settings.daily_backend_call_budget,
        degrade_at=settings.budget_degrade_at,
        log_path=settings.log_path or None,
    )
    if settings.daily_backend_call_budget > 0 and not telemetry.durable_counters:
        logger.warning(
            "DAILY_BACKEND_CALL_BUDGET is set but no shared counter store is "
            "configured. On a single always-on process this is fine. On "
            "serverless it is NOT enforceable -- counters are per-instance, so "
            "real spend will exceed the budget silently. Set "
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN."
        )
    classifier = ClientClassifier()

    # ── Lulu wiring ──────────────────────────────────────────────────────
    # Two-step (widget + middleware) rather than the one-line
    # enable_lulu_ads, because only the middleware path accepts
    # is_error_result, which is what keeps ads off empty results. With two
    # tools, the "easy to forget the _meta on a new tool" problem that
    # enable_lulu_ads exists to solve does not apply.
    app_config: Any = None
    # Empty url or secret means no hotel tools at all. Registering tools that
    # can only 500 is worse than not offering them.
    hotels_enabled = bool(settings.hotels_lambda_url and settings.hotels_auth)

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
    ) -> dict[str, Any]:
        started = time.perf_counter()
        headers, peer = _request_context()
        source_ip = extract_source_ip(headers, peer)
        client_name = _client_name()
        # No-op after the first successful load. Serverless has no awaitable
        # startup hook, so the feed is fetched here instead.
        await classifier.ensure_openai_ranges()
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
        ) -> None:
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

        cap, budget_note = await telemetry.cap_for_budget(decision.cap)

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

        if budget_note:
            plan.degraded_reason = budget_note

        # Reuses the process-wide connection pool; LambdaClient does not
        # close a client it was handed.
        async with LambdaClient(
            settings.base_lambda_url,
            settings.rapid_auth,
            settings.lambda_timeout_seconds,
            client=http_client,
        ) as client:
            outcome: FanoutResult = await execute_plan(
                plan,
                build_payload=payload_builder,
                run_search=client.search,
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

        rows = _sort_results(_dedupe(outcome.results), sort_by)[:limit]
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

        response: dict[str, Any] = {
            "results": rows,
            "result_count": len(rows),
            "search_coverage": plan.coverage(),
        }
        if outcome.backend_failures:
            response["partial"] = (
                f"{outcome.backend_failures} of {plan.executed_combinations} "
                "searches failed; results cover the rest."
            )
        if not rows:
            response["message"] = (
                "No flights were found for this search. Google Flights returns "
                "nothing for some route and date combinations; try a different "
                "date, a nearby airport, or set use_fallback to true."
            )
        return response

    # ── tools ────────────────────────────────────────────────────────────

    @mcp.tool(
        name="search_oneway_flights",
        description=(
            "Search real-time one-way flights from Google Flights.\n\n"
            "IMPORTANT: for any flexible search, make ONE call with a date "
            "range and/or several destinations. Do NOT call this repeatedly, "
            "once per date -- pass departure_date_from and departure_date_to "
            "and the server searches the range for you. 'Cheapest flight to "
            "Sri Lanka anywhere in October' is one call, not thirty.\n\n"
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
            "whether a fare is a good deal."
        ),
        **tool_kwargs("search_oneway_flights"),
    )
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
        use_fallback: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, or a list of them to compare.
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
            use_fallback: Wait longer on hard routes. Slower, fewer empty results.
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
                from_airport=from_airport.strip().upper(),
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
            "search_oneway_flights", plan_builder, payload_builder, sort_by, limit
        )

    @mcp.tool(
        name="search_roundtrip_flights",
        description=(
            "Search real-time round-trip flights from Google Flights, priced "
            "as paired legs rather than two separate one-ways.\n\n"
            "IMPORTANT: for any flexible search, make ONE call. Pass "
            "departure_date_from / departure_date_to for a departure range, "
            "and `nights` instead of return_date to search trip lengths -- "
            "'5 to 7 nights in Rome sometime in May' is one call.\n\n"
            "FREE TIER LIMIT: one call searches at most 15 departure-date x "
            "nights combinations. A wider request is sampled evenly rather "
            "than rejected, and returns truncated: true plus the exact dates "
            "searched in search_coverage.departure_dates_searched. A date "
            "absent from that list was never searched -- which is not the "
            "same as having no flights.\n\n"
            "Returns total price for both legs, per-leg airline, stops and "
            "duration, and a single bookable buy_link covering the trip."
        ),
        **tool_kwargs("search_roundtrip_flights"),
    )
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
        use_fallback: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, or a list of them to compare.
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
            use_fallback: Wait longer on hard routes. Slower, fewer empty results.
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
                from_airport=from_airport.strip().upper(),
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
            "search_roundtrip_flights", plan_builder, payload_builder, sort_by, limit
        )

    # ── hotels ───────────────────────────────────────────────────────────
    # Free tier, so these run against mrabi's own Lambda and every call is his
    # cost -- unlike the paid servers, where the caller's RapidAPI key pays.
    # Registered only when the backend is actually configured.

    if hotels_enabled:

        async def _hotels_run(
            endpoint: str, payload: dict[str, Any], tool: str
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
                    "cap: https://hotels.flightpowers.com/mcp"
                )

            # Same classification path the flight tools use, so hotel calls
            # land in the same by_tier counters rather than a parallel set.
            headers, peer = _request_context()
            source_ip = extract_source_ip(headers, peer)
            await classifier.ensure_openai_ranges()
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
                    "from anywhere: https://hotels.flightpowers.com/mcp"
                )

            async with HotelsLambdaClient(
                settings.hotels_lambda_url,
                settings.hotels_auth,
                settings.lambda_timeout_seconds,
                client=http_client,
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
            response["upgrade"] = UPGRADE_NOTE

            # Recorded with backend_calls=1 so hotel searches actually consume
            # the budget they were just checked against. Without this they
            # would be free forever and the check above would never fire.
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
                )
            )
            return response

        @mcp.tool(
            name="search_hotels",
            description=(
                "Search live hotel availability and nightly prices for a "
                "destination and date range. Input: a free-text destination the "
                "way a person would say it (\"Rome\", \"Tokyo Shibuya\"), plus "
                "check-in and check-out dates.\n\n"
                "Returns each property's price, review score, room type and a "
                "booking link. Rates go stale within minutes -- never reuse an "
                "earlier result, search again.\n\n"
                "FREE TIER: this is the ad-supported server. For rate-parity "
                "pricing by country, the 24 Booking.com filters, no ads and no "
                "caps, use the paid server at "
                "https://hotels.flightpowers.com/mcp"
            ),
            **tool_kwargs("search_hotels"),
        )
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
                destination: Free text, e.g. "Rome" or "Tokyo Shibuya".
                checkin_date: "YYYY-MM-DD".
                checkout_date: "YYYY-MM-DD", after check-in.
                adults: Number of adults. Defaults to the backend's own default.
                children: Number of children.
                currency: ISO currency code, e.g. "USD".
                budget_per_night: Maximum price per night in that currency.
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
            return await _hotels_run("search", payload, "search_hotels")

        @mcp.tool(
            name="find_hotel_by_name",
            description=(
                "Get availability and pricing for one named property. Input: "
                "the hotel name a person would type (adding the city helps when "
                "a chain has many properties) plus check-in and check-out "
                "dates -- no internal property ID needed.\n\n"
                "Returns the property's price, review score, room type and a "
                "booking link. Rates go stale within minutes.\n\n"
                "FREE TIER: ad-supported. The paid server at "
                "https://hotels.flightpowers.com/mcp adds per-country pricing, "
                "filters, and no ads."
            ),
            **tool_kwargs("find_hotel_by_name"),
        )
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
                hotel_name: The name as a person would write it.
                checkin_date: "YYYY-MM-DD".
                checkout_date: "YYYY-MM-DD", after check-in.
                adults: Number of adults.
                children: Number of children.
                currency: ISO currency code.
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
                "hotel_by_name", payload, "find_hotel_by_name"
            )

    # ── operational routes ───────────────────────────────────────────────

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "flight-powers-free"})

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

    mcp.classifier = classifier  # type: ignore[attr-defined]
    mcp.telemetry = telemetry  # type: ignore[attr-defined]
    mcp.settings_obj = settings  # type: ignore[attr-defined]
    return mcp
