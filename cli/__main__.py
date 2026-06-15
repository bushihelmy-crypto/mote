#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``python -m metagpt.cli`` — launch the interactive REPL."""

from __future__ import annotations

import argparse
import asyncio

from metagpt.cli.repl import build_repl


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="metagpt.cli",
        description=(
            "Interactive AgentFrame REPL (Ctrl+C to interrupt a turn / exit at the prompt). "
            "Type /help inside the REPL for slash commands (agents, sessions, resume, fork)."
        ),
    )
    parser.add_argument("--model", default=None, help="Override the LLM model.")
    parser.add_argument("--tools", default=None, help="Comma-separated tool names (default Read,Write,Edit,Bash,Glob,Grep).")
    parser.add_argument("--cwd", default=None, help="Working directory for the agent.")
    parser.add_argument("--name", default="Assistant", help="Agent name.")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
    repl = build_repl(model=args.model, tools=tools, cwd=args.cwd, name=args.name)
    try:
        asyncio.run(repl.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print("\nGoodbye.")


if __name__ == "__main__":
    main()
