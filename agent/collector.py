"""Polls the demo service and returns a snapshot of current metrics."""
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 6.0   # probe /health with a generous timeout so overload is observable

# local_address="127.0.0.1" was here to fix Windows IPv6 localhost resolution delay,
# but it binds the outgoing socket to loopback, which Linux kernel rejects (EINVAL)
# when the destination is another Docker container. Removed: service names don't
# have the IPv6 ambiguity, so the workaround was never needed in production.
_TRANSPORT = httpx.HTTPTransport()


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(transport=_TRANSPORT, timeout=timeout)


@dataclass
class MetricsSnapshot:
    rss_mb: float
    latency_ms: float
    error_rate: float
    fault: str
    reachable: bool


class MetricsCollector:
    def __init__(self, demo_url: str, timeout: float = 5.0):
        self._base = demo_url.rstrip("/")
        self._timeout = timeout

    def collect(self) -> MetricsSnapshot:
        # Probe /health to measure real end-to-end latency (catches overload)
        probe_latency_ms = self._probe_health()
        if probe_latency_ms is None:
            return MetricsSnapshot(
                rss_mb=0.0, latency_ms=9999.0,
                error_rate=1.0, fault="unknown", reachable=False,
            )
        try:
            with _client(self._timeout) as c:
                r = c.get(f"{self._base}/metrics")
            r.raise_for_status()
            d = r.json()
            return MetricsSnapshot(
                rss_mb=d.get("rss_mb", 0.0),
                latency_ms=probe_latency_ms,   # real measured latency
                error_rate=d.get("error_rate", 0.0),
                fault=d.get("fault", "none"),
                reachable=True,
            )
        except Exception as exc:
            logger.warning("Failed to collect metrics: %s", exc)
            return MetricsSnapshot(
                rss_mb=0.0, latency_ms=9999.0,
                error_rate=1.0, fault="unknown", reachable=False,
            )

    def _probe_health(self) -> float | None:
        """Times a GET /health call. Returns latency in ms, or None on failure."""
        t0 = time.monotonic()
        try:
            with _client(PROBE_TIMEOUT) as c:
                r = c.get(f"{self._base}/health")
            r.raise_for_status()
            return (time.monotonic() - t0) * 1000
        except Exception as exc:
            logger.warning("Health probe failed: %s", exc)
            return None

    def reset_service(self) -> bool:
        """Calls POST /reset on the demo service (restart action)."""
        try:
            with _client(self._timeout) as c:
                r = c.post(f"{self._base}/reset")
            r.raise_for_status()
            logger.info("Demo service reset successful")
            return True
        except Exception as exc:
            logger.error("Failed to reset demo service: %s", exc)
            return False
