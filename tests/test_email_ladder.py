"""The upsell ladder: the welcome retry, the new cap copy, day-7, dormant.

Selection and copy: `state/gtm/email/FREE-SIGNIN-UPSELL-2026-09-17.md`.
What this file pins, and why each one is here rather than assumed:

1. **The welcome retry sends once, ever.** Three of nine real sign-ins
   between 09-13 and 09-16 never got a welcome: the flag was on, the
   address was on the row, and nothing retried. One retry is the fix; two
   would be a duplicate of a note some of those people may have received
   after all, which is the failure that costs a sending domain.
2. **A retry that Resend REFUSES un-stamps the row and keeps the ledger.**
   The account still reads as "no welcome" in `/metrics/signins` -- which
   is true -- and the job does not try that address again tomorrow.
3. **The cap note's live fare is a bracket that can be dropped.** A fare
   goes stale in minutes, so a lookup that does not answer costs the
   sentence its bracket and never prints an old number (CLAUDE.md rule 1).
4. **Day-7 and dormant select on windows, not on "sometime after".** Day
   6 is not eligible and day 11 is not either; the copy says "your first
   week" and "two weeks ago" and both have to be true when it lands.
5. **Every flag is off by default and each is its own.** Four sends behind
   one switch get turned on together by accident.
6. **Opt-out beats everything**, including a note nobody has sent yet.

    python -m pytest mcp_server/tests/test_email_ladder.py -q
"""

from __future__ import annotations

import time

import httpx
import pytest

from src.email_templates import ladder
from src.freeusers import (
    DAY7_KIND,
    DORMANT_KIND,
    MemoryFreeUserStore,
    WELCOME_KIND,
    WELCOME_RETRY_KIND,
    cap_kind,
)
from src.giftfare import fetch_gift_fare, gift_fare_bracket
from src.maillist import MailConfig, load_mail_config
from src.sequence import (
    KIND_CAP,
    KIND_CAP_RETRY,
    KIND_DAY7,
    KIND_DORMANT,
    KIND_WELCOME,
    run_sequence,
)
from src.signinstats import build_stats, sent_by_kind

ORIGIN = "https://mcp.test.invalid"
HOUR = 3600.0
DAY = 86400.0

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _config(**kwargs) -> MailConfig:
    base = {"api_key": "k", "unsubscribe_base": ORIGIN}
    base.update(kwargs)
    return MailConfig(**base)


