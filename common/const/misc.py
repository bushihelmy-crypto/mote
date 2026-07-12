#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Miscellaneous constants (timeouts, formats, etc.)."""
import os

# Timeout
USE_CONFIG_TIMEOUT = 0  # Using llm.timeout configuration.
LLM_API_TIMEOUT = 300

# Markdown
MARKDOWN_TITLE_PREFIX = "## "

# Reporter
MOTE_REPORTER_DEFAULT_URL = os.environ.get("MOTE_REPORTER_URL", "")

# experience pool
EXPERIENCE_MASK = "<experience>"
