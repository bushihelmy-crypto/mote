"""Pure builder for a persisted background-task push-once result pointer.

A push-once task result (a graph terminal, an agent-spawn summary, or a
resumable pause marker) must survive history compaction: the live
``BackgroundTaskNotification`` that first surfaced it can be summarized/dropped
by autocompact, after which the result — which the model was expected to consume
— would vanish from context, leaving only a disk file the model is no longer
reminded of. Registering the result as a ``task_result`` ResourceUnit lets the
ResourceRegistry re-project this compact pointer right after the summary.

This module builds only the pointer *text*; it holds no task-subsystem
references and no I/O. Keeping it here (rather than in ``registry.py``) means the
registry stays free of task vocabulary — it just projects opaque unit bodies.
"""
from __future__ import annotations

from xml.sax.saxutils import escape as _escape_xml

from mote.contracts.schema import PERSISTED_OUTPUT_OPEN_TAG

# A large task result is persisted whole and the inline text becomes a
# ``<persisted-output>`` envelope carrying "Full output saved to: {path}". We
# surface that path as its own pointer element so a re-projected pointer keeps
# a durable handle to the full value, not just a preview blob.
_SAVED_TO_MARKER = "Full output saved to: "


def _extract_persisted_path(result: str) -> str | None:
    """Pull the on-disk path out of a ``<persisted-output>`` envelope, if any."""
    for line in result.splitlines():
        idx = line.find(_SAVED_TO_MARKER)
        if idx != -1:
            return line[idx + len(_SAVED_TO_MARKER) :].strip()
    return None


def build_task_result_pointer(
    *,
    task_id: str,
    command_name: str,
    status: str,
    summary: str,
    result: str | None = None,
    result_file: str | None = None,
    output_path: str | None = None,
) -> str:
    """Build the compact ``<task-result>`` pointer re-projected after compaction.

    Shape by size:

    - Small result → inlined under ``<result>`` (the model can act in one shot).
    - Large result → a ``<result-file>`` path (+ ``<output-path>`` process log)
      and a hint to Read the file, since the full value lives on disk. A large
      *result* is detected either from an explicit ``result_file`` or from a
      ``<persisted-output>`` envelope in ``result`` (whose embedded path is
      lifted out).
    - Pause marker (no result) → header + summary only; a ``<resume-hint>`` is
      appended so the model knows the task is resumable, not finished.

    Pure text builder — every dynamic field is XML-escaped; no I/O, no
    task-subsystem imports.
    """
    lines = [
        "<task-result>",
        f"<task-id>{_escape_xml(task_id)}</task-id>",
        f"<command>{_escape_xml(command_name)}</command>",
        f"<status>{_escape_xml(status)}</status>",
        f"<summary>{_escape_xml(summary)}</summary>",
    ]

    # Resolve the large-result file path: explicit override wins, else lift it
    # out of a persisted-output envelope carried on ``result``.
    file_path = result_file
    if file_path is None and result and result.startswith(PERSISTED_OUTPUT_OPEN_TAG):
        file_path = _extract_persisted_path(result)

    if file_path is not None:
        lines.append(f"<result-file>{_escape_xml(file_path)}</result-file>")
        lines.append("<hint>Full result too large to inline; Read the result-file above " "to recover it.</hint>")
    elif result:
        lines.append(f"<result>{_escape_xml(result)}</result>")

    if output_path is not None:
        lines.append(f"<output-path>{_escape_xml(output_path)}</output-path>")

    if result is None and result_file is None:
        # No produced value: this is a resumable pause marker, not a terminal.
        lines.append(
            "<resume-hint>Task is paused awaiting a decision — inspect with "
            "GetNodeState, then resume_tasks (or cancel_tasks). Not yet "
            "finished.</resume-hint>"
        )

    lines.append("</task-result>")
    return "\n".join(lines)


__all__ = ["build_task_result_pointer"]
