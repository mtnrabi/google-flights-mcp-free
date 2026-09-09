"""
The HTTP surface of the free server's sign-in: the account page, the Google
round trip, the consent page, and the five OAuth endpoints.

PROVENANCE. Adapted from `mcp_server_paid/src/oauthroutes.py` plus the
`/connect` routes that live in that server's `build_server`. The differences
are all removals: there is no key form, no key validation, no `fpk_` connect
token to render. What is added is the account page's Delete button and the
Resend hand-off on a first sign-in. See `src/oauth.py`'s header for why this
is a copy and not a shared package.

Route map:

    GET  /connect                                         the account page
    GET  /connect/start                                   -> Google
    GET  /connect/callback                                <- Google
    POST /connect/delete                                  forget me
    GET  /.well-known/oauth-protected-resource            RFC 9728
    GET  /.well-known/oauth-protected-resource/mcp/oauth  (path-scoped form)
    GET  /.well-known/oauth-authorization-server          RFC 8414
    GET  /.well-known/oauth-authorization-server/mcp/oauth
    POST /oauth/register                                  RFC 7591 (DCR)
    GET  /connect/authorize                               the consent page
    POST /connect/authorize                               approve / deny
    POST /oauth/token                                     RFC 6749 4.1.3, 6
    POST /oauth/revoke                                    RFC 7009

`/mcp/oauth` itself is NOT here: it is an ASGI-level gate
(`oauth.OAuthResourceGate`) wired in `api/index.py`, because it has to strip
injected headers before any route runs and rewrite the path to `/mcp` so the
request is served by the same tool registry, and the same sponsored card,
that `/mcp` serves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import cimd
from .oauth import (
    AUTHORIZATION_SERVER_PATH,
    AUTHORIZE_PATH,
    MCP_OAUTH_PATH,
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    REGISTER_PATH,
    REVOKE_PATH,
    TOKEN_PATH,
    OAuthError,
    OAuthSupport,
    consent_html,
    error_html,
    redirect_with,
)
from .oauthstore import OAuthStoreError
from .pages import PRIVACY_URL, TERMS_URL, e, footer, page
from .ratelimit import (
    CIMD_FETCH,
    LIMITER,
    REGISTER,
    TOKEN,
    UNKNOWN_IP,
    Limit,
    client_ip,
    usable_address,
)
from .webauth import (
    COOKIE_PATH,
    OAUTH_COOKIE,
    OAUTH_STATE_TTL_SECONDS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    WebAuthError,
)

logger = logging.getLogger(__name__)

#: Metadata is public, immutable per deployment, and polled by every client
#: on every connect. Five minutes of caching is the difference between a cold
#: start per client and a cold start per client per hour.
_METADATA_CACHE = "public, max-age=300"


def _too_many(limit: Limit) -> JSONResponse:
    """The 429 the public POST endpoints answer with when they are flooded.

    `Retry-After` is not decoration: an MCP client that gets a bare 429 with
    no interval retries immediately, which is the behaviour the limit exists
    to stop.
    """
    return JSONResponse(
        {
            "error": "temporarily_unavailable",
            "error_description": (
                "too many requests to this endpoint; wait and try again"
            ),
        },
        status_code=429,
        headers={"Retry-After": str(limit.retry_after()), "Cache-Control": "no-store"},
    )


def caller_ip(request: Request) -> str:
    """The bucket key for this request: see `ratelimit.client_ip`."""
    ip = client_ip({k.lower(): v for k, v in request.headers.items()})
    if ip == UNKNOWN_IP and request.client is not None:
        ip = usable_address(request.client.host or "") or UNKNOWN_IP
    return ip


def _rate_limited(request: Request, limit: Limit) -> JSONResponse | None:
    """None when the request may proceed, a 429 when it may not."""
    ip = caller_ip(request)
    if LIMITER.allow(limit, ip):
        return None
    logger.warning("rate limit hit on %s by %s", limit.name, ip)
    return _too_many(limit)


def _no_store(body: dict[str, Any], status: int = 200) -> JSONResponse:
    """Token responses must not be cached. RFC 6749 5.1 is explicit."""
    return JSONResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _form(request: Request) -> dict[str, str]:
    """A token/revoke body, from a form or from JSON.

    The RFC says form-encoded. Some MCP clients send JSON anyway, and a
    server that answers `invalid_request` to a correct-but-JSON token request
    produces a support thread rather than a bug report, so both are read.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError:
            return {}
        return {k: str(v) for k, v in body.items()} if isinstance(body, dict) else {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


# ── the account page ─────────────────────────────────────────────────────


def signed_out_html(sign_in_url: str, banner: str = "") -> str:
    warning = f'<p class="bad">{e(banner)}</p>' if banner else ""
    return page(
        "Sign in to FlightPowers free",
        "<h1>Sign in to FlightPowers free</h1>"
        + warning
        + "<p>The free server works without an account and always will. "
        "Signing in changes one thing: your daily allowance is counted "
        "against <strong>your account</strong> instead of a guess made from "
        "your IP address and your user agent, which is what everyone behind "
        "the same gateway currently shares.</p>"
        "<p>There is nothing to paste. No RapidAPI key, no card, no "
        "configuration file.</p>"
        f'<p><a class="btn" href="/connect/start">Sign in with Google</a></p>'
        '<p class="note">We ask Google for two things only: your account id '
        "and your email address. Signing in adds that address to FlightPowers "
        "product updates, and every message carries an unsubscribe link. "
        "Coming back to this page removes your account and your address "
        "entirely.</p>"
        "<h2>Using it from your MCP client</h2>"
        f"<pre>{e(sign_in_url)}</pre>"
        "<p>Add that URL to a client that supports MCP authorization and it "
        "will offer a Sign in button. Clients that do not can keep using "
        "the open endpoint with no account at all.</p>" + footer(),
    )


def signed_in_html(
    *,
    email: str,
    sign_in_url: str,
    csrf: str,
    day_cap: int,
    anon_day_cap: int,
    notice: str = "",
) -> str:
    allowance = (
        f"<p>Your allowance is <strong>{day_cap:,} searches a day</strong>, "
        "counted against this account."
        + (
            f" A caller with no account shares {anon_day_cap:,} a day with "
            "everyone else on their connection."
            if anon_day_cap and anon_day_cap != day_cap
            else ""
        )
        + "</p>"
        if day_cap
        else ""
    )
    note = f'<p class="good">{e(notice)}</p>' if notice else ""
    return page(
        "Your FlightPowers free account",
        "<h1>Your FlightPowers free account</h1>"
        + note
        + f'<p class="note">Signed in as <strong>{e(email or "your Google account")}'
        "</strong>.</p>"
        '<div class="card">'
        + allowance
        + "<p>Point your MCP client at this URL and sign in from inside it:</p>"
        f"<pre>{e(sign_in_url)}</pre>"
        "<p>Results still carry a sponsored card. That is what pays for the "
        "free tier, and it does not change when you sign in.</p>"
        "</div>"
        "<h2>Product updates</h2>"
        "<p>Your address is on the FlightPowers product-updates list. Every "
        "message has an unsubscribe link; unsubscribing there does not sign "
        "you out here, and signing out here does not unsubscribe you.</p>"
        "<h2>Delete everything</h2>"
        "<p>This removes your account id, your email address and every token "
        "any client holds for you. Your client will ask you to sign in again "
        "the next time it calls, and the open endpoint keeps working with no "
        "account.</p>"
        f'<form method="post" action="/connect/delete">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<p><button class="btn danger" type="submit">Delete my account</button></p>'
        "</form>"
        f'<p class="note"><a href="{e(PRIVACY_URL)}">Privacy</a> &middot; '
        f'<a href="{e(TERMS_URL)}">Terms</a></p>' + footer(),
    )


# ── wiring ───────────────────────────────────────────────────────────────


def register_oauth_routes(mcp, oauth: OAuthSupport, settings) -> None:
    """Attach every sign-in route to the FastMCP instance.

    Called only when `oauth.build_free_oauth` returned a support object, so
    an unconfigured deployment 404s all of these -- which is the honest
    answer: there is nothing behind them.
    """

    site_origin = oauth.issuer
    sign_in_url = f"{site_origin}{MCP_OAUTH_PATH}"
    _cookie_secure = site_origin.lower().startswith("https://")
    _canonical_host = site_origin.split("://", 1)[-1].rstrip("/").lower()
    day_cap = getattr(settings, "fair_use_day_cap", 0)
    month_cap = getattr(settings, "fair_use_month_cap", 0)
    anon_day_cap = getattr(settings, "anon_day_cap", day_cap)

    def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
        """HttpOnly, SameSite=Lax, scoped to /connect.

        Lax rather than Strict because the sign-in ends in a top-level GET
        navigation back from Google, and Strict would drop the state cookie
        on exactly that hop -- the flow would fail for every user with an
        error that looks like a Google misconfiguration. `Path=/connect` so
        the cookie is never attached to a `/mcp` request: a session cookie is
        not a credential this server accepts there, and the cheapest way to
        prove that is for it not to arrive.
        """
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            secure=_cookie_secure,
            samesite="lax",
            path=COOKIE_PATH,
        )

    def _wrong_host(request: Request, keep_query: bool = False) -> Response | None:
        """Send an alias hostname to the canonical origin before anything is set.

        The session cookie is per host and Google compares `redirect_uri`
        literally, so a sign-in started on an alias would set its state
        cookie there, come back to the canonical host, find no cookie and
        fail with a message that reads like a Google misconfiguration. One
        302 up front removes the whole class -- and it is why only one
        redirect URI has to be registered in the Cloud Console.
        """
        host = (request.headers.get("host") or "").strip().lower()
        if not host or host == _canonical_host:
            return None
        query = request.url.query
        target = f"{site_origin}{request.url.path}" + (
            f"?{query}" if query and keep_query else ""
        )
        return RedirectResponse(target, status_code=302)

    def _identity(request: Request):
        return oauth.auth.read_session(request.cookies.get(SESSION_COOKIE))

    # ── the account page ─────────────────────────────────────────────────

    @mcp.custom_route("/connect", methods=["GET"])
    async def connect_page(request: Request) -> Response:
        redirect = _wrong_host(request)
        if redirect is not None:
            return redirect
        identity = _identity(request)
        if identity is None:
            return HTMLResponse(signed_out_html(sign_in_url))
        return HTMLResponse(
            signed_in_html(
                email=identity.email,
                sign_in_url=sign_in_url,
                csrf=oauth.csrf(identity.sub),
                day_cap=day_cap,
                anon_day_cap=anon_day_cap,
                notice=(
                    "You are signed in."
                    if request.query_params.get("welcome")
                    else ""
                ),
            )
        )

    @mcp.custom_route("/connect/start", methods=["GET"])
    async def connect_start(request: Request) -> Response:
        redirect = _wrong_host(request, keep_query=True)
        if redirect is not None:
            return redirect
        # `next` is where to land after Google. It is validated by
        # `GoogleWebAuth.start` and then carried INSIDE the signed state
        # cookie, so what comes back at the callback cannot have been edited
        # into an open redirect. Used by /connect/authorize, which sends a
        # not-yet-signed-in user through here and wants them returned to the
        # pending authorization request rather than to a page that has
        # forgotten it.
        url, state_cookie = oauth.auth.start(request.query_params.get("next", ""))
        response = RedirectResponse(url, status_code=302)
        _set_cookie(response, OAUTH_COOKIE, state_cookie, OAUTH_STATE_TTL_SECONDS)
        return response

    @mcp.custom_route("/connect/callback", methods=["GET"])
    async def connect_callback(request: Request) -> Response:
        error = request.query_params.get("error")
        if error:
            # The user pressed Cancel on Google's consent screen, most of the
            # time. Not an error page: send them back to the start with a
            # sentence, not a stack trace.
            return HTMLResponse(
                signed_out_html(
                    sign_in_url,
                    banner="Google sign-in was cancelled. Nothing was changed.",
                )
            )
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        state_cookie = request.cookies.get(OAUTH_COOKIE, "")
        if not code or not state or not state_cookie:
            return HTMLResponse(
                signed_out_html(
                    sign_in_url,
                    banner="That sign-in link was incomplete or had expired. Start again.",
                ),
                status_code=400,
            )
        try:
            identity = await oauth.auth.finish(code, state, state_cookie)
        except WebAuthError as exc:
            logger.info("sign-in rejected: %s", exc)
            return HTMLResponse(
                signed_out_html(
                    sign_in_url,
                    banner="That sign-in could not be completed. Start again.",
                ),
                status_code=400,
            )

        await _remember(identity)

        response = RedirectResponse(
            oauth.auth.next_from_state(state_cookie) or "/connect?welcome=1",
            status_code=303,
        )
        _set_cookie(
            response,
            SESSION_COOKIE,
            oauth.auth.issue_session(identity),
            SESSION_TTL_SECONDS,
        )
        response.delete_cookie(OAUTH_COOKIE, path=COOKIE_PATH)
        return response

    async def _remember(identity, client_kind: str = "") -> None:
        """Write the row, and on a FIRST sign-in hand the address to Resend.

        Both halves are best effort and neither can fail the sign-in: a
        marketing list is not a reason to refuse somebody a login. The
        Resend call runs as a background task so the redirect is not waiting
        on it, and `freeusers.upsert` reports `True` only for the INSERT, so
        the hand-off fires once per account even when two sign-ins race.
        """
        if oauth.users is None:
            return
        try:
            first = await oauth.users.upsert(
                identity.sub, identity.email, client_kind=client_kind
            )
        except Exception as exc:  # noqa: BLE001 - a sign-in must not fail on this
            logger.warning("could not record the sign-in: %s", exc)
            return
        if not first or not identity.email or oauth.mail is None:
            return
        if not oauth.mail.can_send and not oauth.mail.can_sync:
            return
        from .maillist import remember_signin  # noqa: PLC0415

        async def hand_off() -> None:
            try:
                row = await oauth.users.get(identity.sub)
                token = getattr(row, "unsubscribe_token", "") or ""
                await remember_signin(
                    oauth.users,
                    identity.sub,
                    identity.email,
                    token,
                    oauth.mail,
                    day_cap=day_cap,
                    month_cap=month_cap,
                )
            except Exception as exc:  # noqa: BLE001 - best effort by construction
                logger.warning("mailing-list hand-off failed: %s", exc)

        try:
            asyncio.create_task(hand_off())
        except RuntimeError:  # pragma: no cover - no running loop, e.g. a test
            logger.debug("no event loop for the Resend hand-off; skipped")

    @mcp.custom_route("/connect/delete", methods=["POST"])
    async def connect_delete(request: Request) -> Response:
        identity = _identity(request)
        if identity is None:
            return HTMLResponse(
                signed_out_html(
                    sign_in_url, banner="Your sign-in expired. Nothing was changed."
                ),
                status_code=400,
            )
        form = await request.form()
        if not oauth.csrf_ok(identity.sub, str(form.get("csrf", ""))):
            return HTMLResponse(
                signed_out_html(
                    sign_in_url, banner="That form had expired. Nothing was changed."
                ),
                status_code=400,
            )
        # Tokens first. If the row deletion fails after this, the user is
        # signed out of every client -- which is the safe half to have
        # happened; the other order would leave live tokens pointing at an
        # account that no longer exists.
        try:
            await oauth.store.revoke_for_user(identity.sub, "google")
        except OAuthStoreError as exc:
            logger.warning("could not revoke tokens on delete: %s", exc)
        try:
            if oauth.users is not None:
                await oauth.users.delete(identity.sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete the user row: %s", exc)
            return HTMLResponse(
                signed_out_html(
                    sign_in_url,
                    banner=(
                        "Your clients were signed out, but the account row "
                        "could not be removed just now. Try again in a minute."
                    ),
                ),
                status_code=503,
            )
        response = HTMLResponse(
            signed_out_html(
                sign_in_url,
                banner=(
                    "Deleted. Your account id and email address are gone and "
                    "every client has been signed out. Product updates are a "
                    "separate list -- use the unsubscribe link in any message "
                    "to leave it."
                ),
            )
        )
        response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
        return response

    # ── one-click unsubscribe ────────────────────────────────────────────

    @mcp.custom_route("/email/unsubscribe", methods=["GET", "POST"])
    async def unsubscribe(request: Request) -> Response:
        """Stop every email to this address, with no sign-in and no second click.

        `{{{RESEND_UNSUBSCRIBE_URL}}}` only renders inside a Resend BROADCAST
        -- in a transactional send it comes out literally, braces and all --
        so a lane that sends transactionally has to own its own unsubscribe
        route. This is it.

        GET and POST, because `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
        promises a mail client it can POST here without asking the human, and
        because the same URL is the visible link in the footer. There is no
        confirmation step: RFC 8058 is explicit that one click means one
        click, and a token nobody can guess is what makes that safe.
        """
        token = (request.query_params.get("t") or "").strip()
        done = False
        if token and oauth.users is not None:
            try:
                done = await oauth.users.opt_out(token)
            except Exception as exc:  # noqa: BLE001 - an unsubscribe never errors out
                logger.warning("unsubscribe failed: %s", exc)
        # The same page either way. A link that has already been used, or one
        # for an account since deleted, is still "you are unsubscribed" from
        # where the reader stands -- and a different page for a bad token
        # would let anyone test tokens.
        if done:
            logger.info("free-mcp: an address opted out of email")
        return HTMLResponse(
            page(
                "Unsubscribed",
                "<h1>Unsubscribed</h1>"
                "<p>You will not get any more email from FlightPowers about "
                "the free MCP server.</p>"
                "<p>Nothing else changed: your sign-in still works and your "
                "searches still run under your own account. To remove the "
                'account itself, open <a href="/connect">your account page</a> '
                "and press Delete.</p>" + footer(),
            )
        )

    # ── discovery ────────────────────────────────────────────────────────

    async def _protected_resource(_request: Request) -> Response:
        return JSONResponse(
            oauth.protected_resource_metadata(),
            headers={"Cache-Control": _METADATA_CACHE},
        )

    async def _authorization_server(_request: Request) -> Response:
        return JSONResponse(
            oauth.authorization_server_metadata(),
            headers={"Cache-Control": _METADATA_CACHE},
        )

    # Both the bare and the path-scoped form. RFC 9728 3.1 says a client
    # inserts the resource's path between the well-known segment and nothing
    # else, so `/mcp/oauth` is where a client that read our 401 looks; the
    # bare path is where a client that only knows the origin looks. Serving
    # one and not the other is a discovery failure that presents to a user as
    # "this server does not support sign-in".
    # Three forms of each: the bare path, `/mcp` and `/mcp/oauth`. A client
    # that read a 401 on either endpoint follows the path-scoped URL from the
    # challenge literally, and a client that only knows the origin builds the
    # bare one. Serving one and not the others is a discovery failure that
    # presents to a user as "this server does not support sign-in".
    for suffix in ("", MCP_PATH, MCP_OAUTH_PATH):
        mcp.custom_route(f"{PROTECTED_RESOURCE_PATH}{suffix}", methods=["GET"])(
            _protected_resource
        )
        mcp.custom_route(f"{AUTHORIZATION_SERVER_PATH}{suffix}", methods=["GET"])(
            _authorization_server
        )

    # ── dynamic client registration ──────────────────────────────────────

    @mcp.custom_route(REGISTER_PATH, methods=["POST"])
    async def register(request: Request) -> Response:
        # Two limits, in cheapness order: the per-instance rate limit costs a
        # dictionary lookup, the durable per-day cap inside `oauth.register`
        # costs a query.
        throttled = _rate_limited(request, REGISTER)
        if throttled is not None:
            return throttled
        try:
            body = await request.json()
        except ValueError:
            return _no_store(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "the request body must be JSON",
                },
                400,
            )
        if not isinstance(body, dict):
            return _no_store(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "the request body must be a JSON object",
                },
                400,
            )
        try:
            response = await oauth.register(body, ip=caller_ip(request))
        except OAuthError as exc:
            if exc.retry_after:
                return JSONResponse(
                    exc.as_dict(),
                    status_code=exc.status,
                    headers={
                        "Retry-After": str(exc.retry_after),
                        "Cache-Control": "no-store",
                    },
                )
            return _no_store(exc.as_dict(), exc.status)
        except OAuthStoreError as exc:
            logger.warning("client registration failed: %s", exc)
            return _no_store(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "the registration store is not reachable",
                },
                503,
            )
        return _no_store(response, 201)

    # ── the authorization endpoint ───────────────────────────────────────

    def _error_page(title: str, message: str, status: int = 400) -> Response:
        return HTMLResponse(page(title, error_html(title, message)), status_code=status)

    @mcp.custom_route(AUTHORIZE_PATH, methods=["GET"])
    async def authorize(request: Request) -> Response:
        redirect = _wrong_host(request, keep_query=True)
        if redirect is not None:
            return redirect

        params = dict(request.query_params)
        # A CIMD client_id is a URL this server fetches, and this route is
        # reachable without signing in. Rate limited on its own so it cannot
        # be used as an anonymous fetcher; a registered `fpcl_` client never
        # reaches the network and is never limited here.
        if cimd.is_cimd_client_id((params.get("client_id") or "").strip()):
            if not LIMITER.allow(CIMD_FETCH, caller_ip(request)):
                logger.warning(
                    "rate limit hit on the CIMD lookup by %s", caller_ip(request)
                )
                return _error_page(
                    "Too many sign-in attempts",
                    "That is a lot of sign-in requests from one place in a "
                    "short time. Nothing was approved; wait a few minutes and "
                    "try again.",
                    429,
                )
        try:
            client, validated = await oauth.read_authorize_request(params)
        except OAuthError as exc:
            if not exc.redirectable:
                return _error_page(
                    "That sign-in request cannot be completed",
                    exc.description or exc.code,
                    exc.status,
                )
            # Past this point the redirect_uri has been checked against the
            # registration, so bouncing the error back is the RFC's answer and
            # is what lets the client show the user something useful.
            target = (params.get("redirect_uri") or "").strip()
            return RedirectResponse(
                redirect_with(
                    target,
                    {
                        "error": exc.code,
                        "error_description": exc.description,
                        "state": params.get("state") or "",
                    },
                ),
                status_code=302,
            )

        identity = _identity(request)
        if identity is None:
            # Hand off to the Google sign-in and come straight back here.
            # `next` rides inside the signed state cookie, so it cannot be
            # edited into an open redirect.
            return RedirectResponse(
                f"/connect/start?{urlencode({'next': f'{AUTHORIZE_PATH}?{urlencode(params)}'})}",
                status_code=302,
            )

        # This registration is now in front of a human, which is as good a
        # reason to keep the row as the approval that may follow it: the sweep
        # deletes registrations that never got this far, and a client that
        # registered at install and is signing in days later must not be
        # deleted while the consent page is on screen.
        await oauth.note_consent_shown(client.client_id)

        return HTMLResponse(
            page(
                f"Connect {client.client_name}?",
                consent_html(
                    client_name=client.client_name,
                    client_id=client.client_id,
                    redirect_uri=validated["redirect_uri"],
                    email=identity.email,
                    product=oauth.product,
                    sealed=oauth.seal_request(validated, identity.sub),
                    csrf=oauth.csrf(identity.sub),
                    day_cap=day_cap,
                    anon_day_cap=anon_day_cap,
                ),
            )
        )

    @mcp.custom_route(AUTHORIZE_PATH, methods=["POST"])
    async def authorize_decision(request: Request) -> Response:
        identity = _identity(request)
        if identity is None:
            return _error_page(
                "Your sign-in expired",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )
        form = await request.form()
        if not oauth.csrf_ok(identity.sub, str(form.get("csrf", ""))):
            return _error_page(
                "That form had expired",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )
        try:
            validated = oauth.open_request(str(form.get("request", "")), identity.sub)
        except WebAuthError as exc:
            logger.info("consent form rejected: %s", exc)
            return _error_page(
                "That approval could not be read",
                "Nothing was approved. Start the connection again from your "
                "MCP client.",
            )

        state = validated.get("state", "")
        if str(form.get("decision", "")) != "approve":
            return RedirectResponse(
                redirect_with(
                    validated["redirect_uri"],
                    {
                        "error": "access_denied",
                        "error_description": "the user declined",
                        "state": state,
                    },
                ),
                status_code=303,
            )

        try:
            code = await oauth.issue_code(validated, identity.sub, email=identity.email)
        except OAuthStoreError as exc:
            logger.warning("could not issue an authorization code: %s", exc)
            return _error_page(
                "Not right now",
                "The sign-in store is not reachable at the moment. Nothing was "
                "approved; try again in a minute.",
                503,
            )
        # Which client the account approved, for the row. Best effort, and
        # after the code exists: the grant is the thing the user asked for.
        try:
            client = await oauth.lookup_client(validated["client_id"])
        except (OAuthError, OAuthStoreError):
            client = None
        await _remember(
            identity, client_kind=getattr(client, "client_name", "") or ""
        )
        logger.info(
            "authorized client %s for sub=%s", validated["client_id"], identity.sub
        )
        return RedirectResponse(
            redirect_with(validated["redirect_uri"], {"code": code, "state": state}),
            status_code=303,
        )

    # ── token and revocation ─────────────────────────────────────────────

    @mcp.custom_route(TOKEN_PATH, methods=["POST"])
    async def token(request: Request) -> Response:
        throttled = _rate_limited(request, TOKEN)
        if throttled is not None:
            return throttled
        form = await _form(request)
        try:
            issued = await oauth.token(form, request.headers.get("authorization"))
        except OAuthError as exc:
            headers = (
                {"WWW-Authenticate": 'Basic realm="mcp"'}
                if exc.status == 401 and exc.code == "invalid_client"
                else {}
            )
            return JSONResponse(
                exc.as_dict(),
                status_code=exc.status,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache", **headers},
            )
        except OAuthStoreError as exc:
            logger.warning("token endpoint could not reach the store: %s", exc)
            return _no_store(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "the sign-in store is not reachable",
                },
                503,
            )
        return _no_store(issued)

    @mcp.custom_route(REVOKE_PATH, methods=["POST"])
    async def revoke(request: Request) -> Response:
        form = await _form(request)
        await oauth.revoke(form, request.headers.get("authorization"))
        # RFC 7009 2.2: 200 with an empty body, whatever happened.
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    logger.info(
        "free-server sign-in is enabled at %s (issued %s)",
        oauth.resource_url,
        time.strftime("%Y-%m-%d", time.gmtime()),
    )
