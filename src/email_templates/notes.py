"""
The day-2 note, as the exact bytes that were designed and reviewed.

PROVENANCE. Generated from the approved renders in the api-growth repo --
`state/gtm/email/templates/previews/signin-day2.{html,txt}`, which
`state/gtm/scripts/email-render.mjs` produces from the bodies in
`templates/bodies/`. Those renders were checked at 600 px and 375 px, in light and dark, before this copy was taken. The
only edits made on the way in are the two placeholders below; not one word,
tag or style was rewritten. If the copy changes, it changes THERE and is
re-generated here, never patched in place.

Why the markup is a Python string and not a file next to this module: this
package is deployed as a Vercel Python function, nothing in `src/` has ever
been read off disk at runtime, and a template that is missing from a bundle
fails at send time -- in a lane whose whole job is to send one email -- while
a string constant fails at import, which the deploy check already catches.

The placeholders, all of them, and why substitution is `str.replace` and
never `str.format`: the HTML carries CSS, and CSS is full of braces.

    {{unsubscribe_url}}  every note. Our own one-click route on the free
                         server's host, with the per-user token. Never
                         `{{{RESEND_UNSUBSCRIBE_URL}}}` -- that is a
                         BROADCAST variable and comes out literally in a
                         transactional send.
    {{first_name}}       every note. Renders as " Alex" or as nothing, so
                         the greeting is "Hi Alex," or "Hi,". We store no
                         name, so today it is nearly always empty; see
                         `first_name_from_email`.

The cap note used to live here too, as a second pre-rendered pair. It was
replaced on 2026-09-17: its fixed text opened "you hit the free daily cap
twice this week" while the job that sends it selects anybody refused ONCE
today, so a first-time capper would have been told something untrue
(CLAUDE.md rule 1). The cap note, the day-7 note and the dormant note are
now composed in `ladder.py` from one shared body, because two of their
numbers are per-account and one of them -- a live fare -- is re-priced at
send time.
"""

from __future__ import annotations

DAY2_SUBJECT = "signed in, no searches yet"

