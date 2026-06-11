"""
Ops-Sentinel Agent
==================
Background thread: poll → detect → retrieve memory → call Qwen → act → store
FastAPI server:    status page (HTML) + JSON API for judges
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from actions import ActionExecutor
from collector import MetricsCollector
from detector import AnomalyDetector
from memory import Incident, IncidentMemory
from qwen_client import QwenClient

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
QWEN_API_KEY   = os.environ["QWEN_API_KEY"]
QWEN_BASE_URL  = os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL     = os.environ.get("QWEN_MODEL", "qwen3.6-flash")
DEMO_URL       = os.environ.get("DEMO_SERVICE_URL", "http://localhost:8000")
POLL_INTERVAL  = int(os.environ.get("AGENT_POLL_INTERVAL", "10"))
DB_PATH        = os.environ.get("AGENT_DB_PATH", "incidents.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("ops-sentinel")

# ---------------------------------------------------------------------------
# Shared state (written by agent thread, read by FastAPI handlers)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_agent_state = {
    "status": "starting",   # starting | healthy | anomaly | halted
    "safe_mode": False,
    "last_check": None,
    "last_metrics": {},
    "last_symptoms": [],
    "last_incident_id": None,
    "total_incidents": 0,
    "uptime_start": datetime.now(timezone.utc).isoformat(),
}


def _update_state(**kwargs):
    with _state_lock:
        _agent_state.update(kwargs)


def _get_state() -> dict:
    with _state_lock:
        return dict(_agent_state)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def _agent_loop(
    collector: MetricsCollector,
    detector: AnomalyDetector,
    memory: IncidentMemory,
    qwen: QwenClient,
    executor: ActionExecutor,
):
    logger.info("Agent loop started (poll every %ds)", POLL_INTERVAL)
    _update_state(status="healthy")

    while True:
        if executor.halted:
            _update_state(status="halted")
            logger.critical("Agent halted. Sleeping forever.")
            time.sleep(3600)
            continue

        try:
            _tick(collector, detector, memory, qwen, executor)
        except Exception as exc:
            logger.exception("Unhandled error in agent tick: %s", exc)

        time.sleep(POLL_INTERVAL)


def _tick(
    collector: MetricsCollector,
    detector: AnomalyDetector,
    memory: IncidentMemory,
    qwen: QwenClient,
    executor: ActionExecutor,
):
    snap = collector.collect()
    now = datetime.now(timezone.utc).isoformat()

    metrics_dict = {
        "rss_mb": snap.rss_mb,
        "latency_ms": snap.latency_ms,
        "error_rate": snap.error_rate,
        "fault": snap.fault,
        "reachable": snap.reachable,
    }
    _update_state(last_check=now, last_metrics=metrics_dict)

    result = detector.detect(snap)
    _update_state(last_symptoms=result.symptoms)

    if not result.is_anomaly:
        _update_state(status="healthy", safe_mode=qwen.degraded)
        logger.debug("No anomaly. rss=%.1fMB lat=%.0fms err=%.2f",
                     snap.rss_mb, snap.latency_ms, snap.error_rate)
        return

    logger.warning("ANOMALY detected — symptoms: %s  severity: %s",
                   result.symptoms, result.severity)
    _update_state(status="anomaly")

    # ---- Memory retrieval ----
    similar = memory.find_similar(result.symptoms)
    if similar:
        logger.info("Found %d similar past incident(s) in memory", len(similar))
        for inc in similar[:2]:
            logger.info("  past#%d symptoms=%s action=%s resolved=%s",
                        inc.id, inc.symptoms, inc.action, inc.resolved)
    else:
        logger.info("No similar incidents in memory — fresh case")

    # ---- LLM diagnosis ----
    diagnosis = qwen.diagnose(metrics_dict, result.symptoms, similar)
    _update_state(safe_mode=diagnosis.get("safe_mode", False))

    action   = diagnosis.get("action", "alert")
    reasoning = diagnosis.get("reasoning", "")
    confidence = diagnosis.get("confidence", 0.0)

    logger.info("Qwen diagnosis: %s | action=%s confidence=%.2f safe_mode=%s",
                diagnosis.get("diagnosis"), action, confidence,
                diagnosis.get("safe_mode"))

    # ---- Execute ----
    outcome = executor.execute(action, diagnosis.get("diagnosis", ""))

    # ---- Store in memory ----
    inc = Incident(
        ts=now,
        metrics_snapshot=metrics_dict,
        symptoms=result.symptoms,
        diagnosis=diagnosis.get("diagnosis", ""),
        action=action,
        outcome=outcome,
        resolved=(outcome in {"restarted", "halted"}),
    )
    inc_id = memory.save(inc)
    _update_state(
        last_incident_id=inc_id,
        total_incidents=_agent_state["total_incidents"] + 1,
    )
    logger.info("Incident #%d saved to memory (outcome=%s)", inc_id, outcome)


# ---------------------------------------------------------------------------
# FastAPI status page
# ---------------------------------------------------------------------------
app = FastAPI(title="Ops-Sentinel Status")

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Ops-Sentinel Status</title>
<style>
  body {{ font-family: monospace; background:#0d1117; color:#c9d1d9; margin:2rem; }}
  h1 {{ color:#58a6ff; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:4px; font-weight:bold; }}
  .healthy {{ background:#1f6b2e; color:#56d364; }}
  .anomaly {{ background:#6e3513; color:#f0883e; }}
  .halted  {{ background:#6e1313; color:#f85149; }}
  .safe    {{ background:#5a3e00; color:#e3b341; }}
  table {{ border-collapse:collapse; width:100%; margin-top:1rem; }}
  th,td {{ border:1px solid #30363d; padding:6px 10px; text-align:left; }}
  th {{ background:#161b22; color:#8b949e; }}
  tr:hover {{ background:#161b22; }}
  .ts {{ color:#8b949e; font-size:0.85em; }}
</style>
</head>
<body>
<h1>Ops-Sentinel</h1>
<p>Status: <span class="badge {status_cls}">{status}</span>
{safe_badge}
&nbsp; Last check: <span class="ts">{last_check}</span></p>

<h2>Current Metrics</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
{metrics_rows}
</table>

<h2>Recent Incidents (last 10)</h2>
<table>
<tr><th>#</th><th>Time</th><th>Symptoms</th><th>Action</th><th>Outcome</th><th>Resolved</th></tr>
{incident_rows}
</table>
<p style="color:#484f58;font-size:0.8em">Auto-refreshes every 10s &mdash; Qwen Cloud Hackathon Track 1: MemoryAgent</p>
</body>
</html>"""


