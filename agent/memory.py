"""
Persistent incident memory backed by SQLite.

Retrieval in Week 1 uses symptom-overlap scoring.
The `find_similar` interface is intentionally abstract so Week 2-3 can
swap in embedding-based retrieval without touching callers.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


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
        """
        Return up to `limit` past incidents ranked by symptom overlap.
        Replace body with embedding search in Week 2-3; signature stays the same.
        """
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
