from __future__ import annotations

from copy import deepcopy

import pytest

from mote.contracts.file.codec import (
    search_row_from_dict,
    search_row_to_dict,
    search_skipped_from_dict,
    search_skipped_to_dict,
    search_summary_from_dict,
    search_summary_to_dict,
)
from mote.contracts.file.identity import NameIdentity, PathToken, PresentVersion, TargetIdentity
from mote.contracts.file.search import SearchRow, SearchSkippedFile, SearchSkipReason, SearchSummary


def _row() -> SearchRow:
    return SearchRow(
        path=PathToken("src/main.py", "src/main.py"),
        version=PresentVersion(
            name_identity=NameIdentity("main.py", "portable-name-v1"),
            target_identity=TargetIdentity("target-1", "portable-target-v1"),
            size=12,
            mtime_ns=4,
            digest="a" * 64,
            metadata_digest="b" * 64,
        ),
        line_number=2,
        text="match",
        matched_text="match",
        occurrence_count=1,
        is_context=False,
    )


def test_search_records_roundtrip_canonically() -> None:
    row = _row()
    skipped = SearchSkippedFile(row.path, SearchSkipReason.BINARY, "binary")
    summary = SearchSummary(2, 1, 1, 1, 1, complete=False, termination="timeout")

    assert search_row_from_dict(search_row_to_dict(row)) == row
    assert search_skipped_from_dict(search_skipped_to_dict(skipped)) == skipped
    assert search_summary_from_dict(search_summary_to_dict(summary)) == summary


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("line_number", "2"),
        ("line_number", True),
        ("line_number", 0),
        ("occurrence_count", "1"),
        ("occurrence_count", -1),
        ("is_context", 1),
        ("text", None),
    ),
)
def test_search_row_rejects_noncanonical_primitives(field: str, value: object) -> None:
    payload = search_row_to_dict(_row())
    payload[field] = value
    with pytest.raises(ValueError):
        search_row_from_dict(payload)


def test_search_row_rejects_missing_extra_and_absent_version() -> None:
    payload = search_row_to_dict(_row())
    del payload["text"]
    with pytest.raises(ValueError, match="fields are not canonical"):
        search_row_from_dict(payload)
    payload = search_row_to_dict(_row())
    payload["extra"] = True
    with pytest.raises(ValueError, match="fields are not canonical"):
        search_row_from_dict(payload)
    payload = search_row_to_dict(_row())
    payload["version"] = {"kind": "absent", "name_identity": {"key": "main.py", "scheme": "name-v1"}}
    with pytest.raises(ValueError, match="absent version"):
        search_row_from_dict(payload)


def test_search_skipped_rejects_unknown_reason_and_wrong_shape() -> None:
    payload = search_skipped_to_dict(SearchSkippedFile(_row().path, SearchSkipReason.IO, "failed"))
    payload["reason"] = "future_reason"
    with pytest.raises(ValueError, match="unknown search skip reason"):
        search_skipped_from_dict(payload)
    del payload["detail"]
    with pytest.raises(ValueError, match="fields are not canonical"):
        search_skipped_from_dict(payload)


def test_search_summary_rejects_boolean_count_and_inconsistent_termination() -> None:
    canonical = search_summary_to_dict(SearchSummary(1, 1, 1, 1, 0))
    invalid_count = deepcopy(canonical)
    invalid_count["scanned_files"] = True
    with pytest.raises(ValueError, match="scanned_files"):
        search_summary_from_dict(invalid_count)
    invalid_completion = deepcopy(canonical)
    invalid_completion["complete"] = False
    with pytest.raises(ValueError, match="inconsistent"):
        search_summary_from_dict(invalid_completion)
    unknown_termination = deepcopy(canonical)
    unknown_termination["termination"] = "future"
    with pytest.raises(ValueError, match="completion fields"):
        search_summary_from_dict(unknown_termination)


def test_search_contracts_reject_invalid_values_before_encoding() -> None:
    with pytest.raises(ValueError, match="occurrence_count"):
        SearchRow(
            path=_row().path,
            version=None,
            line_number=None,
            text="",
            matched_text="",
            occurrence_count=-1,
        )
    with pytest.raises(ValueError, match="reason"):
        SearchSkippedFile(_row().path, "io", "failed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inconsistent"):
        SearchSummary(1, 1, 1, 1, 0, complete=False, termination="")


def test_search_encoders_revalidate_tampered_frozen_contracts() -> None:
    row = _row()
    object.__setattr__(row, "occurrence_count", -1)
    with pytest.raises(ValueError, match="occurrence_count"):
        search_row_to_dict(row)

    summary = SearchSummary(1, 1, 1, 1, 0)
    object.__setattr__(summary, "scanned_files", -1)
    with pytest.raises(ValueError, match="scanned_files"):
        search_summary_to_dict(summary)
