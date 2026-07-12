#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Interactive REPL CLI for AgentFrame.

A top-level conversation loop: read a line from the user, run one ReAct turn
through the :class:`AgentControl` plane, print the reply, repeat. Two-stage
Ctrl+C (interrupt the in-flight turn vs. double-press-to-exit at the prompt)
mirrors codex / claude-code.
"""

from metagpt.cli.repl import Repl, build_repl, run_repl

__all__ = ["Repl", "build_repl", "run_repl"]
