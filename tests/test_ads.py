"""Lulu wiring, verified against a stub ads server rather than mocked out.

The business-critical assertions live here: an ad must attach to a real
result, and must NOT attach to an empty or errored one.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastmcp import Client

from src import lambda_client as lambda_client_module
from src import server as server_module
from src.server import build_server
from src.settings import load_settings

# NOTE the snake_case `imp_url`. The TS typings call this field `impUrl`, but
# the Python SDK allowlists response keys and only reads `imp_url`
# (lulu_ads/client.py:210). A camelCase key is silently dropped -- and since
# this field carries the rendered-impression beacon, dropping it means the
# widget has nothing to fire and CPM stays at zero with no error anywhere.
SLOT_RESPONSE = {
    "label": "Sponsored",
    "text": "Travel insurance from $12 with Battleface",
    "url": "https://ads.getlulu.dev/c/test-token",
    "imp_url": "https://ads.getlulu.dev/i/test-token",
}


class _StubAds(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b"{}"
        _StubAds.requests.append(
            {
                "path": self.path,
                "api_key": self.headers.get("x-api-key"),
                "body": json.loads(body or b"{}"),
            }
        )
        if self.path.rstrip("/") == "/slot":
            payload = json.dumps(SLOT_RESPONSE).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence the default stderr spam
        return


@pytest.fixture
def ads_server():
    _StubAds.requests = []
    server = HTTPServer(("127.0.0.1", 0), _StubAds)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def ad_server(ads_server, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "true")
    monkeypatch.setenv("LULU_ADS_PUBLISHER_ID", "pub_test")
    monkeypatch.setenv("LULU_ADS_API_KEY", "lk_test")
    monkeypatch.setenv("LULU_ADS_BASE_URL", ads_server)
    return build_server(load_settings())


def _stub_lambda(monkeypatch, rows):
    async def fake(self, endpoint, payload):
        return list(rows)

    monkeypatch.setattr(lambda_client_module.LambdaClient, "search", fake)


ONE_FLIGHT = [{"buy_link": "https://book/x", "price_as_number": 412, "airline": "Delta"}]


class TestAdAttachment:
    @pytest.mark.asyncio
    async def test_ad_attaches_to_a_real_result(self, ad_server, monkeypatch):
        _stub_lambda(monkeypatch, ONE_FLIGHT)
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })
        sponsored = result.structured_content.get("sponsored")
        assert sponsored is not None, "no sponsored field -- no revenue"
        assert sponsored["label"] == "Sponsored"
        assert sponsored["url"].startswith("https://ads.getlulu.dev/c/")
        # The beacon is what CPM is actually counted on. If this field is
        # missing from a live /slot response, there is no rendered impression
        # to bill, however well everything else works.
        assert sponsored.get("imp_url"), "no imp_url -- no billable impression"

    @pytest.mark.asyncio
    async def test_slot_request_is_authenticated_and_carries_context(
        self, ad_server, monkeypatch
    ):
        _stub_lambda(monkeypatch, ONE_FLIGHT)
        async with Client(ad_server) as client:
            await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })
        slot_calls = [r for r in _StubAds.requests if r["path"].rstrip("/") == "/slot"]
        assert slot_calls, "the SDK never asked for a slot"
        assert slot_calls[0]["api_key"] == "lk_test"
        assert slot_calls[0]["body"].get("context", {}).get("tool")

    @pytest.mark.asyncio
    async def test_no_ad_on_zero_results(self, ad_server, monkeypatch):
        # Lulu's own live testing: an ad with no substantive data next to it
        # was flagged by the model as suspected prompt injection, 3/3 runs.
        _stub_lambda(monkeypatch, [])
        async with Client(ad_server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })
        assert result.structured_content.get("sponsored") is None

    @pytest.mark.asyncio
    async def test_no_ad_when_the_backend_is_down(self, ad_server, monkeypatch):
        async def always_fail(self, endpoint, payload):
            raise lambda_client_module.LambdaError("502")

        monkeypatch.setattr(lambda_client_module.LambdaClient, "search", always_fail)
        async with Client(ad_server) as client:
            result = await client.call_tool(
                "search_oneway_flights",
                {"from_airport": "TLV", "to_airport": "CMB",
                 "departure_date": "2026-10-14"},
                raise_on_error=False,
            )
        assert result.is_error
        blob = json.dumps(
            [c.model_dump(mode="json") for c in result.content]
        ) + json.dumps(result.structured_content or {})
        assert "getlulu.dev/c/" not in blob, "never advertise on an error"


class TestAdOutageIsolation:
    @pytest.mark.asyncio
    async def test_flights_still_work_when_the_ads_server_is_dead(
        self, tmp_path, monkeypatch
    ):
        # An ad outage must never take flight search down with it.
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "true")
        monkeypatch.setenv("LULU_ADS_PUBLISHER_ID", "pub_test")
        monkeypatch.setenv("LULU_ADS_API_KEY", "lk_test")
        # Nothing listening on this port.
        monkeypatch.setenv("LULU_ADS_BASE_URL", "http://127.0.0.1:1")
        server = build_server(load_settings())
        _stub_lambda(monkeypatch, ONE_FLIGHT)

        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })
        assert result.structured_content["result_count"] == 1
        assert result.structured_content.get("sponsored") is None

    @pytest.mark.asyncio
    async def test_inert_without_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "true")
        monkeypatch.delenv("LULU_ADS_PUBLISHER_ID", raising=False)
        monkeypatch.delenv("LULU_ADS_API_KEY", raising=False)
        server = build_server(load_settings())
        _stub_lambda(monkeypatch, ONE_FLIGHT)

        async with Client(server) as client:
            result = await client.call_tool("search_oneway_flights", {
                "from_airport": "TLV", "to_airport": "CMB",
                "departure_date": "2026-10-14",
            })
        assert result.structured_content["result_count"] == 1


# ── result widget ────────────────────────────────────────────────────────
#
# This is where CPM actually comes from, and every failure mode in it is
# silent: the card still paints, the ad still shows, the click still pays,
# and the impression -- the thing billed at $0.20/1,000 -- never fires.
# Nothing logs, nothing errors. Hence assertions on the served bytes.

ONEWAY_ROW = {
    "price_range_in_relation_to_other_periods": "high",
    "price_insights_low": 100,
    "price_insights_high": 210,
    "from_airport": "Tel Aviv (TLV)",
    "to_airport": "Budapest (BUD)",
    "departure_date": "2026-08-14",
    "price": "$231",
    "price_as_number": 231,
    "duration": "3 hr 30 min",
    "duration_seconds": 12600,
    "buy_link": "https://book/ow",
    "airline": "Wizz Air",
    "stops": 0,
    "departure_description": "6:50 PM on Fri, Aug 14",
    "arrival_description": "9:20 PM on Fri, Aug 14",
}

ROUNDTRIP_ROW = {
    "price_range_in_relation_to_other_periods": "typical",
    "price_insights_low": 315,
    "price_insights_high": 480,
    "from_airport": "Tel Aviv (TLV)",
    "to_airport": "Budapest (BUD)",
    "departure_date": "2026-08-14",
    "return_date": "2026-08-17",
    "total_price": "$439",
    "total_price_as_number": 439,
    "total_duration_seconds": 24300,
    "total_stops": 0,
    "buy_link": "https://book/rt",
    "departure_flight_airline": "Wizz Air",
    "departure_flight_duration": "3 hr 30 min",
    "departure_flight_stops": 0,
    # Verified against a live roundtrip response 2026-08-17 -- these are the
    # exact field names the backend emits, and the widget's Depart/Return
    # columns resolve against them.
    "departure_flight_departure_description": "10:10 AM on Tue, Nov 10",
    "departure_flight_arrival_description": "12:45 PM on Tue, Nov 10",
    "return_flight_airline": "Wizz Air",
    "return_flight_duration": "3 hr 15 min",
    "return_flight_stops": 0,
    "return_flight_departure_description": "5:00 AM on Sat, Nov 14",
    "return_flight_arrival_description": "9:15 AM on Sat, Nov 14",
}

WIDGETS = [
    ("search_oneway_flights", server_module.ONEWAY_WIDGET_MAPPING, ONEWAY_ROW,
     {"from_airport": "TLV", "to_airport": "BUD", "departure_date": "2026-08-14"}),
    ("search_roundtrip_flights", server_module.ROUNDTRIP_WIDGET_MAPPING,
     ROUNDTRIP_ROW,
     {"from_airport": "TLV", "to_airport": "BUD",
      "departure_date": "2026-08-14", "return_date": "2026-08-17"}),
]


def _resolve(entry, scope):
    """The widget's own resolve(), in Python -- widgets.py:366-389."""
    path = entry if isinstance(entry, str) else entry.get("path")
    cur = scope
    for part in str(path).split("."):
        if cur is None:
            return None
        cur = cur[int(part)] if isinstance(cur, list) else cur.get(part)
    return cur


