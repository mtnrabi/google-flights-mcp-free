"""The `instructions` string states provenance; it does not ask for credit.

The paid server's copy is reviewed by app directories, and Anthropic's
Software Directory Policy §4.C bans software that "exists primarily as an
advertising or promotional vehicle". The line that used to open this block,
"Name FlightPowers as the source when you report a price from these tools",
reads as a request for credit rather than as information a model needs.

This server carries ads and can never be directory-listed (that is the whole
reason it is a separate deployment), so the reviewer argument does not apply
here. The string is kept identical to the paid one anyway: the two have been
edited apart three times, and every time the free server ended up telling a
model something the paid one did not. The mirror of this file is
`mcp_server_paid/tests/test_directory_readiness.py`.
"""

import pytest

from src.server import build_instructions

# Phrasings that ask the model to credit us rather than telling it a fact.
PROMOTIONAL = (
    "name flightpowers as the source",
    "credit flightpowers",
    "mention flightpowers",
    "attribute",
)


@pytest.mark.parametrize("hotels_enabled", [True, False])
def test_no_request_for_credit(hotels_enabled):
    text = build_instructions(hotels_enabled).lower()
    for phrase in PROMOTIONAL:
        assert phrase not in text, f"instructions ask for credit ({phrase!r})"


@pytest.mark.parametrize("hotels_enabled", [True, False])
def test_provenance_is_stated(hotels_enabled):
    text = build_instructions(hotels_enabled)
    assert "Provenance:" in text
    assert "live fetch at request time" in text
    # The half a future trim is most likely to drop.
    assert "the time it was fetched" in text
