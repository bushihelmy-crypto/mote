"""Tests for :mod:`mote.kernel.tools.docstrings` shared parsing utilities."""

from __future__ import annotations

from mote.kernel.tools.docstrings import first_line, parse_section

# ---------------------------------------------------------------------------
# first_line
# ---------------------------------------------------------------------------


def test_first_line_callable():
    def hello():
        """Hello world.

        More details.
        """

    assert first_line(hello) == "Hello world."


def test_first_line_string():
    assert first_line("  First line\n  Second line") == "First line"


def test_first_line_none():
    assert first_line(None) == ""


def test_first_line_empty():
    def no_doc():
        pass

    assert first_line(no_doc) == ""


def test_first_line_leading_blank_lines():
    doc = "\n\n   Actual first line.\n"
    assert first_line(doc) == "Actual first line."


# ---------------------------------------------------------------------------
# parse_section — Args (tool_spec_adapter use-case)
# ---------------------------------------------------------------------------


def test_parse_section_args_basic():
    doc = """Do something.

    Args:
        path: The file path.
        content: The file content.
    """
    result = parse_section(doc, "Args")
    assert result == [("path", "The file path."), ("content", "The file content.")]


def test_parse_section_args_with_type_annotation():
    doc = """Do something.

    Args:
        path (str): The file path.
        count (int): How many times.
    """
    result = parse_section(doc, "Args")
    assert dict(result) == {"path": "The file path.", "count": "How many times."}


def test_parse_section_args_continuation():
    doc = """Do something.

    Args:
        path: The file path which may be
            very long and span multiple lines.
        content: Short.
    """
    result = parse_section(doc, "Args")
    assert result[0] == ("path", "The file path which may be very long and span multiple lines.")
    assert result[1] == ("content", "Short.")


def test_parse_section_stops_at_next_header():
    doc = """Do something.

    Args:
        x: First.

    Returns:
        Nothing.
    """
    result = parse_section(doc, "Args")
    assert result == [("x", "First.")]


# ---------------------------------------------------------------------------
# parse_section — Params (bggraph node use-case)
# ---------------------------------------------------------------------------


def test_parse_section_params_em_dash():
    doc = """Node description.

    Params:
        audio: $input.audio_url — raw audio URL
        text: upstream.output — transcription text
    """
    result = parse_section(doc, "Params")
    assert result == [
        ("audio", "$input.audio_url — raw audio URL"),
        ("text", "upstream.output — transcription text"),
    ]


def test_parse_section_params_no_separator():
    doc = """Node desc.

    Params:
        audio: $input.audio_url
    """
    result = parse_section(doc, "Params")
    assert result == [("audio", "$input.audio_url")]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_parse_section_case_insensitive():
    doc = """Summary.

    args:
        x: value
    """
    result = parse_section(doc, "Args")
    assert result == [("x", "value")]


def test_parse_section_full_width_colon():
    doc = """Summary.

    Params：
        x: value
    """
    result = parse_section(doc, "Params")
    assert result == [("x", "value")]


def test_parse_section_missing():
    doc = """No params here."""
    assert parse_section(doc, "Params") == []


def test_parse_section_none_docstring():
    assert parse_section(None, "Args") == []


def test_parse_section_empty_docstring():
    assert parse_section("", "Args") == []
