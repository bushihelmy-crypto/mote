"""Provider-independent command decoding and projection."""
from mote.kernel.commands.channel import CommandChannel
from mote.kernel.commands.native import NativeToolChannel, make_command_channel
from mote.kernel.commands.prompts import OUTPUT_SECTION
from mote.kernel.commands.xml.channel import XmlCommandChannel

__all__ = [
    "CommandChannel",
    "NativeToolChannel",
    "XmlCommandChannel",
    "make_command_channel",
    "OUTPUT_SECTION",
]