async def _widget_html(server, tool):
    resource = await server.read_resource(f"ui://lulu-ads/result-{tool}.html")
    return resource.contents[0].content


class TestResultWidget:
    @pytest.mark.parametrize("tool,_m,_r,_a", WIDGETS)
    @pytest.mark.asyncio
    async def test_tool_points_at_its_result_widget(self, ad_server, tool, _m, _r, _a):
        # Not the sponsored card: that one has no beacon, so pointing at it
        # earns clicks and exactly zero CPM.
        async with Client(ad_server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        meta = tools[tool].meta or {}
        uri = f"ui://lulu-ads/result-{tool}.html"
        assert meta["ui"]["resourceUri"] == uri
        assert meta["openai/outputTemplate"] == uri, "ChatGPT would show no widget"

    @pytest.mark.parametrize("tool,_m,_r,_a", WIDGETS)
    @pytest.mark.asyncio
    async def test_served_widget_carries_the_impression_beacon(
        self, ad_server, tool, _m, _r, _a
    ):
        html = await _widget_html(ad_server, tool)
        assert "s.imp_url" in html, "widget never reads the beacon URL"
        assert "new Image(1, 1)" in html, "widget never fires the beacon"
        assert 'id="strip"' in html

    @pytest.mark.asyncio
    async def test_the_sponsored_card_still_has_no_beacon(self, ad_server):
        # Documents WHY the result widget exists. If a future lulu-ads
        # release adds the beacon here, this fails and the whole result
        # widget can be reconsidered.
        resource = await ad_server.read_resource("ui://lulu-ads/sponsored.html")
        html = resource.contents[0].content
        assert "imp_url" not in html and "impUrl" not in html

    @pytest.mark.parametrize("tool,_m,_r,_a", WIDGETS)
    @pytest.mark.asyncio
    async def test_widget_declares_the_ads_origin_to_both_host_families(
        self, ad_server, tool, _m, _r, _a
    ):
        # Hosts default to `img-src 'self' data:`. Without these the beacon
        # is blocked and everything else looks perfect.
        async with Client(ad_server) as client:
            resources = {str(r.uri): r for r in await client.list_resources()}
        meta = resources[f"ui://lulu-ads/result-{tool}.html"].meta or {}
        assert server_module.ADS_ORIGIN in meta["ui"]["csp"]["resourceDomains"]
        assert server_module.ADS_ORIGIN in meta["openai/widgetCSP"]["resource_domains"]

    @pytest.mark.parametrize("tool,_m,_r,_a", WIDGETS)
    @pytest.mark.asyncio
    async def test_widget_domain_matches_the_public_url(
        self, ad_server, tool, _m, _r, _a
    ):
        # Lulu hashes MCP_PUBLIC_URL into Claude's _meta.ui.domain; a
        # mismatch means the widget never renders at all.
        from lulu_ads.widget import claude_apps_domain

        async with Client(ad_server) as client:
            resources = {str(r.uri): r for r in await client.list_resources()}
        meta = resources[f"ui://lulu-ads/result-{tool}.html"].meta or {}
        expected = claude_apps_domain(load_settings().public_url)
        assert meta["ui"]["domain"] == expected

    @pytest.mark.parametrize("tool,mapping,row,args", WIDGETS)
    @pytest.mark.asyncio
    async def test_every_mapped_path_resolves_against_a_real_response(
        self, ad_server, monkeypatch, tool, mapping, row, args
    ):
        # A renamed backend field empties a column silently -- the card still
        # renders, just blank. This resolves each mapped path exactly as the
        # widget's JS does, against a real tool response.
        _stub_lambda(monkeypatch, [row])
        async with Client(ad_server) as client:
            result = await client.call_tool(tool, args)
        payload = result.structured_content

        assert _resolve(mapping["eyebrow"], payload), "eyebrow path is dead"
        rows = _resolve(mapping["rows"], payload)
        assert rows, "rows path resolved to nothing -- the table would be empty"
        for column in mapping["columns"]:
            value = _resolve(column, rows[0])
            assert value is not None, (
                f"{tool}: column {column['header']!r} maps to "
                f"{column['path']!r}, which is not in the response"
            )

    @pytest.mark.asyncio
    async def test_no_result_widgets_when_ads_are_disabled(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
        monkeypatch.setenv("ADS_ENABLED", "false")
        server = build_server(load_settings())
        async with Client(server) as client:
            uris = {str(r.uri) for r in await client.list_resources()}
        assert not [u for u in uris if u.startswith("ui://lulu-ads/")]
