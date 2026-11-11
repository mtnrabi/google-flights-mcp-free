"""
The two-page HTML shell the sign-in flow needs, and nothing else.

The paid server renders its pages through `src/legal.py`, which also carries
the privacy policy and the terms. This server has no legal module -- its
terms live on flightpowers.com, its privacy page is src/privacy.py -- so the shell is here, cut down to
what `/connect`, the consent page and `/privacy` use. Copied in shape from
`mcp_server_paid/src/legal.py` (`_STYLE`, `page`) and
`mcp_server_paid/src/connect.py` (`_EXTRA_STYLE`) rather than imported: the
two directories are deployed separately on Vercel, each upload is rooted at
its own folder, and a shared package one level up would simply not be in the
bundle. See the header on `src/oauth.py` for the whole provenance note.
"""

from __future__ import annotations

import html
from typing import Any

CONTACT_EMAIL = "matan@flightpowers.com"

#: Where the free server's policy documents live. The privacy page is served
#: by this deployment (`/privacy`, src/privacy.py) since 2026-10-22: the site's
#: page did not describe the sign-ins, counters, emails, Lulu or Stay22. Absolute
#: and canonical, because the OAuth metadata (`resource_policy_uri`,
#: `op_policy_uri`) must be a full URL. The terms still live on the site.
PRIVACY_URL = "https://free-trial.flightpowers.com/privacy"
TERMS_URL = "https://flightpowers.com/terms"

_STYLE = """
:root { color-scheme: light dark; }
body {
  margin: 0 auto; padding: 3rem 1.25rem 6rem; max-width: 46rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 1.5rem; }
h2 { font-size: 1.2rem; margin: 2.5rem 0 .75rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       font-size: .9em; background: rgba(128,128,128,.15);
       padding: .1em .35em; border-radius: 3px; }
ul { padding-left: 1.3rem; }
li { margin: .3rem 0; }
a { color: inherit; }
.card { border: 1px solid rgba(128,128,128,.35); border-radius: 8px;
        padding: 1.25rem 1.25rem .25rem; margin: 1.5rem 0; }
.btn { display: inline-block; border: 1px solid rgba(128,128,128,.55);
       border-radius: 6px; padding: .55rem 1rem; text-decoration: none;
       font-weight: 600; background: rgba(128,128,128,.12); cursor: pointer;
       font-size: 1rem; color: inherit; }
.btn.danger { font-weight: 400; }
.note { font-size: .9rem; opacity: .8; }
.bad { border-left: 3px solid #c0392b; padding-left: .75rem; }
.good { border-left: 3px solid #27865a; padding-left: .75rem; }
pre { background: rgba(128,128,128,.12); padding: .8rem; border-radius: 6px;
      overflow-x: auto; font-size: .9rem; }
"""


def e(value: Any) -> str:
    """Escape for HTML, attributes included."""
    return html.escape(str(value), quote=True)


def page(title: str, body_html: str, *, extra_style: str = "") -> str:
    """The shell. `extra_style` is appended to the shared stylesheet, for a
    page that needs one or two rules of its own (src/landing.py)."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}{extra_style}</style></head>"
        f"<body>{body_html}</body></html>"
    )


def footer() -> str:
    return (
        f'<p class="note">Questions: {e(CONTACT_EMAIL)}. '
        f'<a href="{e(PRIVACY_URL)}">Privacy</a> &middot; '
        f'<a href="{e(TERMS_URL)}">Terms</a></p>'
    )
