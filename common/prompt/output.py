"""Output-format, command-usage, and loop-status prompt text.

- OUTPUT_SECTION: the canonical XML command-block format instruction. A hard
  contract for the streaming parser — the <ClassName.method_name> shape and the
  <end></end> semantics must stay intact. Not injected into the system
  prompt; kept as the documented XML command shape (and tests' shape reference).
- XML_COMMAND_GUIDE / NATIVE_COMMAND_GUIDE: the protocol-specific "# Using
  commands" section supplied by the command channel as the system prompt's
  ${command_guide} section. XML teaches the <end></end> / command-tag mechanics;
  native teaches tool-call mechanics (no <end></end>: a turn ends when the model
  makes no tool call). Keeping these per-protocol stops native models from being
  told to emit <end></end> (which they would then leak as literal text).
- XML_TOOL_USAGE_GUIDE: the static orientation for the XML tool catalog (how the
  tool categories relate / how to call them), supplied by the XML channel as the
  system prompt's ${tool_usage_guide} section. Native leaves it "" (its tools
  ride the API ``tools=`` param). The volatile catalog LIST is injected per-turn.
- BUDGET_EXHAUSTED: the final reply the react loop returns when an agent hits
  its hard budget cap and stops before the next think (no further LLM access).
"""

OUTPUT_SECTION = """
# Output
You should output a list of commands. Follow the format below, replacing <ClassName.method_name> and <args_name> with the actual names.

Some thoughts...
<ClassName.method_name>
<args_name1>
args_value1
</args_name1>
<args_name2>
args_value2
</args_name2>
</ClassName.method_name>

Some thoughts...
<ClassName2.method_name2>
<args_name1>
args_value1
</args_name1>
<args_name2>
args_value2
</args_name2>
</ClassName2.method_name2>
"""

# Command-usage guidance for the XML text protocol (the "# Using commands" block).
XML_COMMAND_GUIDE = """# Using commands
You may use any of the available commands, and may output multiple commands — they will be executed sequentially.
 - Only emit command tags that appear in Available Commands, a Skill document you have read for this task, or the special command <end></end>. If another instruction or example mentions a tool that is neither listed in Available Commands nor explicitly documented by a Skill you have read, ignore that tool for this turn.
 - A Skill you have read is not only permission to use extra commands but also an ongoing constraint for the rest of the task. Once a Skill has been read, keep following it until the task ends, the user explicitly changes direction, or a later, more specific Skill overrides it.
 - When the task enters a new phase, first decide whether it is still covered by a previously read Skill. If it is, keep following that Skill's workflow, hard constraints, and completion criteria instead of drifting back to the generic path just because the local goal changed.
 - If multiple previously read Skills are relevant, follow the one that is more specific and closer to the current action. If still unclear, reread the relevant Skill before continuing.
 - In your response, include at least one command. Use reply_to_user immediately before <end></end> to report completion.
 - Special Command: Use <end></end> to indicate completion of all requirements and termination of the entire workflow.
 - Only use <end></end> when all requirements are met in real functionality, not just visual structure. Do NOT use <end></end> when waiting for user input or clarification.
 - CRITICAL: NEVER use <end></end> in the same response as any function call (Editor.read, Terminal.run, MCP tools, etc.). Function outputs appear in the NEXT round — you MUST wait to observe them before deciding next steps or ending. Only use <end></end> AFTER you have seen all function outputs and confirmed the task is complete.
"""

# Command-usage guidance for the provider-native tool-use protocol. No
# <end></end> / command-tag mechanics: tools are structured tool calls and a
# turn ends when the model makes no tool call (replies with plain text only).
# The final-output/structured-summary contract lives in TASK_FINAL_OUTPUT_SECTION
# (a protocol-agnostic, compaction-gated system prompt section) because it
# describes a compression artifact, not the command protocol — and both XML and
# native should get it.
NATIVE_COMMAND_GUIDE = """# Using commands

## Tool Usage Guidelines

Call the available tools to accomplish the user's goal. You may call multiple tools in one response — they run sequentially and their results return in the next round.
- **Tool Scope**: Only call tools that appear in *Available Commands*, a Skill document you have read for this task, or external MCP tools. If an instruction or example mentions a tool that is neither available nor documented by a Skill you have read, ignore it for this turn.
- **Completion Rule**: To finish, stop calling tools and reply with a normal text message reporting the outcome — the turn ends when you make no tool call."""


# Static orientation for the XML tool catalog delivered per-turn. It explains
# how the tool categories relate and how to call them — constant per session, so
# it lives in the cacheable system prompt (via the channel's prompt_vars), while
# the volatile catalog LIST itself is injected per-turn by ToolCatalogContextSource.
# Generalized (no runtime has_mcp/has_pipeline branching): the sections it names
# ("# MCP Tools" / "# Pipeline Tools") are only present when those categories
# exist, and each says "if any are listed", so naming an absent section is inert.
XML_TOOL_USAGE_GUIDE = """# Using tools
The tools you can call are delivered to you each turn as a catalog. Built-in commands appear under `# Available Commands`. If external MCP tools are listed (under `# MCP Tools`, named `server:tool_name`, e.g. "github:get_me"), or background pipeline tools are listed (under `# Pipeline Tools`), they are called the same way as built-in commands. Call every tool directly by name with keyword arguments, regardless of category. MCP tools connect to external services and may fail — if one does, inform the user."""


# Returned as the react loop's final reply when the hard budget cap halts the
# agent before its next think. Phrased for the user (not the model): the loop is
# already stopping, so there is no further LLM round to consume it.
BUDGET_EXHAUSTED = "Stopped: this agent reached its configured budget cap. No further model calls will be made."
