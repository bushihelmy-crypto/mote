#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AskUserQuestion schema models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# CC's chip-width limit on a question's `header` (used in the schema description).
ASK_USER_QUESTION_CHIP_WIDTH = 12


class AskUserQuestionOption(BaseModel):
    """One selectable choice for an AskUserQuestion question."""

    label: str = Field(
        description=(
            "The display text for this option that the user will see and select. "
            "Should be concise (1-5 words) and clearly describe the choice."
        )
    )
    description: str = Field(
        description=(
            "Explanation of what this option means or what will happen if chosen. "
            "Useful for providing context about trade-offs or implications."
        )
    )


class AskUserQuestionItem(BaseModel):
    """A single multiple-choice question (mirrors Claude Code's Zod schema)."""

    question: str = Field(
        description=(
            "The complete question to ask the user. Should be clear, specific, and "
            'end with a question mark. Example: "Which library should we use for '
            'date formatting?" If multiSelect is true, phrase it accordingly, e.g. '
            '"Which features do you want to enable?"'
        )
    )
    header: str = Field(
        description=(
            f"Very short label displayed as a chip/tag (max {ASK_USER_QUESTION_CHIP_WIDTH} "
            'chars). Examples: "Auth method", "Library", "Approach".'
        )
    )
    options: list[AskUserQuestionOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "The available choices for this question. Must have 2-4 options. Each "
            "option should be a distinct, mutually exclusive choice (unless "
            "multiSelect is enabled). There should be no 'Other' option, that will "
            "be provided automatically."
        ),
    )
    multiSelect: bool = Field(
        default=False,
        description=(
            "Set to true to allow the user to select multiple options instead of "
            "just one. Use when choices are not mutually exclusive."
        ),
    )

    @model_validator(mode="after")
    def _unique_labels(self) -> "AskUserQuestionItem":
        labels = [o.label for o in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError(f"option labels must be unique within {self.question!r}")
        return self


class AskUserQuestionInput(BaseModel):
    """Top-level input for the AskUserQuestion tool: 1-4 questions."""

    questions: list[AskUserQuestionItem] = Field(
        min_length=1,
        max_length=4,
        description="Questions to ask the user (1-4 questions)",
    )

    @model_validator(mode="after")
    def _unique_questions(self) -> "AskUserQuestionInput":
        texts = [q.question for q in self.questions]
        if len(texts) != len(set(texts)):
            raise ValueError("question texts must be unique")
        return self


class AskUserQuestionAnswer(BaseModel):
    """A single question's answer — selection and free text kept separate.

    ``selected`` (chosen option labels) and ``free_text`` (the "Other" text) are
    stored in distinct fields — exactly the information the old text round-trip
    collapsed. A numeric or multi-line free-text answer can never be misread as
    an option index or misaligned across questions.
    """

    header: str = ""
    question: str  # association key back to the AskUserQuestionItem
    selected: list[str] = Field(default_factory=list)  # chosen option labels
    free_text: str = ""  # the "Other" free text (empty when none)

    @property
    def is_free_text(self) -> bool:
        return bool(self.free_text)

    @property
    def display(self) -> str:
        """Rebuild CC's ``"q"="a"`` answer string at the formatting boundary."""
        parts = list(self.selected)
        if self.free_text:
            parts.append(self.free_text)
        return ", ".join(parts)


class AskUserQuestionAnswers(BaseModel):
    """The structured answers for all questions in one AskUserQuestion call."""

    answers: list[AskUserQuestionAnswer] = Field(default_factory=list)
