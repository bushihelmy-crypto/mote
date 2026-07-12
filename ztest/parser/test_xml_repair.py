#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for truncated-XML repair in ``role_utils.loads_xml``.

A long freeform argument (e.g. ApplyPatch's whole patch carried as the single
``<input>`` body) sometimes arrives truncated — the model's output is cut off
before the closing ``</input></Command>`` tags. The streaming lexer then raises
"Invalid XML" and the command is dropped. The repair path synthesizes the
missing close tags from the lexer's open state and re-parses, recovering the
command (mirroring the native channel's ``json_repair`` fallback).
"""
from __future__ import annotations

import pytest

from mote.common.utils.role_utils import loads_xml, parse_commands2

PATCH = "*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch"


class TestLoadsXmlRepair:
    @pytest.mark.asyncio
    async def test_truncated_value_recovered_full(self):
        # Missing </input></ApplyPatch.run>: the whole patch must be recovered,
        # including the tail bytes the un-repaired lexer would have dropped.
        data = "<ApplyPatch.run>\n<input>\n" + PATCH + "\n"
        cmds, err = await loads_xml(data, {"ApplyPatch.run"})
        assert err == ""
        assert len(cmds) == 1
        assert cmds[0]["command_name"] == "ApplyPatch.run"
        assert cmds[0]["args"]["input"] == PATCH

    @pytest.mark.asyncio
    async def test_missing_function_close_only(self):
        # Arg closed, but the function end tag is missing.
        data = "<ApplyPatch.run>\n<input>\n" + PATCH + "\n</input>\n"
        cmds, err = await loads_xml(data, {"ApplyPatch.run"})
        assert err == ""
        assert cmds[0]["args"]["input"] == PATCH

    @pytest.mark.asyncio
    async def test_patch_with_angle_brackets_recovered(self):
        # Code patches routinely contain '<' / '>' (templates, '->', comparisons).
        # These are part of the <input> value, not tags, and must survive repair.
        patch = "*** Begin Patch\n*** Add File: t.cpp\n+vector<int> v;\n+if (a < b) f();\n*** End Patch"
        data = "<ApplyPatch.run>\n<input>\n" + patch
        cmds, err = await loads_xml(data, {"ApplyPatch.run"})
        assert err == ""
        assert cmds[0]["args"]["input"] == patch

    @pytest.mark.asyncio
    async def test_well_formed_unchanged(self):
        data = "<ApplyPatch.run>\n<input>\n" + PATCH + "\n</input>\n</ApplyPatch.run>"
        cmds, err = await loads_xml(data, {"ApplyPatch.run"})
        assert err == ""
        assert cmds[0]["args"]["input"] == PATCH

    @pytest.mark.asyncio
    async def test_earlier_command_kept_last_truncated_recovered(self):
        data = "<Read>\n<path>\na.py\n</path>\n</Read>\n" "<ApplyPatch.run>\n<input>\n" + PATCH + "\n"
        cmds, err = await loads_xml(data, {"Read", "ApplyPatch.run"})
        assert err == ""
        assert [c["command_name"] for c in cmds] == ["Read", "ApplyPatch.run"]
        assert cmds[1]["args"]["input"] == PATCH

    @pytest.mark.asyncio
    async def test_unrecoverable_keeps_error(self):
        # No valid function tag at all -> nothing to close -> original failure path.
        # (ignore_text swallows prose, so this simply yields no commands.)
        cmds, err = await loads_xml("just prose, no tags", {"ApplyPatch.run"})
        assert cmds == []


class TestParseCommands2Repair:
    @pytest.mark.asyncio
    async def test_truncated_recovered_via_public_entry(self):
        data = "<ApplyPatch.run>\n<input>\n" + PATCH + "\n"
        cmds, err = await parse_commands2(data, {"ApplyPatch.run"})
        assert err == ""
        assert len(cmds) == 1
        assert cmds[0]["args"]["input"] == PATCH
