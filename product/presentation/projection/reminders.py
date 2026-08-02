#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``<system-reminder>`` envelope detection + one-line summarisation.

mote's turn-context bus wraps each injected per-turn block (git/token/
changed-files/skill/tool/compaction context) in a single ``<system-reminder>``
envelope written into history as a *user* message. These helpers let the
projector tell that injected context apart from the human's own typed prompt and
condense it to a dim heading summary so the human sees *what* was fed to the
model without the raw prose crowding the transcript.

Pure string functions only — split out of ``projector.py`` so the fold's main
class stays focused on the ``AgentEvent → ViewEvent`` fold itself.
"""

from __future__ import annotations

import re
from typing import List

# Envelope detection/peeling is owned by the bottom-layer marker authority
# — the same literal the turn-context bus writes on
# the other side, so the two can never desync. This module keeps only the
# CLI-specific *summarisation* (heading extraction, skill counting) below.
from mote.runtime.context.markers import is_system_reminder as _is_system_reminder
from mote.runtime.context.markers import strip_system_reminder as _strip_envelope

# A markdown ATX heading (``# …`` … ``###### …``) — the per-source block boundary.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# A skill-listing/activation heading ("Available Skills", "New Skills available",
# "Relevant Skills") — the blocks whose summary should carry a skill *count*.
_SKILL_HEADING_RE = re.compile(r"skill", re.IGNORECASE)
# A deferred-tool menu heading ("Additional tools", "Additional tools (search to
# enable)") — the block whose summary should list the tool *names* (not a count):
# the human wants to see exactly which tools are search-to-enable, mirroring how
# git/skill blocks surface their contents.
_TOOL_HEADING_RE = re.compile(r"additional tools", re.IGNORECASE)


def _split_blocks(inner: str) -> List[tuple[str, List[str]]]:
    """Split the envelope into ``(heading, body_lines)`` blocks at ATX headings.

    Splitting on headings (not blank lines) keeps a single source's block intact
    even when it contains internal blank lines (a skill listing's prose + table).
    Text before the first heading becomes a headless block (``heading == ""``).
    """
    blocks: List[tuple[str, List[str]]] = []
    heading = ""
    body: List[str] = []
    for line in inner.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            if heading or any(b.strip() for b in body):
                blocks.append((heading, body))
            heading, body = m.group(2).strip(), []
        else:
            body.append(line)
    if heading or any(b.strip() for b in body):
        blocks.append((heading, body))
    return blocks


def _count_skill_rows(body: List[str]) -> int:
    """Count skill entries in a skill block — markdown table rows or ``- `` bullets.

    A skill listing renders as a ``| Skill | Description | Arguments |`` table (its
    header + ``|---|`` separator are skipped) or, when the token budget is tight,
    as a ``- name`` bullet list; skill activation always uses ``- name: …`` bullets.
    Intro prose (the ``Skill(...)`` invocation hint) is neither, so it's ignored.
    """
    count = 0
    for line in body:
        s = line.strip()
        if not s:
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(c and set(c) <= {"-", ":"} for c in cells):
                continue  # ``|---|:--|`` separator row
            if cells and cells[0].lower() == "skill":
                continue  # header row
            count += 1
        elif s.startswith("- "):
            count += 1
    return count


def _tool_names(body: List[str]) -> List[str]:
    """Extract deferred-tool names from a "# Additional tools" block's bullets.

    Both deferred-menu sources render ``- <name>: <one-line desc>`` bullets (the
    withhold-path :class:`DeferredToolIndexContextSource` and the split-path
    :class:`SplitToolMenuContextSource`). We keep only the ``<name>`` (the text
    before the first ``:``) so the transcript summary lists exactly which tools
    are search-to-enable. Intro prose is neither a bullet nor carries a name, so
    it is ignored.
    """
    names: List[str] = []
    for line in body:
        s = line.strip()
        if not s.startswith("- "):
            continue
        entry = s[2:].strip()
        name = entry.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _summarize_reminder(content: str) -> str:
    """Condense a ``<system-reminder>`` envelope to a one-line heading summary.

    The bus joins each source's block (``# Heading\\n<body>``) inside the envelope.
    We strip the tags, split into blocks at their headings, and keep only each
    block's heading (falling back to its first non-empty line) — dropping the
    prose/JSON bodies. Two blocks enrich their heading from the body: a *skill*
    block carries the count of available skills (e.g. "Available Skills (3)"), and
    a *deferred-tool* block ("Additional tools") lists the tool NAMES (e.g.
    "Additional tools (search to enable): WebSearch, RunGraph") so the human sees
    exactly which tools are search-to-enable — matching how git/skill blocks
    surface their contents. Headings are joined with ``·``.
    """
    parts: List[str] = []
    for heading, body in _split_blocks(_strip_envelope(content)):
        if heading:
            label = heading
            if _TOOL_HEADING_RE.search(heading):
                # Deferred-tool menu: list the tool NAMES (not a count) so the
                # human sees exactly what is search-to-enable this turn.
                names = _tool_names(body)
                if names:
                    label = f"{heading}: {', '.join(names)}"
            elif _SKILL_HEADING_RE.search(heading):
                n = _count_skill_rows(body)
                if n:
                    label = f"{heading} ({n})"
            parts.append(label)
        else:
            for line in body:  # headless preamble → its first non-empty line
                if line.strip():
                    parts.append(line.strip())
                    break
    return " · ".join(parts)


__all__ = [
    "_is_system_reminder",
    "_strip_envelope",
    "_split_blocks",
    "_count_skill_rows",
    "_tool_names",
    "_summarize_reminder",
]
