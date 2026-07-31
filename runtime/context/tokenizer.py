"""Runtime tokenizer policy for model-facing text budgets."""

from dataclasses import dataclass

from mote.kernel.inference.tokenization import count_string_tokens


@dataclass(frozen=True, slots=True)
class ModelTextTokenizer:
    model: str

    def count_text(self, text: str) -> int:
        return count_string_tokens(text, self.model)


DEFAULT_TEXT_TOKENIZER = ModelTextTokenizer("gpt-4o")


__all__ = ["DEFAULT_TEXT_TOKENIZER", "ModelTextTokenizer"]
