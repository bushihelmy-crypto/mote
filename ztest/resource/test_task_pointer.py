"""build_task_result_pointer: success / large / pause pointer shapes + escaping."""
from mote.contracts.schema import PERSISTED_OUTPUT_OPEN_TAG
from mote.runtime.resources import build_task_result_pointer


def test_small_result_inlines_result():
    out = build_task_result_pointer(
        task_id="bg_3",
        command_name="code review",
        status="success",
        summary="code review finished (success).",
        result="found 2 issues",
        output_path="/logs/bg_3.output",
    )
    assert "<task-result>" in out and "</task-result>" in out
    assert "<task-id>bg_3</task-id>" in out
    assert "<status>success</status>" in out
    assert "<result>found 2 issues</result>" in out
    assert "<output-path>/logs/bg_3.output</output-path>" in out
    # Not a pause and not large → no resume-hint / result-file.
    assert "<resume-hint>" not in out
    assert "<result-file>" not in out


def test_large_result_points_to_file_and_lifts_persisted_path():
    envelope = (
        f"{PERSISTED_OUTPUT_OPEN_TAG}\n"
        "Output too large (5 KB). Full output saved to: /ws/.tool_results/s/task-bg_3.txt\n\n"
        "Preview (first 2 KB):\nsome preview\n...\n</persisted-output>"
    )
    out = build_task_result_pointer(
        task_id="bg_3",
        command_name="media",
        status="success",
        summary="media finished (success).",
        result=envelope,
        output_path="/logs/bg_3.output",
    )
    # The persisted path is lifted out into <result-file>, not inlined raw.
    assert "<result-file>/ws/.tool_results/s/task-bg_3.txt</result-file>" in out
    assert "<hint>" in out
    assert "<result>" not in out  # large → no inline result body


def test_explicit_result_file_overrides_inline():
    out = build_task_result_pointer(
        task_id="bg_9",
        command_name="agent",
        status="success",
        summary="agent finished.",
        result_file="/ws/.tool_results/s/task-bg_9.txt",
    )
    assert "<result-file>/ws/.tool_results/s/task-bg_9.txt</result-file>" in out
    assert "<result>" not in out
    assert "<resume-hint>" not in out  # result_file present → treated as terminal


def test_pause_marker_has_resume_hint_and_no_result():
    out = build_task_result_pointer(
        task_id="bg_5",
        command_name="pipeline",
        status="waiting_for_route",
        summary="pipeline paused (waiting_for_route), awaiting a decision.",
    )
    assert "<status>waiting_for_route</status>" in out
    assert "<resume-hint>" in out
    assert "<result>" not in out
    assert "<result-file>" not in out


def test_xml_escaping_of_dynamic_fields():
    out = build_task_result_pointer(
        task_id="bg_1",
        command_name="a & b <run>",
        status="failed",
        summary="broke on <tag> & stuff",
        result="value < 5 & > 1",
    )
    assert "a &amp; b &lt;run&gt;" in out
    assert "broke on &lt;tag&gt; &amp; stuff" in out
    assert "value &lt; 5 &amp; &gt; 1" in out
    # No raw unescaped injected angle brackets slipped through the body.
    assert "<run>" not in out
