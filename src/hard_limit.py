"""
The hard escalation: HTTP 429 for a client that ignores the soft refusal.

Why this is middleware and not a tool
-------------------------------------
`fair_use.rate_limited_result` is a 200 with `search_status: "rate_limited"`
in the body, and that is deliberately what a first refusal looks like: a model
reads it, tells the user there is a paid server, and stops. It is worth
nothing to a script that never inspects a response. On 2026-09-06 the 04:00Z
batch client spent its 150 backend calls and then made 486 more tool calls in
24 minutes -- roughly 20 a minute -- because nothing in its code reads the
body (state/gtm/free-mcp-batch-client-searches-2026-09-06.md). Every one of
those cost a function invocation and two Upstash round trips and bought the
caller nothing.

The one signal every HTTP client already understands is the status line, so
past FAIR_USE_HARD_AFTER refusals in a rolling hour that is what the caller
gets: 429 with a Retry-After header. The MCP Streamable HTTP transport is
ordinary HTTP -- the spec describes the client POSTing JSON-RPC to a single
endpoint and reading an ordinary HTTP response -- so the status code is the
server's to choose, and every well-behaved HTTP stack in every language backs
off on a 429 without being taught to.

It has to happen HERE, though, in front of the app. By the time a tool
function runs, FastMCP's session manager has already decided on a 200 and, in
streaming mode, has usually begun writing it; a tool can only put words in a
body. ASGI middleware is the earliest layer that still owns the status line,
and it is also the cheapest -- a hard-blocked request never reaches the
session manager, never parses JSON-RPC, and never touches a tool.

What it deliberately does not do
--------------------------------
* It does not read the request body. Identity comes from headers, the query
  string and the peer address, exactly as `fair_use.identify` takes them, so
  the middleware never has to buffer or replay a stream.
* It does not decide who is a gateway. Pooled gateway keys are exempt from the
  escalation, and that exemption is enforced at the WRITE site
  (`stores.bump`), where the key kind was computed with the OpenAI egress feed
  loaded. It matters because `gateway_pooled` and `direct` are the same digest
  for the same caller -- only the kind separates them -- and this middleware
  runs before the feed is guaranteed loaded. Since a pooled key never has a
  refusal written against it, its count here is always zero. The kind test
  below is a second belt, not the buckle.
* It does not re-arm itself. Serving a 429 counts a 429; it does not count a
  refusal. If it did, the rolling window would top itself up on every blocked
  request and a caller who fixed its loop an hour ago would still be refused.
* It touches nothing but `POST` on the MCP path. `/health` must answer for a
  monitor no matter who else is misbehaving, and `/metrics` must answer for
  us.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .fair_use import (
    GATEWAY_KINDS,
    caps_for,
    hard_block_body,
    hard_block_message,
    hard_log_line,
    identify,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)

#: The canonical MCP path, no trailing slash (api/index.py's docstring
#: explains why the slash matters elsewhere). `/mcp/` is accepted here too so
#: the redirect target cannot become a way around the escalation.
DEFAULT_MCP_PATH = "/mcp"


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Lowercased headers from a raw ASGI scope.

    Repeated headers are joined with ", " -- which is what an HTTP stack does
    anyway, and is what makes a multi-hop `x-forwarded-for` arrive as one
    comma-separated string the way `fair_use` expects it.
    """
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or ():
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        out[name] = f"{out[name]}, {value}" if name in out else value
    return out


def _query(scope: Mapping[str, Any]) -> dict[str, str]:
    """The query string as a flat mapping, last value wins.

    Parsed with the stdlib rather than Starlette's QueryParams so this stays a
    pure-ASGI component that can be unit-tested without building a Request.
    """
    from urllib.parse import parse_qsl

    raw = scope.get("query_string") or b""
    if not raw:
        return {}
    return {
        key: value
        for key, value in parse_qsl(
            raw.decode("latin-1"), keep_blank_values=True
        )
    }


