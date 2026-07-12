"""Executor package.

Tools register themselves via @register_tool and are discovered automatically
by ToolRegistry.discover() (package scan) — no manual import list to maintain.
Import concrete classes from their own submodules when needed.
"""
