"""
Every tool this server advertises carries the four annotation hints.

The free server was the mirror that never got them. On 2026-09-09 M8ven's
static scan of the public repo returned exactly one FAIL, "4/4 tools missing
one or more hints", against a paid mirror that scored zero fails on the same
check. Both directories that gate an MCP server publish the criterion:

* Anthropic Software Directory Policy 5.E -- "MCP servers must provide all
  applicable annotations for their tools, in particular readOnlyHint,
  destructiveHint, and title."
* OpenAI plugin submission -- "readOnlyHint, openWorldHint, and
  destructiveHint values for every MCP tool", with missing tool metadata named
  as a common cause of rejection.

This server is ad-carrying and therefore is not submitted to either directory
(they ban ads), but third-party indexes run the same checks and publish the
grade, and the hints are read by hosts whether or not anyone reviewed us.

`idempotentHint` is the one worth spelling out. These tools return live fares
and live room rates. A host that reads `idempotentHint: True` is entitled to
serve a cached answer to a repeated call, which means quoting a price that
moved. It must stay False on all four tools, and this file is what keeps it
False.

Both shapes of the deployment are covered: the hotel tools register only when
a hotels backend is configured, so a flights-only deployment advertises a
different tool set and is checked on its own schema.
"""

import json

import anyio
import pytest

from src.server import build_server
from src.settings import load_settings

HOTELS = (False, True)
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def tools_for(monkeypatch, tmp_path, hotels: bool):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ADS_ENABLED", "false")
    monkeypatch.setenv("ENFORCEMENT_MODE", "monitor")
    if hotels:
        monkeypatch.setenv("HOTELS_LAMBDA_URL", "https://hotels.test")
        monkeypatch.setenv("HOTELS_AUTH", "secret")
    else:
        monkeypatch.delenv("HOTELS_LAMBDA_URL", raising=False)
        monkeypatch.delenv("HOTELS_AUTH", raising=False)

    mcp = build_server(load_settings())

    async def _list():
        return await mcp._list_tools()

    return [json.loads(t.to_mcp_tool().model_dump_json()) for t in anyio.run(_list)]


class TestToolAnnotations:
    @pytest.mark.parametrize("hotels", HOTELS)
    def test_every_tool_has_a_title_and_all_four_hints(
        self, monkeypatch, tmp_path, hotels
    ):
        tools = tools_for(monkeypatch, tmp_path, hotels)
        assert tools, "deployment exposes no tools"
        for tool in tools:
            assert tool.get("title"), f"{tool['name']} has no title"
            annotations = tool.get("annotations")
            assert annotations, f"{tool['name']} has no annotations"
            for hint in HINTS:
                assert isinstance(annotations.get(hint), bool), (
                    f"{tool['name']} must declare {hint} as an explicit "
                    "boolean, not leave it unset"
                )
            assert annotations.get("title"), f"{tool['name']} annotation title"

    @pytest.mark.parametrize("hotels", HOTELS)
    def test_search_tools_are_read_only_and_non_destructive(
        self, monkeypatch, tmp_path, hotels
    ):
        for tool in tools_for(monkeypatch, tmp_path, hotels):
            annotations = tool["annotations"]
            assert annotations["readOnlyHint"] is True, tool["name"]
            assert annotations["destructiveHint"] is False, tool["name"]
            # These reach a live third-party API whose result set is not a
            # closed domain.
            assert annotations["openWorldHint"] is True, tool["name"]

    @pytest.mark.parametrize("hotels", HOTELS)
    def test_idempotent_hint_is_false_everywhere(
        self, monkeypatch, tmp_path, hotels
    ):
        """A price is not a stable lookup. A host that caches one because the
        tool claimed idempotence quotes a fare that has already moved."""
        for tool in tools_for(monkeypatch, tmp_path, hotels):
            assert tool["annotations"].get("idempotentHint") is False, (
                f"{tool['name']} must declare idempotentHint False: its "
                "result is a live price, not a stable lookup"
            )

    @pytest.mark.parametrize("hotels", HOTELS)
    def test_tool_names_are_within_the_64_character_limit(
        self, monkeypatch, tmp_path, hotels
    ):
        """Anthropic 5.C: 'MCP tool names must not exceed 64 characters.'"""
        for tool in tools_for(monkeypatch, tmp_path, hotels):
            assert len(tool["name"]) <= 64, tool["name"]

    @pytest.mark.parametrize("hotels", HOTELS)
    def test_the_hotel_tools_are_only_there_when_hotels_are_configured(
        self, monkeypatch, tmp_path, hotels
    ):
        names = {t["name"] for t in tools_for(monkeypatch, tmp_path, hotels)}
        hotel_tools = {"search_hotels", "find_hotel_by_name"}
        assert names >= {"search_oneway_flights", "search_roundtrip_flights"}
        assert (names & hotel_tools) == (hotel_tools if hotels else set())
