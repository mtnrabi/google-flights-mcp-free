"""
The upsell list: where a free-tier sign-in's email address goes, and how it
stops going there.

Phase 1 is Neon, not Resend
---------------------------
The obvious design -- add every sign-in to a Resend audience called
`free-mcp-signins` -- cannot be built today: this Resend plan caps the
account at THREE audiences/segments in one namespace, all three are in use,
and `POST /audiences` and `POST /segments` both answer 400 "Your plan
includes 3 segments" (measured 2026-09-05, re-confirmed 2026-09-09 in
`state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md`). Upgrading is a spend
and spends are off.

So the list IS the Neon table. `free_mcp_users` holds the address, the
consent moment (`first_seen`) and an `email_opt_out` flag, and the
transactional sends -- the welcome note, and later the cap note -- go out
through Resend `POST /emails`, which needs no audience, no segment and no
contact record. Contact SYNC into a Resend audience is a phase-2 job, off
until `RESEND_FREE_AUDIENCE_ID` is set, and when it is switched on it must
follow the order in `sync_contact` below.

The hazard that shapes the sync
-------------------------------
**Resend's `POST /contacts` on an address that already exists UPSERTS it and
resets `unsubscribed` to false.** A sign-in flow that blindly POSTs would
silently re-subscribe every person who has ever unsubscribed -- the one email
mistake that cannot be taken back. So the sync GETs first, never POSTs an
address it found, and copies a Resend `unsubscribed: true` back into
`email_opt_out` so our own sends stop too.

Our own unsubscribe link
------------------------
`{{{RESEND_UNSUBSCRIBE_URL}}}` only renders inside a Resend BROADCAST; in a
transactional send it comes out literally, as those five words plus braces,
in the footer of every message. So a transactional lane needs an unsubscribe
route of its own: `GET /email/unsubscribe?t=<token>` on this server, with the
token minted per user at first sign-in and stored beside the address. The
same URL goes in the `List-Unsubscribe` header, because a one-click header is
what keeps a small sender out of the promotions tab.

Nothing here can fail a tool call, or a sign-in
-----------------------------------------------
Every function returns rather than raises, and the callers run them from a
background task after the response is already decided. A marketing list is
never a reason to refuse somebody a login.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from .freeusers import RESEND_ADDED, RESEND_FAILED, RESEND_SKIPPED

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com"

API_KEY_ENV = "RESEND_API_KEY"
#: Phase 2 only. Unset (the shipped state) means the server writes NOTHING to
#: Resend's contact API -- see the module header. Never created from here: an
#: audience is account-level state on a shared account, and a serverless
#: function that can create one will eventually create three.
AUDIENCE_ENV = "RESEND_FREE_AUDIENCE_ID"
#: `on` sends the welcome note at first sign-in. Default OFF, so the code can
#: ship and be verified before a single email leaves: the copy is signed off
#: in `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` and the manager
#: flips this when it is wired.
WELCOME_ENV = "FREE_SIGNIN_WELCOME"

#: From/Reply-To for this lane. `app@` deliberately, never `matan@` (the cold
#: B2B lane since 2026-09-09) and never Gmail. The From never changes.
FROM_ADDRESS = "Matan Rabi <app@flightpowers.com>"

_USER_AGENT = "flightpowers-free-mcp/1.0 (+https://flightpowers.com)"


@dataclass(frozen=True)
class MailConfig:
    api_key: str
    audience_id: str = ""
    welcome: bool = False
    #: Where `/email/unsubscribe` lives for the links we print. Defaults to
    #: this deployment's own origin; set `FREE_UNSUBSCRIBE_BASE` to
    #: `https://flightpowers.com` once the site proxies the route, so the
    #: link in a footer is on the brand domain rather than on an MCP host.
    unsubscribe_base: str = ""

    @property
    def can_send(self) -> bool:
        return bool(self.api_key)

    @property
    def can_sync(self) -> bool:
        return bool(self.api_key and self.audience_id)


def load_mail_config(origin: str = "") -> MailConfig:
    base = (os.environ.get("FREE_UNSUBSCRIBE_BASE") or origin or "").rstrip("/")
    return MailConfig(
        api_key=(os.environ.get(API_KEY_ENV) or "").strip(),
        audience_id=(os.environ.get(AUDIENCE_ENV) or "").strip(),
        welcome=(os.environ.get(WELCOME_ENV) or "").strip().lower()
        in {"1", "on", "true", "yes"},
        unsubscribe_base=base,
    )


def unsubscribe_url(config: MailConfig, token: str) -> str:
    return f"{config.unsubscribe_base}/email/unsubscribe?t={token}"


# ── phase 2: contact sync ────────────────────────────────────────────────


async def sync_contact(
    email: str,
    config: MailConfig,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> str:
    """Put `email` on the audience if -- and only if -- it belongs there.

    Returns a `freeusers` resend_state. The order is not negotiable and is
    the whole reason this is a function rather than one POST:

        GET  /audiences/{audience}/contacts/{email}
          200 + unsubscribed=true   -> SKIP, forever. Never POST.
          200 + unsubscribed=false  -> already right. Do not "refresh" it.
          404                       -> POST /contacts, once.
          anything else             -> FAIL, leave the row pending, retry on
                                       the next first-sign-in job.

    A `skipped` on an unsubscribed address is also the signal to set
    `email_opt_out` locally -- see `remember_signin`.
    """
    address = (email or "").strip().lower()
    if not address or "@" not in address:
        # An unverified Google address arrives as "" (webauth). Nothing to do
        # and nothing wrong: the account still gets a row, just no address.
        return RESEND_SKIPPED
    if not config.can_sync:
        return RESEND_SKIPPED

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        existing = await client.get(
            f"{RESEND_API}/audiences/{config.audience_id}/contacts/{address}",
            headers=_headers(config),
            timeout=timeout,
        )
        if existing.status_code == 200:
            if _json(existing).get("unsubscribed") is True:
                logger.info(
                    "free-mcp sign-in: address is unsubscribed at Resend, not "
                    "re-adding and marking it opted out locally"
                )
                return RESEND_SKIPPED
            return RESEND_SKIPPED
        if existing.status_code not in (404, 400):
            logger.warning(
                "Resend contact lookup returned %d; leaving the row pending",
                existing.status_code,
            )
            return RESEND_FAILED

        created = await client.post(
            f"{RESEND_API}/contacts",
            headers=_headers(config),
            json={
                "email": address,
                "audience_id": config.audience_id,
                "unsubscribed": False,
            },
            timeout=timeout,
        )
        if created.status_code in (200, 201):
            logger.info("free-mcp sign-in added to the upsell audience")
            return RESEND_ADDED
        logger.warning(
            "Resend contact create returned %d; leaving the row pending",
            created.status_code,
        )
        return RESEND_FAILED
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("Resend was not reachable: %s", type(exc).__name__)
        return RESEND_FAILED
    except Exception as exc:  # noqa: BLE001 - a mailing list may never raise here
        logger.warning("Resend contact sync failed: %s", exc)
        return RESEND_FAILED
    finally:
        if own_client:
            await client.aclose()


# ── phase 1: the welcome note ────────────────────────────────────────────

#: Copy from `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` (a), which
#: is where it stays authoritative. Only the day-cap and month-cap numbers and
#: the unsubscribe URL are substituted, so a cap change in settings.py cannot
#: leave this note promising the old one.
WELCOME_SUBJECT = "you're in, {day_cap} searches a day"

WELCOME_TEXT = """Hi,

