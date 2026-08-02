#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ``AgentEvent → ViewEvent`` fold (the 窄腰).

Two layers: the **pure** ``ViewProjector.project`` (no I/O, the unit of truth)
and the ``BaseProjector`` plumbing that routes the fold through each consumer's
capability adapter. The fold's stateful streaming bookkeeping (start-on-first-
delta, ``streamed`` stamping on completion) is the trickiest contract, so it gets
the most coverage.
"""

from __future__ import annotations

import pytest

from mote.contracts.events.model import LLMStreamCommittedEvent, LLMStreamDeltaEvent, LLMStreamDiscardedEvent
from mote.contracts.events.output import OutputCommittedEvent, OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.contracts.events.session import RuntimeDurabilityChangedEvent
from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.presentation.events import (
    AttemptStreamDiscarded,
    Capabilities,
    ConversationCompacted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    RuntimeDurabilityStatus,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
)
from mote.product.presentation.projection.base import BaseProjector
from mote.product.presentation.projection.projector import ViewProjector
from mote.ztest.artifact_fakes import artifact_media

from .conftest import (
    RecordingConsumer,
    ev_budget,
    ev_compaction,
    ev_delta,
    ev_error,
    ev_message,
    ev_post_tool,
    ev_progress,
    ev_stream_end,
    ev_system_reminder,
    ev_tool_started,
)

# --------------------------------------------------------------------------
# Pure fold: ViewProjector.project
# --------------------------------------------------------------------------


def test_first_delta_opens_block_then_passes_through():
    p = ViewProjector()
    out = p.project(ev_delta("Hel"))
    assert isinstance(out[0], MessageBlockStarted)
    assert isinstance(out[1], MessageBlockDelta)
    assert out[1].text == "Hel"
    # A second delta does NOT re-open the block.
    out2 = p.project(ev_delta("lo"))
    assert len(out2) == 1
    assert isinstance(out2[0], MessageBlockDelta)
    assert out2[0].text == "lo"


def test_empty_delta_is_dropped():
    p = ViewProjector()
    assert p.project(ev_delta("")) == []


def test_runtime_durability_change_projects_to_human_status():
    out = ViewProjector().project(
        RuntimeDurabilityChangedEvent(
            runtime_id="runtime-1",
            runtime_kind="canvas",
            alias="main",
            state="lagging",
            current_revision=4,
            recoverable_revision=2,
            detail="OSError: unavailable",
        )
    )

    assert len(out) == 1
    assert isinstance(out[0], RuntimeDurabilityStatus)
    assert out[0].runtime_kind == "canvas"
    assert out[0].current_revision == 4
    assert out[0].recoverable_revision == 2


def test_streamed_message_completes_with_streamed_true():
    p = ViewProjector()
    p.project(ev_delta("hi"))  # opens the block, sets _streaming
    p.project(ev_stream_end())  # emits nothing, keeps _streaming
    out = p.project(ev_message("assistant", "hi there"))
    assert len(out) == 1
    assert isinstance(out[0], MessageBlockCompleted)
    assert out[0].streamed is True
    assert out[0].markdown == "hi there"


def test_non_streamed_assistant_message_completes_streamed_false():
    p = ViewProjector()
    out = p.project(ev_message("assistant", "no stream"))
    assert len(out) == 1
    assert out[0].streamed is False
    assert out[0].markdown == "no stream"


def test_empty_non_streamed_assistant_message_is_dropped():
    p = ViewProjector()
    assert p.project(ev_message("assistant", "   ")) == []


def test_non_assistant_message_dropped_and_resets_streaming():
    p = ViewProjector()
    p.project(ev_delta("partial"))  # _streaming = True
    # A user/tool message is not human-view material; it also clears _streaming.
    assert p.project(ev_message("user", "echo")) == []
    # Now a fresh assistant message with no deltas is treated as non-streamed.
    out = p.project(ev_message("assistant", "fresh"))
    assert out[0].streamed is False


def test_plain_user_message_still_dropped():
    # A human's own typed prompt is a plain user message (not a <system-reminder>
    # envelope); it stays on the drop path — the driver renders it separately.
    p = ViewProjector()
    assert p.project(ev_message("user", "hello there")) == []


def test_system_reminder_user_message_folds_to_summary():
    # The framework's per-turn <system-reminder> block (written to history as a
    # user message) folds to a SystemReminder carrying only the block headings.
    p = ViewProjector()
    inner = "# Git status\nbranch main, 2 files dirty\n\n" "# Files changed on disk\n- a.py\n- b.py"
    out = p.project(ev_system_reminder(inner))
    assert len(out) == 1
    assert isinstance(out[0], SystemReminder)
    # Tags stripped, bodies dropped, headings joined with ·.
    assert out[0].text == "Git status · Files changed on disk"
    assert "<system-reminder>" not in out[0].text
    assert "branch main" not in out[0].text


def test_system_reminder_falls_back_to_first_line_without_heading():
    # A block with no ``# heading`` uses its first non-empty line as the summary.
    p = ViewProjector()
    out = p.project(ev_system_reminder("just a note with no heading"))
    assert isinstance(out[0], SystemReminder)
    assert out[0].text == "just a note with no heading"


def test_empty_system_reminder_folds_to_nothing():
    # An empty envelope yields no headings → no event (nothing to show).
    p = ViewProjector()
    assert p.project(ev_system_reminder("")) == []


def test_system_reminder_skill_table_heading_carries_count():
    # A skill-listing block (markdown table) → its heading gains the skill count,
    # skipping the table header + ``|---|`` separator rows.
    p = ViewProjector()
    inner = (
        "## Available Skills\n"
        "The following Skills are available. Invoke one with\n"
        '`Skill(name="<skill>", arguments="...")` when relevant.\n\n'
        "| Skill | Description | Arguments |\n"
        "|-------|-------------|-----------|\n"
        "| simplify | review code | |\n"
        "| asset-inventory | inventory assets | |\n"
        "| fundamental | qualitative analysis | |"
    )
    out = p.project(ev_system_reminder(inner))
    assert isinstance(out[0], SystemReminder)
    assert out[0].text == "Available Skills (3)"


def test_system_reminder_skill_bullets_counted():
    # Skill activation (and the tier-2 name-only listing) render as ``- `` bullets;
    # each bullet is one skill.
    p = ViewProjector()
    inner = (
        "# Relevant Skills\n"
        "These Skills match files you are working with.\n\n"
        "- alpha: does a thing (use when: x) [args: foo]\n"
        "- beta: does another"
    )
    out = p.project(ev_system_reminder(inner))
    assert out[0].text == "Relevant Skills (2)"


def test_system_reminder_non_skill_bullets_not_counted():
    # A non-skill block's bullets (e.g. changed files) must NOT gain a count.
    p = ViewProjector()
    inner = "# Files changed on disk\n- a.py\n- b.py"
    out = p.project(ev_system_reminder(inner))
    assert out[0].text == "Files changed on disk"


def test_system_reminder_deferred_tools_heading_lists_names():
    # The deferred-tool menu block ("# Additional tools …") lists the tool NAMES
    # (not a count) so the human sees exactly what is search-to-enable — mirroring
    # how git/skill blocks surface their contents. Names come before the first
    # ``:`` in each ``- name: desc`` bullet; the intro prose is ignored.
    p = ViewProjector()
    inner = (
        "# Additional tools (search to enable)\n"
        "These tools exist but are not loaded. Call SearchTools(query=...) to reveal them.\n"
        "- ConvertImage: Convert an image between formats.\n"
        "- WebSearch: Search the web.\n"
        "- RunGraph: Orchestrate a workflow."
    )
    out = p.project(ev_system_reminder(inner))
    assert isinstance(out[0], SystemReminder)
    assert out[0].text == "Additional tools (search to enable): ConvertImage, WebSearch, RunGraph"


def test_system_reminder_split_tool_menu_lists_names():
    # The split-path menu ("# Additional tools") uses the same ``- name: desc``
    # bullets, so it lists names identically.
    p = ViewProjector()
    inner = (
        "# Additional tools\n"
        "These tools are callable but only a hint is shown.\n"
        "- Terminal: Run a shell command.\n"
        "- Jupyter: Execute code in a notebook."
    )
    out = p.project(ev_system_reminder(inner))
    assert out[0].text == "Additional tools: Terminal, Jupyter"


def test_pre_tool_headline_and_body():
    p = ViewProjector()
    # A whole-file write = Edit with an empty old_string; new_string is the body.
    out = p.project(ev_tool_started("Edit", {"file_path": "a.py", "old_string": "", "new_string": "print(1)\n"}))
    assert len(out) == 1
    started = out[0]
    assert isinstance(started, ToolCallStarted)
    assert started.tool_name == "Edit"
    assert started.headline == "a.py"
    assert started.body == "print(1)\n"  # body kept verbatim (only line-count truncated)
    assert started.lexer == "python"  # inferred from .py
    assert started.tool_use_id == "tu-1"


def test_pre_tool_bash_has_body_no_headline():
    p = ViewProjector()
    out = p.project(ev_tool_started("Bash", {"command": "ls -la"}))
    started = out[0]
    assert started.headline == ""
    assert started.body == "ls -la"
    assert started.lexer == "bash"


def test_pre_tool_ask_user_question_suppressed():
    p = ViewProjector()
    assert p.project(ev_tool_started("AskUserQuestion", {"questions": []})) == []


def test_unknown_tool_shows_plain_key_value_body():
    p = ViewProjector()
    out = p.project(ev_tool_started("MysteryTool", {"x": 1, "y": "z"}))
    started = out[0]
    # Plain "key: value" lines — no JSON braces, no json lexer.
    assert started.lexer is None
    assert started.body == "x: 1\ny: z"
    assert "{" not in started.body


def test_post_tool_success_summary_first_nonempty_line():
    p = ViewProjector()
    out = p.project(ev_post_tool("Read", "\n\nfirst line\nsecond line"))
    done = out[0]
    assert isinstance(done, ToolCallCompleted)
    assert done.ok is True
    assert done.summary == "first line"
    # A multi-line body now ships a word-bounded preview as the detail (leading
    # blank lines stripped), not just the one-line summary.
    assert done.detail == "first line\nsecond line"
    assert done.content_truncated is True


def test_post_tool_short_output_no_detail_no_truncation():
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "done"))
    done = out[0]
    assert done.summary == "done"
    # Single line fully shown by the summary → no redundant detail, not folded.
    assert done.detail is None
    assert done.content_truncated is False


def test_post_tool_preview_caps_at_100_words():
    p = ViewProjector()
    body = " ".join(f"w{i}" for i in range(250))
    out = p.project(ev_post_tool("Bash", body))
    done = out[0]
    assert done.detail is not None
    assert len(done.detail.split()) == 100
    assert done.detail.split()[0] == "w0"
    assert done.detail.split()[-1] == "w99"
    assert done.content_truncated is True


def test_post_tool_diff_reports_hidden_line_count():
    """A folded diff carries the exact count of dropped lines (no inline marker)."""
    p = ViewProjector()
    body = "--- a\n+++ b\n" + "\n".join(f"+line{i}" for i in range(60))
    out = p.project(ev_post_tool("Bash", body))
    done = out[0]
    assert done.content_truncated is True
    # 62 body lines, capped at _MAX_DETAIL_LINES (40) → 22 hidden.
    assert done.hidden_lines == 22
    # The detail body is clean — the "N more lines" marker moved out to the count.
    assert "more lines" not in done.detail


def test_post_tool_short_output_no_hidden_lines():
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "done"))
    assert out[0].hidden_lines == 0


# --------------------------------------------------------------------------
# per-tool count summaries ("读取 N 行" / "找到 N 个文件" …)
# --------------------------------------------------------------------------


def _numbered(n: int) -> str:
    """Render *n* lines in Read's ``     i→content`` numbered-body format."""
    return "\n".join(f"{i:>6}\u2192line{i}" for i in range(1, n + 1))


