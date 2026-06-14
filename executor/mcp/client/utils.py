import re


def format_method_name(method_name: str) -> str:
    """Converts internal method name format (e.g., 'Terminal_run') to a more readable format (e.g., 'Terminal.run')

    In internal code, method names use underscores to connect class and method names (since dots can't be used in actual function names),
    but when displaying error messages to users, the dot format is more intuitive and easier for LLMs to understand.

    Args:
        method_name: Internal method name, e.g., 'Terminal_run'

    Returns:
        Formatted method name, e.g., 'Terminal.run'
    """
    return re.sub("_", ".", method_name, count=1)
