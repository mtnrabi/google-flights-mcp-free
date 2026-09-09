"""
The anonymous taster cap, answered as HTTP 401 so a client can act on it.

Why a 401 and not the 200 the tool layer would return
-----------------------------------------------------
`fair_use.rate_limited_result` is a 200 whose body says
`search_status: "rate_limited"`, and that is the right answer for a model:
it reads it, tells the user, stops. It is the WRONG answer for the thing
this cap now exists to cause. Since 2026-09-09 the free server's product is
the signed-in endpoint, and an anonymous caller at the cap is one click away
from fifteen times the allowance -- but only if their client is told, in the
one way an MCP client is defined to understand, that signing in is possible:

    401 + WWW-Authenticate: Bearer resource_metadata="..."

That is the signal (RFC 9728, and the MCP authorization spec) that makes
Claude, Claude Code and Cursor show a Sign in button. A 200 with prose in it
makes none of them do anything. So the cap is enforced HERE, in front of the
app, where the status line is still ours to choose -- by the time a tool
function runs, FastMCP's session manager has already committed to a 200 and
in streaming mode has usually begun writing it.

The body is a JSON-RPC error response, because that is what the caller's
transport is expecting to parse, and `error.message` is the complete
directions sentence rather than a code. A script that logs the body verbatim
and understands nothing else still shows a human what to do.

What it deliberately does not do
--------------------------------
* **It never touches a signed-in caller.** Identity comes from the header the
  OAuth gate injects, so `KIND_SIGNED_IN` passes straight through and is
  capped by the tool layer against its own, much larger allowance. A signed-in
  user can never be blocked by another user's usage: their key is their Google
  account and nothing else.
* **It does not read the request body.** Identity comes from headers, the
  query string and the peer address, exactly as `fair_use.identify` takes
  them, so nothing has to be buffered or replayed.
* **It does not count the refusal.** The soft-refusal counter that arms the
  429 escalation (`hard_limit.py`) is written by the tool layer; a request
  refused here never reaches it. That is deliberate: a 401 is already a status
  every HTTP client understands, and escalating from one status code to
  another buys nothing.
* **It leaves `/health` and `/metrics` alone**, like every other gate in this
  server: a monitor must get an answer whoever else is misbehaving.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .fair_use import (
    KIND_DIRECT,
    FairUseState,
    caps_for,
    identify,
    log_line,
    rate_limited_result,
)
from .hard_limit import DEFAULT_MCP_PATH, _headers, _query

logger = logging.getLogger(__name__)

#: JSON-RPC has no registered code for "authenticate and try again", and the
#: implementation-defined range (-32000 to -32099) is where a server puts its
#: own. -32003 is what the MCP ecosystem has settled on for an authorization
#: failure; the status line is the part clients actually key on.
JSONRPC_AUTH_REQUIRED = -32003


class AnonCapMiddleware:
    """Pure-ASGI. Answers 401 to an anonymous caller past the taster cap.

    Constructed with the same settings, telemetry and classifier the server
    itself uses, so the identity computed here and the identity computed
    inside a tool are the same value for the same request -- which is what
    makes the counter this reads the counter the tool wrote.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        settings: Any,
        telemetry: Any,
        classifier: Any,
        challenge: Callable[[], str] | None = None,
        path: str = DEFAULT_MCP_PATH,
    ) -> None:
        self.app = app
        self._settings = settings
        self._telemetry = telemetry
        self._classifier = classifier
        #: Produces the `WWW-Authenticate` value. A callable because the
        #: OAuthSupport that knows the metadata URL is built after the app in
        #: some orders, and because a deployment with no sign-in configured
        #: has no challenge to send -- in which case this gate does nothing
        #: at all and the tool layer's 200 refusal is the only behaviour,
        #: exactly as before.
        self._challenge = challenge
        self._path = path.rstrip("/") or "/"

    @property
    def armed(self) -> bool:
        """False unless there is a cap to enforce AND a sign-in to offer.

        Both halves matter. Without `fair_use_enabled` there is no cap.
        Without a challenge there is nothing for the caller to do about a
        401, and answering one would be a worse experience than the 200 that
        at least explains itself.
        """
        if not getattr(self._settings, "fair_use_enabled", False):
            return False
        if self._challenge is None:
            return False
        return bool(self._challenge())

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
            # No `ensure_openai_ranges()`: it is a blocking network call and
            # this runs on every MCP POST, cold starts included. Whatever the
            # classifier has already loaded is used. A range that has not
            # loaded yet can only make a caller look `direct`, and under the
            # 2026-09-09 defaults `direct` and `gateway_*` share one cap, so
            # the difference is nil unless an operator has set
            # FAIR_USE_GATEWAY_DAY_CAP back up.
            gateway_networks=self._classifier.gateway_networks(
                getattr(self._settings, "fair_use_gateway_cidrs", ())
            ),
            gateway_user_agents=getattr(
                self._settings, "fair_use_gateway_user_agents", ()
            ),
            identity_params=getattr(self._settings, "fair_use_identity_params", ()),
        )

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
            logger.warning("could not identify a caller for the anon cap: %s", exc)
            identity = None

        # ONLY a direct caller is challenged. Everything else falls through
        # to the tool layer, which answers the same refusal as a 200 with
        # `search_status: "rate_limited"` in the body:
        #
        #   * signed in -- not this gate's business, and a much larger
        #     allowance counted against their own account;
        #   * nothing to key on (stdio, a local connection, a test) -- not
        #     counted and never refused, because refusing on "no headers at
        #     all" would refuse every stdio user on the strength of nothing;
        #   * a pooled gateway or a config-blob caller -- Smithery and
        #     friends. A mid-session upstream 401 is undocumented behaviour
        #     on Smithery's gateway and it proxies our server for its users
        #     with no credential of theirs
        #     (state/gtm/research/smithery-oauth-2026-09-09.md), so a 401
        #     there is a broken integration rather than a Sign in button.
        #     They get the in-band refusal, with directions written for
        #     somebody standing behind a gateway.
        if identity is None or identity.kind != KIND_DIRECT:
            await self.app(scope, receive, send)
            return

        day_cap, month_cap = caps_for(
            identity.kind,
            day_cap=self._settings.fair_use_day_cap,
            month_cap=self._settings.fair_use_month_cap,
            gateway_day_cap=self._settings.fair_use_gateway_day_cap,
            gateway_month_cap=self._settings.fair_use_gateway_month_cap,
            anon_day_cap=getattr(self._settings, "anon_day_cap", 0),
            anon_month_cap=getattr(self._settings, "anon_month_cap", 0),
        )
        used_today, used_month = await self._telemetry.fair_use_usage(identity.key)
        state = FairUseState(
            key=identity.key,
            used_today=used_today,
            used_month=used_month,
            day_cap=day_cap,
            month_cap=month_cap,
            kind=identity.kind,
            signed_in_day_cap=self._settings.fair_use_day_cap,
            signed_in_month_cap=self._settings.fair_use_month_cap,
        )
        if not state.blocked:
            await self.app(scope, receive, send)
            return

        logger.info("%s", log_line(state, "anon_challenge"))
        await _send_401(send, state, self._challenge())


