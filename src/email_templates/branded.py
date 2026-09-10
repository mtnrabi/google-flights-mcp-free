"""The FlightPowers branded email, as a Python string template.

This is a port of `state/gtm/email/templates/flightpowers-broadcast.html` and
the renderer that builds it (`state/gtm/scripts/email-render.mjs`) in the
api-growth repo. The rules it implements, and the evidence behind each one,
are `state/gtm/email/DESIGN-2026-09-10.md`, which was written off two real
marketing emails read raw out of the inbox (Vercel, Railway), Resend's own
`react-email` templates, Cerberus, and the caniemail support table. The short
version, because a future edit will be tempted by every one of them:

* **600 px, one column, tables only.** No flex, no grid, no CSS variables,
  and `margin` never doing real work: all three are partial or unsupported
  across the clients caniemail tests.
* **Every visual style is inline.** The one `<style>` block carries dark
  mode, the mobile breakpoint and client hacks and nothing else, because
  plenty of clients drop it. There is no external stylesheet and no webfont
  link: desktop Outlook chokes on a webfont reference and falls back to
  Times New Roman.
* **Light ground, exactly one deliberately dark element.** Gmail is `n` on
  `prefers-color-scheme` on all four of its surfaces and runs a partial
  inversion instead: it darkens light grounds and leaves dark ones alone. So
  the hero card is the departure-board dark in BOTH modes, and it is the one
  block that cannot break under an inversion we do not control.
* **The hero is a number, and it is live text.** Never a picture of a
  number, and never a number we cannot trace to a call we made.
* **One CTA button, an `<a>` with an inline background, plus the VML
  roundrect for Outlook.** When the only ask is a reply there is no button:
  pass no CTA and the block disappears rather than inventing a second ask.
* **PNG logo off flightpowers.com.** Inline `<svg>` is `n` in Gmail and
  Outlook, and a data URI is worse.
* **One unsubscribe link.** On a transactional send it is OUR
  `/email/unsubscribe?t=<token>` route: `{{{RESEND_UNSUBSCRIBE_URL}}}` only
  renders inside a Resend broadcast and comes out literally anywhere else.

The public entry point is `render`. Everything else is a block, and every
block is optional except the logo, the body and the footer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from string import Template

# ── palette ──────────────────────────────────────────────────────────────
# Straight off the site's `globals.css`, so an email and a landing page are
# recognisably the same product.

GROUND = "#f1f2f4"  # the page behind the card
CARD = "#ffffff"
INK = "#1a1d23"  # body text, never pure black: the references all agree
INK_SOFT = "#5b6472"
INK_FAINT = "#6b7280"  # footer
RULE = "#e3e6ea"
PANEL = "#f5f6f8"  # the steps card
BOARD = "#0c0e11"  # --color-ink-900, the departure board
BOARD_TEXT = "#e8edf2"  # --color-ink-100
BOARD_SOFT = "#a3adba"  # --color-ink-300
BOARD_FAINT = "#7d8794"  # --color-ink-400
SIGNAL = "#ffb020"  # --color-signal-500
BTN_BG = "#0c0e11"
BTN_TEXT = "#ffffff"

SANS = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
    "Arial, sans-serif"
)
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

#: 47x56, 2.7 KB, drawn at 28x33. The only remote image in any of our mail.
LOGO_URL = "https://flightpowers.com/brand/robot-mark-56.png"

#: The broadcast-only variable, named here so the checks can look for it.
RESEND_UNSUBSCRIBE_VARIABLE = "{{{RESEND_UNSUBSCRIBE_URL}}}"


# ── the pieces a caller fills in ─────────────────────────────────────────


@dataclass(frozen=True)
class Hero:
    """The one dark block: an eyebrow, a number, a label and a note.

    `number` is the whole point of the block and is required. Rule 1 of
    CLAUDE.md binds it: it has to be traceable to a call we made or a cap we
    enforce, never a figure that sounds good.
    """

    number: str
    eyebrow: str = ""
    label: str = ""
    note: str = ""


@dataclass(frozen=True)
class Steps:
    """A numbered card. Its steps are instructions, not prose bullets."""

    steps: list[str]
    title: str = ""


@dataclass(frozen=True)
class Cta:
    """The single button. Both fields or no button at all."""

    label: str
    url: str


@dataclass(frozen=True)
class Email:
    """Everything one rendered message is made of.

    `paragraphs` is the body in order. A paragraph that is exactly
    `":::hero"`, `":::steps"` or `":::cta"` drops that block in at that
    point; any block not placed by hand is appended in the order hero,
    steps, cta. That is the same placement rule the api-growth renderer
    uses, so a body can move between the two without changing shape.
    """

    subject: str
    paragraphs: list[str]
    unsubscribe_url: str
    preheader: str = ""
    hero: Hero | None = None
    steps: Steps | None = None
    cta: Cta | None = None
    signoff: list[str] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)
    unsubscribe_label: str = "Unsubscribe"


# ── inline markup ────────────────────────────────────────────────────────

_LINK_MD = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"(^|[\s(])(https?://[^\s<>()]+)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_SENT0, _SENT1 = "\x00", "\x01"


def escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def inline(text: str, link_color: str = "inherit") -> str:
    """`**bold**`, `[label](url)`, bare URLs and `code`, escaped first.

    Links inherit the surrounding text colour and are underlined, which is
    what the site does and what dodges the real problem: the brand amber is
    unreadable as a link colour on white.
    """
    links: list[str] = []

    def _stash(html: str) -> str:
        links.append(html)
        return f"{_SENT0}{len(links) - 1}{_SENT1}"

    out = escape(text)
    out = _LINK_MD.sub(
        lambda m: _stash(
            f'<a href="{m.group(2)}" style="color:{link_color};'
            f'text-decoration:underline;">{m.group(1)}</a>'
        ),
        out,
    )
    out = _BARE_URL.sub(
        lambda m: m.group(1)
        + _stash(
            f'<a href="{m.group(2)}" style="color:{link_color};'
            f'text-decoration:underline;word-break:break-all;">{m.group(2)}</a>'
        ),
        out,
    )
    out = _BOLD.sub(r'<strong style="font-weight:600;">\1</strong>', out)
    out = _CODE.sub(
        lambda m: f'<span style="font-family:{MONO};font-size:14px;">'
        f"{m.group(1)}</span>",
        out,
    )
    return re.sub(
        f"{_SENT0}(\\d+){_SENT1}", lambda m: links[int(m.group(1))], out
    )


# ── the style block ──────────────────────────────────────────────────────

#: Every dark rule is repeated under `[data-ogsc]` (Outlook.com) and guarded
#: with `:not([class^="x_"])`, because Outlook.com rewrites class names with
#: an `x_` prefix and would otherwise apply the rule twice. Vercel's own
#: email does exactly this. Colour only: a dark rule may never move a box.
_DARK_RULES: list[tuple[str, str]] = [
    (".fp-ground", "background-color:#07080a !important;"),
    (".fp-card", "background-color:#101318 !important;"),
    (".fp-text", "color:#e8edf2 !important;"),
    (".fp-soft", "color:#a3adba !important;"),
    (".fp-faint", "color:#7d8794 !important;"),
    (".fp-panel", "background-color:#171b21 !important;border-color:#333c48 !important;"),
    (".fp-rule", "border-color:#333c48 !important;"),
    (".fp-btn a", "background-color:#e8edf2 !important;color:#0c0e11 !important;"),
    (".fp-text a, .fp-soft a, .fp-faint a", "color:inherit !important;"),
]


def _dark_css() -> str:
    body = "\n".join(
        f'{sel}:not([class^="x_"]){{{decl}}}' for sel, decl in _DARK_RULES
    )
    ogsc = "\n".join(
        ",".join(f"[data-ogsc] {part.strip()}" for part in sel.split(","))
        + f"{{{decl}}}"
        for sel, decl in _DARK_RULES
    )
    return f"@media (prefers-color-scheme: dark){{\n{body}\n}}\n{ogsc}"


HEAD_CSS = (
    """
