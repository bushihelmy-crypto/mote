from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.surface import (
    NotebookCell,
    NotebookDisplayDataOutput,
    NotebookDocument,
    NotebookInputReply,
    NotebookStreamOutput,
)


def test_notebook_document_round_trip_preserves_typed_outputs():
    document = NotebookDocument(
        ref="jupyter-notebook:test",
        revision=3,
        cells=[
            NotebookCell(
                id="cell-1",
                source="print('hello')",
                outputs=[NotebookStreamOutput(name="stdout", text="hello\n")],
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
        NotebookDisplayDataOutput(data={"text/html": "<script>bad()</script>"})


def test_notebook_stdin_reply_requires_every_fence() -> None:
    with pytest.raises(ValidationError):
        NotebookInputReply.model_validate({"request_id": "request-1", "value": "secret"})

    reply = NotebookInputReply(
        request_id="request-1",
        value="answer",
        document_revision=4,
        kernel_epoch=2,
        connection_generation=3,
        human_generation=5,
        expected_request_revision=1,
    )
    assert reply.human_generation == 5


def test_notebook_document_decoder_rejects_unknown_output_shape() -> None:
    with pytest.raises(ValidationError):
        NotebookDocument.model_validate(
            {
                "schema_version": "1",
                "ref": "jupyter-notebook:strict",
                "cells": [
                    {
                        "id": "cell-1",
                        "cell_type": "code",
                        "source": "1",
                        "outputs": [{"output_type": "stream", "name": "stdout", "text": "1", "data": {}}],
                    }
                ],
            }
        )
