"""The scheduled lane: `POST /email/run-sequence`, day-2 and cap-day.

Selection and copy: `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md` and
`state/gtm/research/outreach-dm-craft-pass9-2026-09-11.md` §2.2. Both notes
are OFF by default and each has its own switch (`FREE_SIGNIN_DAY2`,
`FREE_SIGNIN_CAPNOTE`).

What this file pins, and why each one is here rather than assumed:

1. **The day-2 note goes to the one state it is written for.** Signed in at
   least 48 hours ago, and never searched -- by `call_count` AND by the day
   rows, because both are written best-effort and either can be short. A
   user who searched once is exited from the flow forever, which is what
   makes a fourth touch defensible at all.
2. **The 48-hour boundary is a boundary.** 47 hours is not eligible; 49 is.
   A note that lands the same evening somebody signed in is a nag.
3. **Idempotent across runs.** The guard is a `sent_emails` row claimed
   BEFORE the Resend call, not "the last run did not error". Running the
   job twice sends one note; running it twice concurrently sends one note.
4. **A dry run sends nothing, and needs no flag to say who it would mail.**
   Zero HTTP requests, and the eligible list still comes back.
5. **A flag off is not a discarded send.** No request is made at all, and
   the response says `flag_off` instead of reporting a silent zero.
6. **A refusal releases the claim; a timeout never does.** Resend saying 422
   means nothing is in flight. A timeout means it may have delivered, and a
   second copy of an unasked-for email is what costs a sending domain.
7. **Every placeholder is filled.** A `{{` left in a sent email is the whole
   note's credibility, and it is one string away at all times.

    python -m pytest mcp_server/tests/test_email_sequence.py -q
"""

from __future__ import annotations

import time

import httpx
import pytest

from src.freeusers import (
    CAP_NOTE_MIN_GAP_DAYS,
    DAY2_KIND,
    MemoryFreeUserStore,
    SequenceCandidate,
    cap_kind,
)
from src.maillist import MailConfig, load_mail_config
from src.sequence import (
    KIND_CAP,
    KIND_DAY2,
    first_name_from_email,
    mask_email,
    render_cap,
    render_day2,
    run_sequence,
)

ORIGIN = "https://mcp.test.invalid"
HOUR = 3600.0
DAY = 86400.0

#: Captured before anything patches httpx, for the same reason
#: `test_email_lane.py` does it: the fakes below build a real client over a
#: MockTransport, and looking the class up through the module under test
#: would make each fake construct the next one.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _config(**kwargs) -> MailConfig:
    base = {
        "api_key": "k",
        "unsubscribe_base": ORIGIN,
        "day2": True,
        "cap_note": True,
    }
    base.update(kwargs)
    return MailConfig(**base)


class _Resend:
    """A Resend that says yes and remembers every payload it was handed."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"id": "e_1"}
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.calls.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.body)


class _Dead:
    """A Resend that never answers. Every attempt is a timeout."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectTimeout("no answer", request=request)


def _client(handler):
    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))


async def _signed_in(
    store: MemoryFreeUserStore, sub: str, email: str, *, ago_hours: float, now: float
) -> None:
    await store.upsert(sub, email, now=now - ago_hours * HOUR)


# ── day-2 selection ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_day2_selects_only_the_signed_in_who_never_searched():
    """The whole justification for a fourth touch, as one assertion.

    Three accounts, all two days old: one that never searched, one that ran
    a search, and one that has a `call_count` but no day row (the shape a
    frozen instance leaves behind). Only the first is written to -- and the
    third is excluded by the counter the day rows do not cover, which is
    why "never searched" is asserted against both.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)
    await _signed_in(store, "searched", "searched@example.com", ago_hours=50, now=now)
    await _signed_in(store, "counted", "counted@example.com", ago_hours=50, now=now)
    await store.touch("searched", 3, now=now - 20 * HOUR)
    # A count with no day row: the instance died between the two writes.
    from dataclasses import replace

    store.rows["counted"] = replace(store.rows["counted"], call_count=1)

    body = await run_sequence(store, _config(), kind=KIND_DAY2, now=now)

    assert [c["sub"] for c in body["candidates"]] == ["quiet"]
    assert body["eligible"] == 1


@pytest.mark.asyncio
async def test_day2_forty_eight_hour_boundary():
    """47 hours is too early, 49 is not. The boundary is the rule."""
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "early", "early@example.com", ago_hours=47, now=now)
    await _signed_in(store, "ready", "ready@example.com", ago_hours=49, now=now)

    body = await run_sequence(store, _config(), kind=KIND_DAY2, now=now)

    assert [c["sub"] for c in body["candidates"]] == ["ready"]


@pytest.mark.asyncio
async def test_day2_respects_the_optional_upper_bound():
    """Pass 9 scopes the note to 48-96 h; `max_hours` is how a caller says so.

    The default has no ceiling, because the first run after this ships has
    a backlog of sign-ins that were never written to and they are real
    people, not a leak. A cron that wants the narrow window asks for it.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "fresh", "fresh@example.com", ago_hours=60, now=now)
    await _signed_in(store, "stale", "stale@example.com", ago_hours=200, now=now)

    wide = await run_sequence(store, _config(), kind=KIND_DAY2, now=now)
    narrow = await run_sequence(
        store, _config(), kind=KIND_DAY2, now=now, max_hours=96
    )

    assert {c["sub"] for c in wide["candidates"]} == {"fresh", "stale"}
    assert [c["sub"] for c in narrow["candidates"]] == ["fresh"]


