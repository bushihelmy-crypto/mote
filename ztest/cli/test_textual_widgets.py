#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Textual transcript widgets — mount + mutate under an ``App.run_test`` pilot.

Each widget is a thin ``Static`` fed by the shared ``render`` builders; these
tests mount them into a harness app and assert the mutation entry points
(``AssistantBlock.append_delta`` accumulation, ``ToolCallWidget.complete``
correlation, ``StatusBar.update_usage`` reactives) behave.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from metagpt.cli.common.view import RetryStatus, ToolCallCompleted, ToolCallStarted, UsageUpdated
from metagpt.cli.consumers.textual.style import textual_css_vars
from metagpt.cli.consumers.textual.widgets import (
    AssistantBlock,
    StatusBar,
    ToolCallWidget,
    UserMessageRow,
)


class _Harness(App):
    """Minimal host that mounts arbitrary widgets into a scroll region.

    Provides the same custom CSS variables (``$brand`` / ``$dim`` / …) the real
    :class:`MetaGPTApp` injects, so widgets whose ``DEFAULT_CSS`` references them
    (``StatusBar``, ``PromptInput``) parse when mounted here.
    """

    def get_css_variables(self) -> dict:
        base = super().get_css_variables()
        base.update(textual_css_vars())
        return base

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="box")

    async def add(self, widget):
        await self.query_one("#box", VerticalScroll).mount(widget)
        return widget


@pytest.mark.asyncio
async def test_assistant_block_accumulates_deltas():
    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.append_delta("Hello ")
        block.append_delta("world")
        assert block._buf == "Hello world"
        block.append_delta("")  # empty delta is a no-op
        assert block._buf == "Hello world"
        block.finalize()  # does not raise


@pytest.mark.asyncio
async def test_assistant_block_set_markdown_replaces():
    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.append_delta("streamed")
        block.set_markdown("# Fresh")
        assert block._buf == "# Fresh"


@pytest.mark.asyncio
async def test_tool_widget_correlates_completion():
    async with _Harness().run_test() as pilot:
        started = ToolCallStarted(tool_name="Read", title="Read", headline="a.py", tool_use_id="tu-1")
        widget = await pilot.app.add(ToolCallWidget(started))
        assert widget.tool_use_id == "tu-1"
        assert widget._completed is None
        completed = ToolCallCompleted(tool_name="Read", tool_use_id="tu-1", summary="12 lines")
        widget.complete(completed)
        assert widget._completed is completed


@pytest.mark.asyncio
async def test_tool_widget_with_diff_detail_renders():
    async with _Harness().run_test() as pilot:
        started = ToolCallStarted(tool_name="Edit", tool_use_id="tu-2")
        widget = await pilot.app.add(ToolCallWidget(started))
        completed = ToolCallCompleted(
            tool_name="Edit",
            tool_use_id="tu-2",
            summary="ok",
            result_kind="diff",
            detail="+added line\n-removed line",
        )
        widget.complete(completed)  # exercises the diff-render branch
        assert widget._completed is completed


@pytest.mark.asyncio
async def test_assistant_block_markdown_is_selectable():
    """A Markdown-rendering block yields selectable text (Textual would return None).

    ``Widget.get_selection`` only extracts from a plain ``Text``/``Content``
    visual; our ``SelectableStatic`` override reconstructs it from the rendered
    strips so the markdown transcript row can be selected + copied like the
    plain-text rows.
    """
    from textual.selection import Selection

    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.set_markdown("Hello selectable markdown")
        await pilot.pause()  # let it render (populates the strip cache)
        result = block.get_selection(Selection(None, None))  # whole-widget selection
        assert result is not None
        text, ending = result
        assert "Hello selectable markdown" in text
        assert ending == "\n"


@pytest.mark.asyncio
async def test_tool_widget_group_is_selectable():
    """A ``Group``-rendering tool row is selectable too (not just plain-text rows)."""
    from textual.selection import Selection

    async with _Harness().run_test() as pilot:
        started = ToolCallStarted(tool_name="Read", headline="a.py", tool_use_id="tu-3")
        widget = await pilot.app.add(ToolCallWidget(started))
        widget.complete(ToolCallCompleted(tool_name="Read", tool_use_id="tu-3", summary="12 lines"))
        await pilot.pause()
        result = widget.get_selection(Selection(None, None))
        assert result is not None
        assert "Read" in result[0]


