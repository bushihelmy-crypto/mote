"""Command-channel factory and provider-neutral package exports."""

from __future__ import annotations

import pytest

import mote.kernel.commands as parser_pkg
from mote.kernel.commands import NativeToolChannel, XmlCommandChannel, make_command_channel


class TestMakeCommandChannel:
    def test_native_protocol_builds_native_channel(self):
        assert isinstance(make_command_channel("native"), NativeToolChannel)

    def test_xml_protocol_builds_xml_channel(self):
        assert isinstance(make_command_channel("xml"), XmlCommandChannel)

    @pytest.mark.parametrize("protocol", ["", "unknown", "XML", "Native", "json"])
    def test_unknown_protocol_falls_back_to_xml(self, protocol):
        assert isinstance(make_command_channel(protocol), XmlCommandChannel)


class TestPackageExports:
    def test_all_names_exported(self):
        expected = {
            "CommandChannel",
            "NativeToolChannel",
            "XmlCommandChannel",
            "make_command_channel",
            "OUTPUT_SECTION",
        }
        assert expected <= set(parser_pkg.__all__)
        assert "infer_native_tool_provider" not in parser_pkg.__all__

    def test_all_exported_names_are_importable(self):
        for name in parser_pkg.__all__:
            assert hasattr(parser_pkg, name), name

    def test_command_channel_is_the_base_class(self):
        from mote.kernel.commands.channel import CommandChannel

        assert parser_pkg.CommandChannel is CommandChannel

    def test_output_section_reexported(self):
        from mote.kernel.commands.prompts import OUTPUT_SECTION

        assert parser_pkg.OUTPUT_SECTION is OUTPUT_SECTION
