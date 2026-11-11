"""
The free server's own privacy page, served at `/privacy` on every hostname.

Why it exists
-------------
Until 2026-10-22 this server linked `https://flightpowers.com/privacy`, a
page about the marketing site that says queries are "not tied to a profile"
and never mentions what this server actually keeps: Google sign-ins, per-user
search counts, the email notes, the route counters in Upstash, the Lulu card
and the Stay22 links. `/privacy` on this host answered 404. Directory and
connector rules (Anthropic 3.A, OpenAI) expect a policy that matches the
service, so the page lives here, next to the code it describes.

Every sentence is meant to be checkable against this directory:

* sign-in rows       migrations/001-003, src/freeusers.py
* the delete button  src/oauthroutes.py `/connect/delete` (freeusers.delete)
* unsubscribe        src/oauthroutes.py `/email/unsubscribe`
* search log line    src/telemetry.py `[tool_call]` (route, dates, fp code)
* counters + routes  src/stores.py (RETENTION_HOURS, FAIR_USE_*_TTL_SECONDS)
* the fp code        src/policy.py `client_fingerprint` (hash of IP + user
                     agent + MCP session id); the raw IP still reaches the
                     hosting logs via telemetry `source_ip` and the
                     oauthroutes rate-limit warning
* app registrations  src/oauth.py `/oauth/register` (`registered_ip`,
                     `client_name` in free_mcp_oauth_clients; approved
                     clients are never swept)
* signed-in counters src/fair_use.py (keyed by a hash of the Google id)
* daily summary      .github/workflows/free-mcp-metrics-snapshot.yml
                     (metrics/free-mcp/<date>.json, signins.recent rows)
* Lulu               lulu_ads/middleware.py sends `{"tool": <name>}` only
* Stay22             src/affiliate.py
* backend headers    src/lambda_client.py (no user id goes to the backend)

Change the code, change this page. No JavaScript, nothing fetched.
"""

from __future__ import annotations

from .pages import TERMS_URL, e, page
from .stores import RETENTION_HOURS

TITLE = "FlightPowers free MCP: privacy"
EFFECTIVE = "2026-10-22"
CONTACT = "app@flightpowers.com"

SITE_PRIVACY = "https://flightpowers.com/privacy"
PAID_FLIGHTS_PRIVACY = "https://flights.flightpowers.com/privacy"
PAID_HOTELS_PRIVACY = "https://hotels.flightpowers.com/privacy"

#: How long the Upstash counters live, from the store's own TTL.
COUNTER_DAYS = RETENTION_HOURS // 24