@pytest.mark.asyncio
async def test_unrendered_selectable_static_returns_none():
    """Before any render (no strip cache) selection extraction is a safe no-op."""
    from textual.selection import Selection

    from metagpt.cli.consumers.textual.widgets import SelectableStatic

    widget = SelectableStatic("never rendered")
    assert widget.get_selection(Selection(None, None)) is None


@pytest.mark.asyncio
async def test_selectable_static_paints_highlight_on_selection():
    """A selection paints the ``screen--selection`` style onto the row's strip.

    ``RichVisual`` (Markdown/Group/Table) ignores the selection style, so a drag
    over these rows copied but showed no highlight; ``render_line`` now applies
    the component style to the selected span. We assert the rendered strip gains
    the highlight background on the selected line (and a no-selection render
    stays plain).
    """
    from textual.selection import Selection

    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.set_markdown("highlight me")
        await pilot.pause()
        sel_bg = block.screen.get_component_rich_style("screen--selection").bgcolor
        plain = block.render_line(0)
        assert sel_bg not in _bgcolors(plain)  # no selection → no highlight band
        # Simulate the screen registering a whole-line selection on this widget.
        block.screen.selections = {block: Selection(None, None)}
        await pilot.pause()
        selected = block.render_line(0)
        assert sel_bg in _bgcolors(selected)  # selected span now carries the highlight bg


def _bgcolors(strip) -> set:
    """The set of background colours across a strip's segments."""
    return {seg.style.bgcolor for seg in strip if seg.style is not None}


@pytest.mark.asyncio
async def test_render_line_tags_character_offsets():
    """Every rendered segment carries an ``{"offset": (char_x, y)}`` meta.

    This is the meta ``Compositor.get_widget_and_offset_at`` scans to map a mouse
    coordinate to a character offset — without it a drag over a ``RichVisual`` row
    collapses to a whole-widget selection. We assert the running character offsets
    are tagged (0, then advancing by each segment's text length) with the right y.
    """
    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.set_markdown("hello world")
        await pilot.pause()
        strip = block.render_line(0)
        char_x = 0
        seen_offsets = []
        for seg in strip:
            assert seg.style is not None and "offset" in seg.style.meta
            ox, oy = seg.style.meta["offset"]
            seen_offsets.append((ox, oy))
            assert ox == char_x
            assert oy == 0
            char_x += len(seg.text)
        assert seen_offsets and seen_offsets[0] == (0, 0)


@pytest.mark.asyncio
async def test_partial_selection_highlights_only_selected_span():
    """A partial (not whole-widget) selection highlights ONLY the selected columns.

    Regression for "拖选失效，直接复制了一个块": a drag must select the characters
    under the pointer, not the entire block. We register a character-range
    ``Selection`` and assert the highlight bg appears on the selected cells but the
    unselected head/tail stay plain.
    """
    from textual.geometry import Offset
    from textual.selection import Selection

    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.set_markdown("hello world")
        await pilot.pause()
        sel_bg = block.screen.get_component_rich_style("screen--selection").bgcolor
        # Select columns [2, 5) on line 0 only (a strict sub-span of the line).
        block.screen.selections = {block: Selection(Offset(2, 0), Offset(5, 0))}
        await pilot.pause()
        strip = block.render_line(0)
        highlighted_cells = [
            seg.style.bgcolor == sel_bg for seg in strip for _ in range(seg.cell_length)
        ]
        assert any(highlighted_cells)  # something is highlighted
        assert not all(highlighted_cells)  # but NOT the whole line
        # The highlight band is exactly the 3 selected cells [2, 5).
        assert highlighted_cells[2:5] == [True, True, True]
        assert highlighted_cells[0] is False and highlighted_cells[5] is False