def test_summary_read_counts_numbered_lines():
    p = ViewProjector()
    out = p.project(ev_post_tool("Read", _numbered(42), success=True))
    assert out[0].summary == t(K.SUMMARY_READ_LINES, count=42)


def test_summary_read_image_and_pdf():
    p = ViewProjector()
    img = p.project(ev_post_tool("Read", "Read image /a.png (png, 100 bytes; x). Shown below.", success=True))
    assert img[0].summary == t(K.SUMMARY_READ_IMAGE)
    pdf = p.project(ev_post_tool("Read", "Read PDF /a.pdf (100 bytes). Shown below.", success=True))
    assert pdf[0].summary == t(K.SUMMARY_READ_PDF)


def test_summary_search_files_mode():
    p = ViewProjector()
    out = p.project(ev_post_tool("Search", "Found 3 files\n/a:1\n/b:2\n/c:3", success=True))
    assert out[0].summary == t(K.SUMMARY_FOUND_FILES, count=3)


def test_summary_search_count_mode():
    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Search",
            "/a:5\n/b:2\n\nFound 7 total occurrences across 2 files",
            success=True,
        )
    )
    assert out[0].summary == t(K.SUMMARY_GREP_MATCHES_FILES, matches=7, files=2)


def test_summary_search_content_mode_counts_lines():
    p = ViewProjector()
    out = p.project(ev_post_tool("Search", "/a:1:foo\n/a:5:bar\n/b:3:baz", success=True))
    assert out[0].summary == t(K.SUMMARY_GREP_MATCHES, count=3)


