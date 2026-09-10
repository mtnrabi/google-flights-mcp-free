"""The branded HTML twin: what it must contain, and what it must never.

The two transactional notes (welcome, cap note) render through
`src/email_templates`, a port of the api-growth repo's
`state/gtm/email/templates/flightpowers-broadcast.html`. The rules and the
evidence behind them are `state/gtm/email/DESIGN-2026-09-10.md`.

Two kinds of test here, and they fail for different reasons on purpose:

1. **Snapshots.** `tests/snapshots/*.html` are the exact bytes each note
   renders to. A diff is not a failure by itself, it is a diff: read it, and
   if the change is wanted, re-record with

       UPDATE_EMAIL_SNAPSHOTS=1 python -m pytest tests/test_email_template.py

   The point is that nobody edits an email people receive without seeing
   every byte that moved. Open the snapshot in a browser to look at it.
2. **Guards.** No `mailto:`, no em-dash, exactly one unsubscribe link, under
   40 KB, no external stylesheet, a dark-mode block present. Each one is
   here because a client, a spam filter or Matan's own rules would punish
   the opposite:
   - a `mailto:` in a designed email is the wave-3 ban, and it is dead
     weight in a client that cannot open one;
   - an em-dash is the AI tell that gets our copy spotted;
   - two unsubscribe links means one of them is wrong, and the broadcast
     variable `{{{RESEND_UNSUBSCRIBE_URL}}}` renders literally in a
     transactional send;
   - Gmail clips a message at 102 KB, and Vercel's whole marketing email is
     34 KB, so 40 KB is slack we have no reason to need;
   - an external stylesheet is dropped by most clients and desktop Outlook
     chokes on a webfont reference, so every visual style is inline;
   - the dark-mode block is the one thing that cannot be verified by reading
     a screenshot taken in light mode.

    python -m pytest mcp_server/tests/test_email_template.py -q
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from src import email_templates as tpl
from src.freeusers import CapSignals
from src.maillist import (
    BRANCH_DAILY,
    BRANCH_MONTHLY,
    _cap_parts,
    _welcome_parts,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"

#: A stable stand-in for the per-user token, so a snapshot diff is a real
#: change and never a fresh random string.
LINK = "https://flightpowers.com/email/unsubscribe?t=TOKEN"

DAY_CAP = 150
MONTH_CAP = 2000

#: One account, month-to-date, with both branches' inputs present so the
#: same numbers can be read in both snapshots.
SIGNALS = CapSignals(
    cap_days_7=2,
    active_days_ever=4,
    month_searches=1400,
    month_tool_searches=1400,
    month_hotel_searches=0,
    days_left_in_month=9,
)


def _snapshot(name: str, html: str) -> None:
    path = SNAPSHOTS / f"{name}.html"
    if os.environ.get("UPDATE_EMAIL_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return
    assert path.exists(), (
        f"{path} is missing. Record it with "
        f"UPDATE_EMAIL_SNAPSHOTS=1 python -m pytest {__file__}"
    )
    recorded = path.read_text(encoding="utf-8")
    if recorded != html:
        pytest.fail(
            f"{path.name} changed. Read the diff, and if the change is "
            f"wanted re-record with UPDATE_EMAIL_SNAPSHOTS=1.\n"
            f"recorded {len(recorded)} bytes, rendered {len(html)} bytes"
        )


def welcome_html() -> str:
    return _welcome_parts(DAY_CAP, MONTH_CAP, LINK)[1]


def cap_html(branch: str) -> str:
    return _cap_parts(branch, SIGNALS, LINK, MONTH_CAP)[2]


RENDERED = {
    "welcome": welcome_html,
    "capnote-daily": lambda: cap_html(BRANCH_DAILY),
    "capnote-monthly": lambda: cap_html(BRANCH_MONTHLY),
}


class TestTheSnapshots:
    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_the_bytes_are_the_ones_that_were_reviewed(self, name):
        _snapshot(name, RENDERED[name]())


class TestTheGuards:
    """Every one of these applies to every note, so they are parametrised
    over the rendered set rather than written out three times."""

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_the_renderer_own_checks_are_clean(self, name):
        assert tpl.problems(RENDERED[name](), unsubscribe_url=LINK) == []

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_no_mailto(self, name):
        assert "mailto:" not in RENDERED[name]().lower()

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_no_em_dash_and_no_en_dash(self, name):
        html = RENDERED[name]()
        assert "—" not in html
        assert "–" not in html

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_exactly_one_unsubscribe_link(self, name):
        html = RENDERED[name]()
        assert html.count(LINK) == 1
        # And never the broadcast-only variable, which renders literally in
        # a transactional send.
        assert "RESEND_UNSUBSCRIBE_URL" not in html

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_under_forty_kilobytes(self, name):
        size = len(RENDERED[name]().encode("utf-8"))
        assert size <= 40 * 1024, f"{name} is {size / 1024:.1f} KB"

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_no_external_css_and_no_webfont(self, name):
        html = RENDERED[name]()
        assert not re.search(r'<link[^>]+rel="?stylesheet', html, re.I)
        assert "@import" not in html
        assert "fonts.googleapis.com" not in html

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_the_dark_mode_block_is_there(self, name):
        html = RENDERED[name]()
        assert "@media (prefers-color-scheme: dark)" in html
        # Outlook.com rewrites class names with an x_ prefix and exposes
        # [data-ogsc]; the media query alone leaves it in light colours on a
        # dark ground.
        assert "[data-ogsc]" in html
        assert 'content="light dark"' in html

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_one_remote_image_and_it_is_ours(self, name):
        html = RENDERED[name]()
        srcs = re.findall(r'<img[^>]+src="([^"]+)"', html)
        assert srcs == [tpl.LOGO_URL]
        assert "data:image" not in html
        assert "<svg" not in html.lower()

    @pytest.mark.parametrize("name", sorted(RENDERED))
    def test_one_button_at_most(self, name):
        # The button is the only `fp-btn` in the document, and the VML twin
        # travels with it.
        html = RENDERED[name]()
        assert html.count('class="fp-btn"') == 1
        assert html.count("v:roundrect") == 2  # open and close


class TestTheTwoNotesSayTheRightThing:
    def test_the_welcome_hero_is_the_allowance_and_the_button_is_the_guide(self):
        html = welcome_html()
        assert ">150</div>" in html
        assert "searches a day under your name, 2,000 a month" in html
        assert (
            "https://flightpowers.com/guides/five-minute-travel-agent" in html
        )
        # One product ask, and it is not the paid server: the welcome's job
        # is one successful search.
        assert "flights.flightpowers.com" not in html

    def test_the_cap_hero_is_the_users_own_count(self):
        html = cap_html(BRANCH_DAILY)
        assert ">1,400</div>" in html
        assert "of 2,000 free searches this month" in html

    def test_the_cap_button_follows_the_tool_split(self):
        flights = cap_html(BRANCH_DAILY)
        assert "https://flights.flightpowers.com/mcp" in flights
        assert "hotels.flightpowers.com" not in flights

        hotelish = _cap_parts(
            BRANCH_DAILY,
            CapSignals(
                cap_days_7=2,
                active_days_ever=4,
                month_searches=900,
                month_tool_searches=900,
                month_hotel_searches=800,
                days_left_in_month=9,
            ),
            LINK,
            MONTH_CAP,
        )[2]
        assert "https://hotels.flightpowers.com/mcp" in hotelish
        assert "flights.flightpowers.com" not in hotelish

    def test_the_two_branches_differ_only_in_the_opener(self):
        daily = cap_html(BRANCH_DAILY)
        monthly = cap_html(BRANCH_MONTHLY)
        assert "You hit the free daily cap twice this week" in daily
        assert "two thirds through this month's free searches" in monthly
        assert "9 days of the month to go" in monthly

    def test_a_count_we_do_not_have_drops_the_hero_rather_than_printing_zero(self):
        blank = CapSignals(
            cap_days_7=2, active_days_ever=4, days_left_in_month=9
        )
        html = _cap_parts(BRANCH_DAILY, blank, LINK, MONTH_CAP)[2]
        assert ">0</div>" not in html
        assert "free searches this month" not in html
        # And the rest of the note is intact.
        assert "I read every reply" in html
        assert tpl.problems(html, unsubscribe_url=LINK) == []

    def test_the_text_twin_is_the_sequence_files_copy_not_the_html_flow(self):
        """The designed wrapper may never rewrite copy that was signed off."""
        text = _welcome_parts(DAY_CAP, MONTH_CAP, LINK)[0]
        assert "150 a day, 2,000 a month" in text
        assert "https://flightpowers.com/guides/five-minute-travel-agent" in text
        assert "<" not in text
        assert text.rstrip().endswith(LINK)


class TestTheTemplateItself:
    """The blocks, exercised directly, including the one neither note uses."""

    def _email(self, **kwargs) -> tpl.Email:
        base = dict(
            subject="a subject",
            paragraphs=["Hi,", "One line."],
            unsubscribe_url=LINK,
        )
        base.update(kwargs)
        return tpl.Email(**base)

    def test_the_smallest_possible_email_still_passes_the_checks(self):
        html = tpl.render(self._email())
        assert tpl.problems(html, unsubscribe_url=LINK) == []
        # No hero, no steps, no button: three blocks that are absent, not
        # empty containers. The class names still appear once each in the
        # <style> block, which is why these look for the attribute.
        assert 'class="fp-btn"' not in html
        assert 'class="fp-panel"' not in html
        assert 'class="fp-num"' not in html

    def test_the_steps_card_renders_numbered_and_in_order(self):
        html = tpl.render(
            self._email(
                paragraphs=["Hi,", ":::steps", "After."],
                steps=tpl.Steps(
                    title="Set it up, no code",
                    steps=["First thing.", "Second thing.", "Third thing."],
                ),
            )
        )
        assert "Set it up, no code" in html
        assert html.index("First thing.") < html.index("Second thing.")
        assert html.index("Second thing.") < html.index("Third thing.")
        assert html.index("Third thing.") < html.index("After.")
        assert ">1</td>" in html and ">3</td>" in html
        assert tpl.problems(html, unsubscribe_url=LINK) == []

    def test_a_block_not_placed_by_hand_is_appended_in_order(self):
        html = tpl.render(
            self._email(
                hero=tpl.Hero(number="42", label="things"),
                cta=tpl.Cta(label="Go", url="https://flightpowers.com/go"),
            )
        )
        assert html.index(">42</div>") < html.index("https://flightpowers.com/go")
        assert html.index("One line.") < html.index(">42</div>")

    def test_markup_in_copy_is_escaped_and_links_are_inlined(self):
        html = tpl.render(
            self._email(
                paragraphs=[
                    "A <script>alert(1)</script> and a "
                    "[labelled link](https://flightpowers.com/x)."
                ]
            )
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert '<a href="https://flightpowers.com/x"' in html
        assert ">labelled link</a>" in html

    def test_the_render_is_pure(self):
        assert tpl.render(self._email()) == tpl.render(self._email())

    def test_the_checks_catch_what_they_are_for(self):
        bad = tpl.render(
            self._email(
                paragraphs=["Write to [me](mailto:matan@flightpowers.com) — now."],
                unsubscribe_url="{{{RESEND_UNSUBSCRIBE_URL}}}",
            )
        )
        found = " ".join(tpl.problems(bad, unsubscribe_url=LINK))
        assert "mailto:" in found
        assert "em-dash" in found
        assert "RESEND_UNSUBSCRIBE_URL" in found
        assert "appears 0 times" in found
