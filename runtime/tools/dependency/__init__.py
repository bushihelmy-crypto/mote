"""Shared dependencies for built-in tools.

Non-tool support modules used by the @register_tool BaseTool subclasses under
``product/toolsets/``: managed runtime engines and path adapters. These are intentionally NOT
registered tools, so they live outside the registry's package scan of ``tools/``.
"""
