from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.surface import TerminalResizeInput


def test_terminal_resize_contract_bounds_character_grid():
    assert TerminalResizeInput(cols=120, rows=40).model_dump() == {
        "cols": 120,
        "rows": 40,
    }
    with pytest.raises(ValidationError):
        TerminalResizeInput(cols=1, rows=40)
