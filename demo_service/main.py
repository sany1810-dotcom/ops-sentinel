"""
Demo target service. Exposes /health, /metrics, /api/data.
Fault injection: POST /inject {"fault": "overload|memory_leak|dependency_down"}
                 POST /reset
"""
import os
import time
import random
import asyncio
import collections
from contextlib import asynccontextmanager
from typing import Literal

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

FAULT_NONE = "none"
FAULT_OVERLOAD = "overload"
FAULT_MEMORY_LEAK = "memory_leak"
FAULT_DEPENDENCY_DOWN = "dependency_down"

fault_state: str = FAULT_NONE
_leak_buckets: list = []           # grows while memory_leak is active
_request_times: collections.deque = collections.deque(maxlen=50)
_error_counts: collections.deque = collections.deque(maxlen=50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_background_leak())
    yield


app = FastAPI(title="Ops-Sentinel Demo Service", lifespan=lifespan)


async def _background_leak():
    """Simulates gradual memory growth when memory_leak fault is active."""
    while True:
        await asyncio.sleep(2)
        if fault_state == FAULT_MEMORY_LEAK:
            # ~200 KB per tick
            _leak_buckets.append(bytearray(200 * 1024))


def _record_request(latency_ms: float, is_error: bool):
    _request_times.append(latency_ms)
    _error_counts.append(1 if is_error else 0)


@app.get("/health")
async def health():
    t0 = time.monotonic()
    if fault_state == FAULT_OVERLOAD:
        await asyncio.sleep(random.uniform(2.5, 4.0))
    latency = (time.monotonic() - t0) * 1000
    _record_request(latency, False)
    return {"status": "ok", "fault": fault_state}


@app.get("/metrics")
async def metrics():
    proc = psutil.Process(os.getpid())
    rss_mb = proc.memory_info().rss / 1024 / 1024

    avg_latency = (
        sum(_request_times) / len(_request_times) if _request_times else 0.0
    )
    error_rate = (
        sum(_error_counts) / len(_error_counts) if _error_counts else 0.0
    )
    return {
        "rss_mb": round(rss_mb, 2),
        "latency_ms": round(avg_latency, 1),
        "error_rate": round(error_rate, 3),
        "fault": fault_state,
        "request_count": len(_request_times),
    }


@app.get("/api/data")
async def api_data():
    t0 = time.monotonic()
    if fault_state == FAULT_OVERLOAD:
        await asyncio.sleep(random.uniform(2.5, 4.0))
    if fault_state == FAULT_DEPENDENCY_DOWN:
        latency = (time.monotonic() - t0) * 1000
        _record_request(latency, True)
        raise HTTPException(status_code=500, detail="Upstream dependency unavailable")
    latency = (time.monotonic() - t0) * 1000
    _record_request(latency, False)
    return {"data": [random.random() for _ in range(10)]}


class InjectRequest(BaseModel):
    fault: Literal["overload", "memory_leak", "dependency_down"]


@app.post("/inject")
async def inject(req: InjectRequest):
    global fault_state, _leak_buckets
    fault_state = req.fault
    if req.fault == FAULT_MEMORY_LEAK:
        _leak_buckets = []  # start fresh
    return {"injected": fault_state}


@app.post("/reset")
async def reset():
    global fault_state, _leak_buckets
    fault_state = FAULT_NONE
    _leak_buckets = []
    _request_times.clear()
    _error_counts.clear()
    return {"status": "reset", "fault": FAULT_NONE}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