:root{color-scheme:light dark;supported-color-schemes:light dark;}
html,body{margin:0 auto !important;padding:0 !important;width:100% !important;}
*{-ms-text-size-adjust:100%;-webkit-text-size-adjust:100%;}
div[style*="margin: 16px 0"]{margin:0 !important;}
#MessageViewBody,#MessageWebViewDiv{width:100% !important;}
table,td{mso-table-lspace:0pt !important;mso-table-rspace:0pt !important;}
table{border-spacing:0 !important;border-collapse:collapse !important;}
img{-ms-interpolation-mode:bicubic;}
a[x-apple-data-detectors],.aBn{border-bottom:0 !important;cursor:default !important;color:inherit !important;text-decoration:none !important;font-size:inherit !important;font-family:inherit !important;font-weight:inherit !important;line-height:inherit !important;}
.a6S{display:none !important;opacity:.01 !important;}
.im{color:inherit !important;}
@media screen and (max-width:599px){
  .fp-pad{padding-left:20px !important;padding-right:20px !important;}
  .fp-h1{font-size:24px !important;line-height:31px !important;}
  .fp-num{font-size:40px !important;line-height:44px !important;}
  .fp-p{font-size:17px !important;line-height:27px !important;}
  .fp-btn a{display:block !important;text-align:center !important;}
}
""".strip()
    + "\n"
    + _dark_css()
)


# ── blocks ───────────────────────────────────────────────────────────────


def _preheader(text: str) -> str:
    """The inbox preview sentence, chosen rather than scraped off the body.

    Hidden the react-email way and padded with zero-width characters so the
    client cannot pull the first body line in after it.
    """
    pad = "&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;" * 20
    hide = (
        "display:none;overflow:hidden;line-height:1px;opacity:0;max-height:0;"
        "max-width:0;mso-hide:all;"
    )
    return (
        f'<div style="{hide}">{escape(text)}</div>'
        f'<div style="{hide}">{pad}</div>'
    )


def _logo_row() -> str:
    return f"""
