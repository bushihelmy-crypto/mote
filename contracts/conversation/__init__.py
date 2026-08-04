"""Stable conversation messages, history, context, and compaction contracts."""

from mote.contracts.conversation.codec import decode_message, dump_message, encode_message, load_message
from mote.contracts.conversation.context import ContextManagerConfig, FoldState, TokenState
from mote.contracts.conversation.document import ActionOutput, CauseBy, Document, Documents, Resource
from mote.contracts.conversation.messages import (
    AIMessage,
    LLMCallContext,
    Message,
    ResourceMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
    to_role_content_dicts,
)
from mote.contracts.conversation.queue import LongTermMemoryItem, MessagePriority, MessageQueue, QueuedMessage

__all__ = [
    "AIMessage",
    "ActionOutput",
    "CauseBy",
    "ContextManagerConfig",
    "Document",
    "Documents",
    "FoldState",
    "LLMCallContext",
    "LongTermMemoryItem",
    "Message",
    "MessagePriority",
    "MessageQueue",
    "QueuedMessage",
    "ResourceMessage",
    "Resource",
    "SystemMessage",
    "TokenState",
    "ToolMessage",
    "UserMessage",
    "to_role_content_dicts",
    "decode_message",
    "dump_message",
    "encode_message",
    "load_message",
]
