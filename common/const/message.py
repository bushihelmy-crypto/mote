#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Message routing and metadata constants."""

MESSAGE_ROUTE_FROM = "sent_from"
MESSAGE_ROUTE_TO = "send_to"
MESSAGE_ROUTE_CAUSE_BY = "cause_by"
MESSAGE_META_ROLE = "role"
MESSAGE_ROUTE_TO_ALL = "<all>"
MESSAGE_ROUTE_TO_NONE = "<none>"
MESSAGE_ROUTE_TO_SELF = "<self>"  # Add this tag to replace `ActionOutput`

# Metadata defines
AGENT = "agent"
USE_ENCODED_MEDIA = "use_encoded_images"  # for compatibility, actually means "use_encoded_medida"
IMAGES = "images"
PDFS = "pdfs"
# Native tool-use metadata keys (carried in Message.metadata, never in content).
# TOOL_CALLS: list of {id, name, args} on an assistant message that invoked tools.
# TOOL_CALL_ID: the call id on a tool-result message (role="tool").
TOOL_CALLS = "tool_calls"
TOOL_CALL_ID = "tool_call_id"

# Message id
IGNORED_MESSAGE_ID = "0"
