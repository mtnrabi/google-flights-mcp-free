"""Declared MCP output schemas for this server's tools.

Why this file exists
--------------------
These tools have always answered with a JSON object and have always carried a
`search_status` field. What they never did was *declare* any of it. FastMCP
derives an output schema from the `-> dict[str, Any]` return annotation, and
for a bare dict that derivation is::

    {"type": "object", "additionalProperties": true}

which is a schema in name only: it tells a client that the result is an
object and nothing else. So `search_status` travelled as an undeclared
convention -- a key a caller could only learn about by reading our prose and
hoping we did not rename it.

The MCP specification already has the mechanism for this. `outputSchema` on
the tool plus `structuredContent` on the result were added in revision
2025-06-18 ("Add support for structured tool output") and are unchanged in
substance in the current revision, 2026-07-28, which loosened `outputSchema`
to any JSON Schema 2020-12 and `structuredContent` to any JSON value. The
spec's rule for us is a MUST:

    "Servers MUST provide structured results that conform to this schema.
     Clients SHOULD validate structured results against this schema."

That MUST is the whole reason for the shape below. Every schema here is
`additionalProperties: true` with `required` limited to the one key that
genuinely appears on every exit path. It is tempting to write a tight schema
listing exactly the keys of a successful search, and it would be wrong: these
tools have several legitimate non-search exits (`blocked`, a zero-result
answer, an upgrade note) and the ad middleware injects a key of its own after
we return. A schema that forbade any of those would make the server violate
the MUST on a path we ship deliberately.

`sponsored` is the sharpest case. `LuluAdsMiddleware` mutates
`structured_content` *after* the tool returns, adding a `sponsored` key and
rewriting the serialized text block to match. Nothing in this package puts it
there, so nothing in this package would have thought to declare it -- and a
strict schema would have turned every ad-carrying result into a validation
failure on exactly the clients that validate. It is declared explicitly, with
no type constraint, because it is the SDK's field and not ours to pin.

Backwards compatibility
-----------------------
Nothing here stops the serialized JSON text block being sent. The spec asks
for it --

    "For backwards compatibility, a tool that returns structured content
     SHOULD also return the serialized JSON in a TextContent block."

-- and we have callers on clients that predate structured content entirely.
FastMCP emits both from a single returned dict, and the one place we build a
result by hand (`ToolResult` for a degraded search) passes only
`structured_content`, which makes FastMCP derive the identical text block
through the identical serializer. Never hand-build the content list here: the
Lulu middleware only keeps `content[0]` in sync with the ad it injected when
that block is a single auto-generated `TextContent`.
"""

from typing import Any

# The four values `search_status` can take, mirroring the backend's own
# `X-Search-Status` vocabulary (backend/src/flight_search/search_outcome.py).
#
# `degraded` is in this list even though a degraded search is now returned
# with `isError: true`. Dropping it would have been the tidier-looking
# choice and a worse one -- a degraded result still carries its coverage and
# its explanation, and a client validating that payload against this schema
# must find the status value it actually contains. See the comment on
# `is_degraded` in server.py for why the error flag is the part that matters.
#
# `rate_limited` is the free server's fair-use refusal. It is a search that did
# not happen and never will on this server, which is why it is a status value
# and not an error: a model handed an exception retries it, and there is
# nothing here to retry. The result carries `retry: false` and an `upgrade`
# object saying how to get past it.
SEARCH_STATUS_VALUES = ("ok", "empty", "partial", "degraded", "rate_limited")

SEARCH_STATUS_DESCRIPTION = (
    "Whether the underlying search actually completed, read from the "
    "backend's X-Search-Status header. 'ok': every combination searched "
    "returned results. 'empty': the search completed and Google genuinely "
    "has no itineraries for it -- a real answer, not a failure. 'partial': "
    "some combinations returned results and some failed, so the list is "
    "incomplete. 'degraded': every combination failed, so the search did not "
    "happen and an empty list means nothing; this case is also flagged with "
    "isError: true and is safe to retry."
)

_RESULT_ROWS: dict[str, Any] = {
    "type": "array",
    "description": (
        "The itineraries or properties found, already sorted and deduplicated. "
        "An empty array is only meaningful when search_status is 'empty'. "
        "Rows are the backend's own objects, passed through unchanged, plus "
        "`book_label` -- a short constant added by this server on rows that "
        "carry a booking URL, so the result widget has something to put in "
        "its Book column. The URL itself is still `buy_link` (flights) or "
        "`link` (hotels), exactly as before.\n\n"
        "On hotel rows, when this deployment has affiliate attribution "
        "configured, `link` is a Stay22 redirect that forwards to the same "
        "property page; the un-wrapped Booking.com URL is then also present "
        "as `booking_url` and the row carries `link_note`. Give the user "
        "`link` -- that is the one to open."
    ),
    "items": {"type": "object", "additionalProperties": True},
}

