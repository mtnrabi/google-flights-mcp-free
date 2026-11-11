"""`GET /metrics/signins` -- the sign-in list, as numbers, behind a token.

Why this file exists. Since 2026-09-09 the free server has required a Google
sign-in on every call, and the 2026-09-11 daily read could not answer the
first question about it: how many people signed in, and did any of them
search? `/metrics/signins`, `/metrics/users`, `/metrics/free-users` and
`/metrics/signin` all returned 404, so a collapse in traffic and an empty
sign-in list looked identical from outside.

What this pins, and why each one is here rather than assumed:

1. **No address leaves the server.** The body is pasted into a daily read
   file that is committed and sometimes into Slack. One character each side
   of the local part; no Google `sub`, no unsubscribe token, ever.
2. **No token, no entry -- including when none is configured.** `/metrics`
   may run open because a counter is not a person. A list of who signed up
   is, so this route follows `/email/run-sequence` and refuses.
3. **"Nobody signed in" and "we cannot see who signed in" are different
   answers.** Both would otherwise render as a page of zeros, and the day-2
   note is gated on exactly this distinction.
4. **The window is honest about its resolution.** Sign-ins are timestamps
   and counted exactly; per-user usage lives in UTC day rows and is counted
   in whole days, and the body says so.
5. **Usage is reported from two systems.** The per-user counters and the
   call counters are written by different paths; the route shows both,
   because the day-2 note must not fire on one system's say-so.

    python -m pytest mcp_server/tests/test_metrics_signins.py -q
"""

from __future__ import annotations

import time

import httpx
import pytest

from src.freeusers import (
    DAY2_KIND,
    MemoryFreeUserStore,
    SigninRecord,
    window_days,
)
from src.signinstats import RECENT_LIMIT, bucket_for, build_stats, mask_email

DAY = 86400.0
HOUR = 3600.0
#: A fixed noon so a "last 24 hours" window always straddles two UTC days,
#: which is the case the day-row rounding has to be honest about.
NOON = 1757592000.0  # 2025-09-11T12:00:00Z


def _record(sub: str, **kw) -> SigninRecord:
    base = dict(
        google_sub=sub,
        email=f"{sub}@gmail.com",
        first_seen=NOON - DAY,
        last_seen=NOON,
    )
    base.update(kw)
    return SigninRecord(**base)


# ── masking ──────────────────────────────────────────────────────────────


class TestTheAddressNeverLeaves:
    def test_one_character_each_side(self):
        assert mask_email("raphael@gmail.com") == "r***l@gmail.com"

    def test_a_two_character_local_still_shows_both_ends(self):
        # Not a bug: "jo" is already public-ish, and padding it to look
        # longer would make the mask lie about the address.
        assert mask_email("jo@fastmail.com") == "j***o@fastmail.com"

    def test_a_single_character_local_is_not_doubled(self):
        assert mask_email("r@gmail.com") == "r***@gmail.com"

    def test_no_address_is_empty_not_a_fake_shape(self):
        # An unverified Google email arrives as "". Rendering it as
        # "***@***" would make an empty row look like a real one.
        assert mask_email("") == ""
        assert mask_email("not-an-address") == ""
        assert mask_email("@gmail.com") == ""

    def test_it_reveals_less_than_the_send_list_mask(self):
        from src.sequence import mask_email as send_mask

        assert send_mask("raphael@gmail.com") == "rap***@gmail.com"
        assert mask_email("raphael@gmail.com") != send_mask("raphael@gmail.com")


# ── buckets ──────────────────────────────────────────────────────────────


class TestTheBuckets:
    @pytest.mark.parametrize(
        "calls,label",
        [
            (0, "0"),
            (1, "1-9"),
            (9, "1-9"),
            (10, "10-49"),
            (49, "10-49"),
            # 50 is the free server's daily cap: this bucket is "spent the
            # whole allowance", which is the paid-server conversation.
            (50, "50+"),
            (149, "50+"),
            (150, "50+"),
            (4000, "50+"),
        ],
    )
    def test_every_boundary(self, calls, label):
        assert bucket_for(calls) == label

    def test_a_bucket_exists_for_every_account(self):
        rows = [_record(f"u{i}", calls_window=i) for i in (0, 5, 20, 90)]
        body = build_stats(rows, hours=24, now=NOON)
        assert sum(body["distribution"].values()) == len(rows)
        assert body["distribution"] == {
            "0": 1,
            "1-9": 1,
            "10-49": 1,
            "50+": 1,
        }


