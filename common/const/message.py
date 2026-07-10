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

# Resource-projection metadata keys (carried in Message.metadata).
# A "resource" is a dynamically-loaded capability body (e.g. a Skill body) that
# must survive history compaction. These keys are the TRUTH that outlives replay:
# Message.load reconstructs via the base Message.from_dict (cls(**m)), so the
# ResourceMessage subclass identity is lost, but metadata is preserved. Every
# consumer (compaction skip, post-compact re-projection, resume rebuild) keys off
# these, never off isinstance(msg, ResourceMessage).
# RESOURCE_ID: stable id of the loaded resource (e.g. the skill name).
# RESOURCE_KIND: resource category ("skill", ...), for future multi-kind support.
# RESOURCE_STICKY: True => re-project this body after compaction / never fold it.
RESOURCE_ID = "resource_id"
RESOURCE_KIND = "resource_kind"
RESOURCE_STICKY = "resource_sticky"

# Result-lifecycle retention (carried in Message.metadata on a tool-result message).
# This is the model-facing counterpart to the tool author's static ``reconstructable``
# ClassVar: a per-result hint about how compaction should treat *this specific*
# tool_result. The channel only carries the string; the compaction layer is the
# sole interpreter. Unknown / absent values fall back to default behavior.
#   RETENTION_ERASABLE — this result may be dropped early even if its tool is not
#     statically reconstructable (the model has read it and no longer needs it).
#   RETENTION_PIN       — never fold, never drop this result during compaction.
#   RETENTION_DEFAULT   — no hint; behave as today (drop iff reconstructable).
RETENTION = "retention"
RETENTION_ERASABLE = "erasable"
RETENTION_PIN = "pin"
RETENTION_DEFAULT = "default"

# Message id
IGNORED_MESSAGE_ID = "0"
