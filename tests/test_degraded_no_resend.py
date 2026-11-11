"""A degraded 5xx is answered once, with the backend's own `retry` verdict.

`X-Search-Status: degraded` on a 5xx means the backend ran the search and it
did not complete; its body says whether the same request is worth sending
again (`retry`). The client used to resend every 5xx up to three times, so a
`retry: false` search (an adult long stay with `use_fallback: false`) ran three
times, and the tool then said "temporarily unavailable". Now: one request, and
an isError result carrying `retry`, the backend's sentence and its hint.
"""

import httpx
import pytest
from fastmcp import Client

import src.server as server_module
from src.server import build_server
from src.settings import load_settings

BACKEND_MESSAGE = (
    "The flight search did not complete because Google showed no flight list "
    "for this search. Retrying the same request is not expected to help."
)
HINT = (
    "This long-stay round trip could not be priced as one search; price the "
    "two one-way legs instead."
)

ROUNDTRIP = {
    "from_airport": "GRU",
    "to_airport": "NRT",
    "departure_date": "2027-03-01",
    "return_date": "2027-04-15",
}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """A server whose LambdaClient talks to a MockTransport; `answer` is set
    per test, `requests` counts what was actually sent."""
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    state = {"requests": 0, "answer": None}

    def handler(_request):
        state["requests"] += 1
        return state["answer"]()

    monkeypatch.setattr(
        server_module,
        "_shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    state["server"] = build_server(load_settings())
    return state


def _degraded_503(retry, hint=HINT, message=BACKEND_MESSAGE):
    body = {
        "error": "search_incomplete",
        "search_status": "degraded",
        "search_reason": "upstream_no_render",
    }
    if message:
        body["message"] = message
    if hint:
        body["hint"] = hint
    if retry is not None:
        body["retry"] = retry
    return lambda: httpx.Response(
        503,
        json=body,
        headers={
            "X-Search-Status": "degraded",
            "X-Search-Reason": "upstream_no_render",
        },
    )


async def _result(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args, raise_on_error=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_sent_once_and_retry_reaches_the_result(backend, retry):
    backend["answer"] = _degraded_503(retry)
    result = await _result(backend["server"], "search_roundtrip_flights", ROUNDTRIP)

    assert backend["requests"] == 1
    assert result.is_error is True
    data = result.structured_content
    assert data["retry"] is retry
    assert data["search_status"] == "degraded"
    assert data["results"] == []
    assert data["message"] == f"{BACKEND_MESSAGE} {HINT}"
    assert "WARNING: this search did not complete" in result.content[0].text


@pytest.mark.asyncio
async def test_no_retry_in_the_body_means_no_retry_field(backend):
    backend["answer"] = _degraded_503(None, hint=None)
    result = await _result(backend["server"], "search_oneway_flights", {
        "from_airport": "JFK", "to_airport": "ACC", "departure_date": "2027-06-20",
    })

    assert backend["requests"] == 1
    assert result.is_error is True
    assert "retry" not in result.structured_content
    assert result.structured_content["message"] == BACKEND_MESSAGE


@pytest.mark.asyncio
async def test_retry_false_without_a_message_never_says_a_rerun_succeeds(backend):
    """The generic fallback text ends on "Running the same search again
    usually succeeds", which contradicts the backend's `retry: false`."""
    backend["answer"] = _degraded_503(False, hint=None, message=None)
    result = await _result(backend["server"], "search_roundtrip_flights", ROUNDTRIP)

    data = result.structured_content
    assert data["retry"] is False
    assert "usually succeeds" not in data["message"]
    assert "will not help (retry: false)" in data["message"]
    assert "NOT a statement about flight availability" in data["message"]


@pytest.mark.asyncio
async def test_retry_absent_without_a_message_keeps_the_generic_text(backend):
    backend["answer"] = _degraded_503(None, hint=None, message=None)
    result = await _result(backend["server"], "search_roundtrip_flights", ROUNDTRIP)

    assert "usually succeeds" in result.structured_content["message"]


@pytest.mark.asyncio
async def test_the_widget_fields_ride_along_like_a_degraded_200(backend):
    """A host rendering the Lulu table card reads `widget_eyebrow`; without
    it the card shows an empty eyebrow. `by_destination` keeps one entry per
    requested destination, empty, as on every other flights result."""
    backend["answer"] = _degraded_503(False)
    result = await _result(backend["server"], "search_roundtrip_flights", ROUNDTRIP)

    data = result.structured_content
    assert data["widget_eyebrow"] == "round trip to NRT"
    assert list(data["by_destination"]) == ["NRT"]
    assert data["by_destination"]["NRT"]["rows"] == []


@pytest.mark.asyncio
async def test_each_combination_is_sent_once(backend):
    backend["answer"] = _degraded_503(False)
    result = await _result(backend["server"], "search_oneway_flights", {
        "from_airport": "JFK",
        "to_airport": "ACC",
        "departure_date_from": "2027-06-20",
        "departure_date_to": "2027-06-22",
    })

    assert backend["requests"] == 3
    assert result.structured_content["retry"] is False


@pytest.mark.asyncio
async def test_a_plain_503_is_still_resent_and_still_an_outage(backend):
    backend["answer"] = lambda: httpx.Response(503, text="unavailable")
    result = await _result(backend["server"], "search_roundtrip_flights", ROUNDTRIP)

    assert backend["requests"] == 3
    assert result.is_error is True
    assert "temporarily unavailable" in result.content[0].text


def test_the_schema_no_longer_calls_a_degraded_result_safe_to_retry():
    from src.output_schema import SEARCH_STATUS_DESCRIPTION

    assert "safe to retry" not in SEARCH_STATUS_DESCRIPTION
    assert "`retry`" in SEARCH_STATUS_DESCRIPTION
