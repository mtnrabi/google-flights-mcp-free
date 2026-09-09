"""
Vercel entrypoint.

Vercel's Python runtime auto-discovers `api/index.py` and serves the
top-level `app` as a single Fluid function receiving every path -- so `/mcp`,
`/health` and `/metrics` all land in this one module. No rewrites are needed
in vercel.json.

Why a wrapper at all, and why Starlette rather than FastAPI
-----------------------------------------------------------
FastMCP's streamable-HTTP transport initialises its session manager in an
ASGI *lifespan* startup event. If the host never runs lifespan, every request
to /mcp fails with "Task group is not initialized" -- a total outage of the
MCP endpoint, verified locally by driving the app without lifespan. So the
wrapper that hands `_mcp_app.lifespan` to the host is load-bearing and must
never be removed. `tests/test_entrypoint.py` pins both halves of that.

The wrapper used to be a FastAPI app, because Vercel's lifespan support
(shipped 2025-12-09) is announced specifically for "FastAPI apps" and it was
not worth betting the endpoint on whether that meant any ASGI app. It is
Starlette now, for one reason: importing FastAPI costs ~110 ms of CPU on
every cold start (fastapi.openapi.models alone is ~80 ms of pydantic model
building, and this app serves no OpenAPI schema -- docs_url, redoc_url and
openapi_url were all None). Starlette is already imported by FastMCP, so the
same wrapper costs 0 ms.

That is safe because `class FastAPI(Starlette)` and FastAPI passes `lifespan`
straight through to `starlette.routing.Router`: there is no FastAPI-specific
lifespan mechanism for a host to special-case, so "supports lifespan events
for FastAPI apps" can only mean the ASGI adapter runs the lifespan protocol,
which is class-agnostic. `fastapi` stays in requirements.txt on purpose --
Vercel picks the framework preset from the dependency list, and a project
that resolves to `framework: null` 404s every route.

Verify after deploying: a real POST /mcp `initialize` through the public
alias. A 500 saying "Task group is not initialized" is what a host that
skips lifespan looks like.

Two other deliberate choices:

* `stateless_http=True` -- serverless invocations are short-lived and are not
  guaranteed to land on the same instance, so there is nowhere to keep a
  session. Vercel's own guidance for remote MCP is the stateless
  streamable-HTTP model, and FastMCP notes most clients (Cursor, Claude Code)
  use `fetch()` internally and never forward `Set-Cookie` anyway.

* `json_response` left off -- buffering the whole response as JSON
  re-acquires Vercel's 4.5 MB body cap, and a 15-way flight fan-out is not
  comfortably under it. Streaming responses are exempt from that limit.

The canonical MCP path is `/mcp` with NO trailing slash. `/mcp/` answers via
a 307, but MCP_PUBLIC_URL must be the exact URL clients use, and Lulu hashes
it into Claude's widget domain -- so a stray slash there costs all the CPM.
"""

from __future__ import annotations

import logging
import os
import sys

# Vercel resolves paths against the project root but does not guarantee it is
# on sys.path for a nested entrypoint. Add it explicitly so `src` imports the
# same way locally and deployed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.applications import Starlette  # noqa: E402

from src.anon_gate import anon_cap_middleware  # noqa: E402
from src.hard_limit import hard_limit_middleware  # noqa: E402
from src.oauth import OAuthResourceGate  # noqa: E402
from src.server import build_server  # noqa: E402
from src.settings import load_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("mcp_server.vercel")

_settings = load_settings()
_server = build_server(_settings)

if _settings.ads_enabled:
    try:
        from lulu_ads.widget import claude_apps_domain

        logger.info(
            "sponsored widget domain for %s -> %s",
            _settings.public_url,
            claude_apps_domain(_settings.public_url),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        logger.warning("could not derive widget domain: %s", exc)

# OpenAI's connector egress ranges load lazily on first classification rather
# than here: this module runs on a cold start, where a blocking network call
# would be charged to whichever user happened to arrive first.
# The hard-limit middleware goes in the app's own stack, not around the
# wrapper below, so it sees the post-mount path and can scope itself to POST
# /mcp -- /health and /metrics must answer for a monitor and for us no matter
# who else is looping through refusals. It is an empty list when
# FAIR_USE_HARD_AFTER is 0. See src/hard_limit.py.
# Two gates in the app's own stack, outermost first:
#
#   hard_limit  429 for a caller looping through refusals (unchanged).
#   anon_gate   401 + the OAuth challenge for an anonymous caller past the
#               taster cap. Empty list unless sign-in is configured, so a
#               deployment without the Google variables has neither the
#               layer nor the behaviour.
#
# Both are inside `http_app` rather than around the wrapper below, so they
# see the post-mount path and can scope themselves to POST /mcp -- /health
# and /metrics must answer for a monitor and for us no matter who else is
# looping through refusals.
_mcp_app = _server.http_app(
    stateless_http=True,
    middleware=list(hard_limit_middleware(_server)) + list(anon_cap_middleware(_server)),
)

# Handing over `_mcp_app.lifespan` is the load-bearing line. Without it the
# session manager never starts and /mcp returns 500 on every request.
app = Starlette(lifespan=_mcp_app.lifespan)

# The OAuth gate is MOUNTED rather than wrapped around the Starlette app, so
# `app` stays a Starlette carrying the MCP lifespan -- Vercel's adapter and
# tests/test_entrypoint.py both reach for `app.router.lifespan_context`, and
# an ASGI callable wrapped around it has no such attribute even though it
# forwards the lifespan scope correctly.
#
# The gate wraps EVERYTHING, including the two middlewares above, and
# it is installed unconditionally -- even with no sign-in configured. Two
# jobs, and only the second one depends on the feature being on:
#
#  1. Strip `x-fp-oauth-*` from every inbound request, whatever the path. The
#     tool layer counts a signed-in caller's fair use against the identity in
#     that header and gives it the full allowance, so a caller able to set it
#     himself would help himself to that allowance. Stripping it above
#     everything is what makes the injected value trustworthy, and it must
#     not depend on a flag because the danger does not.
#  2. Resolve a bearer token into that injected identity, and answer 401 +
#     the OAuth challenge to a request that carries none. Since 2026-09-09
#     that applies to `/mcp` as well as to the `/mcp/oauth` alias: every
#     served call on the free server belongs to a Google account.
#     `FREE_ANON_MODE=open` is the documented rollback.
app.mount(
    "/",
    OAuthResourceGate(_mcp_app, getattr(_server, "fp_oauth", None)),
)
