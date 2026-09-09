"""
Stay22 affiliate attribution for the booking URLs this server hands back.

Why this exists
---------------
The free server is the ad-supported one: every hotel search is billed to
mrabi and earns whatever the Lulu card earns. The booking links it returns
carry Booking.com's *generic* affiliate id (`aid=304142`), which attributes
nothing to us -- so a user who searched here, liked a price and booked it
produced revenue for someone else.

Stay22 sits in front of that. Their `/allez` endpoint takes a target URL,
attaches their own Booking.com affiliate account plus a `label` identifying
us, and 302s the user to the same property page. The link is a plain URL,
which is the only form that works here: an MCP tool result is JSON read by a
model, so there is nowhere to run their page script and nothing to inject it
into.

The format, and where it comes from
-----------------------------------
    https://www.stay22.com/allez/booking?aid=<AID>&campaign=<SUB>&link=<enc>

**None of this is publicly documented.** `help.stay22.com`,
`docs.stay22.com` and `developers.stay22.com` do not resolve; the knowledge
base at community.stay22.com describes only the Hub's point-and-click Allez
Generator and never names a parameter. The shape above was read out of their
production script, `https://scripts.stay22.com/letmeallez.js`::

    i = new URL(`https://www.stay22.com${e}`);
    i.searchParams.set("aid", s.aid);
    i.searchParams.set("campaign", this.autoGenCampaign());
    ...
    let a = {source:"direct", medium:"deepfish", link:t};
    return E.getAllezURL(`/allez/${i}`, a)

and then confirmed against live redirects on 2026-09-06. Full write-up,
including the observed `location:` headers for every variant tried:
state/gtm/stay22-2026-09-06.md in the agent workspace.

Three things that were verified rather than assumed, because each one fails
silently -- a wrong link still redirects to Booking.com and still looks like
it worked:

* **The parameter is `link`, not `url`.** `url=` is ignored and the redirect
  drops to booking.com's root, losing the property.
* **A wrong or missing `aid` credits Stay22, not us.** `aid` absent, or set
  to a string they do not recognise, both came back as `label=stay22...`
  rather than `label=flightpowers...`. They validate the value; they do not
  echo it. So an unset id must mean "do not wrap", never "wrap anyway".
* **`campaign` becomes the `label` suffix**: `label=<aid>-<campaign>`, which
  is where free-server clicks separate from every other surface of ours.

`/allez/<supplier>` -- the path segment is a supplier key from a map in that
same script (`booking`, `expedia`, `agoda`, `kayak`, `vrbo`, ...). Bare
`/allez` with no supplier answers 200 HTML instead of redirecting, so the
segment is required.

Because this contract is reverse-engineered and unversioned, it can change
without notice. That is a second reason the wrap is env-gated: turning it off
is one variable, not a deploy of new code.

Scope, deliberately
-------------------
FREE SERVER ONLY. `mcp_server_paid` is untouched and must stay that way --
the paid product's whole proposition is that it is the clean, unmonetised
one. Flights are untouched too: `buy_link` is a Google Flights deep link into
an itinerary, not a merchant page, and Stay22 does not monetise it.

Off by default
--------------
`STAY22_AID` empty means every link passes through byte-for-byte and the
result shape does not change at all. That is what this ships as.

What a wrapped row looks like
-----------------------------
    {
      "name": "Hotel Leone",
      "link": "https://www.stay22.com/allez/booking?aid=...&link=https%3A%2F%2F...",
      "booking_url": "https://www.booking.com/hotel/it/leone.html?aid=1607597&label=flightpowers-free-mcp&...",
      "link_note": "affiliate link (Stay22)"
    }

`booking_url` is ALSO rewritten, not just preserved: `aid` becomes Stay22's
own Booking.com account (`1607597`, not Booking's generic `304142`) and
`label` carries ours, replacing whatever was there. Every other Booking.com
parameter -- `checkin`, `checkout`, `dest_id`, `group_adults`, ... -- passes
through untouched. See `rewrite_booking_query`.

`link` is rewritten in place rather than added alongside, on purpose. It is
the field the widget's `rowLink` resolves (`HOTELS_WIDGET_MAPPING`), the
field `BOOK_LINK_FIELDS` looks at for the Book column, and the field every
tool description tells the model to hand the user. Rewriting it is the only
change that reaches all three; adding a fifth link field would leave the
clickable one unattributed. The original is preserved as `booking_url` so
nothing is lost and the rewrite stays auditable from the result itself.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

#: Stay22's server-side redirect, with the `booking` supplier segment. See
#: the module docstring for the source and for why the segment is required.
STAY22_ALLEZ_URL = "https://www.stay22.com/allez/booking"

#: What the rewritten rows say about themselves. Short, factual, and in the
#: result rather than only in the tool description, because a model that
#: quotes a link to a user should be able to say what the link is.
LINK_NOTE = "affiliate link (Stay22)"

#: The field the backend puts the property URL in, and the field the widget
#: and the Book column both read. See the module docstring.
LINK_FIELD = "link"

#: Where the untouched original goes.
ORIGINAL_FIELD = "booking_url"

#: The note field.
NOTE_FIELD = "link_note"

#: Stay22 accepts the stay dates as its OWN top-level parameters and writes
#: them back onto the Booking.com URL it redirects to. That is the documented-
#: by-observation way to keep the dates: a target whose query string is passed
#: through inside `link=` came back as a search-results page for the property
#: rather than the priced property page. So the dates are lifted out of the
#: target and sent alongside, and the target itself goes over bare.
_PASSTHROUGH_DATE_PARAMS = ("checkin", "checkout")

#: Only http(s) targets are wrapped. A relative path, a `javascript:` string
#: or anything else the backend might one day emit is left exactly as it came
#: -- percent-encoding it into a redirect would produce a link that 404s
#: instead of a link that does nothing.
_WRAPPABLE_SCHEMES = {"http", "https"}

#: Never double-wrap. A row that already points at Stay22 (because the
#: backend started emitting one, or because this ran twice) is left alone:
#: nesting a redirect inside a redirect loses the attribution on the inner
#: one and produces a URL long enough to be truncated by chat clients.
_STAY22_HOSTS = ("stay22.com", "www.stay22.com", "embed.stay22.com")

#: A raw Booking.com URL -- what `booking_url` holds, and what `link` held
#: before this module rewrote it. Used to find every such field on a row
#: (not just `booking_url` by name) so a second raw-Booking field the
#: backend might one day add gets the same treatment for free.
_BOOKING_HOSTS = ("booking.com", "www.booking.com")


def _host_of(parsed: Any) -> str:
    return parsed.netloc.split("@")[-1].split(":")[0].lower()


def is_wrappable(url: Any) -> bool:
    """True when `url` is an absolute http(s) URL that is not already Stay22."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in _WRAPPABLE_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    host = _host_of(parsed)
    if host in _STAY22_HOSTS or host.endswith(".stay22.com"):
        return False
    return True


