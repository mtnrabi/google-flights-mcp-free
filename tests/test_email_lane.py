"""
The two transactional sends on the free server: the welcome and the cap note.

Copy, triggers and guards are `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md`.
Both sends are OFF by default and each has its own switch
(`FREE_SIGNIN_WELCOME`, `FREE_SIGNIN_CAPNOTE`).

What this file pins, and why each one is here rather than assumed:

1. **Once, ever, per user, whatever races.** The welcome's guard is a row
   claimed with an `UPDATE ... WHERE welcome_sent_at IS NULL` BEFORE the
   Resend call, not "we did not see an error afterwards". A guard that is
   the absence of an error re-sends the note on every redeploy.
2. **A refusal releases the claim, a timeout does not.** Resend answering
   400 means nothing is in flight; a timeout means it may have taken the
   message, and a second copy of an unasked-for email is the failure that
   costs a sending domain.
3. **The gates are real.** Flag off, no request at all -- not a request that
   is discarded.
4. **Opt-out beats everything.** The unsubscribe route takes one click with
   no session (RFC 8058, which the `List-Unsubscribe-Post` header on every
   send promises), and an opted-out row is never mailed again by any lane.
5. **The cap note's triggers.** Two capped days in a week, or 60% of the
   month with five days left, and two active days ever either way. Once per
   30 days after that, whichever branch.
6. **One URL, chosen.** Flights or hotels by what the account actually
   searched, and flights when we cannot tell.

    python -m pytest mcp_server/tests/test_email_lane.py -q
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.freeusers import (
    CAP_NOTE_LANE_DAY_CAP,
    CAP_NOTE_MIN_GAP_DAYS,
    CAP_NOTE_WELCOME_GAP_HOURS,
    CapSignals,
    MemoryFreeUserStore,
    days_left_in_month,
    is_hotel_tool,
)
from src.maillist import (
    BRANCH_DAILY,
    BRANCH_MONTHLY,
    REFUSED,
    SENT,
    UNKNOWN,
    MailConfig,
    cap_note_branch,
    cap_note_product,
    load_mail_config,
    maybe_send_cap_note,
    remember_signin,
    unsubscribe_url,
)

ORIGIN = "https://mcp.test.invalid"
DAY = 86400.0


def _config(**kwargs) -> MailConfig:
    base = {"api_key": "k", "unsubscribe_base": ORIGIN}
    base.update(kwargs)
    return MailConfig(**base)


#: Captured before any test patches `src.maillist.httpx.AsyncClient`. The
#: fakes below build a real client over a MockTransport, and looking the
#: class up through the module under test would make each fake construct
#: the next one, forever.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client(handler):
    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))


class _Recorder:
    """A Resend that says yes and remembers what it was asked to send."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"id": "e_1"}
        self.sends: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sends.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.body)


async def _signed_in(store, sub="sub-1", email="a@example.com"):
    await store.upsert(sub, email)
    return (await store.get(sub)).unsubscribe_token


# ── the welcome ──────────────────────────────────────────────────────────


