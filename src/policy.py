"""
Caller classification and enforcement.

The requirement is "only usable where the sponsored card actually renders".
What is and isn't achievable here is worth stating plainly, because the
obvious approach does not work.

What does NOT work
------------------
`clientInfo.name` cannot gate anything. MCP spec revision 2026-07-28 says so
normatively: those fields "are self-reported by the sender and are not
verified by the protocol... SHOULD NOT rely on them for security decisions."
A five-line script sets `"claude-ai"` just as easily as Claude does. Same for
declared capabilities, protocol version, and User-Agent. We record the name
for reporting only, never as a gate.

What DOES work
--------------
A source IP cannot be forged on a completed TLS handshake, and both hosts
broker remote MCP from their own cloud rather than the user's machine:

  Anthropic  160.79.104.0/21   published as the outbound range used "when
                               making MCP tool calls to external servers"
  OpenAI     openai.com/chatgpt-connectors.json   published, changes, so the
                               feed is refreshed rather than hardcoded

Those two ranges are the surfaces where the MCP Apps widget renders, which
is the only surface that fires Lulu's rendered-impression beacon (a 1px img
from inside the widget frame -- see lulu_ads Sponsored.impUrl). Local clients
like Claude Code connect from the user's own machine and get the CLI text
card, which has no beacon: real users, real traffic, structurally $0 CPM.

What we CANNOT measure here, and why
------------------------------------
The beacon fires from the widget frame straight to ads.getlulu.dev. It never
touches this server, so this process cannot compute a true render rate on its
own. Anyone claiming otherwise has not read the SDK.

What we can do is count slots *served* per tier (see telemetry) and reconcile
that offline against the rendered-impression count on Lulu's side. The ratio
of the two is the number that decides whether a tier is worth serving. Once a
tier is shown to render nothing, add it to BLOCKED_TIERS -- the mechanism is
built, the input is a human decision informed by real numbers, not a
self-measured loop this process is not in a position to close.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Published at platform.claude.com/docs/en/api/ip-addresses as the stable
# outbound range Anthropic uses for MCP tool calls.
ANTHROPIC_OUTBOUND_RANGES = ("160.79.104.0/21",)

OPENAI_CONNECTORS_FEED = "https://openai.com/chatgpt-connectors.json"

# Advisory only -- these strings gate nothing, they only label rows in the
# metrics so we can tell CLI traffic apart when reconciling with Lulu.
KNOWN_LOCAL_CLIENT_HINTS = (
    "claude-code",
    "claude code",
    "cursor",
    "windsurf",
    "cline",
    "continue",
    "zed",
    "goose",
)

TIER_LLM_HOST = "llm_host"
TIER_LOCAL_CLIENT = "local_client"
TIER_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    tier: str
    allowed: bool
    cap: int
    reason: str
    widget_capable: bool


class ClientClassifier:
    """Classifies a caller by source IP, with an advisory client-name label."""

    # Don't retry the feed on every request when it is failing.
    RETRY_COOLDOWN_SECONDS = 300.0

    def __init__(self) -> None:
        self._anthropic = [
            ipaddress.ip_network(c) for c in ANTHROPIC_OUTBOUND_RANGES
        ]
        self._openai: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._openai_loaded = False
        # None means "never attempted". This must NOT be 0.0: time.monotonic()
        # is seconds-since-boot, so on a fresh serverless microVM it returns a
        # small number, and `now - 0.0 < RETRY_COOLDOWN_SECONDS` would suppress
        # the very first attempt forever. Locally monotonic is days, so the bug
        # only appears once deployed -- it silently left every ChatGPT caller
        # classified as `unknown`.
        self._last_attempt: float | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def openai_ranges_loaded(self) -> bool:
        return self._openai_loaded

    @property
    def openai_ranges_error(self) -> str | None:
        """Last failure reason, surfaced on /metrics.

        A classification that silently degrades is worse than one that fails
        loudly -- without this the only symptom is `unknown` traffic that
        should have been `llm_host`.
        """
        return self._last_error

    async def ensure_openai_ranges(self) -> None:
        """Load the feed on first use, then never again.

        Serverless has no startup hook that can await, so this replaces the
        eager load. It is deliberately not awaited on the cold-start import
        path: a blocking network call there would be charged to whichever
        user happened to arrive first.
        """
        if self._openai_loaded:
            return
        now = time.monotonic()
        if (
            self._last_attempt is not None
            and now - self._last_attempt < self.RETRY_COOLDOWN_SECONDS
        ):
            return
        async with self._lock:
            if self._openai_loaded:
                return
            self._last_attempt = time.monotonic()
            await self.refresh_openai_ranges()

    async def refresh_openai_ranges(self, timeout: float = 10.0) -> int:
        """Load OpenAI's published connector egress prefixes.

        Failure is not fatal and is deliberately not retried inline: we would
        rather serve ChatGPT traffic as `unknown` for a while than block it.
        `openai_ranges_loaded` stays False so enforcement can fail open.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(OPENAI_CONNECTORS_FEED)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning(
                "could not load OpenAI connector ranges (%s); "
                "ChatGPT traffic will classify as unknown and will NOT be "
                "blocked while this is unloaded",
                exc,
            )
            return 0

        networks = []
        for entry in payload.get("prefixes", []):
            cidr = entry.get("ipv4Prefix") or entry.get("ipv6Prefix")
            if not cidr:
                continue
            try:
                networks.append(ipaddress.ip_network(cidr))
            except ValueError:
                continue

        if networks:
            self._openai = networks
            self._openai_loaded = True
            self._last_error = None
        else:
            self._last_error = "feed returned no usable prefixes"
        return len(networks)

    def classify(self, source_ip: str | None, client_name: str | None) -> str:
        if source_ip:
            try:
                address = ipaddress.ip_address(source_ip)
            except ValueError:
                address = None
            if address is not None:
                if any(address in net for net in self._anthropic):
                    return TIER_LLM_HOST
                if any(address in net for net in self._openai):
                    return TIER_LLM_HOST

        name = (client_name or "").strip().lower()
        if name and any(hint in name for hint in KNOWN_LOCAL_CLIENT_HINTS):
            return TIER_LOCAL_CLIENT
        return TIER_UNKNOWN


