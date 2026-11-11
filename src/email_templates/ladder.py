"""The upsell ladder: the cap note, the day-7 note and the dormant note.

PROVENANCE. Every sentence below is `state/gtm/email/FREE-SIGNIN-UPSELL-2026-09-17.md`
§2 (a), (b) and (c), verbatim, detector-clean at 0 hits. Three notes, three
states, and each one earns its place by going to somebody in a state the
others do not cover:

    cap        a search was refused today. Something broke for them.
    day7       ten or more searches in the first week, never capped.
               Nothing is wrong; they just have not found the good part.
    dormant14  two weeks, one search or none. A question, not a pitch, and
               the LAST automated note that address ever gets.

Why the copy is built here and not pre-rendered
-----------------------------------------------
`notes.py` holds the day-2 note as the exact bytes `email-render.mjs`
produced, because that note was designed in the api-growth repo and taken
across whole. These three are shorter, they share a body shape, and two of
their numbers are per-account (the day cap, the reader's own search count),
so they are composed from the branded renderer instead -- the same path the
welcome note has always used. The plain-text part is still the reviewed
copy byte for byte; the HTML says the same sentences in the template.

The placeholders, and the two that can be empty
-----------------------------------------------
    first_name   " Alex" or "". The greeting is written `Hi{{first_name}} !`
                 so an empty value leaves the reviewed copy's "Hi !".
    gift_fare    " (164$ TLV to Athens nonstop both ways, one call)" or "".
                 Re-priced at send time by `src/giftfare.py`; a lookup that
                 does not answer DROPS the bracket rather than printing a
                 fare from yesterday (CLAUDE.md rule 1).
    day_cap      the live per-user day cap, so a cap change in settings can
                 never leave a note quoting the old number.
    searches     day-7 only. The reader's own first-week count.
    connect_url  flights or hotels `/connect`, chosen by what the account
                 actually searched. Never `/mcp`: a human who clicks that
                 in a mail client gets a raw JSON 401 (pass 9, 2026-09-11).
    pro_requests travels with the URL -- 2,500 on flights, 2,000 on hotels
                 -- so the two cannot drift apart.
    basic_requests the free BASIC plan of the same product (BASIC_REQUESTS):
                 50 on flights since 2026-11-08, 10 on hotels.
"""

from __future__ import annotations

from dataclasses import dataclass

from .branded import Cta, Email, Hero, render

# ── the footer, identical on all three ───────────────────────────────────

FOOTER_LINES = [
    "You're getting this because you signed in to the free FlightPowers MCP server.",
    "About one email a month, nothing else.",
]


def footer_text(link: str) -> str:
    return (
        "You're getting this because you signed in to the free FlightPowers "
        f"MCP server.\nAbout one email a month, nothing else. Unsubscribe: {link}"
    )


@dataclass(frozen=True)
class NoteParts:
    """One note, ready for `POST /emails`."""

    subject: str
    text: str
    html: str


#: Every note in this ladder signs off the way Matan's own emails do.
SIGNOFF = ["Regards,", "Matan"]

#: There is no fallback cap number, on purpose. A caller that could not
#: read `settings.fair_use_day_cap` does not know what this server
#: enforces, and a literal in an email is a number nobody can trace to
#: anything (CLAUDE.md rule 1). When the cap is unknown the SENTENCE that
#: quotes it is dropped instead -- see `CAP_OPENERS` and `DAY7_NO_CAP`.


def _text(paragraphs: list[str], link: str) -> str:
    body = list(paragraphs) + ["\n".join(SIGNOFF)]
    return "\n\n".join(body) + "\n\n--\n" + footer_text(link) + "\n"


def _render(
    *,
    subject: str,
    preheader: str,
    paragraphs: list[str],
    link: str,
    hero: Hero | None = None,
    cta: Cta | None = None,
) -> str:
    return render(
        Email(
            subject=subject,
            preheader=preheader,
            paragraphs=paragraphs,
            hero=hero,
            cta=cta,
            signoff=list(SIGNOFF),
            footer_lines=list(FOOTER_LINES),
            unsubscribe_url=link,
        )
    )


# ── (a) the cap note ─────────────────────────────────────────────────────

#: Two branches, one body. Branch D is the person whose scan was cut off
#: today -- which is exactly what `cap_candidates` selects, "refused at
#: least once today", so the opener says *today* and not the old copy's
#: "twice this week", which would have been untrue for a first-time capper
#: (the bug the 2026-09-17 read found in the job path).
BRANCH_DAILY = "D"
BRANCH_MONTHLY = "M"

CAP_SUBJECTS = {
    BRANCH_DAILY: "your scan stopped halfway",
    BRANCH_MONTHLY: "more room on your routes",
}

