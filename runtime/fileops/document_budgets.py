"""Hard resource budgets for rich-document text extraction."""

from __future__ import annotations

import zipfile

from mote.contracts.fileops.errors import DocumentResourceLimitError
from mote.contracts.fileops.models import ExtractionBudget

DEFAULT_EXTRACTION_BUDGET = ExtractionBudget(
    max_archive_uncompressed_bytes=256 * 1_024 * 1_024,
    max_output_bytes=50 * 1_024 * 1_024,
)


class BoundedTextSink:
    """Collects extractor output while enforcing one UTF-8 byte budget."""

    def __init__(self, budget: ExtractionBudget) -> None:
        self._maximum = budget.max_output_bytes
        self._consumed = 0
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._parts)

    @property
    def consumed_bytes(self) -> int:
        return self._consumed

    def write(self, value: str) -> int:
        if type(value) is not str:
            raise TypeError("document extractors must emit text")
        encoded_size = len(value.encode("utf-8", errors="strict"))
        consumed = self._consumed + encoded_size
        if consumed > self._maximum:
            raise DocumentResourceLimitError(
                "document extraction exceeded its output budget",
                resource="output_bytes",
                consumed=consumed,
                maximum=self._maximum,
            )
        self._parts.append(value)
        self._consumed = consumed
        return len(value)


def enforce_archive_budget(
    file_path: str,
    budget: ExtractionBudget,
) -> None:
    consumed = 0
    with zipfile.ZipFile(file_path) as archive:
        for entry in archive.infolist():
            consumed += entry.file_size
            if consumed > budget.max_archive_uncompressed_bytes:
                raise DocumentResourceLimitError(
                    "document archive exceeds its uncompressed-size budget",
                    resource="archive_uncompressed_bytes",
                    consumed=consumed,
                    maximum=budget.max_archive_uncompressed_bytes,
                )


__all__ = [
    "BoundedTextSink",
    "DEFAULT_EXTRACTION_BUDGET",
    "enforce_archive_budget",
]