def decide(
    tier: str,
    *,
    mode: str,
    blocked_tiers: frozenset[str],
    full_cap: int,
    openai_ranges_loaded: bool,
) -> Decision:
    """Turn a tier into a serving decision.

    `monitor` classifies and reports but serves everyone at the full cap --
    the right default for week one, when blocking would destroy the very
    traffic sample the POC exists to collect.
    """
    widget_capable = tier == TIER_LLM_HOST

    if mode == "off":
        return Decision(tier, True, full_cap, "enforcement disabled", widget_capable)

    if mode == "monitor":
        return Decision(
            tier, True, full_cap, "monitor mode: classified, not enforced", widget_capable
        )

    # mode == "enforce"
    if tier in blocked_tiers:
        # Never block on a classification we could not actually make. Without
        # the OpenAI feed, `unknown` includes every real ChatGPT caller.
        if tier == TIER_UNKNOWN and not openai_ranges_loaded:
            return Decision(
                tier,
                True,
                full_cap,
                "would block, but OpenAI ranges are unloaded so 'unknown' is "
                "not a trustworthy classification -- failing open",
                widget_capable,
            )
        return Decision(
            tier, False, 0, f"tier '{tier}' is blocked by configuration", widget_capable
        )

    if tier == TIER_LLM_HOST:
        return Decision(tier, True, full_cap, "verified LLM-host egress", widget_capable)

    # Served, but at a reduced fan-out. These callers cost the same per
    # backend call and (for local clients, structurally) return no CPM.
    reduced = max(1, full_cap // 3)
    return Decision(
        tier,
        True,
        reduced,
        f"tier '{tier}' cannot fire a render beacon; fan-out reduced to {reduced}",
        widget_capable,
    )


def extract_source_ip(headers: dict[str, str], peer_ip: str | None) -> str | None:
    """Best-effort client IP, honouring one proxy hop.

    Render and most PaaS front ends terminate TLS and set X-Forwarded-For, in
    which case the peer address is the proxy, not the caller. The leftmost
    entry is the original client. This is trusted only because the server is
    expected to sit behind exactly one such proxy -- it is a classification
    input, not an authentication mechanism.
    """
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return peer_ip
