"""Output-format and command-protocol prompt text.

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
  system prompt's ${tool_usage_guide} section. XML built-ins live in the system
  prompt; hot MCP/pipeline definitions are injected per-turn. Native leaves it
  "" because callable definitions ride the API ``tools=`` param.
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
You may output multiple commands; they execute sequentially. Include at least one command per response.
 - Only emit command tags from Available Commands, a Skill you have read for this task, or the special <end></end>. Ignore any tool mentioned elsewhere that is neither listed nor documented by a Skill you've read.
 - A Skill you have read is both permission to use its extra commands and an ongoing constraint: keep following it until the task ends, the user changes direction, or a later, more specific Skill overrides it. When the task enters a new phase, first check whether a previously read Skill still covers it and keep following that Skill's workflow, constraints, and completion criteria rather than drifting to the generic path. If multiple apply, follow the most specific one; if unclear, reread it before continuing.
 - Use reply_to_user immediately before <end></end> to report completion.
 - Special Command <end></end>: signals all requirements are met in real functionality (not just visual structure) and terminates the workflow. Do NOT use it when waiting for user input or clarification.
 - CRITICAL: NEVER use <end></end> in the same response as any function call (Editor.read, Terminal.run, MCP tools, etc.). Function outputs appear in the NEXT round — wait to observe them before deciding next steps. Only use <end></end> AFTER seeing all outputs and confirming completion.
"""

# Command-usage guidance for the provider-native tool-use protocol. No
# <end></end> / command-tag mechanics: tools are structured tool calls and a
# turn ends when the model makes no tool call (replies with plain text only).
# The final-output/structured-summary contract lives in COMPACTION_SECTION
# (a protocol-agnostic, compaction-gated system prompt section) because it
# describes a compression artifact, not the command protocol — and both XML and
# native should get it.
#
# NOTE: this constant is currently unused — the native channel supplies an empty
# command_guide (a native model reaches its tools via the API tools= param and
# ends a turn by making no tool call, so it needs no "# Using commands"
# mechanics). Kept defined as the documented native command-guide text.
NATIVE_COMMAND_GUIDE = """# Using commands

## Tool Usage Guidelines

Call the available tools to accomplish the user's goal. You may call multiple tools in one response — they run sequentially and their results return in the next round.
- **Tool Scope**: Only call tools that appear in *Available Commands*, a Skill document you have read for this task, or external MCP tools. If an instruction or example mentions a tool that is neither available nor documented by a Skill you have read, ignore it for this turn.
- **Completion Rule**: To finish, stop calling tools and reply with a normal text message reporting the outcome — the turn ends when you make no tool call."""


# Static orientation for the XML tool catalog. It explains how the tool
# categories relate and how to call them. Built-ins live in the system prompt;
# volatile MCP/pipeline definitions are injected by ToolCatalogContextSource.
# Generalized (no runtime has_mcp/has_pipeline branching): the sections it names
# ("# MCP Tools" / "# Pipeline Tools") are only present when those categories
# exist, and each says "if any are listed", so naming an absent section is inert.
XML_TOOL_USAGE_GUIDE = """# Using tools
Built-in commands appear above under `# Available Commands`. External MCP tools and background pipeline tools may be announced dynamically in `<system-reminder>` sections named `# MCP Tools` and `# Pipeline Tools`. Call every tool directly by name with keyword arguments, regardless of category. MCP names use `server:tool_name` (for example `github:get_me`); MCP tools connect to external services and may fail — if one does, inform the user."""


# Returned as the react loop's final reply when the hard budget cap halts the
# agent before its next think. Phrased for the user (not the model): the loop is
# already stopping, so there is no further LLM round to consume it.
BUDGET_EXHAUSTED = "Stopped: this agent reached its configured budget cap. No further model calls will be made."
