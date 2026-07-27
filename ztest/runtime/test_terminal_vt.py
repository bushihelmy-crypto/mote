from __future__ import annotations

import pyte

from mote.contracts.terminal import (
    TERMINAL_FRAME_BASE_SEQUENCE,
    TERMINAL_FRAME_COLS,
    TERMINAL_FRAME_MODE,
    TERMINAL_FRAME_MODE_DELTA,
    TERMINAL_FRAME_MODE_FULL,
    TERMINAL_FRAME_ROWS,
)
from mote.runtime.tools.dependency.terminal_vt import TerminalVTState


def metadata(frame) -> dict[str, str]:
    return dict(frame.metadata)


def test_vt_state_applies_cursor_rewrites_to_canonical_grid():
    state = TerminalVTState(10, 3)

    state.feed_text("abc\rX")

    assert state.display[0].startswith("Xbc")


def test_full_frame_reconstructs_grid_style_and_cursor():
    state = TerminalVTState(12, 3)
    state.feed_text("\x1b[31mred\x1b[0m\r\nnext")

    frame = state.full_frame()
    replay = pyte.Screen(12, 3)
    pyte.Stream(replay).feed(frame.content)

    assert tuple(replay.display) == state.display
    assert (replay.cursor.x, replay.cursor.y) == (4, 1)
    assert replay.buffer[0][0].fg == "red"
    assert metadata(frame)[TERMINAL_FRAME_MODE] == TERMINAL_FRAME_MODE_FULL


def test_full_frame_reconstructs_scrollback_without_sparse_line_failures():
    state = TerminalVTState(8, 2, scrollback_lines=4)
    state.feed_text("one\r\ntwo\r\nthree")

    frame = state.full_frame()
    replay = pyte.HistoryScreen(8, 2, history=4)
    pyte.Stream(replay).feed(frame.content)

    assert "one" in "".join(cell.data for line in replay.history.top for cell in line.values())
    assert replay.display[0].startswith("two")
    assert replay.display[1].startswith("three")


def test_contiguous_update_is_an_incremental_frame():
    state = TerminalVTState(10, 3)
    state.feed_text("one")
    baseline = state.full_frame()

    state.feed_text(" two")
    update = state.frame_after(baseline.sequence)

    assert update.content == " two"
    assert metadata(update)[TERMINAL_FRAME_MODE] == TERMINAL_FRAME_MODE_DELTA
    assert metadata(update)[TERMINAL_FRAME_BASE_SEQUENCE] == str(baseline.sequence)


def test_lost_delta_recovers_with_a_full_canonical_frame():
    state = TerminalVTState(10, 3, delta_bytes=3)
    baseline = state.sequence

    state.feed_text("larger-than-ring")
    recovered = state.frame_after(baseline)

    assert metadata(recovered)[TERMINAL_FRAME_MODE] == TERMINAL_FRAME_MODE_FULL
    assert recovered.content.startswith("\x1bc")


def test_resize_is_a_full_frame_boundary():
    state = TerminalVTState(10, 3)
    state.feed_text("before")
    baseline = state.sequence

    state.resize(20, 5)
    recovered = state.frame_after(baseline)

    assert metadata(recovered)[TERMINAL_FRAME_MODE] == TERMINAL_FRAME_MODE_FULL
    assert metadata(recovered)[TERMINAL_FRAME_COLS] == "20"
    assert metadata(recovered)[TERMINAL_FRAME_ROWS] == "5"


def test_split_utf8_output_is_decoded_once_without_replacement():
    state = TerminalVTState(10, 3)
    encoded = "画".encode("utf-8")

    state.feed_bytes(encoded[:1])
    state.feed_bytes(encoded[1:])

    assert state.display[0].startswith("画")
    assert "�" not in state.display[0]
    assert state.sequence == 1
