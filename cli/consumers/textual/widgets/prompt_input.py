#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The bottom prompt input widget.

:class:`PromptInput` extends Textual's single-line ``Input`` with multi-line
paste (stashed behind a placeholder token), file drag-drop (path cleanup +
inline-image staging) and an Esc-clears-field binding.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from typing import Any, Optional

from textual.binding import Binding
from textual.widgets import Input

from metagpt.cli.consumers.textual.style import PROMPT_SYMBOL


class PromptInput(Input):
    """The bottom prompt input — an orange ``❯`` chevron gutter + text field.

    Textual's ``Input`` has no built-in prompt symbol; the ``❯`` is supplied via
    a ``border-title``-style left accent in the app CSS. The subclass exists so
    the app can target it in CSS and post ``Input.Submitted`` from it distinctly.

    **Multi-line paste.** ``Input`` is single-line: its ``_on_paste`` keeps only
    ``event.text.splitlines()[0]``, silently dropping the rest of a multi-line
    paste (a traceback / code block). Embedding the raw ``\\n`` in ``value`` isn't
    an option either — the single-line renderer emits the literal newline and
    corrupts the layout + cursor. So (mirroring claude-code) a multi-line paste
    is replaced in the visible field by a compact one-line **placeholder token**
    while the real text is stashed in ``_pastes``; :meth:`consume_value` expands
    the tokens back to the full multi-line text on submit. Single-line pastes use
    the default behaviour untouched.

    **File drag-drop.** Dragging a file into the terminal arrives as a bracketed
    paste of its path — shell-escaped (``\\ `` for spaces) and often quoted. When
    a paste is made up entirely of *existing absolute paths* we clean those
    escapes/quotes; a dropped **image** (png/jpg/jpeg/gif/webp) is staged as a
    base64 attachment and shown as a ``[image #N: name]`` token (sent to the model
    as multimodal content on submit — see :meth:`consume_images`), and any other
    file inserts its bare path so the agent can ``Read`` it (mirrors claude-code's
    drop-to-mention).

    **Esc clears the field.** A single ``Esc`` empties the prompt (and the paste
    store) — the main input has no other use for the key.
    """

    BINDINGS = [Binding("escape", "clear_prompt", "Clear", show=False)]

    DEFAULT_CSS = """
    PromptInput {
        border: round $brand;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    PromptInput:focus {
        border: round $brand;
    }
    """

    #: File extensions a dropped path is staged as an inline image attachment for.
    _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("placeholder", "Type a message… (/help for commands)")
        super().__init__(**kwargs)
        # token -> real multi-line text, for pastes staged as placeholders.
        self._pastes: dict[str, str] = {}
        self._paste_seq = 0
        # Dropped image files staged as attachments, each
        # ``{"token", "path", "b64", "mime"}``; consumed (with the text) on submit
        # and sent to the model as multimodal content.
        self._images: list[dict] = []

    def _on_paste(self, event: Any) -> None:
        # Textual dispatches an event to EVERY ``_on_paste`` down the MRO
        # (``_get_dispatch_methods`` reads each class's own ``__dict__``), so the
        # base ``Input._on_paste`` — which raw-inserts ``event.text.splitlines()[0]``
        # — runs right after ours unless we cancel the default. ``event.stop()`` only
        # halts *bubbling* to parents; ``prevent_default()`` sets ``_no_default_action``
        # which breaks that MRO walk. Without it the base handler leaked the paste's
        # first line into the field next to our placeholder ("复制粘贴重复2次").
        event.prevent_default()
        event.stop()
        text = getattr(event, "text", "")
        # Real terminals encode the line breaks inside a bracketed paste as a
        # carriage return (``\r``) or CRLF — NOT a bare ``\n``. Normalise first,
        # else a pasted multi-line block looks single-line and only its first line
        # survives (the "粘贴换行丢失" bug).
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        # File drag-drop: a paste that is only existing absolute path(s) is a
        # dropped file, not text. A dropped *image* is staged as a multimodal
        # attachment (base64) and shown as a ``[image #N: name]`` token; other
        # dropped files insert their cleaned path so the agent can Read them.
        dropped = self._maybe_dropped_paths(text)
        if dropped is not None:
            inserted = self._stage_dropped(dropped)
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(inserted)
            else:
                self.replace(inserted, *selection)
            return
        if "\n" not in text:
            # Single-line paste: insert verbatim (mirrors the default behaviour).
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *selection)
            return
        # Multi-line: stash the real (LF-normalised) text, insert a single-line
        # placeholder token; ``consume_value`` expands it back on submit.
        self._paste_seq += 1
        n = len(text.splitlines())
        token = f"[#{self._paste_seq} pasted {n} lines]"
        self._pastes[token] = text
        selection = self.selection
        if selection.is_empty:
            self.insert_text_at_cursor(token)
        else:
            self.replace(token, *selection)

    def consume_value(self) -> str:
        """Return ``value`` with paste placeholders expanded, then reset the store.

        Called by the app on submit so the agent receives the full multi-line
        text a placeholder token stood in for. Tokens the user edited/deleted
        simply don't match and are left as-is.
        """
        value = self.value
        for token, real in self._pastes.items():
            value = value.replace(token, real)
        self._pastes.clear()
        self._paste_seq = 0
        return value

    def consume_images(self) -> list[dict]:
        """Return the staged image attachments still referenced in the field, reset.

        Only images whose ``[image #N: name]`` token survives in the submitted
        text are returned — a token the user deleted drops its attachment too —
        then the store is cleared. Each entry is ``{"token", "path", "b64",
        "mime"}``; the driver forwards ``b64`` to the model as multimodal content.
        """
        value = self.value
        kept = [img for img in self._images if img["token"] in value]
        self._images = []
        return kept

    # ------------------------------------------------------------------
    # File drag-drop
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_dropped_path(token: str) -> str:
        """Strip surrounding quotes and shell backslash-escapes from a path.

        A terminal reports a dropped path shell-quoted (``'/a/b'``) or with
        spaces/parens escaped (``/a/b\\ c\\(1\\).txt``); both forms are undone
        here so the result is the real on-disk path.
        """
        token = token.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        return re.sub(r"\\(.)", r"\1", token)

    def _maybe_dropped_paths(self, text: str) -> Optional[list[str]]:
        """Return the cleaned path list iff *text* is only dropped file(s).

        Splits the paste into candidate paths (newlines, and spaces that precede
        an absolute POSIX path — spaces *within* a path are shell-escaped, so
        they don't match). Every token must clean to an existing absolute path,
        otherwise this is ordinary text and we return ``None``.
        """
        parts: list[str] = []
        for line in text.split("\n"):
            parts.extend(re.split(r" (?=/)", line))
        tokens = [p for p in (part.strip() for part in parts) if p]
        if not tokens:
            return None
        cleaned: list[str] = []
        for token in tokens:
            path = self._clean_dropped_path(token)
            if not path.startswith("/") or not os.path.exists(path):
                return None
            cleaned.append(path)
        return cleaned

    def _stage_dropped(self, paths: list[str]) -> str:
        """Turn cleaned dropped path(s) into the text to insert into the field.

        Image files are staged as base64 attachments (see :meth:`_stage_image`)
        and represented by a ``[image #N: name]`` token; every other file inserts
        its bare path (re-quoted when it contains spaces) so the agent can Read
        it. An image that can't be read falls back to its path.
        """
        out: list[str] = []
        for path in paths:
            if path.lower().endswith(self._IMAGE_EXTS):
                staged = self._stage_image(path)
                if staged is not None:
                    out.append(staged["token"])
                    continue
            out.append(f'"{path}"' if " " in path else path)
        return " ".join(out)

    def _stage_image(self, path: str) -> Optional[dict]:
        """Read an image file, base64-encode it, and record it as an attachment."""
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        if not raw:
            return None
        self._paste_seq += 1
        entry = {
            "token": f"[image #{self._paste_seq}: {os.path.basename(path)}]",
            "path": path,
            "b64": base64.b64encode(raw).decode("ascii"),
            "mime": mimetypes.guess_type(path)[0] or "image/png",
        }
        self._images.append(entry)
        return entry

    # ------------------------------------------------------------------
    # Esc clears the field
    # ------------------------------------------------------------------
    def action_clear_prompt(self) -> None:
        """Empty the prompt and drop any staged paste text / image attachments."""
        self.value = ""
        self._pastes.clear()
        self._images = []
        self._paste_seq = 0


__all__ = ["PromptInput", "PROMPT_SYMBOL"]
