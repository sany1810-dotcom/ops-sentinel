# Hackathon Log — Ops-Sentinel

All code in this repository was created during the Qwen Cloud Hackathon (started 2026-06-11).
No pre-existing code was used. Commits are the authoritative timeline.

---

## Week 1 — 2026-06-11

### Session 1 — Project bootstrap

**Done:**
- Initialized git repository with MIT LICENSE, README, .gitignore
- Read Qwen Cloud docs: selected `qwen3.6-flash` (fast + cost-effective) for agent loop
- Installed qwencloud Claude Code skills (`npx skills add qwencloud/qwencloud-ai`)
- Created `demo_service/` — FastAPI target service with fault injection:
  - `POST /inject {"fault": "overload|memory_leak|dependency_down"}`
  - `POST /reset` — clears injected fault
  - `GET /health`, `GET /metrics`, `GET /api/data`
- Created `agent/` — full agent skeleton:
  - `memory.py` — SQLite incident store, retrieval by symptom overlap
    (interface designed for embedding-search swap in Week 2-3)
  - `qwen_client.py` — Qwen API client, 3× retry with exponential backoff,
    falls back to rule-based safe mode on API failure
  - `collector.py` — polls demo service metrics (latency, RSS, error rate)
  - `detector.py` — threshold-based anomaly detection → symptom list
  - `actions.py` — executes alert / restart / halt
  - `main.py` — FastAPI status page + background agent loop thread
- Created `deploy/` — Dockerfiles, docker-compose.yml, Alibaba Cloud ECS setup guide
- Created `.env.example` (no secrets committed)

### Session 2 — Smoke test passed locally

**Done:**
- Verified QWEN_API_KEY (qwen3.6-flash responds)
- Diagnosed and fixed Windows localhost IPv6-first issue in collector.py
  (httpx was waiting ~2s for IPv6 timeout before IPv4 fallback;
   fixed with `HTTPTransport(local_address='127.0.0.1')`)
- Full smoke test passed locally:

```
INJECT #1 (overload):
  ANOMALY: high_latency_critical
  Memory: No similar incidents — fresh case
  Qwen: "Service experiencing critical latency due to overload" confidence=0.85
  Action: restart → outcome=restarted → Incident #1 saved

INJECT #2 (same overload):
  ANOMALY: high_latency_critical
  Memory: Found 1 similar past incident (#1, action=restart, resolved=True)
  Qwen: "Service overload causing critical latency" confidence=0.90 ← HIGHER due to memory
  Action: restart → outcome=restarted → Incident #2 saved
```

Key: confidence rose 0.85 → 0.90 between cycles as Qwen gained memory context.
Qwen LLM call latency: ~8-10s (acceptable, non-blocking).

**Next:**
- Week 2: Add MCP layer

---

## Week 2 — 2026-06-13

### Session 3 — MCP server + agentic tool-calling

**Done (§0):**
- Read MCP documentation (modelcontextprotocol.io + Python SDK GitHub)
- Confirmed: SDK v1.27.2, FastMCP API, Streamable HTTP is current recommended transport
- Endpoint: `/mcp`, client: `streamablehttp_client`, server: `mcp.run(transport="streamable-http")`

**Done (§1 — MCP server in isolation):**
- Created `incident_mcp_server/main.py` — FastMCP server, Streamable HTTP, port 8002
  - Reuses `agent/memory.py` (zero duplication)
  - Tools: `search_similar_incidents`, `record_incident`, `get_recent_incidents`, `get_stats`
  - Resources: `incidents://recent`, `incidents://stats`
- Created `incident_mcp_server/requirements.txt` (`mcp[cli]>=1.0.0`)
- Created `incident_mcp_server/smoke_test.py` — async MCP client test
- Smoke test passed: all 4 tools + 2 resources verified in isolation (server not connected to agent)
- Key finding: FastMCP v1.x returns each element of a `list[dict]` tool result as a separate TextContent item

**Done (§2 — Agent as MCP client + Qwen tool-calling):**
- Created `agent/mcp_client.py` — `IncidentMCPClient` class
  - Synchronous wrapper around async MCP SDK
  - Background asyncio event loop in daemon thread
  - Per-call short-lived sessions (simple, no session expiry issues)
  - `probe()` on startup, `reconnect()` available for recovery
- Modified `qwen_client.py` — added `diagnose_with_mcp()` method
  - Full agentic loop: Qwen calls MCP tools via OpenAI function-calling API
  - Tool results fed back to Qwen; loop continues until no more tool calls
  - Safety cap: max 6 rounds
  - Keeps original `diagnose()` method untouched (Week 1 fallback)
