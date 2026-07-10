---
name: ollama
description: >-
  Local LLM inference and model management via Ollama. Pull/list/remove models,
  generate text, run chat completions, and create embeddings using the official
  ollama CLI and REST API. Runs fully offline once models are pulled.
when_to_use: >-
  When you need local (offline/private) LLM inference — quick generation, chat,
  or embeddings — or to manage local models. Use the official ollama command via
  Bash, or the REST API via curl for structured output.
---

# Ollama local LLM (official CLI + REST API)

Ollama serves local LLMs. Use the **official `ollama` binary** and its REST API
directly — no wrapper package needed.

## Prerequisites

- Ollama installed: https://ollama.com/download
- The server running: `ollama serve` (often already running as a service)

Check the server is up before other work:
```bash
curl -s http://localhost:11434/api/version || echo "ollama server not reachable"
```

## Model management (official CLI)

```bash
ollama list                 # locally available models
ollama pull llama3.2        # download a model
ollama show llama3.2        # parameters / template / license
ollama ps                   # models currently loaded in memory
ollama cp llama3.2 mycopy   # copy a model to a new name
ollama rm mycopy            # delete a model
```

## Text generation

Interactive (use the Terminal tool for a persistent session):
```bash
ollama run llama3.2 "Explain quantum computing in one sentence"
```

Structured, non-streaming via REST (best for agents — parseable JSON):
```bash
curl -s http://localhost:11434/api/generate \
  -d '{"model":"llama3.2","prompt":"Hello","stream":false}'
```

## Chat completions (REST)

```bash
curl -s http://localhost:11434/api/chat \
  -d '{
    "model": "llama3.2",
    "messages": [
      {"role": "user", "content": "What is Python?"}
    ],
    "stream": false
  }'
```

Multi-turn: append prior `assistant` / `user` messages to the `messages` array.

## Embeddings (REST)

```bash
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"Hello world"}'
```

## Remote host

Point at a non-default host with the `OLLAMA_HOST` env var or a full URL:
```bash
OLLAMA_HOST=http://192.168.1.100:11434 ollama list
curl -s http://192.168.1.100:11434/api/tags
```

## Agent guidance

1. Verify the server with `/api/version` before issuing generate/chat calls.
2. Use REST with `"stream": false` for complete, parseable responses; use
   `ollama run` in a Terminal session only for interactive exploration.
3. `ollama list` first — if the target model isn't present, `ollama pull` it
   (this can be slow/large; mention it to the user).
4. Pipe REST JSON through `jq` to extract just `.response` / `.message.content`.
5. Embeddings are returned under `.embedding`; `nomic-embed-text` is a common
   local embedding model.
