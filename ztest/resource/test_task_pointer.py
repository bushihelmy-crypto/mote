"""Structured task-result pointer rendering."""

import pytest

from mote.contracts.task.codec import decode_task_result_pointer
from mote.contracts.task.models import CommandName, CompletedInlineTaskResultPointer, InlineTaskOutput, TaskId
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


def test_removed_pause_pointer_fails_closed() -> None:
    with pytest.raises(ValueError, match="shape"):
        decode_task_result_pointer(
            {
                "kind": "paused",
                "task_id": "bg_5",
                "command_name": "pipeline",
                "summary": "awaiting decision",
                "reason": "waiting_for_route",
            }
        )
