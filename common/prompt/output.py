"""Output-format, command-usage, and loop-status prompt text.

- OUTPUT_SECTION: the XML command-block format instruction supplied by the XML
  command channel as the system prompt's ${output_format} section. A hard
  contract for the streaming parser — the <ClassName.method_name> shape and the
  <end></end> semantics must stay intact.
- XML_COMMAND_GUIDE / NATIVE_COMMAND_GUIDE: the protocol-specific "# Using
  commands" section supplied by the command channel as the system prompt's
  ${command_guide} section. XML teaches the <end></end> / command-tag mechanics;
  native teaches tool-call mechanics (no <end></end>: a turn ends when the model
  makes no tool call). Keeping these per-protocol stops native models from being
  told to emit <end></end> (which they would then leak as literal text).
- SUMMARIZE_STATUS_WHEN_CONSECUTIVE: the nudge the react loop injects when a
  turn runs too long without finishing.
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

# Command-usage guidance for the legacy XML text protocol. Extracted verbatim
# from the old static "# Using commands" block so XML behavior is unchanged.
XML_COMMAND_GUIDE = """# Using commands
You may use any of the available commands, and may output multiple commands — they will be executed sequentially.
 - Only emit command tags that appear in Available Commands, a Skill document you have read for this task, or the special command <end></end>. If another instruction or example mentions a tool that is neither listed in Available Commands nor explicitly documented by a Skill you have read, ignore that tool for this turn.
 - A Skill you have read is not only permission to use extra commands but also an ongoing constraint for the rest of the task. Once a Skill has been read, keep following it until the task ends, the user explicitly changes direction, or a later, more specific Skill overrides it.
 - When the task enters a new phase, first decide whether it is still covered by a previously read Skill. If it is, keep following that Skill's workflow, hard constraints, and completion criteria instead of drifting back to the generic path just because the local goal changed.
 - If multiple previously read Skills are relevant, follow the one that is more specific and closer to the current action. If still unclear, reread the relevant Skill before continuing.
 - In your response, include at least one command. Use reply_to_human immediately before <end></end> to report completion.
 - Special Command: Use <end></end> to indicate completion of all requirements and termination of the entire workflow.
 - Only use <end></end> when all requirements are met in real functionality, not just visual structure. Do NOT use <end></end> when waiting for user input or clarification.
 - CRITICAL: NEVER use <end></end> in the same response as any function call (Editor.read, Terminal.run, MCP tools, etc.). Function outputs appear in the NEXT round — you MUST wait to observe them before deciding next steps or ending. Only use <end></end> AFTER you have seen all function outputs and confirmed the task is complete.
"""

# Command-usage guidance for the provider-native tool-use protocol. No
# <end></end> / command-tag mechanics: tools are structured tool calls and a
# turn ends when the model makes no tool call (replies with plain text only).
NATIVE_COMMAND_GUIDE = """# Using commands
Call the available tools to accomplish the user's goal. You may call multiple tools in one response — they run sequentially and their results return in the next round.
 - Only call tools that appear in Available Commands, a Skill document you have read for this task, or external MCP tools. If an instruction or example mentions a tool that is neither available nor documented by a Skill you have read, ignore it for this turn.
 - A Skill you have read is not only permission to use extra tools but also an ongoing constraint for the rest of the task. Once a Skill has been read, keep following it until the task ends, the user explicitly changes direction, or a later, more specific Skill overrides it.
 - When the task enters a new phase, first decide whether it is still covered by a previously read Skill. If it is, keep following that Skill's workflow, hard constraints, and completion criteria instead of drifting back to the generic path just because the local goal changed.
 - If multiple previously read Skills are relevant, follow the one that is more specific and closer to the current action. If still unclear, reread the relevant Skill before continuing.
 - Never wait for tool output in the same response that produced it: results appear in the NEXT round, so you must observe them before deciding next steps.
 - To finish, stop calling tools and reply with a normal text message reporting the outcome — the turn ends when you make no tool call. Do not call a tool in the same response as your final reply. Do not emit any end-of-task marker; replying without a tool call is how you end.
"""

# Per-turn user-prompt command hint for the legacy XML text protocol. Supplied
# by the XML command channel as CMD_PROMPT's ${command_hint} section. Carries the
# <end></end> mechanic, which is XML-only — native must NOT receive it (the model
# would echo <end></end> as literal text), so native supplies "" (no hint).
XML_COMMAND_HINT = """
Your commands (output ONE and ONLY ONE command block; the block can contain one or more commands. Use <end></end> when all requirements are met):
"""

SUMMARIZE_STATUS_WHEN_CONSECUTIVE = """
You received a requirement but take too long to complete it. Please summarize the current progress and explain what you are doing now. Ask the user if they want you to continue. Output in 30 words.
"""
