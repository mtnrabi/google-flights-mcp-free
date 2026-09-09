"""
MCP-protocol OAuth on the FREE server: an optional "Sign in with Google"
button inside ChatGPT, Claude and Cursor.

PROVENANCE, and why this is a copy
----------------------------------
This file, `oauthroutes.py`, `oauthstore.py`, `webauth.py`, `ratelimit.py`
and `cimd.py` are copies of the same-named files in `mcp_server_paid/`,
adapted. They are copies rather than a shared package because
`mcp_server` and `mcp_server_paid` are two separate Vercel projects and each
`vercel deploy` uploads only its own directory -- a package one level up
would not be in either bundle, and the standing deploy procedure for both is
a hand deploy from inside the project folder. The second reason is the one
that matters more: the paid server carries every paying customer, and the
free server is where experiments land. Sharing the file would mean this
server's next change is a change to that one.

Keeping the two honest: a fix to the OAuth mechanics -- PKCE, code
single-use, refresh rotation and reuse detection, the header strip -- belongs
in BOTH files on the same day, and the tests on each side pin the same
properties. Anything about RapidAPI keys, `fpk_` connect tokens or billing
belongs only in the paid copy and is absent here.

What is different from the paid copy
------------------------------------
* **No key store.** There is nothing to paste on this server. Signing in buys
  the user one thing: a fair-use allowance counted against their own account
  instead of against a digest of their IP and user agent, which is also what
  finally gives a gateway user (claude.ai, Smithery) a counter of their own.
* **The consent page states the email.** Signing in puts the address on
  FlightPowers product updates, with an unsubscribe link, and the page says
  so in one sentence before the button rather than in a policy nobody opens.
* **`/mcp` is untouched, and stays untouched.** Same open access, same caps,
  same sponsored card. The free pilot's terms are frozen through 2026-09-20,
  so this whole feature is additive: a new endpoint, and one extra line in
  the `upgrade` object that anonymous callers already receive.
* **Phase 2 is a config flip, not a code change.** `FREE_ANON_DAILY_CAP` /
  `FREE_ANON_MONTHLY_CAP` (in settings.py) and `FREE_ANON_MODE` (below) are
  the levers; both default to today's behaviour exactly.

The endpoints
-------------
A client only ever STARTS an OAuth flow when a request is answered `401` with
`WWW-Authenticate: Bearer resource_metadata=...`. So:

    /mcp        unchanged. Anonymous, ad-supported, capped per client key.
                No 401 -- unless FREE_ANON_MODE=challenge is set later, and
                even then never for a gateway or config-blob caller.
    /mcp/oauth  the same tools, the same sponsored card, the same caps, but
                Bearer-only: no token means 401 + the challenge header, which
                is the signal that makes a client show a Sign in button.

The flow, end to end
--------------------
1. The client GETs `/mcp/oauth`, gets 401 plus the challenge.
2. It fetches the RFC 9728 protected-resource document, learns the
   authorization server, and fetches the RFC 8414 document.
3. It registers at `/oauth/register` (RFC 7591), or presents an https
   `client_id` we fetch as a Client ID Metadata Document (`cimd.py`).
4. It opens `/connect/authorize` in a browser with PKCE S256.
5. That page hands off to Google sign-in when there is no session, then asks
   the human to approve THIS client by name.
6. Approval redirects back with a code; `/oauth/token` exchanges it for an
   access token (1 hour) and a refresh token (30 days).
7. Every `/mcp/oauth` call carries `Authorization: Bearer fpo_...`. The gate
   resolves it to a Google `sub` and injects it as a header the tool layer
   reads -- and strips any inbound copy of that header first, on every path,
   which is what makes the injected value trustworthy.

Why the authorization endpoint lives under /connect
---------------------------------------------------
The session cookie is scoped `Path=/connect` on purpose, so it can never be
attached to a `/mcp` request. Putting the authorization endpoint anywhere
else would mean widening that cookie's path or signing the user in twice.

Why opaque tokens and not JWTs
------------------------------
A JWT would make "revoke" a lie: a signed token stays valid until it expires
whatever we decide afterwards, and this feature has to support revocation
and refresh rotation, both of which are claims about a row existing. Tokens
are 32 bytes of `os.urandom`, stored as SHA-256 hashes, never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from . import cimd
from .oauthstore import (
    AuthCode,
    OAuthClient,
    OAuthStore,
    OAuthStoreError,
    TokenRecord,
    build_oauth_store,
    hash_secret,
)
from .pages import PRIVACY_URL as PAGES_PRIVACY_URL, TERMS_URL as PAGES_TERMS_URL, e
from .ratelimit import UNKNOWN_IP
from .webauth import WebAuthError, build_web_auth, sign_payload, verify_payload

#: The only identity provider this server accepts. A column rather than an
#: assumption, so a second one later is a value and not a migration.
PROVIDER_GOOGLE = "google"

logger = logging.getLogger(__name__)

# ── the shape of the deployment ──────────────────────────────────────────

#: The ALIAS with always-challenge semantics. `/mcp` is the URL we market
#: (Matan, 2026-09-09: "do we really need the path to end in /oauth?"), and
#: it behaves the way the MCP spec calls "authorization required when the
#: server asks": anonymous callers are served under the taster cap and are
#: challenged at the cap. This path exists for the clients that cannot do
#: that -- ChatGPT connectors and Claude's own connector UI fix the auth mode
#: when the server is ADDED, so a 401 arriving later in the session is a
#: failure rather than a prompt -- and for anyone who wants sign-in with no
#: ambiguity at all. Same handler, same tools, same everything; the only
#: difference is that request one is challenged instead of request eleven.
MCP_OAUTH_PATH = "/mcp/oauth"
#: What the gate rewrites the path to before handing the request on. FastMCP
#: mounts a plain `Route("/mcp")`, not a Mount, so `/mcp/oauth` reaches
#: nothing on its own -- the rewrite is what makes "same tools" literal
#: rather than a second copy of the tool registry.
MCP_PATH = "/mcp"


AUTHORIZE_PATH = "/connect/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"

DOCS_URL = "https://flightpowers.com/"

#: The free server's policy documents live on the marketing site, not on this
#: deployment: it serves an MCP endpoint and two sign-in pages, and a policy
#: page here would be a third copy of a document that already exists.
PRIVACY_URL = PAGES_PRIVACY_URL
TERMS_URL = PAGES_TERMS_URL

#: How `/mcp` treats a caller with no OAuth token.
#:
#:   challenge  THE DEFAULT, and what the free server is since 2026-09-09
#:              (Matan: "for free MCP - let's make all of them go through
#:              oauth in the plain /mcp"). A credential-less request is
#:              answered 401 + `WWW-Authenticate: Bearer resource_metadata=`,
#:              which is the signal an MCP client turns into a Sign in
#:              button. Every served call therefore belongs to a Google
#:              account, and every allowance is that account's own.
#:   open       the ROLLBACK, and nothing else. `/mcp` answers a
#:              credential-less caller under the anonymous taster cap
#:              (`FREE_ANON_DAILY_CAP`, enforced by
#:              `anon_gate.AnonCapMiddleware`), which is what the server did
#:              before this change. One env var, no deploy, if requiring
#:              sign-in turns out to cost more traffic than it converts.
#:
#: Nobody is exempt in `challenge`, gateways included. Smithery's release
#: probe arrives with no credential, sees the 401, and flips its listing into
#: OAuth mode -- which is the outcome we want: its users then go through our
#: Google sign-in like everyone else. A gateway that cannot do MCP
#: authorization at all (an old Smithery-style proxy pinned to a
#: config-blob-only mode, a homemade relay) gets the 401 and stops working
#: until it can, and that is the trade being made deliberately rather than
#: by accident.
ANON_MODE_OPEN = "open"
ANON_MODE_CHALLENGE = "challenge"


def anon_mode() -> str:
    """`FREE_ANON_MODE`, defaulting to `challenge`.

    Anything that is not exactly `open` is `challenge`, which is the safe
    direction for a typo now that sign-in is the product: a misspelling costs
    a sign-in prompt, not an anonymous free-for-all.
    """
    raw = (os.environ.get("FREE_ANON_MODE") or "").strip().lower()
    return ANON_MODE_OPEN if raw == ANON_MODE_OPEN else ANON_MODE_CHALLENGE


#: One scope. A second one would be a promise that some tokens can do less
#: than others, and nothing in this server enforces such a split.
DEFAULT_SCOPE = "flightpowers:free-search"

ACCESS_TOKEN_PREFIX = "fpo_"
REFRESH_TOKEN_PREFIX = "fpr_"
CODE_PREFIX = "fpc_"
#: Client ids are not secrets, but they share a channel with values that are,
#: so they get a prefix of their own rather than one a code could be mistaken
#: for at a glance in a log.
CLIENT_ID_PREFIX = "fpcl_"

#: RFC 6749 §4.1.2 says a code SHOULD be short-lived and names 10 minutes as
#: the maximum. This is that maximum, not a number picked for comfort.
CODE_TTL_SECONDS = 10 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
#: How long the signed blob on the consent form stays valid. Long enough to
#: read the page, short enough that a form left open in a tab is not a
#: standing authorisation.
CONSENT_TTL_SECONDS = 15 * 60

# ── registration hygiene (day 3) ─────────────────────────────────────────
# Registration is open, because the MCP spec requires it and because a
# client_id on its own authorises nothing. Open is not the same as unlimited:
# anyone who can POST can make a row, and rows nobody cleans up are how a
# small table becomes an incident. Three mechanisms, deliberately different
# in kind:
#
#   * a per-instance RATE limit (src/ratelimit.py) -- cheap, spoofable, first;
#   * these DURABLE per-day caps, counted in Postgres, so every instance
#     agrees on the number;
#   * a SWEEP that deletes registrations which never became an
#     authorization, so the caps are counted against a table that does not
#     silently fill with abandoned rows.
#
#: Registrations from one address in a rolling day. Generous: a NAT, a CI
#: runner or one directory registering on behalf of many users all share an
#: address, and refusing those is a worse failure than the one being
#: prevented.
DCR_MAX_PER_IP_PER_DAY = 30
#: Registrations from everyone in a rolling day. A real day on this server is
#: single digits, so this number looks absurd -- and that is the point. A
#: global cap set anywhere near real traffic is not a defence, it is a lever:
#: whoever can vary the address the per-address cap is keyed on walks the
#: global counter up in minutes, and from then on every legitimate
#: `/oauth/register` -- a new Claude, Cursor or Smithery user -- gets 429 for
#: a day. The cheap defence would have become a denial of service with a
#: 24-hour tail. So the per-address cap stays as the one that bites, this one
#: is only a backstop against a table growing without bound, and the number
#: that gets a human's attention is the WARN threshold below, which logs and
#: refuses nothing.
DCR_MAX_PER_DAY = 5_000
#: Registrations in a rolling day that mean "look at this". Not a refusal:
#: crossing it logs, once per registration past the line, and that is all.
DCR_WARN_PER_DAY = 500
#: A registration that no human has approved within this long is litter.
#: Seven days, not one: MCP clients commonly register when they are installed
#: and are authorized whenever the person next opens the app, and a sweep
#: that runs a day after registration deletes rows that were about to be
#: used. The row is a name and a redirect URI; keeping it a week costs
#: nothing next to signing somebody out mid-flow.
STALE_CLIENT_SECONDS = 7 * 24 * 3600
#: How often one instance will spend two DELETEs on housekeeping.
SWEEP_INTERVAL_SECONDS = 15 * 60


#: When this instance last swept. Module-level because it is a property of
#: the process, not of one product's OAuthSupport -- both products share one
#: database and sweeping it twice is wasted work.
_SWEEP_STATE = {"last": 0.0}


def reset_sweep_clock() -> None:
    """Make the next registration sweep. For tests and for a fresh process."""
    _SWEEP_STATE["last"] = 0.0


# ── the refresh-retry grace window ───────────────────────────────────────
# Rotation plus reuse detection is the right shape (OAuth 2.1 §4.14.2) and it
# has one ugly edge: a client whose refresh response never arrived retries
# the token it still has, we see a rotated token coming back, and the whole
# family dies. The user is signed out by a dropped packet.
#
# So the FIRST replay of the token we just rotated, within a few seconds and
# from the same client, is answered with the pair that request already
# produced -- an idempotent retry, not a new grant. It creates no token, and
# it is a race an attacker cannot rely on: they would have to present a
# stolen refresh token inside the same ten seconds as the honest client's
# retry, and the honest client's pair is what they would get. Anything later,
# or a second replay, is the real thing and still kills the family.
#
#: How long a rotated refresh token may come back and be answered instead of
#: revoked.
REFRESH_REPLAY_GRACE_SECONDS = 10
#: Per PROCESS, and deliberately not in the database: this is a nicety for a
#: retry that happens milliseconds later, and a Vercel instance that does not
#: have the entry simply falls through to reuse detection, which is the
#: conservative answer. Nothing is weakened by a miss.
_REPLAY_MAX = 512
_REPLAY: dict[str, tuple[float, str, dict[str, Any]]] = {}


def reset_replay_grace() -> None:
    """Forget every in-flight retry. For tests and for a fresh process."""
    _REPLAY.clear()


def _remember_rotation(
    token_hash: str, client_id: str, issued: dict[str, Any], now: float
) -> None:
    if len(_REPLAY) >= _REPLAY_MAX:
        for key, (deadline, _, _) in list(_REPLAY.items()):
            if deadline <= now:
                del _REPLAY[key]
        if len(_REPLAY) >= _REPLAY_MAX:
            # A dictionary that grows without limit is its own denial of
            # service, and losing the grace window only costs a retry.
            _REPLAY.clear()
    _REPLAY[token_hash] = (now + REFRESH_REPLAY_GRACE_SECONDS, client_id, issued)


def _take_replay(
    token_hash: str, client_id: str, now: float
) -> dict[str, Any] | None:
    """The pair this token already produced, once, inside the window."""
    entry = _REPLAY.pop(token_hash, None)  # popped: once, whatever happens
    if entry is None:
        return None
    deadline, owner, issued = entry
    if now >= deadline or not hmac.compare_digest(owner, client_id):
        return None
    return issued


def _cap(name: str, default: int) -> int:
    """A registration cap, overridable by env so ops can loosen one without
    a deploy. A malformed value keeps the default rather than turning the cap
    off, because "0" and "not a number" must not mean "unlimited"."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; keeping %d", name, raw, default)
        return default
    return value if value > 0 else default