@pytest.mark.asyncio
async def test_opt_out_is_never_mailed():
    """One click on the unsubscribe link, and this lane is over for them.

    Checked here and not only in `test_email_lane.py` because an opt-out
    that only the event-driven sends honour is not an opt-out.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "gone", "gone@example.com", ago_hours=50, now=now)
    await store.opt_out(store.rows["gone"].unsubscribe_token)

    body = await run_sequence(store, _config(), kind=KIND_DAY2, now=now)

    assert body["candidates"] == []


@pytest.mark.asyncio
async def test_day2_needs_an_address():
    """An unverified Google account arrives with no address at all.

    `webauth.GoogleWebAuth.finish` hands us "" rather than a guess, so the
    row exists and the person simply cannot be written to.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "anon", "", ago_hours=50, now=now)

    body = await run_sequence(store, _config(), kind=KIND_DAY2, now=now)

    assert body["candidates"] == []


@pytest.mark.asyncio
async def test_day2_is_capped_and_takes_the_newest_first():
    """The lane ceiling is a ceiling, and a backlog drains newest first.

    A spike in sign-ins must not turn into a mail blast; the overflow waits
    for tomorrow. Newest first because if a backlog ever does build up, the
    people who signed in this week are the ones the note is about.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    for i in range(25):
        await _signed_in(
            store, f"u{i:02d}", f"u{i:02d}@example.com", ago_hours=50 + i, now=now
        )

    body = await run_sequence(store, _config(), kind=KIND_DAY2, now=now, limit=100)

    assert body["eligible"] == 20
    assert body["limit"] == 20
    assert body["candidates"][0]["sub"] == "u00"


# ── sending, and not sending ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_sends_nothing_and_still_names_everybody():
    """The default, and the thing that makes the route safe to run by hand."""
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)
    resend = _Resend()

    body = await run_sequence(
        store, _config(), kind=KIND_DAY2, now=now, client=_client(resend)
    )

    assert body["dry_run"] is True
    assert body["reason"] == "dry_run"
    assert body["sent"] == 0
    assert body["eligible"] == 1
    assert resend.calls == []
    assert (store.rows["quiet"].google_sub, DAY2_KIND) not in store.sent


@pytest.mark.asyncio
async def test_flag_off_makes_no_request_at_all():
    """Not a request that is discarded. No request.

    CLAUDE.md rule 14 is the reason each note has its own switch: 35 Studio
    upsell notes produced 0 replies and 0 upgrades, and a lane that can be
    switched on by accident is a lane that will be.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)
    resend = _Resend()

    body = await run_sequence(
        store,
        _config(day2=False),
        kind=KIND_DAY2,
        dry_run=False,
        now=now,
        client=_client(resend),
    )

    assert body["reason"] == "flag_off"
    assert body["sent"] == 0
    assert resend.calls == []
    assert store.sent == {}


@pytest.mark.asyncio
async def test_no_api_key_is_reported_not_guessed():
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)

    body = await run_sequence(
        store, _config(api_key=""), kind=KIND_DAY2, dry_run=False, now=now
    )

    assert body["reason"] == "no_api_key"
    assert body["sent"] == 0