def test_summary_search_no_match():
    p = ViewProjector()
    out = p.project(ev_post_tool("Search", "No matches found", success=True))
    assert out[0].summary == t(K.SUMMARY_NO_MATCHES)


def test_summary_search_no_files():
    p = ViewProjector()
    out = p.project(ev_post_tool("Search", "No files found", success=True))
    assert out[0].summary == t(K.SUMMARY_NO_FILES)


def test_summary_search_counts_paths_dropping_truncation_note():
    p = ViewProjector()
    body = "/a.py\n/b.py\n(Results are truncated. Consider using a more specific path or pattern.)"
    out = p.project(ev_post_tool("Search", body, success=True))
    assert out[0].summary == t(K.SUMMARY_GREP_MATCHES, count=2)


def test_summary_whole_file_write_created_and_overwritten():
    # A whole-file write is an Edit with an empty ``old``: all-additions reads
    # as "created N lines"; an overwrite carries both old and new.
    from types import SimpleNamespace

    p = ViewProjector()
    fc_create = [SimpleNamespace(path="/a.py", old="", new="x\n" * 42)]
    created = p.project(
        ev_post_tool(
            "Edit",
            "The file /a.py has been created successfully.",
            success=True,
            file_changes=fc_create,
        )
    )
    assert created[0].summary == t(K.SUMMARY_CREATED_LINES, count=42)
    fc_overwrite = [SimpleNamespace(path="/a.py", old="a\nb\nc\n", new="X\nY\n")]
    updated = p.project(
        ev_post_tool(
            "Edit",
            "The file /a.py has been updated successfully.",
            success=True,
            file_changes=fc_overwrite,
        )
    )
    assert updated[0].summary == t(K.SUMMARY_EDIT_ADDED_REMOVED, added=2, removed=3)


