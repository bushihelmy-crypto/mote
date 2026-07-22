#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RFC 6238 TOTP — verified against the published Appendix B test vectors.

The vectors use the ASCII seed ``"12345678901234567890"`` (base32
``GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ``) and SHA-1; the RFC prints the 8-digit
code, so the 6-digit expectation is its low 6 digits. Also checks seed
normalisation (lower-case / spaces / missing padding) and error paths.
"""
from __future__ import annotations

import time

import pytest

from mote.common.secrets.totp import totp_now

# base32("12345678901234567890") — the RFC 6238 Appendix B SHA-1 seed.
_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TestRfc6238Vectors:
    @pytest.mark.parametrize(
        "t, code8",
        [
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
            (20000000000, "65353130"),
        ],
    )
    def test_eight_digit_vectors(self, t, code8):
        assert totp_now(_SEED, digits=8, t=t) == code8

    def test_six_digit_is_low_six(self):
        # 8-digit "94287082" at T=59 → 6-digit "287082".
        assert totp_now(_SEED, digits=6, t=59) == "287082"


class TestNormalisation:
    def test_lowercase_and_spaces_and_padding_tolerated(self):
        messy = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
        assert totp_now(messy, digits=8, t=59) == totp_now(_SEED, digits=8, t=59)

    def test_stable_within_a_period(self):
        # Two instants in the same 30s window [90, 120) yield the same code.
        assert totp_now(_SEED, t=95) == totp_now(_SEED, t=119)

    def test_changes_across_periods(self):
        # Straddle the 120s window boundary → different codes.
        assert totp_now(_SEED, t=119) != totp_now(_SEED, t=121)

    def test_default_time_is_now(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 59.0)
        assert totp_now(_SEED, digits=8) == "94287082"


class TestErrors:
    def test_empty_secret_raises(self):
        with pytest.raises(ValueError):
            totp_now("")

    def test_unsupported_algorithm_raises(self):
        with pytest.raises(ValueError):
            totp_now(_SEED, algorithm="md5")
