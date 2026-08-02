#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.product.cli cron ...`` — imperative CRUD over the scheduled-task store.

The command-line face of the cron subsystem: ``add`` / ``list`` / ``rm`` operate
directly on the file-based :class:`CronTaskStore`
(``~/.mote/workspace/.agent_schedules/scheduled_tasks.json``) with no live
:class:`AgentControl` — task management is pure persistence, so the CLI reuses
the store and the shared :func:`validate_new_task` admission gate without
building an agent. A running session (started via ``python -m mote.product.cli``) then
picks the durable tasks up through its own :class:`CronService` and fires them.

Kept a standalone dispatcher (not folded into the flat ``__main__`` argparse) so
the interactive-session flag surface stays byte-for-byte unchanged: ``__main__``
routes a leading ``cron`` token here and this module owns its own subparser.
"""

from __future__ import annotations

import argparse
from typing import List, Optional, Sequence

from mote.orchestration.automation.cron.expression import cron_to_human
from mote.orchestration.automation.cron.service import CronTaskCommands
from mote.orchestration.automation.cron.store import CronTaskStore
from mote.product.automation.timezone import system_timezone_name
from mote.product.paths import default_runtime_paths
from mote.runtime.clock import SystemClock


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mote.product.cli cron",
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


def _cmd_add(commands: CronTaskCommands, args: argparse.Namespace) -> int:
    try:
        task = commands.create(
            args.cron,
            args.prompt,
            args.session,
            recurring=args.recurring,
            durable=True,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    kind = "recurring" if task.recurring else "one-shot"
    print(f"added {task.id}  [{kind}]  {task.cron}  ({cron_to_human(task.cron)})")
    return 0


def _cmd_list(commands: CronTaskCommands) -> int:
    tasks = commands.list()
    if not tasks:
        print("no scheduled tasks")
        return 0
    for t in tasks:
        kind = "recurring" if t.recurring else "one-shot"
        target = t.target_session_id or "(active session)"
        preview = t.prompt if len(t.prompt) <= 60 else t.prompt[:57] + "..."
        print(f"{t.id}  {t.cron:<16}  {kind:<9}  -> {target}  {preview!r}")
    return 0


def _cmd_rm(commands: CronTaskCommands, ids: List[str]) -> int:
    removed = commands.remove(ids)
    print(f"removed {removed} task(s)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Dispatch a ``cron`` subcommand; returns a process exit code."""
    args = _build_parser().parse_args(argv)
    store = CronTaskStore(base_dir=str(default_runtime_paths().workspace_root / ".agent_schedules"))
    commands = CronTaskCommands(
        store,
        default_timezone_name=system_timezone_name(),
        clock_source=SystemClock(),
    )
    if args.action == "add":
        return _cmd_add(commands, args)
    if args.action == "list":
        return _cmd_list(commands)
    if args.action == "rm":
        return _cmd_rm(commands, args.ids)
    return 2  # unreachable: argparse enforces a valid action


__all__ = ["main"]
