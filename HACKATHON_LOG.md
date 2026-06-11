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
- Obtain Alibaba Cloud ECS instance, deploy containers, get public URL
- Update DEMO_SERVICE_URL to use service hostname in docker-compose (already done)
- Polish status page HTML

---