#: Injected by the gate after a token validates, and STRIPPED from every
#: inbound request before anything else runs. The strip is what makes the
#: injection trustworthy: without it, any caller could send these headers to
#: plain `/mcp` and be served from a stranger's stored key.
def header_safe(value: str) -> str:
    """A value fit to be an HTTP header.

    The `sub`, provider and client id all come out of our own database, so
    this is belt-and-braces -- but they are ATTACKER-INFLUENCED in the sense
    that a Google account id and a registered client id both originate
    outside this process, and a CR or LF in one would be a response-splitting
    bug in the layer below. Printable ASCII only, truncated.
    """
    return "".join(ch for ch in value if " " <= ch <= "~")[:256]


SUBJECT_HEADER = "x-fp-oauth-subject"
PROVIDER_HEADER = "x-fp-oauth-provider"
CLIENT_HEADER = "x-fp-oauth-client"
#: The address the token was approved by, so a signed-in caller with no key
#: can be told which account they are signed in as. Stripped from inbound
#: requests with the rest: it is displayed back to a user, and a value a
#: caller could set is a value a caller could use to make our own reply lie.
EMAIL_HEADER = "x-fp-oauth-email"
IDENTITY_HEADERS = (SUBJECT_HEADER, PROVIDER_HEADER, CLIENT_HEADER, EMAIL_HEADER)


