#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for PKCE helpers: S256 challenge derivation + verifier/state shape."""
from __future__ import annotations

import base64
import hashlib
import re

import pytest
from mote.router.oauth.pkce import gen_code_challenge, gen_code_verifier, gen_state

_UNRESERVED = re.compile(r"^[A-Za-z0-9._~-]+$")


def test_challenge_is_s256_of_verifier():
    verifier = gen_code_verifier()
    challenge = gen_code_challenge(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_verifier_length_and_charset():
    v = gen_code_verifier()  # 32 bytes -> 43 chars base64url
    assert 43 <= len(v) <= 128
    assert _UNRESERVED.match(v)
    assert "=" not in v


def test_verifier_is_random():
    assert gen_code_verifier() != gen_code_verifier()


@pytest.mark.parametrize("n", [32, 64, 96])
def test_verifier_custom_size(n):
    v = gen_code_verifier(n)
    assert _UNRESERVED.match(v)
    assert 43 <= len(v) <= 128


def test_verifier_rejects_out_of_range():
    with pytest.raises(ValueError):
        gen_code_verifier(8)
    with pytest.raises(ValueError):
        gen_code_verifier(200)


def test_state_is_urlsafe_and_random():
    s = gen_state()
    assert _UNRESERVED.match(s)
    assert gen_state() != gen_state()
