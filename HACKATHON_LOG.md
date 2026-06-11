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

**Next:**
- Obtain Alibaba Cloud ECS instance, deploy containers, get public URL
- Run smoke test: inject fault → agent detects → Qwen diagnoses → action + memory write → re-inject → agent uses memory
- Polish status page HTML

---
