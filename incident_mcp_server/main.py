"""
Incident Memory MCP Server  (Week 2, §1)

Exposes 4 tools and 2 resources via Streamable HTTP transport on port 8002.
Reuses agent/memory.py — zero duplication.

Tools:
  search_similar_incidents  – symptom-overlap retrieval
  record_incident           – persist a new incident
  get_recent_incidents      – last N incidents
  get_stats                 – aggregate counts + top symptoms

Resources:
  incidents://recent        – JSON of last 10 incidents
  incidents://stats         – JSON of aggregate stats
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

# Works locally (agent/ sibling dir) and in Docker (memory.py copied next to main.py)
_agent_dir = Path(__file__).parent.parent / "agent"
if _agent_dir.exists():
    sys.path.insert(0, str(_agent_dir))
else:
    sys.path.insert(0, str(Path(__file__).parent))
from memory import Incident, IncidentMemory  # noqa: E402

from mcp.server.fastmcp import FastMCP

DB_PATH = os.getenv("AGENT_DB_PATH", str(Path(__file__).parent.parent / "agent" / "incidents.db"))
_MCP_PORT = int(os.getenv("MCP_PORT", "8002"))

_memory = IncidentMemory(db_path=DB_PATH)

mcp = FastMCP("incident-memory", host="0.0.0.0", port=_MCP_PORT)


# ── helpers ────────────────────────────────────────────────────────────────

def _to_dict(inc: Incident) -> dict:
    return {
        "id": inc.id,
        "ts": inc.ts,
        "symptoms": inc.symptoms,
        "metrics_snapshot": inc.metrics_snapshot,
        "diagnosis": inc.diagnosis,
        "action": inc.action,
        "outcome": inc.outcome,
        "resolved": inc.resolved,
    }


def _compute_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE resolved=1"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT symptoms FROM incidents ORDER BY ts DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    freq: dict[str, int] = {}
    for row in rows:
        for s in json.loads(row["symptoms"]):
            freq[s] = freq.get(s, 0) + 1

    top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "total": total,
        "resolved": resolved,
        "unresolved": total - resolved,
        "top_symptoms": [{"symptom": s, "count": c} for s, c in top],
    }


# ── tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def search_similar_incidents(symptoms: list[str], limit: int = 5) -> list[dict]:
    """Find past incidents ranked by symptom overlap with the given symptoms list."""
    return [_to_dict(i) for i in _memory.find_similar(symptoms, limit=limit)]


@mcp.tool()
def record_incident(
    ts: str,
    symptoms: list[str],
    metrics_snapshot: dict,
    diagnosis: str,
    action: str,
    outcome: str,
    resolved: bool,
) -> dict:
    """Persist a new incident to the memory store and return its assigned id."""
    inc = Incident(
        ts=ts,
        symptoms=symptoms,
        metrics_snapshot=metrics_snapshot,
        diagnosis=diagnosis,
        action=action,
        outcome=outcome,
        resolved=resolved,
    )
    incident_id = _memory.save(inc)
    return {"id": incident_id, "status": "recorded"}


@mcp.tool()
def get_recent_incidents(n: int = 10) -> list[dict]:
    """Return the N most recent incidents from memory (newest first)."""
    return [_to_dict(i) for i in _memory.get_recent(n)]


@mcp.tool()
def get_stats() -> dict:
    """Return aggregate statistics: total, resolved, unresolved, top 5 symptoms."""
    return _compute_stats()


# ── resources ──────────────────────────────────────────────────────────────

@mcp.resource("incidents://recent")
def resource_recent() -> str:
    """Last 10 incidents as a JSON array."""
    return json.dumps([_to_dict(i) for i in _memory.get_recent(10)], indent=2)


@mcp.resource("incidents://stats")
def resource_stats() -> str:
    """Aggregate incident statistics as JSON."""
    return json.dumps(_compute_stats(), indent=2)


# ── entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Starting incident-memory MCP server on 0.0.0.0:{_MCP_PORT}/mcp")
    mcp.run(transport="streamable-http")
