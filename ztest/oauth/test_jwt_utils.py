#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for JWT decoding helpers (no signature verification)."""
from __future__ import annotations

import base64
import json

import pytest

from mote.runtime.models.auth.oauth.jwt_utils import JWTDecodeError, decode_jwt_payload, parse_claims


def _b64url(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def _make_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


def test_decode_payload():
    token = _make_jwt({"sub": "abc", "exp": 1700000000})
    payload = decode_jwt_payload(token)
    assert payload["sub"] == "abc"
    assert payload["exp"] == 1700000000


def test_parse_claims_extracts_email_exp_account():
    token = _make_jwt({"email": "u@x.com", "exp": 1700000000, "account": "acct1"})
    claims = parse_claims(token)
    assert claims.email == "u@x.com"
    assert claims.exp == 1700000000
    assert claims.account == "acct1"


def test_parse_claims_fallback_keys():
    token = _make_jwt({"preferred_username": "alice", "sub": "subject-id"})
    claims = parse_claims(token)
    assert claims.email == "alice"
    assert claims.account == "subject-id"


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "only.two", "a.b.c.d"])
def test_malformed_token_raises(bad):
    with pytest.raises(JWTDecodeError):
        decode_jwt_payload(bad)


def test_non_json_payload_raises():
    bad = f"{_b64url(b'{}')}.{_b64url(b'not-json')}.sig"
    with pytest.raises(JWTDecodeError):
        decode_jwt_payload(bad)
