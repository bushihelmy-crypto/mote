#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Background-task constants — output caps, stall detection, attachment limits.

Centralized from mote/tasks/ sub-modules.
"""

# ---------------------------------------------------------------------------
# Disk output (disk_output.py)
# ---------------------------------------------------------------------------
MAX_TASK_OUTPUT_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GB
MAX_TASK_OUTPUT_BYTES_DISPLAY: str = "5GB"
DEFAULT_MAX_READ_BYTES: int = 8 * 1024 * 1024  # 8 MB

# ---------------------------------------------------------------------------
# Task pool (pool.py)
# ---------------------------------------------------------------------------
MAX_RESULT_LEN = 2000
DEFAULT_TASK_TIMEOUT = 1800.0  # 30 minutes per task (per-task execution bound)
DEFAULT_MAX_CONCURRENCY = 10
# Safety bound for wait_for_completion so a bare call on an idle/empty pool
# returns instead of blocking forever. Distinct from DEFAULT_TASK_TIMEOUT (a
# task's execution limit) — this caps how long a *waiter* blocks.
DEFAULT_WAIT_COMPLETION_TIMEOUT = 600.0  # 10 minutes

# ---------------------------------------------------------------------------
# Stall detector (stall_detector.py)
# ---------------------------------------------------------------------------
STALL_CHECK_INTERVAL = 5.0  # seconds between output-size checks
STALL_THRESHOLD = 45.0  # seconds without output growth -> suspected stall
STALL_TAIL_BYTES = 1024  # bytes to read for pattern matching

# ---------------------------------------------------------------------------
# Attachment generator (attachment.py)
# ---------------------------------------------------------------------------
DELTA_MAX_BYTES = 32768  # max bytes per incremental read
DELTA_SUMMARY_MAX_CHARS = 8000  # max chars kept in delta_summary