@pytest.mark.asyncio
async def test_partial_selection_copies_only_selected_text():
    """``get_selection`` returns only the selected sub-string, not the whole block."""
    from textual.geometry import Offset
    from textual.selection import Selection

    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        block.set_markdown("hello world")
        await pilot.pause()
        # The rendered line carries a "● " markdown bullet prefix, so "hello"
        # begins at char offset 2 — select [2, 7) to copy exactly that word.
        result = block.get_selection(Selection(Offset(2, 0), Offset(7, 0)))
        assert result is not None
        assert result[0] == "hello"


@pytest.mark.asyncio
async def test_user_message_row_mounts_with_text():
    from rich.console import Console

    from metagpt.cli.consumers.render.builders import user_message_row

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(UserMessageRow("fix the bug in foo.py"))
        assert isinstance(row, UserMessageRow)  # mounts without raising
        # The widget wraps the shared builder; assert that renders the literal text.
        console = Console(width=80)
        with console.capture() as cap:
            console.print(user_message_row("fix the bug in foo.py"))
        assert "fix the bug in foo.py" in cap.get()


@pytest.mark.asyncio
async def test_status_bar_update_usage_sets_reactives():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.update_usage(UsageUpdated(total_tokens=1234, cost_usd=0.05, context_pct=0.42, model="gpt-4"))
        assert bar.model == "gpt-4"
        assert bar.total_tokens == 1234
        assert bar.cost_usd == 0.05
        assert bar.context_pct == 0.42


@pytest.mark.asyncio
async def test_status_bar_derives_total_from_in_out():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.update_usage(UsageUpdated(input_tokens=100, output_tokens=50))
        assert bar.total_tokens == 150


@pytest.mark.asyncio
async def test_status_bar_set_retry_shows_countdown():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.set_retry(
            RetryStatus(attempt=2, max_attempts=6, delay_ms=3000.0, error_type="LLMOverloadedError")
        )
        assert bar.retry_msg
        assert bar.retry_secs == pytest.approx(3.0)
        rendered = bar.render().plain
        assert "\u27f3" in rendered  # ⟳ retry glyph (not the ⚠ approval gate)
        assert "第 2/6 次重试" in rendered
        assert "LLMOverloadedError" in rendered


@pytest.mark.asyncio
async def test_status_bar_retry_countdown_ticks_down():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.set_retry(RetryStatus(attempt=1, max_attempts=6, delay_ms=1000.0))
        bar._tick()
        assert bar.retry_secs == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_status_bar_clear_retry_restores_normal_line():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.set_retry(RetryStatus(attempt=1, max_attempts=6, delay_ms=1000.0))
        bar.clear_retry()
        assert bar.retry_msg == ""
        assert bar.retry_secs == 0.0
        assert "重试" not in bar.render().plain


@pytest.mark.asyncio
async def test_status_bar_spinner_advances_when_running():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.running = True
        start = bar._frame
        bar._tick()
        assert bar._frame != start
        bar.running = False
        held = bar._frame
        bar._tick()  # idle → frame frozen
        assert bar._frame == held


@pytest.mark.asyncio
async def test_status_bar_working_shows_verb_elapsed_and_tokens():
    """A running turn shows ``<spinner> <verb>… (Ns · Nk tok)`` + ` │ ` fields."""
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.update_usage(UsageUpdated(total_tokens=3400, model="gpt-4"))
        bar.running = True  # watch_running stamps start time + picks a verb
        plain = bar.render().plain
        assert bar._verb in plain  # a rotating activity verb
        assert "…" in plain
        assert "0s" in plain  # live elapsed counter (just started)
        assert "3.4k tok" in plain  # compact live token count
        assert " \u2502 " in plain  # ` │ ` separator between working + model


@pytest.mark.asyncio
async def test_status_bar_idle_resets_elapsed():
    """Ending a turn clears the start stamp so an idle bar shows no ``(Ns)``."""
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.running = True
        assert bar._run_started > 0.0
        bar.running = False
        assert bar._run_started == 0.0


def test_format_tok_compacts_counts():
    from metagpt.cli.consumers.textual.widgets import _format_tok

    assert _format_tok(840) == "840"
    assert _format_tok(3400) == "3.4k"
    assert _format_tok(12000) == "12k"