class TestTheWelcome:
    @pytest.mark.asyncio
    async def test_it_goes_out_once_and_never_again(self, monkeypatch):
        """`remember_signin` only runs on the INSERT, but a redeploy, a
        second client and a retried background task all reach it."""
        store = MemoryFreeUserStore()
        token = await _signed_in(store)
        resend = _Recorder()
        config = _config(welcome=True)

        async def hand_off():
            monkeypatch.setattr(
                "src.maillist.httpx.AsyncClient",
                lambda **kw: _client(resend),
            )
            await remember_signin(
                store, "sub-1", "a@example.com", token, config,
                day_cap=150, month_cap=2000,
            )

        await hand_off()
        await hand_off()
        await hand_off()

        assert len(resend.sends) == 1
        assert (await store.get("sub-1")).welcome_sent_at is not None

    @pytest.mark.asyncio
    async def test_the_claim_comes_before_the_send(self):
        """Two instances racing one first sign-in send exactly one note."""
        store = MemoryFreeUserStore()
        await _signed_in(store)
        assert await store.mark_welcome_sent("sub-1") is True
        assert await store.mark_welcome_sent("sub-1") is False

    @pytest.mark.asyncio
    async def test_an_opted_out_row_is_never_mailed(self, monkeypatch):
        store = MemoryFreeUserStore()
        token = await _signed_in(store)
        await store.opt_out(token)

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("a welcome went to an opted-out address")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token, _config(welcome=True)
        )
        assert (await store.get("sub-1")).welcome_sent_at is None

    @pytest.mark.asyncio
    async def test_the_gate_off_makes_no_request_at_all(self, monkeypatch):
        """Off is off: not a request whose answer is thrown away."""
        store = MemoryFreeUserStore()
        token = await _signed_in(store)

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("a welcome went out with the flag off")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token, _config(welcome=False)
        )
        assert (await store.get("sub-1")).welcome_sent_at is None

    @pytest.mark.asyncio
    async def test_no_api_key_means_no_send_and_no_claim(self, monkeypatch):
        store = MemoryFreeUserStore()
        token = await _signed_in(store)

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("a welcome went out with no API key")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token,
            MailConfig(api_key="", welcome=True, unsubscribe_base=ORIGIN),
        )
        assert (await store.get("sub-1")).welcome_sent_at is None

    @pytest.mark.asyncio
    async def test_the_resend_call_has_the_shape_the_sequence_file_specifies(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        token = await _signed_in(store)
        resend = _Recorder()
        config = _config(welcome=True)
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token, config,
            day_cap=150, month_cap=2000,
        )
        body = resend.sends[0]
        link = unsubscribe_url(config, token)

        assert body["from"] == "Matan Rabi <app@flightpowers.com>"
        assert body["reply_to"] == "Matan Rabi <app@flightpowers.com>"
        assert body["to"] == ["a@example.com"]
        # Subject variant A -- the one that ships, and the only number in it.
        assert body["subject"] == "you're in, 150 searches a day"
        assert body["headers"]["List-Unsubscribe"] == f"<{link}>"
        assert body["headers"]["List-Unsubscribe-Post"] == (
            "List-Unsubscribe=One-Click"
        )
        # The plain-text part is the sequence file's copy, byte for byte.
        assert "150 a day, 2,000 a month" in body["text"]
        # The HTML part is the branded template, so the same two numbers are
        # in the hero card and the guide URL is the one button. Same
        # sentences, different arrangement -- and never the broadcast-only
        # variable in either part.
        assert ">150</div>" in body["html"]
        assert "searches a day under your name, 2,000 a month" in body["html"]
        for part in (body["text"], body["html"]):
            assert "https://flightpowers.com/guides/five-minute-travel-agent" in part
            assert link in part
            assert "RESEND_UNSUBSCRIBE_URL" not in part
        # Exactly one remote image, the hosted logo, and no tracking pixel of
        # our own: open tracking is off on this domain since 2026-09-09 and
        # this lane is judged on replies.
        assert body["html"].count("<img") == 1
        assert "https://flightpowers.com/brand/robot-mark-56.png" in body["html"]

    @pytest.mark.asyncio
    async def test_a_refusal_releases_the_claim_and_a_timeout_does_not(
        self, monkeypatch
    ):
        """Resend saying 400 means nothing is in flight. A timeout means it
        may have taken the message, and a second copy is the worse failure.
        """
        store = MemoryFreeUserStore()
        token = await _signed_in(store)
        config = _config(welcome=True)

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient",
            lambda **kw: _client(lambda request: httpx.Response(422, json={})),
        )
        await remember_signin(store, "sub-1", "a@example.com", token, config)
        assert (await store.get("sub-1")).welcome_sent_at is None

        def timeout(request):
            raise httpx.ReadTimeout("resend took too long")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(timeout)
        )
        await remember_signin(store, "sub-1", "a@example.com", token, config)
        assert (await store.get("sub-1")).welcome_sent_at is not None

    @pytest.mark.asyncio
    async def test_a_two_hundred_with_no_id_is_not_a_send(self, monkeypatch):
        """CLAUDE.md: confirm a JSON id came back. A 200 with no id is not
        a send -- but it is not a refusal either, so the claim stands."""
        store = MemoryFreeUserStore()
        token = await _signed_in(store)
        resend = _Recorder(status=200, body={})
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token, _config(welcome=True)
        )
        assert (await store.get("sub-1")).welcome_sent_at is not None


