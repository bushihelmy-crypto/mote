#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the parser factory/inference helpers and package exports.

- ``infer_native_tool_provider`` keys the native tool-spec envelope off the
  *model name* ("claude" -> anthropic, everything/missing -> openai).
- ``make_command_channel`` maps a RoleSchema.command_protocol value to a channel
  ("native" -> NativeToolChannel, anything else -> XmlCommandChannel).
"""
from __future__ import annotations

import pytest

import metagpt.parser as parser_pkg
from metagpt.parser import (
    NativeToolChannel,
    XmlCommandChannel,
    infer_native_tool_provider,
    make_command_channel,
)

from .conftest import _LLMConfig


class TestInferNativeToolProvider:
    @pytest.mark.parametrize(
        "model",
        ["claude-opus-4-8", "claude-sonnet-4-6", "anthropic/claude-3", "MyClaude", "CLAUDE-X"],
    )
    def test_claude_models_map_to_anthropic(self, model):
        assert infer_native_tool_provider(_LLMConfig(model)) == "anthropic"

    @pytest.mark.parametrize("model", ["gpt-4", "gpt-4o", "o1-mini", "gemini-pro", "deepseek-chat"])
    def test_non_claude_models_map_to_openai(self, model):
        assert infer_native_tool_provider(_LLMConfig(model)) == "openai"

    def test_case_insensitive(self):
        # Matching is done on the lowercased name.
        assert infer_native_tool_provider(_LLMConfig("Claude-3-Opus")) == "anthropic"

    def test_none_model_defaults_to_openai(self):
        assert infer_native_tool_provider(_LLMConfig(None)) == "openai"

    def test_empty_model_defaults_to_openai(self):
        assert infer_native_tool_provider(_LLMConfig("")) == "openai"

    def test_missing_model_attribute_defaults_to_openai(self):
        # getattr(..., "model", None) -> None -> openai.
        class NoModel:
            pass

        assert infer_native_tool_provider(NoModel()) == "openai"


class TestMakeCommandChannel:
    def test_native_protocol_builds_native_channel(self):
        ch = make_command_channel("native")
        assert isinstance(ch, NativeToolChannel)

    def test_native_uses_default_provider(self):
        assert make_command_channel("native")._provider == "openai"

    def test_native_passes_provider(self):
        ch = make_command_channel("native", provider="anthropic")
        assert isinstance(ch, NativeToolChannel)
        assert ch._provider == "anthropic"

    def test_xml_protocol_builds_xml_channel(self):
        assert isinstance(make_command_channel("xml"), XmlCommandChannel)

    @pytest.mark.parametrize("protocol", ["", "unknown", "XML", "Native", "json"])
    def test_unknown_protocol_falls_back_to_xml(self, protocol):
        # Anything that isn't exactly "native" -> the safe XML default.
        assert isinstance(make_command_channel(protocol), XmlCommandChannel)

    def test_provider_ignored_for_xml(self):
        # provider only matters for native; xml accepts and ignores it.
        assert isinstance(make_command_channel("xml", provider="anthropic"), XmlCommandChannel)


class TestPackageExports:
    def test_all_names_exported(self):
        expected = {
            "CommandChannel",
            "NativeToolChannel",
            "XmlCommandChannel",
            "infer_native_tool_provider",
            "make_command_channel",
            "OUTPUT_SECTION",
        }
        assert expected <= set(parser_pkg.__all__)

    def test_all_exported_names_are_importable(self):
        for name in parser_pkg.__all__:
            assert hasattr(parser_pkg, name), name

    def test_command_channel_is_the_base_class(self):
        from metagpt.common.base import CommandChannel

        assert parser_pkg.CommandChannel is CommandChannel

    def test_output_section_reexported(self):
        from metagpt.parser.prompts import OUTPUT_SECTION

        assert parser_pkg.OUTPUT_SECTION is OUTPUT_SECTION
