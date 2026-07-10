#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``metagpt.cli`` local common layer.

Cross-cutting contracts shared across the display framework's hosts (terminal /
Web / IM / machine). Mirrors the framework's ``metagpt.common`` split: structural
``interface`` Protocols here are leaf modules importing only stdlib typing, so any
host can depend on them without risking an import cycle.
"""
