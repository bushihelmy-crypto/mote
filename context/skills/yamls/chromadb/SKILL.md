---
name: chromadb
description: >-
  Manage a ChromaDB vector database — collections, documents, and semantic
  search — via the official chromadb Python client or the HTTP API. For
  building/querying embeddings-based retrieval (RAG) stores without a UI.
when_to_use: >-
  When you need to store documents as embeddings and run semantic search
  (RAG / knowledge retrieval). Use the official chromadb Python client via the
  Python tool, or the REST API via curl for a running server.
---

# ChromaDB vector database (official client + HTTP API)

ChromaDB stores documents as embeddings and serves semantic search. Use the
**official `chromadb` Python client** (via the Python tool) for the richest
surface, or the HTTP API (via curl) against a running server. No wrapper CLI.

## Prerequisites

- Python client: `pip install chromadb`
- For a shared/persistent server: `chroma run --host localhost --port 8000`
  (embedded/in-process mode needs no server)

Check a running server:
```bash
curl -s http://localhost:8000/api/v2/heartbeat || echo "chroma server not reachable"
```

## Python client (recommended — use the Python tool)

```python
import chromadb

# In-process persistent client (no server needed)
client = chromadb.PersistentClient(path="./chroma_store")
# Or connect to a running server:
# client = chromadb.HttpClient(host="localhost", port=8000)

# Create / get a collection (Chroma embeds text for you by default)
col = client.get_or_create_collection(name="hub_knowledge")

# Add documents (ids must be unique; Chroma computes embeddings)
col.add(
    ids=["doc1", "doc2"],
    documents=["How to deploy the service", "Rollback procedure"],
    metadatas=[{"topic": "deploy"}, {"topic": "ops"}],
)

# Semantic search
res = col.query(query_texts=["deployment steps"], n_results=3)
print(res["documents"], res["distances"])

# Introspect
print(col.count())
print(client.list_collections())

# Cleanup
col.delete(ids=["doc2"])
client.delete_collection("hub_knowledge")
```

## HTTP API (v2) against a running server

```bash
# Heartbeat / version
curl -s http://localhost:8000/api/v2/heartbeat
curl -s http://localhost:8000/api/v2/version

# List collections (tenant/database default to 'default_tenant'/'default_database')
curl -s http://localhost:8000/api/v2/tenants/default_tenant/databases/default_database/collections
```

For add/query over HTTP you must supply embeddings yourself (the server does not
auto-embed), so the Python client is strongly preferred for document ops.

## Agent guidance

1. Prefer the Python client via the Python tool — it handles embeddings
   automatically and keeps state in-process; only use HTTP for a shared server.
2. Use `get_or_create_collection` to stay idempotent across reruns.
3. `count()` and `list_collections()` are cheap introspection — check state
   before adding/querying.
4. `query` returns parallel lists (`ids`/`documents`/`distances`/`metadatas`);
   lower distance = closer match.
5. Persist with `PersistentClient(path=...)` so the store survives restarts;
   pass a stable path.
