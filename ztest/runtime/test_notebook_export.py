from __future__ import annotations

import json

import nbformat
import pytest

from mote.contracts.surface import (
    NOTEBOOK_EXPORT_MIME_TYPE,
    NotebookCell,
    NotebookDisplayDataOutput,
    NotebookDocument,
    NotebookErrorOutput,
    NotebookExecuteResultOutput,
    NotebookStreamOutput,
)
from mote.runtime.interactive.kernel.driver import KernelRuntimeDriver
from mote.runtime.interactive.kernel.notebook_export import export_notebook_ipynb

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


def _document() -> NotebookDocument:
    return NotebookDocument(
        ref="jupyter-notebook:export-test",
        revision=7,
        kernel_epoch=2,
        kernel_status="idle",
        truncated=True,
        extensions={"document-extension": {"enabled": True}},
        cells=[
            NotebookCell(
                id="mote-cell-1",
                source="print('hello')\n1 + 1",
                execution_count=3,
                status="error",
                origin="human",
                extensions={"cell-extension": [1, 2]},
                outputs=[
                    NotebookStreamOutput(
                        name="stdout",
                        text="hello\n",
                    ),
                    NotebookExecuteResultOutput(
                        execution_count=3,
                        data={"text/plain": "2", "image/png": _PNG_B64},
                    ),
                    NotebookDisplayDataOutput(
                        data={"text/plain": "display"},
                    ),
                    NotebookErrorOutput(
                        ename="ValueError",
                        evalue="bad value",
                        traceback=["Traceback", "ValueError: bad value"],
                    ),
                ],
            )
        ],
    )


def test_ipynb_export_is_deterministic_and_nbformat_4_valid():
    document = _document()

    first = export_notebook_ipynb(document)
    second = export_notebook_ipynb(document.model_copy(deep=True))
    parsed = nbformat.reads(first.content.decode("utf-8"), as_version=4)
    nbformat.validate(parsed)

    assert first == second
    assert first.representation == "ipynb"
    assert first.mime_type == NOTEBOOK_EXPORT_MIME_TYPE
    assert first.suggested_name == "notebook.ipynb"
    assert parsed.nbformat == 4
    assert parsed.nbformat_minor == 4


def test_ipynb_export_preserves_code_outputs_and_mote_metadata():
    document = _document()
    payload = json.loads(export_notebook_ipynb(document).content)
    cell = payload["cells"][0]
    outputs = cell["outputs"]

    assert cell["cell_type"] == "code"
    assert cell["source"] == document.cells[0].source
    assert cell["execution_count"] == 3
    assert cell["metadata"]["mote"] == {
        "id": "mote-cell-1",
        "status": "error",
        "origin": "human",
        "extensions": {"cell-extension": [1, 2]},
    }
    assert outputs[0] == {
        "name": "stdout",
        "output_type": "stream",
        "text": "hello\n",
    }
    assert outputs[1]["output_type"] == "execute_result"
    assert outputs[1]["execution_count"] == 3
    assert outputs[1]["data"] == {
        "image/png": _PNG_B64,
        "text/plain": "2",
    }
    assert outputs[2]["output_type"] == "display_data"
    assert outputs[2]["data"] == {"text/plain": "display"}
    assert outputs[3] == {
        "ename": "ValueError",
        "evalue": "bad value",
        "output_type": "error",
        "traceback": ["Traceback", "ValueError: bad value"],
    }
    assert payload["metadata"]["mote"] == {
        "schema_version": "1",
        "ref": "jupyter-notebook:export-test",
        "revision": 7,
        "kernel_epoch": 2,
        "kernel_status": "idle",
        "truncated": True,
        "extensions": {"document-extension": {"enabled": True}},
    }


def test_ipynb_export_rejects_invalid_png_base64():
    document = NotebookDocument(
        ref="jupyter-notebook:invalid-image",
        cells=[
            NotebookCell(
                id="cell-1",
                source="display(image)",
                outputs=[
                    NotebookDisplayDataOutput(
                        data={"image/png": "not base64!"},
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="valid base64"):
        export_notebook_ipynb(document)


@pytest.mark.asyncio
async def test_kernel_driver_exports_snapshot_without_starting_kernel():
    driver = KernelRuntimeDriver(session_key="export-only", cwd=None)
    document = _document()

    exported = await driver.export_representations(document)

    assert len(exported) == 1
    payload = json.loads(exported[0].content)
    assert payload["cells"][0]["source"] == document.cells[0].source
    assert driver.closed is True
