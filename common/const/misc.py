#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Miscellaneous constants (timeouts, tokens, formats, etc.)."""
import os

# REAL CONSTS
MEM_TTL = 24 * 30 * 3600

REQUIREMENT_FILENAME = "requirement.txt"
BUGFIX_FILENAME = "bugfix.txt"
PACKAGE_REQUIREMENTS_FILENAME = "requirements.txt"

YAPI_URL = "http://yapi.deepwisdomai.com/"
SD_URL = "http://172.31.0.51:49094"

DEFAULT_LANGUAGE = "English"
DEFAULT_MAX_TOKENS = 1500
COMMAND_TOKENS = 500
BRAIN_MEMORY = "BRAIN_MEMORY"
SKILL_PATH = "SKILL_PATH"
SERPER_API_KEY = "SERPER_API_KEY"
DEFAULT_TOKEN_SIZE = 500
DEFAULT_MAX_COMPLETION_TOKENS = 8192  # for image_getter, memory_compression, check_ui_rendering

# format
BASE64_FORMAT = "base64"

# REDIS
REDIS_KEY = "REDIS_KEY"

# Class Relationship
GENERALIZATION = "Generalize"
COMPOSITION = "Composite"
AGGREGATION = "Aggregate"

# Timeout
USE_CONFIG_TIMEOUT = 0  # Using llm.timeout configuration.
LLM_API_TIMEOUT = 300

# Assistant alias
ASSISTANT_ALIAS = "response"

# Markdown
MARKDOWN_TITLE_PREFIX = "## "

# Reporter
METAGPT_REPORTER_DEFAULT_URL = os.environ.get("METAGPT_REPORTER_URL", "")

# experience pool
EXPERIENCE_MASK = "<experience>"

# TeamLeader's name
TEAMLEADER_NAME = "Mike"
