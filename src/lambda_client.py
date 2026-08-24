"""
Async client for the Flight Rabbi API Lambda.

This mirrors the contract in backend/src/routers/google_flights_router.py
(OneWayAPI / RoundtripAPI) and authenticates exactly like the Apify actor
does: a single `X-RapidAPI-Proxy-Secret` header, checked at
backend/src/api_lambda.py:15. A wrong or missing secret is a 403, not a 401.

Two backend behaviours this module deliberately works around:

1. `sort_type` is a strict enum. `RoundtripAPI.sort_type` is a bare
   `SortType`, so sending an explicit `null` is a 422. We omit every None
   value from the payload entirely rather than sending nulls, which is safe
   for both endpoints and matches what apify_actor/src/main.js:35 does.

2. An empty result is `[]` with HTTP 200, not a 404. Callers must treat an
   empty list as "no flights found", never as an error.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

import httpx

ENDPOINT_MAP = {
    "oneway": "/api/google_flights/oneway/v1",
    "roundtrip": "/api/google_flights/roundtrip/v1",
}

SORT_TYPES = ("Overall", "Price", "Duration")

# Transient failures worth one more attempt. 403 (bad secret) and 422
# (bad payload) are deterministic -- retrying those just burns latency.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class LambdaError(RuntimeError):
    """The backend could not answer this search."""


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so we never send an explicit null.

    See the module docstring: `null` on roundtrip's `sort_type` is a 422.
    Omitting the key lets the backend's own pydantic default apply.
    """
    return {k: v for k, v in payload.items() if v is not None}


def build_oneway_payload(
    *,
    departure_date: str,
    from_airport: str,
    to_airport: str,
    max_stops: int | None = None,
    sort_type: str | None = None,
    airline_codes: list[str] | None = None,
    exclude_airline_codes: list[str] | None = None,
    departure_time_min: int | None = None,
    departure_time_max: int | None = None,
    arrival_time_min: int | None = None,
    arrival_time_max: int | None = None,
    currency: str | None = None,
    max_price: int | None = None,
    seat_type: int | None = None,
    passengers: list[int] | None = None,
    limit: int | None = None,
    use_fallback: bool | None = None,
    use_ext_proxy: bool | None = None,
) -> dict[str, Any]:
    """Mirrors OneWayAPI in backend/src/routers/google_flights_router.py:15."""
    return _compact(
        {
            "departure_date": departure_date,
            "from_airport": from_airport,
            "to_airport": to_airport,
            "max_stops": max_stops,
            "sort_type": sort_type,
            "airline_codes": airline_codes,
            "exclude_airline_codes": exclude_airline_codes,
            "departure_time_min": departure_time_min,
            "departure_time_max": departure_time_max,
            "arrival_time_min": arrival_time_min,
            "arrival_time_max": arrival_time_max,
            "currency": currency,
            "max_price": max_price,
            "seat_type": seat_type,
            "passengers": passengers,
            "limit": limit,
            "use_fallback": use_fallback,
            "use_ext_proxy": use_ext_proxy,
        }
    )


def build_roundtrip_payload(
    *,
    departure_date: str,
    return_date: str,
    from_airport: str,
    to_airport: str,
    max_departure_stops: int | None = None,
    max_return_stops: int | None = None,
    sort_type: str | None = None,
    departure_airline_codes: list[str] | None = None,
    return_airline_codes: list[str] | None = None,
    departure_exclude_airline_codes: list[str] | None = None,
    return_exclude_airline_codes: list[str] | None = None,
    departure_departure_time_min: int | None = None,
    departure_departure_time_max: int | None = None,
    departure_arrival_time_min: int | None = None,
    departure_arrival_time_max: int | None = None,
    return_departure_time_min: int | None = None,
    return_departure_time_max: int | None = None,
    return_arrival_time_min: int | None = None,
    return_arrival_time_max: int | None = None,
    currency: str | None = None,
    max_price: int | None = None,
    seat_type: int | None = None,
    passengers: list[int] | None = None,
    limit: int | None = None,
    use_fallback: bool | None = None,
    use_ext_proxy: bool | None = None,
) -> dict[str, Any]:
    """Mirrors RoundtripAPI in backend/src/routers/google_flights_router.py:40."""
    return _compact(
        {
            "departure_date": departure_date,
            "return_date": return_date,
            "from_airport": from_airport,
            "to_airport": to_airport,
            "max_departure_stops": max_departure_stops,
            "max_return_stops": max_return_stops,
            "sort_type": sort_type,
            "departure_airline_codes": departure_airline_codes,
            "return_airline_codes": return_airline_codes,
            "departure_exclude_airline_codes": departure_exclude_airline_codes,
            "return_exclude_airline_codes": return_exclude_airline_codes,
            "departure_departure_time_min": departure_departure_time_min,
            "departure_departure_time_max": departure_departure_time_max,
            "departure_arrival_time_min": departure_arrival_time_min,
            "departure_arrival_time_max": departure_arrival_time_max,
            "return_departure_time_min": return_departure_time_min,
            "return_departure_time_max": return_departure_time_max,
            "return_arrival_time_min": return_arrival_time_min,
            "return_arrival_time_max": return_arrival_time_max,
            "currency": currency,
            "max_price": max_price,
            "seat_type": seat_type,
            "passengers": passengers,
            "limit": limit,
            "use_fallback": use_fallback,
            "use_ext_proxy": use_ext_proxy,
        }
    )


class LambdaClient:
    """Thin async HTTP client over the two /v1 flight endpoints."""

    def __init__(
        self,
        base_url: str,
        auth_secret: str,
        timeout_seconds: float = 105.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_secret = auth_secret
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "LambdaClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        endpoint: Literal["oneway", "roundtrip"],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """POST one search. Returns the (possibly empty) result list.

        Raises LambdaError on a non-retryable failure or after exhausting
        retries. Never returns None -- an empty search is `[]`.
        """
        if self._client is None:
            raise LambdaError("LambdaClient used outside its async context")

        url = f"{self._base_url}{ENDPOINT_MAP[endpoint]}"
        headers = {
            "Content-Type": "application/json",
            "X-RapidAPI-Proxy-Secret": self._auth_secret,
        }

        last_error: str = "unknown"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == _MAX_ATTEMPTS:
                    break
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return self._parse(response)

            body = response.text[:300]
            last_error = f"HTTP {response.status_code}: {body}"
            if response.status_code not in _RETRYABLE_STATUS:
                raise LambdaError(f"{endpoint} search failed -- {last_error}")
            if attempt == _MAX_ATTEMPTS:
                break
            await self._backoff(attempt)

        raise LambdaError(
            f"{endpoint} search failed after {_MAX_ATTEMPTS} attempts -- {last_error}"
        )

    @staticmethod
    def _parse(response: httpx.Response) -> list[dict[str, Any]]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LambdaError(
                f"backend returned non-JSON: {response.text[:200]}"
            ) from exc

        # The API Lambda returns a bare array (backend/src/api_lambda.py:92).
        # Be tolerant of an enveloped shape in case that ever changes.
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "items", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
            return []
        return []

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Full jitter. The backend fans out to Google behind a shared proxy
        # pool, so synchronised retries are the last thing it needs.
        delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
        await asyncio.sleep(random.uniform(0, delay))
