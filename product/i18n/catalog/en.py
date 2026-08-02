#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Human-English display catalog.

The English *human* wording (distinct from the model-facing English of
the model-facing prompt owners). Count-bearing entries use ICU ``plural`` so English gets its
``one``/``other`` inflection (``1 line`` vs ``2 lines``) that the Chinese catalog
does not need — the same message id renders correctly in either language.
"""

from __future__ import annotations

from typing import Dict

from mote.product.i18n import keys as K

CATALOG: Dict[str, str] = {
    # Status bar
    K.STATUS_IDLE: "ready",
    K.STATUS_IDLE_HINT: "ctrl+x to delete chat",
    K.STATUS_THINKING: "Thinking",
    K.STATUS_VERB_THINKING: "Thinking",
    K.STATUS_VERB_PROCESSING: "Processing",
    K.STATUS_VERB_WORKING: "Working",
    K.STATUS_VERB_BUILDING: "Building",
    K.STATUS_VERB_REASONING: "Reasoning",
    K.STATUS_VERB_PONDERING: "Pondering",
    # Retry
    K.RETRY_FAILED: "LLM request failed ({error_type})",
    K.RETRY_ATTEMPT: "retry {attempt}/{total}",
    K.RETRY_COUNTDOWN: "retrying in {secs}s…",
    # Compaction
    K.COMPACT_COMPACTED: "Conversation compacted",
    K.COMPACT_KEPT: "{count, plural, one{kept # message} other{kept # messages}}",
    # Fold / truncation
    K.FOLD_FULL_REF: "output too large, truncated; full at {ref}",
    K.FOLD_HIDDEN_LINES: "… +{count, plural, one{# line} other{# lines}} folded",
    K.FOLD_CONTENT: "… content folded",
    K.FOLD_MORE_LINES: "… +{count, plural, one{# line} other{# lines}}",
    # Collapsed search/read group
    K.GROUP_SEARCH: "searched {count, plural, one{# pattern} other{# patterns}}",
    K.GROUP_READ: "read {count, plural, one{# file} other{# files}}",
    K.LIST_SEP: ", ",
    # Per-tool result summaries
    K.SUMMARY_READ_IMAGE: "read image",
    K.SUMMARY_READ_PDF: "read PDF",
    K.SUMMARY_READ_LINES: "read {count, plural, one{# line} other{# lines}}",
    K.SUMMARY_GREP_MATCHES_FILES: (
        "found {matches, plural, one{# match} other{# matches}} " "across {files, plural, one{# file} other{# files}}"
    ),
    K.SUMMARY_GREP_MATCHES: "found {count, plural, one{# match} other{# matches}}",
    K.SUMMARY_FOUND_FILES: "found {count, plural, one{# file} other{# files}}",
    K.SUMMARY_NO_MATCHES: "no matches",
    K.SUMMARY_NO_FILES: "no files matched",
    K.SUMMARY_CREATED_LINES: "created {count, plural, one{# line} other{# lines}}",
    K.SUMMARY_UPDATED_LINES: "updated {count, plural, one{# line} other{# lines}}",
    K.SUMMARY_EDIT_ADDED_REMOVED: "updated +{added} -{removed} lines",
    K.SUMMARY_EDIT_ADDED: "updated +{count, plural, one{# line} other{# lines}}",
    K.SUMMARY_EDIT_REMOVED: "updated -{count, plural, one{# line} other{# lines}}",
    K.SUMMARY_UPDATED: "updated",
    K.SUMMARY_REPLACED: "replaced {count, plural, one{# occurrence} other{# occurrences}}",
    # Tool outcomes
    K.TOOL_REJECTED: "rejected by user",
    K.RESULT_NO_OUTPUT: "(no output)",
    K.RESULT_FAILED: "failed",
    K.RESULT_RETRYABLE: "retryable",
    # Driver
    K.DRIVER_TOOLS_LOADED: "loaded {count, plural, one{# tool} other{# tools}}"
    "{deferred, plural, =0{} other{ (# deferred)}}",
    # Prompt input
    K.PROMPT_PLACEHOLDER: "Type a message… (/help for commands)",
    # Keybinding + fold hints
    K.KEY_TOGGLE_TOOL: "expand/collapse tools",
    K.KEY_DELETE_MODE: "delete turns",
    K.KEY_EXPAND_HINT: "ctrl+o expand",
    K.KEY_COLLAPSE_HINT: "ctrl+o collapse",
    K.KEY_EXIT_HINT: "(Press Ctrl+C again to exit)",
    # React-unit delete-mode
    K.DELETE_MODE_HINT: "Delete-mode: click a turn to tick it, Enter to confirm, Esc to cancel",
    K.DELETE_BUSY: "A turn is in flight — can't enter delete-mode",
    K.DELETE_NONE: "No turns selected",
    K.DELETE_DONE: "Deleted {count} messages",
    # Approval gate
    K.APPROVAL_REQUIRED: "approval required",
    K.APPROVAL_PROCEED: "Do you want to proceed?",
    K.APPROVAL_ACTION_RUN: "run: {tool}",
    K.APPROVAL_ACTION_ESCALATE: "escalate: {tool}",
    K.APPROVAL_OPT_YES: "Yes",
    K.APPROVAL_OPT_ALWAYS: "Yes, and don\u2019t ask again for similar actions",
    K.APPROVAL_OPT_NO: "No, and tell me what to do differently (esc)",
    K.APPROVAL_OPT_NEVER: "No, and never allow this action",
    K.APPROVAL_TYPED_HINT: "[y]es / [n]o / [a]lways / [d]eny-always?",
    K.APPROVAL_REASON_ASK_RULE: "an ask rule requires confirmation",
    K.APPROVAL_REASON_DEFAULT: "this action needs your approval",
    K.APPROVAL_SUGGESTION: "always allows {rule} for the session",
    # Interactive select hints
    K.HINT_SELECT_MULTI: "Space select · Enter confirm",
    K.HINT_SELECT_SINGLE: "↑↓ select · Enter confirm",
    K.HINT_SELECT_MULTI_CANCEL: "Space select · Enter confirm · Esc cancel",
    K.HINT_SELECT_SINGLE_CANCEL: "↑↓ select · Enter confirm · Esc cancel",
    K.HINT_ESC_CANCEL: "Esc cancel",
    K.SELECT_OTHER: "Other (type your own answer)",
    K.SELECT_FREE_TEXT_PROMPT: "Type your answer:",
    K.SELECT_ANSWER_PLACEHOLDER: "Your answer…",
    K.SELECT_SUBMIT: "Submit",
    K.HANDOFF_TITLE: "Handoff · {runtime}",
    K.HANDOFF_MESSAGE_PLACEHOLDER: "Optional message to the agent",
    K.HANDOFF_COMPLETE: "Complete",
    K.HANDOFF_CANCEL: "Cancel",
    K.HANDOFF_TERMINAL_INPUT: "Type terminal input and press Enter…",
    K.HANDOFF_WINDOW_ACTIVE: "The live surface is open in a separate window.",
    # /lang command
    K.LANG_CURRENT: "Current language: {code}",
    K.LANG_AVAILABLE: "Available languages: {codes}",
    K.LANG_SWITCHED: "Language switched to {code}",
    K.LANG_UNKNOWN: "Unknown language: {code}. Available: {codes}",
}
