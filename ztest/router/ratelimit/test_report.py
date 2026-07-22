"""format_rate_limits / format_snapshot rendering."""

from __future__ import annotations

from mote.router.ratelimit import RateLimitSnapshot, RateLimitTracker, format_rate_limits, format_snapshot


class TestFormatSnapshot:
    def test_openai_line_with_seconds_reset(self):
        snap = RateLimitSnapshot(
            provider="openai",
            model="gpt-4",
            limit_requests=5000,
            remaining_requests=4999,
            reset_requests_seconds=360.0,
            limit_tokens=160000,
            remaining_tokens=159000,
            reset_tokens_seconds=1.0,
        )
        line = format_snapshot(snap)
        assert "openai/gpt-4:" in line
        assert "requests 4999/5000" in line
        assert "resets in 360s" in line
        assert "tokens 159000/160000" in line

    def test_anthropic_line_with_raw_reset(self):
        snap = RateLimitSnapshot(
            provider="anthropic",
            model="claude",
            limit_requests=50,
            remaining_requests=49,
            reset_requests_raw="2026-07-21T00:00:00Z",
        )
        line = format_snapshot(snap)
        assert "resets at 2026-07-21T00:00:00Z" in line

    def test_unknown_fields_render_question_mark(self):
        snap = RateLimitSnapshot(provider="openai", model="gpt-4", remaining_requests=10)
        line = format_snapshot(snap)
        assert "requests 10/?" in line
        assert "tokens ?/?" in line

    def test_retry_after_appended(self):
        snap = RateLimitSnapshot(provider="openai", model="gpt-4", remaining_requests=0, retry_after_seconds=30.0)
        assert "retry-after 30s" in format_snapshot(snap)


class TestFormatRateLimits:
    def test_empty_tracker(self):
        assert format_rate_limits(RateLimitTracker()) == "Rate limits: (none reported yet)"

    def test_one_line_per_endpoint(self):
        tracker = RateLimitTracker()
        tracker.observe(RateLimitSnapshot(provider="openai", model="gpt-4", remaining_requests=1))
        tracker.observe(RateLimitSnapshot(provider="anthropic", model="claude", remaining_requests=2))
        out = format_rate_limits(tracker)
        lines = out.splitlines()
        assert lines[0] == "Rate limits (latest observed):"
        assert len(lines) == 3  # header + 2 endpoints
        # Sorted: anthropic before openai.
        assert "anthropic/claude" in lines[1]
        assert "openai/gpt-4" in lines[2]