# ── the cap note: the triggers ───────────────────────────────────────────


class TestTheCapNoteTriggers:
    def test_two_capped_days_in_a_week_is_branch_d(self):
        signals = CapSignals(cap_days_7=2, active_days_ever=4)
        assert cap_note_branch(signals, 2000) == BRANCH_DAILY

    def test_one_capped_day_is_not_enough(self):
        signals = CapSignals(cap_days_7=1, active_days_ever=4)
        assert cap_note_branch(signals, 2000) is None

    def test_sixty_percent_of_the_month_is_branch_m(self):
        signals = CapSignals(
            active_days_ever=3, month_searches=1200, days_left_in_month=9
        )
        assert cap_note_branch(signals, 2000) == BRANCH_MONTHLY

    def test_just_under_sixty_percent_is_not(self):
        signals = CapSignals(
            active_days_ever=3, month_searches=1199, days_left_in_month=9
        )
        assert cap_note_branch(signals, 2000) is None

    def test_the_end_of_the_month_is_not_an_offer(self):
        """Telling somebody on the 30th that they are two thirds through the
        month is a fact they cannot act on."""
        signals = CapSignals(
            active_days_ever=3, month_searches=1600, days_left_in_month=4
        )
        assert cap_note_branch(signals, 2000) is None

    def test_one_afternoon_of_a_script_is_not_a_user(self):
        signals = CapSignals(
            cap_days_7=2, active_days_ever=1, month_searches=1900,
            days_left_in_month=20,
        )
        assert cap_note_branch(signals, 2000) is None

    def test_the_daily_branch_wins_when_both_fire(self):
        """Branch D is what actually happened to them; branch M would open
        on a fact while a scan is being cut off mid-way."""
        signals = CapSignals(
            cap_days_7=3, active_days_ever=5, month_searches=1900,
            days_left_in_month=10,
        )
        assert cap_note_branch(signals, 2000) == BRANCH_DAILY

    def test_no_month_cap_means_no_monthly_branch(self):
        signals = CapSignals(
            active_days_ever=3, month_searches=99999, days_left_in_month=20
        )
        assert cap_note_branch(signals, 0) is None


class TestWhichUrlItPrints:
    def test_a_hotel_majority_gets_the_hotels_server(self):
        signals = CapSignals(month_tool_searches=10, month_hotel_searches=6)
        assert cap_note_product(signals) == "hotels"

    def test_an_even_split_and_no_data_both_get_flights(self):
        assert cap_note_product(CapSignals(
            month_tool_searches=10, month_hotel_searches=5
        )) == "flights"
        assert cap_note_product(CapSignals()) == "flights"

    def test_a_hotel_tool_is_recognised_by_its_name(self):
        assert is_hotel_tool("search_hotels")
        assert is_hotel_tool("find_hotel_by_name")
        assert not is_hotel_tool("search_oneway_flights")
        assert not is_hotel_tool("")


class TestSignalsComeFromTheRows:
    @pytest.mark.asyncio
    async def test_capped_days_are_counted_per_day_inside_a_week(self):
        store = MemoryFreeUserStore()
        await _signed_in(store)
        now = time.time()
        # Twice on one day is ONE day, and a hit nine days ago is outside
        # the window.
        await store.note_cap_hit("sub-1", now)
        await store.note_cap_hit("sub-1", now)
        await store.note_cap_hit("sub-1", now - 9 * DAY)
        assert (await store.cap_signals("sub-1", now)).cap_days_7 == 1
        await store.note_cap_hit("sub-1", now - 3 * DAY)
        assert (await store.cap_signals("sub-1", now)).cap_days_7 == 2

    @pytest.mark.asyncio
    async def test_a_refusal_is_not_an_active_day(self):
        """A capped call spends nothing, so it must not make the account
        look like it searched."""
        store = MemoryFreeUserStore()
        await _signed_in(store)
        now = time.time()
        await store.note_cap_hit("sub-1", now)
        assert (await store.cap_signals("sub-1", now)).active_days_ever == 0
        await store.touch("sub-1", 5, now)
        assert (await store.cap_signals("sub-1", now)).active_days_ever == 1

    @pytest.mark.asyncio
    async def test_the_tool_split_follows_what_was_searched(self):
        store = MemoryFreeUserStore()
        await _signed_in(store)
        now = time.time()
        await store.touch("sub-1", 4, now, tool="search_hotels")
        await store.touch("sub-1", 1, now, tool="search_oneway_flights")
        signals = await store.cap_signals("sub-1", now)
        assert signals.month_tool_searches == 5
        assert signals.month_hotel_searches == 4
        assert signals.hotel_majority is True

    @pytest.mark.asyncio
    async def test_deleting_the_account_takes_every_row_with_it(self):
        store = MemoryFreeUserStore()
        await _signed_in(store)
        await store.touch("sub-1", 2, tool="search_hotels")
        await store.note_cap_hit("sub-1")
        assert await store.delete("sub-1") is True
        assert store.days == {} and store.cap_hits == {} and store.tools == {}

    def test_days_left_never_goes_negative(self):
        # 2026-01-31T12:00:00Z -- the last day of a 31-day month.
        assert days_left_in_month(1769860800.0) == 0


