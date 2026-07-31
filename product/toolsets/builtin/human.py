"""Human interaction commands — ask_user, reply_to_user, AskUserQuestion."""
from __future__ import annotations

from typing import ClassVar

from mote.contracts.authorization import PermissionDecision
from mote.contracts.interaction import AskUserQuestionAnswers, AskUserQuestionInput, AskUserQuestionItem
from mote.runtime.errors import ToolError
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import AskUser as AskUserCap
from mote.runtime.tools.capability_types import AskUserQuestion as AskUserQuestionCap
from mote.runtime.tools.capability_types import ReplyToUser as ReplyToUserCap
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_INVALID_QUESTIONS = "Error: invalid questions — {error}"


def _self_approve(self, args: dict) -> PermissionDecision:
    """Permission self-check shared by the human-interaction tools.

    These tools ARE the interactive user channel: they mutate nothing on disk
    or system, they only put a question/reply in front of the human. Gating
    them behind the permission engine's approval prompt is both redundant and
    deadlock-prone — the approval prompt is itself a user interaction, so an
    un-exempted AskUserQuestion fires an "[APPROVAL REQUIRED]" prompt *before*
    the actual question, which can hang the whole react loop. Returning an
    ``allow`` short-circuits engine step 11 (default→ask). Bypass-immune deny/ask
    rules (engine steps 1-4) still win, so a user can explicitly gate these if
    they ever want to.
    """
    return PermissionDecision.allow("safe", "human-interaction tool needs no approval")


@register_tool
class AskUser(BaseTool):
    """Ask the user a question and wait for their response."""

    name = "Ask"
    aliases = ["AskUser"]
    requires = ("ask_user",)

    # Injected from Role by bind(): Role.ask_user.
    ask_user: AskUserCap

    check_permissions = _self_approve

    async def call(self, *, question: str) -> str:
        """Ask the user a free-text question — when you are blocked or unsure.

        Use this when you fail the current task or are unsure of the situation
        you have encountered, and need the user to clarify or decide.

        Args:
            question: The question to ask the user.
        """
        return await self.ask_user(question)


@register_tool
class ReplyToUser(BaseTool):
    """Reply to the user with the content provided."""

    name = "Reply"
    aliases = ["ReplyToUser"]
    requires = ("reply_to_user",)

    # Injected from Role by bind(): Role.reply_to_user.
    reply_to_user: ReplyToUserCap

    check_permissions = _self_approve

    async def call(self, *, content: str) -> str:
        """Reply to the user with a message — deliver content without ending the task.

        Sends the given content to the user immediately while keeping the task
        active, so you can continue working afterward (unlike End).

        Args:
            content: The content to reply to the user.
        """
        return await self.reply_to_user(content)


# ---------------------------------------------------------------------------
# AskUserQuestion — multiple-choice questions
# ---------------------------------------------------------------------------


@register_tool
class AskUserQuestion(BaseTool):
    """Ask the user multiple choice questions to gather information or make decisions."""

    name = "AskUserQuestion"
    aliases: list[str] = []
    # Recall synonyms for tool-search: ways a model expresses "check with the
    # human" that the summary ("ask the user multiple-choice questions") omits.
    keywords: ClassVar[list[str]] = [
        "ask user",
        "prompt user",
        "clarify",
        "confirm",
        "user input",
        "question",
        "问用户",
        "询问",
        "确认",
    ]
    requires = ("ask_user_question",)

    # Injected from Role by bind(): Role.ask_user_question (structured channel).
    ask_user_question: AskUserQuestionCap

    check_permissions = _self_approve

    # --- Execution -----------------------------------------------------------

    async def call(self, *, questions: list[AskUserQuestionItem]) -> ToolResult:
        """Ask the user multiple-choice questions — gather preferences or decisions.

        Use this tool when you need to ask the user questions during execution.
        This lets you gather preferences or requirements, clarify ambiguous
        instructions, get decisions on implementation choices as you work, and
        offer choices about what direction to take.

        Usage notes:
        - Users can always select "Other" to provide custom free-text input.
        - Use multiSelect=true to allow multiple answers for one question.
        - If you recommend a specific option, make it the first option and add
          "(Recommended)" at the end of its label.

        The native input_schema (1-4 questions, each with 2-4 {label, description}
        options + optional multiSelect) is derived automatically from the
        ``list[AskUserQuestionItem]`` annotation — no hand-written schema. This
        tool runs on the native tool-use channel only (structured params).

        The answers come back structured (``selected`` labels + ``free_text`` per
        question, kept in separate fields), so a numeric or multi-line free-text
        answer is never misread as an option index or misaligned across
        questions. ``output`` (the summary) enters history; ``data`` (the
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
        checks (via a schema) and typed access downstream.
        """
        try:
            return AskUserQuestionInput.model_validate({"questions": questions}).questions
        except Exception as e:  # noqa: BLE001 — surface a clean failure to the model
            raise ToolError(_MSG_INVALID_QUESTIONS.format(error=e))

    # --- Result formatting -----------------------------

    @staticmethod
    def _format_result(answers: AskUserQuestionAnswers) -> str:
        parts = [f'"{a.question}"="{a.display}"' for a in answers.answers]
        return (
            "User has answered your questions: "
            + ", ".join(parts)
            + ". You can now continue with the user's answers in mind."
        )
