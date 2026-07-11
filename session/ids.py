"""Session id minting — the single source of new session identifiers.

A session id is ``{timestamp}_{rand}``: a ``YYYYMMDDHHMMSSmmm`` wall-clock
timestamp (year→millisecond) joined by ``_`` to a short random suffix. The
timestamp prefix makes ids lexicographically sortable by creation time (newest
ids sort last) and human-legible at a glance, while the random suffix keeps two
ids minted in the same millisecond distinct. Both the initial ``RoleState`` id
and a forked child id run through :func:`new_session_id` so the format has one
owner.

Zero-dependency leaf (stdlib only), so both ``roles`` and ``session`` may import
it without any cycle.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4


def new_session_id() -> str:
    """Mint a fresh session id as ``{timestamp}_{rand}``.

    ``timestamp`` is the current local time formatted ``YYYYMMDDHHMMSSmmm``
    (down to the millisecond); ``rand`` is an 8-char random hex suffix
    disambiguating ids minted within the same millisecond.
    """
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    return f"{timestamp}_{uuid4().hex[:8]}"


__all__ = ["new_session_id"]
