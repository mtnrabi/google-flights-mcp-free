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

from .email_templates import Cta, Hero
from .email_templates import Email as BrandedEmail
from .email_templates import render as render_branded
from .freeusers import (
    RESEND_ADDED,
    RESEND_FAILED,
    RESEND_SKIPPED,
    CapSignals,
)

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
#: `on` sends the cap note -- phase 2 of the sequence file, the one send
#: that goes to somebody who did not just do anything. Its own switch, and
#: not the welcome's: the welcome is half a receipt and the cap note is an
#: upsell, and CLAUDE.md rule 14 (35 Studio upsell notes, 0 replies, 0
#: upgrades) is the reason those two are not allowed to share a flag.
CAPNOTE_ENV = "FREE_SIGNIN_CAPNOTE"

#: From/Reply-To for this lane. `app@` deliberately, never `matan@` (the cold
#: B2B lane since 2026-09-09) and never Gmail. The From never changes.
FROM_ADDRESS = "Matan Rabi <app@flightpowers.com>"

_USER_AGENT = "flightpowers-free-mcp/1.0 (+https://flightpowers.com)"


@dataclass(frozen=True)
class MailConfig:
    api_key: str
    audience_id: str = ""
    welcome: bool = False
    cap_note: bool = False
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
        cap_note=(os.environ.get(CAPNOTE_ENV) or "").strip().lower()
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


# ── the shape every transactional send has ───────────────────────────────

#: What `_post` reports back. Three states and not a boolean, because the
#: caller has already CLAIMED the send in the database before calling (that
#: is what makes "never twice" true when two instances race) and what it
#: does with a failure depends entirely on which failure it was:
#:
#:   SENT     Resend returned an id. Keep the claim.
#:   REFUSED  Resend answered and said no. Nothing left; release the claim
#:            so the next trigger can try again.
#:   UNKNOWN  A timeout, a transport error, a 2xx with no id. Resend MAY
#:            have accepted it. KEEP the claim -- a missing note is a small
#:            loss and a duplicate is the one that gets a domain reported.
SENT = "sent"
REFUSED = "refused"
UNKNOWN = "unknown"


def _branded(email: BrandedEmail) -> str:
    """The designed HTML twin.

    Until 2026-09-10 both notes went out in a bare 560px `<div>` of
    paragraphs. Matan, on the wave-3 batch that landed plain: "Emails
    shouldn't be plain text - should be designed". So the HTML part is now
    the same template the broadcasts use (`src/email_templates/`, ported
    from the api-growth repo), and the plain-text part below is still the
    approved bytes from the sequence file, unchanged.
    """
    return render_branded(email)


