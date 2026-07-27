"""RateLimitSnapshot header-parsing: both provider dialects + duration parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from mote.runtime.models.ratelimit import RateLimitSnapshot
from mote.runtime.models.ratelimit.snapshot import _parse_duration, _to_int


class TestToInt:
    def test_plain_int(self):
        assert _to_int("5000") == 5000

    def test_whitespace_stripped(self):
        assert _to_int("  42 ") == 42

    def test_none_returns_none(self):
        assert _to_int(None) is None

    def test_garbage_returns_none(self):
        assert _to_int("not-a-number") is None


class TestParseDuration:
    def test_bare_seconds(self):
        assert _parse_duration("30") == 30.0

    def test_seconds_unit(self):
        assert _parse_duration("1s") == 1.0

    def test_minutes_and_seconds(self):
        assert _parse_duration("6m0s") == 360.0

    def test_milliseconds_beats_minutes(self):
        # "ms" must be matched before "m" (longest-unit-first).
        assert _parse_duration("88ms") == 0.088

    def test_fractional(self):
        assert _parse_duration("1.5s") == 1.5

    def test_compound_hms(self):
        assert _parse_duration("1h2m3s") == 3723.0

    def test_none(self):
        assert _parse_duration(None) is None

    def test_empty(self):
        assert _parse_duration("") is None

    def test_unparseable(self):
        assert _parse_duration("soon") is None


class TestFromHeadersOpenAI:
    def test_full_openai_dialect(self):
        headers = {
            "x-ratelimit-limit-requests": "5000",
            "x-ratelimit-remaining-requests": "4999",
            "x-ratelimit-reset-requests": "6m0s",
            "x-ratelimit-limit-tokens": "160000",
            "x-ratelimit-remaining-tokens": "159000",
            "x-ratelimit-reset-tokens": "1s",
        }
        snap = RateLimitSnapshot.from_headers("openai", "gpt-4", headers)
        assert snap is not None
        assert snap.limit_requests == 5000
        assert snap.remaining_requests == 4999
        assert snap.reset_requests_seconds == 360.0
        assert snap.reset_requests_raw is None  # OpenAI uses seconds, not raw
        assert snap.limit_tokens == 160000
        assert snap.remaining_tokens == 159000
        assert snap.reset_tokens_seconds == 1.0
        assert snap.observed_at > 0

    def test_partial_openai(self):
        snap = RateLimitSnapshot.from_headers("openai", "gpt-4", {"x-ratelimit-remaining-requests": "10"})
        assert snap is not None
        assert snap.remaining_requests == 10
        assert snap.limit_requests is None


class TestFromHeadersAnthropic:
    def test_full_anthropic_dialect(self):
        headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "49",
            "anthropic-ratelimit-requests-reset": "2026-07-21T00:00:00Z",
            "anthropic-ratelimit-tokens-limit": "40000",
            "anthropic-ratelimit-tokens-remaining": "39000",
            "anthropic-ratelimit-tokens-reset": "2026-07-21T00:01:00Z",
        }
        snap = RateLimitSnapshot.from_headers("anthropic", "claude-opus-4-8", headers)
        assert snap is not None
        assert snap.limit_requests == 50
        assert snap.remaining_requests == 49
        assert snap.reset_requests_raw == "2026-07-21T00:00:00Z"
        assert snap.reset_requests_seconds is None  # Anthropic uses raw stamp
        assert snap.limit_tokens == 40000
        assert snap.remaining_tokens == 39000
        assert snap.reset_tokens_raw == "2026-07-21T00:01:00Z"

    def test_normalizes_anthropic_reset_timestamp_for_admission(self):
        reset_at = datetime.now(timezone.utc).timestamp() + 30.0
        reset = datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
        snap = RateLimitSnapshot.from_headers(
            "anthropic",
            "claude",
            {
                "anthropic-ratelimit-requests-remaining": "0",
                "anthropic-ratelimit-requests-reset": reset,
            },
        )

        assert snap is not None
        assert snap.normalized_reset_requests_seconds is not None
        assert 29.0 <= snap.normalized_reset_requests_seconds <= 30.0


class TestFromHeadersRetryAfter:
    def test_retry_after_alone_builds_snapshot(self):
        # A 429 may carry only retry-after and no quota headers — still meaningful.
        snap = RateLimitSnapshot.from_headers("openai", "gpt-4", {"retry-after": "30"})
        assert snap is not None
        assert snap.retry_after_seconds == 30.0

    def test_retry_after_http_date_parsed(self):
        # RFC-7231 allows an HTTP-date form; the shared RFC parser handles it
        # (a far-future date → a positive delta), which _parse_duration could not.
        snap = RateLimitSnapshot.from_headers("openai", "gpt-4", {"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
        assert snap is not None
        assert snap.retry_after_seconds is not None and snap.retry_after_seconds > 0


class TestProviderDialectDispatch:
    def test_anthropic_provider_ignores_openai_keys(self):
        # A snapshot keyed anthropic must not pick up stray OpenAI-dialect headers.
        snap = RateLimitSnapshot.from_headers("anthropic", "claude", {"x-ratelimit-remaining-requests": "4999"})
        assert snap is None

    def test_openai_provider_ignores_anthropic_keys(self):
        snap = RateLimitSnapshot.from_headers("openai", "gpt-4", {"anthropic-ratelimit-requests-remaining": "49"})
        assert snap is None

    def test_unknown_provider_reads_either_dialect(self):
        # An unrecognized provider label falls back to trying both dialects.
        snap = RateLimitSnapshot.from_headers("mystery", "m1", {"anthropic-ratelimit-requests-remaining": "7"})
        assert snap is not None and snap.remaining_requests == 7


class TestFromHeadersEmpty:
    def test_no_recognized_headers_returns_none(self):
        assert RateLimitSnapshot.from_headers("openai", "gpt-4", {"content-type": "json"}) is None

    def test_empty_headers_returns_none(self):
        assert RateLimitSnapshot.from_headers("openai", "gpt-4", {}) is None
