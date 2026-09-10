"""Branded HTML for the mail this server sends.

One template, ported from the api-growth repo's
`state/gtm/email/templates/flightpowers-broadcast.html`, so a note from the
free server looks like the newsletter and the landing page rather than like
a system message. The rules live in `branded.py`'s header and, with their
evidence, in `state/gtm/email/DESIGN-2026-09-10.md`.

The plain-text part is NOT built here. It stays verbatim from
`state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md`, in `src/maillist.py`:
a designed wrapper must never quietly rewrite copy that was signed off.
"""

from .branded import (
    LOGO_URL,
    MAX_HTML_BYTES,
    Cta,
    Email,
    Hero,
    Steps,
    problems,
    render,
)

__all__ = [
    "Cta",
    "Email",
    "Hero",
    "LOGO_URL",
    "MAX_HTML_BYTES",
    "Steps",
    "problems",
    "render",
]