async def _post(
    payload: dict,
    config: MailConfig,
    *,
    label: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> str:
    """One `POST /emails`. Returns SENT / REFUSED / UNKNOWN, never raises."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            f"{RESEND_API}/emails",
            headers=_headers(config),
            json=payload,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # The request may well have arrived. Treated as UNKNOWN, so the
        # claim stands and nobody gets two copies.
        logger.warning("%s send did not complete: %s", label, type(exc).__name__)
        return UNKNOWN
    except Exception as exc:  # noqa: BLE001 - an email may never raise here
        logger.warning("%s send failed: %s", label, exc)
        return UNKNOWN
    finally:
        if own_client:
            await client.aclose()
    if response.status_code in (200, 201) and _json(response).get("id"):
        logger.info("free-mcp %s accepted by Resend", label)
        return SENT
    if 400 <= response.status_code < 500:
        # Resend answered and declined: a bad address, a suppressed one, a
        # rejected payload. Retrying the same message changes nothing today,
        # but the claim is released so a later trigger can.
        logger.warning("%s send refused with %d", label, response.status_code)
        return REFUSED
    logger.warning("%s send returned %d", label, response.status_code)
    return UNKNOWN


def _footer_text(link: str, monthly: bool = True) -> str:
    cadence = (
        "A welcome note and about one email a month, nothing else."
        if monthly
        else "About one email a month, nothing else."
    )
    return (
        "You're getting this because you signed in to the free FlightPowers "
        f"MCP server.\n{cadence} Unsubscribe: {link}"
    )


# ── phase 1: the welcome note ────────────────────────────────────────────

#: Copy from `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` (a), which
#: is where it stays authoritative. Subject variant A, which is the one that
#: ships. Only the day-cap and month-cap numbers and the unsubscribe URL are
#: substituted, so a cap change in settings.py cannot leave this note
#: promising the old one.
WELCOME_SUBJECT = "you're in, {day_cap} searches a day"

GUIDE_URL = "https://flightpowers.com/guides/five-minute-travel-agent"

WELCOME_PARAGRAPHS = [
    "Hi,",
    "You're signed in, so the free searches run under your own account now, "
    "not off your IP address: {day_cap} a day, {month_cap} a month. One search "
    "covers one date and one airport, so a wide scan burns through that faster "
    "than it looks. Results carry a sponsored card. That's what pays for the "
    "free tier.",
    "One thing worth five minutes: {guide} - how to point an assistant at your "
    "own route and let it keep looking while you don't.",
    "If the ads or the cap get in the way there's a paid server, same tools, no "
    "ads, and it runs on your own RapidAPI key. BASIC is free with 10 requests a "
    "month, PRO is $10.",
    "Matan",
]


#: The same sentences, re-split for the designed HTML part: the two cap
#: numbers move into the hero card and the guide URL becomes the one
#: button, which is what the approved render in
#: `state/gtm/email/templates/bodies/welcome-signin.md` does. No new
#: sentence and no new link. `:::hero` and `:::cta` are where those blocks
#: land in the flow.
WELCOME_HTML_FLOW = [
    "Hi,",
    "You're signed in, so the free searches run under your own account now, "
    "not off your IP address.",
    ":::hero",
    "Results carry a sponsored card. That's what pays for the free tier.",
    "One thing worth five minutes: how to point an assistant at your own "
    "route and let it keep looking while you don't.",
    ":::cta",
    "If the ads or the cap get in the way there's a paid server, same tools, no "
    "ads, and it runs on your own RapidAPI key. BASIC is free with 10 requests a "
    "month, PRO is $10.",
]

WELCOME_PREHEADER = (
    "Your searches run under your own account now, not off your IP address."
)

WELCOME_HERO_EYEBROW = "your free allowance"
#: The hero is the allowance itself, in live text, substituted from the caps
#: the server actually enforces so it cannot drift from `settings.py`.
WELCOME_HERO_LABEL = "searches a day under your name, {month_cap} a month"
WELCOME_HERO_NOTE = (
    "One search covers one date and one airport, so a wide scan burns "
    "through that faster than it looks."
)
WELCOME_CTA_LABEL = "Read the five minute guide"

WELCOME_FOOTER_LINES = [
    "You're getting this because you signed in to the free FlightPowers MCP server.",
    "A welcome note and about one email a month, nothing else.",
]


def _welcome_parts(day_cap: int, month_cap: int, link: str) -> tuple[str, str]:
    """The plain-text part and the HTML part of the welcome note.

    The text is the approved copy, byte for byte, with only the caps and the
    unsubscribe URL filled in. The HTML says the same sentences in the
    branded template.
    """
    fields = {"day_cap": f"{day_cap:,}", "month_cap": f"{month_cap:,}"}
    text_paras = [
        para.format(guide=GUIDE_URL, **fields) for para in WELCOME_PARAGRAPHS
    ]
    footer = _footer_text(link)
    text = "\n\n".join(text_paras) + "\n\n--\n" + footer + "\n"
    html = _branded(
        BrandedEmail(
            subject=WELCOME_SUBJECT.format(day_cap=fields["day_cap"]),
            preheader=WELCOME_PREHEADER,
            paragraphs=[para.format(**fields) for para in WELCOME_HTML_FLOW],
            hero=Hero(
                eyebrow=WELCOME_HERO_EYEBROW,
                number=fields["day_cap"],
                label=WELCOME_HERO_LABEL.format(**fields),
                note=WELCOME_HERO_NOTE,
            ),
            cta=Cta(label=WELCOME_CTA_LABEL, url=GUIDE_URL),
            signoff=["Matan"],
            footer_lines=list(WELCOME_FOOTER_LINES),
            unsubscribe_url=link,
        )
    )
    return text, html


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
    """One transactional send at first sign-in. True if Resend took it.

    Off unless `FREE_SIGNIN_WELCOME=on`. The caller owns the "once per user,
    ever" guard -- `free_mcp_users.welcome_sent_at`, claimed and stamped in
    one statement BEFORE this runs, because a guard that is "the absence of
    an error" re-sends the note on every redeploy.
    """
    return (
        await welcome_outcome(
            email,
            token,
            config,
            day_cap=day_cap,
            month_cap=month_cap,
            client=client,
            timeout=timeout,
        )
        == SENT
    )


async def welcome_outcome(
    email: str,
    token: str,
    config: MailConfig,
    *,
    day_cap: int,
    month_cap: int,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> str:
    address = (email or "").strip().lower()
    if not address or "@" not in address or not config.can_send or not config.welcome:
        return REFUSED
    link = unsubscribe_url(config, token)
    text, html = _welcome_parts(day_cap, month_cap, link)
    return await _post(
        {
            "from": FROM_ADDRESS,
            "reply_to": FROM_ADDRESS,
            "to": [address],
            "subject": WELCOME_SUBJECT.format(day_cap=f"{day_cap:,}"),
            "text": text,
            "html": html,
            # One-click unsubscribe. `{{{RESEND_UNSUBSCRIBE_URL}}}` is a
            # BROADCAST variable and renders literally here, so the link and
            # the header both point at our own route.
            "headers": {
                "List-Unsubscribe": f"<{link}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        },
        config,
        label="welcome note",
        client=client,
        timeout=timeout,
    )


# ── phase 2: the cap note ────────────────────────────────────────────────

#: Sequence file (b). Two openers, one body, one send. Branch D is the
#: person whose scan was cut off; branch M is a steady user with no acute
#: pain, and opening branch M with "your scan stopped halfway" would be a
#: lie about something they can check.
BRANCH_DAILY = "D"
BRANCH_MONTHLY = "M"

CAP_SUBJECTS = {
    BRANCH_DAILY: "your scan stopped halfway",
    BRANCH_MONTHLY: "more room on your routes",
}

CAP_OPENERS = {
    BRANCH_DAILY: (
        "You hit the free daily cap twice this week, which means a scan "
        "stopped before it finished."
    ),
    BRANCH_MONTHLY: (
        "You're about two thirds through this month's free searches and "
        "there's still {days_left} days of the month to go."
    ),
}

#: Exactly ONE URL in the note, chosen by what the account actually
#: searched. The PRO request number differs per listing, so the two travel
#: together and cannot drift apart.
CAP_PRODUCTS = {
    "flights": ("https://flights.flightpowers.com/mcp", "2,500"),
    "hotels": ("https://hotels.flightpowers.com/mcp", "2,000"),
}

CAP_PARAGRAPHS = [
    "Hi,",
    "{opener}",
    "There's a paid server with the same tools, no ads and no daily cap. You "
    "sign in with the same Google account you used on the free one, paste a "
    "RapidAPI key once, and it's done. BASIC is free with 10 requests a month, "
    "PRO is $10 for {requests} requests. Billed to your own plan.",
    "{url}",
    "If that's not worth it to you, tell me what would be. I read every reply.",
    "Matan",
]


def cap_note_branch(
    signals: CapSignals, month_cap: int
) -> str | None:
    """Which branch fires, or None. The whole trigger, and nothing else.

    Pure, and separate from the send, because this is the part that decides
    whether a person who did not ask for anything gets an email. Both
    branches require two distinct active days ever: one afternoon of a
    script hammering the server is not a user, it is a test.
    """
    if signals.active_days_ever < 2:
        return None
    if signals.cap_days_7 >= 2:
        return BRANCH_DAILY
    if (
        month_cap > 0
        and signals.month_searches * 100 >= month_cap * 60
        and signals.days_left_in_month >= 5
    ):
        return BRANCH_MONTHLY
    return None


def cap_note_product(signals: CapSignals) -> str:
    """`hotels` or `flights`. Flights when we cannot tell.

    Not a coin flip: flights is the larger listing and the one every printed
    guide already points at, so an account with no per-tool rows at all --
    everyone who used the server before `free_mcp_user_tools` existed -- gets
    the link that is right more often.
    """
    return "hotels" if signals.hotel_majority else "flights"


#: The cap-note copy, re-split for the designed HTML part the same way the
#: welcome is: the standalone URL paragraph becomes the one button, and the
#: account's own month-to-date usage becomes the hero. Every sentence is
#: still the sequence file's.
CAP_HTML_FLOW = [
    "Hi,",
    "{opener}",
    ":::hero",
    "There's a paid server with the same tools, no ads and no daily cap. You "
    "sign in with the same Google account you used on the free one, paste a "
    "RapidAPI key once, and it's done. BASIC is free with 10 requests a month, "
    "PRO is $10 for {requests} requests. Billed to your own plan.",
    ":::cta",
    "If that's not worth it to you, tell me what would be. I read every reply.",
]

CAP_PREHEADERS = {
    BRANCH_DAILY: "The paid server has the same tools, no ads and no daily cap.",
    BRANCH_MONTHLY: "The paid server has the same tools, no ads and no daily cap.",
}

CAP_HERO_EYEBROW = "your month so far"
CAP_HERO_LABEL = "of {month_cap} free searches this month"
CAP_CTA_LABEL = "Set up the paid server"

CAP_FOOTER_LINES = [
    "You're getting this because you signed in to the free FlightPowers MCP server.",
    "About one email a month, nothing else.",
]


def _cap_hero(signals: CapSignals, month_cap: int) -> Hero | None:
    """Their own usage, or no hero at all.

    The number in this card is the one thing in the note the reader can
    check against their own logs, so it is their month-to-date searches and
    nothing else. When we have no count -- an account whose capped days were
    last month, or a store that answered 0 -- the block is dropped rather
    than printed as a zero: a hero that says 0 is worse than no hero.
    """
    if signals.month_searches <= 0 or month_cap <= 0:
        return None
    return Hero(
        eyebrow=CAP_HERO_EYEBROW,
        number=f"{signals.month_searches:,}",
        label=CAP_HERO_LABEL.format(month_cap=f"{month_cap:,}"),
    )


def _cap_parts(
    branch: str, signals: CapSignals, link: str, month_cap: int = 0
) -> tuple[str, str, str]:
    """Subject, plain text, HTML. The text is the approved copy verbatim."""
    product = cap_note_product(signals)
    url, requests = CAP_PRODUCTS[product]
    opener = CAP_OPENERS[branch].format(days_left=signals.days_left_in_month)
    fields = {"opener": opener, "requests": requests}
    text_paras = [para.format(url=url, **fields) for para in CAP_PARAGRAPHS]
    footer = _footer_text(link, monthly=False)
    text = "\n\n".join(text_paras) + "\n\n--\n" + footer + "\n"
    html = _branded(
        BrandedEmail(
            subject=CAP_SUBJECTS[branch],
            preheader=CAP_PREHEADERS[branch],
            paragraphs=[para.format(**fields) for para in CAP_HTML_FLOW],
            hero=_cap_hero(signals, month_cap),
            cta=Cta(label=CAP_CTA_LABEL, url=url),
            signoff=["Matan"],
            footer_lines=list(CAP_FOOTER_LINES),
            unsubscribe_url=link,
        )
    )
    return CAP_SUBJECTS[branch], text, html


async def send_cap_note(
    email: str,
    token: str,
    config: MailConfig,
    *,
    branch: str,
    signals: CapSignals,
    month_cap: int = 0,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> bool:
    """One transactional send. True if Resend took it.

    Off unless `FREE_SIGNIN_CAPNOTE=on`. The caller owns the "once per 30
    days" guard, claimed in one statement before this runs.
    """
    return (
        await cap_note_outcome(
            email,
            token,
            config,
            branch=branch,
            signals=signals,
            month_cap=month_cap,
            client=client,
            timeout=timeout,
        )
        == SENT
    )


async def cap_note_outcome(
    email: str,
    token: str,
    config: MailConfig,
    *,
    branch: str,
    signals: CapSignals,
    #: Only the hero card needs it: "1,400 of 2,000 free searches this
    #: month". Zero drops the card rather than printing a denominator we
    #: are not sure of.
    month_cap: int = 0,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> str:
    address = (email or "").strip().lower()
    if (
        not address
        or "@" not in address
        or not config.can_send
        or not config.cap_note
        or branch not in CAP_SUBJECTS
    ):
        return REFUSED
    link = unsubscribe_url(config, token)
    subject, text, html = _cap_parts(branch, signals, link, month_cap)
    return await _post(
        {
            "from": FROM_ADDRESS,
            "reply_to": FROM_ADDRESS,
            "to": [address],
            "subject": subject,
            "text": text,
            "html": html,
            "headers": {
                "List-Unsubscribe": f"<{link}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        },
        config,
        label="cap note",
        client=client,
        timeout=timeout,
    )


async def maybe_send_cap_note(
    store,
    google_sub: str,
    config: MailConfig,
    *,
    month_cap: int,
    now: float | None = None,
) -> str | None:
    """Evaluate the triggers and, if one fires, send exactly one cap note.

    Returns the branch that was sent, or None. Best effort throughout and it
    never raises: this runs from the same background task that records
    usage, after the tool call has already been answered, and an upsell is
    never a reason to fail somebody's search.

    Order matters and is the whole point:

        1. read the signals  -- one query, no side effects
        2. pick a branch     -- pure, `cap_note_branch`
        3. CLAIM the send    -- one UPDATE that also enforces every
                                frequency rule, so two instances racing the
                                same trigger produce one note
        4. send              -- and release the claim only on a REFUSED,
                                never on a timeout

    The sequence file has this evaluated by a 09:00 UTC job. There is no
    scheduler on this deployment, so it is evaluated here instead, on the
    call that would have produced the row the job would have read. The
    guards are the same ones and they live in the database, so the two are
    interchangeable if a cron ever appears.
    """
    if not config.can_send or not config.cap_note:
        return None
    try:
        signals = await store.cap_signals(google_sub, now)
    except Exception as exc:  # noqa: BLE001 - never fail a tool call
        logger.debug("could not read the cap-note signals: %s", exc)
        return None
    branch = cap_note_branch(signals, month_cap)
    if branch is None:
        return None
    try:
        row = await store.get(google_sub)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the row for the cap note: %s", exc)
        return None
    if row is None or not row.email or row.email_opt_out:
        return None
    try:
        claimed = await store.claim_cap_note(google_sub, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not claim the cap note: %s", exc)
        return None
    if not claimed:
        return None
    outcome = await cap_note_outcome(
        row.email,
        row.unsubscribe_token,
        config,
        branch=branch,
        signals=signals,
        month_cap=month_cap,
    )
    if outcome == SENT:
        return branch
    if outcome == REFUSED:
        try:
            await store.release_cap_note(google_sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not release the cap-note claim: %s", exc)
    return None


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
    2. CLAIM the welcome, then send it, IF `FREE_SIGNIN_WELCOME=on` and the
       address has not opted out. Claim first: `mark_welcome_sent` is an
       `UPDATE ... WHERE welcome_sent_at IS NULL` and its row count is the
       only thing that can tell two instances racing one first sign-in apart.
       Sending first and stamping after leaves a window in which both send.
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

    if not opted_out and config.welcome and config.can_send:
        try:
            if await store.mark_welcome_sent(google_sub):
                outcome = await welcome_outcome(
                    email, token, config, day_cap=day_cap, month_cap=month_cap
                )
                if outcome == REFUSED:
                    # Resend answered and declined, so nothing is in flight
                    # and the claim is a lie. A timeout is NOT released: the
                    # message may have gone, and a second copy is worse than
                    # none.
                    await store.release_welcome(google_sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("welcome hand-off failed: %s", exc)

    try:
        await store.set_resend_state(google_sub, state)
    except Exception as exc:  # noqa: BLE001 - best effort by construction
        logger.warning("could not record the Resend state: %s", exc)
    return state
