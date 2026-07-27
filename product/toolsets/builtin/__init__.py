"""Built-in tools package.

Each module defines one or more @register_tool BaseTool subclasses, discovered
automatically by ``product.toolsets.discover_builtin_tools()``. This __init__ makes the
directory a regular package so the scan descends into it.
"""
