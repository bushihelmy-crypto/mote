---
name: exa-search
description: >-
  Neural web search and content retrieval via the Exa API. Search the web in
  multiple modes (fast, deep, deep-reasoning), filter by category/date/domain,
  and fetch full-text or highlighted page contents. Optimised for agent
  research workflows.
when_to_use: >-
  When you need up-to-date web information, academic papers, company/people
  intel, or news, and want neural (semantic) search plus clean page-content
  extraction. Requires an EXA_API_KEY. Call the Exa API with curl via the Bash
  tool.
---

# Exa web search (via the Exa API)

Exa is a neural search engine optimised for AI agents. Rather than depending on
any wrapper CLI, call the Exa HTTP API directly with `curl` from the Bash tool.

## Prerequisites

- An API key: get one at https://dashboard.exa.ai/api-keys
- `export EXA_API_KEY="your-api-key"` in the environment (never hardcode it)

Verify connectivity before a research run:
```bash
test -n "$EXA_API_KEY" && echo "EXA_API_KEY is set" || echo "EXA_API_KEY missing"
```

## Search — POST /search

```bash
curl -s https://api.exa.ai/search \
  -H "x-api-key: $EXA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "retrieval augmented generation",
    "type": "auto",
    "numResults": 10,
    "contents": { "highlights": true }
  }'
```

Key request fields:

| Field | Meaning |
|-------|---------|
| `query` | The search query (natural language works well) |
| `type` | `auto` \| `fast` \| `neural` \| `keyword` |
| `numResults` | 1–100 (default 10) |
| `category` | `company` \| `people` \| `research paper` \| `news` \| `pdf` \| `financial report` |
| `startPublishedDate` / `endPublishedDate` | ISO 8601 date filters |
| `includeDomains` / `excludeDomains` | Arrays of domains |
| `contents.text` | `true` for full text (token-heavy) |
| `contents.highlights` | `true` for concise highlights (≈10× cheaper) |
| `contents.summary` | `true` for an LLM summary per result |

## Fetch page contents — POST /contents

```bash
curl -s https://api.exa.ai/contents \
  -H "x-api-key: $EXA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "ids": ["https://example.com/article"],
    "text": true
  }'
```

## Common agent patterns

Fast keyword lookup (token-efficient highlights):
```bash
curl -s https://api.exa.ai/search -H "x-api-key: $EXA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"site:arxiv.org transformer architectures","type":"fast","contents":{"highlights":true}}'
```

Academic paper discovery:
```bash
curl -s https://api.exa.ai/search -H "x-api-key: $EXA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"retrieval augmented generation","category":"research paper","numResults":20,"contents":{"highlights":true}}'
```

News monitoring since a date:
```bash
curl -s https://api.exa.ai/search -H "x-api-key: $EXA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"AI regulation news","category":"news","startPublishedDate":"2026-01-01","contents":{"highlights":true}}'
```

## Agent guidance

1. Prefer `highlights` over full `text` — roughly 10× more token-efficient; only
   fetch full `text` when you need to summarise a specific page.
2. `type: "fast"` for quick lookups; `type: "auto"` lets Exa pick; deeper modes
   synthesise across more sources but are slower and cost more.
3. `category: "company"` / `"people"` do not support date or domain-exclude
   filters.
4. Cost per query is returned in the JSON response (`costDollars`) — surface it
   if the user cares about spend.
5. Pipe responses through `jq` to extract just the fields you need and keep the
   context small, e.g. `... | jq '.results[] | {title, url, highlights}'`.
