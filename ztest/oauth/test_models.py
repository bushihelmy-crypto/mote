#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OAuthToken.is_expired buffer boundary semantics."""
from __future__ import annotations

import time

from mote.runtime.models.auth.oauth.models import AuthMode, OAuthToken


def test_no_expiry_never_expires():
    tok = OAuthToken(access_token="x", expires_at=None)
    assert tok.is_expired() is False
    assert tok.is_expired(buffer=99999) is False


def test_expired_when_past():
    tok = OAuthToken(access_token="x", expires_at=time.time() - 10)
    assert tok.is_expired() is True


def test_valid_when_future():
    tok = OAuthToken(access_token="x", expires_at=time.time() + 1000)
    assert tok.is_expired() is False


def test_buffer_boundary_triggers_early_expiry():
    # Token expires in 100s; with a 300s buffer it should read as expired.
    tok = OAuthToken(access_token="x", expires_at=time.time() + 100)
    assert tok.is_expired(buffer=300) is True
    # ...but with a small buffer it is still valid.
    assert tok.is_expired(buffer=10) is False


def test_auth_mode_values():
    assert AuthMode.STATIC_KEY.value == "static_key"
    assert AuthMode.OAUTH.value == "oauth"
