#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for cron parsing, next-run computation, jitter, and human translation."""

from datetime import datetime

from metagpt.environment.scheduling.cron import (
    compute_next_cron_run,
    cron_to_human,
    jittered_next_cron_run_ms,
    one_shot_jittered_next_cron_run_ms,
    parse_cron_expression,
)
from metagpt.environment.scheduling.task import CronJitterConfig


def _ms(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


# --- parsing ---------------------------------------------------------------


def test_parse_wildcard():
    fields = parse_cron_expression("* * * * *")
    assert fields is not None
    assert fields.minute == list(range(60))
    assert fields.hour == list(range(24))
    assert fields.day_of_week == list(range(7))


def test_parse_step():
    fields = parse_cron_expression("*/5 * * * *")
    assert fields.minute == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]


def test_parse_range():
    fields = parse_cron_expression("0 1-5 * * *")
    assert fields.hour == [1, 2, 3, 4, 5]


def test_parse_list():
    fields = parse_cron_expression("0,30 * * * *")
    assert fields.minute == [0, 30]


def test_parse_range_with_step():
    fields = parse_cron_expression("0-30/10 * * * *")
    assert fields.minute == [0, 10, 20, 30]


def test_parse_dow_sunday_alias():
    fields = parse_cron_expression("0 0 * * 7")
    assert fields.day_of_week == [0]


def test_parse_invalid_field_count():
    assert parse_cron_expression("* * * *") is None
    assert parse_cron_expression("* * * * * *") is None


def test_parse_invalid_out_of_range():
    assert parse_cron_expression("60 * * * *") is None
    assert parse_cron_expression("0 24 * * *") is None


def test_parse_unsupported_syntax():
    assert parse_cron_expression("L * * * *") is None
    assert parse_cron_expression("? * * * *") is None
    assert parse_cron_expression("MON * * * *") is None


def test_parse_bad_range():
    assert parse_cron_expression("5-1 * * * *") is None


# --- compute_next_cron_run -------------------------------------------------


def test_next_run_minute_step():
    fields = parse_cron_expression("*/5 * * * *")
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 15, 10, 2))
    assert nxt == _ms(2026, 6, 15, 10, 5)


def test_next_run_hourly():
    fields = parse_cron_expression("0 * * * *")
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 15, 10, 30))
    assert nxt == _ms(2026, 6, 15, 11, 0)


def test_next_run_daily():
    fields = parse_cron_expression("0 9 * * *")
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 15, 10, 0))
    assert nxt == _ms(2026, 6, 16, 9, 0)


def test_next_run_strictly_after():
    # When `from` is exactly on a fire boundary, return the NEXT one.
    fields = parse_cron_expression("0 9 * * *")
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 15, 9, 0))
    assert nxt == _ms(2026, 6, 16, 9, 0)


def test_next_run_dom_dow_or_semantics():
    # dom=1 OR dow=Monday(1). From mid-June 2026: next is whichever comes first.
    fields = parse_cron_expression("0 0 1 * 1")
    # 2026-06-15 is a Monday; next Monday is 2026-06-22, next 1st is 2026-07-01.
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 16, 0, 0))
    assert nxt == _ms(2026, 6, 22, 0, 0)


def test_next_run_specific_month():
    fields = parse_cron_expression("0 0 1 1 *")
    nxt = compute_next_cron_run(fields, _ms(2026, 6, 15))
    assert nxt == _ms(2027, 1, 1, 0, 0)


# --- jitter ----------------------------------------------------------------


def test_jitter_deterministic():
    cron = "0 * * * *"
    frm = _ms(2026, 6, 15, 10, 30)
    a = jittered_next_cron_run_ms(cron, frm, "abc12345")
    b = jittered_next_cron_run_ms(cron, frm, "abc12345")
    assert a == b


def test_jitter_within_cap_and_forward():
    cron = "0 * * * *"  # hourly, gap = 3_600_000 ms
    frm = _ms(2026, 6, 15, 10, 30)
    base = _ms(2026, 6, 15, 11, 0)
    cfg = CronJitterConfig()
    val = jittered_next_cron_run_ms(cron, frm, "ffffffff", cfg)
    # Forward only, capped at min(frac*0.1*gap, cap). frac~1 → ~360s.
    assert base <= val <= base + cfg.recurring_cap_ms
    assert val - base <= int(cfg.recurring_frac * 3_600_000) + 1


def test_jitter_zero_for_zero_hash():
    cron = "0 * * * *"
    frm = _ms(2026, 6, 15, 10, 30)
    base = _ms(2026, 6, 15, 11, 0)
    # taskId hashing to 0 → no forward delay.
    assert jittered_next_cron_run_ms(cron, frm, "00000000") == base


def test_one_shot_jitter_backward_on_round_minute():
    cron = "0 12 * * *"  # fires on :00 → eligible for backward jitter
    frm = _ms(2026, 6, 15, 10, 0)
    base = _ms(2026, 6, 15, 12, 0)
    cfg = CronJitterConfig()
    val = one_shot_jittered_next_cron_run_ms(cron, frm, "ffffffff", cfg)
    assert base - cfg.one_shot_max_ms <= val <= base


def test_one_shot_no_jitter_off_round_minute():
    cron = "7 12 * * *"  # :07 → not a multiple of 30, no jitter
    frm = _ms(2026, 6, 15, 10, 0)
    base = _ms(2026, 6, 15, 12, 7)
    assert one_shot_jittered_next_cron_run_ms(cron, frm, "ffffffff") == base


# --- cron_to_human ---------------------------------------------------------


def test_human_every_n_minutes():
    assert cron_to_human("*/5 * * * *") == "Every 5 minutes"
    assert cron_to_human("*/1 * * * *") == "Every minute"


def test_human_every_hour():
    assert cron_to_human("0 * * * *") == "Every hour"
    assert cron_to_human("15 * * * *") == "Every hour at :15"


def test_human_daily():
    assert cron_to_human("0 9 * * *") == "Every day at 9:00 AM"


def test_human_weekdays():
    assert cron_to_human("0 9 * * 1-5") == "Weekdays at 9:00 AM"


def test_human_fallback():
    assert cron_to_human("0 0 1,15 * *") == "0 0 1,15 * *"
    assert cron_to_human("not a cron") == "not a cron"
