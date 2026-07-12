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

# --- Driver notices --------------------------------------------------------
DRIVER_TOOLS_LOADED: Final[str] = "driver.tools_loaded"

# --- Keybinding label + fold affordance hints -----------------------------
KEY_TOGGLE_TOOL: Final[str] = "key.toggle_tool"
KEY_EXPAND_HINT: Final[str] = "key.expand_hint"
KEY_COLLAPSE_HINT: Final[str] = "key.collapse_hint"
KEY_EXIT_HINT: Final[str] = "key.exit_hint"

# --- Interactive select hints (question + approval menus, both hosts) -----
HINT_SELECT_MULTI: Final[str] = "hint.select_multi"
HINT_SELECT_SINGLE: Final[str] = "hint.select_single"
HINT_SELECT_MULTI_CANCEL: Final[str] = "hint.select_multi_cancel"
HINT_SELECT_SINGLE_CANCEL: Final[str] = "hint.select_single_cancel"
HINT_ESC_CANCEL: Final[str] = "hint.esc_cancel"

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
    DRIVER_TOOLS_LOADED,
    PROMPT_PLACEHOLDER,
    KEY_TOGGLE_TOOL,
    KEY_EXPAND_HINT,
    KEY_COLLAPSE_HINT,
    KEY_EXIT_HINT,
    HINT_SELECT_MULTI,
    HINT_SELECT_SINGLE,
    HINT_SELECT_MULTI_CANCEL,
    HINT_SELECT_SINGLE_CANCEL,
    HINT_ESC_CANCEL,
    LANG_CURRENT,
    LANG_AVAILABLE,
    LANG_SWITCHED,
    LANG_UNKNOWN,
)
