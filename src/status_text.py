"""The first line of the text block: prose first, JSON after.

Why this exists
---------------
`search_status` is declared, typed and validated (src/output_schema.py), and
`degraded` additionally rides on `isError: true`. All of that is aimed at a
client. None of it is aimed at the model, which in most hosts sees one thing:
the text block. And that block was, byte for byte, the serialized JSON of
`structuredContent` -- so `"search_status": "degraded"` sat in the middle of
an object, in the same visual register as `"currency": "usd"`, and got
skimmed past exactly the way a bare `[]` used to be skimmed past. Structuring
a warning is not the same as making it read like one.

The fix a reader (u/lulu_dev, r/mcp, comment p7exhmy, 2026-09-02) proposed
and we committed to publicly: keep the schema for the clients that read it,
and put loud natural language first for the ones that do not.

    Schema for the clients that read it, loud natural language for the ones
    that don't.

So a result whose search did not complete now leads with a sentence saying
so, and the serialized JSON follows it as a second content block. The
backwards-compatibility duplicate the spec asks for is still there; it is
just no longer the first thing read.

`partial` is the case this matters most for, not `degraded`
-----------------------------------------------------------
`degraded` carries `isError: true`, so a spec-following host already has
something to flag. `partial` has real rows, and `isError` is false *by
design* because those rows are usable -- which leaves a model every reason to
treat the rows it got as the whole answer, when part of the range it asked
for was never scraped. Nothing in the protocol says otherwise. The only
channel left is the prose, so `partial` gets the more specific line of the
two: which searches are missing, and how many of the ones attempted came
back.

Two rules the wording follows
-----------------------------
1. **No digits in the degraded line.** It is fixed prose with nothing
   interpolated, so no count, latency or percentage can ever drift into it
   and become an invented metric (CLAUDE.md rule 1).
2. **Every number in the partial line comes from the search outcome.**
   `completed` and `attempted` are counted off the plan and the per-search
   results; the named combinations are the ones that actually raised or came
   back flagged. Nothing here is estimated.

On this server there is a second job, at the bottom of the file: keeping the
sponsored card BEHIND the warning, and keeping the ad in sync with the JSON
block now that the JSON is no longer block zero. A card in front of "this
search did not complete" would be the failure this change exists to prevent.

What this module does NOT touch: `structuredContent` and `outputSchema` are
byte-identical to what they were. This is the text block only.
"""

from __future__ import annotations

from typing import Any, Iterable

from fastmcp.server.middleware import Middleware
from mcp.types import TextContent
from pydantic_core import to_json

#: A fan-out is capped at a couple of dozen searches, but a caller can raise
#: the cap, and a first line that names forty dates is a first line nobody
#: reads. Past this many, the rest are counted rather than listed -- the count
#: is still real.
MAX_NAMED_COMBINATIONS = 8

#: Fixed prose, no interpolation. See rule 1 above.
DEGRADED_FIRST_LINE = (
    "WARNING: this search did not complete. Do not treat the result below as "
    "a complete result and do not tell the user whether flights exist on this "
    "route -- the JSON that follows records a failed search, not an answer."
)

_PARTIAL_TAIL = (
    "Treat the results below as a floor on what is available, not the full "
    "picture, and say so if you report them."
)


def serialize_payload(payload: Any) -> str:
    """The exact text FastMCP would have put in the auto-generated block.

    FastMCP's `default_serializer` is `TypeAdapter(Any).dump_json(data,
    fallback=str)`; `pydantic_core.to_json` with the same fallback is the same
    call one layer down, and pydantic is already a declared dependency of this
    server. Pinned by a test that compares this against a real `ok` result's
    auto-generated block rather than trusting the equivalence.
    """
    return to_json(payload, fallback=str).decode()


def describe_combination(combo: dict[str, Any]) -> str:
    """One searched date/destination pair, as a person would say it."""
    date = str(combo.get("departure_date") or "").strip()
    destination = str(combo.get("to_airport") or "").strip()
    if date and destination:
        return f"{date} to {destination}"
    return date or destination


