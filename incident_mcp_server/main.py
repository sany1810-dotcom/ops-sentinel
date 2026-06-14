"""
Incident Memory MCP Server  (Week 3: semantic embedding search)

Exposes 4 tools and 2 resources via Streamable HTTP on port 8002.

Week 3 additions:
  - search_similar_incidents uses Qwen text-embedding-v3 (cosine similarity)
  - record_incident computes and stores embedding on every write
  - get_stats includes embedding coverage + semantic_search flag
  - Graceful fallback to text-overlap search when embedding API is down (§5)
"""
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_log = logging.getLogger("mcp.search")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

# Works locally (agent/ sibling dir) and in Docker (memory.py + embedding_client.py copied here)
_agent_dir = Path(__file__).parent.parent / "agent"
if _agent_dir.exists():
    sys.path.insert(0, str(_agent_dir))
else:
    sys.path.insert(0, str(Path(__file__).parent))

from memory import Incident, IncidentMemory          # noqa: E402
from embedding_client import EmbeddingClient, build_embed_text  # noqa: E402

from mcp.server.fastmcp import FastMCP

# ── config ─────────────────────────────────────────────────────────────────
DB_PATH    = os.getenv("AGENT_DB_PATH",   str(Path(__file__).parent.parent / "agent" / "incidents.db"))
_MCP_PORT  = int(os.getenv("MCP_PORT",    "8002"))
_API_KEY   = os.getenv("QWEN_API_KEY",    "").strip()
_BASE_URL  = os.getenv("QWEN_BASE_URL",   "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").strip()
_EMB_MODEL = os.getenv("QWEN_EMBED_MODEL","text-embedding-v3")

_memory   = IncidentMemory(db_path=DB_PATH)
_embedder = EmbeddingClient(_API_KEY, _BASE_URL, _EMB_MODEL) if _API_KEY else None
_log.info("EmbeddingClient init: key_set=%s model=%s available=%s",
          bool(_API_KEY), _EMB_MODEL,
          _embedder.available if _embedder else "N/A (no key)")

mcp = FastMCP("incident-memory", host="0.0.0.0", port=_MCP_PORT)


# ── helpers ────────────────────────────────────────────────────────────────

def _to_dict(inc: Incident) -> dict:
    return {
        "id":               inc.id,
        "ts":               inc.ts,
        "symptoms":         inc.symptoms,
        "metrics_snapshot": inc.metrics_snapshot,
        "diagnosis":        inc.diagnosis,
        "action":           inc.action,
        "outcome":          inc.outcome,
        "resolved":         inc.resolved,
    }


def _compute_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total    = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM incidents WHERE resolved=1").fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM incident_embeddings").fetchone()[0]
        rows     = conn.execute(
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
        "total":              total,
        "resolved":           resolved,
        "unresolved":         total - resolved,
        "top_symptoms":       [{"symptom": s, "count": c} for s, c in top],
        "embedded":           embedded,
        "embedding_coverage": round(embedded / total, 2) if total else 0,
        "semantic_search":    bool(_embedder and _embedder.available),
    }


# ── tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def search_similar_incidents(
    symptoms: list[str],
    limit: int = 5,
    metrics: dict | None = None,
) -> list[dict]:
    """
    Find past incidents by semantic similarity (Qwen text-embedding-v3, cosine).
    Falls back to text-overlap search if embedding API is unavailable.
    Each result includes similarity_score (0.0-1.0) and search_mode.
    """
    if _embedder is None:
        _log.warning("semantic search skipped: no API key")
    else:
        # Don't gate on _embedder.available — flag sticks False after any transient
        # startup error and never recovers. Let embed() self-recover (it resets
        # available=True on success). Log every path so we can see why it fell back.
        query_text = build_embed_text(symptoms, metrics or {})
        _log.info("embedding query: %r (symptoms=%s)", query_text[:120], symptoms)
        query_vec = _embedder.embed(query_text)
        if query_vec is None:
            _log.warning("semantic search failed: embed() returned None "
                         "(embedder.available=%s)", _embedder.available)
        else:
            results = _memory.find_similar_semantic(query_vec, limit=limit)
            _log.info("semantic results: %d found (coverage=%s)",
                      len(results), _memory.embedding_coverage())
            if results:
                return [
                    {
                        **_to_dict(inc),
                        "similarity_score": round(score, 3),
                        "search_mode":      "semantic",
                    }
                    for inc, score in results
                ]
            _log.warning("semantic search: embed OK but 0 results — "
                         "DB has no embeddings yet?")
    # §5 fallback
    _log.info("falling back to text-overlap search")
    return [
        {**_to_dict(i), "similarity_score": None, "search_mode": "text_fallback"}
        for i in _memory.find_similar(symptoms, limit=limit)
    ]


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
    """Persist a new incident and compute its embedding for future semantic search."""
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

    embedded = False
    if _embedder and _embedder.available:
        text = build_embed_text(symptoms, metrics_snapshot, diagnosis)
        vec  = _embedder.embed(text)
        if vec is not None:
            _memory.save_embedding(incident_id, _embedder._model, vec)
            embedded = True

    return {"id": incident_id, "status": "recorded", "embedded": embedded}


@mcp.tool()
def get_recent_incidents(n: int = 10) -> list[dict]:
    """Return the N most recent incidents from memory (newest first)."""
    return [_to_dict(i) for i in _memory.get_recent(n)]


@mcp.tool()
def get_stats() -> dict:
    """Return aggregate stats including embedding coverage and semantic_search flag."""
    return _compute_stats()


# ── resources ──────────────────────────────────────────────────────────────

@mcp.resource("incidents://recent")
def resource_recent() -> str:
    return json.dumps([_to_dict(i) for i in _memory.get_recent(10)], indent=2)


@mcp.resource("incidents://stats")
def resource_stats() -> str:
    return json.dumps(_compute_stats(), indent=2)


# ── entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    emb_status = f"embedding={_EMB_MODEL}" if (_embedder and _embedder.available) else "embedding=OFF"
    print(f"Starting incident-memory MCP server on 0.0.0.0:{_MCP_PORT}/mcp  [{emb_status}]")
    mcp.run(transport="streamable-http")
