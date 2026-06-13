# Ops-Sentinel

Ops-Sentinel is an autonomous on-call agent with persistent incident memory. It monitors a target service, detects anomalies, consults Qwen LLM for diagnosis via MCP tool-calling, executes remediation actions, and stores every incident in a shared SQLite database — so each time a fault recurs, the agent responds faster and more confidently using its own history.

Built for **Qwen Cloud Hackathon — Track 1: MemoryAgent**.

## Architecture (Week 2)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Browser / Judge                                                             │
│    GET /  →  Status page (MCP calls visible, fault-injection buttons)        │
└─────────────────────┬───────────────────────────────────────────────────────┘
                      │ HTTP :80
┌─────────────────────▼───────────────────────────────────────────────────────┐
│  ops-agent  (FastAPI + background thread)                                    │
│                                                                              │
│  poll loop:                                                                  │
│    MetricsCollector ──► demo-service:8000/metrics                           │
│    AnomalyDetector  ──► symptom list                                         │
│    QwenClient ──────────────────────────────────► Qwen API (qwen3.6-flash)  │
│      tool-calling loop:                           (OpenAI-compatible)        │
│        Qwen calls search_similar_incidents ──┐                               │
│        Qwen calls get_stats           ───────┤ MCP JSON-RPC                 │
│        Qwen calls record_incident     ───────┤ Streamable HTTP              │
│                                              │                               │
└──────────────────────────────────────────────┼───────────────────────────────┘
                                               │ :8002/mcp
┌──────────────────────────────────────────────▼───────────────────────────────┐
│  incident-mcp-server  (FastMCP, Streamable HTTP)                             │
│                                                                              │
│  Tools:      search_similar_incidents  record_incident                       │
│              get_recent_incidents      get_stats                             │
│  Resources:  incidents://recent        incidents://stats                     │
│                                                                              │
│  ──► /data/incidents.db  (shared Docker volume with agent)                  │
└──────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  ops-demo  (FastAPI target)                                                  │
│    /health  /metrics  /api/data  POST /inject  POST /reset                  │
└─────────────────────────────────────────────────────────────────────────────┘

Safe-mode fallback (§4):
  MCP down  →  agent queries SQLite directly + rule-based fallback
  Qwen down →  3× retry → rule-based safe mode (alert / conditional restart)
```

## Quick Start

```bash
cp .env.example .env
# edit .env and set QWEN_API_KEY
docker compose -f deploy/docker-compose.yml up -d
```

Status page: http://localhost  
Agent API:   http://localhost/api/status

## Smoke Test

```bash
# Inject a fault
curl -X POST http://localhost/demo/inject \
  -H "Content-Type: application/json" -d '{"fault":"overload"}'

# Watch agent logs
docker compose -f deploy/docker-compose.yml logs -f agent

# View MCP tool calls from last cycle
curl http://localhost/api/mcp/calls

# Inject same fault again — Qwen finds it in memory via MCP and acts faster
curl -X POST http://localhost/demo/inject \
  -H "Content-Type: application/json" -d '{"fault":"overload"}'
```

## MCP Server Isolation Test (§1)

```bash
# Start MCP server standalone
python incident_mcp_server/main.py

# Run smoke test (separate terminal)
python incident_mcp_server/smoke_test.py
```

## Repository Layout

```
demo_service/           FastAPI target with fault injection
agent/                  Agent loop, Qwen client, SQLite memory, MCP client
incident_mcp_server/    FastMCP server exposing incident memory as MCP tools
deploy/                 Dockerfiles (demo, agent, mcp), docker-compose, ECS guide
```

## Deployment

See [deploy/aliyun_setup.md](deploy/aliyun_setup.md) for step-by-step Alibaba Cloud ECS setup.