@pytest.mark.asyncio
async def test_a_real_run_sends_once_and_a_second_run_sends_nothing():
    """Idempotency, which is the only property of this job that can hurt.

    The guard is the `sent_emails` row, claimed before the send. The second
    run does not merely skip the send -- the account is not even selected,
    because the claim is part of the selection query.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)
    resend = _Resend(body={"id": "e_day2"})

    first = await run_sequence(
        store,
        _config(),
        kind=KIND_DAY2,
        dry_run=False,
        now=now,
        client=_client(resend),
    )
    second = await run_sequence(
        store,
        _config(),
        kind=KIND_DAY2,
        dry_run=False,
        now=now + DAY,
        client=_client(resend),
    )

    assert first["sent"] == 1
    assert first["resend_ids"] == ["e_day2"]
    assert second["eligible"] == 0
    assert second["sent"] == 0
    assert len(resend.calls) == 1
    assert store.sent[("quiet", DAY2_KIND)]["resend_id"] == "e_day2"


@pytest.mark.asyncio
async def test_a_refusal_releases_the_claim_and_a_timeout_does_not():
    """The asymmetry that keeps a duplicate off somebody's phone.

    Resend answering 422 means nothing is in flight, so the claim is a lie
    and a later run may try again. A timeout means it may have delivered,
    and the claim stands forever.
    """
    now = time.time()
    refused_store = MemoryFreeUserStore()
    await _signed_in(refused_store, "a", "a@example.com", ago_hours=50, now=now)
    refused = _Resend(status=422, body={"message": "no"})
    body = await run_sequence(
        refused_store,
        _config(),
        kind=KIND_DAY2,
        dry_run=False,
        now=now,
        client=_client(refused),
    )
    assert body["refused"] == 1
    assert refused_store.sent == {}

    lost_store = MemoryFreeUserStore()
    await _signed_in(lost_store, "b", "b@example.com", ago_hours=50, now=now)
    dead = _Dead()
    body = await run_sequence(
        lost_store,
        _config(),
        kind=KIND_DAY2,
        dry_run=False,
        now=now,
        client=_client(dead),
    )
    assert body["unknown"] == 1
    assert ("b", DAY2_KIND) in lost_store.sent
    assert lost_store.sent[("b", DAY2_KIND)]["resend_id"] == ""


@pytest.mark.asyncio
async def test_a_claim_already_held_is_counted_not_sent():
    """Two overlapping runs of the cron are one email.

    Simulated by claiming the row first, which is exactly what the other
    run would have done a millisecond earlier.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "quiet", "quiet@example.com", ago_hours=50, now=now)
    candidates = await store.day2_candidates(now)
    assert len(candidates) == 1
    await store.claim_sent_email("quiet", DAY2_KIND, now)
    resend = _Resend()

    # The selection query would now skip it, so drive the send path with a
    # store whose row was claimed between the SELECT and the INSERT.
    class _Racing(MemoryFreeUserStore):
        async def day2_candidates(self, *args, **kwargs):
            return candidates

    racing = _Racing()
    racing.rows = store.rows
    racing.sent = store.sent
    body = await run_sequence(
        racing,
        _config(),
        kind=KIND_DAY2,
        dry_run=False,
        now=now,
        client=_client(resend),
    )

    assert body["already_claimed"] == 1
    assert body["sent"] == 0
    assert resend.calls == []


