"""Canonical model-context projection for a user-interrupted turn."""

TURN_ABORTED_FRAGMENT = (
    "<turn_aborted>\n"
    "The user interrupted the previous turn on purpose. Any running unified exec processes "
    "may still be running in the background. If any tools/commands were aborted, they may "
    "have partially executed.\n"
    "</turn_aborted>"
)

__all__ = ["TURN_ABORTED_FRAGMENT"]
