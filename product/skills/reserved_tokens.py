"""Pure escaping for model-reserved prompt tokens."""

import re

RESERVED_MODEL_TOKEN_PATTERN = re.compile(r"</?system>|<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>", re.IGNORECASE)


def escape_reserved_model_tokens(text: str) -> str:
    return RESERVED_MODEL_TOKEN_PATTERN.sub("", text)


__all__ = ["RESERVED_MODEL_TOKEN_PATTERN", "escape_reserved_model_tokens"]
