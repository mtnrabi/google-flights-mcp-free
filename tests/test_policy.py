"""Caller classification, enforcement decisions, and the fail-open rules."""

import pytest

from src.policy import (
    TIER_LLM_HOST,
    TIER_LOCAL_CLIENT,
    TIER_UNKNOWN,
    ClientClassifier,
    decide,
    extract_source_ip,
)


@pytest.fixture
def classifier() -> ClientClassifier:
    return ClientClassifier()


class TestClassification:
    def test_anthropic_egress_is_an_llm_host(self, classifier):
        # 160.79.104.0/21 -- published as the range used for MCP tool calls.
        assert classifier.classify("160.79.104.10", None) == TIER_LLM_HOST
        assert classifier.classify("160.79.111.255", None) == TIER_LLM_HOST

    def test_just_outside_the_anthropic_range_is_not(self, classifier):
        assert classifier.classify("160.79.112.0", None) != TIER_LLM_HOST
        assert classifier.classify("160.79.103.255", None) != TIER_LLM_HOST

    def test_random_ip_is_unknown(self, classifier):
        assert classifier.classify("8.8.8.8", None) == TIER_UNKNOWN

    def test_local_client_recognised_by_name_only(self, classifier):
        assert classifier.classify("8.8.8.8", "claude-code") == TIER_LOCAL_CLIENT
        assert classifier.classify("8.8.8.8", "Cursor") == TIER_LOCAL_CLIENT

    def test_ip_beats_a_spoofable_name(self, classifier):
        # A caller from Anthropic's range claiming to be Claude Code is still
        # classified by the thing that cannot be forged.
        assert classifier.classify("160.79.104.10", "claude-code") == TIER_LLM_HOST

    def test_claiming_to_be_claude_earns_nothing(self, classifier):
        # The whole point: clientInfo is self-reported, so a name alone must
        # never promote a caller into the paying tier.
        assert classifier.classify("8.8.8.8", "claude-ai") == TIER_UNKNOWN

    def test_garbage_ip_does_not_crash(self, classifier):
        assert classifier.classify("not-an-ip", None) == TIER_UNKNOWN
        assert classifier.classify(None, None) == TIER_UNKNOWN


class TestDecide:
    def test_off_serves_everyone_at_full_cap(self):
        d = decide(TIER_UNKNOWN, mode="off", blocked_tiers=frozenset(),
                   full_cap=15, openai_ranges_loaded=True)
        assert d.allowed and d.cap == 15

    def test_monitor_serves_everyone_but_still_classifies(self):
        d = decide(TIER_UNKNOWN, mode="monitor", blocked_tiers=frozenset({"unknown"}),
                   full_cap=15, openai_ranges_loaded=True)
        assert d.allowed and d.cap == 15
        assert d.tier == TIER_UNKNOWN

    def test_enforce_blocks_a_blocked_tier(self):
        d = decide(TIER_LOCAL_CLIENT, mode="enforce",
                   blocked_tiers=frozenset({"local_client"}),
                   full_cap=15, openai_ranges_loaded=True)
        assert not d.allowed and d.cap == 0

    def test_enforce_gives_llm_hosts_the_full_cap(self):
        d = decide(TIER_LLM_HOST, mode="enforce", blocked_tiers=frozenset(),
                   full_cap=15, openai_ranges_loaded=True)
        assert d.allowed and d.cap == 15
        assert d.widget_capable

    def test_enforce_reduces_fan_out_for_non_rendering_tiers(self):
        d = decide(TIER_UNKNOWN, mode="enforce", blocked_tiers=frozenset(),
                   full_cap=15, openai_ranges_loaded=True)
        assert d.allowed
        assert d.cap == 5
        assert not d.widget_capable

    def test_fails_open_on_unknown_when_openai_ranges_missing(self):
        # Without the feed, every real ChatGPT caller looks "unknown". Blocking
        # on that classification would take out a whole host.
        d = decide(TIER_UNKNOWN, mode="enforce",
                   blocked_tiers=frozenset({"unknown"}),
                   full_cap=15, openai_ranges_loaded=False)
        assert d.allowed, "must not block on a classification we could not make"
        assert "failing open" in d.reason

    def test_still_blocks_local_clients_without_the_feed(self):
        # local_client is name-derived, so a missing OpenAI feed is irrelevant.
        d = decide(TIER_LOCAL_CLIENT, mode="enforce",
                   blocked_tiers=frozenset({"local_client"}),
                   full_cap=15, openai_ranges_loaded=False)
        assert not d.allowed

    def test_only_llm_hosts_are_widget_capable(self):
        for tier in (TIER_LOCAL_CLIENT, TIER_UNKNOWN):
            d = decide(tier, mode="monitor", blocked_tiers=frozenset(),
                       full_cap=15, openai_ranges_loaded=True)
            assert not d.widget_capable


