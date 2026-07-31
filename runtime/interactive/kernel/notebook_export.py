"""Pure deterministic notebook export of canonical Notebook documents."""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from mote.contracts.surface import (
    NOTEBOOK_EXPORT_MIME_TYPE,
    NotebookCell,
    NotebookDocument,
    NotebookExportRepresentation,
    NotebookOutput,
)

_MOTE_METADATA_NAMESPACE = "mote"


def export_notebook_ipynb(document: NotebookDocument) -> NotebookExportRepresentation:
    """Return a deterministic, standards-valid `.ipynb` representation."""
    payload = {
        "cells": [_export_cell(cell) for cell in document.cells],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            _MOTE_METADATA_NAMESPACE: {
                "schema_version": document.schema_version,
                "ref": document.ref,
                "revision": document.revision,
                "kernel_epoch": document.kernel_epoch,
                "kernel_status": document.kernel_status,
                "truncated": document.truncated,
                "extensions": document.extensions,
            },
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }
    content = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return NotebookExportRepresentation(
        representation="ipynb",
        mime_type=NOTEBOOK_EXPORT_MIME_TYPE,
        content=content,
        suggested_name="notebook.ipynb",
    )


def _export_cell(cell: NotebookCell) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": cell.execution_count,
        "metadata": {
            _MOTE_METADATA_NAMESPACE: {
                "id": cell.id,
                "status": cell.status,
                "origin": cell.origin,
                "extensions": cell.extensions,
            }
        },
        "outputs": [_export_output(output) for output in cell.outputs],
        "source": cell.source,
    }


def _export_output(output: NotebookOutput) -> dict[str, Any]:
    if output.output_type == "stream":
        return {
            "name": output.name or "stdout",
            "output_type": "stream",
            "text": output.text,
        }
    if output.output_type == "error":
        return {
            "ename": output.ename,
            "evalue": output.evalue,
            "output_type": "error",
            "traceback": list(output.traceback),
        }

    exported: dict[str, Any] = {
        "data": _export_display_data(output.data),
        "metadata": {},
        "output_type": output.output_type,
    }
    if output.output_type == "execute_result":
        exported["execution_count"] = output.execution_count
    return exported


def _export_display_data(data: dict[str, str]) -> dict[str, str]:
    exported: dict[str, str] = {}
    text = data.get("text/plain")
    if text is not None:
        exported["text/plain"] = text
    image = data.get("image/png")
    if image is not None:
        try:
            base64.b64decode(image, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("notebook image/png output is not valid base64") from exc
        exported["image/png"] = image
    return exported


__all__ = ["export_notebook_ipynb"]
