"""
Every parameter this server advertises must carry a description.

A model plans a call from `inputSchema` alone. `{"type": "integer"}` for
`seat_type` tells it nothing; "1 economy, 2 premium economy, 3 business,
4 first" tells it everything. The descriptions were already written -- they sat
in each tool's Google-style `Args:` block -- but FastMCP builds the schema from
type hints, so until `src/schema_docs.document_params` was applied here 48 of
this server's 50 parameters reached the client bare. The two exceptions were
the `use_fallback` flags, which carry an explicit `Annotated[..., Field(...)]`.

The failure mode is silent: the server keeps answering perfectly while the
schema it advertises quietly stops being usable. So this file is a standing
gate rather than a one-off check -- a parameter added without a docstring line
fails the suite instead of shipping.

Both shapes of this deployment are covered. The hotel tools are registered only
when a hotels backend is configured, so a flights-only deployment advertises a
different tool set and has to be checked on its own schema.
"""

import json

import anyio
import pytest

from src.schema_docs import (
    document_params,
    parse_args_section,
    undocumented_params,
)
from src.server import build_server
from src.settings import load_settings

HOTELS = (False, True)


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


class TestParameterDescriptions:
    @pytest.mark.parametrize("hotels", HOTELS)
    def test_no_parameter_ships_without_a_description(
        self, monkeypatch, tmp_path, hotels
    ):
        tools = tools_for(monkeypatch, tmp_path, hotels)
        assert tools, "the server registered no tools at all"
        for tool in tools:
            missing = undocumented_params(tool["inputSchema"])
            assert not missing, (
                f"{tool['name']} exposes undocumented parameters: {missing}. "
                "Add a line for each to the function's Args: docstring block."
            )

    def test_the_hotel_tools_are_covered_when_configured(
        self, monkeypatch, tmp_path
    ):
        """Guards the parametrisation itself: if the hotel tools stopped
        registering, the check above would pass by describing nothing."""
        names = {t["name"] for t in tools_for(monkeypatch, tmp_path, True)}
        assert names == {
            "search_oneway_flights",
            "search_roundtrip_flights",
            "search_hotels",
            "find_hotel_by_name",
        }

    def test_the_descriptions_are_the_docstring_text(self, monkeypatch, tmp_path):
        """Not just non-empty -- actually the sentence from the source."""
        tools = tools_for(monkeypatch, tmp_path, True)
        oneway = next(t for t in tools if t["name"] == "search_oneway_flights")
        properties = oneway["inputSchema"]["properties"]
        assert "1 economy" in properties["seat_type"]["description"]
        assert "IATA" in properties["from_airport"]["description"]

        hotels = next(t for t in tools if t["name"] == "search_hotels")
        checkout = hotels["inputSchema"]["properties"]["checkout_date"]
        # A wrapped multi-line entry arrives as one sentence, not with the
        # source's line breaks baked into it.
        assert "\n" not in checkout["description"]
        assert "Must be after checkin_date" in checkout["description"]

    def test_use_fallback_keeps_its_explicit_field_text(
        self, monkeypatch, tmp_path
    ):
        """`use_fallback` is annotated by hand and its docstring line points at
        that text rather than repeating it. The decorator must leave the
        explicit Field alone, or the schema would advertise the pointer."""
        tools = tools_for(monkeypatch, tmp_path, False)
        for name in ("search_oneway_flights", "search_roundtrip_flights"):
            tool = next(t for t in tools if t["name"] == name)
            text = tool["inputSchema"]["properties"]["use_fallback"]["description"]
            assert "USE_FALLBACK_DESCRIPTION" not in text
            assert text.strip()


class TestArgsBlockParser:
    """The parser this depends on, copied from mcp_server_paid alongside the
    module itself. Kept here so a divergence between the two copies fails in
    whichever package it was introduced in."""

    def test_reads_a_simple_block(self):
        assert parse_args_section("Args:\n    a: First.\n    b: Second.") == {
            "a": "First.",
            "b": "Second.",
        }

    def test_folds_a_wrapped_entry_into_one_sentence(self):
        docs = parse_args_section(
            """
            Args:
                a: First line
                    and its continuation.
            """
        )
        assert docs == {"a": "First line and its continuation."}

    def test_stops_at_a_sibling_section(self):
        docs = parse_args_section(
            """
            Args:
                a: First.

            Returns:
                Something that is not a parameter.
            """
        )
        assert docs == {"a": "First."}

    def test_no_block_and_no_docstring_are_both_empty(self):
        assert parse_args_section(None) == {}
        assert parse_args_section("Just a summary.") == {}

    def test_decorator_is_a_no_op_without_a_block(self):
        def fn(a: int) -> int:
            """No Args here."""
            return a

        before = dict(fn.__annotations__)
        assert document_params(fn).__annotations__ == before

    def test_decorator_leaves_an_existing_annotated_alone(self):
        """A second Field() inside one Annotated is a pydantic error, not a
        merge, so an explicitly annotated parameter must be skipped."""
        from typing import Annotated, get_args

        from pydantic import Field

        def fn(a: Annotated[int, Field(description="explicit")]) -> int:
            """
            Args:
                a: docstring version.
            """
            return a

        document_params(fn)
        metadata = get_args(fn.__annotations__["a"])[1:]
        assert len(metadata) == 1
        assert metadata[0].description == "explicit"
