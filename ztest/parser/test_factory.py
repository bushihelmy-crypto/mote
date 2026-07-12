#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the parser factory/inference helpers and package exports.

- ``infer_native_tool_provider`` keys the native tool-spec envelope off the
  *resolved transport* (the wire protocol of the endpoint that issues the
  request), NOT the model name: ANTHROPIC transport (api_type=anthropic or an
  anthropic.com base_url) -> "anthropic"; everything else -> "openai". This is
  what makes a Claude model behind an OpenAI-compatible gateway send the
  OpenAI-shaped ``tools`` the gateway actually understands.
- ``make_command_channel`` maps a RoleSchema.command_protocol value to a channel
  ("native" -> NativeToolChannel, anything else -> XmlCommandChannel).
"""
from __future__ import annotations

import pytest

import mote.parser as parser_pkg
from mote.common.config.config.llm_config import LLMType
from mote.parser import NativeToolChannel, XmlCommandChannel, infer_native_tool_provider, make_command_channel

from .conftest import _LLMConfig


class TestInferNativeToolProvider:
    def test_explicit_anthropic_api_type_maps_to_anthropic(self):
        cfg = _LLMConfig("claude-opus-4-6", api_type=LLMType.ANTHROPIC, base_url="https://api.anthropic.com")
        assert infer_native_tool_provider(cfg) == "anthropic"

    def test_anthropic_base_url_maps_to_anthropic(self):
        # Auto-detected native transport from the base_url, even with api_type=openai.
        cfg = _LLMConfig("claude-3", api_type=LLMType.OPENAI, base_url="https://api.anthropic.com/v1")
        assert infer_native_tool_provider(cfg) == "anthropic"

    def test_claude_via_openai_gateway_maps_to_openai(self):
        # The regression: a Claude model reached through an OpenAI-compatible
        # gateway must still emit OpenAI-shaped tools (the gateway translates),
        # otherwise the malformed ``tools`` are dropped and the model improvises.
        cfg = _LLMConfig("claude-opus-4-6", api_type=LLMType.OPENAI, base_url="https://gateway.example.com/v1")
        assert infer_native_tool_provider(cfg) == "openai"

    @pytest.mark.parametrize("model", ["gpt-4", "gpt-4o", "o1-mini", "gemini-pro", "deepseek-chat"])
    def test_openai_transport_maps_to_openai(self, model):
        cfg = _LLMConfig(model, api_type=LLMType.OPENAI, base_url="https://api.openai.com/v1")
        assert infer_native_tool_provider(cfg) == "openai"

    def test_defaults_to_openai(self):
        # Plain stub (api_type defaults to OPENAI, no anthropic base_url).
        assert infer_native_tool_provider(_LLMConfig("claude-3-opus")) == "openai"

    def test_malformed_config_defaults_to_openai(self):
        # resolve_api_type raising on a bad config degrades to the safe default.
        class NoFields:
            pass

        assert infer_native_tool_provider(NoFields()) == "openai"


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
        from mote.common.base import CommandChannel

        assert parser_pkg.CommandChannel is CommandChannel

    def test_output_section_reexported(self):
        from mote.common.prompt.output import OUTPUT_SECTION

        assert parser_pkg.OUTPUT_SECTION is OUTPUT_SECTION
