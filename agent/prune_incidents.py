"""
Prune duplicate incidents, keeping the N most recent per fault type.
Idempotent: safe to run multiple times (already-pruned DB → nothing to delete).
Creates a timestamped backup before any deletions.

Usage (in container):
    python prune_incidents.py [--db /data/incidents.db] [--keep 20] [--dry-run]
"""
import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB   = os.getenv("AGENT_DB_PATH", "/data/incidents.db")
DEFAULT_KEEP = 20


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",      default=DEFAULT_DB)
    ap.add_argument("--keep",    type=int, default=DEFAULT_KEEP,
                    help="Max incidents to keep per fault type (default 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be deleted without touching the DB")
    args = ap.parse_args()

    # ── Backup ────────────────────────────────────────────────────────────────
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = f"{args.db}.bak.{ts}"
    shutil.copy2(args.db, bak)
    print(f"Backup: {bak}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # ── Distribution before ───────────────────────────────────────────────────
    dist = conn.execute("""
        SELECT json_extract(metrics_snapshot, '$.fault') AS fault,
               COUNT(*) AS cnt
        FROM   incidents
        GROUP  BY fault
        ORDER  BY cnt DESC
    """).fetchall()

    total_before = sum(r["cnt"] for r in dist)
    print(f"\nBefore: {total_before} incidents")
    for r in dist:
        label = r["fault"] or "(null)"
        print(f"  {label:28s} {r['cnt']:>6}")

    # ── Collect IDs to delete (keep N most recent per fault type) ─────────────
    to_delete: list[int] = []
    will_keep = 0

    for r in dist:
        fault = r["fault"]
        ids = [
            row["id"]
            for row in conn.execute(
                """SELECT id FROM incidents
                   WHERE  json_extract(metrics_snapshot, '$.fault') IS ?
                   ORDER  BY ts DESC""",
                (fault,),
            )
        ]
        keep_ids   = ids[:args.keep]
        delete_ids = ids[args.keep:]
        will_keep += len(keep_ids)
        to_delete.extend(delete_ids)

    print(f"\nKeeping {args.keep} per type → {will_keep} survive, {len(to_delete)} deleted")

    if not to_delete:
        print("Nothing to delete — already clean.")
        conn.close()
        return

    if args.dry_run:
        print(f"[dry-run] First 10 IDs that would be deleted: {to_delete[:10]}")
        conn.close()
        return

    # ── Delete (embeddings FK first, then incidents) ──────────────────────────
    ph = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM incident_embeddings WHERE incident_id IN ({ph})", to_delete)
    conn.execute(f"DELETE FROM incidents            WHERE id           IN ({ph})", to_delete)
    conn.execute("VACUUM")
    conn.commit()

    total_after = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    emb_after   = conn.execute("SELECT COUNT(*) FROM incident_embeddings").fetchone()[0]
    conn.close()

    print(f"\nAfter: {total_after} incidents, {emb_after} embeddings")
    print(f"Backup at: {bak}")


if __name__ == "__main__":
    main()
