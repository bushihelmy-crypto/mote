"""Output-format and loop-status prompt text.

- OUTPUT_SECTION: the XML command-block format instruction supplied by the XML
  command channel as the system prompt's ${output_format} section. A hard
  contract for the streaming parser — the <ClassName.method_name> shape and the
  <end></end> semantics must stay intact.
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

SUMMARIZE_STATUS_WHEN_CONSECUTIVE = """
You received a requirement but take too long to complete it. Please summarize the current progress and explain what you are doing now. Ask the user if they want you to continue. Output in 30 words.
"""