class TestSourceIp:
    def test_prefers_rightmost_forwarded_for(self):
        # The edge appends the real client on the right; the left entry is
        # whatever the inbound request already carried.
        headers = {"x-forwarded-for": "10.0.0.1, 160.79.104.10"}
        assert extract_source_ip(headers, "10.0.0.1") == "160.79.104.10"

    def test_leftmost_forwarded_for_is_not_trusted(self):
        # Vercel-shaped header set: a caller types a spoofed leftmost hop
        # naming an address inside Anthropic's published egress range, and
        # the edge appends the true client after it. Trusting the leftmost
        # entry (the old behaviour) would classify the spoofing caller as
        # TIER_LLM_HOST; trusting the rightmost, edge-appended entry
        # classifies the true, unprivileged client instead.
        headers = {"x-forwarded-for": "160.79.104.1, 8.8.8.8"}
        assert extract_source_ip(headers, "8.8.8.8") == "8.8.8.8"

    def test_falls_back_to_real_ip(self):
        assert extract_source_ip({"x-real-ip": "1.2.3.4"}, None) == "1.2.3.4"

    def test_falls_back_to_peer(self):
        assert extract_source_ip({}, "5.6.7.8") == "5.6.7.8"

    def test_none_when_nothing_available(self):
        assert extract_source_ip({}, None) is None


class TestSourceIpCannotBeSpoofedIntoLlmHost:
    """A caller who forges the leftmost X-Forwarded-For hop must not land in
    TIER_LLM_HOST. Regression test for the finding in
    state/gtm/fair-use-gateway-key-2026-09-05.md: extract_source_ip used to
    trust the leftmost (caller-supplied) entry, so sending
    `x-forwarded-for: 160.79.104.1` alone was enough to self-declare into the
    ad-eligible, full-cap tier.
    """

    def test_spoofed_leftmost_hop_does_not_classify_as_llm_host(self, classifier):
        # Vercel-shaped: caller-typed leftmost hop naming an address inside
        # Anthropic's published range, true client appended by the edge.
        headers = {"x-forwarded-for": "160.79.104.1, 8.8.8.8"}
        source_ip = extract_source_ip(headers, "8.8.8.8")
        assert classifier.classify(source_ip, None) != TIER_LLM_HOST

    def test_real_anthropic_egress_still_classifies_llm_host(self, classifier):
        # Legitimate Vercel-shaped traffic: the edge appends the real
        # Anthropic-range address as the rightmost hop. Must still classify
        # as TIER_LLM_HOST -- this fix must not break real traffic.
        headers = {"x-forwarded-for": "203.0.113.5, 160.79.104.10"}
        source_ip = extract_source_ip(headers, "160.79.104.10")
        assert classifier.classify(source_ip, None) == TIER_LLM_HOST


class TestFeedLoadScheduling:
    """The first load attempt must never be suppressed by its own cooldown."""

    @pytest.mark.asyncio
    async def test_first_attempt_happens_even_when_monotonic_is_small(
        self, classifier, monkeypatch
    ):
        # Regression: `_last_attempt` used to init to 0.0, and time.monotonic()
        # is seconds-since-boot. On a fresh serverless microVM that is a small
        # number, so `now - 0.0 < RETRY_COOLDOWN_SECONDS` skipped the very
        # first fetch forever -- and every ChatGPT caller silently classified
        # as `unknown`. Locally monotonic is days, so this passed in dev and
        # failed only once deployed.
        monkeypatch.setattr("src.policy.time.monotonic", lambda: 3.0)
        called = {"n": 0}

        async def fake_refresh(timeout: float = 10.0) -> int:
            called["n"] += 1
            return 0

        monkeypatch.setattr(classifier, "refresh_openai_ranges", fake_refresh)
        await classifier.ensure_openai_ranges()
        assert called["n"] == 1, "first attempt must not be gated by the cooldown"

    @pytest.mark.asyncio
    async def test_second_attempt_is_suppressed_by_the_cooldown(
        self, classifier, monkeypatch
    ):
        monkeypatch.setattr("src.policy.time.monotonic", lambda: 3.0)
        called = {"n": 0}

        async def fake_refresh(timeout: float = 10.0) -> int:
            called["n"] += 1
            return 0

        monkeypatch.setattr(classifier, "refresh_openai_ranges", fake_refresh)
        await classifier.ensure_openai_ranges()
        await classifier.ensure_openai_ranges()
        assert called["n"] == 1, "a failing feed must not be refetched every request"

    @pytest.mark.asyncio
    async def test_no_refetch_once_loaded(self, classifier, monkeypatch):
        called = {"n": 0}

        async def fake_refresh(timeout: float = 10.0) -> int:
            called["n"] += 1
            return 0

        monkeypatch.setattr(classifier, "refresh_openai_ranges", fake_refresh)
        classifier._openai_loaded = True
        await classifier.ensure_openai_ranges()
        assert called["n"] == 0

    def test_error_is_exposed_for_diagnosis(self, classifier):
        assert classifier.openai_ranges_error is None