- Modified `agent/main.py` — wired MCP into agent loop
  - `MCP_SERVER_URL` env var (default: `http://localhost:8002/mcp`)
  - MCP-first path: `diagnose_with_mcp()` → Qwen calls tools autonomously
  - Falls back to Week 1 path on any MCP failure (§4)
  - Incident recording also routed through MCP when available

**Done (§3 — MCP observability on status page):**
- `last_mcp_calls` tracked in shared agent state (tool name, args, result per call)
- Status page has new "MCP Tool Calls — Last Anomaly Cycle" table
- MCP ON/OFF badge shown next to agent status
- New API endpoint: `GET /api/mcp/calls` for JSON access
- `GET /health` now includes `"mcp": true/false`

**Done (§4 — Safe-mode preserved):**
- MCP down at startup → `mcp.available = False` → agent uses Week 1 direct path
- MCP tool call exception during cycle → catches exception → falls back to Week 1
- `qwen.diagnose_with_mcp()` failure → falls back to `_rule_based_fallback()`
- Background reconnect attempt after each failed cycle

**Done (§5 — Architecture diagram):**
- ASCII diagram in README.md showing all three containers + data flow

**Done (§6 — Three-container docker-compose):**
- Created `deploy/Dockerfile.mcp`
- Added `incident-mcp-server` service to `docker-compose.yml`
  - Shares `agent-data` volume with agent (same `/data/incidents.db`)
  - No public port — only accessible by agent at `incident-mcp-server:8002`
- Agent `depends_on: [demo-service, incident-mcp-server]`
- Updated `Dockerfile.agent` to install `mcp[cli]>=1.0.0`
- Added `mcp[cli]>=1.0.0` to `agent/requirements.txt`

**End-to-end verification (local):**
```
MCP server: 4 tools, 2 resources — smoke test passed
Agentic loop test:
  metrics={rss_mb:350, latency_ms:2800, fault:"overload"}
  symptoms=["high_latency", "high_rss"]

  Qwen called: search_similar_incidents(symptoms=["high_latency","high_rss"], limit=5)
  Result: 2 past incidents with overlapping symptoms

  Final diagnosis:
    "Transient service overload causing elevated memory usage and latency"
    action=restart  confidence=0.90
```

### Session 4 — Bug fix + deployment to Alibaba Cloud ECS

**Bug fixed:**
- Production agent fell into safe-mode with `UnicodeEncodeError: 'ascii' codec
  can't encode characters in position 7-15`
- Root cause: `str(tool_result)` in the MCP tool-call log line produced raw Unicode
  (Russian diagnosis text from SQLite), which failed on ASCII-locale Docker container
- Fix in `qwen_client.py`:
  - `_sanitize_ascii()` — fails fast at startup if QWEN_API_KEY/URL/MODEL have
    non-ASCII (logs positions, never reveals key value)
  - `_safe_json()` wrapper — `json.dumps(ensure_ascii=True)` everywhere tool results
    are serialised for logs or HTTP body
  - Replaced unicode arrow `->` in log format strings (ASCII-safe)
  - Added dedicated `except UnicodeEncodeError` branch with encoding/position/reason
    logged for unambiguous diagnosis
  - `main.py`: `.strip()` all env vars at read time

**Deployed to Alibaba Cloud ECS 47.237.196.56:**
- Three containers: `ops-demo`, `incident-mcp-server`, `ops-agent`
- Shared `agent-data` Docker volume for `incidents.db`
- MCP server on internal port 8002 (not exposed publicly)
- Agent on public port 80

**Live agentic cycle confirmed:**
```
MCP server: 4 tools online (search_similar_incidents, record_incident,
            get_recent_incidents, get_stats) + 2 resources
Agent cycle (MCP ON):
  Anomaly detected -> symptoms: [high_latency, high_rss]
  Qwen called: search_similar_incidents(symptoms=[...], limit=5) via MCP
  Result: past incidents retrieved from memory
  Final diagnosis: action=restart  confidence=0.90  safe_mode=false
  Incident recorded via MCP record_incident
  No [SAFE MODE] prefix — autonomous Qwen + MCP loop working
```

Status page: http://47.237.196.56 (MCP ON badge, tool call log visible)

---

## Week 3 — 2026-06-13 / 2026-06-14

### Session 5 — Semantic embedding search (text-embedding-v3)

**Done (§0–§3 — embedding layer):**
- Created `agent/embedding_client.py`:
  - `EmbeddingClient` — thin wrapper around Qwen `text-embedding-v3` (DashScope intl, 1024-dim)
  - `build_embed_text(symptoms, metrics, diagnosis)` — canonical text for storage and query
  - Returns unit-normalised float32 vectors; cosine similarity = dot product
  - `available` flag; sets False on API failure, resets True on recovery
