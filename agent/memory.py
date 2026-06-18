"""
Persistent incident memory backed by SQLite.

Week 1: symptom-overlap scoring (find_similar)
Week 3: semantic embedding search (find_similar_semantic) — separate table,
        fully backward-compatible; old methods untouched.
Copilot: pending_actions table for human-in-the-loop approval flow.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np


@dataclass
class Incident:
    ts: str
    metrics_snapshot: dict
    symptoms: list[str]
    diagnosis: str
    action: str
    outcome: str
    resolved: bool
    id: Optional[int] = field(default=None)


@dataclass
class PendingAction:
    proposed_action: str
    reasoning: str
    symptoms: list[str]
    metrics_snapshot: dict
    created_at: str
    status: str = "pending"
    id: Optional[int] = field(default=None)


class IncidentMemory:
    def __init__(self, db_path: str = "incidents.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               TEXT    NOT NULL,
                    metrics_snapshot TEXT    NOT NULL,
                    symptoms         TEXT    NOT NULL,
                    diagnosis        TEXT,
                    action           TEXT,
                    outcome          TEXT,
                    resolved         INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON incidents(ts)")
            # Week 3: separate table keeps embeddings out of the hot incidents table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incident_embeddings (
                    incident_id INTEGER PRIMARY KEY,
                    model       TEXT    NOT NULL,
                    embedding   BLOB    NOT NULL,
                    created_at  TEXT    NOT NULL,
                    FOREIGN KEY (incident_id) REFERENCES incidents(id)
                )
            """)
            # Copilot mode: human-in-the-loop approval queue
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposed_action  TEXT    NOT NULL,
                    reasoning        TEXT,
                    symptoms         TEXT    NOT NULL,
                    metrics_snapshot TEXT    NOT NULL,
                    status           TEXT    NOT NULL DEFAULT 'pending',
                    created_at       TEXT    NOT NULL
                )
            """)

    # ── Week 1 methods (unchanged) ─────────────────────────────────────────

    def save(self, incident: Incident) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO incidents
                   (ts, metrics_snapshot, symptoms, diagnosis, action, outcome, resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident.ts,
                    json.dumps(incident.metrics_snapshot),
                    json.dumps(incident.symptoms),
                    incident.diagnosis,
                    incident.action,
                    incident.outcome,
                    1 if incident.resolved else 0,
                ),
            )
            return cur.lastrowid

    def update_outcome(self, incident_id: int, outcome: str, resolved: bool):
        with self._conn() as conn:
            conn.execute(
                "UPDATE incidents SET outcome=?, resolved=? WHERE id=?",
                (outcome, 1 if resolved else 0, incident_id),
            )

    def find_similar(self, symptoms: list[str], limit: int = 5) -> list[Incident]:
        """Week 1 text-overlap fallback — untouched."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY ts DESC LIMIT 200"
            ).fetchall()

        query_set = set(symptoms)
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            stored = set(json.loads(row["symptoms"]))
            overlap = len(query_set & stored)
            if overlap:
                scored.append((overlap, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._row_to_incident(r) for _, r in scored[:limit]]

    def get_recent(self, limit: int = 10) -> list[Incident]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_incident(r) for r in rows]

    # ── Week 3 embedding methods ───────────────────────────────────────────

    def save_embedding(self, incident_id: int, model: str, vector: np.ndarray) -> None:
        """Persist a unit-normalised float32 vector for an incident. Idempotent."""
        blob = vector.astype(np.float32).tobytes()
        now  = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO incident_embeddings
                   (incident_id, model, embedding, created_at)
                   VALUES (?, ?, ?, ?)""",
                (incident_id, model, blob, now),
            )

    def find_similar_semantic(
        self,
        query_vec: np.ndarray,
        limit: int = 5,
    ) -> list[tuple[Incident, float]]:
        """
        Cosine similarity search over stored embeddings.
        query_vec must be unit-normalised (EmbeddingClient.embed() guarantees this).
        Returns [(Incident, score)] sorted by score desc, or [] if no embeddings stored.
        """
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT i.id, i.ts, i.metrics_snapshot, i.symptoms,
                       i.diagnosis, i.action, i.outcome, i.resolved,
                       e.embedding
                FROM   incidents i
                JOIN   incident_embeddings e ON i.id = e.incident_id
                ORDER  BY i.ts DESC
                LIMIT  1000
            """).fetchall()

        if not rows:
            return []

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            stored = np.frombuffer(bytes(row["embedding"]), dtype=np.float32)
            # Both vectors are unit-normalised → dot product = cosine similarity
            score = float(np.dot(query_vec, stored))
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(self._row_to_incident(r), s) for s, r in scored[:limit]]

    def get_incidents_without_embeddings(self) -> list[Incident]:
        """Return incidents that have no stored embedding — for migration script."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT i.* FROM incidents i
                LEFT  JOIN incident_embeddings e ON i.id = e.incident_id
                WHERE e.incident_id IS NULL
                ORDER BY i.ts ASC
            """).fetchall()
        return [self._row_to_incident(r) for r in rows]

    def embedding_coverage(self) -> tuple[int, int]:
        """Return (embedded_count, total_count) for observability."""
        with self._conn() as conn:
            total    = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            embedded = conn.execute("SELECT COUNT(*) FROM incident_embeddings").fetchone()[0]
        return embedded, total

    # ── Copilot / pending-action methods ─────────────────────────────────

    def save_pending_action(
        self,
        proposed_action: str,
        reasoning: str,
        symptoms: list[str],
        metrics_snapshot: dict,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO pending_actions
                   (proposed_action, reasoning, symptoms, metrics_snapshot, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (proposed_action, reasoning,
                 json.dumps(symptoms), json.dumps(metrics_snapshot), now),
            )
            return cur.lastrowid

    def get_pending_actions(self, status: str = "pending") -> list[PendingAction]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE status=? ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        return [self._row_to_pending(r) for r in rows]

    def get_pending_action(self, pending_id: int) -> Optional[PendingAction]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE id=?", (pending_id,)
            ).fetchone()
        return self._row_to_pending(row) if row else None

    def update_pending_status(self, pending_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE pending_actions SET status=? WHERE id=?",
                (status, pending_id),
            )

    def has_pending_for_symptoms(self, symptoms: list[str]) -> bool:
        """True if there is already an unresolved pending action with the same symptom set."""
        key = json.dumps(sorted(symptoms))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symptoms FROM pending_actions WHERE status='pending'"
            ).fetchall()
        return any(json.dumps(sorted(json.loads(r["symptoms"]))) == key for r in rows)

    # ── helpers ───────────────────────────────────────────────────────────

    def _row_to_pending(self, row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            id=row["id"],
            proposed_action=row["proposed_action"],
            reasoning=row["reasoning"] or "",
            symptoms=json.loads(row["symptoms"]),
            metrics_snapshot=json.loads(row["metrics_snapshot"]),
            status=row["status"],
            created_at=row["created_at"],
        )

    def _row_to_incident(self, row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            ts=row["ts"],
            metrics_snapshot=json.loads(row["metrics_snapshot"]),
            symptoms=json.loads(row["symptoms"]),
            diagnosis=row["diagnosis"] or "",
            action=row["action"] or "",
            outcome=row["outcome"] or "",
            resolved=bool(row["resolved"]),
        )
