"""
Ops-Sentinel Agent  (Week 2: MCP + Qwen tool-calling)
======================================================
Background thread:
  poll → detect → [MCP] Qwen calls tools → act → [MCP] record → store

FastAPI server:  status page + JSON API + demo fault-injection proxy

Week 1 behaviour is preserved as safe-mode fallback when MCP server is down.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from actions import ActionExecutor
from collector import MetricsCollector, _TRANSPORT
from detector import AnomalyDetector
from mcp_client import IncidentMCPClient
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
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8002/mcp")

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
    "status": "starting",
    "safe_mode": False,
    "mcp_available": False,
    "last_check": None,
    "last_metrics": {},
    "last_symptoms": [],
    "last_incident_id": None,
    "last_mcp_calls": [],   # [{tool, args, result}, …] from most recent cycle
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
    mcp: IncidentMCPClient,
):
    logger.info("Agent loop started (poll every %ds)", POLL_INTERVAL)
    _update_state(status="healthy", mcp_available=mcp.available)

    while True:
        if executor.halted:
            _update_state(status="halted")
            logger.critical("Agent halted. Sleeping forever.")
            time.sleep(3600)
            continue

        try:
            _tick(collector, detector, memory, qwen, executor, mcp)
        except Exception as exc:
            logger.exception("Unhandled error in agent tick: %s", exc)

        time.sleep(POLL_INTERVAL)


def _tick(
    collector: MetricsCollector,
    detector: AnomalyDetector,
    memory: IncidentMemory,
    qwen: QwenClient,
    executor: ActionExecutor,
    mcp: IncidentMCPClient,
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
        _update_state(status="healthy", safe_mode=qwen.degraded,
                      mcp_available=mcp.available)
        logger.debug("No anomaly. rss=%.1fMB lat=%.0fms err=%.2f",
                     snap.rss_mb, snap.latency_ms, snap.error_rate)
        return

    logger.warning("ANOMALY detected — symptoms: %s  severity: %s",
                   result.symptoms, result.severity)
    _update_state(status="anomaly")

    # ── Diagnosis: try MCP agentic loop, fall back to Week 1 ───────────────
    tool_log: list[dict] = []

    if mcp.available:
        logger.info("Using MCP agentic loop (Qwen + tool-calling)")
        try:
            diagnosis, tool_log = qwen.diagnose_with_mcp(metrics_dict, result.symptoms, mcp)
        except Exception as exc:
            logger.warning("MCP agentic loop failed (%s) — falling back to Week 1", exc)
            mcp.available = False
            diagnosis = _week1_diagnose(memory, qwen, metrics_dict, result.symptoms)
    else:
        # Attempt reconnect every 5 minutes (300s / POLL_INTERVAL ticks)
        logger.info("MCP unavailable — using Week 1 direct memory path")
        diagnosis = _week1_diagnose(memory, qwen, metrics_dict, result.symptoms)
        # Non-blocking reconnect attempt in background
        threading.Thread(target=mcp.reconnect, daemon=True).start()

    _update_state(
        safe_mode=diagnosis.get("safe_mode", False),
        mcp_available=mcp.available,
        last_mcp_calls=tool_log,
    )

    action     = diagnosis.get("action", "alert")
    confidence = diagnosis.get("confidence", 0.0)

    logger.info("Diagnosis: %s | action=%s confidence=%.2f mcp=%s",
                diagnosis.get("diagnosis"), action, confidence, mcp.available)

    outcome = executor.execute(action, diagnosis.get("diagnosis", ""))

    # ── Record incident (via MCP if available, else direct) ────────────────
    inc = Incident(
        ts=now,
        metrics_snapshot=metrics_dict,
        symptoms=result.symptoms,
        diagnosis=diagnosis.get("diagnosis", ""),
        action=action,
        outcome=outcome,
        resolved=(outcome in {"restarted", "halted"}),
    )

    if mcp.available:
        try:
            rec = mcp.call_tool("record_incident", {
                "ts": inc.ts,
                "symptoms": inc.symptoms,
                "metrics_snapshot": inc.metrics_snapshot,
                "diagnosis": inc.diagnosis,
                "action": inc.action,
                "outcome": inc.outcome,
                "resolved": inc.resolved,
            })
            inc_id = rec.get("id") if isinstance(rec, dict) else None
            logger.info("Incident recorded via MCP → id=%s (outcome=%s)", inc_id, outcome)
        except Exception as exc:
            logger.warning("MCP record_incident failed (%s) — saving directly", exc)
            inc_id = memory.save(inc)
    else:
        inc_id = memory.save(inc)
        logger.info("Incident #%d saved to memory directly (outcome=%s)", inc_id, outcome)

    _update_state(
        last_incident_id=inc_id,
        total_incidents=_agent_state["total_incidents"] + 1,
    )


def _week1_diagnose(
    memory: IncidentMemory,
    qwen: QwenClient,
    metrics_dict: dict,
    symptoms: list[str],
) -> dict:
    """Week 1 path: query SQLite directly, call Qwen with pre-fetched context."""
    similar = memory.find_similar(symptoms)
    if similar:
        logger.info("Found %d similar past incident(s) in memory", len(similar))
    else:
        logger.info("No similar incidents in memory — fresh case")
    return qwen.diagnose(metrics_dict, symptoms, similar)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Ops-Sentinel")


class InjectRequest(BaseModel):
    fault: Literal["overload", "memory_leak", "dependency_down"]


def _demo_client() -> httpx.Client:
    return httpx.Client(transport=_TRANSPORT, timeout=5.0)


@app.post("/demo/inject")
async def demo_inject(req: InjectRequest):
    try:
        with _demo_client() as c:
            r = c.post(f"{DEMO_URL}/inject", json={"fault": req.fault})
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/demo/reset")
async def demo_reset():
    try:
        with _demo_client() as c:
            r = c.post(f"{DEMO_URL}/reset")
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/demo/metrics")
async def demo_metrics():
    try:
        with _demo_client() as c:
            r = c.get(f"{DEMO_URL}/metrics")
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Status page
# ---------------------------------------------------------------------------
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Ops-Sentinel</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:1.5rem}}
  h1{{color:#58a6ff;margin:0 0 .5rem}}
  h2{{color:#8b949e;font-size:.9rem;margin:1.2rem 0 .4rem;text-transform:uppercase;letter-spacing:.05em}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:4px;font-weight:bold}}
  .healthy{{background:#1f6b2e;color:#56d364}}
  .anomaly{{background:#6e3513;color:#f0883e}}
  .halted {{background:#6e1313;color:#f85149}}
  .safe   {{background:#5a3e00;color:#e3b341}}
  .mcp-on {{background:#0d3b6e;color:#79c0ff}}
  .mcp-off{{background:#3b3b3b;color:#8b949e}}
  table{{border-collapse:collapse;width:100%;margin-top:.4rem}}
  th,td{{border:1px solid #30363d;padding:5px 10px;text-align:left;font-size:.85rem}}
  th{{background:#161b22;color:#8b949e}}
  tr:hover{{background:#161b22}}
  .ts{{color:#8b949e;font-size:.78rem}}
  .controls{{display:flex;gap:.6rem;flex-wrap:wrap;margin:.5rem 0}}
  button{{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-family:monospace;font-size:.85rem;font-weight:bold}}
  .btn-fault{{background:#6e3513;color:#f0883e}}
  .btn-fault:hover{{background:#8a4520}}
  .btn-reset{{background:#1f6b2e;color:#56d364}}
  .btn-reset:hover{{background:#2a8a3e}}
  #msg{{margin:.4rem 0;color:#e3b341;min-height:1.2em;font-size:.85rem}}
  .result-cell{{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.78rem}}
  footer{{margin-top:1.5rem;color:#484f58;font-size:.75rem}}
</style>
</head>
<body>
<h1>Ops-Sentinel <span style="color:#484f58;font-weight:normal;font-size:.7em">/ Qwen Cloud Hackathon Track 1: MemoryAgent</span></h1>
<p>Status: <span class="badge {status_cls}">{status}</span>
{safe_badge}
<span class="badge {mcp_cls}">MCP {mcp_label}</span>
&nbsp;&nbsp;<span class="ts">Last check: {last_check}</span>
&nbsp;&nbsp;<span class="ts">Total incidents: {total_incidents}</span></p>

<h2>Fault Injection (Demo Controls)</h2>
<div class="controls">
  <button class="btn-fault" onclick="inject('overload')">Inject: Overload</button>
  <button class="btn-fault" onclick="inject('memory_leak')">Inject: Memory Leak</button>
  <button class="btn-fault" onclick="inject('dependency_down')">Inject: Dependency Down</button>
  <button class="btn-reset" onclick="resetDemo()">Reset Service</button>
</div>
<div id="msg"></div>

<h2>Current Metrics</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
{metrics_rows}
</table>

<h2>MCP Tool Calls — Last Anomaly Cycle</h2>
<table>
<tr><th>Tool</th><th>Arguments</th><th>Result (truncated)</th></tr>
{mcp_rows}
</table>

<h2>Recent Incidents — Memory (last 10)</h2>
<table>
<tr><th>#</th><th>Time (UTC)</th><th>Symptoms</th><th>Diagnosis</th><th>Action</th><th>Outcome</th><th>OK?</th></tr>
{incident_rows}
</table>

<footer>Auto-refreshes every 10s &mdash; Agent uptime since {uptime}</footer>

<script>
async function inject(fault) {{
  document.getElementById('msg').textContent = 'Injecting ' + fault + '...';
  const r = await fetch('/demo/inject', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{fault}})}});
  const d = await r.json();
  document.getElementById('msg').textContent = r.ok ? 'Injected: ' + JSON.stringify(d) : 'Error: ' + JSON.stringify(d);
}}
async function resetDemo() {{
  document.getElementById('msg').textContent = 'Resetting...';
  const r = await fetch('/demo/reset', {{method:'POST'}});
  const d = await r.json();
  document.getElementById('msg').textContent = r.ok ? 'Reset: ' + JSON.stringify(d) : 'Error: ' + JSON.stringify(d);
}}
</script>
</body>
</html>"""


