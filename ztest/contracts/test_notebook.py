from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.notebook import NotebookCell, NotebookDocument, NotebookOutput


def test_notebook_document_round_trip_preserves_typed_outputs():
    document = NotebookDocument(
        ref="jupyter-notebook:test",
        revision=3,
        cells=[
            NotebookCell(
                id="cell-1",
                source="print('hello')",
                outputs=[NotebookOutput(output_type="stream", name="stdout", text="hello\n")],
            )
        ],
    )

    restored = NotebookDocument.model_validate_json(document.model_dump_json())
    assert restored == document


def test_notebook_document_rejects_duplicate_cell_ids():
    with pytest.raises(ValidationError, match="unique"):
        NotebookDocument(
            ref="jupyter-notebook:test",
            cells=[
                NotebookCell(id="cell-1", source="1"),
                NotebookCell(id="cell-1", source="2"),
            ],
        )


def test_notebook_output_rejects_active_content_media_types():
    with pytest.raises(ValidationError, match="unsafe"):
        NotebookOutput(output_type="display_data", data={"text/html": "<script>bad()</script>"})
