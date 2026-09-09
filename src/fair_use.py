"""
Per-client fair use on the free server, and the upgrade path that goes with it.

Why this exists
---------------
On 2026-09-05 one client accounted for 3,270 of the 4,078 backend (Lambda)
calls this server made in 30 hours: a scheduled job starting at 04:00Z every
morning, one stateless HTTP request per search, expanding date ranges and
destination lists into roughly ten backend calls per tool call
(``state/gtm/free-mcp-batch-client-2026-09-05.md``). That is about $90 a month
of proxy bandwidth for one caller who had never been shown that a paid server
exists. The daily spend budget could not help: it is a total, so the first
caller of the day spends it and everyone else gets a degraded server.

So the limit is per client and it counts BACKEND calls, not tool calls. A tool
call is the unit the caller sees; a backend call is the unit that costs money,
and one tool call with a two-week date range and three destinations is 42 of
them. Counting tool calls would leave the exact behaviour that caused this
uncapped.

The numbers, and why they are these numbers
-------------------------------------------
150 backend calls per UTC day and 2,000 per calendar month, decided by mrabi
on 2026-09-05. The month cap is the one doing the real work: 150 a day for a
whole month would be 4,500, and RapidAPI PRO is 2,500 requests for $10. Free
must not be a better deal than the cheapest paid plan, so the month cap sits
below it. Enforced from day one rather than after a monitor period, because
the traffic that prompted it is already running daily.

The client key
--------------
sha256 of the forwarded client IP plus the user agent, truncated to 12 hex.
Deliberately NOT ``policy.client_fingerprint``: that one also mixes in
``mcp-session-id``, and the client this cap exists for opens a new session for
every single request (75 "Terminating session" lines in two minutes), so a
counter keyed on it would reset before it ever counted to two. The two digests
are different values for the same caller and both are one-way; the fingerprint
stays the reporting key on /metrics so nothing that reads it has to change.

Weak, and that is understood. A caller who rotates IPs or user agents gets a
fresh allowance. This is a fair-use limit on a free service, not an
authentication mechanism, and the honest ceiling on spend is still
DAILY_BACKEND_CALL_BUDGET.

The gateway problem
-------------------
That key has one failure mode that is worse than being weak, and it is the
opposite failure: it can be too STRONG. A remote gateway terminates the
end user's connection and opens its own to us, so every user behind it
arrives with the same forwarded IP and the same user agent -- one key, one
150-a-day allowance, shared. Claude.ai's connector fetcher egresses from
160.79.104.0/21 for every Claude user on earth; Smithery's gateway proxies
every install of this server through its own infrastructure. Under the
2026-09-05 key, a handful of real chat users behind one gateway would have
started refusing each other, while the caller the cap was written for -- a
direct script with its own IP -- is capped exactly as intended. A free
server that works for a script and fails for Claude users is backwards.

So the key is chosen per request, and which way it was chosen is recorded:

  config          The request carries a per-user configuration value.
                  Smithery injects the user's saved configuration on every
                  request: historically as `?config=<base64 JSON>` (which is
                  why mcp_server_paid/src/credentials.py has a "generic names
                  last" rule at all), and in the current docs as pass-through
                  query parameters and headers named by the server's own
                  configuration schema -- "Smithery Gateway passes through all
                  query parameters and headers to your upstream server"
                  (smithery.ai/docs/build/session-config, read 2026-09-05).
                  Both spellings are accepted, and FAIR_USE_IDENTITY_PARAMS
                  names any extra per-user parameter a gateway is seen to send.
                  Different users have different values, so the value
                  separates them: key = IP + user agent + value digest, at the
                  normal per-client caps -- the point of telling users apart is
                  that each of them can then be held to the ordinary allowance.

                  This free server declares no configuration schema and takes
                  no key, so in practice almost nothing lands here today. It
                  exists because it is the ONLY signal that survives a
                  gateway: a value the user themselves supplied. Every other
                  signal -- IP, user agent, session id -- identifies the
                  vendor, and Anthropic and OpenAI both say so in their own
                  docs.

  gateway_session The request came from a KNOWN gateway/LLM-host egress
                  range and carries an `mcp-session-id`. The session is the
                  closest thing to a per-user handle a pooled connection
                  offers, so it keys the counter -- with the higher gateway
                  caps, because a session is much shorter-lived than a day
                  and several sessions belong to one person.

                  Read the guarantee narrowly. Under the MCP Streamable HTTP
                  spec the SERVER mints this id, and behind a proxy the
                  server's client is the gateway, so the id belongs to the
                  gateway's connection, not to a person. It separates users
                  only insofar as the gateway opens one upstream connection
                  per user connection, which no gateway documents. It is a
                  better guess than one shared counter, and it is a guess.

  gateway_pooled  Known gateway egress, no session id: nothing in the
                  request separates one user from another and we say so
                  rather than pretending. One shared counter for everyone
                  behind that gateway, at the higher gateway caps.

  direct          Everything else, including the batch client. Unchanged
                  from 2026-09-05: IP + user agent, 150/day, 2,000/month.

The same narrowness cuts the other way: because a caller can start a new
session whenever it likes, `gateway_session` is escapable by re-initialising.
That is accepted here rather than papered over. Escaping it requires already
being inside a published LLM-host egress range -- so the caller is a real
Claude or ChatGPT user, not a script on a VPS -- and the ceiling that does not
care about any of this is DAILY_BACKEND_CALL_BUDGET, which is the control to
reach for if pooled traffic ever becomes the top line of the bill.

Why an abuser cannot simply claim to be a gateway: the egress test is run
against addresses OUR edge sets -- the rightmost X-Forwarded-For entry,
X-Real-IP, and the peer address -- never the leftmost X-Forwarded-For entry,
which is whatever the caller typed. A caller who sends
`x-forwarded-for: 160.79.104.1` gets their own address appended after it by
the edge, so the leftmost entry is theirs to forge and the rightmost is not.
That is also why the counting key still uses the leftmost entry: for counting
we want the value that varies per end user, and forging it only ever splits a
caller's own allowance into smaller pieces.

When the refusal itself is ignored
----------------------------------
The refusal above is a 200 with a structured body, which is the right shape
for a model and worth nothing to a script that never reads one. On 2026-09-06
the batch client proved it: refused at 150 backend calls, it made 486 more
tool calls in the next 24 minutes. So a client that collects
FAIR_USE_HARD_AFTER refusals inside a rolling hour stops getting a 200 at all
and gets HTTP 429 with Retry-After instead, from ASGI middleware in front of
the MCP app (``hard_limit.py``). First hit stays soft; only a loop earns the
status code. Pooled gateway keys are exempt -- see ``stores.bump``, where that
exemption is enforced at the write site.

The upsell is the point
-----------------------
Being refused is only half of it. The result of hitting the cap has to teach
the model, and through it the user, that there is a paid server and exactly
how to get onto it: subscribe on RapidAPI, pass the key. The same instructions
ride in the server ``instructions`` string and in every tool description, so a
model knows about the upgrade before it ever hits the cap rather than
discovering it inside an error.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

#: What goes into the fair-use client key, in this order. No session id: see
#: the module docstring. The leftmost X-Forwarded-For entry is the original
#: client; the rest are proxy hops.
CLIENT_KEY_HEADERS = ("x-forwarded-for", "user-agent")

#: How the key for one request was chosen. Recorded on every `[fair_use]`
#: line and counted per day on /metrics/calls, because "the cap fired 40
#: times" means two completely different things depending on which of these
#: it fired against.
#: A caller who signed in with Google. The key is the account and nothing
#: else -- no address, no user agent, no session -- so the allowance follows
#: the person across machines, across gateways and across a changing IP, and
#: two people behind one proxy stop spending each other's. This is the whole
#: reason sign-in exists on a free server (Matan, 2026-09-09).
KIND_SIGNED_IN = "signed_in"
KIND_CONFIG = "config"
KIND_GATEWAY_SESSION = "gateway_session"
KIND_GATEWAY_POOLED = "gateway_pooled"
KIND_DIRECT = "direct"

#: The kinds that get the higher, pooled caps.
GATEWAY_KINDS = frozenset({KIND_GATEWAY_SESSION, KIND_GATEWAY_POOLED})

ALL_KINDS = (
    KIND_SIGNED_IN,
    KIND_CONFIG,
    KIND_GATEWAY_SESSION,
    KIND_GATEWAY_POOLED,
    KIND_DIRECT,
)

#: Query parameter carrying a gateway-injected, per-user configuration blob.
#: Smithery's, specifically -- `?config=<base64 JSON>` -- which is the one
#: gateway convention this codebase has first-hand evidence for
#: (mcp_server_paid/src/credentials.py). Dot-notation `?config.<field>=` is
#: the same thing spelled out, so both are accepted.
CONFIG_QUERY_NAME = "config"
CONFIG_QUERY_PREFIX = "config."

#: Query parameters that look per-user but are NOT, and must never separate
#: one caller from another: `api_key` on a gateway URL is the GATEWAY's key,
#: identical for every user behind it (rule 6, and the bug that shipped on
#: 2026-08-17). Listed so the exclusion is explicit rather than implied by
#: absence.
NEVER_IDENTIFYING_PARAMS = ("api_key", "apikey", "key")

#: The header the OAuth gate injects after an access token validates, and
#: strips from every inbound request first. Read here rather than plumbed
#: through, because it arrives exactly the way every other identity signal in
#: this module does -- and because the strip in `oauth.strip_identity_headers`
#: is what makes reading it safe. If that strip is ever removed, the cap below
#: becomes something a caller can hand themselves.
SIGNED_IN_HEADER = "x-fp-oauth-subject"

#: Headers whose value our own edge sets and a caller cannot choose. Only
#: these decide "is this a known gateway": the leftmost X-Forwarded-For entry
#: is caller-supplied text and would let anyone claim the higher caps.
TRUSTED_ADDRESS_HEADERS = ("x-real-ip",)

#: Hex characters kept, matching the reporting fingerprint's width so the two
#: are visibly the same kind of thing (and visibly not the same value).
CLIENT_KEY_LENGTH = 12

#: Longest Retry-After this server will ever ask for, in seconds. An hour.
#:
#: The honest answer is often longer -- a client that spends its day cap at
#: 05:00 UTC really does have nineteen hours to wait -- but a Retry-After of
#: 68,400 is a number a scheduler may well round up into "never come back",
#: and the day cap is not the only reason a block lifts (an operator raising
#: FAIR_USE_DAY_CAP, or FAIR_USE_ENABLED going to 0, both take effect
#: immediately). An hour is long enough to stop a 20-a-minute loop dead and
#: short enough that a caller who was refused for a reason that has since
#: gone away finds out.
MAX_RETRY_AFTER_SECONDS = 3600

#: Fraction of a cap at which a result starts carrying the `fair_use` object.
#: A warning is only useful if it arrives while there is still room to act on
#: it, which is the whole argument for warning below the cap rather than at it.
WARN_AT = 0.8

# ── the paid path ────────────────────────────────────────────────────────
# Every URL and every price below is checked against something live:
# the two MCP endpoints are the ones in CLAUDE.md's asset table, and the plan
# names and prices come from the RapidAPI listings' own billingPlans payload
# (state/gtm/PASTE-long-descriptions-2026-09-04.md, verified 2026-09-04):
# flights BASIC $0 / 10 requests a month, PRO $10 / 2,500; hotels BASIC $0 /
# 10, PRO $10 / 2,000. Nothing here is estimated or rounded.

# The keyed endpoints. `flights.flightpowers.com` and
# `google-flights-mcp.flightpowers.com` are the same deployment (#415, host
# header routing); we publish the short one because it is the pair a reader
# has to hold in their head next to `hotels.flightpowers.com`.
PAID_FLIGHTS_URL = "https://flights.flightpowers.com/mcp"
PAID_HOTELS_URL = "https://hotels.flightpowers.com/mcp"

# The sign-in endpoints (2026-09-08). Same servers, same tools; the client
# shows a Sign in button, the user signs in with Google, and the RapidAPI key
# is pasted once on the /connect page instead of into a client config file.
# This is the first path we name now: a header is the thing most people get
# wrong, and several MCP clients have no way to set one at all.
SIGNIN_FLIGHTS_URL = "https://flights.flightpowers.com/mcp/oauth"
SIGNIN_HOTELS_URL = "https://hotels.flightpowers.com/mcp/oauth"

# The free server's own two URLs. `/mcp` is the one we market (Matan,
# 2026-09-09): it serves anonymous callers under the taster cap and offers
# sign-in through the RFC 9728 metadata, so a client set to "authorization
# required" signs the user in up front and every other client keeps working.
# `/mcp/oauth` is the same endpoint with sign-in demanded on request one, for
# clients that fix the auth mode when a server is ADDED and cannot act on a
# 401 that arrives later (ChatGPT connectors, Claude's connector UI saved
# with auth "None").
FREE_MCP_URL = "https://google-flights-lulu.flightpowers.com/mcp"
FREE_SIGNIN_URL = "https://google-flights-lulu.flightpowers.com/mcp/oauth"

FLIGHTS_LISTING_URL = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
HOTELS_LISTING_URL = "https://rapidapi.com/mtnrabi/api/booking-live-api"
DOCS_URL = (
    "https://flightpowers.com/"
    "?utm_source=free-mcp&utm_medium=cap&utm_campaign=upgrade"
)

#: Same landing page as DOCS_URL, `utm_medium=429` instead of `cap`, so a
#: click from the plain-sentence hard-block message is told apart from a
#: click from the structured soft-refusal `upgrade` object in analytics.
DOCS_URL_HARD = (
    "https://flightpowers.com/"
    "?utm_source=free-mcp&utm_medium=429&utm_campaign=upgrade"
)


def signin_directions() -> str:
    """The whole instruction for a caller that has not signed in.

    This is the text on the 401 that `/mcp` answers to a credential-less
    request, and it is the only thing many callers will ever read from us:
    an MCP client turns the status and the challenge header into a Sign in
    button and never shows this, while a script sees only this. So it names
    both audiences and gives each one an action, in one line, no em dash,
    URLs bare so they survive being pasted into a log.
    """
    return (
        "Sign in with Google: add "
        f"{FREE_MCP_URL} in your client and click Sign in; no key needed. "
        f"Scripts: use your RapidAPI key on the paid server {PAID_FLIGHTS_URL}"
    )


def anon_directions(anon_day_cap: int, signed_in_day_cap: int,
                    signed_in_month_cap: int = 0, *, pooled: bool = False) -> str:
    """What an anonymous caller at the taster cap must actually DO.

    Written to be complete on its own, because it is the text a script prints
    verbatim into a log and the text a model reads out to a person. It names
    the number that stopped the call, the one action that lifts it, the URL
    to use if the client cannot do that action, and the paid path -- in that
    order, because that is the order of increasing effort for the user.
    """
    allowance = f"{signed_in_day_cap:,} a day"
    if signed_in_month_cap:
        allowance += f" and {signed_in_month_cap:,} a month"
    if pooled:
        # A pooled caller is behind a gateway that opened this connection on
        # their behalf -- Smithery, a hosted assistant -- so "your client
        # should offer a Sign in button" is advice about a client we are not
        # talking to. The action that works from where they are standing is
        # to add our server directly, or to bring a key to the paid one.
        return (
            f"This shared connection has reached its daily allowance of "
            f"{anon_day_cap:,} searches. To get your own {allowance}, add "
            f"{FREE_MCP_URL} to your assistant directly and sign in with "
            f"Google -- no key, no card. Or use your own RapidAPI key on the "
            f"paid, ad-free server: {PAID_FLIGHTS_URL} for flights, "
            f"{PAID_HOTELS_URL} for hotels."
        )
    return (
        f"Free searches without signing in are limited to {anon_day_cap:,} a "
        f"day. Sign in with Google -- no key, no card, nothing to paste -- for "
        f"{allowance} counted against your own account: your MCP client should "
        f"offer a Sign in button for this server, and if it does not, add "
        f"{FREE_SIGNIN_URL} instead, which asks for the sign-in up front. Or "
        f"use your own RapidAPI key on the paid, ad-free server: "
        f"{PAID_FLIGHTS_URL} for flights, {PAID_HOTELS_URL} for hotels."
    )


def upgrade_steps() -> list[str]:
    """The three steps, written for a model to follow or read out.

    Step 2 lists only key-passing methods the paid server actually implements
    and its README documents (mcp_server_paid/README.md, "Three ways to pass
    your key"). There is no Claude Desktop JSON block or Smithery one-liner
    here because the repo documents neither, and inventing a config snippet
    that has never been run is how a user ends up debugging our example.
    """
    return [
        (
            "1. Get a RapidAPI key: subscribe at "
            f"{FLIGHTS_LISTING_URL} for flights, or "
            f"{HOTELS_LISTING_URL} for hotels. BASIC is free and includes 10 "
            "requests a month; PRO is $10 a month (2,500 requests on flights, "
            "2,000 on hotels)."
        ),
        (
            "2. Point your MCP client at the paid server. Sign in, nothing to "
            f"paste into your client: {SIGNIN_FLIGHTS_URL} for flights, "
            f"{SIGNIN_HOTELS_URL} for hotels. The client shows a Sign in "
            "button, you sign in with Google, and you paste the key once on "
            "the page it opens. Or bring your own key: "
            f"{PAID_FLIGHTS_URL} for flights, {PAID_HOTELS_URL} for hotels, "
            "passed any one of three ways, first non-empty wins: an "
            "`x-rapidapi-key` header (preferred), `?rapidapi_key=YOUR_KEY` on "
            "the server URL for hosts that only accept a URL, or your client's "
            "own API key field (Smithery's saved config field `rapidApiKey` is "
            "accepted)."
        ),
        (
            "3. Nothing else changes: same tools, same fields, same request "
            "shape, and no ads. Your RapidAPI plan becomes the only limit, "
            "and every call reports what it spent and what is left of it."
        ),
    ]


def upgrade_block(
    day_cap: int,
    month_cap: int,
    *,
    signed_in: bool = False,
    signed_in_day_cap: int = 0,
    signed_in_month_cap: int = 0,
    pooled: bool = False,
) -> dict[str, Any]:
    """The `upgrade` object attached to a rate-limited result, and to a
    warned one -- see `fair_use_note`.

    Two shapes, and which one a caller gets is the whole point of the
    2026-09-09 change:

    * **anonymous** gets `sign_in_free` FIRST, because the cheapest thing
      that caller can do is not pay us anything: sign in and get fifteen
      times the allowance. Putting a RapidAPI subscription in front of that
      would be asking for money to solve a problem a button solves.
    * **signed in** gets the paid path only. They have already done the free
      step; repeating it would read as a server that has not noticed.
    """
    why = (
        f"Free fair-use reached: {day_cap:,} searches a day"
        + (f" and {month_cap:,} a month" if month_cap else "")
        + (" for this account" if signed_in else " for callers who are not signed in")
        + ". One search counts once per date and destination combination, so "
        "a wide call spends several. The daily count resets at 00:00 UTC."
    )
    block: dict[str, Any] = {"why": why}
    if not signed_in:
        block["sign_in_free"] = (
            "Sign in with Google to keep your searches and your daily "
            f"allowance under your own name: {FREE_SIGNIN_URL}"
        )
        block["sign_in_free_url"] = FREE_SIGNIN_URL
        block["do_this_first"] = anon_directions(
            day_cap, signed_in_day_cap or day_cap, signed_in_month_cap,
            pooled=pooled,
        )
    block.update(
        {
            "sign_in_flights": SIGNIN_FLIGHTS_URL,
            "sign_in_hotels": SIGNIN_HOTELS_URL,
            "paid_server": PAID_FLIGHTS_URL,
            "hotels_server": PAID_HOTELS_URL,
            "how": upgrade_steps(),
            "docs": DOCS_URL,
        }
    )
    return block


def upgrade_tail(
    day_cap: int, month_cap: int, anon_day_cap: int = 0
) -> str:
    """The short version, for the server instructions and tool descriptions.

    A model that only learns about the allowance from a refusal has already
    failed one of the user's requests. This says it up front, in the two
    places every client reads before calling anything, and it tells the model
    when to bring it up: on a `fair_use` field, not on a plain result, and
    not more than once.

    Since 2026-09-09 the free server requires a Google sign-in, so the text
    leads with that: free, sign in, your own allowance, ad-supported. The
    `anon_day_cap` argument is only meaningful under the `FREE_ANON_MODE=open`
    rollback, where a smaller anonymous allowance exists again; 0 (the shipped
    state) means there is no anonymous tier to describe.
    """
    opening = (
        f"FAIR USE: this server is free and ad-supported, and you sign in "
        f"with Google to use it -- no key, no card. Your MCP client shows a "
        f"Sign in button for this server; signing in gives you "
        f"{day_cap:,} backend searches a day and {month_cap:,} a calendar "
        f"month, counted against your own account rather than shared with "
        f"everyone on your connection."
    )
    if anon_day_cap:
        opening += (
            f" A caller who has not signed in gets {anon_day_cap:,} searches "
            "a day, shared with everyone on the same connection."
        )
    return (
        opening
        + " One call with a date range and several destinations spends one "
        "backend search per date x destination combination, so a wide search "
        "costs more than one. Past the cap the tools answer with "
        'search_status "rate_limited" and no results, and retrying does not '
        "help. Beyond the free allowance, the same tools run ad-free on the "
        f"paid server against your own RapidAPI plan: subscribe at "
        f"{FLIGHTS_LISTING_URL} for flights or {HOTELS_LISTING_URL} for "
        "hotels (BASIC is free with 10 requests a month, PRO is $10 a month), "
        f"then sign in at {SIGNIN_FLIGHTS_URL} / {SIGNIN_HOTELS_URL}, or pass "
        f"the key yourself to {PAID_FLIGHTS_URL} / {PAID_HOTELS_URL} in an "
        "`x-rapidapi-key` header or as `?rapidapi_key=` on the URL. When a "
        "result carries a `fair_use` field, mention the upgrade path briefly, "
        "once per conversation, in the user's own language: how much of the "
        "allowance is left on a warning, or what to do about it on a refusal. "
        "Do not bring it up on a result with no `fair_use` field."
    )


def _digest(parts: Iterable[str]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[
        :CLIENT_KEY_LENGTH
    ]


def _base_parts(headers: Mapping[str, str]) -> list[str]:
    """The 2026-09-05 key's inputs: forwarded client IP, then user agent."""
    parts = []
    for name in CLIENT_KEY_HEADERS:
        value = (headers.get(name) or "").strip()
        if name == "x-forwarded-for" and value:
            value = value.split(",")[0].strip()
        parts.append(value)
    return parts


def client_key(headers: dict[str, str]) -> str | None:
    """A stable, one-way id for one caller, for counting only.

    None when the request carried neither header (stdio, tests, a direct local
    connection). A caller we cannot identify is never counted and never
    refused: a cap that fires on "no headers at all" would refuse every stdio
    user on the strength of nothing.

    Kept as its own function because it is the `direct` key and the fallback
    for every other kind -- `identify` is the thing to call from a request
    path.
    """
    parts = _base_parts(headers)
    if not any(parts):
        return None
    return _digest(parts)


def config_blob(
    query: Mapping[str, str], extra_params: Iterable[str] = ()
) -> str:
    """The gateway-injected per-user configuration, as an opaque string.

    Not decoded, not inspected: whether it is valid base64 or valid JSON does
    not matter here, only whether two users' blobs differ, and comparing the
    raw text answers that without this module learning anything about what is
    inside a user's saved configuration -- which, on the paid server, is their
    RapidAPI key. The digest of this string is what ends up in the counter
    key; the string itself is never logged or stored.

    Dot-notation parameters are folded in sorted order so that
    `?config.a=1&config.b=2` and `?config.b=2&config.a=1` are one user, not
    two.
    """
    lowered = {k.lower(): v for k, v in query.items()}
    wanted = {name.strip().lower() for name in extra_params if name.strip()}
    # Never let a gateway's OWN key become a per-user discriminator: it is the
    # same value for everyone behind it, so keying on it would hand one shared
    # counter the ordinary caps instead of the pooled ones. Rule 6.
    wanted -= set(NEVER_IDENTIFYING_PARAMS)
    blob = (lowered.get(CONFIG_QUERY_NAME) or "").strip()
    dotted = sorted(
        (name, value)
        for name, value in lowered.items()
        if (name.startswith(CONFIG_QUERY_PREFIX) or name in wanted)
        and (value or "").strip()
    )
    if dotted:
        blob = "&".join(f"{name}={value}" for name, value in dotted) + (
            f"&{blob}" if blob else ""
        )
    return blob


#: Blobs that carry no user-distinguishing information. A server that declares
#: no configuration schema -- which this free one does not -- can still be
#: called with an empty or default blob by a gateway that always appends the
#: parameter, and treating "everyone sent {}" as "everyone is a different
#: user" would silently disable the cap for all gateway traffic.
TRIVIAL_CONFIG_BLOBS = frozenset(
    {"", "{}", "e30", "e30=", "eyJ9", "null", "e30%3D"}
)


def _is_identifying_config(blob: str) -> bool:
    return blob.strip().strip('"').lower() not in TRIVIAL_CONFIG_BLOBS


def gateway_egress(
    addresses: Iterable[str | None],
    networks: Iterable[Any],
) -> bool:
    """True when any trusted address for this request sits in a known range.

    `addresses` must be values our own edge sets -- see
    TRUSTED_ADDRESS_HEADERS and the module docstring. Handing the leftmost
    X-Forwarded-For entry to this function would let a caller ask for the
    higher caps by typing a header.
    """
    nets = list(networks)
    if not nets:
        return False
    for raw in addresses:
        if not raw:
            continue
        try:
            address = ipaddress.ip_address(raw.strip())
        except ValueError:
            continue
        if any(address in net for net in nets):
            return True
    return False


def trusted_addresses(
    headers: Mapping[str, str], peer: str | None
) -> list[str]:
    """Addresses for this request that the caller could not have chosen.

    The RIGHTMOST X-Forwarded-For entry is the hop our edge saw and appended;
    the leftmost is the caller's own text. X-Real-IP and the socket peer are
    set by the edge too. Everything here is safe to gate on; the leftmost
    entry is deliberately absent.
    """
    found: list[str] = []
    forwarded = (headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            found.append(last)
    for name in TRUSTED_ADDRESS_HEADERS:
        value = (headers.get(name) or "").strip()
        if value:
            found.append(value)
    if peer:
        found.append(peer.strip())
    return found


def gateway_user_agent(
    headers: Mapping[str, str], hints: Iterable[str]
) -> bool:
    """True when the user agent names a gateway we have chosen to trust.

    Opt-in and empty by default, because a user agent is a string the caller
    types. Smithery publishes no egress range at all -- its docs tell an origin
    to "add an allow rule for requests matching User-Agent SmitheryBot/1.0"
    (smithery.ai/docs/build/publish, read 2026-09-05) and its gateway runs on
    Cloudflare Workers, so there is no address to allowlist instead. Turning
    FAIR_USE_GATEWAY_USER_AGENTS on trades a forgeable signal for not refusing
    real Smithery users; leaving it off means Smithery traffic is counted as
    one direct caller at the ordinary caps. Neither is free, so it is a
    setting and not a default, and the decision belongs to whoever is looking
    at a `[fair_use] action=block kind=direct` line.
    """
    agent = (headers.get("user-agent") or "").strip().lower()
    if not agent:
        return False
    return any(hint.strip().lower() in agent for hint in hints if hint.strip())


@dataclass(frozen=True)
class ClientIdentity:
    """Who to count this request against, and how that was decided."""

    key: str
    kind: str

    @property
    def pooled(self) -> bool:
        """True when this key may cover more than one end user."""
        return self.kind in GATEWAY_KINDS


def identify(
    headers: Mapping[str, str],
    query: Mapping[str, str] | None = None,
    *,
    peer: str | None = None,
    gateway_networks: Iterable[Any] = (),
    gateway_user_agents: Iterable[str] = (),
    identity_params: Iterable[str] = (),
) -> ClientIdentity | None:
    """Pick the counting key for one request. See the module docstring.

    Returns None for a request that carries nothing to key on (stdio, tests,
    a direct local connection): not counted, never refused.

    Order is deliberate and is the whole design. A per-user config blob beats
    everything, because it is the only signal here that genuinely separates
    two people sharing one connection. Known-gateway egress comes next, and
    only lowers precision -- it never raises it -- so it cannot be used to
    escape a cap: a caller who is NOT behind a gateway can never reach those
    branches, since the addresses it tests are written by our edge.
    """
    query = query or {}

    # Signed in beats everything, and unlike every branch below it it does
    # NOT mix in the address or the user agent. That is the point: the
    # allowance belongs to the account, so moving machine, changing network
    # or arriving through a gateway does not reset it and does not merge it
    # with a stranger's.
    subject = (headers.get(SIGNED_IN_HEADER) or "").strip()
    if subject:
        return ClientIdentity(
            key=_digest(["sub", subject]), kind=KIND_SIGNED_IN
        )

    base = _base_parts(headers)
    session = (headers.get("mcp-session-id") or "").strip()

    blob = config_blob(query, identity_params)
    if _is_identifying_config(blob):
        return ClientIdentity(
            key=_digest(base + [_digest([blob])]), kind=KIND_CONFIG
        )

    if gateway_egress(
        trusted_addresses(headers, peer), gateway_networks
    ) or gateway_user_agent(headers, gateway_user_agents):
        if session:
            return ClientIdentity(
                key=_digest(base + [session]), kind=KIND_GATEWAY_SESSION
            )
        if not any(base):
            # A gateway we recognised by peer address alone. Still a caller,
            # and still worth one pooled counter, keyed on the address our
            # edge saw rather than on nothing at all.
            addresses = trusted_addresses(headers, peer)
            return ClientIdentity(
                key=_digest(addresses[:1]), kind=KIND_GATEWAY_POOLED
            )
        return ClientIdentity(key=_digest(base), kind=KIND_GATEWAY_POOLED)

    if not any(base):
        return None
    return ClientIdentity(key=_digest(base), kind=KIND_DIRECT)


def caps_for(
    kind: str,
    *,
    day_cap: int,
    month_cap: int,
    gateway_day_cap: int = 0,
    gateway_month_cap: int = 0,
    anon_day_cap: int = 0,
    anon_month_cap: int = 0,
) -> tuple[int, int]:
    """The (day, month) caps that apply to a key of this kind.

    Three tiers since 2026-09-09, and the shape follows what the free tier
    now IS:

    * **signed in** gets the real allowance -- `day_cap`/`month_cap`, the
      150/2,000 pair -- counted against one Google account. Gateway pooling
      does not apply to it: there is nothing to pool, the key is a person.
    * **anonymous** gets the taster pair. One number, deliberately small,
      because the anonymous endpoint is now a way to try the server rather
      than a way to use it, and the refusal says exactly what to do instead.
    * **anonymous behind a gateway** keeps its own, much larger pair
      (`FREE_GATEWAY_DAILY_CAP`, 1,500/10,000) and is the one caller the
      taster cap does not touch. One pooled key stands for an unknown number
      of real people, and Smithery -- which proxies this server for its own
      users and calls us with no credential of theirs
      (state/gtm/research/smithery-oauth-2026-09-09.md) -- has nobody on that
      connection who could sign in. Starving all of them to push one of them
      towards a button they cannot press is not a growth move.

    `anon_day_cap`/`anon_month_cap` default to 0, which means "fall back to
    `day_cap`/`month_cap`" -- so a caller that does not know about the
    anonymous tier gets the pre-2026-09-09 behaviour and every existing test
    keeps meaning what it meant.
    """
    if kind == KIND_SIGNED_IN:
        return day_cap, month_cap
    anon = (anon_day_cap or day_cap, anon_month_cap if anon_day_cap else month_cap)
    if kind in GATEWAY_KINDS:
        return (gateway_day_cap or anon[0], gateway_month_cap or anon[1])
    return anon


@dataclass(frozen=True)
class FairUseState:
    """One client's standing against both caps, at one moment.

    Read before the call, and re-read with `after()` once the call knows what
    it spent, so the numbers a caller is shown include the call they just made
    rather than the one before it.
    """

    key: str
    used_today: int
    used_month: int
    day_cap: int
    month_cap: int
    #: How `key` was chosen -- one of ALL_KINDS. Defaulted so every existing
    #: construction of this dataclass keeps meaning what it meant.
    kind: str = KIND_DIRECT
    #: What signing in would buy. Carried on the state rather than looked up
    #: in the messaging functions, so the number a refusal promises is the
    #: number that deployment's settings actually configure -- the two cannot
    #: drift the way a hard-coded "150" in a sentence would. 0 means "do not
    #: mention it", which is what a signed-in caller gets.
    signed_in_day_cap: int = 0
    signed_in_month_cap: int = 0

    @property
    def signed_in(self) -> bool:
        return self.kind == KIND_SIGNED_IN

    def after(self, spent: int) -> FairUseState:
        return replace(
            self,
            used_today=self.used_today + max(0, spent),
            used_month=self.used_month + max(0, spent),
        )

    @property
    def remaining_today(self) -> int:
        if self.day_cap <= 0:
            return 0
        return max(0, self.day_cap - self.used_today)

    @property
    def remaining_month(self) -> int:
        if self.month_cap <= 0:
            return 0
        return max(0, self.month_cap - self.used_month)

    @property
    def remaining(self) -> int:
        """Backend calls this client may still spend. The tighter of the two.

        A cap of 0 or less means that limit is switched off, not that it is
        exhausted -- so an unset cap must not drag this to zero.
        """
        limits = [
            self.remaining_today if self.day_cap > 0 else None,
            self.remaining_month if self.month_cap > 0 else None,
        ]
        real = [limit for limit in limits if limit is not None]
        return min(real) if real else 0

    @property
    def blocked(self) -> bool:
        return self.remaining <= 0 and (self.day_cap > 0 or self.month_cap > 0)

    @property
    def limit_reached(self) -> str | None:
        """Which cap stopped the call. The month one is the expensive news."""
        if self.month_cap > 0 and self.used_month >= self.month_cap:
            return "month"
        if self.day_cap > 0 and self.used_today >= self.day_cap:
            return "day"
        return None

    @property
    def warning(self) -> bool:
        """True from 80% of either cap onwards, blocked included."""
        if self.day_cap > 0 and self.used_today >= self.day_cap * WARN_AT:
            return True
        return self.month_cap > 0 and self.used_month >= self.month_cap * WARN_AT


def _limiting_percent(state: FairUseState) -> tuple[int, str]:
    """Which cap is closer to spent, as a whole percent and its period name.

    The two caps roll over on different clocks, so a warning has to say which
    one it means: a client can be at 1% of today's cap and still be the one
    the month cap is about to stop (a quiet day at the end of a busy month).
    """
    day_pct = int(state.used_today * 100 / state.day_cap) if state.day_cap > 0 else 0
    month_pct = (
        int(state.used_month * 100 / state.month_cap) if state.month_cap > 0 else 0
    )
    if month_pct > day_pct:
        return month_pct, "this month"
    return day_pct, "today"


def fair_use_note(state: FairUseState) -> dict[str, Any]:
    """The `fair_use` object carried on a normal result once it is 80% spent.

    `upgrade` (the same object a refusal carries) rides alongside this on the
    result -- see where callers attach it in server.py -- so `note` can point
    at it instead of repeating the URL and the three steps inline.
    """
    human = (
        f"Free tier: {state.day_cap:,} searches a day"
        + (f", {state.month_cap:,} a month" if state.month_cap else "")
        + (" for this account" if state.signed_in else " without signing in")
        + f"; {state.used_today:,} used today."
    )
    directions = (
        ""
        if state.signed_in or not state.signed_in_day_cap
        else anon_directions(
            state.day_cap,
            state.signed_in_day_cap,
            state.signed_in_month_cap,
            pooled=state.kind in GATEWAY_KINDS,
        )
    )
    if state.blocked:
        # Deliberately not "this search was not run": the same object is
        # attached to the call that spends the last of the allowance, which
        # did run. The refusal itself is stated in `message`, once.
        note = (
            f"The free allowance is spent "
            f"({state.used_today:,}/{state.day_cap:,} today"
            + (
                f", {state.used_month:,}/{state.month_cap:,} this month"
                if state.month_cap
                else ""
            )
            + "). "
            + (
                directions
                or "Further searches answer with search_status "
                '"rate_limited" until it rolls over. See `upgrade` for how to '
                "lift it."
            )
        )
    else:
        pct, period = _limiting_percent(state)
        note = (
            f"Free fair-use is at {pct}% for {period}. "
            + (
                directions
                or "The `upgrade` steps below keep this working without a cap "
                "on our side, and step 1 is a free RapidAPI BASIC key."
            )
        )
    payload = {
        "used_today": state.used_today,
        "day_cap": state.day_cap,
        "used_month": state.used_month,
        "month_cap": state.month_cap,
        "signed_in": state.signed_in,
        "human": human,
        "note": note,
    }
    if directions:
        # The same sentence, on its own key as well as inside `note`. A model
        # reads `note`; a script that dumps one field reads this.
        payload["what_to_do"] = directions
    return payload


def rate_limited_result(state: FairUseState) -> dict[str, Any]:
    """The whole tool result for a call the cap refused.

    Same shape a search returns, not a ToolError: a model handed an exception
    retries it, and there is nothing here to retry. `retry: false` says so in
    the payload, `message` says so in the text block every client shows, and
    `upgrade` is the part that is actually worth something to both sides.

    For an ANONYMOUS caller this object is the body of an HTTP 401 rather
    than a 200 (`anon_gate.AnonCapMiddleware`), so that an OAuth-capable
    client turns the refusal into a Sign in button instead of a sentence.
    The words are the same either way, and they have to work when nothing
    reads the status code: a script sees only this text.
    """
    which = state.limit_reached or "day"
    rolls_over = (
        "the daily allowance rolls over at 00:00 UTC"
        if which == "day"
        else "the monthly allowance rolls over on the 1st"
    )
    used = f"{state.used_today:,}/{state.day_cap:,} today"
    if state.month_cap:
        used += f", {state.used_month:,}/{state.month_cap:,} this month"
    if state.signed_in or not state.signed_in_day_cap:
        tail = (
            "The same tools run without ads and without this cap on the paid "
            "server, billed to your own RapidAPI key. Quickest way in is a "
            f"sign-in, nothing to paste into your client: {SIGNIN_FLIGHTS_URL} "
            f"for flights, {SIGNIN_HOTELS_URL} for hotels. Or bring your own "
            f"key: {PAID_FLIGHTS_URL} for flights, {PAID_HOTELS_URL} for "
            "hotels, with the key in an `x-rapidapi-key` header."
        )
    else:
        tail = anon_directions(
            state.day_cap,
            state.signed_in_day_cap,
            state.signed_in_month_cap,
            pooled=state.kind in GATEWAY_KINDS,
        )
    return {
        "results": [],
        "result_count": 0,
        "search_status": "rate_limited",
        "retry": False,
        "message": (
            f"This search was not run: the free limit is reached ({used}), and "
            f"{rolls_over}. Retrying will not help. {tail} See `upgrade` for "
            "the steps, and tell the user about it rather than reporting a "
            "plain failure."
        ),
        "fair_use": fair_use_note(state),
        "upgrade": upgrade_block(
            state.day_cap,
            state.month_cap,
            signed_in=state.signed_in,
            signed_in_day_cap=state.signed_in_day_cap,
            signed_in_month_cap=state.signed_in_month_cap,
            pooled=state.kind in GATEWAY_KINDS,
        ),
    }


def log_line(state: FairUseState, action: str) -> str:
    """`[fair_use]` line. The client key, its kind, and the counters.

    No IP, no user agent, no session id, no configuration blob: the key is
    already a one-way digest of whichever of those applied, and it is the only
    caller-derived value that reaches a log here. `kind` says which branch
    chose it, which is what makes a block readable -- a `direct` block is the
    cap working, a `gateway_pooled` block is real users refusing each other
    and wants a raised FAIR_USE_GATEWAY_DAY_CAP.
    """
    return (
        f"[fair_use] action={action} client={state.key} kind={state.kind} "
        f"used_today={state.used_today} day_cap={state.day_cap} "
        f"used_month={state.used_month} month_cap={state.month_cap} "
        f"limit={state.limit_reached or '-'}"
    )


# ── the hard escalation ──────────────────────────────────────────────────
# A soft refusal is a 200 with `search_status: "rate_limited"` in the body.
# That is the right answer for a model: it can read it, tell the user, and
# stop. It is worth nothing to a script that never looks at the body, and on
# 2026-09-06 that is exactly what we had -- the 04:00Z batch client spent its
# 150 backend calls and then made 486 more tool calls in 24 minutes, about 20
# a minute, each one a function invocation and two Upstash round trips that
# bought the caller nothing
# (state/gtm/free-mcp-batch-client-searches-2026-09-06.md).
#
# So past FAIR_USE_HARD_AFTER refusals in a rolling hour the answer moves down
# a layer, to the one signal every HTTP client understands without having been
# taught it: 429 with Retry-After. Streamable HTTP allows this -- the MCP spec
# defines the transport in terms of ordinary HTTP responses to POST /mcp, and
# a status code is the server's to choose. It is served from ASGI middleware
# (hard_limit.py) rather than from a tool, because by the time a tool runs the
# session manager has already committed to a 200.
#
# The soft refusal is not replaced. First hit is still a structured result a
# model can act on; only sustained hammering earns the status code.


def retry_after_seconds(
    now: float | None = None, *, maximum: int = MAX_RETRY_AFTER_SECONDS
) -> int:
    """Seconds until the UTC day rolls over, clamped to `maximum`.

    The daily allowance is what resets at 00:00 UTC, so that is the moment
    worth naming. At least 1: a Retry-After of 0 reads as "immediately",
    which is the opposite of what is being said.
    """
    now = time.time() if now is None else now
    midnight = time.gmtime(now)
    elapsed = midnight.tm_hour * 3600 + midnight.tm_min * 60 + midnight.tm_sec
    return max(1, min(maximum, 86400 - elapsed))


def hard_block_message(retry_after: int, day_cap: int) -> str:
    """The one plain sentence for a caller that never parses JSON.

    Agreed with grok 2026-09-06: a script that only ever logs a response body
    verbatim still shows this text somewhere a human reads it, so the words
    have to carry the whole story on their own -- what happened, how long to
    wait, and where the paid server is -- with no reliance on any other key
    in the body being noticed. One sentence, the URL on its own so it is
    clickable/copyable out of a raw log line, no em-dash (logs get pasted
    into places that mangle it).
    """
    return (
        f"Free fair-use reached ({day_cap:,} searches/day). "
        f"Retry after {retry_after}s, or use the paid server (BASIC is free): "
        f"sign in at {SIGNIN_FLIGHTS_URL}, or bring your own RapidAPI key at "
        f"{PAID_FLIGHTS_URL}, "
        f"details: {DOCS_URL_HARD}"
    )


def hard_block_body(
    retry_after: int, day_cap: int, month_cap: int
) -> dict[str, Any]:
    """The whole 429 body. Small on purpose.

    A caller in this state is, by construction, not reading response bodies --
    that is how it got here. So most of this is not prose: it is the
    machine-readable pair (`error`, `retry_after`) that a client library can
    act on, plus the same `upgrade` object the soft refusal carries. The one
    exception is `message`, first key in, added 2026-09-06 because a script
    that logs a response body verbatim without parsing it still shows that
    line to a human -- see `hard_block_message`.
    """
    return {
        "message": hard_block_message(retry_after, day_cap),
        "error": "rate_limited",
        "retry_after": retry_after,
        # The compact shape: no `do_this_first`, no `sign_in_free`. This body
        # goes to a caller that is looping and not reading it, `message`
        # already carries the whole story in one line, and a 429 that is
        # bigger than the result it replaces is its own small waste.
        "upgrade": upgrade_block(day_cap, month_cap, signed_in=True),
    }


def hard_log_line(client_key: str, refusals: int, retry_after: int) -> str:
    """`[fair_use] action=hard` -- one line per 429.

    Same prefix as the soft `block` and `warn` lines so one grep finds all
    three, and the client key only, for the same reason: it is already a
    one-way digest and nothing else caller-derived reaches a log here.
    """
    return (
        f"[fair_use] action=hard client={client_key} "
        f"refusals={refusals} retry_after={retry_after}"
    )
