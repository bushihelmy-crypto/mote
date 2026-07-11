"""Human interaction commands — ask_human, reply_to_human, AskUserQuestion."""
from __future__ import annotations

from typing import Awaitable, Callable

from mote.common.prompt.tools import ASK_HUMAN_DESCRIPTION, ASK_USER_QUESTION_PROMPT, REPLY_TO_HUMAN_DESCRIPTION
from mote.common.schema import AskUserQuestionAnswers, AskUserQuestionInput, AskUserQuestionItem
from mote.executor.base_tool import BaseTool
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_INVALID_QUESTIONS = "Error: invalid questions — {error}"


@register_tool
class AskHuman(BaseTool):
    """Ask the human user a question and wait for their response."""

    name = "Ask"
    aliases = ["AskHuman"]
    description = ASK_HUMAN_DESCRIPTION
    requires = ("ask_human",)

    # Injected from Role by bind(): Role.ask_human.
    ask_human: Callable[[str], Awaitable[str]]

    async def call(self, *, question: str) -> str:
        """Ask the human user a question.

        Args:
            question: The question to ask the human user.
        """
        return await self.ask_human(question)


@register_tool
class ReplyToHuman(BaseTool):
    """Reply to the human user with the content provided."""

    name = "Reply"
    aliases = ["ReplyToHuman"]
    description = REPLY_TO_HUMAN_DESCRIPTION
    requires = ("reply_to_human",)

    # Injected from Role by bind(): Role.reply_to_human.
    reply_to_human: Callable[[str], Awaitable[str]]

    async def call(self, *, content: str) -> str:
        """Reply to the human user.

        Args:
            content: The content to reply to the human user.
        """
        return await self.reply_to_human(content)


# ---------------------------------------------------------------------------
# AskUserQuestion — multiple-choice questions (ported from Claude Code)
# ---------------------------------------------------------------------------


@register_tool
class AskUserQuestion(BaseTool):
    """Ask the user multiple choice questions to gather information or make decisions."""

    name = "AskUserQuestion"
    aliases: list[str] = []
    # The long-form prompt is the model-facing description.
    description = ASK_USER_QUESTION_PROMPT
    requires = ("ask_user_question",)

    # Injected from Role by bind(): Role.ask_user_question (structured channel).
    ask_user_question: Callable[[list[AskUserQuestionItem]], Awaitable[AskUserQuestionAnswers]]

    # --- Execution -----------------------------------------------------------

    async def call(self, *, questions: list[AskUserQuestionItem]) -> ToolResult:
        """Ask the user one or more multiple-choice questions and collect answers.

        The native input_schema (1-4 questions, each with 2-4 {label, description}
        options + optional multiSelect) is derived automatically from the
        ``list[AskUserQuestionItem]`` annotation — no hand-written schema. This
        tool runs on the native tool-use channel only (structured params).

        The answers come back structured (``selected`` labels + ``free_text`` per
        question, kept in separate fields), so a numeric or multi-line free-text
        answer is never misread as an option index or misaligned across
        questions. ``output`` (the CC summary) enters history; ``data`` (the
        structured answers, ``repr=False``) is available to downstream consumers
        without entering history.

        Args:
            questions: A list of 1-4 question objects. Each object has:
                - question (str): the full question text.
                - header (str): a short chip/tag label.
                - options (list): 2-4 {label, description} option objects. Do not
                  include an "Other" option; it is added automatically.
                - multiSelect (bool, optional): allow multiple selections.
        """
        items = self._coerce(questions)  # keep the pydantic validation gate
        answers = await self.ask_user_question(items)  # structured round-trip
        return ToolResult(output=self._format_result(answers), data=answers)

    # --- Validation (pydantic models enforce min/max + uniqueness) -----------

    @staticmethod
    def _coerce(questions: list) -> list[AskUserQuestionItem]:
        """Validate raw input and return typed AskUserQuestionItem models.

        The native channel delivers ``questions`` as plain dicts, so we run it
        through ``AskUserQuestionInput`` to get pydantic's min/max + uniqueness
        checks (mirrors CC's Zod schema) and typed access downstream.
        """
        try:
            return AskUserQuestionInput.model_validate({"questions": questions}).questions
        except Exception as e:  # noqa: BLE001 — surface a clean failure to the model
            raise ToolError(_MSG_INVALID_QUESTIONS.format(error=e))

    # --- Result formatting (verbatim CC wording) -----------------------------

    @staticmethod
    def _format_result(answers: AskUserQuestionAnswers) -> str:
        parts = [f'"{a.question}"="{a.display}"' for a in answers.answers]
        return (
            "User has answered your questions: "
            + ", ".join(parts)
            + ". You can now continue with the user's answers in mind."
        )
