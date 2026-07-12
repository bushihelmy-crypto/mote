"""mote.common.prompt — all prompt text, organized by category.

Single home for every prompt/template/description string in the framework:

- role: core Role system prompt, dynamic sections, JSON-repair / ask-human helpers.
- memory: persistent file-based memory instructions + MEMORY.md context block.
- compaction: autocompact summarization prompt text.
- output: XML command-block output format + over-long-turn status nudge.
- agent: child-agent delegation task prompt, agent-tool section, example.
- tools: model-facing tool descriptions + tool-result prompt text.

Names are re-exported here so callers can do ``from mote.common.prompt
import SYSTEM_PROMPT`` or import the submodule directly.
"""
from mote.common.prompt.agent import (
    AGENT_SECTION_TEMPLATE,
    AGENT_TASK_PROMPT,
    SUBAGENT_SECTION_TEMPLATE,
    SUBAGENT_TASK_PROMPT,
)
from mote.common.prompt.compaction import NO_TOOLS_PREAMBLE, NO_TOOLS_TRAILER
from mote.common.prompt.memory import (
    MEMORY_CONTEXT,
    MEMORY_EMPTY_STATE,
    MEMORY_FRONTMATTER_EXAMPLE,
    MEMORY_INSTRUCTIONS,
)
from mote.common.prompt.output import OUTPUT_SECTION, SUMMARIZE_STATUS_WHEN_CONSECUTIVE
from mote.common.prompt.role import (
    CMD_EXPERIENCE_MASK,
    CMD_PROMPT,
    FRC_SECTION,
    JSON_REPAIR_PROMPT,
    LANGUAGE_SECTION,
    ROLE_INSTRUCTION,
    SCRATCHPAD_SECTION,
    SUMMARIZE_PROBLEM_WHEN_DUPLICATE,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    TASK_FINAL_OUTPUT_SECTION,
)

__all__ = [
    # role
    "CMD_EXPERIENCE_MASK",
    "CMD_PROMPT",
    "FRC_SECTION",
    "JSON_REPAIR_PROMPT",
    "LANGUAGE_SECTION",
    "ROLE_INSTRUCTION",
    "SCRATCHPAD_SECTION",
    "SUMMARIZE_PROBLEM_WHEN_DUPLICATE",
    "TASK_FINAL_OUTPUT_SECTION",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_DYNAMIC_BOUNDARY",
    # memory
    "MEMORY_CONTEXT",
    "MEMORY_EMPTY_STATE",
    "MEMORY_FRONTMATTER_EXAMPLE",
    "MEMORY_INSTRUCTIONS",
    # compaction
    "NO_TOOLS_PREAMBLE",
    "NO_TOOLS_TRAILER",
    # output
    "OUTPUT_SECTION",
    "SUMMARIZE_STATUS_WHEN_CONSECUTIVE",
    # agent
    "AGENT_SECTION_TEMPLATE",
    "AGENT_TASK_PROMPT",
    "SUBAGENT_SECTION_TEMPLATE",
    "SUBAGENT_TASK_PROMPT",
]
