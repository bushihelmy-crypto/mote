#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal cron expression parsing, next-run calculation, and jitter (port of cron.ts).

Supports the standard 5-field cron subset::

    minute hour day-of-month month day-of-week

Field syntax: wildcard (``*``), step (``*/n``), range (``a-b``, ``a-b/n``), list
(``a,b,c``), and plain ``n``. No ``L``, ``W``, ``?``, or name aliases. All times
are interpreted in the process's local timezone — ``0 9 * * *`` means 9am wherever
the agent is running.

The jitter helpers add a deterministic per-task delay/lead so a herd of identically
scheduled tasks across many sessions does not all hit inference on the same
wall-clock mark.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from mote.orchestration.automation.cron.task import DEFAULT_CRON_JITTER_CONFIG, CronDstPolicy, CronJitterConfig


@dataclass
class CronFields:
    """A parsed cron expression: each field expanded to its matching values."""

    minute: List[int]
    hour: List[int]
    day_of_month: List[int]
    month: List[int]
    day_of_week: List[int]


#: (min, max) inclusive range per field, in cron order.
_FIELD_RANGES = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day_of_month
    (1, 12),  # month
    (0, 6),  # day_of_week (0=Sunday; 7 accepted as Sunday alias)
]


def _expand_field(field: str, rng: tuple[int, int]) -> Optional[List[int]]:
    """Expand a single cron field to a sorted list of values, or ``None`` if invalid."""
    lo_bound, hi_bound = rng
    is_dow = lo_bound == 0 and hi_bound == 6
    out: set[int] = set()

    for part in field.split(","):
        # wildcard or */n
        if part == "*" or part.startswith("*/"):
            if part == "*":
                step = 1
            else:
                step_str = part[2:]
                if not step_str.isdigit():
                    return None
                step = int(step_str)
            if step < 1:
                return None
            for i in range(lo_bound, hi_bound + 1, step):
                out.add(i)
            continue

        # a-b or a-b/n
        if "-" in part:
            range_part, _, step_part = part.partition("/")
            lo_str, _, hi_str = range_part.partition("-")
            if not (lo_str.isdigit() and hi_str.isdigit()):
                return None
            lo = int(lo_str)
            hi = int(hi_str)
            if step_part:
                if not step_part.isdigit():
                    return None
                step = int(step_part)
            else:
                step = 1
            # dayOfWeek: accept 7 as Sunday alias in ranges (e.g. 5-7 -> [5,6,0]).
            eff_max = 7 if is_dow else hi_bound
            if lo > hi or step < 1 or lo < lo_bound or hi > eff_max:
                return None
            for i in range(lo, hi + 1, step):
                out.add(0 if (is_dow and i == 7) else i)
            continue

        # plain n
        if part.isdigit():
            n = int(part)
            if is_dow and n == 7:
                n = 0
            if n < lo_bound or n > hi_bound:
                return None
            out.add(n)
            continue

        return None

    if not out:
        return None
    return sorted(out)


