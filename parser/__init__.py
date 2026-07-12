"""metagpt.parser — protocol parsing and formatting for the react loop."""
from metagpt.common.base import CommandChannel
from metagpt.parser.native_channel import (
    NativeToolChannel,
    infer_native_tool_provider,
    make_command_channel,
)
from metagpt.common.prompt.output import OUTPUT_SECTION
from metagpt.parser.xml_channel import XmlCommandChannel

__all__ = [
    "CommandChannel",
    "NativeToolChannel",
    "XmlCommandChannel",
    "infer_native_tool_provider",
    "make_command_channel",
    "OUTPUT_SECTION",
]
