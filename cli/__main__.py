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
    parser.add_argument("--tools", default=None, help="Comma-separated tool names (default Read,Edit,Search,Bash).")
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
    # Network server host (AG-UI SSE). When --serve is set the interactive UI is
    # bypassed and an aiohttp server is run instead (one turn per POST /run).
    parser.add_argument(
        "--serve",
        choices=["agui", "acp"],
        default=None,
        help=(
            "Run as a server instead of an interactive session. 'agui' = AG-UI SSE "
            "(network); 'acp' = Agent Client Protocol over stdio (editor host)."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Server bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8808, help="Server bind port (default 8808).")
    parser.add_argument("--token", default=None, help="Bearer auth token for the server (required unless --insecure).")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Run the server WITHOUT auth (explicit opt-out; do not expose publicly).",
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


def _run_server(args: argparse.Namespace, tools) -> None:
    """Build the shared engine once and serve it over the chosen network protocol."""
    from mote.cli.app import build_engine

    eng = build_engine(model=args.model, tools=tools, cwd=args.cwd, name=args.name)
    if args.serve == "agui":
        from mote.cli.consumers.agui.server import serve as serve_agui

        serve_agui(
            eng.role_factory,
            host=args.host,
            port=args.port,
            token=args.token,
            insecure=args.insecure,
            name=args.name,
        )
    elif args.serve == "acp":
        # ACP is stdio JSON-RPC — no network bind / token (the editor process that
        # launched us IS the trust boundary), so host/port/token are ignored.
        from mote.cli.consumers.acp.server import serve as serve_acp

        serve_acp(eng.role_factory, name=args.name)


def main(argv=None) -> None:
    raw = list(argv) if argv is not None else sys.argv[1:]
    # ``mote.cli cron ...`` — imperative scheduled-task CRUD. Routed before the
    # interactive parser so the session flag surface stays untouched.
    if raw and raw[0] == "cron":
        from mote.cli.cron_cli import main as cron_main

        sys.exit(cron_main(raw[1:]))

    args = _parse_args(raw)
    tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None

    if args.serve:
        _run_server(args, tools)
        return

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
