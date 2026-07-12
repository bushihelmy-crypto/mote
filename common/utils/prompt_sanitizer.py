"""Shared prompt sanitization and token truncation utilities.

Used by both SkillInjector (P0) and ContextInjector (P1) to prevent
prompt injection attacks and control injected content size.
"""

import re

from metagpt.common.utils import count_string_tokens

# Patterns that could be used for prompt injection attacks
DANGEROUS_PATTERNS = re.compile(r"</?system>|<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>", re.IGNORECASE)

# Using gpt-4o tokenizer as an approximate token counter for budget control.
# The exact count may differ across models (Claude, Gemini, DeepSeek, etc.),
# but for coarse truncation thresholds (2000-3000 tokens) the deviation is acceptable.
_TOKEN_MODEL = "gpt-4o"


def sanitize(text: str) -> str:
    """Remove potentially dangerous prompt injection patterns."""
    return DANGEROUS_PATTERNS.sub("", text)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens (line-level granularity)."""
    lines = text.split("\n")
    result = []
    cumulative = 0
    for line in lines:
        line_tokens = count_string_tokens(line, _TOKEN_MODEL)
        if cumulative + line_tokens > max_tokens:
            break
        cumulative += line_tokens
        result.append(line)
    truncated = "\n".join(result) if result else ""
    return truncated + "\n\n[... truncated due to token limit]"


def count_tokens(text: str) -> int:
    """Count tokens using the shared approximate tokenizer."""
    return count_string_tokens(text, _TOKEN_MODEL)
