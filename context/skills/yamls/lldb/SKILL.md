---
name: lldb
description: >-
  Debug native programs (C, C++, Objective-C, Rust, Swift) with the official
  LLDB debugger — set breakpoints, step, inspect variables/backtraces, evaluate
  expressions, read memory, and analyse core dumps. Interactive and persistent.
when_to_use: >-
  When a native (compiled) program crashes, segfaults, hangs, or misbehaves and
  you need to inspect its runtime state — breakpoints, backtraces, variable
  values, or a core dump. Drive the official lldb via the Terminal tool (it is
  an interactive, stateful session).
---

# LLDB native debugger (official CLI, via Terminal)

LLDB is the LLVM debugger for native code. It is **interactive and stateful**:
breakpoints, the loaded target, and the stopped program state must persist
across commands. Drive it with the **Terminal tool** (persistent PTY), not the
one-shot Bash tool — a fresh subprocess per command would lose all state.

## Prerequisites

- `lldb` installed (bundled with Xcode/CommandLineTools on macOS; `apt install
  lldb` / `dnf install lldb` on Linux).
- Build the target with debug symbols (`-g`) for useful variable/line info.

Verify: `lldb --version`

## Core interactive workflow (in a Terminal session)

Start lldb once in the Terminal, then issue commands into the same session:

```text
lldb ./my_program          # load the executable
(lldb) breakpoint set --name main      # or: b main
(lldb) run                              # start; args: run arg1 arg2
(lldb) bt                               # backtrace when stopped
(lldb) frame variable                   # locals in current frame
(lldb) print my_var                     # evaluate an expression / var
(lldb) next                             # step over (n)
(lldb) step                             # step into (s)
(lldb) finish                           # step out
(lldb) continue                         # resume (c)
(lldb) quit
```

## Breakpoints

```text
(lldb) b main                           # by function
(lldb) breakpoint set --file main.c --line 42
(lldb) breakpoint set --name foo --condition 'i > 10'
(lldb) breakpoint list
(lldb) breakpoint delete 1
(lldb) breakpoint disable 1 / enable 1
```

## Inspecting state

```text
(lldb) thread list
(lldb) thread backtrace all             # all threads' stacks
(lldb) frame select 2                   # switch stack frame
(lldb) frame variable                   # all locals
(lldb) print (int)argc
(lldb) expression -- myVec.size()       # call methods / evaluate
(lldb) register read                    # CPU registers
(lldb) memory read --size 4 --count 16 0x1000
```

## Analysing a crash / core dump

```text
lldb ./my_program --core /path/to/core   # load a core dump
(lldb) bt                                # where it died
(lldb) frame variable                    # state at crash
```

To catch a live crash: `run`, let it segfault, then `bt` at the stop.

## Attach to a running process

```text
(lldb) process attach --pid 1234
(lldb) process attach --name myapp --waitfor
(lldb) process detach
```

## Batch / non-interactive one-shot (Bash tool is fine here)

For a scripted, no-state-needed run, use `-o` (one command) and `-b` (batch):

```bash
lldb -b -o "run" -o "bt" -o "quit" ./my_program
lldb --core core.dump -o "bt" -o "frame variable" -o "quit" ./my_program
```

## Agent guidance

1. Interactive debugging → **Terminal tool** (state must persist). Only a
   fully-scripted `lldb -b -o ... -o quit` run belongs in the Bash tool.
2. Always `run`/load a target before stepping or inspecting; `bt` first after a
   stop to orient yourself.
3. Build with `-g`; without debug symbols you get addresses, not names/lines.
4. `quit` (or detach) when done so the debuggee is cleaned up and the terminal
   returns to the shell.
5. `thread backtrace all` is invaluable for deadlocks/hangs — inspect every
   thread's stack, not just the current one.
