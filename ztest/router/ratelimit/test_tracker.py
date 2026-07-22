"""RateLimitTracker: last-write-wins per endpoint + best-effort observe."""

from __future__ import annotations

from mote.router.ratelimit import RateLimitSnapshot, RateLimitTracker


class TestObserveHeaders:
    def test_observe_records_snapshot(self):
        tracker = RateLimitTracker()
        tracker.observe_headers("openai", "gpt-4", {"x-ratelimit-remaining-requests": "4999"})
        snap = tracker.get("openai", "gpt-4")
        assert snap is not None
        assert snap.remaining_requests == 4999

    def test_no_recognized_header_is_noop(self):
        tracker = RateLimitTracker()
        tracker.observe_headers("openai", "gpt-4", {"content-type": "json"})
        assert tracker.get("openai", "gpt-4") is None
        assert tracker.is_empty()

    def test_last_write_wins(self):
        tracker = RateLimitTracker()
        tracker.observe_headers("openai", "gpt-4", {"x-ratelimit-remaining-requests": "100"})
        tracker.observe_headers("openai", "gpt-4", {"x-ratelimit-remaining-requests": "99"})
        assert tracker.get("openai", "gpt-4").remaining_requests == 99

    def test_bad_headers_never_raise(self):
        tracker = RateLimitTracker()

        class Exploding:
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        # A parse failure must be swallowed — telemetry can't break a live call.
        tracker.observe_headers("openai", "gpt-4", Exploding())
        assert tracker.is_empty()


class TestObserve:
    def test_observe_snapshot_directly(self):
        tracker = RateLimitTracker()
        snap = RateLimitSnapshot(provider="anthropic", model="claude", remaining_requests=5)
        tracker.observe(snap)
        assert tracker.get("anthropic", "claude") is snap


class TestSnapshots:
    def test_sorted_by_endpoint(self):
        tracker = RateLimitTracker()
        tracker.observe(RateLimitSnapshot(provider="openai", model="gpt-4", remaining_requests=1))
        tracker.observe(RateLimitSnapshot(provider="anthropic", model="claude", remaining_requests=1))
        snaps = tracker.snapshots()
        assert [(s.provider, s.model) for s in snaps] == [
            ("anthropic", "claude"),
            ("openai", "gpt-4"),
        ]

    def test_distinct_endpoints_coexist(self):
        tracker = RateLimitTracker()
        tracker.observe(RateLimitSnapshot(provider="openai", model="gpt-4", remaining_requests=1))
        tracker.observe(RateLimitSnapshot(provider="openai", model="gpt-4o", remaining_requests=2))
        assert len(tracker.snapshots()) == 2


class TestIsEmpty:
    def test_empty_on_construction(self):
        assert RateLimitTracker().is_empty()

    def test_not_empty_after_observe(self):
        tracker = RateLimitTracker()
        tracker.observe(RateLimitSnapshot(provider="openai", model="gpt-4"))
        assert not tracker.is_empty()
