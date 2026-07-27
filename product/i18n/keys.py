#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Message-id constants — the single source of truth for display-string identity.

Every human-facing display string that the CLI localises (``cli/view`` +
``cli/consumers``, plus the driver/terminal ports that share their wording) is
addressed by one of these dotted ``Final[str]`` ids, grouped by domain. Using
constants (not bare literals) makes the ids pyright-checkable and find-usable,
guards against typos, and — because each catalog dict is keyed by these same
constants — makes a missing translation a *test failure* (completeness test),
not a silent runtime ``⟦key⟧``.

Scope note: only the human display layer is localised. Model-facing text (prompt
builders, tool output, ``<system-reminder>``, ``common/text/*``) stays English
and is deliberately absent here.
"""
from __future__ import annotations

from typing import Final, Tuple

# --- Status bar (activity verbs / idle / thinking) ------------------------
STATUS_IDLE: Final[str] = "status.idle"
STATUS_IDLE_HINT: Final[str] = "status.idle_hint"
STATUS_THINKING: Final[str] = "status.thinking"
STATUS_VERB_THINKING: Final[str] = "status.verb.thinking"
STATUS_VERB_PROCESSING: Final[str] = "status.verb.processing"
STATUS_VERB_WORKING: Final[str] = "status.verb.working"
STATUS_VERB_BUILDING: Final[str] = "status.verb.building"
STATUS_VERB_REASONING: Final[str] = "status.verb.reasoning"
STATUS_VERB_PONDERING: Final[str] = "status.verb.pondering"

#: The rotating activity verbs, in a fixed order (a locale supplies each text).
STATUS_VERB_KEYS: Final[Tuple[str, ...]] = (
    STATUS_VERB_THINKING,
    STATUS_VERB_PROCESSING,
    STATUS_VERB_WORKING,
    STATUS_VERB_BUILDING,
    STATUS_VERB_REASONING,
    STATUS_VERB_PONDERING,
)

# --- Retry countdown (shared by the terminal + Textual hosts → no dup) ----
RETRY_FAILED: Final[str] = "retry.failed"
RETRY_ATTEMPT: Final[str] = "retry.attempt"
RETRY_COUNTDOWN: Final[str] = "retry.countdown"

# --- Compaction boundary + recap (shared by both hosts) -------------------
COMPACT_COMPACTED: Final[str] = "compact.compacted"
COMPACT_KEPT: Final[str] = "compact.kept"

# --- Fold / truncation hints ----------------------------------------------
FOLD_FULL_REF: Final[str] = "fold.full_ref"
FOLD_HIDDEN_LINES: Final[str] = "fold.hidden_lines"
FOLD_CONTENT: Final[str] = "fold.content"
FOLD_MORE_LINES: Final[str] = "fold.more_lines"

# --- Collapsed search/read group summary ----------------------------------
GROUP_SEARCH: Final[str] = "group.search"
GROUP_READ: Final[str] = "group.read"
LIST_SEP: Final[str] = "list.sep"

# --- Per-tool result summaries (the model-English → human-display seam) ---
SUMMARY_READ_IMAGE: Final[str] = "summary.read_image"
SUMMARY_READ_PDF: Final[str] = "summary.read_pdf"
SUMMARY_READ_LINES: Final[str] = "summary.read_lines"
SUMMARY_GREP_MATCHES_FILES: Final[str] = "summary.grep_matches_files"
SUMMARY_GREP_MATCHES: Final[str] = "summary.grep_matches"
SUMMARY_FOUND_FILES: Final[str] = "summary.found_files"
SUMMARY_NO_MATCHES: Final[str] = "summary.no_matches"
SUMMARY_NO_FILES: Final[str] = "summary.no_files"
SUMMARY_CREATED_LINES: Final[str] = "summary.created_lines"
SUMMARY_UPDATED_LINES: Final[str] = "summary.updated_lines"
SUMMARY_EDIT_ADDED_REMOVED: Final[str] = "summary.edit_added_removed"
SUMMARY_EDIT_ADDED: Final[str] = "summary.edit_added"
SUMMARY_EDIT_REMOVED: Final[str] = "summary.edit_removed"
SUMMARY_UPDATED: Final[str] = "summary.updated"
SUMMARY_REPLACED: Final[str] = "summary.replaced"

# --- Tool outcomes ---------------------------------------------------------
TOOL_REJECTED: Final[str] = "tool.rejected"
RESULT_NO_OUTPUT: Final[str] = "result.no_output"
RESULT_FAILED: Final[str] = "result.failed"
RESULT_RETRYABLE: Final[str] = "result.retryable"

# --- Driver notices --------------------------------------------------------
DRIVER_TOOLS_LOADED: Final[str] = "driver.tools_loaded"

# --- Keybinding label + fold affordance hints -----------------------------
KEY_TOGGLE_TOOL: Final[str] = "key.toggle_tool"
KEY_DELETE_MODE: Final[str] = "key.delete_mode"
KEY_EXPAND_HINT: Final[str] = "key.expand_hint"
KEY_COLLAPSE_HINT: Final[str] = "key.collapse_hint"
KEY_EXIT_HINT: Final[str] = "key.exit_hint"

# --- React-unit delete-mode (Textual host) --------------------------------
DELETE_MODE_HINT: Final[str] = "delete.mode_hint"
DELETE_BUSY: Final[str] = "delete.busy"
DELETE_NONE: Final[str] = "delete.none"
DELETE_DONE: Final[str] = "delete.done"

# --- Approval gate (title / prompt / options / reasons, both hosts) -------
APPROVAL_REQUIRED: Final[str] = "approval.required"
APPROVAL_PROCEED: Final[str] = "approval.proceed"
APPROVAL_ACTION_RUN: Final[str] = "approval.action.run"
APPROVAL_ACTION_ESCALATE: Final[str] = "approval.action.escalate"
APPROVAL_OPT_YES: Final[str] = "approval.opt.yes"
APPROVAL_OPT_ALWAYS: Final[str] = "approval.opt.always"
APPROVAL_OPT_NO: Final[str] = "approval.opt.no"
APPROVAL_OPT_NEVER: Final[str] = "approval.opt.never"
APPROVAL_TYPED_HINT: Final[str] = "approval.typed_hint"
APPROVAL_REASON_ASK_RULE: Final[str] = "approval.reason.ask_rule"
APPROVAL_REASON_DEFAULT: Final[str] = "approval.reason.default"
APPROVAL_SUGGESTION: Final[str] = "approval.suggestion"

# --- Interactive select hints (question + approval menus, both hosts) -----
HINT_SELECT_MULTI: Final[str] = "hint.select_multi"
HINT_SELECT_SINGLE: Final[str] = "hint.select_single"
HINT_SELECT_MULTI_CANCEL: Final[str] = "hint.select_multi_cancel"
HINT_SELECT_SINGLE_CANCEL: Final[str] = "hint.select_single_cancel"
HINT_ESC_CANCEL: Final[str] = "hint.esc_cancel"

# --- Interactive select — "Other" free-text branch (both hosts) -----------
SELECT_OTHER: Final[str] = "select.other"
SELECT_FREE_TEXT_PROMPT: Final[str] = "select.free_text_prompt"
SELECT_ANSWER_PLACEHOLDER: Final[str] = "select.answer_placeholder"
SELECT_SUBMIT: Final[str] = "select.submit"

# --- Managed Runtime handoff modal -----------------------------------------
HANDOFF_TITLE: Final[str] = "handoff.title"
HANDOFF_MESSAGE_PLACEHOLDER: Final[str] = "handoff.message_placeholder"
HANDOFF_COMPLETE: Final[str] = "handoff.complete"
HANDOFF_CANCEL: Final[str] = "handoff.cancel"
HANDOFF_TERMINAL_INPUT: Final[str] = "handoff.terminal_input"
HANDOFF_WINDOW_ACTIVE: Final[str] = "handoff.window_active"

# --- Prompt input ----------------------------------------------------------
PROMPT_PLACEHOLDER: Final[str] = "prompt.placeholder"

# --- /lang command ---------------------------------------------------------
LANG_CURRENT: Final[str] = "lang.current"
LANG_AVAILABLE: Final[str] = "lang.available"
LANG_SWITCHED: Final[str] = "lang.switched"
LANG_UNKNOWN: Final[str] = "lang.unknown"


#: Every id above (the completeness test asserts each catalog covers this set).
ALL_KEYS: Final[Tuple[str, ...]] = (
    STATUS_IDLE,
    STATUS_IDLE_HINT,
    STATUS_THINKING,
    STATUS_VERB_THINKING,
    STATUS_VERB_PROCESSING,
    STATUS_VERB_WORKING,
    STATUS_VERB_BUILDING,
    STATUS_VERB_REASONING,
    STATUS_VERB_PONDERING,
    RETRY_FAILED,
    RETRY_ATTEMPT,
    RETRY_COUNTDOWN,
    COMPACT_COMPACTED,
    COMPACT_KEPT,
    FOLD_FULL_REF,
    FOLD_HIDDEN_LINES,
    FOLD_CONTENT,
    FOLD_MORE_LINES,
    GROUP_SEARCH,
    GROUP_READ,
    LIST_SEP,
    SUMMARY_READ_IMAGE,
    SUMMARY_READ_PDF,
    SUMMARY_READ_LINES,
    SUMMARY_GREP_MATCHES_FILES,
    SUMMARY_GREP_MATCHES,
    SUMMARY_FOUND_FILES,
    SUMMARY_NO_MATCHES,
    SUMMARY_NO_FILES,
    SUMMARY_CREATED_LINES,
    SUMMARY_UPDATED_LINES,
    SUMMARY_EDIT_ADDED_REMOVED,
    SUMMARY_EDIT_ADDED,
    SUMMARY_EDIT_REMOVED,
    SUMMARY_UPDATED,
    SUMMARY_REPLACED,
    TOOL_REJECTED,
    RESULT_NO_OUTPUT,
    RESULT_FAILED,
    RESULT_RETRYABLE,
    DRIVER_TOOLS_LOADED,
    PROMPT_PLACEHOLDER,
    KEY_TOGGLE_TOOL,
    KEY_DELETE_MODE,
    KEY_EXPAND_HINT,
    KEY_COLLAPSE_HINT,
    KEY_EXIT_HINT,
    DELETE_MODE_HINT,
    DELETE_BUSY,
    DELETE_NONE,
    DELETE_DONE,
    APPROVAL_REQUIRED,
    APPROVAL_PROCEED,
    APPROVAL_ACTION_RUN,
    APPROVAL_ACTION_ESCALATE,
    APPROVAL_OPT_YES,
    APPROVAL_OPT_ALWAYS,
    APPROVAL_OPT_NO,
    APPROVAL_OPT_NEVER,
    APPROVAL_TYPED_HINT,
    APPROVAL_REASON_ASK_RULE,
    APPROVAL_REASON_DEFAULT,
    APPROVAL_SUGGESTION,
    HINT_SELECT_MULTI,
    HINT_SELECT_SINGLE,
    HINT_SELECT_MULTI_CANCEL,
    HINT_SELECT_SINGLE_CANCEL,
    HINT_ESC_CANCEL,
    SELECT_OTHER,
    SELECT_FREE_TEXT_PROMPT,
    SELECT_ANSWER_PLACEHOLDER,
    SELECT_SUBMIT,
    HANDOFF_TITLE,
    HANDOFF_MESSAGE_PLACEHOLDER,
    HANDOFF_COMPLETE,
    HANDOFF_CANCEL,
    HANDOFF_TERMINAL_INPUT,
    HANDOFF_WINDOW_ACTIVE,
    LANG_CURRENT,
    LANG_AVAILABLE,
    LANG_SWITCHED,
    LANG_UNKNOWN,
)
