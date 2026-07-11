#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``python -m mote.cli`` — the §8 entry point (parse argv, select host, run app).

The §8 successor of ``mote.cli.__main__``: same argv surface, but instead of
``build_repl`` it assembles the decoupled stack via :func:`mote.cli.app.build_app`
and runs the returned :class:`SessionDriver`. The ``--consumer`` flag selects the
active consumer set (default ``terminal``); future hosts (``app-server`` over stdio,
``structured`` JSON-lines) plug in here without touching the spine (§8.1 phase ②/③).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.dont_write_bytecode = True

from mote.cli.app import build_app  # noqa: E402  # after dont_write_bytecode so no stray .pyc


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mote.cli",
        description=(
            "Interactive Mote session (Ctrl+C to interrupt a turn / exit at the prompt). "
            "Type /help inside the session for slash commands (agents, sessions, resume, fork)."
        ),
    )
    parser.add_argument("--model", default=None, help="Override the LLM model.")
    parser.add_argument(
        "--tools", default=None, help="Comma-separated tool names (default Read,Write,Edit,Bash,Glob,Grep)."
    )
    parser.add_argument("--cwd", default=None, help="Working directory for the agent.")
    parser.add_argument("--name", default="Assistant", help="Agent name.")
    parser.add_argument(
        "--consumer",
        action="append",
        default=None,
        help="Active consumer channel (repeatable). Default: terminal.",
    )
    parser.add_argument(
        "--ui",
        choices=["textual", "rich", "plain", "auto"],
        default="auto",
        help=(
            "Host UI. 'textual' = full-screen TUI; 'rich'/'plain' = the scrolling "
            "terminal host; 'auto' (default) picks textual on a TTY when available, "
            "else the terminal host."
        ),
    )
    return parser.parse_args(argv)


def _resolve_ui(choice: str) -> str:
    """Collapse ``auto`` to a concrete host based on TTY + availability."""
    if choice != "auto":
        return choice
    from mote.cli.consumers.textual import _HAS_TEXTUAL

    if sys.stdin.isatty() and sys.stdout.isatty() and _HAS_TEXTUAL:
        return "textual"
    return "rich"


def main(argv=None) -> None:
    args = _parse_args(argv)
    tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
    ui = _resolve_ui(args.ui)

    if ui == "textual":
        from mote.cli.consumers.textual.app import run_textual

        run_textual(model=args.model, tools=tools, cwd=args.cwd, name=args.name)
        print("\nGoodbye.")
        return

    driver = build_app(
        model=args.model,
        tools=tools,
        cwd=args.cwd,
        name=args.name,
        consumers=args.consumer,
    )
    try:
        asyncio.run(driver.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print("\nGoodbye.")


if __name__ == "__main__":
    main()