def test_summary_edit_reports_added_removed_from_file_changes():
    p = ViewProjector()
    from types import SimpleNamespace

    fc = [SimpleNamespace(path="/a.py", old="a\nb\nc\n", new="a\nX\nc\nd\n")]
    out = p.project(
        ev_post_tool(
            "Edit",
            "The file /a.py has been updated successfully.",
            success=True,
            file_changes=fc,
        )
    )
    assert out[0].summary == t(K.SUMMARY_EDIT_ADDED_REMOVED, added=2, removed=1)


def test_summary_edit_create_is_all_additions():
    p = ViewProjector()
    from types import SimpleNamespace

    fc = [SimpleNamespace(path="/n.py", old="", new="x\ny\nz\n")]
    out = p.project(
        ev_post_tool(
            "Edit",
            "The file /n.py has been created successfully.",
            success=True,
            file_changes=fc,
        )
    )
    assert out[0].summary == t(K.SUMMARY_CREATED_LINES, count=3)


def test_summary_bash_falls_back_to_first_line():
    # Bash has no honest count → keep the raw first output line.
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "hello world\nsecond line", success=True))
    assert out[0].summary == "hello world"


def test_compaction_folds_to_conversation_compacted():
    """CONTEXT_COMPACTED surfaces as a ConversationCompacted boundary marker."""
    p = ViewProjector()
    out = p.project(
        ev_compaction(
            summary="recap of earlier turns",
            model_context_messages=[1, 2, 3, 4],
        )
    )
    assert len(out) == 1
    ev = out[0]
    assert isinstance(ev, ConversationCompacted)
    assert ev.summary == "recap of earlier turns"
    assert ev.message_count == 4


