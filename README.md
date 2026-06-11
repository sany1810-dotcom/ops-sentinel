# Ops-Sentinel

Ops-Sentinel is an autonomous on-call agent with persistent incident memory. It monitors a target service, detects anomalies, consults Qwen LLM for diagnosis, executes remediation actions, and stores every incident in a local SQLite database — so each time a fault recurs, the agent responds faster and more confidently using its own history.

Built for **Qwen Cloud Hackathon — Track 1: MemoryAgent**.

## Quick Start

```bash
cp .env.example .env
# edit .env and set QWEN_API_KEY
docker compose -f deploy/docker-compose.yml up -d
```

Status page: http://localhost:8001  
Demo service: http://localhost:8000/health

## Smoke Test

```bash
# Inject a fault
curl -X POST http://localhost:8000/inject -H "Content-Type: application/json" -d '{"fault":"overload"}'

# Watch agent logs
docker compose -f deploy/docker-compose.yml logs -f agent

# Inject the same fault again — agent finds it in memory and acts faster
curl -X POST http://localhost:8000/inject -H "Content-Type: application/json" -d '{"fault":"overload"}'
```

## Architecture

```
demo_service/   FastAPI target with fault injection (/inject, /reset)
agent/          Polling loop + Qwen LLM client + SQLite memory + status page
deploy/         Dockerfiles, docker-compose, Alibaba Cloud setup guide
```

## Deployment

See [deploy/aliyun_setup.md](deploy/aliyun_setup.md) for step-by-step Alibaba Cloud ECS setup.