def _body(state: FairUseState) -> dict[str, Any]:
    """A JSON-RPC error response whose `message` is the whole instruction.

    `id: null` because the request body was never read -- JSON-RPC 2.0 says
    that is the id to use when it cannot be determined, and reading the body
    to find one would mean buffering every MCP POST on the server for the
    sake of a refusal.

    `data` is the same object the tool layer would have returned as a 200, so
    a client that already knows how to render a `rate_limited` search result
    renders this one identically.
    """
    payload = rate_limited_result(state)
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": JSONRPC_AUTH_REQUIRED,
            "message": payload["message"],
            "data": payload,
        },
    }


async def _send_401(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    state: FairUseState,
    challenge: str,
) -> None:
    """The whole response, written by hand.

    `WWW-Authenticate` is the load-bearing header: it is what turns this from
    "the server said no" into a Sign in button. `Cache-Control: no-store`
    matches what vercel.json sets on /mcp, so an intermediary cannot pin a
    401 in front of a caller whose allowance has since rolled over.
    """
    raw = json.dumps(_body(state)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode("ascii")),
                (b"www-authenticate", challenge.encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw})


def anon_cap_middleware(server: Any) -> Iterable[Any]:
    """`[Middleware(...)]` for `http_app(middleware=...)`, or nothing.

    Empty whenever there is no sign-in configured, so a deployment without
    the Google variables has no extra layer in its stack at all rather than
    one that decides to do nothing on every request.
    """
    from starlette.middleware import Middleware

    settings = server.settings_obj
    oauth = getattr(server, "fp_oauth", None)
    if oauth is None or not settings.fair_use_enabled:
        return []
    return [
        Middleware(
            AnonCapMiddleware,
            settings=settings,
            telemetry=server.telemetry,
            classifier=server.classifier,
            challenge=oauth.challenge_header,
        )
    ]