<tr>
<td class="fp-pad" style="padding:32px 32px 0 32px;">
<table role="presentation" border="0" cellpadding="0" cellspacing="0"><tr>
<td style="padding:0 9px 0 0;vertical-align:middle;"><img src="{LOGO_URL}" width="28" height="33" alt="FlightPowers" style="display:block;width:28px;height:33px;border:0;outline:none;text-decoration:none;-ms-interpolation-mode:bicubic;"></td>
<td class="fp-text" style="vertical-align:middle;font-family:{SANS};font-size:16px;font-weight:600;letter-spacing:-0.01em;color:{INK};">FlightPowers</td>
</tr></table>
</td>
</tr>"""


def _hero_row(hero: Hero | None) -> str:
    """The dark card. No dark-mode override on purpose: dark in both modes."""
    if hero is None or not hero.number:
        return ""
    eyebrow = (
        f'<div style="font-family:{MONO};font-size:11px;line-height:16px;'
        f"letter-spacing:0.18em;text-transform:uppercase;color:{SIGNAL};\">"
        f"{escape(hero.eyebrow)}</div>"
        if hero.eyebrow
        else ""
    )
    label = (
        f'<div style="font-family:{SANS};font-size:15px;line-height:22px;'
        f'color:{BOARD_SOFT};padding-top:8px;">'
        f"{inline(hero.label, BOARD_SOFT)}</div>"
        if hero.label
        else ""
    )
    note = (
        f'<div style="font-family:{SANS};font-size:13px;line-height:20px;'
        f'color:{BOARD_FAINT};padding-top:12px;">'
        f"{inline(hero.note, BOARD_FAINT)}</div>"
        if hero.note
        else ""
    )
    return f"""
<tr><td class="fp-pad" style="padding:24px 32px 0 32px;">
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="{BOARD}" style="width:100%;background-color:{BOARD};border-radius:10px;">
<tr><td style="padding:22px 24px;">
{eyebrow}
<div class="fp-num" style="font-family:{SANS};font-size:46px;line-height:52px;font-weight:600;letter-spacing:-0.03em;color:{BOARD_TEXT};font-variant-numeric:tabular-nums;padding-top:10px;">{escape(hero.number)}</div>
{label}{note}
</td></tr></table>
</td></tr>"""


def _steps_row(steps: Steps | None) -> str:
    if steps is None or not steps.steps:
        return ""
    head = (
        f'<div class="fp-text" style="font-family:{SANS};font-size:13px;'
        f"line-height:18px;font-weight:600;letter-spacing:0.04em;"
        f'text-transform:uppercase;color:{INK};padding-bottom:12px;">'
        f"{escape(steps.title)}</div>"
        if steps.title
        else ""
    )
    rows = "".join(
        "<tr>"
        f'<td style="padding:0 10px 0 0;vertical-align:top;font-family:{MONO};'
        f'font-size:13px;line-height:24px;color:{SIGNAL};">{i + 1}</td>'
        f'<td class="fp-text" style="padding:0 0 '
        f"{0 if i == len(steps.steps) - 1 else 10}px 0;font-family:{SANS};"
        f'font-size:15px;line-height:24px;color:{INK};">{inline(step)}</td>'
        "</tr>"
        for i, step in enumerate(steps.steps)
    )
    return f"""
