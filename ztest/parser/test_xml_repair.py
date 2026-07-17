#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for truncated-XML repair in ``role_utils.loads_xml``.

A long freeform argument (e.g. Write's whole file body carried as the single
``<content>`` body) sometimes arrives truncated — the model's output is cut off
before the closing ``</content></Command>`` tags. The streaming lexer then raises
"Invalid XML" and the command is dropped. The repair path synthesizes the
missing close tags from the lexer's open state and re-parses, recovering the
command (mirroring the native channel's ``json_repair`` fallback).
"""
from __future__ import annotations

import pytest

from mote.common.utils.role_utils import loads_xml, parse_commands2

BODY = "def f():\n    return vector<int>()\n\nif a < b:\n    g()"


class TestLoadsXmlRepair:
    @pytest.mark.asyncio
    async def test_truncated_value_recovered_full(self):
        # Missing </content></Write>: the whole body must be recovered,
        # including the tail bytes the un-repaired lexer would have dropped.
        data = "<Write>\n<content>\n" + BODY + "\n"
        cmds, err = await loads_xml(data, {"Write"})
        assert err == ""
        assert len(cmds) == 1
        assert cmds[0]["command_name"] == "Write"
        assert cmds[0]["args"]["content"] == BODY

    @pytest.mark.asyncio
    async def test_missing_function_close_only(self):
        # Arg closed, but the function end tag is missing.
        data = "<Write>\n<content>\n" + BODY + "\n</content>\n"
        cmds, err = await loads_xml(data, {"Write"})
        assert err == ""
        assert cmds[0]["args"]["content"] == BODY

    @pytest.mark.asyncio
    async def test_patch_with_angle_brackets_recovered(self):
        # File bodies routinely contain '<' / '>' (templates, '->', comparisons).
        # These are part of the <content> value, not tags, and must survive repair.
        body = "template <typename T>\nvector<int> v;\nif (a < b) f();"
        data = "<Write>\n<content>\n" + body
        cmds, err = await loads_xml(data, {"Write"})
        assert err == ""
        assert cmds[0]["args"]["content"] == body

    @pytest.mark.asyncio
    async def test_well_formed_unchanged(self):
        data = "<Write>\n<content>\n" + BODY + "\n</content>\n</Write>"
        cmds, err = await loads_xml(data, {"Write"})
        assert err == ""
        assert cmds[0]["args"]["content"] == BODY

    @pytest.mark.asyncio
    async def test_earlier_command_kept_last_truncated_recovered(self):
        data = "<Read>\n<path>\na.py\n</path>\n</Read>\n" "<Write>\n<content>\n" + BODY + "\n"
        cmds, err = await loads_xml(data, {"Read", "Write"})
        assert err == ""
        assert [c["command_name"] for c in cmds] == ["Read", "Write"]
        assert cmds[1]["args"]["content"] == BODY

    @pytest.mark.asyncio
    async def test_unrecoverable_keeps_error(self):
        # No valid function tag at all -> nothing to close -> original failure path.
        # (ignore_text swallows prose, so this simply yields no commands.)
        cmds, err = await loads_xml("just prose, no tags", {"Write"})
        assert cmds == []


class TestParseCommands2Repair:
    @pytest.mark.asyncio
    async def test_truncated_recovered_via_public_entry(self):
        data = "<Write>\n<content>\n" + BODY + "\n"
        cmds, err = await parse_commands2(data, {"Write"})
        assert err == ""
        assert len(cmds) == 1
        assert cmds[0]["args"]["content"] == BODY
