"""Human interaction commands — ask_human, reply_to_human, AskUserQuestion."""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.common.prompt.tools import (
    ASK_HUMAN_DESCRIPTION,
    ASK_USER_QUESTION_PROMPT,
    REPLY_TO_HUMAN_DESCRIPTION,
)
from metagpt.common.schema import AskUserQuestionInput, AskUserQuestionItem


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

# CC sentinel for the auto-appended free-text choice.
_OTHER_SENTINEL = "__other__"




@register_tool
class AskUserQuestion(BaseTool):
    """Ask the user multiple choice questions to gather information or make decisions."""

    name = "AskUserQuestion"
    aliases: list[str] = []
    # The long-form prompt is the model-facing description.
    description = ASK_USER_QUESTION_PROMPT
    requires = ("ask_human",)

    # Injected from Role by bind(): Role.ask_human (text channel to the human).
    ask_human: Callable[[str], Awaitable[str]]

    # --- Execution -----------------------------------------------------------

    async def call(self, *, questions: list[AskUserQuestionItem]) -> str:
        """Ask the user one or more multiple-choice questions and collect answers.

        The native input_schema (1-4 questions, each with 2-4 {label, description}
        options + optional multiSelect) is derived automatically from the
        ``list[AskUserQuestionItem]`` annotation — no hand-written schema. This
        tool runs on the native tool-use channel only (structured params).

        Args:
            questions: A list of 1-4 question objects. Each object has:
                - question (str): the full question text.
                - header (str): a short chip/tag label.
                - options (list): 2-4 {label, description} option objects. Do not
                  include an "Other" option; it is added automatically.
                - multiSelect (bool, optional): allow multiple selections.
        """
        items = self._coerce(questions)

        # Render every question (with numbered options + auto "Other") into one
        # text prompt, send it through the human text channel, then parse the
        # human's reply back into per-question answers.
        prompt = self._render_questions(items)
        reply = await self.ask_human(prompt)
        answers = self._parse_reply(items, reply)
        return self._format_result(answers)

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
            raise ToolError(f"Error: invalid questions — {e}")


    # --- Rendering: questions -> text prompt for the human -------------------

    @staticmethod
    def _render_questions(questions: list[AskUserQuestionItem]) -> str:
        """Render questions as numbered options + an auto "Other" choice.

        The human replies by typing the option number (or label) per question;
        for multiSelect, multiple numbers separated by commas. The "Other"
        choice lets them type free text.
        """
        single = len(questions) == 1
        blocks: list[str] = []
        for qi, q in enumerate(questions, start=1):
            head = q.question if single else f"Q{qi} [{q.header}]: {q.question}"
            lines = [head]
            for oi, opt in enumerate(q.options, start=1):
                suffix = f" — {opt.description}" if opt.description else ""
                lines.append(f"  {oi}. {opt.label}{suffix}")
            other_no = len(q.options) + 1
            lines.append(f"  {other_no}. Other (type your own answer)")
            hint = (
                "Select one or more by number (comma-separated), or type your own answer."
                if q.multiSelect
                else "Select one by number, or type your own answer."
            )
            lines.append(f"  ({hint})")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    # --- Parsing: human reply -> per-question answers ------------------------

    def _parse_reply(self, questions: list[AskUserQuestionItem], reply: str) -> dict[str, str]:
        """Map the human's text reply back to an answer per question.

        Single question: the whole reply is that question's answer. Multiple
        questions: the reply is split into lines, paired with questions in order.
        A line that is a bare option number (or comma-separated numbers for
        multiSelect) resolves to the corresponding option label(s); anything else
        is treated as free text ("Other").
        """
        reply = (reply or "").strip()
        if len(questions) == 1:
            return {questions[0].question: self._resolve_answer(questions[0], reply)}

        lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
        answers: dict[str, str] = {}
        for i, q in enumerate(questions):
            raw = lines[i] if i < len(lines) else ""
            answers[q.question] = self._resolve_answer(q, raw)
        return answers

    @staticmethod
    def _resolve_answer(question: AskUserQuestionItem, raw: str) -> str:
        """Resolve one raw answer string to option label(s) or free text."""
        raw = raw.strip()
        # Strip a leading "Qn:" / "Qn." prefix the human may have echoed back.
        raw = re.sub(r"^Q\d+\s*[:.\-]\s*", "", raw, flags=re.IGNORECASE).strip()
        options = question.options
        other_no = len(options) + 1

        if not raw:
            return ""

        tokens = [t.strip() for t in raw.split(",")] if question.multiSelect else [raw]
        labels: list[str] = []
        all_numeric = True
        for tok in tokens:
            if tok.isdigit():
                idx = int(tok)
                if 1 <= idx <= len(options):
                    labels.append(options[idx - 1].label)
                    continue
                if idx == other_no:
                    # "Other" selected with no accompanying text: nothing to add.
                    all_numeric = True
                    continue
            all_numeric = False
            break

        if all_numeric and labels:
            return ", ".join(labels)
        # Not a clean numeric selection — treat the whole reply as free text.
        return raw


    # --- Result formatting (verbatim CC wording) -----------------------------

    @staticmethod
    def _format_result(answers: dict[str, str]) -> str:
        parts = [f'"{q}"="{a}"' for q, a in answers.items()]
        return (
            "User has answered your questions: "
            + ", ".join(parts)
            + ". You can now continue with the user's answers in mind."
        )




