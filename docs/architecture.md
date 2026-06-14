# Ops-Sentinel — Architecture

```mermaid
flowchart TD
    Browser["Browser / Judge\nStatus Page + Fault Injection UI"]

    subgraph public["Public  :80"]
        StatusPage["FastAPI\nGET /  →  status page\nPOST /demo/inject\nPOST /demo/reset"]
    end

    subgraph agent_box["ops-agent container"]
        Collector["MetricsCollector\npolls every 10 s"]
        Detector["AnomalyDetector\nsymptom extraction"]
        QwenLoop["QwenClient\ntool-calling loop\nqwen3.6-flash"]
        SafeMode["Safe-Mode Fallback\nrule-based diagnosis\n(MCP or Qwen down)"]
        Executor["ActionExecutor\nrestart / alert / halt"]
    end

    subgraph mcp_box["incident-mcp-server container"]
        MCPServer["FastMCP\nStreamable HTTP  :8002/mcp"]
        T1["search_similar_incidents\ncosine similarity + sim score"]
        T2["record_incident\nsave + auto-embed"]
        T3["get_recent_incidents"]
        T4["get_stats\ncoverage + semantic flag"]
        EmbedClient["EmbeddingClient\ntext-embedding-v3"]
        DB[("SQLite  /data/incidents.db\nincidents table\nincident_embeddings BLOB")]
    end

    subgraph demo_box["ops-demo container  :8000"]
        Demo["Demo Service\n/health  /metrics\n/inject  /reset"]
    end

    subgraph qwen_cloud["Qwen Cloud  (DashScope)"]
        ChatAPI["Chat API\nqwen3.6-flash"]
        EmbedAPI["Embedding API\ntext-embedding-v3"]
    end

    Browser -->|HTTP| StatusPage
    StatusPage <-->|internal| Collector

    Collector -->|GET /health\nGET /metrics| Demo
    Demo -->|metrics snapshot| Collector
    Collector --> Detector
    Detector -->|symptoms + metrics| QwenLoop
    QwenLoop <-->|chat + tool_calls| ChatAPI

    QwenLoop -->|MCP tool calls\nStreamable HTTP| MCPServer
    MCPServer --> T1 & T2 & T3 & T4
    T1 & T2 --> EmbedClient
    EmbedClient <-->|embed text| EmbedAPI
    T1 & T2 & T3 & T4 <--> DB

    MCPServer -->|results + similarity_score| QwenLoop
    QwenLoop --> Executor

    Detector -- "MCP/Qwen down" --> SafeMode
    SafeMode -->|direct read| DB
    SafeMode --> Executor
```

## Data flow — anomaly cycle

1. **Collect** — `MetricsCollector` calls `demo-service:8000/health` (latency) and `/metrics` (RSS, error rate, fault) every `AGENT_POLL_INTERVAL` seconds.
2. **Detect** — `AnomalyDetector` compares metrics against thresholds and emits a symptom list (`high_latency`, `high_rss`, `dependency_error`, …).
3. **Diagnose** — `QwenClient` opens a tool-calling session with `qwen3.6-flash`.  Qwen calls:
   - `search_similar_incidents` — MCP server embeds the query with `text-embedding-v3`, runs cosine similarity over stored vectors, returns top-N incidents with `similarity_score`.
   - `get_stats` — embedding coverage, semantic search flag.
   - `record_incident` — saves new incident and immediately computes + stores its embedding.
4. **Act** — agent executes the action (restart / alert / halt).
5. **Safe mode** — if MCP server or Qwen API is unreachable, agent falls back to rule-based diagnosis and direct SQLite text-overlap search, then retries MCP in the background.

## Storage

| Table | Contents |
|-------|----------|
| `incidents` | ts, symptoms (JSON), metrics_snapshot (JSON), diagnosis, action, outcome, resolved |
| `incident_embeddings` | incident_id (FK), model, embedding (float32 BLOB, 1024-dim), created_at |

Shared as a named Docker volume (`agent-data`) between `ops-agent` and `incident-mcp-server`.
