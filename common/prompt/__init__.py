"""metagpt.common.prompt — all prompt text, organized by category.

Single home for every prompt/template/description string in the framework:

- role: core Role system prompt, identity templates, dynamic sections, MGX info,
  summary prompts, JSON-repair / ask-human helpers.
- memory: persistent file-based memory instructions + MEMORY.md context block.
- compaction: autocompact summarization prompt text (CC port).
- output: XML command-block output format + over-long-turn status nudge.
- agent: child-agent delegation task prompt, agent-tool section, example.
- tools: model-facing tool descriptions + tool-result prompt text.

Names are re-exported here so callers can do ``from metagpt.common.prompt
import SYSTEM_PROMPT`` or import the submodule directly.
"""
from metagpt.common.prompt.role import (
    CMD_EXPERIENCE_MASK,
    CMD_PROMPT,
    CONSTRAINT_TEMPLATE,
    FRC_SECTION,
    JSON_REPAIR_PROMPT,
    LANGUAGE_SECTION,
    PREFIX_TEMPLATE,
    ROLE_INSTRUCTION,
    SCRATCHPAD_SECTION,
    SUMMARIZE_PROBLEM_WHEN_DUPLICATE,
    SUMMARIZE_TOOL_RESULTS_SECTION,
    SUMMARY_PROMPT,
    SUMMARY_WITH_RECOMMEND_PROMPT,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
)
from metagpt.common.prompt.memory import (
    MEMORY_CONTEXT,
    MEMORY_EMPTY_STATE,
    MEMORY_FRONTMATTER_EXAMPLE,
    MEMORY_INSTRUCTIONS,
)
from metagpt.common.prompt.compaction import (
    NO_TOOLS_PREAMBLE,
    NO_TOOLS_TRAILER,
)
from metagpt.common.prompt.output import (
    OUTPUT_SECTION,
    SUMMARIZE_STATUS_WHEN_CONSECUTIVE,
)
from metagpt.common.prompt.agent import (
    AGENT_SECTION_TEMPLATE,
    AGENT_TASK_PROMPT,
    SUBAGENT_SECTION_TEMPLATE,
    SUBAGENT_TASK_PROMPT,
)

__all__ = [
    # role
    "CMD_EXPERIENCE_MASK",
    "CMD_PROMPT",
    "CONSTRAINT_TEMPLATE",
    "FRC_SECTION",
    "JSON_REPAIR_PROMPT",
    "LANGUAGE_SECTION",
    "PREFIX_TEMPLATE",
    "ROLE_INSTRUCTION",
    "SCRATCHPAD_SECTION",
    "SUMMARIZE_PROBLEM_WHEN_DUPLICATE",
    "SUMMARIZE_TOOL_RESULTS_SECTION",
    "SUMMARY_PROMPT",
    "SUMMARY_WITH_RECOMMEND_PROMPT",
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