_SPONSORED: dict[str, Any] = {
    "description": (
        "Injected by the Lulu ad SDK after the tool returns, on results that "
        "carry substantive data. Not produced by this server, and deliberately "
        "left untyped here: the shape belongs to the SDK."
    )
}

_FLIGHT_COVERAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "What was actually searched. Present whether or not the request was "
        "truncated, so a model can state honestly what its answer rests on."
    ),
    "properties": {
        "requested_combinations": {"type": "integer", "minimum": 0},
        "searched_combinations": {"type": "integer", "minimum": 0},
        "truncated": {
            "type": "boolean",
            "description": (
                "True when the request expanded past the free-tier cap and was "
                "sampled. A date absent from departure_dates_searched was never "
                "searched, which is not the same as having no flights."
            ),
        },
        "max_searches_per_request": {"type": "integer", "minimum": 1},
        "departure_dates_searched": {"type": "array", "items": {"type": "string"}},
        "destinations_searched": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "additionalProperties": True,
}

#: The `reason` vocabulary on a `by_destination` entry. `_by_destination` in
#: src/server.py is what produces the values; the comment above it says what
#: each one means.
DESTINATION_REASONS = (
    "ok",
    "no_flights",
    "search_failed",
    "not_in_limit",
    "not_searched",
)

_BY_DESTINATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One entry per destination the REQUEST asked for, in request order, "
        "present whether or not that destination has any flights in "
        "`results`. A destination with an empty `rows` array is a hole in the "
        "answer, and `reason` says which kind of hole: 'no_flights' (searched, "
        "answered, Google has nothing), 'search_failed' (searched and the "
        "search errored, so nothing is known), 'not_in_limit' (searched, found "
        "flights, none fitted in `limit`) or 'not_searched' (never searched -- "
        "the per-call fan-out cap sampled it away). 'ok' means it has rows.\n\n"
        "Read this rather than inferring coverage from `results`: a "
        "destination missing from `results` looks identical to one that has "
        "no flights, and they are not the same answer. `rows` are the same "
        "row objects that are in `results`, in the same order -- nothing here "
        "is data the answer does not already contain."
    ),
    "additionalProperties": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            # `anyOf` with one type per branch, not `"type": ["object",
            # "null"]`. Both are valid JSON Schema and mean the same thing,
            # but a type ARRAY is the form tools in the wild handle worst:
            # the MCP Inspector flags it, and several client-side validators
            # and code generators read only the first entry -- which would
            # make a legitimate `null` here look like a schema violation to
            # the caller. The wire format does not change at all.
            "cheapest": {
                "description": (
                    "The lowest-priced of this destination's rows in "
                    "`results`, or null when it has none."
                ),
                "anyOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ],
            },
            "searched": {
                "type": "boolean",
                "description": (
                    "Whether at least one search actually ran for this "
                    "destination. False means the fan-out cap dropped it."
                ),
            },
            "reason": {"type": "string", "enum": list(DESTINATION_REASONS)},
            "dates": {
                "type": "object",
                "description": (
                    "Present only on multi-date searches: one entry per "
                    "departure date requested for this destination, so a date "
                    "the fan-out cap sampled away is visible rather than "
                    "absent. `cheapest_price` is null when that date has no "
                    "row in `results`."
                ),
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "searched": {"type": "boolean"},
                        "reason": {
                            "type": "string",
                            "enum": list(DESTINATION_REASONS),
                        },
                        "row_count": {"type": "integer", "minimum": 0},
                        "cheapest_price": {
                            "anyOf": [{"type": "number"}, {"type": "null"}]
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
        "required": ["rows", "searched", "reason"],
        "additionalProperties": True,
    },
}


