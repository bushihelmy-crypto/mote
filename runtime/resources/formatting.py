"""Formatting for persisted runtime resources."""

from __future__ import annotations


def _one_decimal(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def format_file_size(size_in_bytes: int) -> str:
    """Format a byte count for persisted-output notices."""
    kb = size_in_bytes / 1024
    if kb < 1:
        return f"{size_in_bytes} bytes"
    if kb < 1024:
        return f"{_one_decimal(kb)}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{_one_decimal(mb)}MB"
    return f"{_one_decimal(mb / 1024)}GB"


__all__ = ["format_file_size"]
