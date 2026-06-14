# Ops-Sentinel — Devpost Submission

## Inspiration

On-call engineers face the same incidents over and over. A service overloads, a dependency goes down — and every time, someone has to diagnose it from scratch. Runbooks go stale, context is lost between shifts, and response times degrade under alert fatigue. We wanted to build an agent that actually *remembers* — one that gets smarter with every incident it handles.

## What it does

Ops-Sentinel is an autonomous on-call agent with persistent incident memory. It continuously monitors a target service, detects anomalies, and — instead of starting from a blank slate — searches its own history of past incidents using **semantic vector similarity**. When a fault recurs, the agent retrieves the most similar past cases (with cosine similarity scores), feeds them to Qwen as context, and reasons: *"last time this happened, we restarted the service and it resolved."* It then executes the remediation action and records the new incident with an embedding for future retrieval.

Key capabilities:
- **Semantic memory**: every incident embedded with `text-embedding-v3` (1024-dim), retrieved by cosine similarity — not keyword matching
- **MCP tool-calling**: Qwen autonomously calls 4 MCP tools (`search_similar_incidents`, `record_incident`, `get_recent_incidents`, `get_stats`) in a loop with no hardcoded decision tree
- **Live observability**: status page shows each MCP tool call with `similarity_score` — judges can see `[semantic] id=12 sim=0.94` in real time
- **Three-layer resilience**: semantic search → text-overlap fallback → rule-based safe mode; the agent never crashes

## How we built it

**Week 1** — Core agent loop: `MetricsCollector` polls a FastAPI demo service, `AnomalyDetector` extracts symptoms, `QwenClient` diagnoses via chat API, `IncidentMemory` stores incidents in SQLite with text-overlap retrieval. Full stack running locally.

**Week 2** — MCP layer: built `incident_mcp_server` using FastMCP with Streamable HTTP transport. Rewrote the Qwen client to use OpenAI function-calling API — Qwen drives the tool-calling loop autonomously. Agent connects as MCP client via a sync wrapper over an async event loop in a daemon thread. Three-container Docker Compose deployed to Alibaba Cloud ECS.

**Week 3** — Semantic embeddings: added `EmbeddingClient` wrapping `text-embedding-v3`, stored vectors as float32 BLOBs in a new `incident_embeddings` SQLite table. `search_similar_incidents` now embeds the query and runs cosine similarity in Python over retrieved vectors. Fixed a critical production bug: `HTTPTransport(local_address="127.0.0.1")` was binding the outgoing socket to loopback, causing `EINVAL` for all cross-container connections and generating thousands of false `service_unreachable` incidents. After the fix, semantic search activated immediately with sim scores ≈ 0.91–0.94.

## Challenges

**The loopback trap**: the hardest bug to diagnose — demo service was reachable via `urllib` but the agent's `httpx` client silently failed with `[Errno 22] Invalid argument`. Root cause: a Windows IPv6 workaround (`local_address='127.0.0.1'`) that was harmless on the dev machine bound the Linux container socket to loopback, making it impossible to reach other containers. `urllib` has no such bind, so it worked. Four thousand false incidents later, we found it.

**Sticky fallback**: `EmbeddingClient.available` was set to `False` on any API error and never reset (the recovery path required a successful call, but the gate prevented retrying). Semantic search stayed permanently disabled after one transient startup hiccup. Fixed by removing the gate entirely — `embed()` is tried on every call and self-heals.

**FastMCP list serialisation**: FastMCP wraps each element of a `list[dict]` return value as a separate `TextContent` item rather than a single JSON array. Required a custom parse helper on the client side.

## Accomplishments

- Semantic retrieval working in production: queries with zero keyword overlap correctly find conceptually similar past incidents
- Similarity scores visible in the UI — concrete, judge-readable proof of semantic memory
- Zero-downtime architecture: agent keeps running through MCP failures, Qwen failures, and embedding API failures independently
- Clean MCP integration: 4 tools exposed via Streamable HTTP, Qwen calls them autonomously with no prompt engineering tricks
- Full stack deployed on Alibaba Cloud ECS, publicly accessible at http://47.237.196.56/

## What's next

- **Multi-service memory**: extend to monitor multiple services; use symptom + service tags in embeddings for cross-service pattern detection
- **Adaptive thresholds**: agent updates anomaly thresholds based on observed false-positive rate stored in memory
- **Human-in-the-loop**: for `halt` actions, surface a confirmation webhook before executing; log the decision with full context
- **Embedding re-ranking**: use Qwen chat to re-rank the top-K semantic hits before feeding to the diagnosis prompt
- **Memory pruning strategy**: implement importance-weighted retention (resolved + rare faults kept longer than common noise)

## Built with

- **Qwen Cloud** — `qwen3.6-flash` (chat + tool-calling), `text-embedding-v3` (1024-dim semantic embeddings)
- **Python 3.11** — FastAPI, uvicorn, httpx, numpy, openai SDK
- **MCP** — `mcp[cli]` (FastMCP), Streamable HTTP transport
- **SQLite** — incident store + float32 embedding BLOBs in a shared Docker volume
- **Docker Compose** — three-container architecture (demo service, MCP server, agent)
- **Alibaba Cloud ECS** — live deployment at http://47.237.196.56/