# ── the cap note: the send and its guards ────────────────────────────────


async def _make_capped(store, sub="sub-1", email="a@example.com", now=None):
    """An account that fires branch D: two capped days, two active days."""
    now = now if now is not None else time.time()
    await store.upsert(sub, email, now=now - 8 * DAY)
    await store.touch(sub, 10, now - 2 * DAY, tool="search_oneway_flights")
    await store.touch(sub, 10, now, tool="search_oneway_flights")
    await store.note_cap_hit(sub, now - 2 * DAY)
    await store.note_cap_hit(sub, now)
    return now


class TestTheCapNoteSend:
    @pytest.mark.asyncio
    async def test_it_fires_once_and_then_not_for_thirty_days(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        resend = _Recorder()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        config = _config(cap_note=True)

        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000, now=now
        ) == BRANCH_DAILY
        # Same day, and again a month minus a day later: still one note.
        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000, now=now
        ) is None
        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000,
            now=now + (CAP_NOTE_MIN_GAP_DAYS - 1) * DAY,
        ) is None
        assert len(resend.sends) == 1

        # Past the window, with the triggers still true, it may fire again.
        later = now + (CAP_NOTE_MIN_GAP_DAYS + 1) * DAY
        await store.note_cap_hit("sub-1", later)
        await store.note_cap_hit("sub-1", later - DAY)
        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000, now=later
        ) == BRANCH_DAILY
        assert len(resend.sends) == 2

    @pytest.mark.asyncio
    async def test_the_gate_off_makes_no_request_at_all(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = await _make_capped(store)

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("a cap note went out with the flag off")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=False), month_cap=2000, now=now
        ) is None
        assert (await store.get("sub-1")).cap_note_sent_at is None

    @pytest.mark.asyncio
    async def test_the_welcome_switch_does_not_turn_this_on(self, monkeypatch):
        """CLAUDE.md rule 14: the receipt and the upsell get separate flags."""

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("the welcome flag sent a cap note")

        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        assert await maybe_send_cap_note(
            store, "sub-1", _config(welcome=True), month_cap=2000, now=now
        ) is None

    @pytest.mark.asyncio
    async def test_an_opted_out_row_is_never_mailed(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        await store.opt_out((await store.get("sub-1")).unsubscribe_token)

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("a cap note went to an opted-out address")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=2000, now=now
        ) is None
        assert (await store.get("sub-1")).cap_note_sent_at is None

    @pytest.mark.asyncio
    async def test_not_inside_seventy_two_hours_of_the_welcome(self, monkeypatch):
        """Two notes in three days from a server somebody just signed in to
        reads as a drip, and there is no drip."""
        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        await store.mark_welcome_sent("sub-1")
        resend = _Recorder()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        config = _config(cap_note=True)
        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000, now=now
        ) is None
        assert await maybe_send_cap_note(
            store, "sub-1", config, month_cap=2000,
            now=now + (CAP_NOTE_WELCOME_GAP_HOURS + 1) * 3600,
        ) == BRANCH_DAILY
        assert len(resend.sends) == 1

    @pytest.mark.asyncio
    async def test_the_lane_has_a_ceiling_for_the_day(self, monkeypatch):
        """A spike in sign-ins must not turn into a mail blast."""
        store = MemoryFreeUserStore()
        now = time.time()
        for index in range(CAP_NOTE_LANE_DAY_CAP + 2):
            await _make_capped(store, f"sub-{index}", f"u{index}@example.com", now)
        resend = _Recorder()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        config = _config(cap_note=True)
        sent = [
            await maybe_send_cap_note(
                store, f"sub-{index}", config, month_cap=2000, now=now
            )
            for index in range(CAP_NOTE_LANE_DAY_CAP + 2)
        ]
        assert sent.count(BRANCH_DAILY) == CAP_NOTE_LANE_DAY_CAP
        assert len(resend.sends) == CAP_NOTE_LANE_DAY_CAP

    @pytest.mark.asyncio
    async def test_a_refusal_releases_the_claim(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient",
            lambda **kw: _client(lambda request: httpx.Response(400, json={})),
        )
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=2000, now=now
        ) is None
        assert (await store.get("sub-1")).cap_note_sent_at is None

    @pytest.mark.asyncio
    async def test_a_timeout_keeps_the_claim(self, monkeypatch):
        def timeout(request):
            raise httpx.ConnectTimeout("resend took too long")

        store = MemoryFreeUserStore()
        now = await _make_capped(store)
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(timeout)
        )
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=2000, now=now
        ) is None
        assert (await store.get("sub-1")).cap_note_sent_at is not None

    @pytest.mark.asyncio
    async def test_the_note_carries_one_url_and_the_one_click_header(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 8 * DAY)
        await store.touch("sub-1", 6, now - 2 * DAY, tool="search_hotels")
        await store.touch("sub-1", 1, now, tool="search_oneway_flights")
        await store.note_cap_hit("sub-1", now - 2 * DAY)
        await store.note_cap_hit("sub-1", now)
        resend = _Recorder()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        config = _config(cap_note=True)
        await maybe_send_cap_note(store, "sub-1", config, month_cap=2000, now=now)

        body = resend.sends[0]
        link = unsubscribe_url(config, (await store.get("sub-1")).unsubscribe_token)
        assert body["subject"] == "your scan stopped halfway"
        assert body["from"] == "Matan Rabi <app@flightpowers.com>"
        assert body["headers"]["List-Unsubscribe"] == f"<{link}>"
        # Hotels, because that is what this account searched -- and exactly
        # one product URL, never both.
        assert "https://hotels.flightpowers.com/mcp" in body["text"]
        assert "flights.flightpowers.com" not in body["text"]
        assert "2,000 requests" in body["text"]
        assert "You hit the free daily cap twice this week" in body["text"]
        for part in (body["text"], body["html"]):
            assert link in part
            assert "RESEND_UNSUBSCRIBE_URL" not in part

    @pytest.mark.asyncio
    async def test_the_monthly_branch_opens_on_the_month(self, monkeypatch):
        """Branch M has nothing broken yet, so branch A's subject would be a
        lie about something they can check."""
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 20 * DAY)
        await store.touch("sub-1", 700, now - DAY, tool="search_oneway_flights")
        await store.touch("sub-1", 700, now, tool="search_oneway_flights")
        resend = _Recorder()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        signals = await store.cap_signals("sub-1", now)
        if cap_note_branch(signals, 2000) is None:
            pytest.skip("month-to-date rows fall in a month with under 5 days left")
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=2000, now=now
        ) == BRANCH_MONTHLY
        body = resend.sends[0]
        assert body["subject"] == "more room on your routes"
        assert "two thirds through this month's free searches" in body["text"]
        assert "https://flights.flightpowers.com/mcp" in body["text"]
        assert "2,500 requests" in body["text"]

    @pytest.mark.asyncio
    async def test_nothing_here_can_raise(self):
        """It runs in the background task that follows a tool call."""

        class Exploding:
            async def cap_signals(self, sub, now=None):
                raise RuntimeError("neon is down")

        assert await maybe_send_cap_note(
            Exploding(), "sub-1", _config(cap_note=True), month_cap=2000
        ) is None


