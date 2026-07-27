"""Compaction prompts.

These build the instruction the model follows when autocompact summarizes the
conversation, and the wrapper that turns the returned summary into the single
user message that replaces the summarized history.

Only the pieces autocompact needs are included:
- ``BASE_COMPACT_PROMPT`` — summarize the *whole* conversation (no tail kept).
- ``PARTIAL_COMPACT_UP_TO_PROMPT`` — summarize the earlier portion when a recent
  tail is preserved verbatim after the summary (our usual case).
- ``format_compact_summary`` — strip the ``<analysis>`` scratchpad, unwrap
  ``<summary>``.
- ``get_compact_user_summary_message`` — the continued-session preface.

The session-memory / partial-"from" variants and the cache/attachment plumbing
are out of scope here (no equivalent infrastructure in Mote yet).
"""

from __future__ import annotations

import re
from string import Template

from mote.kernel.prompt.compaction import (
    _BASE_COMPACT_BODY,
    _DETAILED_ANALYSIS_INSTRUCTION_BASE,
    _PARTIAL_UP_TO_BODY,
    NO_TOOLS_PREAMBLE,
    NO_TOOLS_TRAILER,
)


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Full-conversation summarization prompt."""
    body = Template(_BASE_COMPACT_BODY).safe_substitute(analysis=_DETAILED_ANALYSIS_INSTRUCTION_BASE)
    prompt = NO_TOOLS_PREAMBLE + body
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


def get_partial_compact_prompt(custom_instructions: str | None = None) -> str:
    """Earlier-portion summarization prompt when a recent tail is kept verbatim.

    The summary is placed BEFORE the preserved tail, so it must read as a
    self-contained prologue to messages it cannot see.
    """
    body = Template(_PARTIAL_UP_TO_BODY).safe_substitute(analysis=_DETAILED_ANALYSIS_INSTRUCTION_BASE)
    prompt = NO_TOOLS_PREAMBLE + body
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


_ANALYSIS_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_BLANKS_RE = re.compile(r"\n\n+")


def format_compact_summary(summary: str) -> str:
    """Strip the ``<analysis>`` scratchpad and unwrap ``<summary>``.

    The model drafts in ``<analysis>`` (improves quality, no lasting value) then
    writes the real summary in ``<summary>``. We drop the former and replace the
    latter's tags with a readable ``Summary:`` header. If the model omitted the
    tags entirely, the text is returned cleaned but otherwise intact.
    """
    formatted = _ANALYSIS_RE.sub("", summary)
    m = _SUMMARY_RE.search(formatted)
    if m:
        content = (m.group(1) or "").strip()
        formatted = _SUMMARY_RE.sub(f"Summary:\n{content}", formatted)
    formatted = _BLANKS_RE.sub("\n\n", formatted)
    return formatted.strip()


def get_compact_user_summary_message(
    summary: str,
    *,
    suppress_follow_up_questions: bool = False,
    transcript_path: str | None = None,
    recent_messages_preserved: bool = False,
) -> str:
    """Wrap a summary into the continued-session user message."""
    formatted = format_compact_summary(summary)
    base = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        f"conversation.\n\n{formatted}"
    )
    if transcript_path:
        base += (
            "\n\nIf you need specific details from before compaction (like exact "
            "code snippets, error messages, or content you generated), read the "
            f"full transcript at: {transcript_path}"
        )
    if recent_messages_preserved:
        base += "\n\nRecent messages are preserved verbatim."
    if suppress_follow_up_questions:
        base += (
            "\nContinue the conversation from where it left off without asking "
            "the user any further questions. Resume directly — do not acknowledge "
            "the summary, do not recap what was happening. Pick up the last task "
            "as if the break never happened."
        )
    return base
