"""Unit tests for the shared host normalizer + domain-glob matcher.

Lives in ``common/net`` (leaf layer) so both the sandbox egress proxy and the
``SandboxRuntimeConfig`` config-time subset check import the *same* matcher.
"""

from mote.contracts.net import matches_pattern, normalize_host


class TestNormalizeHost:
    def test_lowercases_and_strips_port(self):
        assert normalize_host("API.Example.COM:443") == "api.example.com"

    def test_strips_trailing_dot(self):
        assert normalize_host("example.com.") == "example.com"

    def test_bracketed_ipv6_with_port(self):
        assert normalize_host("[::1]:8080") == "::1"

    def test_bracketed_ipv6_no_port(self):
        assert normalize_host("[2001:db8::1]") == "2001:db8::1"

    def test_plain_ipv6_untouched(self):
        # Many colons => not a host:port, leave as-is.
        assert normalize_host("2001:db8::1") == "2001:db8::1"

    def test_empty_and_blank(self):
        assert normalize_host("") == ""
        assert normalize_host("   ") == ""


class TestMatchesPattern:
    def test_exact(self):
        assert matches_pattern("example.com", "example.com")
        assert not matches_pattern("api.example.com", "example.com")

    def test_single_label_wildcard(self):
        assert matches_pattern("api.example.com", "*.example.com")
        # Apex does not match a single-label wildcard.
        assert not matches_pattern("example.com", "*.example.com")
        # Two extra labels do not match a single-label wildcard.
        assert not matches_pattern("a.b.example.com", "*.example.com")

    def test_deep_wildcard_matches_apex_and_any_depth(self):
        assert matches_pattern("example.com", "**.example.com")
        assert matches_pattern("api.example.com", "**.example.com")
        assert matches_pattern("a.b.example.com", "**.example.com")

    def test_normalizes_host_and_pattern(self):
        # Host lowercased + port stripped before matching the exact pattern.
        assert matches_pattern("Example.COM:443", "example.com")
        # Pattern is also lowercased/stripped/trailing-dot-trimmed.
        assert matches_pattern("example.com.", "  Example.Com ")
        # A subdomain still fails an exact-apex pattern after normalization.
        assert not matches_pattern("API.Example.COM:443", "example.com")

    def test_empty_pattern_never_matches(self):
        assert not matches_pattern("example.com", "")
        assert not matches_pattern("example.com", "   ")


class TestSharedWithPolicy:
    """The matcher moved out of sandbox.network.policy; it must re-export it."""

    def test_policy_reexports_matcher(self):
        from mote.runtime.sandbox.network import policy

        assert policy.matches_pattern is matches_pattern
        assert policy.normalize_host is normalize_host