#: Branch D in two sentences, because the second one quotes the day cap
#: and is dropped whole when we do not know it.
CAP_OPENERS = {
    BRANCH_DAILY: (
        "You hit the free cap today, so a search stopped before it "
        "finished."
    ),
    BRANCH_MONTHLY: (
        "You're about two thirds through this month's free searches and "
        "there's still {days_left} days of the month to go."
    ),
}

#: The closing ask, per branch. Branch D's is the reviewed line from the
#: upsell file; branch M keeps the older approved one, because nothing has
#: been cut off for that reader and asking what they were scanning "when it
#: cut out" would describe something that did not happen to them.
CAP_CLOSERS = {
    BRANCH_DAILY: "Would love to hear what you were scanning when it cut out.",
    BRANCH_MONTHLY: (
        "If that's not worth it to you, tell me what would be. I read every reply."
    ),
}

#: The second sentence of branch D's opener. Printed only when the cap is
#: known; the note reads correctly without it.
CAP_WHY = (
    " The cap is {day_cap} a day because every one of those is a real "
    "proxy call I pay for."
)

CAP_OFFER = (
    "The paid server has the same tools, no ads and no daily cap, and it "
    "bills to your own RapidAPI key. BASIC is free with {basic_requests} requests a "
    "month, PRO is 10$ for {pro_requests}, and a round trip counts as one "
    "request there{gift_fare}."
)

CAP_STEP = "Subscribe on RapidAPI, then paste the key once at {connect_url}."

#: Exactly ONE URL in a note, chosen by what the account actually
#: searched, and it is `/connect` rather than `/mcp`: `/mcp` is the machine
#: endpoint and a human who clicks it in a mail client gets a raw JSON 401
#: (pass 9, 2026-09-11). The PRO request count travels with the URL so the
#: two cannot drift apart. One table, read by both send paths.
CONNECT_PRODUCTS = {
    "flights": ("https://flights.flightpowers.com/connect", "2,500"),
    "hotels": ("https://hotels.flightpowers.com/connect", "2,000"),
}

#: The free BASIC plan per listing, monthly, for NEW RapidAPI subscribers.
#: Flights went 10 -> 50 on 2026-11-08 06:51:59Z (a test until 2026-11-22;
#: a rollback is this one line). Hotels (booking-live-api) stays 10. Keyed
#: like CONNECT_PRODUCTS so a note's BASIC and PRO numbers come from the
#: same product choice.
BASIC_REQUESTS = {
    "flights": "50",
    "hotels": "10",
}

CAP_PREHEADER = "The paid server has the same tools, no ads and no daily cap."
CAP_HERO_EYEBROW = "your month so far"
CAP_HERO_LABEL = "of {month_cap} free searches this month"
CAP_CTA_LABEL = "Connect your key"


def _cap_hero(month_searches: int, month_cap: int) -> Hero | None:
    """Their own usage, or no hero at all.

    The number in this card is the one thing the reader can check against
    their own logs. With no count -- an account whose capped days were last
    month, or a store that answered 0 -- the block is dropped rather than
    printed as a zero.
    """
    if month_searches <= 0 or month_cap <= 0:
        return None
    return Hero(
        eyebrow=CAP_HERO_EYEBROW,
        number=f"{month_searches:,}",
        label=CAP_HERO_LABEL.format(month_cap=f"{month_cap:,}"),
    )


def cap_note(
    *,
    unsubscribe_url: str,
    connect_url: str,
    pro_requests: str,
    basic_requests: str = BASIC_REQUESTS["flights"],
    branch: str = BRANCH_DAILY,
    first_name: str = "",
    gift_fare: str = "",
    day_cap: int = 0,
    days_left: int = 0,
    month_searches: int = 0,
    month_cap: int = 0,
) -> NoteParts:
    opener = CAP_OPENERS[branch].format(days_left=days_left)
    if branch == BRANCH_DAILY and day_cap > 0:
        opener += CAP_WHY.format(day_cap=f"{day_cap:,}")
    offer = CAP_OFFER.format(
        basic_requests=basic_requests, pro_requests=pro_requests, gift_fare=gift_fare
    )
    step = CAP_STEP.format(connect_url=connect_url)
    closer = CAP_CLOSERS[branch]
    paragraphs = [f"Hi{first_name} !", opener, offer, step, closer]
    html_flow = [f"Hi{first_name} !", opener, ":::hero", offer, ":::cta", closer]
    return NoteParts(
        subject=CAP_SUBJECTS[branch],
        text=_text(paragraphs, unsubscribe_url),
        html=_render(
            subject=CAP_SUBJECTS[branch],
            preheader=CAP_PREHEADER,
            paragraphs=html_flow,
            link=unsubscribe_url,
            hero=_cap_hero(month_searches, month_cap),
            cta=Cta(label=CAP_CTA_LABEL, url=connect_url),
        ),
    )