def parse_cron_expression(expr: str) -> Optional[CronFields]:
    """Parse a 5-field cron expression into expanded value lists.

    Returns ``None`` if the expression is invalid or uses unsupported syntax.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None

    expanded: List[List[int]] = []
    for i in range(5):
        result = _expand_field(parts[i], _FIELD_RANGES[i])
        if result is None:
            return None
        expanded.append(result)

    return CronFields(
        minute=expanded[0],
        hour=expanded[1],
        day_of_month=expanded[2],
        month=expanded[3],
        day_of_week=expanded[4],
    )


def compute_next_cron_run(
    fields: CronFields,
    from_ms: int,
    *,
    timezone_name: str | None = None,
    dst_policy: CronDstPolicy = CronDstPolicy.EARLIEST_FOLD_SKIP_GAP,
) -> Optional[int]:
    """Compute the next fire time (epoch ms) strictly after ``from_ms``.

    Walks forward minute-by-minute in local time, bounded at 366 days. When both
    day-of-month and day-of-week are constrained, a date matches if *either*
    matches (standard vixie-cron OR semantics). Returns ``None`` if no match in
    the window (impossible for valid cron, but satisfies the type).
    """
    minute_set = set(fields.minute)
    hour_set = set(fields.hour)
    dom_set = set(fields.day_of_month)
    month_set = set(fields.month)
    dow_set = set(fields.day_of_week)

    dom_wild = len(fields.day_of_month) == 31
    dow_wild = len(fields.day_of_week) == 7

    if dst_policy is not CronDstPolicy.EARLIEST_FOLD_SKIP_GAP:
        raise ValueError("unsupported cron DST policy")
    zone = datetime.now().astimezone().tzinfo if timezone_name is None else ZoneInfo(timezone_name)
    if zone is None:
        raise ValueError("local timezone is unavailable")
    # Walk absolute UTC minutes, then project each candidate into the declared
    # timezone. Gaps have no candidate; fold=1 is skipped by the fixed policy.
    t = datetime.fromtimestamp(from_ms / 1000.0, timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)

    max_iter = 366 * 24 * 60
    for _ in range(max_iter):
        local = t.astimezone(zone)
        if local.fold == 1:
            t += timedelta(minutes=1)
            continue
        if local.month not in month_set:
            t += timedelta(minutes=1)
            continue

        dom = local.day
        # Python weekday(): Mon=0..Sun=6; cron dow: Sun=0..Sat=6.
        dow = (local.weekday() + 1) % 7
        if dom_wild and dow_wild:
            day_matches = True
        elif dom_wild:
            day_matches = dow in dow_set
        elif dow_wild:
            day_matches = dom in dom_set
        else:
            day_matches = dom in dom_set or dow in dow_set

        if not day_matches:
            t += timedelta(minutes=1)
            continue

        if local.hour not in hour_set:
            t += timedelta(minutes=1)
            continue

        if local.minute not in minute_set:
            t += timedelta(minutes=1)
            continue

        return int(t.timestamp() * 1000)

    return None


def _next_cron_run_ms(
    cron: str,
    from_ms: int,
    *,
    timezone_name: str | None = None,
    dst_policy: CronDstPolicy = CronDstPolicy.EARLIEST_FOLD_SKIP_GAP,
) -> Optional[int]:
    """Parse + compute next fire (epoch ms) for a cron string, or ``None``."""
    fields = parse_cron_expression(cron)
    if fields is None:
        return None
    return compute_next_cron_run(
        fields,
        from_ms,
        timezone_name=timezone_name,
        dst_policy=dst_policy,
    )


# --- jitter -----------------------------------------------------------------


def _jitter_frac(task_id: str) -> float:
    """Map a task id to a stable fraction in ``[0, 1)``.

    8-hex-char ids (see :meth:`task.CronTask.new`) parse directly as a u32.
    Hand-edited / non-hex ids fall back to a stable hash so jitter stays
    deterministic across restarts (Python's ``hash`` is salted per-process).
    """
    head = task_id[:8]
    try:
        value = int(head, 16)
    except (ValueError, TypeError):
        value = int(hashlib.md5(task_id.encode("utf-8")).hexdigest()[:8], 16)
    frac = value / 0x1_0000_0000
    return frac if math.isfinite(frac) else 0.0


def jittered_next_cron_run_ms(
    cron: str,
    from_ms: int,
    task_id: str,
    cfg: CronJitterConfig = DEFAULT_CRON_JITTER_CONFIG,
    *,
    timezone_name: str | None = None,
    dst_policy: CronDstPolicy = CronDstPolicy.EARLIEST_FOLD_SKIP_GAP,
) -> Optional[int]:
    """Next recurring fire (epoch ms) plus a deterministic forward delay.

    The delay is proportional to the gap between consecutive fires
    (``recurring_frac``, capped at ``recurring_cap_ms``) so an hourly task spreads
    across several minutes while a per-minute task spreads by only seconds.
    """
    t1 = _next_cron_run_ms(cron, from_ms, timezone_name=timezone_name, dst_policy=dst_policy)
    if t1 is None:
        return None
    t2 = _next_cron_run_ms(cron, t1, timezone_name=timezone_name, dst_policy=dst_policy)
    if t2 is None:
        # No second match (e.g. pinned date) → nothing to proportion against.
        return t1
    jitter = min(_jitter_frac(task_id) * cfg.recurring_frac * (t2 - t1), cfg.recurring_cap_ms)
    return int(t1 + jitter)


def one_shot_jittered_next_cron_run_ms(
    cron: str,
    from_ms: int,
    task_id: str,
    cfg: CronJitterConfig = DEFAULT_CRON_JITTER_CONFIG,
    *,
    timezone_name: str | None = None,
    dst_policy: CronDstPolicy = CronDstPolicy.EARLIEST_FOLD_SKIP_GAP,
) -> Optional[int]:
    """Next one-shot fire (epoch ms) minus a deterministic lead on round minutes.

    Firing a user-pinned one-shot slightly early is invisible but spreads the load
    spike from everyone picking the same round wall-clock time. Only fire times
    landing on minutes matching ``one_shot_minute_mod`` get jitter. Clamped to
    ``from_ms`` so a task created inside its own window never fires before creation.
    """
    t1 = _next_cron_run_ms(cron, from_ms, timezone_name=timezone_name, dst_policy=dst_policy)
    if t1 is None:
        return None
    # Cron resolution is 1 minute → computed times always have :00 seconds.
    zone = datetime.now().astimezone().tzinfo if timezone_name is None else ZoneInfo(timezone_name)
    if zone is None:
        raise ValueError("local timezone is unavailable")
    minute = datetime.fromtimestamp(t1 / 1000.0, zone).minute
    if minute % cfg.one_shot_minute_mod != 0:
        return t1
    lead = cfg.one_shot_floor_ms + _jitter_frac(task_id) * (cfg.one_shot_max_ms - cfg.one_shot_floor_ms)
    return int(max(t1 - lead, from_ms))


# --- cron_to_human ----------------------------------------------------------

_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _format_local_time(minute: int, hour: int) -> str:
    # Use a fixed non-DST date; %I/%p with leading-zero strip for "9:05 AM".
    d = datetime(2000, 1, 1, hour, minute)
    return d.strftime("%I:%M %p").lstrip("0")


def cron_to_human(cron: str) -> str:
    """Best-effort human translation of common patterns; falls back to the raw string."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, day_of_month, month, day_of_week = parts

    wild_rest = day_of_month == "*" and month == "*" and day_of_week == "*"

    # Every N minutes: */N * * * *
    if minute.startswith("*/") and minute[2:].isdigit() and hour == "*" and wild_rest:
        n = int(minute[2:])
        return "Every minute" if n == 1 else f"Every {n} minutes"

    # Every hour (at :MM): M * * * *
    if minute.isdigit() and hour == "*" and wild_rest:
        m = int(minute)
        return "Every hour" if m == 0 else f"Every hour at :{m:02d}"

    # Every N hours: M */N * * *
    if minute.isdigit() and hour.startswith("*/") and hour[2:].isdigit() and wild_rest:
        n = int(hour[2:])
        m = int(minute)
        suffix = "" if m == 0 else f" at :{m:02d}"
        return f"Every hour{suffix}" if n == 1 else f"Every {n} hours{suffix}"

    # Remaining cases reference a concrete hour+minute.
    if not (minute.isdigit() and hour.isdigit()):
        return cron
    m = int(minute)
    h = int(hour)

    # Daily at a specific time: M H * * *
    if day_of_month == "*" and month == "*" and day_of_week == "*":
        return f"Every day at {_format_local_time(m, h)}"

    # Specific day of week: M H * * D
    if day_of_month == "*" and month == "*" and len(day_of_week) == 1 and day_of_week.isdigit():
        day_index = int(day_of_week) % 7
        return f"Every {_DAY_NAMES[day_index]} at {_format_local_time(m, h)}"

    # Weekdays: M H * * 1-5
    if day_of_month == "*" and month == "*" and day_of_week == "1-5":
        return f"Weekdays at {_format_local_time(m, h)}"

    return cron


__all__ = [
    "CronFields",
    "parse_cron_expression",
    "compute_next_cron_run",
    "cron_to_human",
    "jittered_next_cron_run_ms",
    "one_shot_jittered_next_cron_run_ms",
]