def wrap_url(url: str, *, aid: str, campaign: str = "") -> str:
    """The Stay22 redirect for `url`, or `url` unchanged when off/ineligible.

    The target goes over as the bare property URL -- scheme, host and path,
    with its own query string dropped -- and any `checkin`/`checkout` in that
    query is lifted out and sent as Stay22's own parameters instead. See
    `_PASSTHROUGH_DATE_PARAMS`: passing the target's query through inside
    `link=` is what produced a search-results page instead of the priced
    property. Booking's generic `aid=304142` and its `label` go with the
    dropped query, which is the point -- they are the attribution we are
    replacing.

    Whatever is sent, the value of `link` is percent-encoded in full
    (`safe=""`). `quote`'s default `safe="/"` leaves `&`, `=` and `?` bare,
    which would truncate a target at its first ampersand and hand Stay22 the
    tail as parameters of its own -- a link that still redirects, to the
    wrong page. The full original is always preserved on the row as
    `booking_url`, so nothing here is lossy from the caller's side.
    """
    if not aid or not is_wrappable(url):
        return url

    parsed = urlparse(url)
    bare = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    original_query = parse_qs(parsed.query)

    query = [f"aid={quote(aid, safe='')}"]
    if campaign:
        query.append(f"campaign={quote(campaign, safe='')}")
    for name in _PASSTHROUGH_DATE_PARAMS:
        values = original_query.get(name) or []
        if values and values[0]:
            query.append(f"{name}={quote(values[0], safe='')}")
    query.append(f"link={quote(bare, safe='')}")
    return f"{STAY22_ALLEZ_URL}?{'&'.join(query)}"


