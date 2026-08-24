"""Telemetry: the spend guard, the Lambda request count, and durability."""

import json
import time

import pytest

from src.stores import MemoryCounterStore
from src.telemetry import STDOUT_PREFIX, CallRecord, Telemetry


def _record(**overrides) -> CallRecord:
    base = dict(
        timestamp=time.time(),
        tool="search_oneway_flights",
        tier="llm_host",
        client_name="claude-ai",
        source_ip="160.79.104.10",
        widget_capable=True,
        requested_combinations=31,
        backend_calls=15,
        backend_failures=0,
        results_returned=10,
        duration_ms=1200,
        truncated=True,
        allowed=True,
        decision_reason="verified LLM-host egress",
        ad_eligible=True,
    )
    base.update(overrides)
    return CallRecord(**base)


class TestStdoutLogging:
    @pytest.mark.asyncio
    async def test_emits_one_prefixed_json_line_per_call(self, capsys):
        # stdout is the only sink that works on every target, so it is the
        # record of truth on Vercel where the filesystem is read-only.
        t = Telemetry()
        await t.record(_record())

        out = capsys.readouterr().out.strip()
        assert out.startswith(STDOUT_PREFIX)
        payload = json.loads(out[len(STDOUT_PREFIX):])
        assert payload["tool"] == "search_oneway_flights"
        assert payload["backend_calls"] == 15
        assert payload["tier"] == "llm_host"

    @pytest.mark.asyncio
    async def test_one_line_per_tool_call_not_per_backend_call(self, capsys):
        # Vercel caps runtime logs at 256 lines per request.
        t = Telemetry()
        await t.record(_record(backend_calls=15))
        lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_backend_calls_is_the_lambda_request_count(self, capsys):
        t = Telemetry()
        await t.record(_record(backend_calls=15))
        await t.record(_record(backend_calls=3))
        total = sum(
            json.loads(l[len(STDOUT_PREFIX):])["backend_calls"]
            for l in capsys.readouterr().out.splitlines()
            if l.startswith(STDOUT_PREFIX)
        )
        assert total == 18
        assert (await t.snapshot())["totals"]["backend_calls"] == 18


class TestFileSink:
    @pytest.mark.asyncio
    async def test_writes_to_file_when_writable(self, tmp_path):
        log = tmp_path / "calls.jsonl"
        t = Telemetry(log_path=str(log))
        assert t.file_sink_enabled
        await t.record(_record())
        assert len(log.read_text().strip().splitlines()) == 1

    def test_disables_itself_when_the_path_is_not_writable(self):
        # Vercel's filesystem is read-only outside /tmp. Appending into a
        # void must not look like logging.
        t = Telemetry(log_path="/proc/definitely/not/writable/calls.jsonl")
        assert not t.file_sink_enabled

    def test_no_file_sink_by_default(self):
        assert not Telemetry().file_sink_enabled


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_reports_backend_calls_per_tool_call(self):
        t = Telemetry()
        await t.record(_record(backend_calls=15))
        await t.record(_record(backend_calls=5))

        snap = await t.snapshot()
        assert snap["totals"]["tool_calls"] == 2
        assert snap["totals"]["backend_calls"] == 20
        assert snap["backend_calls_per_tool_call"] == 10.0

    @pytest.mark.asyncio
    async def test_breaks_down_by_tier(self):
        t = Telemetry()
        await t.record(_record(tier="llm_host", backend_calls=10))
        await t.record(_record(tier="unknown", backend_calls=4, ad_eligible=False))

        by_tier = (await t.snapshot())["by_tier"]
        assert by_tier["llm_host"]["tool_calls"] == 1
        assert by_tier["llm_host"]["ad_eligible_calls"] == 1
        assert by_tier["unknown"]["backend_calls"] == 4
        assert by_tier["unknown"].get("ad_eligible_calls", 0) == 0

    @pytest.mark.asyncio
    async def test_says_plainly_that_it_cannot_see_renders(self):
        notes = " ".join((await Telemetry().snapshot())["notes"])
        assert "NOT rendered" in notes

    @pytest.mark.asyncio
    async def test_warns_loudly_when_counters_are_not_durable(self):
        # The failure this prevents: believing a spend guard is armed on
        # serverless when per-instance counters make it unenforceable.
        snap = await Telemetry(daily_budget=100).snapshot()
        assert snap["durable_counters"] is False
        assert any("NOT DURABLE" in n for n in snap["notes"])
        assert snap["budget"]["enforceable"] is False


class TestBudget:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        t = Telemetry()
        assert (await t.budget_state())["enabled"] is False
        assert await t.cap_for_budget(15) == (15, None)

    @pytest.mark.asyncio
    async def test_full_cap_below_the_degrade_threshold(self):
        t = Telemetry(daily_budget=100, degrade_at=0.8)
        await t.record(_record(backend_calls=10))
        cap, note = await t.cap_for_budget(15)
        assert cap == 15 and note is None

    @pytest.mark.asyncio
    async def test_degrades_past_the_threshold(self):
        t = Telemetry(daily_budget=100, degrade_at=0.8)
        await t.record(_record(backend_calls=85))
        cap, note = await t.cap_for_budget(15)
        assert cap == 5
        assert "protect the budget" in note

    @pytest.mark.asyncio
    async def test_drops_to_one_when_exhausted(self):
        t = Telemetry(daily_budget=100, degrade_at=0.8)
        await t.record(_record(backend_calls=120))
        cap, note = await t.cap_for_budget(15)
        # Degrade, never refuse: a hard stop reads as an outage all day.
        assert cap == 1
        assert "spent" in note
        assert (await t.budget_state())["exhausted"] is True

    @pytest.mark.asyncio
    async def test_old_calls_fall_out_of_the_window(self):
        t = Telemetry(daily_budget=100, degrade_at=0.8)
        await t.record(_record(timestamp=time.time() - 90000, backend_calls=95))
        assert await t.backend_calls_in_window() == 0
        assert await t.cap_for_budget(15) == (15, None)


class TestRestartSafety:
    @pytest.mark.asyncio
    async def test_budget_survives_a_restart_with_a_file_sink(self, tmp_path):
        log = tmp_path / "c.jsonl"
        first = Telemetry(log_path=str(log), daily_budget=100, degrade_at=0.8)
        await first.record(_record(backend_calls=90))

        # A restart must not hand the process a fresh budget -- otherwise the
        # spend guard is defeated by crash-looping.
        second = Telemetry(log_path=str(log), daily_budget=100, degrade_at=0.8)
        assert await second.backend_calls_in_window() == 90
        assert (await second.cap_for_budget(15))[0] == 5

    @pytest.mark.asyncio
    async def test_survives_a_corrupt_log_line(self, tmp_path):
        log = tmp_path / "c.jsonl"
        await Telemetry(log_path=str(log)).record(_record())
        with open(log, "a") as handle:
            handle.write("not json at all\n")
        assert (await Telemetry(log_path=str(log)).snapshot())["totals"][
            "tool_calls"
        ] == 1

    @pytest.mark.asyncio
    async def test_does_not_replay_into_a_durable_store(self, tmp_path):
        """Replaying into a shared store would double-count on every restart."""

        class FakeDurable(MemoryCounterStore):
            durable = True

        log = tmp_path / "c.jsonl"
        await Telemetry(log_path=str(log)).record(_record(backend_calls=7))

        revived = Telemetry(store=FakeDurable(), log_path=str(log))
        assert (await revived.snapshot())["totals"] == {}
