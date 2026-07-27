from __future__ import annotations

import re
from dataclasses import FrozenInstanceError

import pytest

from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.query_semantics import (
    CandidateDiscovery,
    CandidateDiscoveryRequest,
    RegexProgram,
    RegexProgramError,
    RegexPurpose,
)


def test_regex_program_has_one_fixed_flag_policy():
    ordinary = RegexProgram.for_search("^hit$")
    configured = RegexProgram.for_search(
        "a.b",
        case_insensitive=True,
        dot_matches_newline=True,
    )

    assert ordinary.flags == re.MULTILINE
    assert [match.group(0) for match in ordinary.finditer("miss\nhit\nno")] == ["hit"]
    assert configured.flags == re.MULTILINE | re.IGNORECASE | re.DOTALL
    assert [match.group(0) for match in configured.finditer("A\nb")] == ["A\nb"]


def test_regex_occurrence_semantics_count_every_match():
    program = RegexProgram.for_search("hit")

    assert program.occurrence_count("hit hit\nhit") == 3
    assert [match.span() for match in program.finditer("hit hit")] == [
        (0, 3),
        (4, 7),
    ]


def test_edit_regex_rejects_empty_input_and_realized_empty_matches():
    with pytest.raises(RegexProgramError, match="empty string"):
        RegexProgram.for_edit("a*")

    contextual = RegexProgram.for_edit("(?=target)")
    with pytest.raises(RegexProgramError, match="empty matches"):
        tuple(contextual.finditer("target"))


def test_edit_replacement_uses_match_expand_capture_semantics():
    program = RegexProgram.for_edit(r"(?P<word>[a-z]+)-(\d+)")
    match = next(program.finditer("item-42"))

    assert program.expand_replacement(match, r"\g<word>[\2]") == "item[42]"

    foreign = next(RegexProgram.for_edit(r"[a-z]+-\d+").finditer("item-42"))
    with pytest.raises(RegexProgramError, match="does not belong"):
        program.expand_replacement(foreign, "replacement")


def test_search_regex_cannot_be_used_for_replacement():
    program = RegexProgram.for_search("hit")
    match = next(program.finditer("hit"))

    with pytest.raises(RegexProgramError, match="requires an edit"):
        program.expand_replacement(match, "replacement")


def test_regex_program_is_immutable_and_invalid_patterns_are_typed():
    program = RegexProgram.for_search("hit")

    with pytest.raises(FrozenInstanceError):
        program.pattern = "changed"
    with pytest.raises(RegexProgramError, match="invalid regular expression"):
        RegexProgram.for_search("[")
    with pytest.raises(TypeError):
        RegexProgram("hit", RegexPurpose.SEARCH, case_insensitive=1)


def test_candidate_discovery_freezes_request_and_canonical_path_order(tmp_path):
    root = path_token(tmp_path)
    request = CandidateDiscoveryRequest(
        root=root,
        globs=("*.py", "*.txt", "*.py"),
        type_name="python",
    )
    first = path_token(tmp_path / "a.txt")
    second = path_token(tmp_path / "b.txt")

    discovery = CandidateDiscovery.freeze(request, (second, first))

    assert request.globs == ("*.py", "*.txt")
    assert discovery.candidates == (first, second)
    with pytest.raises(FrozenInstanceError):
        request.type_name = "text"
    with pytest.raises(FrozenInstanceError):
        discovery.candidates = ()


def test_candidate_discovery_rejects_duplicate_native_paths(tmp_path):
    request = CandidateDiscoveryRequest(root=path_token(tmp_path))
    candidate = path_token(tmp_path / "missing.txt")

    with pytest.raises(ValueError, match="duplicate path"):
        CandidateDiscovery.freeze(request, (candidate, candidate))


def test_candidate_discovery_does_not_require_candidates_to_exist(tmp_path):
    request = CandidateDiscoveryRequest(root=path_token(tmp_path / "missing-root"))
    candidate = path_token(tmp_path / "missing-root" / "never-opened.txt")

    discovery = CandidateDiscovery.freeze(request, (candidate,))

    assert discovery.candidates == (candidate,)
