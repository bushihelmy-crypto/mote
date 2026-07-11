#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``proto`` — the machine protocol (窄腰之二), a documented stub for phase ②.

This is the **twin** of ``view/``: where ``view/`` folds the single ``AgentEvent``
truth source into ``ViewEvent``\\s for human eyes, ``proto/`` will fold the *same*
stream into ``ServerNotification``\\s for machines (codex app-server / Symphony-
compatible). The two protocols are deliberately the only two (§1.1) — humans and
machines diverge enough to warrant distinct contracts, but no more than two.

Phase ① (the terminal vertical slice) ships this as an **empty, documented stub**.
The shape is fixed by ARCHITECTURE §6; filling it is pure addition that touches
*none* of §1–§5:

* ``proto/projector.py`` — ``AppServerProjector``: the ``ViewProjector`` twin. Same
  ``ObservationSubscriber`` registration on the role ``EventBus``, same fold-once
  discipline; only the target contract differs. Mapping table in §6.3
  (``SessionStartEvent`` → ``thread/started``, ``LLMStreamDeltaEvent`` →
  ``item/agentMessage/delta``, ``PostToolUseEvent`` → ``item/completed``, …).
* ``proto/schema.py`` — field-for-field alignment with the codex app-server schema
  (``codex-rs/app-server-protocol/schema/json/...``). The one piece of irreducible
  hand-checking (§6.5).
* ``proto/rpc.py`` — newline-delimited JSON ``StdioTransport`` + method routing +
  the thread/turn correlation state machine. Inbound mapping in §6.2.

Approval semantics (§6.4): a high-trust auto-approve permission profile answers
``requestApproval`` reverse-requests, and ``AskUserQuestion`` inside an orchestrated
turn becomes a hard ``turn_input_required`` → ``turn_failed`` rather than blocking
forever — a *consumer-level* policy, not a core change.

The eventual entry is ``python -m mote.cli app-server`` (stdio, no TUI).
"""

from __future__ import annotations

__all__: list = []
