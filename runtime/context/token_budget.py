"""Tokenizer-independent text budget helpers."""

from typing import Protocol

from mote.runtime.text.elision import Elision, ElisionStrategy, ElisionUnit


class Tokenizer(Protocol):
    def count_text(self, text: str) -> int:
        ...


def count_tokens(text: str, *, tokenizer: Tokenizer) -> int:
    return tokenizer.count_text(text)


def truncate_to_tokens(text: str, max_tokens: int, *, tokenizer: Tokenizer) -> str:
    if max_tokens < 0:
        raise ValueError("max_tokens must be non-negative")
    total = tokenizer.count_text(text)
    if total <= max_tokens:
        return text
    result: list[str] = []
    cumulative = 0
    for line in text.split("\n"):
        line_tokens = tokenizer.count_text(line)
        if cumulative + line_tokens > max_tokens:
            break
        cumulative += line_tokens
        result.append(line)
    truncated = "\n".join(result)
    marker = Elision(
        ElisionUnit.TOKENS,
        max(total - cumulative, 0),
        total,
        ElisionStrategy.HEAD,
    ).render_for_model()
    return f"{truncated}\n\n{marker}" if truncated else marker


__all__ = ["Tokenizer", "count_tokens", "truncate_to_tokens"]