class OAuthError(Exception):
    """One OAuth 2.0 error, with the code the RFC names.

    `redirectable` says whether the client's `redirect_uri` has been
    validated yet. It has not for a bad `client_id` or a bad `redirect_uri`,
    and RFC 6749 §4.1.2.1 is explicit that those two must NOT be redirected --
    doing so turns the authorization endpoint into an open redirector.
    """

    def __init__(
        self, code: str, description: str = "", *, redirectable: bool = True,
        status: int = 400, retry_after: int = 0,
    ) -> None:
        super().__init__(description or code)
        self.code = code
        self.description = description
        self.redirectable = redirectable
        self.status = status
        #: Seconds, for a `Retry-After` header. Only a refusal that a caller
        #: can usefully repeat later sets it.
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, str]:
        body = {"error": self.code}
        if self.description:
            body["error_description"] = self.description
        return body


# ── tokens ───────────────────────────────────────────────────────────────


def mint(prefix: str) -> str:
    """One opaque secret. 32 bytes of urandom, URL-safe, prefixed."""
    return prefix + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """S256, exactly as RFC 7636 §4.6 defines it."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def bearer_token(authorization: str | None) -> str:
    """The token out of an `Authorization: Bearer …` header, or "".

    Case-insensitive on the scheme, because RFC 7235 says the scheme is
    case-insensitive and at least one MCP client sends `bearer`.
    """
    raw = (authorization or "").strip()
    if not raw:
        return ""
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


# ── the feature, assembled ───────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthSupport:
    """Everything the OAuth endpoints and the /mcp/oauth gate need.

    Built once per product in `build_server`, or not at all. `None` is the
    normal state: a deployment that has not been given the day-1 variables
    registers nothing and changes no request path.
    """

    store: OAuthStore
    auth: Any               # webauth.GoogleWebAuth -- the browser sign-in
    origin: str             # this deployment's canonical scheme+host
    product: str
    #: freeusers.FreeUserStore -- the row per signed-in account. Not a
    #: credential store: see that module's header for what is and is not in
    #: it, and why nothing in it is encrypted.
    users: Any = None
    #: maillist.MailConfig -- the welcome note and the phase-2 contact sync.
    mail: Any = None

    # ── forms ────────────────────────────────────────────────────────────

    def csrf(self, sub: str) -> str:
        """A per-session form token.

        The session cookie is SameSite=Lax, which already stops a cross-site
        POST from carrying it. This is the second lock and costs one hmac: a
        hidden field bound to the signed-in account, so a form replayed for a
        different account is rejected on its value rather than on its cookie.
        """
        return hmac.new(
            self.auth.session_secret, f"csrf:{sub}".encode(), hashlib.sha256
        ).hexdigest()[:32]

    def csrf_ok(self, sub: str, given: str) -> bool:
        return bool(given) and hmac.compare_digest(self.csrf(sub), given)

    # ── identity of this authorization server / resource ─────────────────

    @property
    def issuer(self) -> str:
        return self.origin.rstrip("/")

    @property
    def resource_url(self) -> str:
        """The canonical protected resource: `/mcp`, the URL we publish.

        One resource identifier, not two, even though two paths reach it.
        A token's audience is a claim about which SERVER it may be spent at,
        and `/mcp` and `/mcp/oauth` are the same server, the same tools and
        the same account. Minting two audiences would mean a token obtained
        through the alias failed on the primary path, which is a bug the user
        experiences as "sign-in worked and then nothing works".
        """
        return f"{self.issuer}{MCP_PATH}"

    def resource_metadata_url(self, path: str = MCP_PATH) -> str:
        """The RFC 9728 document for one of the two paths.

        Path-scoped, because a client that read a 401 challenge follows the
        URL in it literally and RFC 9728 3.1 builds that URL by inserting the
        resource's path after the well-known segment. The unscoped path is
        served as well, for clients that construct it from the origin alone.
        Both documents name the same `resource`; only the URL differs, so a
        client that discovered us either way ends up asking for the same
        audience.
        """
        return f"{self.issuer}{PROTECTED_RESOURCE_PATH}{path}"

    # ── metadata documents ───────────────────────────────────────────────

    def protected_resource_metadata(self) -> dict[str, Any]:
        """RFC 9728. What resource this is and who can authorise it.

        Served for `/mcp` as well as for `/mcp/oauth`, which is what makes a
        client offering "authorization: always" sign the user in BEFORE the
        first call rather than after the tenth. A client that does not look
        is not broken: it gets the anonymous taster allowance and the
        challenge when it runs out.
        """
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.issuer],
            "scopes_supported": [DEFAULT_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": f"FlightPowers free {self.product} MCP",
            "resource_documentation": DOCS_URL,
            "resource_policy_uri": PRIVACY_URL,
            "resource_tos_uri": TERMS_URL,
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        """RFC 8414. Note `code_challenge_methods_supported` is S256 only:
        `plain` is in the RFC and is not offered, because a challenge that is
        the verifier protects nothing."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}{AUTHORIZE_PATH}",
            "token_endpoint": f"{self.issuer}{TOKEN_PATH}",
            "registration_endpoint": f"{self.issuer}{REGISTER_PATH}",
            "revocation_endpoint": f"{self.issuer}{REVOKE_PATH}",
            "scopes_supported": [DEFAULT_SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "revocation_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256"],
            # A client_id that is an https URL we can fetch, instead of one
            # we minted. Smithery asks for this before it will proxy a remote
            # OAuth server, and it is how a client avoids leaving a row in
            # our table per install. DCR is still offered above; this is an
            # additional shape, not a replacement. See src/cimd.py.
            "client_id_metadata_document_supported": True,
            "service_documentation": DOCS_URL,
            "op_policy_uri": PRIVACY_URL,
            "op_tos_uri": TERMS_URL,
        }

    # ── resource indicator (RFC 8707) ────────────────────────────────────

    def resource_matches(self, requested: str) -> bool:
        """Whether a client's `resource=` names this server.

        Deliberately forgiving about the path and the trailing slash, strict
        about the origin. Clients in the wild send the origin, the `/mcp`
        path and the `/mcp/oauth` path for the same server, and rejecting two
        of those would break a flow over a formatting difference. Rejecting a
        different HOST is the part that matters: that is the confused-deputy
        case RFC 8707 exists for.
        """
        if not requested:
            return True
        want = urlsplit(self.resource_url)
        got = urlsplit(requested.strip())
        if not got.scheme or not got.netloc:
            return False
        if (got.scheme.lower(), got.netloc.lower()) != (
            want.scheme.lower(),
            want.netloc.lower(),
        ):
            return False
        path = got.path.rstrip("/")
        return path in ("", MCP_PATH, MCP_OAUTH_PATH)

    # ── dynamic client registration ──────────────────────────────────────

    # ── the client behind a client_id ─────────────────────────────────────

    async def lookup_client(
        self, client_id: str, redirect_uri: str = ""
    ) -> OAuthClient | None:
        """The client for this `client_id`, from our table or from its URL.

        Two shapes, told apart by the id itself: `fpcl_…` is a row we wrote
        at registration, an https URL is a Client ID Metadata Document we
        fetch and validate now (src/cimd.py). Nothing else is accepted, so a
        client that registered the old way keeps behaving exactly as it did.

        `redirect_uri` is checked against the DOCUMENT here rather than left
        to the caller, because for a CIMD client the document is the only
        registration there is: skipping it would let anyone who knows a CIMD
        URL have that client's codes delivered somewhere else.
        """
        if not cimd.is_cimd_client_id(client_id):
            return await self.store.get_client(client_id)
        try:
            document = await cimd.load(client_id)
            uris = cimd.redirect_uris(document)
        except cimd.CimdError as exc:
            logger.info("CIMD client_id %s refused: %s", client_id, exc)
            raise OAuthError(
                "invalid_client", str(exc), redirectable=False, status=400
            ) from exc
        allowed = tuple(u for u in uris if _redirect_uri_allowed(u))
        if not allowed:
            raise OAuthError(
                "invalid_client",
                "the client metadata document lists no usable redirect_uris",
                redirectable=False,
            )
        if redirect_uri and redirect_uri not in allowed:
            raise OAuthError(
                "invalid_request",
                "that redirect_uri is not listed in the client metadata document",
                redirectable=False,
            )
        return OAuthClient(
            client_id=client_id,
            client_name=cimd.client_name(document, client_id),
            redirect_uris=allowed,
            token_endpoint_auth_method="none",
            scope=str(document.get("scope") or DEFAULT_SCOPE),
            client_secret_hash="",
            created_at=time.time(),
            metadata={},
            ephemeral=True,
        )

    # ── dynamic client registration ──────────────────────────────────────

    async def _sweep(self, now: float) -> None:
        """Housekeeping, at most once every SWEEP_INTERVAL per instance.

        Hung off registration rather than a cron because this deployment has
        no scheduler and adding one for two DELETEs would be the bigger
        change. Registration is the only endpoint that GROWS the tables, so
        it is the honest place to pay for cleaning them.

        Best effort throughout: a sweep that fails must never turn a valid
        registration into an error.
        """
        if now - _SWEEP_STATE["last"] < SWEEP_INTERVAL_SECONDS:
            return
        # Stamped BEFORE the work, so a store that is failing does not get a
        # sweep attempt per registration.
        _SWEEP_STATE["last"] = now
        try:
            expired = await self.store.purge_expired(now)
            stale = await self.store.purge_stale_clients(now - STALE_CLIENT_SECONDS)
        except OAuthStoreError as exc:
            logger.warning("OAuth sweep failed: %s", exc)
            return
        if expired or stale:
            logger.info(
                "OAuth sweep removed %d expired code(s)/token(s) and %d "
                "unused client registration(s)",
                expired,
                stale,
            )

    async def _check_registration_caps(self, ip: str, now: float) -> None:
        """The durable half of the registration limit.

        Counted in Postgres, so every Vercel instance sees the same number --
        unlike the per-instance rate limiter, which is the cheap first line.
        Both are needed: the rate limiter stops a burst, this stops a slow
        drip that would otherwise fill the table over a day.
        """
        since = now - 24 * 3600
        per_day = _cap("MCP_OAUTH_DCR_MAX_PER_DAY", DCR_MAX_PER_DAY)
        per_ip = _cap("MCP_OAUTH_DCR_MAX_PER_IP_PER_DAY", DCR_MAX_PER_IP_PER_DAY)
        warn_per_day = _cap("MCP_OAUTH_DCR_WARN_PER_DAY", DCR_WARN_PER_DAY)
        # `unknown` is the shared bucket for callers whose address the
        # platform did not give us (`ratelimit.client_ip`). Counting a
        # durable per-address cap against it would mean one missing header on
        # the edge locks every registration on the server out for a day, so
        # it is treated as "no address": the rate limiter still buckets them
        # together, and the global backstop still applies.
        if ip and ip != UNKNOWN_IP:
            from_here = await self.store.count_clients_since(since, ip)
            if from_here >= per_ip:
                logger.warning(
                    "registration cap: %s has registered %d client(s) today",
                    ip,
                    from_here,
                )
                raise OAuthError(
                    "temporarily_unavailable",
                    "too many client registrations from this address today; "
                    "try again later",
                    status=429,
                    retry_after=3600,
                )
        total = await self.store.count_clients_since(since)
        if warn_per_day <= total < per_day:
            logger.warning(
                "registration volume: %d client registration(s) in the last "
                "24h, above the %d that a normal day looks like",
                total,
                warn_per_day,
            )
        if total >= per_day:
            logger.warning("registration cap: %d registrations today", total)
            raise OAuthError(
                "temporarily_unavailable",
                "this server is not accepting new client registrations right "
                "now; try again later",
                status=429,
                retry_after=3600,
            )

    async def register(
        self, body: dict[str, Any], ip: str = "", now: float | None = None
    ) -> dict[str, Any]:
        """RFC 7591. Returns the registration response to send back.

        Open registration, which the MCP spec requires and which is safe
        here for one reason worth stating: a `client_id` authorises nothing.
        Every flow through it still ends at a consent page that a human has
        to be signed into Google to see and has to press a button on. The
        row is a name and a redirect URI, not a permission.

        Open, capped and swept: see `_check_registration_caps` and `_sweep`.
        """
        now = now if now is not None else time.time()
        await self._sweep(now)
        await self._check_registration_caps(ip, now)
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            raise OAuthError(
                "invalid_redirect_uri", "redirect_uris must be a non-empty list"
            )
        cleaned: list[str] = []
        for raw in uris:
            if not isinstance(raw, str) or not raw.strip():
                raise OAuthError("invalid_redirect_uri", "a redirect_uri was empty")
            uri = raw.strip()
            if not _redirect_uri_allowed(uri):
                raise OAuthError(
                    "invalid_redirect_uri",
                    f"redirect_uri {uri!r} is not an https URL, a loopback "
                    "http URL, or a private-use scheme",
                )
            cleaned.append(uri)

        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list):
            raise OAuthError("invalid_client_metadata", "grant_types must be a list")
        unsupported = set(grant_types) - {"authorization_code", "refresh_token"}
        if unsupported:
            raise OAuthError(
                "invalid_client_metadata",
                f"unsupported grant_types: {', '.join(sorted(unsupported))}",
            )
        response_types = body.get("response_types") or ["code"]
        if isinstance(response_types, list) and set(response_types) - {"code"}:
            raise OAuthError(
                "invalid_client_metadata", "only response_type=code is supported"
            )

        method = str(body.get("token_endpoint_auth_method") or "none").strip()
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError(
                "invalid_client_metadata",
                f"token_endpoint_auth_method {method!r} is not supported",
            )

        client_id = mint(CLIENT_ID_PREFIX)
        secret = "" if method == "none" else mint("fps_")
        client = OAuthClient(
            client_id=client_id,
            client_name=str(body.get("client_name") or "an MCP client")[:120],
            redirect_uris=tuple(cleaned),
            token_endpoint_auth_method=method,
            scope=str(body.get("scope") or DEFAULT_SCOPE),
            client_secret_hash=hash_secret(secret) if secret else "",
            created_at=now,
            registered_ip=(ip or "")[:64],
            metadata={
                k: v
                for k, v in body.items()
                if k in ("client_uri", "logo_uri", "software_id", "software_version")
                and isinstance(v, str)
            },
        )
        await self.store.register_client(client)
        logger.info(
            "registered MCP client %s (%s)", client_id, client.client_name
        )
        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(client.created_at),
            "client_name": client.client_name,
            "redirect_uris": list(client.redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": client.scope,
        }
        if secret:
            # Returned once and never again -- only its hash is stored, so
            # there is nothing to re-issue it from. `0` means "does not
            # expire", per RFC 7591 §3.2.1.
            response["client_secret"] = secret
            response["client_secret_expires_at"] = 0
        return response

    # ── the authorization request ────────────────────────────────────────

    async def read_authorize_request(
        self, params: dict[str, str]
    ) -> tuple[OAuthClient, dict[str, str]]:
        """Validate `/connect/authorize` query parameters.

        Raises OAuthError with `redirectable=False` for the two failures that
        must render a page instead of bouncing (unknown client, unregistered
        redirect_uri), and with `redirectable=True` for everything after
        that.
        """
        client_id = (params.get("client_id") or "").strip()
        if not client_id:
            raise OAuthError(
                "invalid_request", "client_id is missing", redirectable=False
            )
        try:
            client = await self.lookup_client(
                client_id, (params.get("redirect_uri") or "").strip()
            )
        except OAuthStoreError as exc:
            logger.warning("client lookup failed: %s", exc)
            raise OAuthError(
                "temporarily_unavailable",
                "The sign-in store is not reachable right now.",
                redirectable=False,
                status=503,
            ) from exc
        if client is None:
            raise OAuthError(
                "invalid_client",
                "That client is not registered with this server.",
                redirectable=False,
            )

        redirect_uri = (params.get("redirect_uri") or "").strip()
        if not redirect_uri:
            if len(client.redirect_uris) != 1:
                raise OAuthError(
                    "invalid_request",
                    "redirect_uri is required when a client registered more "
                    "than one",
                    redirectable=False,
                )
            redirect_uri = client.redirect_uris[0]
        elif redirect_uri not in client.redirect_uris:
            # Exact string match, not a prefix or a host match: a loose
            # comparison here is how authorization codes get delivered to
            # somebody else's URL.
            raise OAuthError(
                "invalid_request",
                "That redirect_uri is not registered for this client.",
                redirectable=False,
            )

        if (params.get("response_type") or "").strip() != "code":
            raise OAuthError(
                "unsupported_response_type", "only response_type=code is supported"
            )

        challenge = (params.get("code_challenge") or "").strip()
        method = (params.get("code_challenge_method") or "").strip()
        if not challenge:
            raise OAuthError(
                "invalid_request",
                "PKCE is required: send code_challenge with "
                "code_challenge_method=S256",
            )
        if method != "S256":
            raise OAuthError(
                "invalid_request",
                "code_challenge_method must be S256; plain is not accepted",
            )

        resource = (params.get("resource") or "").strip()
        if not self.resource_matches(resource):
            raise OAuthError(
                "invalid_target",
                f"this server is {self.resource_url}, not {resource}",
            )

        return client, {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "scope": (params.get("scope") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
            "state": params.get("state") or "",
            "resource": resource or self.resource_url,
        }

    # ── the consent form's signed blob ───────────────────────────────────

    def seal_request(self, request: dict[str, str], sub: str, now: float | None = None) -> str:
        """The validated authorization request, signed, for the hidden field.

        Signed rather than re-read from the form on POST: everything in it has
        already been checked against the registered client, and re-validating
        a value the browser could have edited is how a consent page ends up
        approving a request the user never saw. Bound to `sub` as well, so a
        blob minted for one account cannot be posted by another.
        """
        payload = dict(request)
        payload["typ"] = "authz"
        payload["sub"] = sub
        payload["exp"] = int((now if now is not None else time.time())) + CONSENT_TTL_SECONDS
        return sign_payload(payload, self.auth.session_secret)

    def open_request(self, sealed: str, sub: str, now: float | None = None) -> dict[str, str]:
        payload = verify_payload(sealed, self.auth.session_secret, now=now)
        if payload.get("typ") != "authz":
            raise WebAuthError("wrong token type")
        if not hmac.compare_digest(str(payload.get("sub", "")), sub):
            raise WebAuthError("this form belongs to a different account")
        return {
            k: str(v)
            for k, v in payload.items()
            if k in ("client_id", "redirect_uri", "code_challenge", "scope", "state", "resource")
        }

    # ── issuing ──────────────────────────────────────────────────────────

    async def issue_code(
        self,
        request: dict[str, str],
        sub: str,
        now: float | None = None,
        email: str = "",
    ) -> str:
        now = now if now is not None else time.time()
        code = mint(CODE_PREFIX)
        await self.store.put_code(
            AuthCode(
                code_hash=hash_secret(code),
                client_id=request["client_id"],
                redirect_uri=request["redirect_uri"],
                code_challenge=request["code_challenge"],
                scope=request.get("scope", DEFAULT_SCOPE),
                user_sub=sub,
                provider=PROVIDER_GOOGLE,
                resource=request.get("resource", self.resource_url),
                expires_at=now + CODE_TTL_SECONDS,
                user_email=email,
            )
        )
        # This registration has now been approved by a human, so the sweep
        # must never take it. A CIMD client has no row to stamp.
        if not cimd.is_cimd_client_id(request["client_id"]):
            try:
                await self.store.mark_client_authorized(request["client_id"], now)
            except OAuthStoreError as exc:
                # Bookkeeping, not the grant. The NOT EXISTS clauses in the
                # sweep already protect a client that has a code or a token.
                logger.warning("could not stamp client as authorized: %s", exc)
        return code

    async def note_consent_shown(
        self, client_id: str, now: float | None = None
    ) -> None:
        """A consent page for this client is being put in front of a human.

        Same stamp the approval writes, moved earlier for one reason: the
        sweep deletes registrations with no code, no token and no stamp, and
        until this existed the only stamp happened when Approve was pressed.
        A client that registered at install and signs in a week later spent
        the whole consent page inside a window where the sweep could take its
        row -- and the exchange that followed would fail `invalid_client`
        with nothing in the logs naming the cause.

        Best effort, like the stamp in `issue_code`: this is bookkeeping, and
        a store hiccup must not stop a page rendering. A CIMD client has no
        row to stamp.
        """
        if not client_id or cimd.is_cimd_client_id(client_id):
            return
        try:
            await self.store.mark_client_authorized(
                client_id, now if now is not None else time.time()
            )
        except OAuthStoreError as exc:
            logger.warning("could not stamp client at the consent page: %s", exc)

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        user_sub: str,
        provider: str,
        scope: str,
        resource: str,
        now: float,
        email: str = "",
        family_id: str = "",
    ) -> dict[str, Any]:
        access = mint(ACCESS_TOKEN_PREFIX)
        refresh = mint(REFRESH_TOKEN_PREFIX)
        # One family per authorization, carried across every rotation. It is
        # what makes "revoke the whole line" a single statement when a
        # rotated refresh token comes back.
        family = family_id or new_family()
        await self.store.put_token(
            TokenRecord(
                token_hash=hash_secret(access),
                kind="access",
                client_id=client_id,
                user_sub=user_sub,
                provider=provider,
                scope=scope,
                resource=resource,
                expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
                user_email=email,
                family_id=family,
            )
        )
        await self.store.put_token(
            TokenRecord(
                token_hash=hash_secret(refresh),
                kind="refresh",
                client_id=client_id,
                user_sub=user_sub,
                provider=provider,
                scope=scope,
                resource=resource,
                expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
                user_email=email,
                family_id=family,
            )
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": scope,
        }

    # ── the token endpoint ───────────────────────────────────────────────

    async def _authenticate_client(
        self, form: dict[str, str], authorization: str | None
    ) -> OAuthClient:
        """Whichever of the three registered methods this client uses.

        A public client proves nothing here; PKCE is what stands in for a
        secret, and this server requires it from every client, so a public
        client is not a weaker case -- it is the normal one.
        """
        client_id = (form.get("client_id") or "").strip()
        client_secret = (form.get("client_secret") or "").strip()

        raw = (authorization or "").strip()
        if raw.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(raw[6:].strip() + "==").decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
                raise OAuthError(
                    "invalid_client", "malformed Basic credentials", status=401
                ) from exc
            basic_id, _, basic_secret = decoded.partition(":")
            # Body and header disagreeing is a client bug worth naming rather
            # than silently preferring one.
            if client_id and basic_id and client_id != basic_id:
                raise OAuthError(
                    "invalid_client",
                    "client_id in the body and in the Authorization header "
                    "disagree",
                    status=401,
                )
            client_id = client_id or basic_id
            client_secret = client_secret or basic_secret

        if not client_id:
            raise OAuthError("invalid_client", "client_id is missing", status=401)
        try:
            client = await self.lookup_client(client_id)
        except OAuthError as exc:
            # A CIMD document that stopped resolving between authorize and
            # the exchange. `invalid_client` with the document's reason, at
            # 401 like every other client-authentication failure here.
            raise OAuthError(
                "invalid_client", exc.description or exc.code, status=401
            ) from exc
        except OAuthStoreError as exc:
            logger.warning("client lookup failed at the token endpoint: %s", exc)
            raise OAuthError(
                "temporarily_unavailable",
                "The sign-in store is not reachable right now.",
                status=503,
            ) from exc
        if client is None:
            raise OAuthError("invalid_client", "unknown client_id", status=401)
        if client.is_public:
            return client
        if not client_secret or not hmac.compare_digest(
            client.client_secret_hash, hash_secret(client_secret)
        ):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
        return client

    async def token(
        self,
        form: dict[str, str],
        authorization: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        client = await self._authenticate_client(form, authorization)
        grant = (form.get("grant_type") or "").strip()
        if grant == "authorization_code":
            return await self._authorization_code_grant(client, form, now)
        if grant == "refresh_token":
            return await self._refresh_token_grant(client, form, now)
        raise OAuthError(
            "unsupported_grant_type",
            f"grant_type {grant!r} is not supported; use authorization_code "
            "or refresh_token",
        )

    async def _authorization_code_grant(
        self, client: OAuthClient, form: dict[str, str], now: float
    ) -> dict[str, Any]:
        code = (form.get("code") or "").strip()
        if not code:
            raise OAuthError("invalid_request", "code is missing")
        # Consumed BEFORE anything is checked. A code presented with a wrong
        # verifier has still been presented, and leaving it alive would let
        # an attacker who intercepted it keep guessing.
        record = await self.store.consume_code(hash_secret(code))
        if record is None:
            raise OAuthError(
                "invalid_grant", "that code is unknown, already used, or expired"
            )
        if now >= record.expires_at:
            raise OAuthError("invalid_grant", "that code has expired")
        if record.client_id != client.client_id:
            raise OAuthError("invalid_grant", "that code was issued to another client")

        redirect_uri = (form.get("redirect_uri") or "").strip()
        if redirect_uri and redirect_uri != record.redirect_uri:
            raise OAuthError(
                "invalid_grant", "redirect_uri does not match the authorization request"
            )

        verifier = (form.get("code_verifier") or "").strip()
        if not verifier:
            raise OAuthError("invalid_grant", "code_verifier is missing")
        if not hmac.compare_digest(pkce_challenge(verifier), record.code_challenge):
            raise OAuthError("invalid_grant", "code_verifier does not match the challenge")

        resource = (form.get("resource") or "").strip()
        if resource and not self.resource_matches(resource):
            raise OAuthError("invalid_target", "resource does not name this server")

        return await self._issue_tokens(
            client_id=client.client_id,
            user_sub=record.user_sub,
            provider=record.provider,
            scope=record.scope,
            resource=record.resource,
            now=now,
            email=record.user_email,
        )

    async def _refresh_token_grant(
        self, client: OAuthClient, form: dict[str, str], now: float
    ) -> dict[str, Any]:
        presented = (form.get("refresh_token") or "").strip()
        if not presented:
            raise OAuthError("invalid_request", "refresh_token is missing")
        presented_hash = hash_secret(presented)
        record = await self.store.get_token(presented_hash, "refresh", now=now)
        if record is None:
            # Before assuming theft: the same client asking again, seconds
            # after we rotated this token, is a retry of a response it never
            # received. Answer it with the pair that request produced. See
            # REFRESH_REPLAY_GRACE_SECONDS.
            retry = _take_replay(presented_hash, client.client_id, now)
            if retry is not None:
                logger.info(
                    "refresh retry inside the grace window for client %s; "
                    "returning the pair that rotation already issued",
                    client.client_id,
                )
                return retry
            await self._detect_reuse(presented_hash, client, now)
            raise OAuthError(
                "invalid_grant", "that refresh token is unknown, revoked or expired"
            )
        if record.client_id != client.client_id:
            raise OAuthError(
                "invalid_grant", "that refresh token belongs to another client"
            )
        scope = (form.get("scope") or record.scope).strip() or record.scope
        if scope != record.scope:
            # RFC 6749 §6: a refresh may narrow scope, never widen it. With
            # one scope there is nothing to narrow to, so anything different
            # is a request for something that was not granted.
            raise OAuthError("invalid_scope", "a refresh cannot change the scope")
        issued = await self._issue_tokens(
            client_id=client.client_id,
            user_sub=record.user_sub,
            provider=record.provider,
            scope=record.scope,
            resource=record.resource,
            now=now,
            email=record.user_email,
            family_id=record.family_id,
        )
        # Rotation: the presented refresh token stops working here. Rotated
        # after the new pair is written, so a crash in between leaves the
        # user with a token that still works rather than none at all.
        #
        # STAMPED, not deleted (day 3). A deleted row and a token that never
        # existed are indistinguishable, and the difference is the whole
        # signal: a rotated refresh token coming back means either a client
        # that lost the response or a copy in somebody else's hands, and
        # OAuth 2.1 §4.14.2 says to assume the second.
        await self.store.rotate_token(record.token_hash, now)
        _remember_rotation(record.token_hash, client.client_id, issued, now)
        return issued

    async def _detect_reuse(
        self, token_hash: str, client: OAuthClient, now: float
    ) -> None:
        """A refresh token that was already rotated, presented again.

        The response is the same `invalid_grant` either way -- this is about
        what happens to the OTHER tokens. Every token descended from that one
        authorization is deleted, so an attacker replaying a stolen refresh
        token cannot keep the access token they got with it, and the real
        user's next call fails in a way that makes them sign in again.

        The honest retry -- a client asking again for a response it never
        received -- is caught before this function runs, by the few-second
        grace window in `_refresh_token_grant`. What reaches here is a
        rotated token coming back late, or coming back twice, and there the
        safe reading is theft: a stolen refresh token is indistinguishable
        from a retried one, and OAuth 2.1 §4.14.2 says to assume the first.
        """
        try:
            stale = await self.store.get_token_any(token_hash, "refresh")
        except OAuthStoreError as exc:
            logger.warning("reuse check could not read the store: %s", exc)
            return
        if stale is None or stale.revoked_at is None:
            return
        logger.warning(
            "refresh token reuse detected for client %s (sub=%s); revoking "
            "the whole token family",
            client.client_id,
            stale.user_sub,
        )
        try:
            dropped = await self.store.revoke_family(stale.family_id)
        except OAuthStoreError as exc:
            logger.warning("could not revoke the token family: %s", exc)
            return
        if dropped:
            logger.warning("revoked %d token(s) after refresh reuse", dropped)

    async def revoke(self, form: dict[str, str], authorization: str | None = None) -> None:
        """RFC 7009. Always succeeds from the client's point of view.

        The RFC is explicit (§2.2): an invalid or already-revoked token gets
        200, because telling a caller which tokens exist is an oracle and
        "the token is not valid" is the outcome they asked for anyway.
        """
        try:
            client = await self._authenticate_client(form, authorization)
        except OAuthError:
            # A revoke with bad client credentials still must not tell the
            # caller anything. Nothing is revoked; nothing is disclosed.
            return
        token = (form.get("token") or "").strip()
        if not token:
            return
        token_hash = hash_secret(token)
        kind = "refresh" if token.startswith(REFRESH_TOKEN_PREFIX) else "access"
        try:
            # RFC 7009 §2.1: the server "validates whether the token was
            # issued to the client making the revocation request". Without
            # that check, any client holding somebody else's token can sign
            # that user out -- and since revoking a refresh token now takes
            # the whole family, the blast radius is a whole authorization
            # rather than one token. The answer stays 200 either way: §2.2's
            # silence rule does not stop applying because we said no.
            record = await self.store.get_token_any(token_hash, kind)
            if record is not None and record.client_id != client.client_id:
                logger.warning(
                    "client %s tried to revoke a token issued to %s",
                    client.client_id,
                    record.client_id,
                )
                return
            # Revoking a refresh token SHOULD revoke the access tokens issued
            # with it. The family id is exactly that set, so one statement
            # does it -- and it also means a client that logs out cannot
            # leave a live access token behind.
            family = (
                record.family_id
                if record is not None and kind == "refresh"
                else ""
            )
            if family:
                await self.store.revoke_family(family)
            else:
                await self.store.revoke_token(token_hash)
        except OAuthStoreError as exc:
            logger.warning("revocation failed: %s", exc)

    # ── the resource server ──────────────────────────────────────────────

    async def validate_access_token(
        self, token: str, now: float | None = None
    ) -> TokenRecord | None:
        if not token or not token.startswith(ACCESS_TOKEN_PREFIX):
            return None
        try:
            record = await self.store.get_token(hash_secret(token), "access", now=now)
        except OAuthStoreError as exc:
            # A database outage is not "your token is bad", but there is no
            # way to serve the request without the lookup, so the caller gets
            # the same 401 and we get the log line.
            logger.warning("access token lookup failed: %s", exc)
            return None
        if record is None:
            return None
        # AUDIENCE. Both products share one deployment, one database and one
        # stored RapidAPI key per user, so a token approved on the flights
        # consent page ("search live flight fares") would otherwise be
        # accepted on the hotels hostname and spend the user's hotels
        # subscription -- a grant the user was never shown. MCP 2025-06-18
        # requires a resource server to check that a token was issued for it,
        # and RFC 8707 exists for exactly this confused-deputy case. The
        # `resource` column was already written down for this; this is where
        # it is read. An empty value can only come from a row predating the
        # column's default and is treated as unscoped, which is what
        # `resource_matches` already means by "".
        if not self.resource_matches(record.resource):
            logger.warning(
                "access token for %s presented at %s; refused",
                record.resource,
                self.resource_url,
            )
            return None
        return record

    def challenge_header(
        self, error: str = "", description: str = "", path: str = MCP_PATH
    ) -> str:
        """The `WWW-Authenticate` value for a 401 on `path`.

        `path` decides only which metadata URL is advertised -- the resource,
        the authorization server and the token audience are the same either
        way. A client challenged on `/mcp` is pointed at `/mcp`'s document so
        the URL it fetches is the one it asked about.
        """
        parts = [f'Bearer resource_metadata="{self.resource_metadata_url(path)}"']
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return ", ".join(parts)


def _redirect_uri_allowed(uri: str) -> bool:
    """What a registered redirect_uri may look like.

    https anywhere; http ONLY on loopback (RFC 8252 §7.3 -- this is how a
    desktop MCP client receives its code); and private-use schemes like
    `cursor://` or `vscode://`, which is how the other half of them do it.
    Plain http to a remote host is refused: a code delivered over cleartext
    to somewhere we cannot see is the one shape that is always a mistake.
    """
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http":
        host = (parts.hostname or "").lower()
        return host in ("127.0.0.1", "::1", "localhost")
    # A private-use scheme must be a real scheme, not a bare path.
    return bool(parts.scheme) and ":" in uri and not uri.startswith(("javascript:", "data:"))


def build_free_oauth(origin: str, product: str = "travel") -> "OAuthSupport | None":
    """The sign-in feature for this deployment, or None.

    Returns None -- and the server then behaves exactly as it does today,
    answering 404 on `/mcp/oauth` -- unless all three of these are set:
    `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and
    `DATABASE_URL`. That is the normal state until ops provisions them, and
    it is the rollback: unset one and the endpoint disappears with no deploy
    of new code. `MCP_OAUTH=off` does the same thing explicitly.

    Never raises. A free server that cannot reach Postgres must still serve
    flights to anonymous callers, because that is the product.
    """
    if os.environ.get("MCP_OAUTH", "").strip().lower() in {"0", "off", "false", "no"}:
        logger.info("MCP_OAUTH is off; /mcp/oauth stays unregistered.")
        return None
    auth = build_web_auth(origin)
    if auth is None:
        logger.info(
            "GOOGLE_OAUTH_CLIENT_ID/SECRET are not set; /mcp/oauth stays "
            "unregistered and /mcp is unchanged."
        )
        return None
    store = build_oauth_store()
    if not getattr(store, "available", False):
        logger.info(
            "DATABASE_URL is not set; /mcp/oauth stays unregistered and /mcp "
            "is unchanged."
        )
        return None
    from .freeusers import build_user_store  # noqa: PLC0415 -- flat import graph
    from .maillist import load_mail_config  # noqa: PLC0415

    return OAuthSupport(
        store=store,
        auth=auth,
        origin=origin.rstrip("/"),
        product=product,
        users=build_user_store(),
        mail=load_mail_config(origin.rstrip("/")),
    )


# ── the /mcp/oauth gate ──────────────────────────────────────────────────


def strip_identity_headers(scope: dict) -> dict:
    """Remove any inbound copy of the headers the gate injects.

    Unconditional, on every request, whether or not OAuth is configured and
    whatever the path. This is the half of the mechanism that makes the
    injected header trustworthy: `_resolve` in server.py serves a stored
    RapidAPI key on the strength of it, so a caller who could set it himself
    would be able to spend a stranger's plan.
    """
    headers = scope.get("headers") or []
    kept = [
        (name, value)
        for name, value in headers
        if name.decode("latin-1").lower() not in IDENTITY_HEADERS
    ]
    if len(kept) == len(headers):
        return scope
    scope = dict(scope)
    scope["headers"] = kept
    return scope


def _json_response(status: int, body: dict[str, Any], extra: dict[str, str] | None = None):
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    for name, value in (extra or {}).items():
        headers.append((name.encode("ascii"), value.encode("latin-1")))
    return payload, headers


class OAuthResourceGate:
    """ASGI wrapper: strip injected headers everywhere, resolve bearer tokens.

    Sits above the app rather than inside it, because the strip has to happen
    before ANY route sees a request.

    Three jobs. The first is installed even on a deployment with no sign-in
    configured, because the danger it removes does not depend on a flag:

    1. **Strip `x-fp-oauth-*` from every inbound request, whatever the path.**
       The tool layer counts a signed-in caller's fair use against the
       identity in that header and gives it the full allowance. A caller able
       to set the header himself would help himself to that allowance, and to
       somebody else's counter. Stripping it here, above everything, is what
       makes the injected value trustworthy.
    2. **`/mcp`** -- the URL we publish. A bearer token is RESOLVED here and
       becomes the injected identity; no token is answered 401 + the
       challenge, so every served call on the free server belongs to a Google
       account. `FREE_ANON_MODE=open` is the rollback and puts the old
       anonymous behaviour back, capped by `anon_gate.AnonCapMiddleware`.
    3. **`/mcp/oauth`** -- the same behaviour under the name printed in old
       guides and saved in connectors added before 2026-09-09, so nobody has
       to re-add a server. The path is rewritten to `/mcp` after the token
       validates, so it is literally the same tool registry and the same
       sponsored card, not a second copy.

    Nobody is exempt from the challenge, gateways included -- see the
    `FREE_ANON_MODE` note near the top of this module for why, and for what
    the `open` rollback does instead.
    """

    def __init__(self, app, support) -> None:
        self.app = app
        #: The OAuthSupport for this deployment, or None. A callable is
        #: accepted too, so a lazily-built server can hand one over later.
        self._support = support

    @property
    def support(self):
        return self._support() if callable(self._support) else self._support

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        scope = strip_identity_headers(scope)
        path = (scope.get("path") or "").rstrip("/") or "/"
        support = self.support

        if path == MCP_OAUTH_PATH:
            await self._guarded(scope, receive, send, support, mode="always")
            return
        if path == MCP_PATH:
            await self._guarded(scope, receive, send, support, mode="at_cap")
            return
        await self.app(scope, receive, send)

    async def _guarded(self, scope, receive, send, support, *, mode: str) -> None:
        """One handler, two doors.

            at_cap   /mcp        the URL we publish, and what the rest of the
                                 market does: a credential is served, none is
                                 served on the taster allowance, and the
                                 challenge arrives when that runs out. That
                                 is also what makes Smithery's release probe
                                 -- which arrives with no credential -- flip
                                 its listing into OAuth mode.
            always   /mcp/oauth  no token, no service. Kept for printed
                                 guides and for connectors already added on
                                 that URL, so nobody has to re-add a server.

        A token is resolved on both: a caller who signed in through the alias
        must not be second-class on the primary path, or the other way round.
        """
        always = mode == "always"
        path = MCP_OAUTH_PATH if always else MCP_PATH

        if support is None:
            if not always:
                # Sign-in is not configured on this deployment. `/mcp` is the
                # product and must keep answering exactly as it does today.
                await self.app(scope, receive, send)
                return
            payload, headers = _json_response(
                404,
                {
                    "error": "not_found",
                    "error_description": (
                        "This deployment does not have sign-in configured. "
                        "Use /mcp, which is open and needs no account."
                    ),
                },
            )
            await _send(send, 404, headers, payload)
            return

        headers = _scope_headers(scope)
        token = bearer_token(headers.get("authorization"))

        if not token:
            if always or anon_mode() == ANON_MODE_CHALLENGE:
                await _challenge(send, support, path, scope)
                return
            # The ordinary anonymous request. Served, under the taster cap,
            # which `anon_gate.AnonCapMiddleware` enforces further down the
            # stack where the counter store is reachable.
            await self.app(_rewrite(scope), receive, send)
            return

        record = await support.validate_access_token(token)
        if record is None:
            payload, out = _json_response(
                401,
                {
                    "error": "invalid_token",
                    "error_description": (
                        "That access token is unknown, expired or revoked. "
                        "Sign in again."
                    ),
                },
                {
                    "WWW-Authenticate": support.challenge_header(
                        "invalid_token",
                        "the access token is expired or revoked",
                        path,
                    )
                },
            )
            await _send(send, 401, out, payload)
            return

        # Authenticated. Rewrite to the real MCP route (a no-op on `/mcp`)
        # and hand the identity to the tool layer through headers it reads
        # off the live request.
        scope = _rewrite(scope)
        scope["headers"] = list(scope.get("headers") or []) + [
            (SUBJECT_HEADER.encode("ascii"), header_safe(record.user_sub).encode("ascii")),
            (PROVIDER_HEADER.encode("ascii"), header_safe(record.provider).encode("ascii")),
            (CLIENT_HEADER.encode("ascii"), header_safe(record.client_id).encode("ascii")),
            (EMAIL_HEADER.encode("ascii"), header_safe(record.user_email).encode("ascii")),
        ]
        await self.app(scope, receive, send)


def _rewrite(scope: dict) -> dict:
    """Point the request at `/mcp`.

    FastMCP mounts a plain `Route("/mcp")`, not a `Mount`, so `/mcp/oauth`
    reaches nothing on its own -- the rewrite is what makes "the same tools,
    the same sponsored card" literal rather than a second copy of the
    registry.
    """
    scope = dict(scope)
    scope["path"] = MCP_PATH
    scope["raw_path"] = MCP_PATH.encode("ascii")
    return scope


#: JSON-RPC has no registered code for "authenticate and try again", and the
#: implementation-defined range (-32000 to -32099) is where a server puts its
#: own. -32003 is what the MCP ecosystem has settled on for an authorization
#: failure; the status line is the part clients actually key on.
JSONRPC_AUTH_REQUIRED = -32003


async def _challenge(send, support, path: str, scope: dict | None = None) -> None:
    """401 + the header that makes an MCP client show a Sign in button.

    The header is the part a client acts on. The BODY is for everyone else --
    a script, a log, a person reading a curl output -- and it carries the
    whole instruction rather than a code, because for those readers it is the
    only thing we ever get to say.

    Two body shapes, chosen by method. A POST is a JSON-RPC call and gets a
    JSON-RPC error response, which is what the caller's transport is already
    parsing; anything else (a GET probe, a HEAD) gets the plain OAuth error
    object the RFCs describe. Both carry the same sentence.
    """
    from .fair_use import signin_directions  # noqa: PLC0415 -- flat import graph

    directions = signin_directions()
    method = ((scope or {}).get("method") or "").upper()
    if method == "POST":
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            # The request body was never read; JSON-RPC 2.0 says null is the
            # id to use when it cannot be determined, and buffering every MCP
            # POST on the server to find one would be a real cost for a
            # refusal.
            "id": None,
            "error": {
                "code": JSONRPC_AUTH_REQUIRED,
                "message": directions,
                "data": {"reason": "sign_in_required"},
            },
        }
    else:
        body = {"error": "invalid_request", "error_description": directions}
    payload, out = _json_response(
        401, body, {"WWW-Authenticate": support.challenge_header(path=path)}
    )
    await _send(send, 401, out, payload)