# ── the body ─────────────────────────────────────────────────────────────


class TestTheTotals:
    def test_all_time_activity_is_the_day2_question(self):
        rows = [
            # Signed in, never searched -- the day-2 note's whole audience.
            _record("cold", calls_total=0, calls_window=0),
            # Searched once long ago, nothing in the window. Still counts as
            # "has used it", which is what exits them from the flow forever.
            _record("warm", calls_total=7, calls_window=0),
            _record("hot", calls_total=300, calls_window=42),
        ]
        totals = build_stats(rows, hours=24, now=NOON)["totals"]
        assert totals["users"] == 3
        assert totals["users_with_calls"] == 2
        assert totals["users_zero_calls"] == 1

    def test_the_send_and_opt_out_counters(self):
        rows = [
            _record("a", welcome_sent_at=NOON - DAY, opted_out=True),
            _record("b", welcome_sent_at=NOON - DAY, day2_sent_at=NOON),
            _record("c", cap_note_sent_at=NOON),
            _record("d"),
        ]
        totals = build_stats(rows, hours=24, now=NOON)["totals"]
        assert totals["opted_out"] == 1
        assert totals["welcome_sent"] == 2
        assert totals["day2_sent"] == 1
        assert totals["cap_sent"] == 1


class TestTheWindow:
    def test_new_signins_are_counted_exactly_not_by_day(self):
        rows = [
            _record("inside", first_seen=NOON - 2 * HOUR),
            _record("edge", first_seen=NOON - 23 * HOUR),
            # Yesterday lunchtime: inside the two UTC days the usage rows
            # cover, outside the 24 hours asked for. A day-rounded count
            # would wrongly call this a new sign-in.
            _record("outside", first_seen=NOON - 30 * HOUR),
        ]
        window = build_stats(rows, hours=24, now=NOON)["window"]
        assert window["new_signins"] == 2
        assert window["days_read"] == 2

    def test_activity_and_cap_hits_come_from_the_rows(self):
        rows = [
            _record("a", calls_window=12, cap_hits_window=1),
            _record("b", calls_window=0, cap_hits_window=0),
            _record("c", calls_window=3),
        ]
        window = build_stats(rows, hours=24, now=NOON)["window"]
        assert window["active_users"] == 2
        assert window["user_searches"] == 15
        assert window["cap_hits"] == 1

    def test_the_call_counters_are_reported_beside_them_not_merged(self):
        """The independent system. If these disagree with the rows above,
        that disagreement is the finding -- so they get their own keys."""
        body = build_stats(
            [_record("a", calls_window=0)],
            hours=24,
            now=NOON,
            calls={"totals": {"tool_calls": 9, "backend_calls": 31}},
        )
        assert body["window"]["tool_calls"] == 9
        assert body["window"]["backend_calls"] == 31
        assert body["window"]["user_searches"] == 0
        assert "calls_source" not in body["window"]

    def test_a_counter_outage_is_named_not_reported_as_zero(self):
        window = build_stats([_record("a")], hours=24, now=NOON, calls=None)["window"]
        assert window["calls_source"] == "unavailable"

    def test_the_day_span_is_calendar_days_not_hours_over_24(self):
        """Asked at noon, "the last 24 hours" touches two UTC days. A naive
        hours/24 would read only today and lose this morning, which turns an
        active account into a day-2 note candidate."""
        assert window_days(1, NOON) == 1
        assert window_days(24, NOON) == 2
        assert window_days(168, NOON) == 8
        midnight = NOON - 12 * HOUR  # 00:00Z exactly
        assert window_days(24, midnight) == 2
        assert window_days(1, midnight) == 2