You're signed in, so the free searches run under your own account now, not off your IP address: {day_cap} a day, {month_cap} a month. One search covers one date and one airport, so a wide scan burns through that faster than it looks. Results carry a sponsored card. That's what pays for the free tier.

One thing worth five minutes: https://flightpowers.com/guides/five-minute-travel-agent - how to point an assistant at your own route and let it keep looking while you don't.

If the ads or the cap get in the way there's a paid server, same tools, no ads, and it runs on your own RapidAPI key. BASIC is free with 10 requests a month, PRO is $10.

Matan

--
You're getting this because you signed in to the free FlightPowers MCP server.
A welcome note and about one email a month, nothing else. Unsubscribe: {unsubscribe}
"""


async def send_welcome(
    email: str,
    token: str,
    config: MailConfig,
    *,
    day_cap: int,
    month_cap: int,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> bool:
    """One transactional send at first sign-in. Returns True if Resend took it.

    Off unless `FREE_SIGNIN_WELCOME=on`. The caller is responsible for the
    "once per user, ever" guard -- here that is `free_mcp_users.welcome_sent_at`
    being null, checked and stamped in one statement, because a guard that is
    "the absence of an error" re-sends the note on every redeploy.
    """
    address = (email or "").strip().lower()
    if not address or "@" not in address or not config.can_send or not config.welcome:
        return False
    link = unsubscribe_url(config, token)
    body = WELCOME_TEXT.format(
        day_cap=f"{day_cap:,}", month_cap=f"{month_cap:,}", unsubscribe=link
    )
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            f"{RESEND_API}/emails",
            headers=_headers(config),
            json={
                "from": FROM_ADDRESS,
                "reply_to": FROM_ADDRESS,
                "to": [address],
                "subject": WELCOME_SUBJECT.format(day_cap=f"{day_cap:,}"),
                "text": body,
                # One-click unsubscribe. `{{{RESEND_UNSUBSCRIBE_URL}}}` is a
                # BROADCAST variable and renders literally here, so the link
                # and the header both point at our own route.
                "headers": {
                    "List-Unsubscribe": f"<{link}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a welcome note may never raise
        logger.warning("welcome send failed: %s", exc)
        return False
    finally:
        if own_client:
            await client.aclose()
    if response.status_code in (200, 201) and _json(response).get("id"):
        logger.info("free-mcp welcome note accepted by Resend")
        return True
    logger.warning("welcome send returned %d", response.status_code)
    return False


def _headers(config: MailConfig) -> dict[str, str]:
    return {
        "authorization": f"Bearer {config.api_key}",
        "content-type": "application/json",
        # Resend's edge 403s (error 1010) a default python-urllib user agent.
        # httpx sends its own, which is not on that list, but the UA is set
        # explicitly so the reason it works is written down.
        "user-agent": _USER_AGENT,
    }


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def remember_signin(
    store,
    google_sub: str,
    email: str,
    token: str,
    config: MailConfig,
    *,
    day_cap: int = 0,
    month_cap: int = 0,
) -> str:
    """The whole first-sign-in hand-off, in the order that cannot mis-fire.

    1. Sync the contact IF phase 2 is switched on. A Resend `unsubscribed`
       becomes `email_opt_out` locally, so our own transactional sends stop
       for that address too -- an opt-out expressed anywhere means opted out
       everywhere.
    2. Send the welcome IF `FREE_SIGNIN_WELCOME=on` and the address has not
       opted out.
    3. Record what happened.

    Best effort throughout. Called from a background task; a failure leaves
    the row `pending` and the next first-sign-in for that account -- which
    normally never happens -- would retry it.
    """
    state = RESEND_SKIPPED
    if config.can_sync:
        state = await sync_contact(email, config)

    opted_out = False
    try:
        row = await store.get(google_sub)
        opted_out = bool(getattr(row, "email_opt_out", False)) if row else False
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read the user row before the welcome: %s", exc)

    if not opted_out and config.welcome:
        try:
            sent = await send_welcome(
                email, token, config, day_cap=day_cap, month_cap=month_cap
            )
            if sent:
                await store.mark_welcome_sent(google_sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("welcome hand-off failed: %s", exc)

    try:
        await store.set_resend_state(google_sub, state)
    except Exception as exc:  # noqa: BLE001 - best effort by construction
        logger.warning("could not record the Resend state: %s", exc)
    return state
