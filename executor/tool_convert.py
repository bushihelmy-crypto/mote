import inspect

from mote.common.utils.parse_docstring import GoogleDocstringParser, remove_spaces

PARSER = GoogleDocstringParser


def function_docstring_to_schema(fn_obj, docstring="") -> dict:
    """
    Converts a function's docstring into a schema dictionary.

    Args:
        fn_obj: The function object.
        docstring: The docstring of the function.

    Returns:
        A dictionary representing the schema of the function's docstring.
        The dictionary contains the following keys:
        - 'type': The type of the function ('function' or 'async_function').
        - 'description': The first section of the docstring describing the function overall. Provided to LLMs for both recommending and using the function.
        - 'signature': The signature of the function, which helps LLMs understand how to call the function.
        - 'parameters': Docstring section describing parameters including args and returns, served as extra details for LLM perception.
    """
    signature = inspect.signature(fn_obj)

    docstring = remove_spaces(docstring)

    overall_desc, param_desc = PARSER.parse(docstring)

    function_type = "function" if not inspect.iscoroutinefunction(fn_obj) else "async_function"

    return {"type": function_type, "description": overall_desc, "signature": str(signature), "parameters": param_desc}