def privacy_html() -> str:
    body = (
        f"<h1>{e(TITLE)}</h1>"
        f'<p class="note">Effective {e(EFFECTIVE)}. A plain-language account of '
        "what this server's code does, written by the developer who runs it. "
        "It is not legal advice.</p>"
        "<p>This page covers the free, ad-supported MCP server at "
        "<code>free-trial.flightpowers.com</code> (and its older names "
        "<code>google-flights-lulu.flightpowers.com</code> and "
        "<code>lulu.flightpowers.com</code>). The paid servers have their own "
        f'pages (<a href="{e(PAID_FLIGHTS_PRIVACY)}">flights</a>, '
        f'<a href="{e(PAID_HOTELS_PRIVACY)}">hotels</a>), and so do the website '
        f'and the demo (<a href="{e(SITE_PRIVACY)}">flightpowers.com/privacy</a>).</p>'
        "<h2>When you sign in with Google</h2>"
        "<p>We ask Google for your account id and your email address. We keep:</p>"
        "<ul>"
        "<li>your Google account id and email address;</li>"
        "<li>when you first signed in and when you last used the server;</li>"
        "<li>which AI app you approved (for example Claude or Cursor);</li>"
        "<li>how many searches you ran, in total and per day, split into "
        "flights and hotels, and on which days you reached the daily limit;</li>"
        "<li>which of our emails we sent you, and when;</li>"
        "<li>the sign-in tokens your AI app uses (stored scrambled, and they "
        "expire).</li>"
        "</ul>"
        "<p>When your AI app first connects, it registers with this server. We "
        "keep that registration: the app's name and the IP address it "
        "registered from. A registration nobody approves within 7 days is "
        "deleted; one you approve is kept with no end date.</p>"
        "<p>Why: to give you your own daily and monthly allowance, and to write "
        "to you (see Emails). We keep it until you delete your account.</p>"
        "<h2>Your searches</h2>"
        "<p>To answer a search, the server sends what you asked for (airports or "
        "a destination, dates, travellers, filters, currency) to our own search "
        "backend, which reads Google Flights or Booking.com. Your account id and "
        "email are not sent with it. What we keep about searches:</p>"
        "<ul>"
        "<li><strong>A log line per search</strong> with the route, dates, nights, "
        "travellers, stops and currency, a short scrambled code made from "
        "your connection's IP address, app details and session, and your IP "
        "address itself. Our hosting keeps these logs for about a day.</li>"
        f"<li><strong>Counters for {COUNTER_DAYS} days</strong>: searches per hour "
        "by tool and by that code, and per day the routes searched (from and to "
        "only, no dates, no prices) by that code. If you are signed in, your "
        "daily and monthly search counts are kept under a scrambled form of "
        "your Google account id. They are deleted automatically "
        f"after {COUNTER_DAYS} days.</li>"
        "<li><strong>A daily summary</strong> saved in our own private records: "
        "totals, the most searched routes (from and to, with the scrambled code), "
        "the most active codes, and the latest sign-ins: the email address "
        "masked (like <code>j***n@gmail.com</code>), the AI app, first and "
        "last use to the second, search counts, which of our emails were sent "
        "and whether you opted out. It has no set end date.</li>"
        "<li><strong>Failed flight searches.</strong> When a flight search fails, "
        "our backend saves its inputs (airports, dates, filters, currency) with "
        "the error, so we can fix it. Your account, email, IP address and "
        "scrambled code are not saved with them. There is no automatic "
        "deletion yet: they are kept until we delete them.</li>"
        "<li>The search backends' own logs also include the search; the "
        "flight backend keeps them for about a day.</li>"
        "</ul>"
        "<p>The fares and hotel rates we find are returned to you and are not "
        "stored.</p>"
        "<h2>The sponsored card (Lulu)</h2>"
        "<p>Each result carries one sponsored card from Lulu "
        "(<code>getlulu.dev</code>); that is what pays for the free tier. To pick "
        "the card, this server sends Lulu only the name of the tool you used: "
        "not your search, not your account. Your AI app then shows the card, and "
        "the card may report to Lulu that it was shown or opened (in a terminal "
        "app, this server sends that signal). Lulu handles that under its own "
        "privacy policy.</p>"
        "<h2>Hotel links (Stay22)</h2>"
        "<p>Hotel booking links go through Stay22 (<code>stay22.com</code>), so we "
        "earn a commission if you book. The link carries the Booking.com page "
        "for that hotel, your dates and number of guests; when you open it, Stay22 sees the click "
        "and sends you on to Booking.com. Flight links go straight to Google "
        "Flights.</p>"
        "<h2>Emails</h2>"
        "<p>We use your address to send:</p>"
        "<ul>"
        "<li>a welcome note when you first sign in;</li>"
        "<li>a few short notes about your use (for example when you reach the "
        "daily limit, or a few days in) about the free tier and the paid plans;</li>"
        "<li>our product updates: signing in adds your address to that list;</li>"
        "<li>sometimes one personal email from Matan, who runs FlightPowers, "
        "from <code>matan@flightpowers.com</code>.</li>"
        "</ul>"
        "<p>The notes are sent through Resend. To stop them: the unsubscribe "
        "link in a note from this server stops every note from this server, in "
        "one click, with no sign-in. Product updates have their own unsubscribe "
        "link at the bottom of each one. Reply STOP to a personal email. Or write "
        f"to {e(CONTACT)}.</p>"
        "<h2>Deleting your account</h2>"
        '<p>Open <a href="/connect">your account page</a> and press Delete. That '
        "removes your account id, your email address, your usage counts, the "
        "record of emails we sent you and every sign-in token. It does not "
        f"remove the counters (they expire within {COUNTER_DAYS} days), your AI "
        "app's registration (its name and the IP address it registered from), "
        "the failed-search inputs (which do not name you), the daily summaries "
        "(which show your address only masked, with your sign-in times and "
        "search counts), or your address on the product-updates list (use its "
        f"unsubscribe link, or ask us). You can also write to {e(CONTACT)} and we will do it "
        "by hand.</p>"
        "<h2>Who else handles the data</h2>"
        "<ul>"
        "<li>Google: the sign-in, and flight searches run on Google Flights;</li>"
        "<li>Booking.com: hotel searches;</li>"
        "<li>the proxy providers our backend uses to reach those sites;</li>"
        "<li>Vercel (hosting and its logs), Amazon Web Services (the search "
        "backend), Neon (the database), Upstash (the counters);</li>"
        "<li>Resend (the notes), Zoho Mail (the personal email from Matan), "
        "Lulu (the sponsored card), Stay22 (hotel links);</li>"
        "<li>GitHub (hosts the private records that hold the daily summary).</li>"
        "</ul>"
        "<p><strong>We do not sell your personal data</strong>, and we do not give "
        "your email address to advertisers.</p>"
        "<h2>Questions and changes</h2>"
        f"<p>Write to {e(CONTACT)}. When this page changes, the date at the top "
        "changes with it.</p>"
        f'<p class="note"><a href="/">FlightPowers free MCP</a> &middot; '
        f'<a href="{e(TERMS_URL)}">Terms</a></p>'
    )
    return page(TITLE, body)