# ── the switches, read from the environment ──────────────────────────────


class TestTheSwitches:
    def test_both_are_off_by_default(self, monkeypatch):
        monkeypatch.delenv("FREE_SIGNIN_WELCOME", raising=False)
        monkeypatch.delenv("FREE_SIGNIN_CAPNOTE", raising=False)
        config = load_mail_config(ORIGIN)
        assert config.welcome is False
        assert config.cap_note is False

    def test_each_switch_turns_on_only_its_own_send(self, monkeypatch):
        monkeypatch.setenv("FREE_SIGNIN_WELCOME", "on")
        monkeypatch.delenv("FREE_SIGNIN_CAPNOTE", raising=False)
        assert load_mail_config(ORIGIN).welcome is True
        assert load_mail_config(ORIGIN).cap_note is False
        monkeypatch.setenv("FREE_SIGNIN_CAPNOTE", "on")
        monkeypatch.delenv("FREE_SIGNIN_WELCOME", raising=False)
        assert load_mail_config(ORIGIN).welcome is False
        assert load_mail_config(ORIGIN).cap_note is True

    def test_the_outcomes_are_three_and_distinct(self):
        assert len({SENT, REFUSED, UNKNOWN}) == 3


# ── the unsubscribe route, over the wire ─────────────────────────────────


