"""
The landing page: what a PERSON sees when they open the server's URL.

Why this file exists
--------------------
Every Reddit post and reply carries `https://free-trial.flightpowers.com/mcp`.
On 2026-09-28 Matan clicked it from one of them: `/` answered Starlette's
plain-text 404 and `/mcp` answered the 401 JSON that an MCP client turns
into a Sign in button and a browser turns into nothing. The first click of
every reader landed on an error.

What it serves, and to whom
---------------------------
* `GET /` and `HEAD /`: the page, to anyone. Nothing has ever lived there.
* `GET /privacy` and `HEAD /privacy`: the privacy page (src/privacy.py), to
  anyone. It answered 404 until 2026-10-22.
* `GET /mcp`, `/mcp/` and `/mcp/oauth`: the page, ONLY to a browser
  navigation (`is_browser_navigation` below). Everything else on those
  paths -- an MCP client's POST or SSE GET, OAuth discovery, Smithery's
  release probe, Glama's hourly check, a curl -- passes through this layer
  untouched. The `401 + WWW-Authenticate` on a credential-less call is how
  clients find the sign-in, and tests/test_landing.py pins it byte for byte.

A browser navigation is: GET or HEAD, `Accept` names `text/html`, and the
request carries nothing an MCP client would send -- no `text/event-stream`
or `application/json` in `Accept`, no `Mcp-Session-Id`, no
`Mcp-Protocol-Version`, no `Authorization`. The streamable-HTTP transport
requires `Accept: text/event-stream` on its GET and
`application/json, text/event-stream` on its POST, so a client can never
match; curl sends `*/*` and keeps the 401 with the directions in its body.

The connector URL printed in the copy box is the one for the host the
request arrived on (`settings.public_url_for_host`): the canonical hostname
for the canonical host, an alias for an alias -- with a line pointing the
alias reader at the canonical one, since every sentence a person reads is
meant to teach the new hostname (tests/test_hostnames.py) -- and the
canonical URL for a host we do not publish (a preview deployment, a
hostname somebody pointed at us). A preview must never advertise itself.

Where it sits
-------------
Outside `OAuthResourceGate`, in api/index.py, so the module that decides who
is served is not touched. No JavaScript, no tracking, nothing fetched: the
page is a string, rendered per request from the host and the settings.
"""

from __future__ import annotations

from typing import Any

from .pages import CONTACT_EMAIL, PRIVACY_URL, TERMS_URL, e, page
from .privacy import privacy_html

#: Paths a browser navigation is answered with the page. `/` for any GET;
#: the other two only when the request looks like a browser.
BROWSER_PATHS = frozenset({"/mcp", "/mcp/oauth"})
ROOT_PATH = "/"
#: The privacy page (src/privacy.py), to anyone, like `/`.
PRIVACY_PATH = "/privacy"

TITLE = "FlightPowers free MCP"

#: Facts only, each one traceable to the server's own instructions or the
#: listings. No metric appears here that is not already public.
SITE_URL = "https://flightpowers.com"
PAID_FLIGHTS_LISTING = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
PAID_HOTELS_LISTING = "https://rapidapi.com/mtnrabi/api/booking-live-api"
PAID_FLIGHTS_MCP = "https://flights.flightpowers.com/mcp"
PAID_HOTELS_MCP = "https://hotels.flightpowers.com/mcp"

#: Headers an MCP client sends and a browser navigation never does.
_CLIENT_HEADERS = ("mcp-session-id", "mcp-protocol-version", "authorization")

#: The copy box is a readonly textarea, not an input: a textarea soft-wraps,
#: so the whole URL is visible on a phone (an input clips it at the edge and
#: only scrolls), and a soft wrap adds no newline to what gets copied.
_EXTRA_STYLE = """
.url { display: block; width: 100%; box-sizing: border-box; margin: .5rem 0;
       padding: .7rem .8rem; font: 1rem/1.4 ui-monospace, SFMono-Regular, Menlo,
       monospace; color: inherit; background: rgba(128,128,128,.12);
       border: 1px solid rgba(128,128,128,.45); border-radius: 6px;
       resize: none; overflow-wrap: anywhere; }
ol { padding-left: 1.3rem; }
"""


def _headers(scope: dict) -> dict[str, str]:
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in (scope.get("headers") or [])
    }


def is_browser_navigation(scope: dict) -> bool:
    """Whether this request is a person's browser opening a URL.

    Strict on purpose: the cost of a false positive is an MCP client that
    was owed a 401 getting an HTML page instead, and there is no client
    behaviour that sends `text/html` without also naming the transport's own
    media types or carrying its own headers.
    """
    if scope.get("type") != "http":
        return False
    if (scope.get("method") or "").upper() not in ("GET", "HEAD"):
        return False
    headers = _headers(scope)
    accept = headers.get("accept", "").lower()
    if "text/html" not in accept:
        return False
    if "text/event-stream" in accept or "application/json" in accept:
        return False
    return not any(name in headers for name in _CLIENT_HEADERS)


