#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Headless host: a JSON-lines consumer of the human ``ViewEvent`` protocol."""

from mote.cli.consumers.structured.consumer import StructuredConsumer, build_structured_consumer

__all__ = ["StructuredConsumer", "build_structured_consumer"]