def test_history_edited_folds_to_nothing_no_compaction_marker():
    """A durable react-unit delete MUST NOT surface a
    ``ConversationCompacted`` boundary marker. The projector ignores the source
    ``HistoryEditedEvent`` by construction (unknown name → ``[]``), so the delete
    silently prunes history with no "conversation compacted" UI."""
    from mote.contracts.events.conversation import HISTORY_EDITED

    from .conftest import AgentEvt

    p = ViewProjector()
    ev = AgentEvt(
        HISTORY_EDITED,
        remaining_messages=[1, 2, 3],
        reason="delete",
    )
    assert p.project(ev) == []


def test_post_tool_failure_read_from_structured_success():
    """When the event carries ``success=False``, that fact drives ``ok`` — no sniff."""
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "boom", success=False))
    done = out[0]
    assert done.ok is False
    assert "boom" in done.summary


def test_post_tool_failure_fills_structured_error_fields():
    """A failed call carrying an ``ErrorReport`` bleeds its facts onto the completion.

    The projector reads code/type/retryable/recovery off ``event.error`` as flat
    scalars (never importing the exception type) so a host can render machine-
    reasonable failure facts alongside the plain-text summary.
    """
    from mote.contracts.foundation.errors.report import ErrorReport

    report = ErrorReport(
        error="PermissionError",
        code="TOOL_PERMISSION_DENIED",
        message="",
        retryable=False,
        recovery="abort",
    )
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "denied", success=False, error=report))
    done = out[0]
    assert done.ok is False
    assert done.error_type == "PermissionError"
    assert done.error_code == "TOOL_PERMISSION_DENIED"
    assert done.retryable is False
    assert done.recovery == "abort"


def test_post_tool_failure_prefers_report_message_over_raw_error_xml():
    """The CLI shows the report's clean human message, never the ``<error …>`` XML.

    The executor hands the LLM a ``render_error_block`` wrapper (``<error …>…\
</error>``) as ``tool_response``; the projector must instead surface the
    structured ``ErrorReport.message`` so the machine-facing XML never leaks onto
    the human transcript.
    """
    from mote.contracts.foundation.errors.report import ErrorReport

    report = ErrorReport(
        error="PermissionError",
        code="TOOL_PERMISSION_DENIED",
        retryable=False,
        recovery="abort",
        message="Permission to run 'Bash' was denied",
    )
    xml = '<error code="tool.permission_denied" retryable="false">\nPermission to run \'Bash\' was denied\n</error>'
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", xml, success=False, error=report))
    done = out[0]
    assert done.summary == "Permission to run 'Bash' was denied"
    assert "<error" not in done.summary


def test_post_tool_failure_without_error_leaves_fields_empty():
    """A failure with no structured ``error`` degrades to empty fields (summary stays)."""
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "boom", success=False))
    done = out[0]
    assert done.ok is False
    assert "boom" in done.summary
    assert (done.error_type, done.error_code, done.recovery) == ("", "", "")
    assert done.retryable is False


def test_post_tool_success_output_starting_with_error_not_misjudged():
    """A successful output beginning with ``Error:`` must NOT be judged failed.

    This is the semantic bug the structured ``success`` field fixes: the legacy
    prefix heuristic would flag this as a failure, but ``success=True`` is the
    executor's actual fact (``from_tool_return`` treats any string as success).
    """
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "Error: this is normal output", success=True))
    done = out[0]
    assert done.ok is True


def test_post_tool_empty_output_summary():
    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", ""))
    assert out[0].summary == t(K.RESULT_NO_OUTPUT)


def test_post_tool_media_block_from_structured_image():
    """A structured image artifact folds into a ``MediaBlock`` (no text sniffing).

    The executor mirrors the ToolResult's image (with its local ``ref`` path) onto
    ``event.media``; the projector resolves the path and emits the block directly,
    independent of the ``tool_response`` text.
    """
    from mote.product.presentation.events import MediaBlock
    from mote.runtime.tools.tool_result import ToolMedia

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Read",
            "Read image /tmp/pic.png (png, 42 bytes). Shown below.",
            tool_input={"file_path": "/tmp/pic.png"},
            media=[artifact_media("image", ref="/tmp/pic.png")],
        )
    )
    assert isinstance(out[0], ToolCallCompleted)
    media = [e for e in out if isinstance(e, MediaBlock)]
    assert len(media) == 1
    assert media[0].media_kind == "image"
    assert media[0].ref == "/tmp/pic.png"
    assert media[0].alt == "pic.png"
    assert media[0].tool_use_id == "tu-1"