def landing_html(connector_url: str, *, canonical_url: str | None = None) -> str:
    """The page, for one connector URL.

    `canonical_url` is the URL every new setup should use; when it differs
    from `connector_url` the reader arrived on an alias and gets one line
    saying so.
    """
    alias_note = ""
    if canonical_url and canonical_url != connector_url:
        alias_note = (
            '<p class="note">This is the older hostname and it keeps working, '
            f"same server and same account. New setups use "
            f"<code>{e(canonical_url)}</code>.</p>"
        )
    cursor_json = (
        '{"mcpServers": {"flightpowers-free": {"url": "'
        f"{connector_url}"
        '"}}}'
    )
    body = (
        f"<h1>{e(TITLE)}</h1>"
        "<p>Live Google Flights fares and Booking.com hotel rates for AI "
        "agents. Free, with one sponsored card on each result. Sign in with "
        "Google and you get 50 searches a day, 250 a month. No key, no card.</p>"
        "<p>Four tools: one-way and round-trip fares across a date range and a "
        "list of destinations (a round trip is one request, both legs priced "
        "together), hotel search by destination, and a rate by hotel name.</p>"
        "<h2>Your connector URL</h2>"
        f'<textarea class="url" readonly rows="2" wrap="soft" spellcheck="false" '
        f'aria-label="MCP connector URL">{e(connector_url)}</textarea>'
        '<p class="note">Copy it as is: the path is /mcp, no trailing slash.</p>'
        f"{alias_note}"
        "<h2>Claude (claude.ai or the desktop app)</h2>"
        "<ol>"
        "<li>Open Settings, then Connectors.</li>"
        "<li>Click Add custom connector and paste the URL above.</li>"
        "<li>Click Sign in with Google when Claude asks. That is the whole "
        "setup.</li>"
        "</ol>"
        "<h2>Other clients</h2>"
        '<p class="step"><strong>Claude Code:</strong> '
        f"<code>claude mcp add --transport http flightpowers-free {e(connector_url)}</code></p>"
        '<p class="step"><strong>Cursor:</strong> add it as a remote MCP server in '
        f"your <code>mcp.json</code>: <code>{e(cursor_json)}</code></p>"
        '<p class="step"><strong>Any MCP client:</strong> add the URL as a remote '
        "(streamable HTTP) MCP server. Your client shows the Sign in button on "
        "its own.</p>"
        "<h2>When you want the big scans</h2>"
        "<p>Free to try, no card: sign in with Google and you get 50 searches a "
        "day. There's a $10 key if you want the big scans (2,500 searches, "
        "about nine month-wide ones), but the free one is enough to see if it "
        "works for you.</p>"
        "<p>The key is a RapidAPI plan: "
        f'<a href="{e(PAID_FLIGHTS_LISTING)}">Google Flights Live API</a> for '
        f'flights, <a href="{e(PAID_HOTELS_LISTING)}">Booking Live API</a> for '
        "hotels. With the key the same searches run ad-free on the paid "
        f"servers, <code>{e(PAID_FLIGHTS_MCP)}</code> and "
        f"<code>{e(PAID_HOTELS_MCP)}</code>, or as plain HTTP calls.</p>"
        f'<p class="note"><a href="{e(SITE_URL)}">flightpowers.com</a> &middot; '
        f"Questions: {e(CONTACT_EMAIL)} &middot; "
        f'<a href="{e(PRIVACY_URL)}">Privacy</a> &middot; '
        f'<a href="{e(TERMS_URL)}">Terms</a></p>'
    )
    return page(TITLE, body, extra_style=_EXTRA_STYLE)


class LandingPage:
    """ASGI layer: the page for people, everything else passed through as is.

    `settings` is anything with `public_url` and `public_url_for_host`
    (src/settings.py). The wrapped app is whatever answers every other
    request; on this deployment that is `OAuthResourceGate`.
    """

    def __init__(self, app: Any, settings: Any) -> None:
        self.app = app
        self.settings = settings

    def wants_page(self, scope: dict) -> bool:
        if scope.get("type") != "http":
            return False
        path = (scope.get("path") or "").rstrip("/") or ROOT_PATH
        if path in (ROOT_PATH, PRIVACY_PATH):
            return (scope.get("method") or "").upper() in ("GET", "HEAD")
        return path in BROWSER_PATHS and is_browser_navigation(scope)

    async def __call__(self, scope, receive, send) -> None:
        if not self.wants_page(scope):
            await self.app(scope, receive, send)
            return
        path = (scope.get("path") or "").rstrip("/") or ROOT_PATH
        if path == PRIVACY_PATH:
            body = privacy_html().encode("utf-8")
        else:
            host = _headers(scope).get("host", "")
            connector_url = self.settings.public_url_for_host(host)
            body = landing_html(
                connector_url, canonical_url=self.settings.public_url
            ).encode("utf-8")
        headers = [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            # The same path answers JSON to a client and HTML to a person,
            # so a cache in between must key on Accept. /mcp is no-store at
            # the edge already (vercel.json); `/` gets the same.
            (b"cache-control", b"no-store"),
            (b"vary", b"Accept"),
            (b"x-content-type-options", b"nosniff"),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        if (scope.get("method") or "").upper() == "HEAD":
            await send({"type": "http.response.body", "body": b""})
            return
        await send({"type": "http.response.body", "body": body})
