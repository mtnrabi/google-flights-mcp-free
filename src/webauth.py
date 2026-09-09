"""
Google sign-in for the free server's /connect page and its consent screen.

PROVENANCE. This file is a copy of `mcp_server_paid/src/webauth.py` with the
RapidAPI half removed. It is a copy rather than a shared import because the
two servers are two separate Vercel projects whose deploys upload only their
own directory, so a package one level up is simply not in either bundle --
and because the paid server's behaviour must not move when this one changes.
The rule for keeping them honest: a fix to the SIGNING or the Google exchange
below belongs in both files on the same day; anything about keys, connect
tokens or billing belongs only in the paid one, which is why none of it is
here.

What is different from the paid copy
------------------------------------
* No `fpk_` connect token. The free server takes no RapidAPI key, so there is
  no key to point a token at: the only credential this file issues is the
  browser session cookie, and the only thing identity buys on this server is
  a fair-use counter in your own name.
* The signing key is DERIVED from the Google OAuth client secret rather than
  read from `MCP_KEY_MASTER`. The paid server needs its own master because it
  encrypts a stored RapidAPI key; this server stores no secret of the user's
  at all, so adding a fourth variable to set would be ceremony. Rotating the
  Google client secret rotates every session and every consent form with it,
  which is the behaviour you want anyway. `MCP_SIGNIN_SECRET` overrides it
  for a staged rotation.

Two things are signed here and they are not interchangeable:

* a **session cookie**, an hour long, that says "this browser is Google
  account <sub>". It is scoped `Path=/connect` and is never accepted on /mcp.
* a **sealed authorization request**, minutes long, that carries an already
  validated consent form through the browser so the POST cannot approve
  something the user never saw.

Both are HMAC-SHA256 over a compact JSON payload, under per-purpose derived
keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Minimum for "who is this". No Drive, no Calendar, no profile: the consent
#: screen a user sees should ask for exactly what the feature needs, because
#: a scope list longer than the feature is the reason people abandon a
#: sign-in they were otherwise happy with.
GOOGLE_SCOPES = ("openid", "email")

SESSION_COOKIE = "fp_session"
OAUTH_COOKIE = "fp_oauth"
COOKIE_PATH = "/connect"

SESSION_TTL_SECONDS = 60 * 60           # one hour of browser session
OAUTH_STATE_TTL_SECONDS = 10 * 60       # one round trip to Google


class WebAuthError(RuntimeError):
    """Anything that means "start the sign-in again"."""


# ── signing ──────────────────────────────────────────────────────────────


def derive_secret(master: bytes, label: bytes) -> bytes:
    """A per-purpose signing key from the master key.

    Separate labels so a session cookie can never be replayed as a connect
    token, or the other way round, even though both are HMAC-SHA256 over
    JSON. (A `typ` field in the payload is checked too; this is the belt to
    that pair of braces.)
    """
    return hmac.new(master, label, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def sign_payload(payload: dict[str, Any], secret: bytes, prefix: str = "") -> str:
    """`<prefix><b64 json>.<b64 hmac>` -- compact, URL-safe, no dependency."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{prefix}{body}.{_b64e(mac)}"


def verify_payload(
    token: str, secret: bytes, prefix: str = "", now: float | None = None
) -> dict[str, Any]:
    """The inverse. Raises WebAuthError for a bad prefix, a bad signature, a
    malformed payload or an expired one -- one exception type, because the
    only useful thing to tell a user about any of them is "sign in again".
    """
    if prefix:
        if not token.startswith(prefix):
            raise WebAuthError("token has the wrong prefix")
        token = token[len(prefix) :]
    body, _, mac = token.partition(".")
    if not body or not mac:
        raise WebAuthError("token is malformed")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _b64d(mac)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise WebAuthError("token is malformed") from exc
    # compare_digest, not ==: signature comparison is the one place in this
    # file where a timing difference is an oracle.
    if not hmac.compare_digest(expected, given):
        raise WebAuthError("token signature does not match")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebAuthError("token payload is malformed") from exc
    if not isinstance(payload, dict):
        raise WebAuthError("token payload is malformed")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise WebAuthError("token has no expiry")
    if (now if now is not None else time.time()) >= exp:
        raise WebAuthError("token has expired")
    return payload


def is_local_path(target: str) -> bool:
    """True for a value that can only be a path on this origin.

    An open redirect is the classic way a sign-in gets weaponised: the user
    authenticates on the real site and is then bounced to an attacker's copy.
    So the test is deliberately narrow -- one leading slash, never two (`//`
    is protocol-relative and goes to another host), no scheme, no backslash
    (some browsers normalise `\\` to `/`), and no control characters.
    """
    if not target or not isinstance(target, str):
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target or ":" in target.split("?", 1)[0]:
        return False
    return all(ch >= " " and ch != "\x7f" for ch in target)