def test_post_tool_media_block_from_structured_pdf():
    """A structured PDF artifact folds into a ``MediaBlock(media_kind='pdf')``.

    This is the P1 gap the old sniff couldn't cover — visual PDF reads now render.
    """
    from mote.product.presentation.events import MediaBlock
    from mote.runtime.tools.tool_result import ToolMedia

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Read",
            "Read PDF /tmp/doc.pdf (9000 bytes). Shown below.",
            tool_input={"file_path": "/tmp/doc.pdf"},
            media=[artifact_media("pdf", ref="/tmp/doc.pdf")],
        )
    )
    media = [e for e in out if isinstance(e, MediaBlock)]
    assert len(media) == 1
    assert media[0].media_kind == "pdf"
    assert media[0].ref == "/tmp/doc.pdf"


def test_post_tool_media_empty_list_emits_no_block():
    """An explicit empty ``media`` list is authoritative: no block, no sniffing.

    Even when the output *looks* like an image read, a present-but-empty media
    field is the structured fact ("this result carries no media") and wins over
    the legacy prefix heuristic.
    """
    from mote.product.presentation.events import MediaBlock

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Read",
            "Read image /tmp/pic.png (png, 42 bytes). Shown below.",
            tool_input={"file_path": "/tmp/pic.png"},
            media=[],
        )
    )
    assert not any(isinstance(e, MediaBlock) for e in out)


def test_post_tool_media_ref_without_path_degrades():
    """A structured artifact with an empty ``ref`` (bytes-only, e.g. a screenshot)
    still emits a block, degrading ``alt`` to the media kind for a text host."""
    from mote.product.presentation.events import MediaBlock
    from mote.runtime.tools.tool_result import ToolMedia

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "web_browser",
            "[screenshot of the active tab; shown below]",
            media=[artifact_media("image", ref="")],
        )
    )
    media = [e for e in out if isinstance(e, MediaBlock)]
    assert len(media) == 1
    assert media[0].ref == ""
    assert media[0].alt == "image"


def test_post_tool_file_diff_block_from_structured_change():
    """A structured ``FileChange`` folds into a ``FileDiffBlock`` (no text sniff).

    Edit mirrors the ToolResult's ``file_changes`` onto the event; the
    projector resolves the path and carries the ``old``/``new`` facts directly,
    independent of the tool_response text (which says "updated successfully", not a
    diff). The ToolCallCompleted still rides alongside.
    """
    from mote.product.presentation.events import FileDiffBlock
    from mote.runtime.tools.tool_result import FileChange

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Edit",
            "The file /tmp/a.py has been updated successfully.",
            tool_input={"file_path": "/tmp/a.py"},
            file_changes=[FileChange(path="/tmp/a.py", old="x = 1\n", new="x = 2\n")],
        )
    )
    assert isinstance(out[0], ToolCallCompleted)
    diffs = [e for e in out if isinstance(e, FileDiffBlock)]
    assert len(diffs) == 1
    assert diffs[0].path == "/tmp/a.py"
    assert diffs[0].old == "x = 1\n"
    assert diffs[0].new == "x = 2\n"
    assert diffs[0].tool_use_id == "tu-1"


def test_post_tool_multiple_file_changes_fold_to_multiple_blocks():
    """A tool may touch several files → one ``FileDiffBlock`` per change."""
    from mote.product.presentation.events import FileDiffBlock
    from mote.runtime.tools.tool_result import FileChange

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Edit",
            "A /tmp/new.py\nM /tmp/mod.py\nD /tmp/gone.py",
            file_changes=[
                FileChange(path="/tmp/new.py", old="", new="hello\n"),  # add
                FileChange(path="/tmp/mod.py", old="a\n", new="b\n"),  # update
                FileChange(path="/tmp/gone.py", old="bye\n", new=""),  # delete
            ],
        )
    )
    diffs = [e for e in out if isinstance(e, FileDiffBlock)]
    assert len(diffs) == 3
    assert [d.path for d in diffs] == ["/tmp/new.py", "/tmp/mod.py", "/tmp/gone.py"]
    # Creation carries empty old, deletion carries empty new.
    assert diffs[0].old == "" and diffs[0].new == "hello\n"
    assert diffs[2].old == "bye\n" and diffs[2].new == ""