def _render_status(state: dict, incidents) -> str:
    import json as _json

    status = state.get("status", "unknown")
    status_cls = status if status in {"healthy", "anomaly", "halted"} else "anomaly"
    safe_badge = (
        '<span class="badge safe">SAFE MODE</span>' if state.get("safe_mode") else ""
    )
    mcp_on = state.get("mcp_available", False)
    mcp_cls   = "mcp-on" if mcp_on else "mcp-off"
    mcp_label = "ON" if mcp_on else "OFF"

    metrics = state.get("last_metrics", {})
    metrics_rows = "\n".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in metrics.items()
    ) or "<tr><td colspan=2>No data yet</td></tr>"

    # MCP tool call log
    mcp_calls = state.get("last_mcp_calls", [])
    if mcp_calls:
        mcp_parts = []
        for c in mcp_calls:
            args_s  = _json.dumps(c.get("args", {}))[:80]
            res_s   = _json.dumps(c.get("result", ""))[:120]
            mcp_parts.append(
                f"<tr>"
                f"<td><b>{c.get('tool', '')}</b></td>"
                f"<td class='result-cell' title='{args_s}'>{args_s}</td>"
                f"<td class='result-cell' title='{res_s}'>{res_s}</td>"
                f"</tr>"
            )
        mcp_rows = "\n".join(mcp_parts)
    else:
        mcp_rows = "<tr><td colspan=3 style='color:#484f58'>No MCP calls yet — waiting for next anomaly</td></tr>"

    incident_rows_parts = []
    for inc in incidents:
        diag = (inc.diagnosis or "")[:80] + ("…" if len(inc.diagnosis or "") > 80 else "")
        incident_rows_parts.append(
            f"<tr>"
            f"<td>{inc.id}</td>"
            f"<td class='ts'>{inc.ts[:19]}</td>"
            f"<td>{', '.join(inc.symptoms)}</td>"
            f"<td>{diag}</td>"
            f"<td>{inc.action}</td>"
            f"<td>{inc.outcome}</td>"
            f"<td>{'&#10003;' if inc.resolved else '&#10007;'}</td>"
            f"</tr>"
        )
    incident_rows = "\n".join(incident_rows_parts) or (
        "<tr><td colspan=7 style='color:#484f58'>No incidents yet</td></tr>"
    )

    uptime = (state.get("uptime_start") or "")[:19]

    return _HTML.format(
        status=status.upper(),
        status_cls=status_cls,
        safe_badge=safe_badge,
        mcp_cls=mcp_cls,
        mcp_label=mcp_label,
        last_check=(state.get("last_check") or "—")[:19],
        total_incidents=state.get("total_incidents", 0),
        metrics_rows=metrics_rows,
        mcp_rows=mcp_rows,
        incident_rows=incident_rows,
        uptime=uptime,
    )


