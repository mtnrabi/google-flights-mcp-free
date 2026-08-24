"""
Environment-driven configuration for the free, ad-supported flights MCP server.

Everything here is read once at import. Missing Lambda credentials are fatal
(the server has nothing to serve without them); missing Lulu credentials are
not -- the Lulu SDK is inert without them, so the server still runs and still
serves flights, just without ads. That asymmetry is deliberate: an ad outage
must never take flight search down.
"""

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    return Settings(
        base_lambda_url=_env_str("BASE_LAMBDA_URL", required=True).rstrip("/"),
        rapid_auth=_env_str("RAPID_AUTH", required=True),
        # The backend's own internal budget is 90s and its router allows
        # 105s (backend/src/constants.py:16, google_flights_router.py:161).
        # Match that ceiling so we never time out before the backend does.
        lambda_timeout_seconds=_env_float("LAMBDA_TIMEOUT_SECONDS", 105.0),
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
    )
