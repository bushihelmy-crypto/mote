"""Deterministic rendering of structured background-task result pointers."""

from xml.sax.saxutils import escape

from mote.contracts.task.models import (
    CompletedArtifactTaskResultPointer,
    CompletedInlineTaskResultPointer,
    FailedTaskResultPointer,
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
    elif isinstance(pointer, CompletedArtifactTaskResultPointer):
        lines.extend(("<status>completed</status>", f"<result-ref>{escape(pointer.output.readable)}</result-ref>"))
    elif isinstance(pointer, FailedTaskResultPointer):
        lines.extend(("<status>failed</status>", f"<error>{escape(pointer.error.message)}</error>"))
    else:
        raise TypeError(f"unsupported task result pointer: {type(pointer).__name__}")
    lines.append("</task-result>")
    return "\n".join(lines)


__all__ = ["render_task_result_pointer"]