class TestTheRecentList:
    def test_newest_first_and_capped(self):
        rows = [_record(f"u{i}", first_seen=NOON - i * HOUR) for i in range(30)]
        recent = build_stats(rows, hours=24, now=NOON)["recent"]
        assert len(recent) == RECENT_LIMIT
        assert recent[0]["email"] == "u***0@gmail.com"
        assert [r["first_seen"] for r in recent] == sorted(
            [r["first_seen"] for r in recent], reverse=True
        )

    def test_the_row_carries_both_call_numbers_and_every_send_stamp(self):
        rows = [
            _record(
                "raphael",
                calls_total=90,
                calls_window=12,
                welcome_sent_at=NOON - DAY,
                day2_sent_at=NOON - HOUR,
                cap_note_sent_at=None,
                opted_out=False,
                client_kind="Claude Code",
            )
        ]
        row = build_stats(rows, hours=24, now=NOON)["recent"][0]
        assert row["email"] == "r***l@gmail.com"
        assert row["calls_total"] == 90
        assert row["calls_window"] == 12
        assert row["welcome_sent_at"].endswith("Z")
        assert row["day2_sent_at"].endswith("Z")
        assert row["cap_note_sent_at"] is None
        assert row["opted_out"] is False
        assert row["client_kind"] == "Claude Code"

    def test_no_identifier_survives_into_the_body(self):
        rows = [_record("sub-12345", email="raphael@gmail.com")]
        text = repr(build_stats(rows, hours=24, now=NOON))
        assert "sub-12345" not in text
        assert "raphael@gmail.com" not in text


class TestAnEmptyStore:
    def test_zeros_everywhere_and_no_crash(self):
        body = build_stats([], hours=24, now=NOON)
        assert body["totals"] == {
            "users": 0,
            "opted_out": 0,
            "welcome_sent": 0,
            "day2_sent": 0,
            "cap_sent": 0,
            "users_with_calls": 0,
            "users_zero_calls": 0,
            "sent_by_kind": {
                "welcome": 0,
                "day2": 0,
                "cap": 0,
                "day7": 0,
                "dormant14": 0,
            },
            "welcome_failed": 0,
            "welcome_unconfirmed": 0,
        }
        assert body["window"]["new_signins"] == 0
        assert body["recent"] == []
        assert body["distribution"]["0"] == 0
        assert "truncated" not in body

    def test_a_clipped_read_says_so(self):
        rows = [_record(f"u{i}") for i in range(3)]
        body = build_stats(rows, hours=24, now=NOON, limit=3)
        assert body["truncated"] is True
        assert any("newest" in note for note in body["notes"])


# ── the store read ───────────────────────────────────────────────────────


