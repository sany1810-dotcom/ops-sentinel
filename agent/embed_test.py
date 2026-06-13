"""
§7 local test — semantic retrieval beats text-overlap.

Three seed incidents with DISTINCT fault types are recorded.
Queries are phrased with ZERO keyword overlap with the matching incident's
symptoms — a text search would return nothing; embedding search must win.

Run from the agent/ directory:
    python embed_test.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from memory import IncidentMemory, Incident
from embedding_client import EmbeddingClient, build_embed_text

DB      = "/tmp/ops_sentinel_embed_test.db"
API_KEY  = os.environ["QWEN_API_KEY"].strip()
BASE_URL = os.environ.get(
    "QWEN_BASE_URL",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
).strip()

mem = IncidentMemory(DB)
emb = EmbeddingClient(API_KEY, BASE_URL)

if not emb.available:
    print("ABORT: embedding API not reachable")
    sys.exit(1)

# ── seed three incidents with distinct fault patterns ──────────────────────
SEEDS = [
    dict(
        label     = "A:overload",
        symptoms  = ["high_latency", "high_cpu"],
        metrics   = {"latency_ms": 2800, "cpu_pct": 94, "fault": "overload"},
        diagnosis = "Service overloaded: CPU saturated, request queue backing up",
        action    = "restart", outcome="restarted", resolved=True,
    ),
    dict(
        label     = "B:memory_leak",
        symptoms  = ["high_rss"],
        metrics   = {"rss_mb": 680, "fault": "memory_leak"},
        diagnosis = "Memory leak: RSS growing unbounded, garbage collector cannot reclaim",
        action    = "restart", outcome="restarted", resolved=True,
    ),
    dict(
        label     = "C:dep_down",
        symptoms  = ["dependency_error", "timeout"],
        metrics   = {"error_rate": 0.9, "fault": "dependency_down"},
        diagnosis = "Downstream dependency unreachable: connection timeout on every call",
        action    = "alert", outcome="alerted", resolved=False,
    ),
]

print("Recording seed incidents and embedding them...")
ids: dict[str, int] = {}
for s in SEEDS:
    ts  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    inc = Incident(
        ts=ts, symptoms=s["symptoms"], metrics_snapshot=s["metrics"],
        diagnosis=s["diagnosis"], action=s["action"],
        outcome=s["outcome"], resolved=s["resolved"],
    )
    inc_id = mem.save(inc)
    text   = build_embed_text(inc.symptoms, inc.metrics_snapshot, inc.diagnosis)
    vec    = emb.embed(text)
    mem.save_embedding(inc_id, emb._model, vec)
    ids[s["label"]] = inc_id
    print(f"  id={inc_id}  {s['label']}  symptoms={s['symptoms']}")

print()

# ── semantic queries — zero keyword overlap with target symptoms ────────────
#
# "slow responses under load"       → NO overlap with high_latency / high_cpu
# "process consuming too much RAM"  → NO overlap with high_rss
# "cannot reach downstream service" → NO overlap with dependency_error / timeout
#
QUERIES = [
    ("slow responses under load",           "A:overload"),
    ("process consuming too much RAM",       "B:memory_leak"),
    ("cannot reach downstream service",      "C:dep_down"),
]

print("=== Semantic search (zero keyword overlap with stored symptoms) ===\n")
all_pass = True
for query_text, expected_label in QUERIES:
    qvec    = emb.embed(query_text)
    results = mem.find_similar_semantic(qvec, limit=3)

    top_inc, top_score = results[0] if results else (None, 0.0)
    expected_id        = ids[expected_label]
    ok                 = top_inc is not None and top_inc.id == expected_id

    if not ok:
        all_pass = False

    print(f'Query:    "{query_text}"')
    print(f"Expected: id={expected_id} ({expected_label})")
    print(f"Got:      id={getattr(top_inc,'id',None)}  score={top_score:.3f}  "
          f"{'PASS' if ok else '*** FAIL ***'}")
    for inc, score in results[:3]:
        lbl = next((s["label"] for s in SEEDS if ids.get(s["label"]) == inc.id), "?")
        print(f"  [{score:.3f}] id={inc.id} {lbl}  symptoms={inc.symptoms}")

    # Text-overlap baseline (expected: 0 results since no keyword matches)
    text_hits = mem.find_similar(query_text.split(), limit=3)
    print(f"  Text-overlap baseline: {len(text_hits)} hit(s) "
          f"(0 expected — no keyword match)")
    print()

print("RESULT:", "ALL PASS" if all_pass else "SOME TESTS FAILED")