DAY2_HTML = """\
<!DOCTYPE html>
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
<title>signed in, no searches yet</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<style>*{font-family:Arial,Helvetica,sans-serif !important;}table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}</style>
<![endif]-->
<style>
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
@media (prefers-color-scheme: dark){
.fp-ground:not([class^="x_"]){background-color:#07080a !important;}
.fp-card:not([class^="x_"]){background-color:#101318 !important;}
.fp-text:not([class^="x_"]){color:#e8edf2 !important;}
.fp-soft:not([class^="x_"]){color:#a3adba !important;}
.fp-faint:not([class^="x_"]){color:#7d8794 !important;}
.fp-panel:not([class^="x_"]){background-color:#171b21 !important;border-color:#333c48 !important;}
.fp-rule:not([class^="x_"]){border-color:#333c48 !important;}
.fp-btn a:not([class^="x_"]){background-color:#e8edf2 !important;color:#0c0e11 !important;}
.fp-text a, .fp-soft a, .fp-faint a:not([class^="x_"]){color:inherit !important;}
}
[data-ogsc] .fp-ground{background-color:#07080a !important;}
[data-ogsc] .fp-card{background-color:#101318 !important;}
[data-ogsc] .fp-text{color:#e8edf2 !important;}
[data-ogsc] .fp-soft{color:#a3adba !important;}
[data-ogsc] .fp-faint{color:#7d8794 !important;}
[data-ogsc] .fp-panel{background-color:#171b21 !important;border-color:#333c48 !important;}
[data-ogsc] .fp-rule{border-color:#333c48 !important;}
[data-ogsc] .fp-btn a{background-color:#e8edf2 !important;color:#0c0e11 !important;}
[data-ogsc] .fp-text a,[data-ogsc] .fp-soft a,[data-ogsc] .fp-faint a{color:inherit !important;}
</style>
</head>
<body class="fp-ground" width="100%" style="margin:0;padding:0 !important;mso-line-height-rule:exactly;background-color:#f1f2f4;" bgcolor="#f1f2f4">
<div style="display:none;overflow:hidden;line-height:1px;opacity:0;max-height:0;max-width:0;mso-hide:all;">The shortest way to get something out of it is to ask for a range, not a date.</div><div style="display:none;overflow:hidden;line-height:1px;opacity:0;max-height:0;max-width:0;mso-hide:all;">&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;&#847;&zwnj;&nbsp;&#8203;&#8204;&#8205;&#8206;&#8207;&#65279;</div>
<center role="article" aria-roledescription="email" lang="en" class="fp-ground" style="width:100%;background-color:#f1f2f4;">
<!--[if mso | IE]><table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color:#f1f2f4;"><tr><td align="center"><table role="presentation" border="0" cellpadding="0" cellspacing="0" width="600"><tr><td><![endif]-->
<table role="presentation" class="fp-ground" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color:#f1f2f4;" bgcolor="#f1f2f4">
<tr><td align="center" style="padding:28px 12px 0 12px;">
<table role="presentation" class="fp-card" border="0" cellpadding="0" cellspacing="0" width="100%" align="center" bgcolor="#ffffff" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:12px;">

<tr>
<td class="fp-pad" style="padding:32px 32px 0 32px;">
<table role="presentation" border="0" cellpadding="0" cellspacing="0"><tr>
<td style="padding:0 9px 0 0;vertical-align:middle;"><img src="https://flightpowers.com/brand/robot-mark-56.png" width="28" height="33" alt="FlightPowers" style="display:block;width:28px;height:33px;border:0;outline:none;text-decoration:none;-ms-interpolation-mode:bicubic;"></td>
<td class="fp-text" style="vertical-align:middle;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;font-weight:600;letter-spacing:-0.01em;color:#1a1d23;">FlightPowers</td>
</tr></table>
</td>
</tr>
<tr><td class="fp-pad" style="padding:6px 32px 0 32px;"><p class="fp-p fp-text" style="margin:0;padding-top:18px;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;line-height:26px;color:#1a1d23;">Hi{{first_name}},</p>
<p class="fp-p fp-text" style="margin:0;padding-top:18px;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;line-height:26px;color:#1a1d23;">You signed in a couple of days ago and haven't run a search yet, so here's the one thing that makes it worth the setup.</p>
<p class="fp-p fp-text" style="margin:0;padding-top:18px;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;line-height:26px;color:#1a1d23;">Don't ask it for a date. Ask it for a range and let it price the whole grid in one go.</p></td></tr>
<tr><td class="fp-pad" style="padding:24px 32px 0 32px;">
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#0c0e11" style="width:100%;background-color:#0c0e11;border-radius:10px;">
<tr><td style="padding:22px 24px;">
<div style="font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;font-size:11px;line-height:16px;letter-spacing:0.18em;text-transform:uppercase;color:#ffb020;">what one wide question costs</div>
<div class="fp-num" style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:46px;line-height:52px;font-weight:600;letter-spacing:-0.03em;color:#e8edf2;font-variant-numeric:tabular-nums;padding-top:10px;">42</div>
<div style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:15px;line-height:22px;color:#a3adba;padding-top:8px;">14 departure dates across 3 airports, asked in one go</div><div style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:13px;line-height:20px;color:#7d8794;padding-top:12px;">One search is one date and one airport, so a range is where the free allowance actually pays off. That grid is 42 of your 50 for the day.</div>
</td></tr></table>
</td></tr>
<tr><td class="fp-pad" style="padding:26px 32px 0 32px;">
<table role="presentation" class="fp-panel" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#f5f6f8" style="width:100%;background-color:#f5f6f8;border:1px solid #e3e6ea;border-radius:10px;">
<tr><td style="padding:20px 22px;"><div class="fp-text" style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:13px;line-height:18px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:#1a1d23;padding-bottom:12px;">Ask it like this</div>
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%"><tr><td style="padding:0 10px 0 0;vertical-align:top;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;font-size:13px;line-height:24px;color:#ffb020;">1</td><td class="fp-text" style="padding:0 0 10px 0;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:15px;line-height:24px;color:#1a1d23;">cheapest week to fly TLV to Rome in October, 4 or 7 nights</td></tr><tr><td style="padding:0 10px 0 0;vertical-align:top;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;font-size:13px;line-height:24px;color:#ffb020;">2</td><td class="fp-text" style="padding:0 0 10px 0;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:15px;line-height:24px;color:#1a1d23;">one way from Berlin to Athens, Split or Palermo, first two weeks of November</td></tr><tr><td style="padding:0 10px 0 0;vertical-align:top;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;font-size:13px;line-height:24px;color:#ffb020;">3</td><td class="fp-text" style="padding:0 0 0px 0;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:15px;line-height:24px;color:#1a1d23;">what does a 3 night stay in Lisbon cost the week of 12 October</td></tr></table>
</td></tr></table>
</td></tr>
<tr><td class="fp-pad" style="padding:6px 32px 0 32px;"><p class="fp-p fp-text" style="margin:0;padding-top:18px;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;line-height:26px;color:#1a1d23;">If it comes back with nothing useful, reply with the route and I'll tell you what went wrong.</p></td></tr>
<tr><td class="fp-pad" style="padding:26px 32px 34px 32px;">
<div class="fp-text" style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:16px;line-height:26px;color:#1a1d23;">Matan</div>

</td></tr>
</table>

<table role="presentation" class="fp-ground" border="0" cellpadding="0" cellspacing="0" width="100%" align="center" style="max-width:600px;width:100%;">
<tr><td class="fp-pad fp-faint" style="padding:20px 32px 40px 32px;font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;font-size:13px;line-height:20px;color:#6b7280;">
<div style="padding-bottom:4px;">You're getting this because you signed in to the free FlightPowers MCP server.</div><div style="padding-bottom:4px;">A welcome note and about one email a month, nothing else.</div>
<div><a href="{{unsubscribe_url}}" style="color:#6b7280;text-decoration:underline;">Unsubscribe</a></div>
</td></tr></table>
</td></tr></table>
<!--[if mso | IE]></td></tr></table></td></tr></table><![endif]-->
</center>
</body>
</html>"""

DAY2_TEXT = """\
Hi{{first_name}},

You signed in a couple of days ago and haven't run a search yet, so here's the one thing that makes it worth the setup.

Don't ask it for a date. Ask it for a range and let it price the whole grid in one go.

42 - 14 departure dates across 3 airports, asked in one go
One search is one date and one airport, so a range is where the free allowance actually pays off. That grid is 42 of your 50 for the day.

Ask it like this
1. cheapest week to fly TLV to Rome in October, 4 or 7 nights
2. one way from Berlin to Athens, Split or Palermo, first two weeks of November
3. what does a 3 night stay in Lisbon cost the week of 12 October

If it comes back with nothing useful, reply with the route and I'll tell you what went wrong.

Matan

You're getting this because you signed in to the free FlightPowers MCP server.
A welcome note and about one email a month, nothing else.
Unsubscribe: {{unsubscribe_url}}
"""