def is_booking_url(value: Any) -> bool:
    """True when `value` is an absolute http(s) URL on booking.com."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in _WRAPPABLE_SCHEMES:
        return False
    host = _host_of(parsed)
    return host in _BOOKING_HOSTS or host.endswith(".booking.com")


def booking_label(aid: str, campaign: str = "") -> str:
    """The `label` Stay22's own redirect produces for `aid`/`campaign`.

    Matches what a live `/allez/booking` redirect writes onto the
    Booking.com URL it 302s to (`label=<aid>-<campaign>`, or bare `<aid>`
    with no campaign) -- see state/gtm/stay22-2026-09-06.md ss4. Kept as one
    function so `link` and `booking_url` never end up attributed under two
    different labels for the same click.
    """
    return f"{aid}-{campaign}" if campaign else aid


def rewrite_booking_query(url: str, *, booking_aid: str, label: str) -> str:
    """Returns a raw Booking.com `url` with its `aid`/`label` replaced.

    `booking_aid` is Stay22's OWN Booking.com affiliate account
    (`aid=1607597`, confirmed live 2026-09-06), not ours -- Stay22 is the
    party with the Booking.com partnership, so their account id is what has
    to sit in `aid=` for the click to be billable at all. `label` is where
    our attribution actually lives, and it is built by `booking_label` so it
    matches what the `link` redirect already produces.

    Every other query parameter (`checkin`, `checkout`, `dest_id`,
    `group_adults`, whatever else the backend sent) passes through
    untouched, in its original position -- only `aid` and `label` are
    dropped and re-appended. `url` is returned unchanged when `booking_aid`
    is empty (the off switch) or `url` is not a Booking.com URL, so a caller
    can run this over every field on a row without checking first.
    """
    if not booking_aid or not is_booking_url(url):
        return url
    parsed = urlparse(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ("aid", "label")
    ]
    kept.append(("aid", booking_aid))
    kept.append(("label", label))
    new_query = urlencode(kept, quote_via=quote)
    return urlunparse(parsed._replace(query=new_query))


def apply_to_rows(
    rows: list[dict[str, Any]],
    *,
    aid: str,
    campaign: str = "",
    booking_aid: str = "",
) -> list[dict[str, Any]]:
    """Returns `rows` with every eligible `link` wrapped for attribution.

    Copies rather than mutating, for the reason `_annotate_book_labels` does:
    the rows handed in are the caller's, and in tests they are module-level
    fixtures shared across cases -- rewriting one in place leaked into later
    tests as a pass that depended on execution order.

    A row is left byte-for-byte alone when the wrap is off (`aid` empty), when
    its `link` is missing, empty, non-http or already a Stay22 URL, or when it
    already carries a `booking_url` of its own (that would be the backend's
    field, not ours to overwrite -- the same rule `book_label` follows).

    When a row IS wrapped and `booking_aid` is set, every remaining raw
    Booking.com URL on the row -- `booking_url`, the original this function
    just preserved, and any other such field the backend might send -- gets
    its `aid`/`label` rewritten too (`rewrite_booking_query`). `link` itself
    is skipped: it is already the Stay22 redirect, not a Booking.com URL, by
    the time this runs. Without this, a caller reading `booking_url` instead
    of following the redirect in `link` would still see Booking's generic
    `aid=304142` and attribute nothing to us.
    """
    if not aid:
        return rows

    label = booking_label(aid, campaign)
    wrapped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            wrapped.append(row)
            continue
        original = row.get(LINK_FIELD)
        if ORIGINAL_FIELD in row or not is_wrappable(original):
            wrapped.append(row)
            continue
        new_row = {
            **row,
            LINK_FIELD: wrap_url(original, aid=aid, campaign=campaign),
            ORIGINAL_FIELD: original,
            NOTE_FIELD: LINK_NOTE,
        }
        if booking_aid:
            for key, value in list(new_row.items()):
                if key == LINK_FIELD:
                    continue
                if is_booking_url(value):
                    new_row[key] = rewrite_booking_query(
                        value, booking_aid=booking_aid, label=label
                    )
        wrapped.append(new_row)
    return wrapped
