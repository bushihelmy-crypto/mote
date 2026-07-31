"""English wording shared by runtime presentation surfaces."""

from __future__ import annotations


def plural(noun: str, count: int) -> str:
    return noun if count == 1 else noun + "s"


def count_noun(count: int, noun: str) -> str:
    return f"{count} {plural(noun, count)}"


def verb_agree(count: int, singular: str, plural_form: str) -> str:
    return singular if count == 1 else plural_form


__all__ = ["plural", "count_noun", "verb_agree"]
