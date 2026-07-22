#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.cli cron ...`` — imperative CRUD over the scheduled-task store.

The command-line face of the cron subsystem: ``add`` / ``list`` / ``rm`` operate
directly on the file-based :class:`CronTaskStore`
(``~/.mote/workspace/.agent_schedules/scheduled_tasks.json``) with no live
:class:`AgentControl` — task management is pure persistence, so the CLI reuses
the store and the shared :func:`validate_new_task` admission gate without
building an agent. A running session (started via ``python -m mote.cli``) then
picks the durable tasks up through its own :class:`CronService` and fires them.

Kept a standalone dispatcher (not folded into the flat ``__main__`` argparse) so
the interactive-session flag surface stays byte-for-byte unchanged: ``__main__``
routes a leading ``cron`` token here and this module owns its own subparser.
"""

from __future__ import annotations

import argparse
import time
from typing import List, Optional, Sequence

from mote.environment.scheduling.cron import cron_to_human
from mote.environment.scheduling.service import validate_new_task
from mote.environment.scheduling.store import CronTaskStore
from mote.environment.scheduling.task import CronTask


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mote.cli cron",
        description="Manage scheduled prompt-injection tasks (a running session fires them).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Add a scheduled task.")
    p_add.add_argument("cron", help="5-field cron expression, e.g. '0 9 * * 1-5'.")
    p_add.add_argument("prompt", help="Prompt injected into the running session when it fires.")
    p_add.add_argument(
        "--recurring",
        action="store_true",
        help="Fire on every match (default: one-shot, auto-deleted after firing).",
    )
    p_add.add_argument(
        "--session",
        default=None,
        help="Target session id (default: whichever live session runs the scheduler).",
    )

    sub.add_parser("list", help="List scheduled tasks.")

    p_rm = sub.add_parser("rm", help="Remove scheduled tasks by id.")
    p_rm.add_argument("ids", nargs="+", help="Task id(s) to remove.")

    return parser


def _cmd_add(store: CronTaskStore, args: argparse.Namespace) -> int:
    try:
        validate_new_task(args.cron, len(store.list()))
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    task = CronTask.new(
        args.cron,
        args.prompt,
        int(time.time() * 1000),
        recurring=args.recurring,
        durable=True,
        target_session_id=args.session,
    )
    store.add(task)
    kind = "recurring" if task.recurring else "one-shot"
    print(f"added {task.id}  [{kind}]  {task.cron}  ({cron_to_human(task.cron)})")
    return 0


def _cmd_list(store: CronTaskStore) -> int:
    tasks = store.list()
    if not tasks:
        print("no scheduled tasks")
        return 0
    for t in tasks:
        kind = "recurring" if t.recurring else "one-shot"
        target = t.target_session_id or "(active session)"
        preview = t.prompt if len(t.prompt) <= 60 else t.prompt[:57] + "..."
        print(f"{t.id}  {t.cron:<16}  {kind:<9}  -> {target}  {preview!r}")
    return 0


def _cmd_rm(store: CronTaskStore, ids: List[str]) -> int:
    removed = store.remove(ids)
    print(f"removed {removed} task(s)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Dispatch a ``cron`` subcommand; returns a process exit code."""
    args = _build_parser().parse_args(argv)
    store = CronTaskStore()
    if args.action == "add":
        return _cmd_add(store, args)
    if args.action == "list":
        return _cmd_list(store)
    if args.action == "rm":
        return _cmd_rm(store, args.ids)
    return 2  # unreachable: argparse enforces a valid action


__all__ = ["main"]
