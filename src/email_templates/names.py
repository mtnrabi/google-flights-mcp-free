"""`Hi Alex,` or `Hi,` -- the only name we have, and when not to use it.

Its own module because BOTH send paths need it and they must not import
each other: the inline notes live in `src/maillist.py` and the scheduled
ones in `src/sequence.py`, and `sequence` already imports `maillist`.
"""

from __future__ import annotations


#: Local parts that are never a person. A note opening "Hi Noreply," is
#: worse than one opening "Hi,".
_ROLE_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "admin",
        "billing",
        "contact",
        "dev",
        "help",
        "hello",
        "hi",
        "info",
        "mail",
        "me",
        "no-reply",
        "noreply",
        "postmaster",
        "root",
        "sales",
        "security",
        "support",
        "team",
        "webmaster",
    }
)


def first_name_from_email(email: str) -> str:
    """A leading space plus a capitalised first name, or nothing at all.

    We do not store names -- `free_mcp_users` has the Google `sub`, the
    address and timestamps -- so this is the only source there is, and it is
    a weak one. It therefore answers "" for everything it is not confident
    about: anything with a digit, a dot, a plus or an underscore, anything
    shorter than three letters or longer than twelve, and every role
    address. The greeting then renders as the reviewed copy's plain "Hi,",
    which is the right default and not a degraded one.

    The leading space is deliberate: the placeholder sits inside the
    greeting as `Hi{{first_name}},` so an empty value leaves "Hi," exactly
    as designed rather than "Hi ,".
    """
    local = (email or "").strip().lower().split("@", 1)[0]
    if not local or local in _ROLE_LOCAL_PARTS:
        return ""
    if not local.isalpha() or not (3 <= len(local) <= 12):
        return ""
    return " " + local.capitalize()