class HardLimitMiddleware:
    """Pure-ASGI. Answers 429 to a client that is looping through refusals.

    Constructed with the same settings, telemetry and classifier the server
    itself uses, so the identity computed here and the identity computed
    inside a tool are the same value for the same request.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        settings: Any,
        telemetry: Any,
        classifier: Any,
        path: str = DEFAULT_MCP_PATH,
    ) -> None:
        self.app = app
        self._settings = settings
        self._telemetry = telemetry
        self._classifier = classifier
        self._path = path.rstrip("/") or "/"

    # ── the decision ─────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        """False when the escalation is switched off, in either of two ways.

        FAIR_USE_ENABLED=0 removes the caps entirely, and there is nothing to
        escalate from when nothing is refused. FAIR_USE_HARD_AFTER=0 keeps the
        soft refusal and drops only the 429.
        """
        return bool(
            getattr(self._settings, "fair_use_enabled", False)
            and getattr(self._settings, "fair_use_hard_after", 0) > 0
        )

    def _applies(self, scope: Mapping[str, Any]) -> bool:
        if scope.get("type") != "http":
            return False
        if (scope.get("method") or "").upper() != "POST":
            return False
        path = (scope.get("path") or "/").rstrip("/") or "/"
        return path == self._path

    def _identity(self, scope: Mapping[str, Any]) -> Any:
        client = scope.get("client") or None
        peer = client[0] if client else None
        return identify(
            _headers(scope),
            _query(scope),
            peer=peer,
            # No `ensure_openai_ranges()` here: it is a blocking network call
            # and this runs on every MCP POST, cold starts included. Whatever
            # the classifier has already loaded is used, and a range that has
            # not loaded yet can only make a caller look `direct` -- which is
            # harmless, because a pooled caller has no refusals counted
            # against its key in the first place. See the module docstring.
            gateway_networks=self._classifier.gateway_networks(
                getattr(self._settings, "fair_use_gateway_cidrs", ())
            ),
            gateway_user_agents=getattr(
                self._settings, "fair_use_gateway_user_agents", ()
            ),
            identity_params=getattr(
                self._settings, "fair_use_identity_params", ()
            ),
        )

    # ── ASGI ─────────────────────────────────────────────────────────────

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if not (self.armed and self._applies(scope)):
            await self.app(scope, receive, send)
            return

        try:
            identity = self._identity(scope)
        except Exception as exc:  # noqa: BLE001 - never fail a request here
            logger.warning("could not identify a caller for hard limit: %s", exc)
            identity = None

        # Nothing to key on (stdio, a local connection, a test), or a pooled
        # gateway key. Both pass straight through -- the second one can only
        # ever read zero anyway, and saying so costs nothing.
        if identity is None or identity.kind in GATEWAY_KINDS:
            await self.app(scope, receive, send)
            return

        threshold = int(getattr(self._settings, "fair_use_hard_after", 0))
        refusals = await self._telemetry.fair_use_refusals(identity.key)
        if refusals < threshold:
            await self.app(scope, receive, send)
            return

        day_cap, month_cap = caps_for(
            identity.kind,
            day_cap=self._settings.fair_use_day_cap,
            month_cap=self._settings.fair_use_month_cap,
            gateway_day_cap=self._settings.fair_use_gateway_day_cap,
            gateway_month_cap=self._settings.fair_use_gateway_month_cap,
        )
        retry_after = retry_after_seconds(time.time())
        # The plain sentence goes first, ahead of the grep-able structured
        # line, so a caller that only ever prints a log record verbatim still
        # gets the human-readable version and not just `action=hard`.
        logger.info(
            "%s\n%s",
            hard_block_message(retry_after, day_cap),
            hard_log_line(identity.key, refusals, retry_after),
        )
        await self._telemetry.record_hard_block(identity.key)
        await _send_429(
            send,
            retry_after=retry_after,
            body=hard_block_body(retry_after, day_cap, month_cap),
        )


async def _send_429(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    retry_after: int,
    body: dict[str, Any],
) -> None:
    """The whole response, written by hand.

    `Retry-After` is the load-bearing header and is sent as delta-seconds
    (RFC 9110 allows either that or an HTTP-date; every client library parses
    the integer form). `Cache-Control: no-store` matches what vercel.json sets
    on /mcp, so an intermediary cannot pin a 429 in front of a caller whose
    allowance has since rolled over.
    """
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"retry-after", str(retry_after).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def hard_limit_middleware(server: Any) -> Iterable[Any]:
    """`[Middleware(...)]` for `http_app(middleware=...)`, or nothing.

    Returns an empty list when the escalation is off, so a server with
    FAIR_USE_HARD_AFTER=0 has no extra layer in its stack at all rather than
    one that decides to do nothing on every request.
    """
    from starlette.middleware import Middleware

    settings = server.settings_obj
    if not (settings.fair_use_enabled and settings.fair_use_hard_after > 0):
        return []
    return [
        Middleware(
            HardLimitMiddleware,
            settings=settings,
            telemetry=server.telemetry,
            classifier=server.classifier,
        )
    ]
