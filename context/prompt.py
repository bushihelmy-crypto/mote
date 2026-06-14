"""Compaction prompts — ported from Claude Code ``services/compact/prompt.ts``.

These build the instruction the model follows when autocompact summarizes the
conversation, and the wrapper that turns the returned summary into the single
user message that replaces the summarized history.

Only the pieces autocompact needs are ported:
- ``BASE_COMPACT_PROMPT`` — summarize the *whole* conversation (no tail kept).
- ``PARTIAL_COMPACT_UP_TO_PROMPT`` — summarize the earlier portion when a recent
  tail is preserved verbatim after the summary (our usual case).
- ``format_compact_summary`` — strip the ``<analysis>`` scratchpad, unwrap
  ``<summary>``.
- ``get_compact_user_summary_message`` — the continued-session preface.

The session-memory / partial-"from" variants and the cache/attachment plumbing
are out of scope here (no equivalent infrastructure in MetaGPT yet).
"""

from __future__ import annotations

import re

# Aggressive no-tools preamble (CC NO_TOOLS_PREAMBLE). The summary turn must be
# plain text; a tool call would waste the single turn.
NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)

_DETAILED_ANALYSIS_INSTRUCTION_BASE = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

_BASE_COMPACT_BODY = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{analysis}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.

Wrap the structured summary in <summary> tags. Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response."""

# 'up_to': the summary will PRECEDE the kept recent messages (cache invalidated),
# so the model is told newer messages follow that it does not see here.
_PARTIAL_UP_TO_BODY = """Your task is to create a detailed summary of this conversation. This summary will be placed at the start of a continuing session; newer messages that build on this context will follow after your summary (you do not see them here). Summarize thoroughly so that someone reading only your summary and then the newer messages can fully understand what happened and continue the work.

{analysis}

Your summary should include the following sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents in detail
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List errors encountered and how they were fixed.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results.
7. Pending Tasks: Outline any pending tasks.
8. Work Completed: Describe what was accomplished by the end of this portion.
9. Context for Continuing Work: Summarize any context, decisions, or state that would be needed to understand and continue the work in subsequent messages.

Wrap the structured summary in <summary> tags. Please provide your summary following this structure, ensuring precision and thoroughness in your response."""


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Full-conversation summarization prompt (CC ``getCompactPrompt``)."""
    body = _BASE_COMPACT_BODY.format(analysis=_DETAILED_ANALYSIS_INSTRUCTION_BASE)
    prompt = NO_TOOLS_PREAMBLE + body
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


def get_partial_compact_prompt(custom_instructions: str | None = None) -> str:
    """Earlier-portion summarization prompt when a recent tail is kept verbatim.

    Ports CC ``getPartialCompactPrompt(direction='up_to')`` — the summary is
    placed BEFORE the preserved tail, so it must read as a self-contained
    prologue to messages it cannot see.
    """
    body = _PARTIAL_UP_TO_BODY.format(analysis=_DETAILED_ANALYSIS_INSTRUCTION_BASE)
    prompt = NO_TOOLS_PREAMBLE + body
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


_ANALYSIS_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_BLANKS_RE = re.compile(r"\n\n+")


def format_compact_summary(summary: str) -> str:
    """Strip the ``<analysis>`` scratchpad and unwrap ``<summary>`` (CC formatCompactSummary).

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
    """Wrap a summary into the continued-session user message (CC getCompactUserSummaryMessage)."""
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
