"""Prompt text that is not a tool description.

Tool descriptions now live docstring-native on each tool's ``call()`` method
(first line = the tool-search menu summary, the body before ``Args:`` = the full
operating manual). This module keeps only the one prompt string that is NOT a
single tool's description: :data:`FILE_UNCHANGED_STUB` (a tool-RESULT string, the
recovery note returned in place of an unchanged file body).
"""

# --- Filesystem tools ------------------------------------------------------

# Returned in place of file contents when an already-read file is unchanged on
# disk — prompt text, not a real read result. Note: the referenced earlier
# result MAY have been cleared by context folding, so the wording must not
# promise it is still visible; it gives a recovery path when it is not.
FILE_UNCHANGED_STUB = (
    "File unchanged on disk since your last read. If that earlier Read result "
    "is still visible above, use it. If it has been cleared from context and "
    "you can no longer see the content, do NOT fall back to shell cat — re-read "
    "with an explicit offset/limit slice (any range you have not requested at "
    "this exact same offset+limit before) to force fresh content."
)