# ── identity ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Read the claims out of an id_token WITHOUT verifying its signature.

    That is correct here and only here: this id_token was just fetched by us,
    over TLS, directly from Google's token endpoint, in response to a code we
    generated -- OpenID Connect Core 3.1.3.7 says a client MAY skip
    verification when the token comes straight from the token endpoint over a
    protected channel. It would NOT be correct anywhere the token arrived
    from a client.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise WebAuthError("Google returned a malformed id_token")
    try:
        claims = json.loads(_b64d(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebAuthError("Google returned an unreadable id_token") from exc
    if not isinstance(claims, dict):
        raise WebAuthError("Google returned an unreadable id_token")
    return claims


@dataclass(frozen=True)
class GoogleWebAuth:
    """The browser half of Google sign-in for one origin."""

    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: bytes

    # ── step 1: send them to Google ──────────────────────────────────────

    def start(self, next_path: str = "") -> tuple[str, str]:
        """Returns (authorize_url, signed state cookie value).

        PKCE even though this is a confidential client with a secret. It
        costs two lines and it closes the authorization-code interception
        window that a plain confidential flow leaves open on a redirect URI
        anybody can navigate to.

        `next_path` is where to land after the sign-in, and it rides INSIDE
        the signed state cookie rather than on the query string. That is what
        makes it safe: a value the browser cannot edit cannot be turned into
        an open redirect, so the callback can send the user straight on to a
        pending MCP authorization request without re-validating a URL a
        stranger supplied. `is_local_path` is still applied on the way out,
        because a bug that put a full URL in here should fail closed.
        """
        verifier = _b64e(os.urandom(32))
        challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(16)
        payload = {
            "typ": "oauth",
            "state": state,
            "v": verifier,
            "exp": int(time.time()) + OAUTH_STATE_TTL_SECONDS,
        }
        if next_path and is_local_path(next_path):
            payload["n"] = next_path
        cookie = sign_payload(payload, self.session_secret)
        url = f"{GOOGLE_AUTHORIZE_URL}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                # No refresh token is wanted: we never call Google again on
                # the user's behalf. Asking for offline access would mean
                # holding a credential we have no use for.
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return url, cookie

    # ── step 2: they come back ───────────────────────────────────────────

    async def finish(
        self,
        code: str,
        state: str,
        state_cookie: str,
        client: httpx.AsyncClient | None = None,
    ) -> GoogleIdentity:
        payload = verify_payload(state_cookie, self.session_secret)
        if payload.get("typ") != "oauth":
            raise WebAuthError("wrong token type")
        if not hmac.compare_digest(str(payload.get("state", "")), state):
            raise WebAuthError("state does not match")
        verifier = str(payload.get("v", ""))
        if not verifier:
            raise WebAuthError("state carried no PKCE verifier")

        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.post(GOOGLE_TOKEN_URL, data=data)
        finally:
            if own_client:
                await client.aclose()
        if response.status_code != 200:
            # Google's body echoes the code and sometimes the client_id;
            # neither belongs in a log line, so only the status is kept.
            logger.warning("Google token exchange failed: %d", response.status_code)
            raise WebAuthError("Google would not exchange that sign-in")
        body = response.json()
        claims = _decode_id_token_claims(str(body.get("id_token", "")))
        sub = str(claims.get("sub", "")).strip()
        if not sub:
            raise WebAuthError("Google returned no account id")
        if str(claims.get("aud", "")) != self.client_id:
            raise WebAuthError("Google returned a token for a different app")
        email = str(claims.get("email", "")).strip()
        if email and claims.get("email_verified") is False:
            # Keep the account, drop the address: an unverified email must
            # never end up somewhere it could be treated as a contact.
            email = ""
        return GoogleIdentity(sub=sub, email=email)

    def next_from_state(self, state_cookie: str | None) -> str:
        """Where the sign-in was headed, out of the signed state cookie.

        "" for anything that does not verify. Called by /connect/callback
        after `finish` has already accepted the same cookie, so a second
        signature check here costs one HMAC and removes the need for the two
        call sites to agree about which of them validated what.
        """
        if not state_cookie:
            return ""
        try:
            payload = verify_payload(state_cookie, self.session_secret)
        except WebAuthError:
            return ""
        if payload.get("typ") != "oauth":
            return ""
        target = str(payload.get("n", ""))
        return target if is_local_path(target) else ""

    # ── sessions ─────────────────────────────────────────────────────────

    def issue_session(self, identity: GoogleIdentity, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        return sign_payload(
            {
                "typ": "session",
                "sub": identity.sub,
                "email": identity.email,
                "exp": int(now) + SESSION_TTL_SECONDS,
            },
            self.session_secret,
        )

    def read_session(
        self, cookie: str | None, now: float | None = None
    ) -> GoogleIdentity | None:
        if not cookie:
            return None
        try:
            payload = verify_payload(cookie, self.session_secret, now=now)
        except WebAuthError:
            return None
        if payload.get("typ") != "session":
            return None
        sub = str(payload.get("sub", ""))
        if not sub:
            return None
        return GoogleIdentity(sub=sub, email=str(payload.get("email", "")))


def signing_master(client_secret: str = "") -> bytes:
    """The one secret every signature in this file is derived from.

    `MCP_SIGNIN_SECRET` when it is set, so a rotation can be staged
    independently of Google. Otherwise the Google OAuth client secret, which
    this deployment already has to hold for the flow to work at all -- a
    fourth variable to set would buy nothing here, because unlike the paid
    server this one encrypts nothing: there is no stored ciphertext that a
    lost key would strand.
    """
    override = (os.environ.get("MCP_SIGNIN_SECRET") or "").strip()
    raw = override or (client_secret or "").strip()
    if not raw:
        return b""
    return hashlib.sha256(b"fp-free-signin-master-v1|" + raw.encode("utf-8")).digest()


def build_web_auth(
    origin: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> GoogleWebAuth | None:
    """The sign-in for this deployment's public origin, or None when unconfigured.

    `origin` is the scheme+host clients reach this server on, derived from
    `MCP_PUBLIC_URL`, because Google compares `redirect_uri` literally and the
    session cookie is per host.
    """
    client_id = (
        client_id if client_id is not None else os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    ).strip()
    client_secret = (
        client_secret
        if client_secret is not None
        else os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    ).strip()
    if not client_id or not client_secret:
        return None
    master = signing_master(client_secret)
    if not master:
        return None
    return GoogleWebAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=f"{origin.rstrip('/')}/connect/callback",
        session_secret=derive_secret(master, b"fp-free-session-v1"),
    )
