"""Pure canonical Canvas transaction semantics shared by all backends."""
from __future__ import annotations

from mote.contracts.canvas import CanvasDocument, CanvasOperation


def apply_canvas_operations(
    document: CanvasDocument,
    operations: tuple[CanvasOperation, ...] | list[CanvasOperation],
) -> tuple[CanvasDocument, bool, tuple[str, ...]]:
    """Build one validated atomic scene candidate without mutating the input."""
    elements = [element.model_copy(deep=True) for element in document.elements]
    positions = {element.id: index for index, element in enumerate(elements)}
    affected: list[str] = []
    changed = False

    for operation in operations:
        if operation.op == "clear":
            if elements:
                affected.extend(element.id for element in elements)
                elements = []
                positions = {}
                changed = True
            continue
        if operation.op == "remove":
            index = positions.get(operation.element_id)
            if index is None:
                continue
            affected.append(operation.element_id)
            del elements[index]
            positions = {element.id: pos for pos, element in enumerate(elements)}
            changed = True
            continue

        assert operation.element is not None
        element = operation.element.model_copy(deep=True)
        index = positions.get(element.id)
        if index is None:
            positions[element.id] = len(elements)
            elements.append(element)
            changed = True
        elif elements[index] != element:
            elements[index] = element
            changed = True
        affected.append(element.id)

    candidate = document.model_copy(update={"elements": elements})
    validated = CanvasDocument.model_validate(candidate.model_dump(mode="json"))
    return validated, changed, tuple(dict.fromkeys(affected))


__all__ = ["apply_canvas_operations"]