<tr><td class="fp-pad" style="padding:26px 32px 0 32px;">
<table role="presentation" class="fp-panel" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="{PANEL}" style="width:100%;background-color:{PANEL};border:1px solid {RULE};border-radius:10px;">
<tr><td style="padding:20px 22px;">{head}
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%">{rows}</table>
</td></tr></table>
</td></tr>"""


def _cta_row(cta: Cta | None) -> str:
    """One button, or none. The VML twin is what makes Outlook honour the
    padding; `mso-padding-alt:0px` on the `<a>` is the other half of it."""
    if cta is None or not cta.label or not cta.url:
        return ""
    return f"""
<tr><td class="fp-pad" style="padding:26px 32px 0 32px;">
<div class="fp-btn">
<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{cta.url}" style="height:48px;v-text-anchor:middle;width:260px;" arcsize="18%" stroke="f" fillcolor="{BTN_BG}"><w:anchorlock/><center style="color:{BTN_TEXT};font-family:Arial,sans-serif;font-size:16px;font-weight:bold;"><![endif]-->
<a href="{cta.url}" target="_blank" style="display:inline-block;background-color:{BTN_BG};color:{BTN_TEXT};font-family:{SANS};font-size:16px;font-weight:600;line-height:20px;text-align:center;text-decoration:none;padding:14px 26px;border-radius:8px;mso-padding-alt:0px;mso-hide:all;">{escape(cta.label)}</a>
<!--[if mso]></center></v:roundrect><![endif]-->
</div>
</td></tr>"""


def _paragraph(text: str) -> str:
    return (
        f'<p class="fp-p fp-text" style="margin:0;padding-top:18px;'
        f"font-family:{SANS};font-size:16px;line-height:26px;color:{INK};\">"
        f"{inline(text)}</p>"
    )


def _prose_row(group: list[str]) -> str:
    if not group:
        return ""
    body = "\n".join(_paragraph(p) for p in group)
    return (
        '\n<tr><td class="fp-pad" style="padding:6px 32px 0 32px;">'
        f"{body}</td></tr>"
    )


def _signoff_row(lines: list[str]) -> str:
    if not lines:
        return ""
    rest = "".join(
        f'<div class="fp-soft" style="font-family:{SANS};font-size:14px;'
        f'line-height:22px;color:{INK_SOFT};">{escape(line)}</div>'
        for line in lines[1:]
    )
    return f"""
<tr><td class="fp-pad" style="padding:26px 32px 34px 32px;">
<div class="fp-text" style="font-family:{SANS};font-size:16px;line-height:26px;color:{INK};">{escape(lines[0])}</div>
{rest}
</td></tr>"""


def _footer_block(lines: list[str], unsub_url: str, unsub_label: str) -> str:
    """Plain type, the permission line, one unsubscribe. No button, no icons."""
    printed = "".join(
        f'<div style="padding-bottom:4px;">{inline(line, INK_FAINT)}</div>'
        for line in lines
        if line
    )
    return f"""
<table role="presentation" class="fp-ground" border="0" cellpadding="0" cellspacing="0" width="100%" align="center" style="max-width:600px;width:100%;">
<tr><td class="fp-pad fp-faint" style="padding:20px 32px 40px 32px;font-family:{SANS};font-size:13px;line-height:20px;color:{INK_FAINT};">
{printed}
<div><a href="{unsub_url}" style="color:{INK_FAINT};text-decoration:underline;">{escape(unsub_label)}</a></div>
</td></tr></table>"""


_SLOTS = ("hero", "steps", "cta")


def _compose_rows(paragraphs: list[str], slots: dict[str, str]) -> str:
    """Walk the body, dropping placed blocks in and appending the rest."""
    used: set[str] = set()
    out = ""
    group: list[str] = []
    for para in paragraphs:
        name = para.strip()[3:] if para.strip().startswith(":::") else ""
        if name in _SLOTS:
            out += _prose_row(group)
            group = []
            if name not in used:
                out += slots[name]
                used.add(name)
            continue
        if para.strip():
            group.append(para)
    out += _prose_row(group)
    for name in _SLOTS:
        if name not in used:
            out += slots[name]
    return out


# ── the document ─────────────────────────────────────────────────────────

#: `string.Template`, not `str.format`, because the CSS is full of braces.
SHELL = Template(
    """<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" style="color-scheme:light dark;supported-color-schemes:light dark;">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,address=no,email=no,date=no,url=no">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>$title</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<style>*{font-family:Arial,Helvetica,sans-serif !important;}table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}</style>