class _Resend:
    """A Resend that says yes and remembers every payload."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"id": "e_1"}
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.calls.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.body)


def _client(handler):
    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_live_fare(monkeypatch):
    """No test in this file reaches the api front for a real fare.

    `RAPIDAPI_KEY` is unset in CI, so `gift_fare_bracket` already returns
    "" -- but a developer's shell that has it exported would otherwise
    make these tests spend a real search, and that is exactly the kind of
    surprise the fixture exists to remove.
    """
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)


# ── the welcome retry ────────────────────────────────────────────────────


class TestTheWelcomeRetry:
    @pytest.mark.asyncio
    async def test_it_picks_up_the_rows_the_callback_never_stamped(self):
        store = MemoryFreeUserStore()
        now = time.time()
        # Never stamped, and old enough to be a failure rather than a race.
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)
        # Stamped: the callback's own send went out.
        await store.upsert("sub-2", "b@example.com", now=now - 2 * DAY)
        await store.mark_welcome_sent("sub-2")
        # Signed in four minutes ago: the callback may still be running.
        await store.upsert("sub-3", "c@example.com", now=now - 4 * 60)
        # Signed in five weeks ago. Too late to welcome anybody.
        await store.upsert("sub-4", "d@example.com", now=now - 35 * DAY)

        body = await run_sequence(
            store, _config(), kind=KIND_WELCOME, now=now
        )
        assert body["eligible"] == 1
        assert body["candidates"][0]["sub"] == "sub-1"

    @pytest.mark.asyncio
    async def test_it_sends_once_and_a_second_run_sends_nothing(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )

        first = await run_sequence(
            store,
            _config(welcome=True),
            kind=KIND_WELCOME,
            dry_run=False,
            now=now,
            day_cap=50,
            month_cap=250,
        )
        assert first["sent"] == 1
        assert first["resend_ids"] == ["e_1"]
        assert len(resend.calls) == 1
        # Both guards are now held: the ledger row AND the once-ever stamp.
        assert (await store.get("sub-1")).welcome_sent_at is not None
        assert ("sub-1", WELCOME_RETRY_KIND) in store.sent

        second = await run_sequence(
            store,
            _config(welcome=True),
            kind=KIND_WELCOME,
            dry_run=False,
            now=now + HOUR,
        )
        assert second["eligible"] == 0
        assert len(resend.calls) == 1

    @pytest.mark.asyncio
    async def test_the_note_is_the_welcome_itself(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        await run_sequence(
            store,
            _config(welcome=True),
            kind=KIND_WELCOME,
            dry_run=False,
            now=now,
            day_cap=50,
            month_cap=250,
        )
        payload = resend.calls[0]
        assert payload["subject"] == "you're in, 50 searches a day"
        assert "50 a day, 250 a month" in payload["text"]
        assert "{{" not in payload["text"] and "{{" not in payload["html"]

    @pytest.mark.asyncio
    async def test_a_refusal_unstamps_the_row_and_does_not_try_again(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)
        refuser = _Resend(status=422, body={"name": "validation_error"})
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(refuser)
        )
        body = await run_sequence(
            store,
            _config(welcome=True),
            kind=KIND_WELCOME,
            dry_run=False,
            now=now,
        )
        assert body["refused"] == 1
        # The stamp is off: the note did not go, and the daily read must
        # keep counting this account as one the welcome never reached.
        assert (await store.get("sub-1")).welcome_sent_at is None
        # The ledger row stays, so the retry is one attempt and not a
        # daily re-send to an address Resend has already declined.
        assert ("sub-1", WELCOME_RETRY_KIND) in store.sent
        again = await run_sequence(
            store, _config(welcome=True), kind=KIND_WELCOME, now=now + DAY
        )
        assert again["eligible"] == 0

    @pytest.mark.asyncio
    async def test_the_welcome_flag_off_makes_no_request_at_all(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)

        def explode(request):  # pragma: no cover - reaching it is the failure
            raise AssertionError("a flag that is off may not call Resend")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        body = await run_sequence(
            store, _config(welcome=False), kind=KIND_WELCOME, dry_run=False, now=now
        )
        assert body["reason"] == "flag_off"
        assert body["sent"] == 0
        assert (await store.get("sub-1")).welcome_sent_at is None

    @pytest.mark.asyncio
    async def test_an_opted_out_row_is_never_retried(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 2 * DAY)
        await store.opt_out((await store.get("sub-1")).unsubscribe_token)
        body = await run_sequence(store, _config(), kind=KIND_WELCOME, now=now)
        assert body["eligible"] == 0


# ── the cap note's copy ──────────────────────────────────────────────────


class TestTheCapCopy:
    def test_branch_d_says_today_not_twice_this_week(self):
        note = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
            day_cap=50,
        )
        assert note.subject == "your scan stopped halfway"
        assert "You hit the free cap today" in note.text
        # The bug this replaced: the job selects anybody refused ONCE
        # today, so the old fixed text was untrue for a first-time capper.
        assert "twice this week" not in note.text
        assert "The cap is 50 a day" in note.text

    def test_basic_number_follows_the_product(self):
        """Flights BASIC went 10 -> 50 a month on 2026-11-08; hotels stays
        10. The hotels variant must swap the BASIC number too, not only the
        link and the PRO count, or a hotels reader is promised 50."""
        flights = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url=ladder.CONNECT_PRODUCTS["flights"][0],
            pro_requests=ladder.CONNECT_PRODUCTS["flights"][1],
            basic_requests=ladder.BASIC_REQUESTS["flights"],
        )
        hotels = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url=ladder.CONNECT_PRODUCTS["hotels"][0],
            pro_requests=ladder.CONNECT_PRODUCTS["hotels"][1],
            basic_requests=ladder.BASIC_REQUESTS["hotels"],
        )
        assert "BASIC is free with 50 requests a month, PRO is 10$ for 2,500" in flights.text
        assert "BASIC is free with 10 requests a month, PRO is 10$ for 2,000" in hotels.text
        assert set(ladder.BASIC_REQUESTS) == set(ladder.CONNECT_PRODUCTS)

    def test_branch_m_keeps_its_own_opener_and_ask(self):
        note = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
            branch=ladder.BRANCH_MONTHLY,
            days_left=9,
        )
        assert note.subject == "more room on your routes"
        assert "two thirds through this month's free searches" in note.text
        assert "9 days of the month to go" in note.text
        assert "I read every reply" in note.text

    def test_the_fare_bracket_is_printed_or_dropped_and_never_stale(self):
        with_fare = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
            gift_fare=" (164$ TLV to Athens nonstop both ways, one call)",
        )
        without = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
        )
        assert "one request there (164$ TLV to Athens" in with_fare.text
        assert "one request there." in without.text
        assert "$" not in without.text.split("BASIC")[1].split("\n")[0].replace(
            "10$", ""
        )

    def test_the_greeting_falls_back_to_the_reviewed_copy(self):
        anonymous = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
        )
        named = ladder.cap_note(
            unsubscribe_url="https://u/x",
            connect_url="https://flights.flightpowers.com/connect",
            pro_requests="2,500",
            first_name=" Alex",
        )
        assert anonymous.text.startswith("Hi !")
        assert named.text.startswith("Hi Alex !")

    @pytest.mark.asyncio
    async def test_the_job_renders_branch_d_and_asks_for_a_fare_once(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 8 * DAY)
        await store.upsert("sub-2", "b@example.com", now=now - 8 * DAY)
        for sub in ("sub-1", "sub-2"):
            await store.touch(sub, 40, now, tool="search_oneway_flights")
            await store.note_cap_hit(sub, now)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        asked = {"n": 0}

        async def one_fare(**kwargs):
            asked["n"] += 1
            return " (164$ TLV to Athens nonstop both ways, one call)"

        monkeypatch.setattr("src.sequence.gift_fare_bracket", one_fare)
        body = await run_sequence(
            store,
            _config(cap_note=True),
            kind=KIND_CAP,
            dry_run=False,
            now=now,
            day_cap=50,
            month_cap=250,
        )
        assert body["sent"] == 2
        assert body["gift_fare"] is True
        # ONE lookup for the whole run: twenty recipients is still one
        # round trip, billed once to our own plan.
        assert asked["n"] == 1
        for payload in resend.calls:
            assert "You hit the free cap today" in payload["text"]
            assert "164$ TLV to Athens" in payload["text"]
            assert "https://flights.flightpowers.com/connect" in payload["text"]

    @pytest.mark.asyncio
    async def test_a_fare_lookup_that_fails_costs_the_bracket_not_the_note(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 8 * DAY)
        await store.touch("sub-1", 40, now, tool="search_oneway_flights")
        await store.note_cap_hit("sub-1", now)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        body = await run_sequence(
            store,
            _config(cap_note=True),
            kind=KIND_CAP,
            dry_run=False,
            now=now,
            day_cap=50,
            month_cap=250,
        )
        assert body["sent"] == 1
        assert body["gift_fare"] is False
        assert "nonstop both ways" not in resend.calls[0]["text"]
        assert "You hit the free cap today" in resend.calls[0]["text"]


class TestTheFareLookup:
    @pytest.mark.asyncio
    async def test_no_key_means_no_fare_and_no_request(self, monkeypatch):
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)

        def explode(request):  # pragma: no cover - reaching it is the failure
            raise AssertionError("no key, no call")

        assert await gift_fare_bracket(client=_client(explode)) == ""

    @pytest.mark.asyncio
    async def test_the_cheapest_nonstop_pair_wins(self, monkeypatch):
        monkeypatch.setenv("RAPIDAPI_KEY", "key")
        payload = {
            "results": [
                {
                    "total_price_as_number": 132,
                    "departure_flight_stops": 1,
                    "return_flight_stops": 0,
                },
                {
                    "total_price_as_number": 164.4,
                    "departure_flight_stops": 0,
                    "return_flight_stops": 0,
                },
                {
                    "total_price_as_number": 210,
                    "departure_flight_stops": 0,
                    "return_flight_stops": 0,
                },
            ]
        }
        fare = await fetch_gift_fare(
            client=_client(lambda r: httpx.Response(200, json=payload))
        )
        assert fare is not None
        assert fare.price == 164
        assert fare.bracket == (
            " (164$ TLV to Athens nonstop both ways, one call)"
        )

    @pytest.mark.asyncio
    async def test_no_nonstop_pair_drops_the_bracket(self, monkeypatch):
        monkeypatch.setenv("RAPIDAPI_KEY", "key")
        payload = {
            "results": [
                {
                    "total_price_as_number": 99,
                    "departure_flight_stops": 1,
                    "return_flight_stops": 1,
                }
            ]
        }
        assert (
            await gift_fare_bracket(
                client=_client(lambda r: httpx.Response(200, json=payload))
            )
            == ""
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            lambda r: httpx.Response(401, json={"error": "no"}),
            lambda r: httpx.Response(200, text="not json"),
            lambda r: httpx.Response(200, json={"results": []}),
        ],
    )
    async def test_every_failure_is_a_dropped_bracket_not_an_exception(
        self, monkeypatch, answer
    ):
        monkeypatch.setenv("RAPIDAPI_KEY", "key")
        assert await gift_fare_bracket(client=_client(answer)) == ""

    @pytest.mark.asyncio
    async def test_a_timeout_is_a_dropped_bracket(self, monkeypatch):
        monkeypatch.setenv("RAPIDAPI_KEY", "key")

        def timeout(request):
            raise httpx.ConnectTimeout("no answer", request=request)

        assert await gift_fare_bracket(client=_client(timeout)) == ""


# ── day 7 ────────────────────────────────────────────────────────────────


async def _active(store, sub, email, *, signed_up_hours, searches, now):
    first_seen = now - signed_up_hours * HOUR
    await store.upsert(sub, email, now=first_seen)
    # Spread the searches over the first two days, inside the first week.
    await store.touch(sub, searches, first_seen + HOUR, tool="search_oneway_flights")


class TestTheDay7Note:
    @pytest.mark.asyncio
    async def test_the_window_and_the_floor_are_both_boundaries(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await _active(store, "in", "in@example.com", signed_up_hours=170, searches=19, now=now)
        # Day 6: the sentence "in your first week" is not true yet.
        await _active(store, "early", "e@example.com", signed_up_hours=160, searches=19, now=now)
        # Day 11: too late to describe their first week.
        await _active(store, "late", "l@example.com", signed_up_hours=250, searches=19, now=now)
        # Nine searches: the note's own opening sentence would be thin.
        await _active(store, "quiet", "q@example.com", signed_up_hours=170, searches=9, now=now)

        body = await run_sequence(store, _config(), kind=KIND_DAY7, now=now)
        assert [c["sub"] for c in body["candidates"]] == ["in"]
        assert body["candidates"][0]["first_week_searches"] == 19

    @pytest.mark.asyncio
    async def test_somebody_the_cap_refused_belongs_to_the_other_note(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await _active(store, "sub-1", "a@example.com", signed_up_hours=180, searches=64, now=now)
        await store.note_cap_hit("sub-1", now - 2 * DAY)
        body = await run_sequence(store, _config(), kind=KIND_DAY7, now=now)
        assert body["eligible"] == 0

    @pytest.mark.asyncio
    async def test_it_sends_once_with_their_own_number(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = time.time()
        await _active(store, "sub-1", "a@example.com", signed_up_hours=180, searches=47, now=now)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        body = await run_sequence(
            store,
            _config(day7=True),
            kind=KIND_DAY7,
            dry_run=False,
            now=now,
            day_cap=50,
        )
        assert body["sent"] == 1
        payload = resend.calls[0]
        assert payload["subject"] == "the thing almost nobody tries"
        assert "You've run 47 searches in your first week" in payload["text"]
        assert "eats the 50 a day fast" in payload["text"]
        assert "{{" not in payload["text"] and "{{" not in payload["html"]
        assert ("sub-1", DAY7_KIND) in store.sent

        again = await run_sequence(
            store, _config(day7=True), kind=KIND_DAY7, dry_run=False, now=now + HOUR
        )
        assert again["eligible"] == 0
        assert len(resend.calls) == 1

    @pytest.mark.asyncio
    async def test_the_flag_is_its_own(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = time.time()
        await _active(store, "sub-1", "a@example.com", signed_up_hours=180, searches=47, now=now)

        def explode(request):  # pragma: no cover
            raise AssertionError("a flag that is off may not call Resend")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        # Every OTHER note switched on, and this one still does not fire.
        body = await run_sequence(
            store,
            _config(welcome=True, cap_note=True, day2=True, dormant=True),
            kind=KIND_DAY7,
            dry_run=False,
            now=now,
        )
        assert body["reason"] == "flag_off"

    @pytest.mark.asyncio
    async def test_an_opted_out_row_is_never_mailed(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await _active(store, "sub-1", "a@example.com", signed_up_hours=180, searches=47, now=now)
        await store.opt_out((await store.get("sub-1")).unsubscribe_token)
        body = await run_sequence(store, _config(), kind=KIND_DAY7, now=now)
        assert body["eligible"] == 0


# ── dormant ──────────────────────────────────────────────────────────────


class TestTheDormantNote:
    @pytest.mark.asyncio
    async def test_the_fourteen_to_seventeen_day_window(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("in", "in@example.com", now=now - 15 * DAY)
        # Thirteen days: "two weeks ago" is not true yet.
        await store.upsert("early", "e@example.com", now=now - 13 * DAY)
        # Three weeks: a backlog must not fire as "two weeks ago".
        await store.upsert("late", "l@example.com", now=now - 21 * DAY)
        # One search still counts as quiet; two does not.
        await store.upsert("once", "o@example.com", now=now - 15 * DAY)
        await store.touch("once", 1, now - 14 * DAY, tool="search_hotels")
        await store.upsert("busy", "b@example.com", now=now - 15 * DAY)
        await store.touch("busy", 2, now - 14 * DAY, tool="search_hotels")

        body = await run_sequence(store, _config(), kind=KIND_DORMANT, now=now)
        assert sorted(c["sub"] for c in body["candidates"]) == ["in", "once"]

    @pytest.mark.asyncio
    async def test_it_is_a_question_with_no_link_and_no_price(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 15 * DAY)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        body = await run_sequence(
            store, _config(dormant=True), kind=KIND_DORMANT, dry_run=False, now=now
        )
        assert body["sent"] == 1
        payload = resend.calls[0]
        assert payload["subject"] == "what were you trying to search?"
        assert "What did you want to search?" in payload["text"]
        # No pitch means no pitch: no product URL and no price anywhere but
        # the unsubscribe footer.
        assert "flightpowers.com/connect" not in payload["text"]
        assert "$" not in payload["text"]
        assert ("sub-1", DORMANT_KIND) in store.sent

        again = await run_sequence(
            store,
            _config(dormant=True),
            kind=KIND_DORMANT,
            dry_run=False,
            now=now + HOUR,
        )
        assert again["eligible"] == 0
        assert len(resend.calls) == 1

    @pytest.mark.asyncio
    async def test_the_flag_is_its_own(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 15 * DAY)

        def explode(request):  # pragma: no cover
            raise AssertionError("a flag that is off may not call Resend")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        body = await run_sequence(
            store,
            _config(welcome=True, cap_note=True, day2=True, day7=True),
            kind=KIND_DORMANT,
            dry_run=False,
            now=now,
        )
        assert body["reason"] == "flag_off"

    @pytest.mark.asyncio
    async def test_an_opted_out_row_is_never_mailed(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 15 * DAY)
        await store.opt_out((await store.get("sub-1")).unsubscribe_token)
        body = await run_sequence(store, _config(), kind=KIND_DORMANT, now=now)
        assert body["eligible"] == 0


# ── the flags, and what the daily read now says ──────────────────────────


def test_every_new_flag_defaults_off(monkeypatch):
    for name in (
        "FREE_SIGNIN_WELCOME",
        "FREE_SIGNIN_CAPNOTE",
        "FREE_SIGNIN_DAY2",
        "FREE_SIGNIN_DAY7",
        "FREE_SIGNIN_DORMANT",
    ):
        monkeypatch.delenv(name, raising=False)
    config = load_mail_config(ORIGIN)
    assert not config.day7
    assert not config.dormant
    assert not config.welcome
    assert not config.cap_note
    assert not config.day2

    monkeypatch.setenv("FREE_SIGNIN_DAY7", "on")
    assert load_mail_config(ORIGIN).day7
    # And turning one on does not turn on its neighbours.
    assert not load_mail_config(ORIGIN).dormant


class TestTheDailyReadCountsEachKind:
    @pytest.mark.asyncio
    async def test_counts_per_kind_and_the_welcome_queue(self):
        store = MemoryFreeUserStore()
        now = time.time()
        # Welcomed, and later got a day-7 note.
        await store.upsert("sub-1", "a@example.com", now=now - 8 * DAY)
        await store.mark_welcome_sent("sub-1")
        await store.claim_sent_email("sub-1", DAY7_KIND, now)
        # Welcomed and capped, through the inline path (no ledger row).
        await store.upsert("sub-2", "b@example.com", now=now - 8 * DAY)
        # The cap claim first: the 72-hour gap after the welcome is a real
        # guard and this test is about the counting, not about that.
        assert await store.claim_cap_note("sub-2", now)
        await store.mark_welcome_sent("sub-2")
        # Signed in two days ago and never welcomed: the retry's queue.
        await store.upsert("sub-3", "c@example.com", now=now - 2 * DAY)
        # Signed in one minute ago: the callback may still be running, so
        # this one is not a failure and must not be counted as one.
        await store.upsert("sub-4", "d@example.com", now=now - 60)

        records = await store.signin_records(now=now, hours=24)
        body = build_stats(records, hours=24, now=now)
        counts = body["totals"]["sent_by_kind"]
        assert counts == {
            "welcome": 2,
            "day2": 0,
            "cap": 1,
            "day7": 1,
            "dormant14": 0,
        }
        assert body["totals"]["welcome_failed"] == 1

    def test_sent_by_kind_counts_accounts_and_not_messages(self):
        from src.freeusers import SigninRecord

        rows = [
            SigninRecord(
                google_sub="s",
                email="a@example.com",
                kinds_sent=("cap:2026-09-17", "cap:2026-09-18", DORMANT_KIND),
            )
        ]
        assert sent_by_kind(rows) == {
            "welcome": 0,
            "day2": 0,
            "cap": 1,
            "day7": 0,
            "dormant14": 1,
        }


# ── the receipt: a claim is not a send ───────────────────────────────────


class TestTheSendLedger:
    """`welcome_sent_at` is stamped BEFORE the send, so it cannot say the
    note went out. The ledger row carries Resend's id and is written after
    the 2xx, which is what makes a lost send a queryable fact instead of
    something a human has to reconstruct from a table four days later."""

    @pytest.mark.asyncio
    async def test_the_callback_writes_the_receipt_on_a_real_send(
        self, monkeypatch
    ):
        from src.maillist import remember_signin

        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now)
        resend = _Resend(body={"id": "e_welcome"})
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        await remember_signin(
            store,
            "sub-1",
            "a@example.com",
            (await store.get("sub-1")).unsubscribe_token,
            _config(welcome=True),
            day_cap=50,
            month_cap=250,
        )
        assert (await store.get("sub-1")).welcome_sent_at is not None
        assert store.sent[("sub-1", WELCOME_KIND)]["resend_id"] == "e_welcome"

    @pytest.mark.asyncio
    async def test_a_lost_send_leaves_a_stamp_with_no_receipt(
        self, monkeypatch
    ):
        """A timeout: Resend may have taken it, so the claim stands -- and
        the missing receipt is the only trace that it might not have."""
        from src.maillist import remember_signin

        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now)

        def timeout(request):
            raise httpx.ConnectTimeout("no answer", request=request)

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(timeout)
        )
        await remember_signin(
            store,
            "sub-1",
            "a@example.com",
            (await store.get("sub-1")).unsubscribe_token,
            _config(welcome=True),
            day_cap=50,
            month_cap=250,
        )
        assert (await store.get("sub-1")).welcome_sent_at is not None
        assert ("sub-1", WELCOME_KIND) not in store.sent

    @pytest.mark.asyncio
    async def test_a_stamp_from_before_the_ledger_is_never_re_welcomed(self):
        """The one thing worse than a missing welcome is a second one.

        Every welcome sent before this code shipped is stamped and has no
        receipt, and there is no way to tell those from a lost send -- so
        with no cutover configured they are left alone.
        """
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 3 * DAY)
        await store.mark_welcome_sent("sub-1")

        body = await run_sequence(store, _config(), kind=KIND_WELCOME, now=now)
        assert body["eligible"] == 0

        records = await store.signin_records(now=now, hours=24)
        stats = build_stats(records, hours=24, now=now)
        assert stats["totals"]["welcome_failed"] == 0
        assert stats["totals"]["welcome_unconfirmed"] == 1

    @pytest.mark.asyncio
    async def test_past_the_cutover_a_stamp_with_no_receipt_is_a_lost_send(
        self,
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 3 * DAY)
        await store.mark_welcome_sent("sub-1")
        cutover = now - 30 * DAY  # the ledger has been live for a month

        body = await run_sequence(
            store, _config(), kind=KIND_WELCOME, now=now, ledger_since=cutover
        )
        assert [c["sub"] for c in body["candidates"]] == ["sub-1"]

        records = await store.signin_records(now=now, hours=24)
        stats = build_stats(
            records, hours=24, now=now, ledger_since=cutover
        )
        assert stats["totals"]["welcome_failed"] == 1
        assert stats["totals"]["welcome_unconfirmed"] == 0

    @pytest.mark.asyncio
    async def test_a_confirmed_welcome_is_never_a_candidate(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 3 * DAY)
        await store.mark_welcome_sent("sub-1")
        await store.claim_sent_email("sub-1", WELCOME_KIND, now)
        await store.finish_sent_email("sub-1", WELCOME_KIND, "e_1")

        body = await run_sequence(
            store,
            _config(),
            kind=KIND_WELCOME,
            now=now,
            ledger_since=now - 30 * DAY,
        )
        assert body["eligible"] == 0
        records = await store.signin_records(now=now, hours=24)
        stats = build_stats(records, hours=24, now=now, ledger_since=now - 30 * DAY)
        assert stats["totals"]["welcome_failed"] == 0


# ── the cap note's own retry ─────────────────────────────────────────────


class TestTheCapRetry:
    async def _lost_cap_note(self, store, sub, email, *, now, ago_days=0.0):
        """An account whose cap note was claimed and never confirmed."""
        moment = now - ago_days * DAY
        await store.upsert(sub, email, now=now - 10 * DAY)
        await store.touch(sub, 40, moment, tool="search_oneway_flights")
        await store.note_cap_hit(sub, moment)
        assert await store.claim_cap_note(sub, moment)

    @pytest.mark.asyncio
    async def test_a_claimed_note_with_no_receipt_is_retried_once(
        self, monkeypatch
    ):
        store = MemoryFreeUserStore()
        now = time.time()
        await self._lost_cap_note(store, "sub-1", "a@example.com", now=now)
        resend = _Resend(body={"id": "e_cap"})
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )

        first = await run_sequence(
            store,
            _config(cap_note=True),
            kind=KIND_CAP_RETRY,
            dry_run=False,
            now=now,
            day_cap=50,
            month_cap=250,
        )
        assert first["sent"] == 1
        assert "You hit the free cap today" in resend.calls[0]["text"]
        # The receipt lands under the LOST note's own day, so the account
        # stops looking lost and a genuine new cap note later today is not
        # suppressed by a claim taken for this one.
        assert store.sent[("sub-1", cap_kind(now))]["resend_id"] == "e_cap"

        second = await run_sequence(
            store,
            _config(cap_note=True),
            kind=KIND_CAP_RETRY,
            dry_run=False,
            now=now + HOUR,
        )
        assert second["eligible"] == 0
        assert len(resend.calls) == 1

    @pytest.mark.asyncio
    async def test_a_confirmed_note_is_not_retried(self):
        store = MemoryFreeUserStore()
        now = time.time()
        await self._lost_cap_note(store, "sub-1", "a@example.com", now=now)
        await store.claim_sent_email("sub-1", cap_kind(now), now)
        await store.finish_sent_email("sub-1", cap_kind(now), "e_cap")
        body = await run_sequence(
            store, _config(), kind=KIND_CAP_RETRY, now=now
        )
        assert body["eligible"] == 0

    @pytest.mark.asyncio
    async def test_a_note_older_than_three_days_is_left_alone(self):
        """"Your scan stopped halfway" is about today, not about Tuesday."""
        store = MemoryFreeUserStore()
        now = time.time()
        await self._lost_cap_note(
            store, "sub-1", "a@example.com", now=now, ago_days=4
        )
        body = await run_sequence(
            store, _config(), kind=KIND_CAP_RETRY, now=now
        )
        assert body["eligible"] == 0

    @pytest.mark.asyncio
    async def test_it_rides_the_cap_notes_flag(self, monkeypatch):
        store = MemoryFreeUserStore()
        now = time.time()
        await self._lost_cap_note(store, "sub-1", "a@example.com", now=now)

        def explode(request):  # pragma: no cover
            raise AssertionError("a flag that is off may not call Resend")

        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(explode)
        )
        body = await run_sequence(
            store,
            _config(cap_note=False, welcome=True, day7=True),
            kind=KIND_CAP_RETRY,
            dry_run=False,
            now=now,
        )
        assert body["reason"] == "flag_off"


# ── the two timeouts that decide whether a note ships at all ─────────────


class TestTheTimeouts:
    @pytest.mark.asyncio
    async def test_the_inline_fare_lookup_is_capped_at_two_seconds(
        self, monkeypatch
    ):
        """It runs after a tool call, on an instance that can freeze: a
        slow fare must cost the bracket, never the note."""
        from src import maillist

        seen = {}

        async def slow(**kwargs):
            seen.update(kwargs)
            return ""

        monkeypatch.setattr(maillist, "gift_fare_bracket", slow)
        store = MemoryFreeUserStore()
        now = time.time()
        await store.upsert("sub-1", "a@example.com", now=now - 10 * DAY)
        for day in (2, 1):
            await store.touch("sub-1", 20, now - day * DAY, tool="search_hotels")
            await store.note_cap_hit("sub-1", now - day * DAY)
        resend = _Resend()
        monkeypatch.setattr(
            "src.maillist.httpx.AsyncClient", lambda **kw: _client(resend)
        )
        branch = await maillist.maybe_send_cap_note(
            store, "sub-1", _config(cap_note=True), month_cap=250, day_cap=50, now=now
        )
        assert branch == maillist.BRANCH_DAILY
        assert seen["timeout"] == maillist.INLINE_FARE_TIMEOUT == 2.0
        # The note went out, without the sentence's bracket.
        assert len(resend.calls) == 1
        assert "nonstop both ways" not in resend.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_a_hand_off_that_outruns_its_window_keeps_running(self):
        """The sign-in redirect waits, but never cancels: a send that may
        already be at Resend must not be killed by a timeout."""
        import asyncio

        from src.tasks import spawn_and_wait

        finished = asyncio.Event()

        async def slow() -> None:
            await asyncio.sleep(0.05)
            finished.set()

        assert await spawn_and_wait(slow(), timeout=0.01) is False
        assert not finished.is_set()
        await asyncio.wait_for(finished.wait(), 1.0)

    @pytest.mark.asyncio
    async def test_a_hand_off_inside_its_window_is_awaited(self):
        from src.tasks import spawn_and_wait

        done = []

        async def quick() -> None:
            done.append(1)

        assert await spawn_and_wait(quick(), timeout=1.0) is True
        assert done == [1]


# ── no literal caps ──────────────────────────────────────────────────────


def test_an_unknown_cap_drops_the_sentence_rather_than_inventing_one():
    """Rule 1: a number in an email has to be traceable to what this
    server enforces. A fallback literal is not."""
    known = ladder.cap_note(
        unsubscribe_url="https://u/x",
        connect_url="https://flights.flightpowers.com/connect",
        pro_requests="2,500",
        day_cap=50,
    )
    unknown = ladder.cap_note(
        unsubscribe_url="https://u/x",
        connect_url="https://flights.flightpowers.com/connect",
        pro_requests="2,500",
        day_cap=0,
    )
    assert "The cap is 50 a day" in known.text
    assert "The cap is" not in unknown.text
    assert "a search stopped before it finished." in unknown.text

    day7_known = ladder.day7_note(
        unsubscribe_url="https://u/x",
        connect_url="https://flights.flightpowers.com/connect",
        searches=19,
        day_cap=50,
    )
    day7_unknown = ladder.day7_note(
        unsubscribe_url="https://u/x",
        connect_url="https://flights.flightpowers.com/connect",
        searches=19,
        day_cap=0,
    )
    assert "eats the 50 a day fast" in day7_known.text
    assert "eats the free allowance fast" in day7_unknown.text
    # The link survives either way: it is the note's only path forward.
    for note in (day7_known, day7_unknown):
        assert "https://flights.flightpowers.com/connect" in note.text
    for note in (known, unknown, day7_known, day7_unknown):
        assert "150" not in note.text