FLIGHTS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Flight search result",
    "description": (
        "A completed flight search. Read search_status before reading results: "
        "an empty array means 'no flights' only when search_status is 'empty'."
    ),
    "properties": {
        "results": _RESULT_ROWS,
        "result_count": {"type": "integer", "minimum": 0},
        "by_destination": _BY_DESTINATION,
        "search_status": {
            "type": "string",
            "enum": list(SEARCH_STATUS_VALUES),
            "description": SEARCH_STATUS_DESCRIPTION,
        },
        "search_coverage": _FLIGHT_COVERAGE,
        "fare_band": {
            "type": "string",
            "description": (
                "One line of Google's own price tracking for the first result: "
                "its low|typical|high verdict, the usual price range for the "
                "route (price_insights_low to price_insights_high) and the "
                "fare itself. Absent when the backend returned no insights -- "
                "there is no default and nothing is inferred from the results."
            ),
        },
        "widget_eyebrow": {
            "type": "string",
            "description": (
                "Display line for the result widget: what was searched, then "
                "fare_band. Presentation only -- every value in it is already "
                "in search_coverage and fare_band."
            ),
        },
        "partial": {
            "type": "string",
            "description": (
                "Present when some searches failed but others succeeded. Plain "
                "text saying how much of the request the results cover."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "Present when there is something the model must relay to the "
                "user rather than silently absorb -- no results, a degraded "
                "search, or a refusal."
            ),
        },
        "blocked": {
            "type": "boolean",
            "description": (
                "True when this host cannot display the sponsored card that "
                "funds the free tier, so no search was run."
            ),
        },
        "retry": {
            "type": "boolean",
            "description": (
                "Whether repeating this exact call could succeed. Present and "
                "false on a rate_limited result, where the allowance, not the "
                "request, is what failed."
            ),
        },
        "fair_use": {
            "type": "object",
            "description": (
                "This client's standing against the free server's per-client "
                "caps. Present from 80% of either cap onwards, and on every "
                "rate_limited result. Counts BACKEND searches, so one call "
                "over a date range can move it by more than one."
            ),
            "properties": {
                "used_today": {"type": "integer", "minimum": 0},
                "day_cap": {"type": "integer", "minimum": 0},
                "used_month": {"type": "integer", "minimum": 0},
                "month_cap": {"type": "integer", "minimum": 0},
                "human": {
                    "type": "string",
                    "description": (
                        "One-line, chat-host-renderable summary of the caps "
                        "and today's usage, e.g. \"Free tier: 150 searches a "
                        "day, 2,000 a month; 120 used today.\""
                    ),
                },
                "note": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "upgrade": {
            "type": "object",
            "description": (
                "How to move to the paid, ad-free server: why (the cap that "
                "fired or is close to it), paid_server, hotels_server, the "
                "ordered `how` steps to subscribe on RapidAPI and connect "
                "with your own key, and docs. Present on a rate_limited "
                "result and, from 80% of either cap onwards, alongside "
                "`fair_use` on a normal result too."
            ),
            "additionalProperties": True,
        },
        "sponsored": _SPONSORED,
    },
    # `results` is the only key on every exit path -- the search result, the
    # zero-result answer and the `blocked` refusal all carry it. Requiring
    # anything else here would make one of those paths violate the spec's
    # "servers MUST provide structured results that conform".
    "required": ["results"],
    "additionalProperties": True,
}

HOTELS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Hotel search result",
    "description": (
        "A completed hotel search. The hotels backend reports no search-status "
        "header, so these results carry no search_status field."
    ),
    "properties": {
        "results": _RESULT_ROWS,
        "result_count": {"type": "integer", "minimum": 0},
        "search_coverage": {
            "type": "object",
            "description": "The destination and dates this search actually used.",
            "properties": {
                "destination": {"type": "string"},
                "checkin_date": {
                    "anyOf": [{"type": "string"}, {"type": "null"}]
                },
                "checkout_date": {
                    "anyOf": [{"type": "string"}, {"type": "null"}]
                },
            },
            "additionalProperties": True,
        },
        "message": {"type": "string"},
        # The hotels backend reports no search-status header, so a hotel
        # result carries this key on exactly one path: the fair-use refusal,
        # which is this server's own answer and not the backend's.
        "search_status": {
            "type": "string",
            "enum": ["rate_limited"],
            "description": (
                "Present only when the free server's per-client fair-use "
                "allowance is spent and no search was run. See `upgrade`."
            ),
        },
        "retry": {
            "type": "boolean",
            "description": (
                "Whether repeating this exact call could succeed. Present and "
                "false on a rate_limited result, where the allowance, not the "
                "request, is what failed."
            ),
        },
        "fair_use": {
            "type": "object",
            "description": (
                "This client's standing against the free server's per-client "
                "caps. Present from 80% of either cap onwards, and on every "
                "rate_limited result. Counts BACKEND searches, so one call "
                "over a date range can move it by more than one."
            ),
            "properties": {
                "used_today": {"type": "integer", "minimum": 0},
                "day_cap": {"type": "integer", "minimum": 0},
                "used_month": {"type": "integer", "minimum": 0},
                "month_cap": {"type": "integer", "minimum": 0},
                "human": {
                    "type": "string",
                    "description": (
                        "One-line, chat-host-renderable summary of the caps "
                        "and today's usage, e.g. \"Free tier: 150 searches a "
                        "day, 2,000 a month; 120 used today.\""
                    ),
                },
                "note": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "upgrade": {
            "type": "object",
            "description": (
                "How to move to the paid, ad-free server: why (the cap that "
                "fired or is close to it), paid_server, hotels_server, the "
                "ordered `how` steps to subscribe on RapidAPI and connect "
                "with your own key, and docs. Present on a rate_limited "
                "result and, from 80% of either cap onwards, alongside "
                "`fair_use` on a normal result too."
            ),
            "additionalProperties": True,
        },
        "sponsored": _SPONSORED,
    },
    "required": ["results"],
    "additionalProperties": True,
}
