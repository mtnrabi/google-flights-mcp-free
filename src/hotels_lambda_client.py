"""
Async client for the hotels backend Lambda.

Separate from `lambda_client.py` because it is a different deployment with a
different response shape, but deliberately the same auth header and the same
error type: both are mrabi's own backends behind
`X-RapidAPI-Proxy-Secret`, and a caller debugging one should not have to learn
a second vocabulary for the other.

The shape difference that matters: the flights Lambda answers with a bare
list, the hotels one answers with an object carrying `properties`. Callers get
a list either way -- normalising here rather than in the tool keeps the
branching in one place.

Verified against the live Lambda 2026-08-18:
    GET  /isalive             -> 200 "true"
    POST /search (no secret)  -> 403 "invalid or missing X-RapidAPI-Proxy-Secret"
    POST /search (+ secret)   -> 200, 26 properties
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

import httpx

from .lambda_client import LambdaError

ENDPOINT_MAP = {
    "search": "/search",
    "hotel_by_name": "/hotel_by_name",
}

# Server faults only. A 403 here is a wrong proxy secret -- our configuration
# problem, not a transient one, and retrying just delays a clear error.
_RETRYABLE_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 2


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so the backend never receives an explicit null."""
    return {k: v for k, v in payload.items() if v is not None}


def build_search_payload(
    *,
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    currency: str | None = None,
    budget_per_night: int | None = None,
) -> dict[str, Any]:
    return _compact(
        {
            "destination": destination,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": currency,
            "budget_per_night": budget_per_night,
        }
    )


def build_hotel_by_name_payload(
    *,
    hotel_name: str,
    checkin_date: str,
    checkout_date: str,
    adults: int | None = None,
    children: int | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    return _compact(
        {
            "hotel_name": hotel_name,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": currency,
        }
    )


class HotelsLambdaClient:
    """Thin async HTTP client over the hotel endpoints."""

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

    async def __aenter__(self) -> "HotelsLambdaClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        endpoint: Literal["search", "hotel_by_name"],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """POST one search. Always returns a list; an empty result is `[]`."""
        if self._client is None:
            raise LambdaError("HotelsLambdaClient used outside its async context")

        url = f"{self._base_url}{ENDPOINT_MAP[endpoint]}"
        headers = {
            "Content-Type": "application/json",
            "X-RapidAPI-Proxy-Secret": self._auth_secret,
        }

        last_error = "unknown"
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

            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in _RETRYABLE_STATUS:
                raise LambdaError(f"{endpoint} failed -- {last_error}")
            if attempt == _MAX_ATTEMPTS:
                break
            await self._backoff(attempt)

        raise LambdaError(
            f"{endpoint} failed after {_MAX_ATTEMPTS} attempts -- {last_error}"
        )

    @staticmethod
    def _parse(response: httpx.Response) -> list[dict[str, Any]]:
        """Normalise both answer shapes to a list.

        `/search` returns `{"properties": [...]}`; `/hotel_by_name` returns a
        single property object. A model should not have to branch on that.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise LambdaError(f"backend returned non-JSON: {exc}") from exc

        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if isinstance(body, dict):
            rows = body.get("properties")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            return [body]
        return []

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Jittered, so two callers retrying after the same blip do not march
        # back in lockstep.
        await asyncio.sleep(0.25 * attempt + random.uniform(0, 0.25))