# --------------------------------------------------------------------------
# StatusBar thinking (✻ 思考中) + token-burst sparkline
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_bar_thinking_shows_reasoning_label():
    """A reasoning stream flips the bar to the distinct ``✻ 思考中`` state."""
    from metagpt.cli.consumers.render.palette import COMPACT

    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.set_thinking(True)
        plain = bar.render().plain
        assert "思考中" in plain
        assert COMPACT in plain  # ✻ thinking marker leads the label (not the ⠋ spinner)


@pytest.mark.asyncio
async def test_status_bar_thinking_clears_back_to_idle():
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.set_thinking(True)
        bar.set_thinking(False)
        assert "思考中" not in bar.render().plain


@pytest.mark.asyncio
async def test_status_bar_token_bursts_render_a_sparkline():
    """Successive usage updates feed a per-turn token-burst sparkline segment."""
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        for total in (100, 300, 600, 1500):  # deltas: 100, 200, 300, 900
            bar.update_usage(UsageUpdated(total_tokens=total))
        assert bar._tok_history == [100, 200, 300, 900]
        plain = bar.render().plain
        assert any(g in plain for g in "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588")


@pytest.mark.asyncio
async def test_status_bar_single_usage_update_has_no_sparkline():
    # One sample isn't a trend — no sparkline until there are at least two.
    async with _Harness().run_test() as pilot:
        bar = await pilot.app.add(StatusBar())
        bar.update_usage(UsageUpdated(total_tokens=500))
        assert bar._tok_history == [500]
        plain = bar.render().plain
        assert not any(g in plain for g in "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588")


# --------------------------------------------------------------------------
# ToolCallWidget bullet blink (running) + status colour (on completion)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_widget_blinks_while_running_then_stops_on_complete():
    async with _Harness().run_test() as pilot:
        started = ToolCallStarted(tool_name="Bash", title="Bash", headline="ls", tool_use_id="tu-b")
        widget = await pilot.app.add(ToolCallWidget(started))
        assert widget._blink_timer is not None  # pulsing while the call runs
        widget._pulse()
        assert widget._blink is True  # a pulse toggled the band on
        widget.complete(ToolCallCompleted(tool_name="Bash", tool_use_id="tu-b", summary="ok"))
        assert widget._blink is False  # settles off
        assert widget._blink_timer is None  # timer released once done


@pytest.mark.asyncio
async def test_tool_widget_completed_before_mount_never_blinks():
    async with _Harness().run_test() as pilot:
        started = ToolCallStarted(tool_name="Read", tool_use_id="tu-r")
        widget = ToolCallWidget(started)
        widget.complete(ToolCallCompleted(tool_name="Read", tool_use_id="tu-r", summary="12 lines"))
        await pilot.app.add(widget)  # on_mount sees an already-completed call
        assert widget._blink_timer is None


@pytest.mark.asyncio
async def test_prompt_multiline_paste_stashes_placeholder_and_expands():
    """A multi-line paste shows a one-line token but ``consume_value`` restores it.

    Regression for "输入框内粘贴的换行内容没有": Textual's single-line ``Input``
    drops everything after the first line of a paste. ``PromptInput`` instead
    stages the real text behind a compact placeholder and expands it on submit.
    """
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        pasted = "line1\nline2\nline3"
        prompt._on_paste(Paste(text=pasted))
        # The visible value is a single-line placeholder (no raw newline leaked).
        assert "\n" not in prompt.value
        assert "pasted 3 lines" in prompt.value
        # …but consuming it on submit yields the full multi-line text back.
        assert prompt.consume_value() == pasted
        # The store resets after consumption.
        assert prompt._pastes == {}


