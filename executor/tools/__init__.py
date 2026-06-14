"""Built-in tools package.

Each module defines one or more @register_tool BaseTool subclasses, discovered
automatically by ToolRegistry.discover() (package scan). This __init__ makes the
directory a regular package so the scan descends into it.
"""
