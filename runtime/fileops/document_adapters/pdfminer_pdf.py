"""pdfminer.six PDF extraction adapter."""

from __future__ import annotations

from pdfminer.high_level import extract_text_to_fp  # type: ignore

from mote.runtime.fileops.document_budgets import BoundedTextSink


def extract(file_path: str, *, sink: BoundedTextSink) -> None:
    with open(file_path, "rb") as source:
        extract_text_to_fp(source, sink, codec=None)
