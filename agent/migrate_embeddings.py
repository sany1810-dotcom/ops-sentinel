"""
Backfill embeddings for existing incidents that were recorded before Week 3.
Idempotent: skips incidents that already have an embedding.

Usage:
    cd agent/
    python migrate_embeddings.py [--db /path/to/incidents.db]
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from memory import IncidentMemory
from embedding_client import EmbeddingClient, build_embed_text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("AGENT_DB_PATH", "incidents.db"))
    args = ap.parse_args()

    api_key  = os.getenv("QWEN_API_KEY", "").strip()
    base_url = os.getenv("QWEN_BASE_URL",
                         "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").strip()
    model    = os.getenv("QWEN_EMBED_MODEL", "text-embedding-v3").strip()

    if not api_key:
        print("ERROR: QWEN_API_KEY not set")
        sys.exit(1)

    memory   = IncidentMemory(args.db)
    embedder = EmbeddingClient(api_key, base_url, model)

    pending = memory.get_incidents_without_embeddings()
    if not pending:
        print("All incidents already have embeddings. Nothing to do.")
        return

    print(f"Backfilling embeddings for {len(pending)} incident(s) using {model}...")
    ok = skip = 0
    for inc in pending:
        text = build_embed_text(inc.symptoms, inc.metrics_snapshot, inc.diagnosis)
        vec  = embedder.embed(text)
        if vec is not None:
            memory.save_embedding(inc.id, model, vec)
            ok += 1
            print(f"  [OK   {ok:>4}/{len(pending)}] id={inc.id}  {inc.symptoms}")
        else:
            skip += 1
            print(f"  [SKIP {skip:>4}      ] id={inc.id}  — embedding API unavailable")
        time.sleep(0.05)   # gentle rate-limit

    embedded, total = memory.embedding_coverage()
    print(f"\nDone: {ok} embedded, {skip} skipped  |  coverage: {embedded}/{total}")


if __name__ == "__main__":
    main()