class TestTheMemoryStore:
    """The fake store is the one the tests drive, and it has to behave the
    way the SQL does or these tests pin nothing."""

    @pytest.mark.asyncio
    async def test_it_joins_days_cap_hits_and_the_day2_ledger(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-a", "raphael@gmail.com", now=NOON - 3 * DAY)
        await store.touch("sub-a", spent=4, now=NOON - 2 * HOUR, tool="flights")
        await store.note_cap_hit("sub-a", now=NOON - HOUR)
        await store.claim_sent_email("sub-a", DAY2_KIND, now=NOON - HOUR)

        rows = await store.signin_records(now=NOON, hours=24)
        assert len(rows) == 1
        row = rows[0]
        assert row.email == "raphael@gmail.com"
        assert row.calls_total == 4
        assert row.calls_window == 4
        assert row.cap_hits_window == 1
        assert row.day2_sent_at == pytest.approx(NOON - HOUR)

    @pytest.mark.asyncio
    async def test_usage_outside_the_window_stays_out_of_it(self):
        store = MemoryFreeUserStore()
        await store.upsert("sub-a", "old@gmail.com", now=NOON - 30 * DAY)
        await store.touch("sub-a", spent=5, now=NOON - 10 * DAY)

        narrow = (await store.signin_records(now=NOON, hours=24))[0]
        wide = (await store.signin_records(now=NOON, hours=24 * 30))[0]
        # All-time never moves; the window does. Both numbers matter: the
        # first is "has ever searched", the second is "is searching now".
        assert narrow.calls_total == wide.calls_total == 5
        assert narrow.calls_window == 0
        assert wide.calls_window == 5

    @pytest.mark.asyncio
    async def test_newest_first_so_a_clipped_read_keeps_the_new_accounts(self):
        store = MemoryFreeUserStore()
        for i in range(5):
            await store.upsert(f"sub-{i}", f"u{i}@gmail.com", now=NOON - i * DAY)
        rows = await store.signin_records(now=NOON, hours=24, limit=2)
        assert [r.google_sub for r in rows] == ["sub-0", "sub-1"]


# ── the route ────────────────────────────────────────────────────────────


_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def app(monkeypatch):
    """The real server app, with sign-in wired and a fake user store.

    `DATABASE_URL` is what switches sign-in on; the store behind it is then
    swapped for the in-memory one, so the route is exercised end to end
    without a database.
    """
    import dataclasses

    import src.server as server_mod
    from src.settings import load_settings

    monkeypatch.setenv("METRICS_TOKEN", "sekret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.test/neon")
    monkeypatch.setenv("ADS_ENABLED", "false")

    store = MemoryFreeUserStore()
    real = server_mod.build_free_oauth

    def with_memory_users(*args, **kwargs):
        support = real(*args, **kwargs)
        return support if support is None else dataclasses.replace(
            support, users=store
        )

    monkeypatch.setattr(server_mod, "build_free_oauth", with_memory_users)
    server = server_mod.build_server(load_settings())
    return server.http_app(stateless_http=True), store


async def _get(app, path, headers=None):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with _REAL_ASYNC_CLIENT(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path, headers=headers or {})


class TestWhoMayCall:
    @pytest.mark.asyncio
    async def test_no_token_and_a_wrong_token_are_both_401(self, app):
        http, _store = app
        naked = await _get(http, "/metrics/signins")
        wrong = await _get(
            http, "/metrics/signins", {"x-metrics-token": "nope"}
        )
        assert naked.status_code == 401
        assert wrong.status_code == 401

    @pytest.mark.asyncio
    async def test_an_unconfigured_token_locks_the_route(self, app, monkeypatch):
        """Unlike `/metrics`. A counter may run open; a list of who signed
        up may not."""
        http, _store = app
        monkeypatch.delenv("METRICS_TOKEN", raising=False)
        response = await _get(
            http, "/metrics/signins", {"x-metrics-token": "sekret"}
        )
        assert response.status_code == 401


class TestWhatItAnswers:
    @pytest.mark.asyncio
    async def test_a_real_read(self, app):
        http, store = app
        now = time.time()
        await store.upsert("sub-a", "raphael@gmail.com", now=now - 3 * DAY)
        await store.touch("sub-a", spent=6, now=now - HOUR, tool="flights")
        await store.upsert("sub-b", "dana@gmail.com", now=now - 2 * HOUR)

        response = await _get(
            http, "/metrics/signins?hours=24", {"x-metrics-token": "sekret"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["hours"] == 24
        assert body["totals"]["users"] == 2
        assert body["totals"]["users_with_calls"] == 1
        assert body["totals"]["users_zero_calls"] == 1
        assert body["window"]["new_signins"] == 1
        assert body["window"]["active_users"] == 1
        assert body["distribution"]["1-9"] == 1
        assert body["distribution"]["0"] == 1
        # Newest first, and masked.
        assert [r["email"] for r in body["recent"]] == [
            "d***a@gmail.com",
            "r***l@gmail.com",
        ]
        assert "raphael@gmail.com" not in response.text
        assert "sub-a" not in response.text

    @pytest.mark.asyncio
    async def test_an_empty_store_is_zeros_and_a_200(self, app):
        http, _store = app
        response = await _get(
            http, "/metrics/signins", {"x-metrics-token": "sekret"}
        )
        assert response.status_code == 200
        assert response.json()["totals"]["users"] == 0
        assert response.json()["recent"] == []

    @pytest.mark.asyncio
    async def test_hours_must_be_a_positive_integer(self, app):
        http, _store = app
        bad = await _get(
            http, "/metrics/signins?hours=soon", {"x-metrics-token": "sekret"}
        )
        zero = await _get(
            http, "/metrics/signins?hours=0", {"x-metrics-token": "sekret"}
        )
        assert bad.status_code == 400
        assert zero.status_code == 400


class TestWhenItCannotSee:
    @pytest.mark.asyncio
    async def test_no_store_configured_is_503_with_a_reason(self, monkeypatch):
        """"Nobody signed in" and "we cannot see who signed in" must never
        render as the same page of zeros: the day-2 note is gated on it."""
        from src.server import build_server
        from src.settings import load_settings

        monkeypatch.setenv("METRICS_TOKEN", "sekret")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        server = build_server(load_settings())
        http = server.http_app(stateless_http=True)
        response = await _get(
            http, "/metrics/signins", {"x-metrics-token": "sekret"}
        )
        assert response.status_code == 503
        assert response.json()["reason"] == "no_user_store"

    @pytest.mark.asyncio
    async def test_a_store_that_raises_is_503_not_500(self, app):
        http, store = app

        async def boom(*args, **kwargs):
            raise RuntimeError("neon is gone")

        store.signin_records = boom
        response = await _get(
            http, "/metrics/signins", {"x-metrics-token": "sekret"}
        )
        assert response.status_code == 503
        assert response.json()["reason"] == "store_unavailable"
