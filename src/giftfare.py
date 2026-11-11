"""The one live number in the cap note, fetched at send time or dropped.

The cap note's third paragraph ends on a real round trip -- "a round trip
counts as one request there (164$ TLV to Athens nonstop both ways, one
call)" -- because the sentence it sits in is a claim about our pricing and
the bracket is the proof. `state/gtm/email/FREE-SIGNIN-UPSELL-2026-09-17.md`
§2(a) is where the copy and that number came from.

**A fare goes stale in minutes.** So the bracket is never a constant: it is
re-fetched by the same call the copy was written from, at the moment the
note is rendered, and when that call does not come back with a real answer
the whole bracket is DROPPED. Never a stale number, never a rounded guess,
never yesterday's fare with today's date -- CLAUDE.md rule 1.

Where the call goes, and why not the free server
------------------------------------------------
`POST https://api.flightpowers.com/v1/flights/roundtrip` with our own
`RAPIDAPI_KEY` in `x-api-key`, exactly as `state/gtm/scripts/route-gift.mjs`
does it. CLAUDE.md "Gift calls use Matan's key, not the free server": our
own outreach is not allowed to spend a free-tier allowance, and since
2026-09-15 that allowance is 50 a day per user. `X-FP-Client` carries the
attribution the api front turns into the `[source]` line (rule 11).

Everything here is best effort and nothing raises. A note that ships
without the bracket is a complete note; a note that does not ship because
a fare lookup timed out is a bug.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

#: Our own RapidAPI key, for OUR calls. Never a user's key, and never read
#: from a request: this is the account's own plan being billed for one
#: search so that an email can carry a number somebody can check.
API_KEY_ENV = "RAPIDAPI_KEY"
#: The keyed front. Overridable only so a test can point it at a mock.
FRONT_ENV = "GIFT_FARE_API_BASE"
DEFAULT_FRONT = "https://api.flightpowers.com"

#: Attribution, CLAUDE.md rule 11. The api front turns `X-FP-Client` into
#: `source=api-front tool=<route>` plus this string, so a fare call made by
#: an email is distinguishable from a customer's traffic in CloudWatch.
FP_CLIENT = "free-mcp-capnote"

#: The route the reviewed copy used. TLV to Athens, nonstop both ways, one
#: request -- which is the point the sentence is making, so the route is
#: not a variable and the phrasing cannot drift away from what was checked.
ROUTE_FROM = "TLV"
ROUTE_TO = "ATH"
ROUTE_TO_NAME = "Athens"
#: Far enough out that a nonstop pair is normally on sale, close enough
#: that the fare is a real one somebody could book. The dates themselves
#: never appear in the copy.
DEPART_DAYS_AHEAD = 21
NIGHTS = 4
CURRENCY = "USD"

#: A cap note must not wait on a fare. Eight seconds is well inside the
#: send path's own budget, and a slower answer is treated as no answer.
TIMEOUT = 8.0


@dataclass(frozen=True)
class GiftFare:
    """One priced round trip, and the bracket it renders to."""

    price: int
    origin: str = ROUTE_FROM
    destination: str = ROUTE_TO_NAME

    @property
    def bracket(self) -> str:
        """` (164$ TLV to Athens nonstop both ways, one call)`.

        The leading space is part of the value: the placeholder sits at the
        end of a sentence as `...one request there{{gift_fare}}.`, so an
        empty value leaves the sentence intact rather than leaving a gap.
        """
        return (
            f" ({self.price}$ {self.origin} to {self.destination} "
            "nonstop both ways, one call)"
        )


def _dates(now: float | None = None) -> tuple[str, str]:
    start = datetime.fromtimestamp(
        now if now is not None else time.time(), tz=timezone.utc
    ) + timedelta(days=DEPART_DAYS_AHEAD)
    end = start + timedelta(days=NIGHTS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _nonstop_both_ways(flights: list[dict]) -> list[dict]:
    """Only the pairs the copy actually describes.

    The sentence says "nonstop both ways". A cheaper one-stop pair is a
    different claim, so it is not used -- the bracket is dropped instead.
    """
    out = []
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        out_stops = flight.get("departure_flight_stops")
        back_stops = flight.get("return_flight_stops")
        if out_stops == 0 and back_stops == 0:
            out.append(flight)
    return out


def _cheapest(flights: list[dict]) -> int | None:
    best: float | None = None
    for flight in flights:
        raw = flight.get("total_price_as_number")
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if best is None or price < best:
            best = price
    if best is None:
        return None
    return int(round(best))


async def fetch_gift_fare(
    *,
    client: httpx.AsyncClient | None = None,
    now: float | None = None,
    timeout: float = TIMEOUT,
) -> GiftFare | None:
    """One live round trip, or None. Never raises.

    None means "print no bracket". Every path that cannot produce a fare we
    just priced -- no key configured, a non-2xx, an unparseable body, no
    nonstop pair in the results, a timeout -- returns None, and the caller
    drops the sentence's bracket rather than printing a number from memory.
    """
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        logger.info("no %s configured; the cap note ships without a fare", API_KEY_ENV)
        return None
    base = (os.environ.get(FRONT_ENV) or DEFAULT_FRONT).rstrip("/")
    depart, back = _dates(now)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            f"{base}/v1/flights/roundtrip",
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "X-FP-Client": FP_CLIENT,
            },
            json={
                "from_airport": ROUTE_FROM,
                "to_airport": ROUTE_TO,
                "departure_date": depart,
                "return_date": back,
                "currency": CURRENCY,
                "limit": 5,
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - an email may never raise here
        logger.warning("gift fare lookup did not complete: %s", type(exc).__name__)
        return None
    finally:
        if own_client:
            await client.aclose()

    if response.status_code < 200 or response.status_code >= 300:
        logger.warning("gift fare lookup returned %d", response.status_code)
        return None
    try:
        body = response.json()
    except ValueError:
        logger.warning("gift fare lookup returned an unparseable body")
        return None
    if isinstance(body, dict):
        flights = body.get("results")
    else:
        flights = body
    if not isinstance(flights, list) or not flights:
        logger.info("gift fare lookup found no flights; dropping the bracket")
        return None
    price = _cheapest(_nonstop_both_ways(flights))
    if price is None:
        logger.info("no nonstop pair in the gift fare results; dropping the bracket")
        return None
    return GiftFare(price=price)


async def gift_fare_bracket(
    *,
    client: httpx.AsyncClient | None = None,
    now: float | None = None,
    timeout: float = TIMEOUT,
) -> str:
    """The rendered bracket, or `""`. The only thing a caller needs."""
    fare = await fetch_gift_fare(client=client, now=now, timeout=timeout)
    return fare.bracket if fare is not None else ""