def _scope_headers(scope: dict) -> dict[str, str]:
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in (scope.get("headers") or [])
    }


async def _send(send, status: int, headers: list, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ── the consent page ─────────────────────────────────────────────────────


def consent_html(
    *,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    email: str,
    product: str,
    sealed: str,
    csrf: str,
    day_cap: int = 0,
    anon_day_cap: int = 0,
) -> str:
    """What the human sees before a client gets a token.

    Names the client, the account, what signing in actually buys, and -- in
    one plain sentence, above the button, not in a policy -- that the address
    goes on FlightPowers product updates and how to stop them. Matan's call
    on 2026-09-09 was to build the upsell list from these sign-ins, and a
    list built from a consent screen that did not say so is the kind of list
    that produces spam complaints instead of customers.
    """
    allowance = (
        f"<li>Your free allowance is counted against <strong>your account</strong> "
        f"instead of a guess made from your IP address and user agent. "
        f"That is {day_cap:,} searches a day"
        + (
            f", against {anon_day_cap:,} for a caller we cannot tell apart from "
            "anyone else sharing their connection."
            if anon_day_cap and anon_day_cap != day_cap
            else ", the same allowance, counted somewhere it cannot be spent "
            "by a stranger who shares your gateway."
        )
        + "</li>"
        if day_cap
        else ""
    )
    host = e(urlsplit(redirect_uri).netloc or redirect_uri)
    return f"""<h1>Connect {e(client_name)}?</h1>
<p class="note">Signed in as <strong>{e(email or "your Google account")}</strong>.</p>
<div class="card">
<p><strong>{e(client_name)}</strong> is asking to search live flight fares and
hotel rates on your behalf through the free FlightPowers server.</p>
<ul>
  {allowance}
  <li>Results still carry a sponsored card, and the free daily budget still
      applies. This is the same free server, signed in.</li>
  <li>We ask Google for two things only: your account id and your email
      address. No RapidAPI key, no payment details, nothing to paste.</li>
  <li>Access lasts one hour at a time and is renewed silently until you
      revoke it.</li>
</ul>
<p class="note">Signing in adds your email address to FlightPowers product
updates. Every message carries an unsubscribe link, and
<a href="/connect">this page</a> removes your account and your address
entirely.</p>
<p class="note">It will be sent back to <code>{host}</code>.</p>
</div>
<form method="post" action="{e(AUTHORIZE_PATH)}">
  <input type="hidden" name="csrf" value="{e(csrf)}">
  <input type="hidden" name="request" value="{e(sealed)}">
  <p>
    <button class="btn" type="submit" name="decision" value="approve">Approve</button>
    <button class="btn danger" type="submit" name="decision" value="deny">Deny</button>
  </p>
</form>
<p class="note">Client id <code>{e(client_id)}</code>.
<a href="/connect">Your account</a> &middot;
<a href="{e(PRIVACY_URL)}">Privacy</a> &middot;
<a href="{e(TERMS_URL)}">Terms</a></p>
"""


def error_html(title: str, message: str) -> str:
    return (
        f"<h1>{e(title)}</h1>"
        + f'<p class="bad">{e(message)}</p>'
        + '<p class="note">Nothing was approved and nothing was changed. '
        "Start the sign-in again from your MCP client, or "
        '<a href="/connect">open your account page</a>.</p>'
    )


def redirect_with(base: str, params: dict[str, str]) -> str:
    """Append parameters to a redirect_uri, preserving any it already has."""
    clean = {k: v for k, v in params.items() if v}
    if not clean:
        return base
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{urlencode(clean)}"


def new_state() -> str:
    return secrets.token_urlsafe(16)


def new_family() -> str:
    """The id every token descended from one authorization shares."""
    return "fam_" + secrets.token_urlsafe(16)