def _render_status(state: dict, incidents) -> str:
    status = state.get("status", "unknown")
    status_cls = status if status in {"healthy", "anomaly", "halted"} else "anomaly"
    safe_badge = (
        '<span class="badge safe">SAFE MODE</span>'
        if state.get("safe_mode")
        else ""
    )

    metrics = state.get("last_metrics", {})
    metrics_rows = "\n".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in metrics.items()
    )

    incident_rows_parts = []
    for inc in incidents:
        resolved = "✓" if inc.resolved else "✗"
        symptoms_str = ", ".join(inc.symptoms)
        incident_rows_parts.append(
            f"<tr><td>{inc.id}</td>"
            f"<td class='ts'>{inc.ts[:19]}</td>"
            f"<td>{symptoms_str}</td>"
            f"<td>{inc.action}</td>"
            f"<td>{inc.outcome}</td>"
            f"<td>{resolved}</td></tr>"
        )
    incident_rows = "\n".join(incident_rows_parts) or "<tr><td colspan=6>No incidents yet</td></tr>"

    return _HTML_TEMPLATE.format(
        status=status.upper(),
        status_cls=status_cls,
        safe_badge=safe_badge,
        last_check=state.get("last_check") or "—",
        metrics_rows=metrics_rows or "<tr><td colspan=2>No data yet</td></tr>",
        incident_rows=incident_rows,
    )


@app.get("/", response_class=HTMLResponse)
async def status_page():
    state = _get_state()
    incidents = _memory.get_recent(10)
    return _render_status(state, incidents)


@app.get("/api/status")
async def api_status():
    return JSONResponse(_get_state())


@app.get("/api/incidents")
async def api_incidents():
    incidents = _memory.get_recent(20)
    return JSONResponse([
        {
            "id": i.id, "ts": i.ts, "symptoms": i.symptoms,
            "diagnosis": i.diagnosis, "action": i.action,
            "outcome": i.outcome, "resolved": i.resolved,
        }
        for i in incidents
    ])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
_memory: IncidentMemory  # assigned before thread start


def main():
    global _memory
    _memory   = IncidentMemory(DB_PATH)
    collector = MetricsCollector(DEMO_URL)
    detector  = AnomalyDetector()
    qwen      = QwenClient(QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL)
    executor  = ActionExecutor(collector)

    t = threading.Thread(
        target=_agent_loop,
        args=(collector, detector, _memory, qwen, executor),
        daemon=True,
        name="agent-loop",
    )
    t.start()

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