- Extended `agent/memory.py`:
  - New table `incident_embeddings (incident_id PK, model, embedding BLOB, created_at)`
  - `save_embedding(id, model, vec)` — INSERT OR REPLACE, float32 tobytes()
  - `find_similar_semantic(query_vec, limit)` — JOIN + numpy dot product, sorted desc
  - `get_incidents_without_embeddings()` — LEFT JOIN WHERE NULL, for migration
  - `embedding_coverage()` — (embedded, total) for observability
- Extended `incident_mcp_server/main.py`:
  - `search_similar_incidents` uses semantic search when embedder available; falls back to text-overlap (§5)
  - `record_incident` computes and stores embedding immediately after save
  - `get_stats` includes `embedded`, `embedding_coverage`, `semantic_search` fields
- Created `agent/migrate_embeddings.py` — idempotent backfill; 0.05s sleep between API calls
- Updated `deploy/Dockerfile.mcp`, `docker-compose.yml` — QWEN_EMBED_MODEL env var added
- Updated `requirements.txt` files — `numpy>=1.24.0`, `openai>=1.30.0`

**Done (§6 — similarity score in UI):**
- Status page "MCP Tool Calls" table now renders `search_similar_incidents` result as:
  `[semantic] id=X sim=0.91, id=Y sim=0.84` instead of truncated raw JSON
- Full JSON still available on hover (title attribute)
- Column renamed "Result / Similarity"

**Done (§7 — local embed test):**
- `agent/embed_test.py` — 3 seed incidents with distinct fault patterns,
  3 queries with ZERO keyword overlap (e.g. "slow responses under load" → A:overload)
- Result: **3/3 PASS**, text-overlap baseline: 0 hits each — semantic retrieval wins

### Session 6 — Production fixes + DB cleanup + submission artifacts

**Bug fixed — collector loopback (EINVAL / Errno 22):**
- Demo service was live (`/health` → `{"status":"ok","fault":"none"}`) but agent logged
  `Health probe failed: [Errno 22] Invalid argument` → `reachable=False` → all incidents
  recorded as `service_unreachable` (4015 false positives)
- Root cause: `HTTPTransport(local_address="127.0.0.1")` in `collector.py` bound the
  outgoing socket to loopback; Linux kernel rejects this for cross-container connections
  (EINVAL). The Week 1 Windows localhost fix was never needed for Docker service names.
- Fix: removed `local_address="127.0.0.1"` from transport. `urllib` confirmed working
  from inside the container (no bind → no EINVAL).

**DB cleanup:**
- 4015 incidents in DB, all `service_unreachable` (false positives from loopback bug)
- Pruned to 15 most recent with one-liner (backup created first at `/data/incidents.db.bak.*`)
- Added `agent/prune_incidents.py` — idempotent script, keeps N most recent per fault type,
  deletes embeddings FK first, VACUUMs
- Ran `migrate_embeddings.py` → coverage **15/15**

**Bug fixed — sticky `available=False` in semantic search:**
- `embed()` sets `available=False` on any exception; `search_similar_incidents` gated on
  `_embedder.available` → after one transient startup failure, semantic search permanently
  disabled until container restart
- Fix in `incident_mcp_server/main.py`: removed `available` gate from both
  `search_similar_incidents` and `record_incident`; `embed()` is tried every call and
  self-heals on success. Added INFO/WARNING logs at every decision point.

**Semantic search confirmed in production:**
```
search_similar_incidents called
  → [semantic] id=15 sim=0.94, id=14 sim=0.91, id=12 sim=0.87
  search_mode: semantic  (not text_fallback)
```
Status page MCP Tool Calls table shows similarity scores live.

**Auto-embed on write:**
- `record_incident` now computes and stores embedding immediately (no migration needed
  for new incidents). Log: `record_incident id=N: embedded OK`

**Submission artifacts:**
- `README.md` — rewritten in English for international judges: pitch, problem, Mermaid
  architecture diagram (3 containers + Qwen Cloud), feature table, tech stack, demo flow
- `docs/architecture.md` — expanded diagram + data-flow walkthrough + storage schema table
- `.env.example` — updated with `QWEN_EMBED_MODEL`
- `docs/Ops-Sentinel.pptx` — 10-slide dark-theme deck (generated by `docs/make_pptx.py`):
  title, problem, solution, architecture, MCP integration, semantic memory with UI evidence,
  resilience, tech stack, demo flow, closing pillars
- `docs/devpost_description.md` — Devpost submission text (Inspiration / What it does /
  How we built it / Challenges / Accomplishments / What's next / Built with)

**Final state:**
- Live: http://47.237.196.56/ — semantic search active, sim scores visible in UI
- DB: 15 incidents, 15/15 embedded, every new incident auto-embedded on write
- Three-layer fallback: semantic → text-overlap → rule-based (agent never stops)

---