def _name_them(combinations: Iterable[str]) -> str:
    named = [c for c in combinations if c]
    if len(named) > MAX_NAMED_COMBINATIONS:
        head = ", ".join(named[:MAX_NAMED_COMBINATIONS])
        return f"{head} and {len(named) - MAX_NAMED_COMBINATIONS} more"
    return ", ".join(named)


def partial_first_line(
    *,
    completed: int,
    attempted: int,
    missing: Iterable[str],
) -> str:
    """The coverage line: what came back, and what is missing by name.

    `completed` and `attempted` are counts off the executed plan. `missing`
    is the set of combinations that raised or came back flagged incomplete --
    real searches, named from the payload that was actually sent. When the
    combinations cannot be identified (an older path, or a failure with no
    payload to name), the line falls back to the counts alone rather than
    guessing at names.
    """
    named = _name_them(sorted(set(missing)))
    head = f"COVERAGE WARNING: {completed} of {attempted} searches completed."
    if named:
        return (
            f"{head} Nothing came back for {named}, so those searches are "
            f"missing from the results below. {_PARTIAL_TAIL}"
        )
    return (
        f"{head} The searches that did not complete are missing from the "
        f"results below. {_PARTIAL_TAIL}"
    )


# ── keeping the ad behind the warning, and in sync ───────────────────────


class SponsoredTextSyncMiddleware(Middleware):
    """Re-syncs the JSON block with the ad, now that it is not block zero.

    `LuluAdsMiddleware` attaches `sponsored` to `structured_content` after the
    tool returns, and then rewrites the text block so `content[]` matches --
    which matters, because the client that renders the card reads `content[]`,
    not `structuredContent` (confirmed live against Claude.ai; see the SDK's
    own comment). It only performs that rewrite when `content` is exactly one
    auto-generated `TextContent`, and deliberately leaves anything richer
    alone rather than destroying real content.

    A warning line makes the result exactly that "anything richer": prose in
    block 0, JSON in block 1. So the SDK now (correctly) declines, and without
    this the ad-carrying JSON block would go stale on precisely the results
    that carry a coverage warning. This middleware does the rewrite the SDK
    skipped, on the shape it skipped, and nothing else.

    Ordering is the other half of the job, and it is why this is registered
    BEFORE the ads middleware. FastMCP builds its chain with
    `for mw in reversed(self.middleware)`, so the first-registered middleware
    is the outermost and therefore the LAST to run on the way out -- after the
    ad has been attached. That is the only position from which this can see
    the final content, and it is what guarantees the warning stays block 0
    with the sponsored card behind it. A sponsored card in front of "this
    search did not complete" would be the failure this whole change exists to
    prevent.

    Fail-open, like the ad middleware itself: a search result must never be
    lost to a formatting concern.
    """

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        try:
            sync_sponsored_into_text(result)
        except Exception:  # noqa: BLE001 - never break a tool result
            pass
        return result


def sync_sponsored_into_text(result: Any) -> None:
    """Rewrite the JSON block of a prose-first result so it carries the ad.

    Only touches the exact shape this module produces -- prose in block 0, a
    JSON object in block 1, and nothing else -- and only when an ad was
    actually attached.

    The exactness matters on the CLI path. There the SDK appends its own
    human-readable card as a third block and deliberately does NOT rewrite the
    JSON, because the card is already the rendering the terminal will show.
    Rewriting under it would be second-guessing that choice, so a
    three-block result is left alone, exactly as the SDK leaves it. Either
    way the card is appended at the END, which is what keeps it behind the
    warning rather than in front of it.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict) or "sponsored" not in structured:
        return
    content = getattr(result, "content", None) or []
    if len(content) != 2:
        return
    prose, payload_block = content[0], content[1]
    if not isinstance(prose, TextContent) or not isinstance(payload_block, TextContent):
        return
    if prose.text.startswith("{") or not payload_block.text.startswith("{"):
        return
    content[1] = TextContent(type="text", text=serialize_payload(structured))
