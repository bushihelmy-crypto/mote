"""Read-only git working-tree state for the environment section.

A best-effort, never-raising snapshot of the current repo (branch, dirty/clean
status with change counts, and recent commits), plus a renderer that turns it
into the ``# Environment`` git block. Injected per-turn below the system-prompt
cache boundary so the changing state never busts the cacheable prefix.
"""
from mote.common.utils.git_state.collector import GitState, collect_git_state, find_git_root
from mote.common.utils.git_state.render import render_git_section

__all__ = ["GitState", "collect_git_state", "find_git_root", "render_git_section"]