def test_post_tool_no_file_changes_emits_no_diff_block():
    """A tool with no structured change (Bash) emits no ``FileDiffBlock``.

    Bash's diff-shaped text still falls to the ``_looks_like_diff`` path in the
    completed event — the structured block is only for old/new-bearing tools.
    """
    from mote.product.presentation.events import FileDiffBlock

    p = ViewProjector()
    out = p.project(ev_post_tool("Bash", "some plain output"))
    assert not any(isinstance(e, FileDiffBlock) for e in out)


def test_git_diff_text_classifies_as_diff_without_file_diff_block():
    """``git diff`` output is diff *text* → ``result_kind=diff``, no structured block.

    The text-diff classifier (``_looks_like_diff``) and the structured
    ``file_changes`` path are orthogonal: a tool whose *output* is a unified diff
    carries no ``old``/``new`` fact, so it takes the text path and emits no
    ``FileDiffBlock``.
    """
    from mote.product.presentation.events import RESULT_KIND_DIFF, FileDiffBlock

    p = ViewProjector()
    body = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"
    out = p.project(ev_post_tool("Bash", body, success=True))
    done = out[0]
    assert done.result_kind == RESULT_KIND_DIFF
    assert not any(isinstance(e, FileDiffBlock) for e in out)


def test_structured_change_emits_block_without_text_diff_classification():
    """Edit's structured change → ``FileDiffBlock``, and the completion stays plain.

    The other side of the orthogonality: Edit ships ``old``/``new`` as a
    ``FileDiffBlock`` while its ``tool_response`` ("updated successfully") is *not*
    diff text, so the completion is classified ``plain`` — the text-diff path is
    not triggered for a structured change.
    """
    from mote.product.presentation.events import RESULT_KIND_PLAIN, FileDiffBlock
    from mote.runtime.tools.tool_result import FileChange

    p = ViewProjector()
    out = p.project(
        ev_post_tool(
            "Edit",
            "The file /tmp/a.py has been updated successfully.",
            success=True,
            file_changes=[FileChange(path="/tmp/a.py", old="x = 1\n", new="x = 2\n")],
        )
    )
    done = out[0]
    assert done.result_kind == RESULT_KIND_PLAIN
    diffs = [e for e in out if isinstance(e, FileDiffBlock)]
    assert len(diffs) == 1


def test_task_progress_fold():
    p = ViewProjector()
    out = p.project(ev_progress(stage="build", status="running", detail="step 1"))
    tp = out[0]
    assert isinstance(tp, TaskProgress)
    assert (tp.stage, tp.status, tp.detail) == ("build", "running", "step 1")


def test_budget_soft_warning_folds_to_notice():
    p = ViewProjector()
    out = p.project(ev_budget(spend=8.0, limit=10.0, fraction=0.8, stopped=False))
    assert len(out) == 1
    n = out[0]
    assert isinstance(n, Notice)
    assert n.level == "warning"
    assert "80%" in n.text and "warning" in n.text.lower()


def test_budget_hard_stop_folds_to_notice():
    p = ViewProjector()
    out = p.project(ev_budget(spend=10.5, limit=10.0, fraction=1.05, stopped=True))
    assert len(out) == 1
    n = out[0]
    assert isinstance(n, Notice)
    assert n.level == "warning"
    assert "Stopping" in n.text and "$10.50" in n.text


def test_llm_error_folds_to_nothing():
    # Per-attempt LLM failures are silent: retry progress rides on RetryStatus and
    # the final failure surfaces once via the turn-level ErrorRaised path (driver).
    p = ViewProjector()
    assert p.project(ev_error(error="boom")) == []


def test_unknown_event_folds_to_nothing():
    p = ViewProjector()
    from .conftest import AgentEvt

    assert p.project(AgentEvt("session_started")) == []


# --------------------------------------------------------------------------
# BaseProjector plumbing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_projector_routes_async_handle_to_consumer():
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([consumer], projector=ViewProjector())
    await bp.handle(ev_message("assistant", "hello"))
    assert len(consumer.events) == 1
    assert isinstance(consumer.events[0], MessageBlockCompleted)