@pytest.fixture
def wired(monkeypatch):
    """The real app with sign-in on, holding an in-memory user store.

    Same build as `tests/test_signin.py::wired`; the store is swapped for
    the memory one so the route's effect on a row can be read back.
    `OAuthSupport` is frozen and the route closures captured the instance,
    so the swap has to be in place rather than a `replace()`.
    """
    from starlette.applications import Starlette

    from src.anon_gate import anon_cap_middleware
    from src.hard_limit import hard_limit_middleware
    from src.oauth import OAuthResourceGate
    from src.server import build_server
    from src.settings import load_settings

    monkeypatch.setenv("FAIR_USE_ENABLED", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.test/neon")
    monkeypatch.setenv("ADS_ENABLED", "false")
    server = build_server(load_settings())
    store = MemoryFreeUserStore()
    object.__setattr__(server.fp_oauth, "users", store)
    inner = server.http_app(
        stateless_http=True,
        middleware=list(hard_limit_middleware(server))
        + list(anon_cap_middleware(server)),
    )
    app = Starlette(lifespan=inner.lifespan)
    app.mount("/", OAuthResourceGate(inner, server.fp_oauth))
    return store, app


async def _get(app, path):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)


class TestTheUnsubscribeRoute:
    @pytest.mark.asyncio
    async def test_one_click_flips_the_opt_out(self, wired):
        """RFC 8058 one-click, which the `List-Unsubscribe-Post` header on
        every send in this lane promises: no session, no second click."""
        store, app = wired
        token = await _signed_in(store)
        page = await _get(app, f"/email/unsubscribe?t={token}")
        assert page.status_code == 200
        assert "Unsubscribed" in page.text
        assert (await store.get("sub-1")).email_opt_out is True

    @pytest.mark.asyncio
    async def test_a_wrong_token_changes_nothing_and_looks_the_same(self, wired):
        """A different page for a bad token would let anyone test tokens."""
        store, app = wired
        await _signed_in(store)
        page = await _get(app, "/email/unsubscribe?t=not-a-real-token")
        assert page.status_code == 200
        assert "Unsubscribed" in page.text
        assert (await store.get("sub-1")).email_opt_out is False

    @pytest.mark.asyncio
    async def test_an_opted_out_row_gets_neither_send(self, wired, monkeypatch):
        """The route is the whole opt-out: after it, no lane mails them."""
        store, app = wired
        token = await _signed_in(store)
        await _get(app, f"/email/unsubscribe?t={token}")

        def explode(request):  # pragma: no cover - must never be called
            raise AssertionError("an unsubscribed address was mailed")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        await remember_signin(
            store, "sub-1", "a@example.com", token, _config(welcome=True)
        )
        now = time.time()
        await store.touch("sub-1", 10, now - 2 * DAY, tool="search_oneway_flights")
        await store.touch("sub-1", 10, now, tool="search_oneway_flights")
        await store.note_cap_hit("sub-1", now - 2 * DAY)
        await store.note_cap_hit("sub-1", now)
        assert await maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=2000, now=now
        ) is None
        row = await store.get("sub-1")
        assert row.welcome_sent_at is None and row.cap_note_sent_at is None
