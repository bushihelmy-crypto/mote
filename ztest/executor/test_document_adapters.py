import pytest

from mote.contracts.fileops import DocumentExtractorUnavailableError
from mote.runtime.fileops import documents as _document
from mote.runtime.fileops.document_budgets import DEFAULT_EXTRACTION_BUDGET


def test_pdf_uses_first_available_adapter(monkeypatch):
    calls = []

    def extract(path, *, sink):
        calls.append(path)
        sink.write(f"extracted:{path}")

    monkeypatch.setattr(_document, "_PDF_ADAPTERS", (extract,))

    assert (
        _document.extract_pdf_text(
            "report.pdf",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
        == "extracted:report.pdf"
    )
    assert calls == ["report.pdf"]


def test_pdf_falls_back_after_an_extractor_failure(monkeypatch):
    calls = []

    def extract(path, *, sink):
        calls.append(path)
        raise ValueError("broken adapter")

    def fallback(path, *, sink):
        sink.write("masked")

    monkeypatch.setattr(_document, "_PDF_ADAPTERS", (extract, fallback))

    assert (
        _document.extract_pdf_text(
            "broken.pdf",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
        == "masked"
    )
    assert calls == ["broken.pdf"]


def test_document_dispatch_preserves_extension_contract(monkeypatch):
    monkeypatch.setattr(
        _document,
        "extract_pdf_text",
        lambda path, *, budget: f"pdf:{path}",
    )
    monkeypatch.setattr(
        _document,
        "extract_docx_text",
        lambda path, *, budget: f"docx:{path}",
    )
    monkeypatch.setattr(
        _document,
        "extract_xlsx_text",
        lambda path, *, budget: f"xlsx:{path}",
    )

    assert (
        _document.extract_document_text(
            "A.PDF",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
        == "pdf:A.PDF"
    )
    assert (
        _document.extract_document_text(
            "a.docx",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
        == "docx:a.docx"
    )
    assert (
        _document.extract_document_text(
            "a.xlsx",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
        == "xlsx:a.xlsx"
    )
    with pytest.raises(DocumentExtractorUnavailableError):
        _document.extract_document_text(
            "a.txt",
            budget=DEFAULT_EXTRACTION_BUDGET,
        )