@pytest.mark.asyncio
async def test_prompt_carriage_return_paste_is_multiline():
    """A CR/CRLF-delimited paste (what real terminals send) is treated as multi-line.

    Regression for "输入框粘贴换行丢失未解决": terminals encode paste line breaks as
    ``\\r`` (or ``\\r\\n``), not ``\\n``. ``_on_paste`` normalises to ``\\n`` before
    deciding, so the block is stashed behind a placeholder and ``consume_value``
    yields the FULL text with proper ``\\n`` newlines (not just the first line).
    """
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text="line1\rline2\r\nline3"))
        assert "\n" not in prompt.value  # visible field is a single-line placeholder
        assert "pasted 3 lines" in prompt.value
        # Consumed value carries LF-normalised newlines, all three lines intact.
        assert prompt.consume_value() == "line1\nline2\nline3"


@pytest.mark.asyncio
async def test_prompt_singleline_paste_inserts_verbatim():
    """A single-line paste keeps Textual's default behaviour (no placeholder)."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text="just one line"))
        assert prompt.value == "just one line"
        assert prompt._pastes == {}
        assert prompt.consume_value() == "just one line"


@pytest.mark.asyncio
async def test_prompt_paste_mixed_with_typed_text_expands_inline():
    """A placeholder embedded among typed text expands in place on submit."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt.insert_text_at_cursor("see: ")
        prompt._on_paste(Paste(text="a\nb"))
        prompt.insert_text_at_cursor(" done")
        assert prompt.consume_value() == "see: a\nb done"


@pytest.mark.asyncio
async def test_prompt_base_paste_handler_is_prevented():
    """Our ``_on_paste`` cancels the default so the base ``Input._on_paste`` can't run.

    Regression for "复制粘贴重复2次": Textual dispatches a ``Paste`` to EVERY
    ``_on_paste`` down the MRO (``_get_dispatch_methods`` reads each class's own
    ``__dict__``), so the base ``Input._on_paste`` — which raw-inserts the paste's
    first line — ran right after ours and leaked that line next to our placeholder.
    ``prevent_default()`` sets ``_no_default_action`` which breaks that MRO walk;
    ``event.stop()`` alone only halts bubbling to parents. We assert a multi-line
    paste leaves ONLY the placeholder (no leaked first line) and the event was
    marked prevented.
    """
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        event = Paste(text="● 你好\n跑测试")
        prompt._on_paste(event)
        assert event._no_default_action is True  # base handler won't run
        assert prompt.value == "[#1 pasted 2 lines]"  # ONLY the placeholder
        assert "你好" not in prompt.value  # no leaked first line
        assert prompt.consume_value() == "● 你好\n跑测试"


@pytest.mark.asyncio
async def test_prompt_dropped_file_path_is_cleaned(tmp_path):
    """Dragging a file inserts its cleaned path, not the raw shell-escaped text.

    Terminals report a drop as a shell-escaped/quoted absolute path; ``_on_paste``
    recognises an existing path and strips the escapes so the agent can read it.
    """
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    dropped = tmp_path / "a file (1).txt"
    dropped.write_text("hi", encoding="utf-8")
    # What a terminal sends on drop: spaces/parens backslash-escaped.
    escaped = str(dropped).replace(" ", r"\ ").replace("(", r"\(").replace(")", r"\)")

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text=escaped))
        # The field holds the real path (space-containing → re-quoted), no `\`.
        assert prompt.value == f'"{dropped}"'
        assert "\\" not in prompt.value
        assert prompt._pastes == {}


@pytest.mark.asyncio
async def test_prompt_nonexistent_path_falls_through_to_text(tmp_path):
    """A slash-leading string that isn't an existing file stays ordinary text."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text="/nope/not/here.txt is the path"))
        assert prompt.value == "/nope/not/here.txt is the path"


@pytest.mark.asyncio
async def test_prompt_escape_clears_field():
    """A single Esc empties the prompt and drops any staged paste text."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt.insert_text_at_cursor("half-typed message")
        prompt._on_paste(Paste(text="line1\nline2"))  # stages a placeholder
        assert prompt.value != ""
        assert prompt._pastes  # staged
        await pilot.press("escape")
        assert prompt.value == ""
        assert prompt._pastes == {}


def _write_png(path) -> None:
    """Write a tiny valid PNG so drop/staging tests exercise the real reader."""
    from PIL import Image

    Image.new("RGB", (4, 4), (200, 100, 50)).save(str(path))