@app.get("/", response_class=HTMLResponse)
async def status_page():
    return _render_status(_get_state(), _memory.get_recent(10))


@app.get("/api/status")
async def api_status():
    return JSONResponse(_get_state())


@app.get("/api/incidents")
async def api_incidents():
    return JSONResponse([
        {
            "id": i.id, "ts": i.ts, "symptoms": i.symptoms,
            "diagnosis": i.diagnosis, "action": i.action,
            "outcome": i.outcome, "resolved": i.resolved,
        }
        for i in _memory.get_recent(20)
    ])


@app.get("/api/mcp/calls")
async def api_mcp_calls():
    """Last MCP tool calls for the most recent anomaly cycle."""
    return JSONResponse(_get_state().get("last_mcp_calls", []))


@app.get("/health")
async def health():
    state = _get_state()
    return {"status": "ok", "agent": state.get("status"), "mcp": state.get("mcp_available")}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
_memory: IncidentMemory


def main():
    global _memory
    _memory   = IncidentMemory(DB_PATH)
    collector = MetricsCollector(DEMO_URL)
    detector  = AnomalyDetector()
    qwen      = QwenClient(QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL)
    executor  = ActionExecutor(collector)
    mcp       = IncidentMCPClient(MCP_SERVER_URL)

    t = threading.Thread(
        target=_agent_loop,
        args=(collector, detector, _memory, qwen, executor, mcp),
        daemon=True,
        name="agent-loop",
    )
    t.start()

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
