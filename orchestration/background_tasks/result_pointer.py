"""Deterministic rendering of structured background-task result pointers."""

from xml.sax.saxutils import escape

from mote.contracts.task.models import (
    CompletedInlineTaskResultPointer,
    CompletedStoredTaskResultPointer,
    FailedTaskResultPointer,
    PausedTaskResultPointer,
    TaskResultPointer,
)


def render_task_result_pointer(pointer: TaskResultPointer) -> str:
    lines = [
        "<task-result>",
        f"<task-id>{escape(pointer.task_id)}</task-id>",
        f"<command>{escape(pointer.command_name)}</command>",
        f"<summary>{escape(pointer.summary)}</summary>",
    ]
    if isinstance(pointer, CompletedInlineTaskResultPointer):
        lines.extend(("<status>completed</status>", f"<result>{escape(pointer.output.content)}</result>"))
    elif isinstance(pointer, CompletedStoredTaskResultPointer):
        lines.extend(("<status>completed</status>", f"<result-ref>{escape(pointer.output.locator)}</result-ref>"))
    elif isinstance(pointer, FailedTaskResultPointer):
        lines.extend(("<status>failed</status>", f"<error>{escape(pointer.error.message)}</error>"))
    elif isinstance(pointer, PausedTaskResultPointer):
        lines.extend(("<status>paused</status>", f"<pause-reason>{escape(pointer.reason.message)}</pause-reason>"))
    else:
        raise TypeError(f"unsupported task result pointer: {type(pointer).__name__}")
    lines.append("</task-result>")
    return "\n".join(lines)


__all__ = ["render_task_result_pointer"]