# ── the cap-day note ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cap_selects_the_accounts_the_day_cap_refused_today():
    """Today's row, and only today's.

    Yesterday's refusal is yesterday's news: the note says a scan stopped,
    and a person whose scan stopped last week has already worked around it.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "capped", "capped@example.com", ago_hours=200, now=now)
    await _signed_in(store, "fine", "fine@example.com", ago_hours=200, now=now)
    await _signed_in(store, "old", "old@example.com", ago_hours=200, now=now)
    await store.touch("capped", 150, now=now, tool="search_oneway_flights")
    await store.note_cap_hit("capped", now=now)
    await store.touch("fine", 12, now=now, tool="search_oneway_flights")
    await store.note_cap_hit("old", now=now - 2 * DAY)

    body = await run_sequence(store, _config(), kind=KIND_CAP, now=now)

    assert [c["sub"] for c in body["candidates"]] == ["capped"]
    assert body["candidates"][0]["month_searches"] == 150
    assert body["candidates"][0]["product"] == "flights"


@pytest.mark.asyncio
async def test_cap_is_once_per_account_per_day_and_then_thirty_days():
    """Two guards, and the test is that both of them bite.

    `sent_emails` keyed `cap:<UTC day>` stops a second note the same day --
    the case a re-run of the cron produces. The 30-day spacing on
    `free_mcp_users.cap_note_sent_at`, which the inline path shares, stops
    a note tomorrow.
    """
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "capped", "capped@example.com", ago_hours=200, now=now)
    await store.touch("capped", 150, now=now)
    await store.note_cap_hit("capped", now=now)
    resend = _Resend(body={"id": "e_cap"})

    first = await run_sequence(
        store,
        _config(),
        kind=KIND_CAP,
        dry_run=False,
        now=now,
        client=_client(resend),
    )
    same_day = await run_sequence(
        store, _config(), kind=KIND_CAP, dry_run=False, now=now, client=_client(resend)
    )
    await store.note_cap_hit("capped", now=now + DAY)
    next_day = await run_sequence(
        store,
        _config(),
        kind=KIND_CAP,
        dry_run=False,
        now=now + DAY,
        client=_client(resend),
    )

    assert first["sent"] == 1
    assert store.sent[("capped", cap_kind(now))]["resend_id"] == "e_cap"
    assert same_day["eligible"] == 0
    assert next_day["eligible"] == 0
    assert len(resend.calls) == 1

    # And after the spacing has run out, they are reachable again.
    later = now + (CAP_NOTE_MIN_GAP_DAYS + 1) * DAY
    await store.note_cap_hit("capped", now=later)
    reopened = await run_sequence(store, _config(), kind=KIND_CAP, now=later)
    assert reopened["eligible"] == 1


@pytest.mark.asyncio
async def test_cap_note_flag_is_its_own():
    now = time.time()
    store = MemoryFreeUserStore()
    await _signed_in(store, "capped", "capped@example.com", ago_hours=200, now=now)
    await store.touch("capped", 150, now=now)
    await store.note_cap_hit("capped", now=now)
    resend = _Resend()

    body = await run_sequence(
        store,
        _config(cap_note=False, day2=True),
        kind=KIND_CAP,
        dry_run=False,
        now=now,
        client=_client(resend),
    )

    assert body["reason"] == "flag_off"
    assert resend.calls == []


# ── rendering ────────────────────────────────────────────────────────────


def _candidate(**kwargs) -> SequenceCandidate:
    base = {
        "google_sub": "s",
        "email": "alex@example.com",
        "unsubscribe_token": "tok123",
    }
    base.update(kwargs)
    return SequenceCandidate(**base)


def test_every_placeholder_is_filled():
    """A `{{` that reaches an inbox is the whole note's credibility.

    One string constant away at all times, in a template nobody re-reads
    because it was approved once.
    """
    config = _config()
    day2 = render_day2(_candidate(), config)
    cap = render_cap(
        _candidate(month_searches=1400), config, day_cap=50, month_cap=250
    )

    for note in (day2, cap):
        for part in (note.text, note.html):
            assert "{{" not in part
            assert f"{ORIGIN}/email/unsubscribe?t=tok123" in part
    # The day-2 note greets with a comma, the ladder notes with the
    # reviewed "Hi Alex !" -- both are the approved bytes of their own copy.
    assert "Hi Alex," in day2.text
    assert "Hi Alex !" in cap.text
    # The month-to-date number lives in the hero card, which is HTML only:
    # the reviewed plain text of the cap note does not carry it.
    assert "1,400" in cap.html


def test_the_cap_note_links_the_product_the_account_actually_used():
    """One URL in the note, chosen -- and it is `/connect`, not `/mcp`.

    `/mcp` is the machine endpoint: a human who clicks it in a mail client
    gets a raw JSON 401 (pass 9, 2026-09-11). The PRO request count travels
    with the URL, so a hotels reader is never quoted the flights number.
    """
    config = _config()
    hotels = render_cap(
        _candidate(
            month_searches=900, month_tool_searches=100, month_hotel_searches=80
        ),
        config,
    )
    flights = render_cap(
        _candidate(
            month_searches=900, month_tool_searches=100, month_hotel_searches=20
        ),
        config,
    )
    tied = render_cap(
        _candidate(
            month_searches=900, month_tool_searches=100, month_hotel_searches=50
        ),
        config,
    )

    assert "https://hotels.flightpowers.com/connect" in hotels.text
    assert "2,000" in hotels.text
    assert "https://flights.flightpowers.com/connect" in flights.text
    assert "2,500" in flights.text
    # A tie goes to flights: the larger listing, and the one every printed
    # guide already points at.
    assert "https://flights.flightpowers.com/connect" in tied.text
    for note in (hotels, flights, tied):
        assert "flightpowers.com/mcp" not in note.text


def test_the_greeting_falls_back_to_the_reviewed_copy():
    """"Hi," is the designed default, not a degraded one.

    We store no name, so the only source is the local part of the address,
    and it is a bad one. Everything that is not obviously a given name gets
    the plain greeting.
    """
    assert first_name_from_email("alex@example.com") == " Alex"
    assert first_name_from_email("a.long.name@example.com") == ""
    assert first_name_from_email("alex99@example.com") == ""
    assert first_name_from_email("noreply@example.com") == ""
    assert first_name_from_email("jo@example.com") == ""
    assert first_name_from_email("") == ""

    plain = render_day2(_candidate(email="no-reply@example.com"), _config())
    assert "Hi," in plain.text
    assert "Hi ," not in plain.text


def test_a_dry_run_prints_masked_addresses():
    """A dry run is pasted into Slack and committed into session notes."""
    assert mask_email("alexander@example.com") == "ale***@example.com"
    assert mask_email("not-an-address") == ""


# ── configuration ────────────────────────────────────────────────────────


def test_the_flags_default_off(monkeypatch):
    for name in ("FREE_SIGNIN_DAY2", "FREE_SIGNIN_CAPNOTE", "FREE_SIGNIN_WELCOME"):
        monkeypatch.delenv(name, raising=False)
    assert load_mail_config(ORIGIN).day2 is False

    monkeypatch.setenv("FREE_SIGNIN_DAY2", "on")
    assert load_mail_config(ORIGIN).day2 is True
    # And nothing else came on with it.
    assert load_mail_config(ORIGIN).cap_note is False


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused_rather_than_guessed():
    body = await run_sequence(MemoryFreeUserStore(), _config(), kind="monthly")
    assert body["reason"] == "unknown_kind"
    assert body["eligible"] == 0


@pytest.mark.asyncio
async def test_a_store_that_is_down_never_raises():
    """This runs on a cron. A 500 here is a page nobody can act on."""

    class _Broken(MemoryFreeUserStore):
        async def day2_candidates(self, *args, **kwargs):
            raise RuntimeError("neon is gone")

    body = await run_sequence(_Broken(), _config(), kind=KIND_DAY2)
    assert body["reason"] == "store_unavailable"
    assert body["sent"] == 0


# ── the route ────────────────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch):
    """The real server app, as Vercel builds it."""
    from src.server import build_server
    from src.settings import load_settings

    monkeypatch.setenv("METRICS_TOKEN", "sekret")
    server = build_server(load_settings())
    return server.http_app(stateless_http=True)


class TestTheRoute:
    """`POST /email/run-sequence`. Who may call it, and what it accepts.

    The gate is stricter than `/metrics`: that route runs open when
    `METRICS_TOKEN` is unset, which is a reasonable default for a counter
    and a terrible one for a job that mails people. Here, no token
    configured means nobody gets in.
    """

    @pytest.mark.asyncio
    async def test_no_token_no_entry(self, app):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(
                transport=transport, base_url="http://testserver"
            ) as client:
                naked = await client.post("/email/run-sequence", json={})
                wrong = await client.post(
                    "/email/run-sequence",
                    json={},
                    headers={"x-metrics-token": "nope"},
                )
        assert naked.status_code == 401
        assert wrong.status_code == 401

    @pytest.mark.asyncio
    async def test_an_unconfigured_token_locks_the_route(self, app, monkeypatch):
        monkeypatch.delenv("METRICS_TOKEN", raising=False)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post("/email/run-sequence", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_bad_kind_is_a_400_and_an_empty_body_is_a_dry_run(self, app):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(
                transport=transport, base_url="http://testserver"
            ) as client:
                bad = await client.post(
                    "/email/run-sequence",
                    json={"kind": "monthly"},
                    headers={"x-metrics-token": "sekret"},
                )
                empty = await client.post(
                    "/email/run-sequence",
                    content=b"",
                    headers={"x-metrics-token": "sekret"},
                )
        assert bad.status_code == 400
        # No DATABASE_URL in the test environment, so there is no list to
        # read -- and the route says so instead of answering "0 eligible",
        # which is what a store outage would otherwise look like.
        assert empty.status_code == 503
        assert empty.json()["reason"] == "no_user_store"
        assert empty.json()["dry_run"] is True
