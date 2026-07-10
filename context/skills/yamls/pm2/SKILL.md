---
name: pm2
description: >-
  Manage Node.js (and other) processes with the official PM2 process manager —
  start, stop, restart, list, inspect, and tail logs of long-running services.
  Uses the official pm2 CLI directly.
when_to_use: >-
  When you need to run, monitor, or control long-lived background processes
  (Node servers, workers, dev services) with restart-on-crash and log capture.
  Use the official pm2 command via the Bash or Terminal tool.
---

# PM2 process manager (official CLI)

PM2 keeps processes alive, restarts them on crash, and captures their logs. Use
the **official `pm2` binary** directly — no wrapper needed.

## Prerequisites

- Node.js installed
- PM2 installed globally: `npm install -g pm2`

Verify:
```bash
pm2 --version || echo "pm2 not installed"
```

## Process lifecycle

```bash
# Start a script as a named process
pm2 start app.js --name my-api

# Start with an interpreter / args / watch mode
pm2 start server.py --name worker --interpreter python3
pm2 start npm --name web -- run start

pm2 restart my-api
pm2 reload my-api        # zero-downtime reload (cluster mode)
pm2 stop my-api
pm2 delete my-api
```

## Inspection (introspect before acting)

```bash
pm2 list                 # all processes, status, cpu/mem
pm2 jlist                # same as list but JSON (parseable for agents)
pm2 describe my-api       # detailed info for one process
pm2 show my-api           # alias of describe
pm2 prettylist            # verbose human dump
```

Prefer `pm2 jlist` when you need to parse state programmatically.

## Logs

```bash
pm2 logs my-api --lines 50        # tail recent logs (streams; use Terminal)
pm2 logs my-api --lines 100 --nostream   # print then return (use Bash)
pm2 flush                          # clear all logs
pm2 flush my-api                   # clear one process's logs
```

## Persistence / startup

```bash
pm2 save                  # snapshot current process list
pm2 resurrect             # restore the saved list
pm2 startup               # generate an OS boot startup script (prints a command to run)
```

## Agent guidance

1. Use `pm2 jlist` for machine-readable state; `pm2 list` only for human view.
2. `pm2 logs ... --nostream` when you just need recent output and control back;
   plain `pm2 logs` streams and belongs in a persistent Terminal session.
3. Check `pm2 describe <name>` (status, restarts, uptime) before restart/stop to
   understand why a process is unhealthy rather than blindly restarting.
4. `pm2 startup` and `pm2 save` require care — `startup` prints a privileged
   command; surface it to the user rather than assuming sudo.
5. Name every process (`--name`) so later commands are unambiguous.
