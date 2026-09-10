"""
Discovery without a credential: what a caller who has not signed in may look
at, and where the 401 still stands.

Why this file exists
--------------------
On 2026-09-09 `/mcp` began answering a credential-less caller with
`401 + WWW-Authenticate`, which is the one thing on the wire that makes an
MCP client show a Sign in button (src/oauth.py). It also broke every
directory that lists us. Glama re-checks each connector HOURLY by opening an
MCP connection and listing its tools, and that check connects with NO
credentials: a 401 there marks the listing unhealthy and ranks it down, which
is what Glama's mail to Matan reported the same day for the paid listings.
Smithery's release scan, mcpservers.org and M8ven probe the same way, and
Claude's own connector dialog probes before deciding which auth mode to
offer.

So the line is drawn at spending, not at connecting:

    initialize, notifications/initialized, ping, tools/list, prompts/list,
    resources/list, resources/templates/list
        -> served, no credential needed. None of these reaches a Lambda,
           spends a backend search or reads anything belonging to an account.
           The tool list they return is already public: it is in every
           directory entry and in the README.

    tools/call, and every other method
        -> the 401 that was there before, with the same challenge header and
           the same directions body.

Why this is not `hard_limit.BILLABLE_METHOD`
--------------------------------------------
The caps (`hard_limit.HardLimitMiddleware`, `anon_gate.AnonCapMiddleware`)
already ask a version of this question, and they answer it the other way
round on purpose: anything that is not provably a `tools/call` is LET
THROUGH, because a body they cannot parse should cost a slightly worse
refusal downstream, never a free search.

An authorization gate has to fail the other way. "I could not read what you
were asking for" is not a reason to serve an anonymous caller, so this module
matches against a closed list of read-only methods and challenges everything
else -- unparseable bodies, oversized bodies, batches with one `tools/call`
hidden in them. Same buffering machinery (`hard_limit._buffered`, so a
request body is drained and replayed exactly once, and the RESPONSE, which
has to stream, is never touched), opposite default.

What this deliberately does NOT do
----------------------------------
* **It does not touch `/mcp/oauth`.** That alias challenges everything,
  whatever the mode: it is the URL for a client whose auth mode is fixed when
  the server is ADDED, and for a directory that wants a server which always
  requires auth.
* **It only reads POSTs.** A GET on `/mcp` is the optional server-to-client
  stream; it carries no method to check, so it keeps the challenge -- which
  is also the only thing a browser or a `curl` probe would ever see. A client
  that cannot open that stream still completes `initialize` and `tools/list`,
  which is what a health check is.
* **It does not decide anything about a caller who brought a credential.**
  That request was never challenged and never reaches this file.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

from .hard_limit import _buffered

#: Everything a directory scanner, a connector dialog or a curious human
#: needs to see the menu, and nothing that reads the kitchen.
#:
#: `notifications/initialized` is here because it is the message a client
#: sends immediately after `initialize`; challenging it would break the
#: handshake one step before the tool list. `ping` is here because it is how
#: several health checks decide a server is alive.
DISCOVERY_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }
)

#: A discovery POST is a few hundred bytes; an `initialize` carrying a fat
#: capabilities object is still under a kilobyte. Past this, a body is not a
#: discovery request and is challenged without being read further.
MAX_DISCOVERY_BODY = 64 * 1024

Receive = Callable[[], Awaitable[dict]]


def is_discovery_payload(raw: bytes) -> bool:
    """True when this body contains ONLY read-only discovery calls.

    False for anything unparseable, anything empty, and any batch with one
    non-discovery entry in it -- a batch is answered as one HTTP response, so
    it is served whole or challenged whole, and a `tools/call` behind two
    `tools/list`s must not be the way through.
    """
    if not raw or len(raw) > MAX_DISCOVERY_BODY:
        return False
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    entries: list[Any]
    if isinstance(parsed, dict):
        entries = [parsed]
    elif isinstance(parsed, list) and parsed:
        entries = list(parsed)
    else:
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        method = entry.get("method")
        if not isinstance(method, str) or method not in DISCOVERY_METHODS:
            return False
    return True


def joined_body(messages: list[dict[str, Any]]) -> bytes:
    """The body bytes out of what `hard_limit._buffered` collected."""
    return b"".join(
        m.get("body") or b"" for m in messages if m.get("type") == "http.request"
    )


async def discovery_probe(
    scope: Mapping[str, Any], receive: Receive | None
) -> tuple[bool, Receive | None]:
    """`(serve_it, receive)` for a credential-less request on `/mcp`.

    `serve_it` is True only for a POST whose body is discovery and nothing
    else. The returned `receive` is the one to pass on in EITHER case: the
    body has been taken off the channel by then, and the app must be given it
    back or it will wait for bytes that already arrived.

    A `None` receive (a unit test driving the gate by hand) is answered
    `(False, None)`: no body, nothing to serve, challenge.
    """
    if (scope.get("method") or "").upper() != "POST":
        return False, receive
    if receive is None:
        return False, None
    messages, replay = await _buffered(receive)
    return is_discovery_payload(joined_body(messages)), replay
