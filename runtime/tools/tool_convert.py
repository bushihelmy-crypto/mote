import inspect
from collections.abc import Callable
from typing import Any

from mote.runtime.tools.docstring_parser import GoogleDocstringParser, remove_spaces

PARSER = GoogleDocstringParser


def function_docstring_to_schema(fn_obj: Callable[..., Any], docstring: str = "") -> dict[str, Any]:
    """Convert a function signature and argument docs into an XML schema.

    Args:
        fn_obj: The function object.
        docstring: The docstring of the function.

    Returns:
        A dictionary representing the schema of the function's docstring.
        The model-facing call signature and ``Args:`` documentation. The
        overall description is intentionally omitted because the enclosing XML
        tool schema already carries it.
    """
    docstring = remove_spaces(docstring)
    _, param_desc = PARSER.parse(docstring)
    return {"signature": str(inspect.signature(fn_obj)), "parameters": param_desc}
