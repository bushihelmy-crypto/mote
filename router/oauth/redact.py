#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Redaction helpers for safe logging of OAuth URLs/params.

Strips secret-bearing query parameters (codes, tokens, secrets) before a URL or
param dict is ever logged. Used by the redirect/loopback flow in P2; added now
(cheap, unit-tested) so no new primitives are needed later.
"""
from __future__ import annotations

from typing import Dict, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query/body parameter names that must never appear in logs.
_SENSITIVE_PARAMS = {
    "code",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "assertion",
    "client_assertion",
    "password",
}

_PLACEHOLDER = "***"


def redact_params(params: Mapping[str, str]) -> Dict[str, str]:
    """Return a copy of ``params`` with sensitive values replaced by ``***``."""
    return {k: (_PLACEHOLDER if k.lower() in _SENSITIVE_PARAMS else v) for k, v in params.items()}


def redact_url(url: str) -> str:
    """Return ``url`` with sensitive query-string params redacted."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted = [
        (k, _PLACEHOLDER if k.lower() in _SENSITIVE_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))