<![endif]-->
<style>
$css
</style>
</head>
<body class="fp-ground" width="100%" style="margin:0;padding:0 !important;mso-line-height-rule:exactly;background-color:$ground;" bgcolor="$ground">
$preheader
<center role="article" aria-roledescription="email" lang="en" class="fp-ground" style="width:100%;background-color:$ground;">
<!--[if mso | IE]><table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color:$ground;"><tr><td align="center"><table role="presentation" border="0" cellpadding="0" cellspacing="0" width="600"><tr><td><![endif]-->
<table role="presentation" class="fp-ground" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color:$ground;" bgcolor="$ground">
<tr><td align="center" style="padding:28px 12px 0 12px;">
<table role="presentation" class="fp-card" border="0" cellpadding="0" cellspacing="0" width="100%" align="center" bgcolor="$card" style="max-width:600px;width:100%;background-color:$card;border-radius:12px;">
$rows
</table>
$footer
</td></tr></table>
<!--[if mso | IE]></td></tr></table></td></tr></table><![endif]-->
</center>
</body>
</html>"""
)


def render(email: Email) -> str:
    """One message, as an HTML string. Pure: same input, same bytes."""
    slots = {
        "hero": _hero_row(email.hero),
        "steps": _steps_row(email.steps),
        "cta": _cta_row(email.cta),
    }
    rows = (
        _logo_row()
        + _compose_rows(email.paragraphs, slots)
        + _signoff_row(email.signoff)
    )
    return SHELL.substitute(
        title=escape(email.subject),
        css=HEAD_CSS,
        ground=GROUND,
        card=CARD,
        preheader=_preheader(email.preheader or email.subject),
        rows=rows,
        footer=_footer_block(
            email.footer_lines, email.unsubscribe_url, email.unsubscribe_label
        ),
    )


# ── checks ───────────────────────────────────────────────────────────────

#: Gmail clips a message at 102 KB. Vercel's whole email is 34 KB, so 40 KB
#: is a cap we have no reason to reach and a tripwire if a future edit
#: pastes something big into a template.
MAX_HTML_BYTES = 40 * 1024


def problems(html: str, *, unsubscribe_url: str = "") -> list[str]:
    """Everything that would make this message wrong to send.

    Returned rather than raised, so a test can print all of them at once and
    a caller can log them. Empty list means clean.
    """
    found: list[str] = []
    if RESEND_UNSUBSCRIBE_VARIABLE in html or "RESEND_UNSUBSCRIBE_URL" in html:
        found.append(
            "carries the broadcast-only {{{RESEND_UNSUBSCRIBE_URL}}} variable, "
            "which renders literally in a transactional send"
        )
    if "mailto:" in html.lower():
        found.append("contains a mailto: link")
    if "—" in html:
        found.append("contains an em-dash")
    if "–" in html:
        found.append("contains an en-dash")
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        found.append(
            f"is {size / 1024:.1f} KB, over the {MAX_HTML_BYTES // 1024} KB cap"
        )
    for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
        if not src.startswith("https://flightpowers.com/"):
            found.append(f"image is not on flightpowers.com: {src}")
    if "data:image" in html:
        found.append("contains a data: URI image")
    if re.search(r"<svg", html, re.I):
        found.append("contains an inline <svg> (Gmail and Outlook do not render it)")
    if re.search(r'<link[^>]+rel="?stylesheet', html, re.I) or "@import" in html:
        found.append("loads an external stylesheet")
    if "@media (prefers-color-scheme: dark)" not in html:
        found.append("has no dark-mode block")
    if unsubscribe_url:
        count = html.count(unsubscribe_url)
        if count != 1:
            found.append(
                f"the unsubscribe link appears {count} times, expected exactly 1"
            )
    return found