def test_base_projector_routes_sync_handle_to_consumer():
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([consumer], projector=ViewProjector())
    bp.handle_sync(ev_delta("tok"))
    # streaming consumer sees: block-started + delta
    kinds = [type(e).__name__ for e in consumer.events]
    assert kinds == ["MessageBlockStarted", "MessageBlockDelta"]


def test_nonrollback_consumer_releases_only_committed_attempt_stream():
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([consumer], projector=ViewProjector())

    bp.handle_sync(
        LLMStreamDeltaEvent(
            token="bad",
            model_call_id="call",
            attempt_id="call:1",
            sequence=1,
            provisional=True,
        )
    )
    bp.handle_sync(
        LLMStreamDiscardedEvent(
            model_call_id="call",
            attempt_id="call:1",
            chunk_count=1,
        )
    )
    assert consumer.events == []

    bp.handle_sync(
        LLMStreamDeltaEvent(
            token="good",
            model_call_id="call",
            attempt_id="call:2",
            sequence=1,
            provisional=True,
        )
    )
    bp.handle_sync(
        LLMStreamCommittedEvent(
            model_call_id="call",
            attempt_id="call:2",
            chunk_count=1,
        )
    )

    assert [type(event).__name__ for event in consumer.events] == [
        "MessageBlockStarted",
        "MessageBlockDelta",
    ]
    assert consumer.events[1].text == "good"
    assert consumer.events[1].provisional is False


def test_rollback_consumer_receives_live_delta_and_discard_terminal():
    consumer = RecordingConsumer(Capabilities(streaming=True, provisional_rollback=True))
    bp = BaseProjector([consumer], projector=ViewProjector())

    bp.handle_sync(
        LLMStreamDeltaEvent(
            token="draft",
            model_call_id="call",
            attempt_id="call:1",
            sequence=1,
            provisional=True,
        )
    )
    bp.handle_sync(
        LLMStreamDiscardedEvent(
            model_call_id="call",
            attempt_id="call:1",
            chunk_count=1,
            reason="timeout",
        )
    )

    assert [type(event).__name__ for event in consumer.events] == [
        "MessageBlockStarted",
        "MessageBlockDelta",
        "AttemptStreamDiscarded",
    ]
    assert isinstance(consumer.events[-1], AttemptStreamDiscarded)
    assert consumer.events[-1].reason == "timeout"


@pytest.mark.asyncio
async def test_base_projector_fans_out_to_multiple_consumers():
    a = RecordingConsumer(Capabilities(streaming=True))
    b = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([a, b], projector=ViewProjector())
    await bp.handle(ev_message("assistant", "x"))
    assert len(a.events) == 1 and len(b.events) == 1


@pytest.mark.asyncio
async def test_deliver_pushes_prebuilt_view_event():
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([consumer], projector=ViewProjector())
    await bp.deliver(Notice(text="hi", level="info"))
    assert consumer.events == [Notice(text="hi", level="info")]


def test_deliver_sync_pushes_prebuilt_view_event():
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp = BaseProjector([consumer], projector=ViewProjector())
    bp.deliver_sync(Notice(text="cmd output"))
    assert consumer.events[0].text == "cmd output"


def test_add_consumer_registers_with_its_capabilities():
    bp = BaseProjector(projector=ViewProjector())
    consumer = RecordingConsumer(Capabilities(streaming=True))
    bp.add_consumer(consumer)
    assert bp.consumers == [consumer]


def test_output_snapshot_lifecycle_projects_without_becoming_a_message():
    projector = ViewProjector()

    snapshot = projector.project(
        OutputSnapshotEvent(
            run_id="run-1",
            revision=1,
            schema_fingerprint="sha",
            value={"count": 7},
        )
    )[0]
    invalidated = projector.project(
        OutputSnapshotInvalidatedEvent(run_id="run-1", revision=1, reason="stream_changed")
    )[0]

    assert snapshot.kind == "output_snapshot"
    assert snapshot.value == {"count": 7}
    assert invalidated.kind == "output_snapshot_invalidated"
    assert invalidated.revision == 1


def test_committed_output_projects_as_typed_terminal_event():
    output = ViewProjector().project(
        OutputCommittedEvent(
            candidate_id="candidate",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 7},
            run_id="run-1",
            run_kind="agent",
        )
    )[0]

    assert output.kind == "output_committed"
    assert output.run_id == "run-1"
    assert output.value == {"count": 7}
