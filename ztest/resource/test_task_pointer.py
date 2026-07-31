"""Structured task-result pointer rendering."""

import pytest

from mote.contracts.task.models import (
    CommandName,
    CompletedInlineTaskResultPointer,
    CompletedStoredTaskResultPointer,
    InlineTaskOutput,
    PausedTaskResultPointer,
    PauseReason,
    StoredTaskOutput,
    TaskId,
)
from mote.orchestration.background_tasks.result_pointer import render_task_result_pointer


def test_inline_result_is_rendered_and_escaped():
    pointer = CompletedInlineTaskResultPointer(
        TaskId("bg_3"),
        CommandName("a & b <run>"),
        "finished <now>",
        InlineTaskOutput("value < 5 & > 1"),
    )
    out = render_task_result_pointer(pointer)
    assert "<status>completed</status>" in out
    assert "a &amp; b &lt;run&gt;" in out
    assert "<result>value &lt; 5 &amp; &gt; 1</result>" in out


def test_stored_result_uses_opaque_locator():
    pointer = CompletedStoredTaskResultPointer(
        TaskId("bg_3"),
        CommandName("media"),
        "finished",
        StoredTaskOutput("task-output:session:bg_3"),
    )
    out = render_task_result_pointer(pointer)
    assert "<result-ref>task-output:session:bg_3</result-ref>" in out
    assert "/tmp/" not in out and "file://" not in out


def test_stored_result_rejects_filesystem_path():
    with pytest.raises(ValueError):
        StoredTaskOutput("/tmp/result.txt")


def test_pause_is_a_distinct_variant():
    out = render_task_result_pointer(
        PausedTaskResultPointer(
            TaskId("bg_5"),
            CommandName("pipeline"),
            "awaiting decision",
            PauseReason("waiting_for_route"),
        )
    )
    assert "<status>paused</status>" in out
    assert "<pause-reason>waiting_for_route</pause-reason>" in out
