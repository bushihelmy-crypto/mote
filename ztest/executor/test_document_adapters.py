from mote.executor.dependency import _document


def test_pdf_uses_first_available_adapter(monkeypatch):
    calls = []

    def load(module_name):
        calls.append(module_name)
        if module_name == _document._PDF_ADAPTERS[0]:
            return None
        return lambda path: f"extracted:{path}"

    monkeypatch.setattr(_document, "_load_adapter", load)

    assert _document.extract_pdf_text("report.pdf") == "extracted:report.pdf"
    assert calls == list(_document._PDF_ADAPTERS[:2])


def test_pdf_does_not_mask_extraction_failure_with_another_backend(monkeypatch):
    calls = []

    def load(module_name):
        calls.append(module_name)
        return lambda _path: None

    monkeypatch.setattr(_document, "_load_adapter", load)

    assert _document.extract_pdf_text("broken.pdf") is None
    assert calls == [_document._PDF_ADAPTERS[0]]


def test_document_dispatch_preserves_extension_contract(monkeypatch):
    monkeypatch.setattr(_document, "extract_pdf_text", lambda path: f"pdf:{path}")
    monkeypatch.setattr(_document, "extract_docx_text", lambda path: f"docx:{path}")
    monkeypatch.setattr(_document, "extract_xlsx_text", lambda path: f"xlsx:{path}")

    assert _document.extract_document_text("A.PDF") == "pdf:A.PDF"
    assert _document.extract_document_text("a.docx") == "docx:a.docx"
    assert _document.extract_document_text("a.xlsx") == "xlsx:a.xlsx"
    assert _document.extract_document_text("a.txt") is None