@pytest.mark.asyncio
async def test_prompt_dropped_image_is_staged_as_token(tmp_path):
    """Dragging an image stages a compact token + base64, not the raw path.

    The dropped image path is recognised by extension, read to base64, and shown
    behind an ``[image #n: name]`` token so ``consume_images`` can attach it to
    the turn while the visible field stays a one-liner.
    """
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    img = tmp_path / "pic.png"
    _write_png(img)

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text=str(img)))
        # Visible field is the token, not the filesystem path.
        assert prompt.value == f"[image #1: {img.name}]"
        assert str(img) not in prompt.value
        images = prompt.consume_images()
        assert len(images) == 1
        assert images[0]["path"] == str(img)
        assert images[0]["mime"] == "image/png"
        assert images[0]["b64"]  # non-empty base64 payload
        assert prompt._images == []  # drained on consume


@pytest.mark.asyncio
async def test_prompt_consume_images_drops_removed_tokens(tmp_path):
    """An image whose token was deleted from the field is not sent."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    img = tmp_path / "pic.png"
    _write_png(img)

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text=str(img)))
        prompt.value = "no token here"  # user erased the placeholder
        assert prompt.consume_images() == []


@pytest.mark.asyncio
async def test_prompt_dropped_nonimage_file_stays_a_path(tmp_path):
    """A dropped non-image file is still inserted as a (cleaned) path, not staged."""
    from textual.events import Paste

    from metagpt.cli.consumers.textual.widgets import PromptInput

    doc = tmp_path / "notes.txt"
    doc.write_text("hi", encoding="utf-8")

    async with _Harness().run_test() as pilot:
        prompt = await pilot.app.add(PromptInput())
        prompt.focus()
        await pilot.pause()
        prompt._on_paste(Paste(text=str(doc)))
        assert prompt.value == str(doc)
        assert prompt.consume_images() == []


@pytest.mark.asyncio
async def test_media_row_renders_inline_image(tmp_path):
    """``MediaRow`` paints an image inline when the ref is a readable image file."""
    from metagpt.cli.common.view import MediaBlock
    from metagpt.cli.consumers.textual.widgets import MediaRow

    img = tmp_path / "pic.png"
    _write_png(img)

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(MediaRow(MediaBlock(media_kind="image", ref=str(img))))
        # A Group (caption + half-block image) rather than the bare caption Text.
        from rich.console import Group

        assert isinstance(row._Static__content, Group)


@pytest.mark.asyncio
async def test_media_row_missing_image_degrades_to_caption():
    """A non-existent image ref degrades to the reference caption (no crash)."""
    from metagpt.cli.common.view import MediaBlock
    from metagpt.cli.consumers.textual.widgets import MediaRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(MediaRow(MediaBlock(media_kind="image", ref="/no/such.png")))
        # Falls back to the plain caption Text, referencing the path.
        assert "/no/such.png" in row._Static__content.plain


@pytest.mark.asyncio
async def test_file_diff_row_renders_caption_and_diff():
    """``FileDiffRow`` builds a caption naming the file + the synthesized diff."""
    from metagpt.cli.common.view import FileDiffBlock
    from metagpt.cli.consumers.textual.widgets import FileDiffRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(
            FileDiffRow(FileDiffBlock(path="/tmp/a.py", old="x = 1\n", new="x = 2\n"))
        )
        from rich.console import Group

        content = row._Static__content
        assert isinstance(content, Group)
        # The caption (first renderable) names the file and the update verb.
        caption = content.renderables[0]
        assert "/tmp/a.py" in caption.plain
        assert "(updated)" in caption.plain


@pytest.mark.asyncio
async def test_file_diff_row_caption_verb_for_creation():
    """A creation (empty old) labels the caption ``(created)``."""
    from metagpt.cli.common.view import FileDiffBlock
    from metagpt.cli.consumers.textual.widgets import FileDiffRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(
            FileDiffRow(FileDiffBlock(path="/tmp/new.py", old="", new="hi\n"))
        )
        caption = row._Static__content.renderables[0]
        assert "(created)" in caption.plain


def _has_link_span(text, url: str) -> bool:
    """True when *text* has a span whose style links to *url*."""
    from rich.style import Style

    for span in text.spans:
        style = span.style
        if isinstance(style, str):
            style = Style.parse(style)
        if getattr(style, "link", None) == url:
            return True
    return False


@pytest.mark.asyncio
async def test_notice_row_linkifies_bare_url():
    """A URL in a system notice becomes a clickable link span."""
    from metagpt.cli.common.view import Notice
    from metagpt.cli.consumers.textual.widgets import NoticeRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(NoticeRow(Notice(text="see https://example.com now", level="info")))
        assert _has_link_span(row._Static__content, "https://example.com")


@pytest.mark.asyncio
async def test_system_reminder_row_renders_note_glyph():
    """A SystemReminder renders as a dim ⚑ note carrying the summary text."""
    from metagpt.cli.common.view import SystemReminder
    from metagpt.cli.consumers.textual.style import NOTE
    from metagpt.cli.consumers.textual.widgets import SystemReminderRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(SystemReminderRow(SystemReminder(text="Git status · Files changed")))
        plain = row._Static__content.plain
        assert NOTE in plain
        assert "Git status · Files changed" in plain


@pytest.mark.asyncio
async def test_conversation_compacted_row_renders_marker():
    """A ConversationCompacted renders the dim ✻ boundary with the retained count."""
    from metagpt.cli.common.view import ConversationCompacted
    from metagpt.cli.consumers.textual.style import COMPACT
    from metagpt.cli.consumers.textual.widgets import ConversationCompactedRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(
            ConversationCompactedRow(ConversationCompacted(summary="recap", message_count=5))
        )
        plain = row._Static__content.plain
        assert COMPACT in plain
        assert "对话已压缩" in plain
        assert "保留 5 条消息" in plain


@pytest.mark.asyncio
async def test_error_row_linkifies_bare_url():
    """A URL in an error message becomes a clickable link span (inside the bullet_row)."""
    from rich.console import Console

    from metagpt.cli.common.view import ErrorRaised
    from metagpt.cli.consumers.textual.widgets import ErrorRow

    async with _Harness().run_test() as pilot:
        row = await pilot.app.add(ErrorRow(ErrorRaised(text="failed: https://example.com/err")))
        # The error text is wrapped in a bullet_row grid; render it and assert the
        # URL emits an OSC 8 hyperlink.
        console = Console(file=__import__("io").StringIO(), force_terminal=True, color_system="truecolor", width=80)
        console.print(row._Static__content)
        out = console.file.getvalue()
        assert "\x1b]8;;" in out
        assert "https://example.com/err" in out


class _FakeClick:
    """A minimal stand-in for a Textual ``Click`` carrying a link style + ctrl flag."""

    def __init__(self, url: str, *, ctrl: bool) -> None:
        from rich.style import Style

        self.style = Style(link=url) if url else Style()
        self.ctrl = ctrl
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_ctrl_click_on_link_opens_url():
    """Ctrl+click over a link span opens the URL via ``app.open_url``."""
    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        opened: list[str] = []
        pilot.app.open_url = lambda url, **kw: opened.append(url)
        event = _FakeClick("https://example.com", ctrl=True)
        await block._on_click(event)
        assert opened == ["https://example.com"]
        assert event.stopped is True  # consumed, so no selection starts


@pytest.mark.asyncio
async def test_plain_click_on_link_does_not_open_url():
    """A plain (no-ctrl) click over a link does NOT open it — selection wins.

    Matches claude-code: the terminal only follows a link on Ctrl+click, so a
    plain click here must fall through to the base handler (starting a drag
    selection) instead of navigating.
    """
    from rich.style import Style
    from textual.events import Click

    async with _Harness().run_test() as pilot:
        block = await pilot.app.add(AssistantBlock())
        opened: list[str] = []
        pilot.app.open_url = lambda url, **kw: opened.append(url)
        # A real Click (ctrl=False) so the base handler can run without raising.
        event = Click(
            widget=block, x=0, y=0, delta_x=0, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False,
            style=Style(link="https://example.com"),
        )
        await block._on_click(event)
        assert opened == []  # nothing opened on a plain click
