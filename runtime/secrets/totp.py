"""RFC 6238 one-time passwords for Runtime secret references.

A login form's second factor is often a 6-digit TOTP derived from a shared
base32 seed (the QR code / "manual entry key" a site shows when you enable 2FA).
The autonomous-fill path (:mod:`mote.runtime.secrets.refs`) resolves a
``<totp:KEY>`` placeholder by reading that *seed* from the vault by key and
computing the current code here — so the seed never reaches the model and the
one-time code is generated at fill time, not stored.

Deliberately dependency-free (no ``pyotp``): RFC 6238 is HMAC over a time
counter + RFC 4226 dynamic truncation, ~20 lines of ``hmac``/``struct``/
``base64``. This keeps the vault's crypto surface to the stdlib we already trust.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional

# RFC 6238 defaults: 30-second step, 6 digits, SHA-1 (the near-universal
# authenticator-app configuration).
DEFAULT_PERIOD = 30
DEFAULT_DIGITS = 6
DEFAULT_ALGORITHM = "sha1"

_ALGORITHMS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 authenticator seed (case-insensitive, pad-tolerant).

    Authenticator "manual entry keys" are base32, frequently shown lower-case
    and/or with spaces and without ``=`` padding. Normalise to the strict
    upper-case, space-free, correctly-padded form :func:`base64.b32decode` wants.
    """
    cleaned = secret.strip().replace(" ", "").upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad))


def totp_now(
    secret: str,
    *,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = DEFAULT_ALGORITHM,
    t: Optional[float] = None,
) -> str:
    """Return the current TOTP code for a base32 ``secret`` (RFC 6238).

    Args:
        secret: The base32-encoded shared seed (authenticator "setup key").
        digits: Number of output digits (default 6).
        period: Time step in seconds (default 30).
        algorithm: HMAC hash — ``sha1`` (default), ``sha256`` or ``sha512``.
        t: Unix time to compute for; defaults to ``time.time()`` (override in
            tests for the published RFC 6238 vectors).

    Returns:
        The zero-padded numeric code as a string (e.g. ``"287082"``).
    """
    algo = _ALGORITHMS.get(algorithm.lower())
    if algo is None:
        raise ValueError(f"unsupported TOTP algorithm: {algorithm!r}")
    now = time.time() if t is None else t
    counter = int(now // period)
    key = _decode_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, algo).digest()
    # RFC 4226 dynamic truncation: low 4 bits of the last byte pick a 4-byte
    # window, masked to 31 bits, then reduced to the requested digit count.
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


__all__ = ["totp_now", "DEFAULT_PERIOD", "DEFAULT_DIGITS", "DEFAULT_ALGORITHM"]
