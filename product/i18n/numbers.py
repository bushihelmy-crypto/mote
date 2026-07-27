#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Locale number symbols + a formatting seam for the human display layer.

:class:`Numbers` holds a locale's decimal / grouping / percent / currency
symbols; the ``format_*`` helpers are the single place a display string turns a
raw number into a locale-aware token. zh and en share symbols today, so the
seam is a no-op difference — but it exists, so making ``"3,400"`` / ``"42%"`` /
``"$0.01"`` locale-aware later needs *no* call-site change.

Deliberately NOT auto-applied by the message formatter: plain ``{var}`` / ``#``
interpolation stays ungrouped (matching the CLI's current wording, e.g.
``读取 1024 行``), so grouping is an explicit, opt-in decision at the call site.

Zero dependencies (stdlib only).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Numbers:
    """A locale's number symbols (the extension point for locale-aware digits)."""

    decimal: str = "."
    group: str = ","
    percent: str = "%"
    currency: str = "$"


#: The default (en/zh share these) symbol set.
DEFAULT_NUMBERS = Numbers()


def format_decimal(value: float, numbers: Numbers = DEFAULT_NUMBERS) -> str:
    """Format *value* with locale grouping/decimal symbols (integers stay integral)."""
    if isinstance(value, int) or float(value).is_integer():
        grouped = f"{int(value):,}"
        return grouped.replace(",", numbers.group)
    grouped = f"{value:,}"
    # Python emits ``,`` groups + ``.`` decimal; remap both to the locale symbols.
    return grouped.replace(",", "\x00").replace(".", numbers.decimal).replace("\x00", numbers.group)


def format_percent(ratio: float, numbers: Numbers = DEFAULT_NUMBERS, *, digits: int = 0) -> str:
    """Format a 0-1 *ratio* as a locale percent (e.g. ``0.42`` → ``"42%"``)."""
    body = f"{ratio * 100:.{digits}f}"
    if numbers.decimal != ".":
        body = body.replace(".", numbers.decimal)
    return f"{body}{numbers.percent}"


def format_currency(amount: float, numbers: Numbers = DEFAULT_NUMBERS, *, digits: int = 2) -> str:
    """Format *amount* with the locale currency symbol (e.g. ``0.01`` → ``"$0.01"``)."""
    body = f"{amount:.{digits}f}"
    if numbers.decimal != ".":
        body = body.replace(".", numbers.decimal)
    return f"{numbers.currency}{body}"


__all__ = ["Numbers", "DEFAULT_NUMBERS", "format_decimal", "format_percent", "format_currency"]
