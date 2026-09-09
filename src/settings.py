"""
Environment-driven configuration for the free, ad-supported flights MCP server.

Everything here is read once at import. Missing Lambda credentials are fatal
(the server has nothing to serve without them); missing Lulu credentials are
not -- the Lulu SDK is inert without them, so the server still runs and still
serves flights, just without ads. That asymmetry is deliberate: an ad outage
must never take flight search down.
"""

import ipaddress
import os
from dataclasses import dataclass, field


def _strip_quotes(value: str) -> str:
    """Drop one layer of surrounding quotes.

    Both existing env files in this repo (backend/.env, apify_actor/.env)
    quote their values. A value copied across verbatim would otherwise arrive
    as '"https://..."' and produce a 403 that looks like a wrong secret rather
    than a quoting mistake.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _env_str(name: str, default: str | None = None, required: bool = False) -> str:
    raw = os.environ.get(name, default)
    value = _strip_quotes(raw) if isinstance(raw, str) else raw
    if required and not value:
        raise RuntimeError(
            f"{name} is required. Copy example.env to .env and fill it in."
        )
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _csv(name: str) -> tuple[str, ...]:
    """A comma-separated env var as a tuple, blanks dropped."""
    raw = _env_str(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: The ``Timeout`` on the deployed ``flyMyGApi`` function this server calls,
#: read from the live function configuration on 2026-08-27. **The only place
#: that number is written down in this project**; the wait below is derived from
#: it rather than restated, because a second literal is what let the previous
#: value go stale the day the ``Timeout`` moved.
UPSTREAM_FUNCTION_TIMEOUT_SECONDS = 60.0

#: Extra wait on top of the callee's whole life, covering connect, TLS and
#: transit between this process and the function.
#:
#: Measured through the RapidAPI edge on 2026-08-27, over 46 requests taken
#: after the ``Timeout`` was raised to 60: successful answers as late as 59.7s,
#: failures at 60.21s and 60.23s. This server calls the function directly rather
#: than through that edge, so it sees the earlier of those two -- but the same
#: rule applies, and the same margin is used so the two hops cannot be tuned
#: apart by accident.
UPSTREAM_RELAY_MARGIN_SECONDS = 15.0

#: How long this server waits on a backend call. Derived, deliberately.
#:
#: It was 45.0, matched to a ``Timeout`` of 45 on the assumption the two would
#: move together. They did not: the function was raised to 60 on 2026-08-27 and
#: this was not, which left the server abandoning searches the callee would have
#: answered -- two of the 46 measured requests came back successfully at 48.7s
#: and 59.7s, and both would have been thrown away.
#:
#: Before that it was 105, from ``backend/src/constants.py``'s deleted
#: LAMBDA_REQUEST_TIMEOUT_SECONDS (90) plus a router's +15, describing a
#: function that never existed. The rule that replaces both guesses has a
#: direction: below the callee's ``Timeout`` discards answers that were coming;
#: above it only costs latency on a request that has already failed.
#:
#: There is **no separate RapidAPI gateway ceiling near 45s**, whatever earlier
#: comments in this repo said. The 2026-08-27 measurement shows the failure wall
#: moving exactly with the function ``Timeout``.
DEFAULT_TIMEOUT_SECONDS = (
    UPSTREAM_FUNCTION_TIMEOUT_SECONDS + UPSTREAM_RELAY_MARGIN_SECONDS
)

#: What this deployment calls itself in the ``X-FP-Source`` header on every
#: backend call. This directory is deployed as the free, Lulu-monetized server,
#: so the default is what it is; ``FP_SOURCE`` overrides it for anything else
#: that runs this code. The backend reads it, prints it, and changes nothing
#: else -- an unrecognised value costs a mislabelled log line and nothing more.
DEFAULT_SOURCE = "lulu"

#: Default Stay22 sub-id for clicks that originate in this server, so its
#: revenue is separable from any other surface of ours carrying Stay22 links.
DEFAULT_STAY22_CAMPAIGN = "free-mcp"

#: Stay22's OWN Booking.com affiliate account -- not ours. Confirmed live
#: 2026-09-06 (`&aid=1607597` on a redirected `stay22.com/allez/booking`
#: response): Stay22 is the party with the Booking.com partnership, so this
#: is the id that has to sit in `aid=` on a raw Booking.com URL for the click
#: to be billable at all. Our own attribution rides on `label`, built from
#: `STAY22_AID`/`STAY22_CAMPAIGN`. See src/affiliate.py.
DEFAULT_STAY22_BOOKING_AID = "1607597"


@dataclass(frozen=True)
class Settings:
    # ── Backend (the API Lambda this server proxies to) ──────────────────
    # Same endpoint + auth header the Apify actor uses. See
    # backend/src/api_lambda.py:15 -- a wrong/missing secret is a 403.
    base_lambda_url: str
    rapid_auth: str
    lambda_timeout_seconds: float


    # ── Lulu ads ─────────────────────────────────────────────────────────
    # public_url MUST exactly match the URL clients connect to. Lulu derives
    # Claude's `_meta.ui.domain` from it, and the widget SILENTLY never
    # renders on a mismatch -- which means $0 CPM with no error anywhere.
    public_url: str
    lulu_publisher_id: str
    lulu_api_key: str
    ads_enabled: bool

    # ── Cost control ─────────────────────────────────────────────────────
    # One user intent ("anywhere in October") can expand to dozens of
    # backend calls. Revenue is per rendered ad, which is per *tool call*,
    # not per backend call -- so backend fan-out is pure cost and is capped.
    max_backend_calls_per_tool_call: int
    max_concurrent_backend_calls: int
    # Ceiling on the shared connection pool. Vercel functions share 1,024
    # file descriptors across all concurrent executions on an instance, and
    # sockets come out of that pool -- an unbounded pool plus 15-way fan-out
    # hits "too many open files" under load.
    max_http_connections: int
    # Rolling 24h budget. 0 disables the guard entirely.
    daily_backend_call_budget: int
    # Fraction of the daily budget at which we degrade to a single backend
    # call per tool call instead of refusing outright.
    budget_degrade_at: float

    # ── Fair use, per client ─────────────────────────────────────────────
    # The daily budget above is a TOTAL: one scheduled client can spend it
    # and every other caller gets a degraded server for the rest of the day.
    # That is exactly what happened (state/gtm/free-mcp-batch-client-2026-09-05
    # .md: 3,270 of 4,078 backend calls in 30 hours came from one caller). So
    # these are per client, and they count BACKEND calls -- the unit that
    # costs money -- not tool calls.
    #
    # 2,000 a month is the number that matters: 150/day for a month would be
    # 4,500, and RapidAPI PRO is 2,500 requests for $10. Free must not beat
    # the cheapest paid plan.
    fair_use_day_cap: int
    fair_use_month_cap: int
    # The same two caps for a POOLED key -- one counter standing for an
    # unknown number of real people behind one gateway. Claude.ai's connector
    # fetcher reaches us from a single published range for every Claude user
    # on earth; Smithery proxies every install of this server through its own
    # infrastructure. At 150/day those users would refuse each other while the
    # direct script the cap was written for is capped exactly as intended, so
    # a gateway key gets its own, higher pair. Ten times the day cap and five
    # times the month cap: generous enough that ordinary chat traffic never
    # notices, still bounded, and still under DAILY_BACKEND_CALL_BUDGET as the
    # real ceiling on spend.
    fair_use_gateway_day_cap: int
    fair_use_gateway_month_cap: int
    # ── The anonymous allowance: ROLLBACK ONLY (2026-09-09) ─────────────
    # As shipped these do nothing, because nothing is served anonymously.
    # `/mcp` requires a Google sign-in for every call (Matan: "for free MCP -
    # let's make all of them go through oauth in the plain /mcp"), so every
    # served request belongs to an account and takes the 150/2,000 pair
    # above, counted per Google `sub` and not per address.
    #
    # They exist for exactly one situation: `FREE_ANON_MODE=open` puts the
    # pre-2026-09-09 behaviour back with one env var and no deploy, and these
    # are the allowance an anonymous caller gets under it. 10 a day, and 10
    # rather than a rounder number for one reason -- it is exactly the
    # RapidAPI BASIC monthly quota, so an anonymous free tier reads as "a day
    # of what BASIC gives you in a month" and cannot be mistaken for a plan
    # anybody would build on. 0 means "this limit is off" everywhere in
    # fair_use.py, which is why the month one is 0: the day cap is the only
    # thing that needs to bite.
    anon_day_cap: int
    anon_month_cap: int
    # Extra CIDRs to treat as gateway egress, comma-separated. Anthropic's
    # published range and OpenAI's published feed are already known through
    # policy.py; this is where a range we learn about later (Smithery, Glama,
    # mcp.run) goes without a deploy of new code. Empty by default -- an
    # unverified range here would hand the higher caps to whoever is in it.
    fair_use_gateway_cidrs: tuple[str, ...]
    # User-agent substrings to treat as gateway egress, comma-separated, and
    # EMPTY by default. Smithery publishes no IP range -- its own advice is to
    # allowlist `SmitheryBot/1.0` -- so this is the only handle on it, and a
    # user agent is a string the caller types. Setting it protects real
    # Smithery users from sharing one 150-a-day counter and simultaneously
    # lets anyone claim the higher caps by copying a header. Off until a
    # `[fair_use] action=block kind=direct` line says it is needed.
    fair_use_gateway_user_agents: tuple[str, ...]
    # Extra query parameters that carry a PER-USER value a gateway injects.
    # Smithery's current gateway passes a server's declared config fields
    # through as plain query params rather than one base64 `config` blob, and
    # the names are whatever that server's schema declares. `config` and
    # `config.<field>` are always read; this names anything else. Never
    # include a gateway's own `api_key` -- see rule 6.
    fair_use_identity_params: tuple[str, ...]
    # How many soft refusals one client may collect in a rolling hour before
    # the server stops answering it at the MCP layer at all and starts
    # returning HTTP 429 + Retry-After instead.
    #
    # The soft refusal (`search_status: "rate_limited"`, a normal 200) is
    # right for a model: it can read it, tell the user, and stop. It is worth
    # nothing to a script. On 2026-09-06 the 04:00Z batch client spent its 150
    # backend calls and then made 486 more tool calls in 24 minutes -- about
    # 20 a minute -- because nothing in its code reads the result body
    # (state/gtm/free-mcp-batch-client-searches-2026-09-06.md). Each of those
    # costs us a function invocation and two Upstash round trips and buys the
    # caller nothing. An HTTP status with Retry-After is the one signal every
    # HTTP client understands without being taught, so past this many
    # refusals that is what the caller gets.
    #
    # 20 in an hour is chosen so no human-driven client can reach it: the soft
    # refusal only starts after the day cap is spent, and a model that reads
    # one refusal stops. Twenty of them in an hour is a loop.
    # 0 disables the escalation and leaves the soft refusal as the only
    # behaviour.
    fair_use_hard_after: int
    # The off switch. Unlike ENFORCEMENT_MODE there is no monitor setting:
    # mrabi's decision on 2026-09-05 was to enforce from day one, because the
    # traffic this exists for runs every morning already.
    fair_use_enabled: bool

    # ── Enforcement ──────────────────────────────────────────────────────
    # off      -- classify nothing, serve everyone at full cap
    # monitor  -- classify and log, but serve everyone at full cap
    # enforce  -- apply per-tier caps and blocks
    enforcement_mode: str
    blocked_tiers: frozenset[str]

    # ── Serving ──────────────────────────────────────────────────────────
    host: str
    port: int
    log_path: str
    default_result_limit: int

    # ── Hotels backend ───────────────────────────────────────────────────
    # A different Lambda in a different region, same X-RapidAPI-Proxy-Secret
    # scheme. Empty url or secret disables the hotel tools entirely rather
    # than registering tools that can only fail -- a tool that 500s on every
    # call is worse than one that is not offered.
    hotels_lambda_url: str = ""
    hotels_auth: str = ""

    # ── Attribution ──────────────────────────────────────────────────────
    # Sent as X-FP-Source on every backend call. See DEFAULT_SOURCE above.
    fp_source: str = DEFAULT_SOURCE

    # ── Stay22 affiliate links (hotels, free server only) ────────────────
    # The affiliate id from the Stay22 hub. EMPTY BY DEFAULT and empty means
    # off: every booking link passes through byte-for-byte, which is the
    # state this ships in until a real id exists. It is NOT the LetMeAllez
    # script id (`lmaID`) -- that one drives a page widget and has no meaning
    # in a redirect. See src/affiliate.py.
    #
    # Never set on the paid server. `mcp_server_paid` does not read this and
    # must not start: the paid product is the ad-free, unmonetised one.
    stay22_aid: str = ""
    # Sub-id passed through to Stay22's reporting so free-server clicks are
    # separable from every other place a Stay22 link of ours might appear
    # (the site, an email). Only sent when the wrap is on.
    stay22_campaign: str = DEFAULT_STAY22_CAMPAIGN
    # Stay22's own Booking.com affiliate id (`aid=1607597`), applied to the
    # RAW Booking.com URL we hand back as `booking_url` -- not to `link`,
    # which already goes through Stay22's redirect. Defaults on (matches
    # `STAY22_AID` being the on/off switch): only takes effect when
    # `stay22_aid` is set, same as everything else in this section.
    stay22_booking_aid: str = DEFAULT_STAY22_BOOKING_AID

def load_settings() -> Settings:
    mode = _env_str("ENFORCEMENT_MODE", "monitor").strip().lower()
    if mode not in {"off", "monitor", "enforce"}:
        raise RuntimeError(
            f"ENFORCEMENT_MODE must be one of off|monitor|enforce, got {mode!r}"
        )

    blocked = _env_str("BLOCKED_TIERS", "")
    blocked_tiers = frozenset(
        t.strip() for t in blocked.split(",") if t.strip()
    )

    # Validated here rather than at first use: a typo in a CIDR should stop a
    # deploy, not silently drop a gateway range and start refusing real users
    # at the direct cap with nothing in the logs to say why.
    gateway_cidrs = _csv("FAIR_USE_GATEWAY_CIDRS")
    for cidr in gateway_cidrs:
        try:
            ipaddress.ip_network(cidr)
        except ValueError as exc:
            raise RuntimeError(
                f"FAIR_USE_GATEWAY_CIDRS contains {cidr!r}, which is not a "
                f"network: {exc}"
            ) from exc

    return Settings(
        base_lambda_url=_env_str("BASE_LAMBDA_URL", required=True).rstrip("/"),
        rapid_auth=_env_str("RAPID_AUTH", required=True),
        # Derived; see DEFAULT_TIMEOUT_SECONDS above for the measurement and
        # for why it is never written down as a second literal.
        lambda_timeout_seconds=_env_float("LAMBDA_TIMEOUT_SECONDS",
                                          DEFAULT_TIMEOUT_SECONDS),
        hotels_lambda_url=_env_str("HOTELS_LAMBDA_URL", "").rstrip("/"),
        hotels_auth=_env_str("HOTELS_AUTH", ""),
        public_url=_env_str("MCP_PUBLIC_URL", "http://localhost:8000/mcp"),
        lulu_publisher_id=_env_str("LULU_ADS_PUBLISHER_ID", ""),
        lulu_api_key=_env_str("LULU_ADS_API_KEY", ""),
        ads_enabled=_env_bool("ADS_ENABLED", True),
        max_backend_calls_per_tool_call=_env_int(
            "MAX_BACKEND_CALLS_PER_TOOL_CALL", 15
        ),
        max_concurrent_backend_calls=_env_int("MAX_CONCURRENT_BACKEND_CALLS", 10),
        max_http_connections=_env_int("MAX_HTTP_CONNECTIONS", 60),
        daily_backend_call_budget=_env_int("DAILY_BACKEND_CALL_BUDGET", 0),
        budget_degrade_at=_env_float("BUDGET_DEGRADE_AT", 0.8),
        fair_use_day_cap=_env_int("FAIR_USE_DAY_CAP", 150),
        fair_use_month_cap=_env_int("FAIR_USE_MONTH_CAP", 2000),
        # UNCHANGED at 1500/10000, deliberately, and this is the one place
        # the anonymous taster cap does not reach. A pooled gateway key
        # stands for an unknown number of real people behind one connection
        # -- every claude.ai user, every Smithery install of this server --
        # and dropping them to the taster cap would starve all of them at
        # once for the sins of none of them. Smithery in particular proxies
        # our server for its users and calls us with no credential of theirs
        # at all (state/gtm/research/smithery-oauth-2026-09-09.md), so there
        # is nobody on that connection who could have signed in.
        # `FREE_GATEWAY_DAILY_CAP` is the name going forward;
        # `FAIR_USE_GATEWAY_DAY_CAP` still works so nothing set on a live
        # deployment silently stops applying.
        fair_use_gateway_day_cap=_env_int(
            "FREE_GATEWAY_DAILY_CAP", _env_int("FAIR_USE_GATEWAY_DAY_CAP", 1500)
        ),
        fair_use_gateway_month_cap=_env_int(
            "FREE_GATEWAY_MONTHLY_CAP", _env_int("FAIR_USE_GATEWAY_MONTH_CAP", 10000)
        ),
        anon_day_cap=_env_int("FREE_ANON_DAILY_CAP", 10),
        anon_month_cap=_env_int("FREE_ANON_MONTHLY_CAP", 0),
        fair_use_gateway_cidrs=gateway_cidrs,
        fair_use_gateway_user_agents=_csv("FAIR_USE_GATEWAY_USER_AGENTS"),
        fair_use_identity_params=_csv("FAIR_USE_IDENTITY_PARAMS"),
        fair_use_hard_after=_env_int("FAIR_USE_HARD_AFTER", 20),
        fair_use_enabled=_env_bool("FAIR_USE_ENABLED", True),
        enforcement_mode=mode,
        blocked_tiers=blocked_tiers,
        host=_env_str("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        # Empty disables the file sink and leaves stdout MCP_CALL lines as
        # the record -- which is the right setting on serverless, where the
        # filesystem is read-only outside an ephemeral /tmp.
        log_path=_env_str("LOG_PATH", ""),
        # Matches TOP_N_RESULTS_PER_COMBINATION in backend/src/constants.py:25.
        default_result_limit=_env_int("DEFAULT_RESULT_LIMIT", 10),
        fp_source=_env_str("FP_SOURCE", DEFAULT_SOURCE) or DEFAULT_SOURCE,
        # Empty = off. Deliberately no `required`, no fallback id, and no
        # warning: an unset affiliate id is the normal, shipped state.
        stay22_aid=_env_str("STAY22_AID", ""),
        stay22_campaign=_env_str("STAY22_CAMPAIGN", DEFAULT_STAY22_CAMPAIGN),
        stay22_booking_aid=_env_str(
            "STAY22_BOOKING_AID", DEFAULT_STAY22_BOOKING_AID
        ),
    )