# ── (b) the day-7 note ───────────────────────────────────────────────────

DAY7_SUBJECT = "the thing almost nobody tries"

DAY7_PARAGRAPHS = [
    "Hi{first_name} !",
    "You've run {searches} searches in your first week on the free server, "
    "so here's the thing I actually built it for and almost nobody tries.",
    "Don't ask it once. Tell your assistant to check the route every "
    "morning and ping you when the fare drops under your number, and it "
    "keeps looking while you don't. The same endpoints sit inside an n8n "
    "or Zapier flow if you'd rather it ran without a chat window.",
    "{watch_line}",
    "Would love to hear which routes you'd put on a watch.",
]

#: The same sentence with and without the number. The cap is the vivid
#: part, so it is used whenever we know it -- but an unknown cap costs the
#: figure, never the sentence or its link.
DAY7_WATCH = (
    "A watch list like that eats the {day_cap} a day fast, and past it "
    "you're on your own RapidAPI key: {connect_url}"
)
DAY7_NO_CAP = (
    "A watch list like that eats the free allowance fast, and past it "
    "you're on your own RapidAPI key: {connect_url}"
)

DAY7_PREHEADER = "Tell it to keep looking while you don't."
DAY7_HERO_EYEBROW = "your first week"
DAY7_HERO_LABEL = "searches on the free server"
DAY7_HERO_NOTE = (
    "A morning check on one route is one search a day. A watch list is "
    "that, times the routes you care about."
)
DAY7_CTA_LABEL = "Connect your key"


def day7_note(
    *,
    unsubscribe_url: str,
    connect_url: str,
    searches: int,
    first_name: str = "",
    day_cap: int = 0,
) -> NoteParts:
    watch = DAY7_WATCH if day_cap > 0 else DAY7_NO_CAP
    fields = {
        "first_name": first_name,
        "searches": f"{searches:,}",
        "connect_url": connect_url,
        "watch_line": watch.format(
            day_cap=f"{day_cap:,}", connect_url=connect_url
        ),
    }
    paragraphs = [para.format(**fields) for para in DAY7_PARAGRAPHS]
    # In the HTML twin the bare URL becomes the one button, the way every
    # other note in this lane renders it; the sentence keeps its own link
    # in the plain-text part so that part is complete on its own.
    html_flow = [
        paragraphs[0],
        paragraphs[1],
        ":::hero",
        paragraphs[2],
        paragraphs[3].split(":")[0].rstrip() + ".",
        ":::cta",
        paragraphs[4],
    ]
    return NoteParts(
        subject=DAY7_SUBJECT,
        text=_text(paragraphs, unsubscribe_url),
        html=_render(
            subject=DAY7_SUBJECT,
            preheader=DAY7_PREHEADER,
            paragraphs=html_flow,
            link=unsubscribe_url,
            hero=Hero(
                eyebrow=DAY7_HERO_EYEBROW,
                number=fields["searches"],
                label=DAY7_HERO_LABEL,
                note=DAY7_HERO_NOTE,
            ),
            cta=Cta(label=DAY7_CTA_LABEL, url=connect_url),
        ),
    )


# ── (c) the dormant note ─────────────────────────────────────────────────

DORMANT_SUBJECT = "what were you trying to search?"

#: No link, no price, no product path, no button, no hero. It is a
#: question, and the reply IS the deliverable. It is also the last
#: automated note this address ever gets: welcome, day-2, dormant, and then
#: the lane is closed for them.
DORMANT_PARAGRAPHS = [
    "Hi{first_name} !",
    "You signed in two weeks ago and it went quiet after that, so I'm "
    "curious rather than pushy here.",
    "What did you want to search? Send me the route and a rough window and "
    "I'll run it on my own key and send you back what it finds, and if the "
    "server just never connected in your client then tell me which client "
    "you were in and I'll go look at it.",
    "No pitch in this one. I'm trying to work out whether people stop at "
    "the sign-in or at the answers.",
]

DORMANT_PREHEADER = "Send me the route and I'll run it on my own key."


def dormant_note(*, unsubscribe_url: str, first_name: str = "") -> NoteParts:
    paragraphs = [para.format(first_name=first_name) for para in DORMANT_PARAGRAPHS]
    return NoteParts(
        subject=DORMANT_SUBJECT,
        text=_text(paragraphs, unsubscribe_url),
        html=_render(
            subject=DORMANT_SUBJECT,
            preheader=DORMANT_PREHEADER,
            paragraphs=paragraphs,
            link=unsubscribe_url,
        ),
    )
