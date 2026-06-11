"""Polls the demo service and returns a snapshot of current metrics."""
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


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
        try:
            r = httpx.get(f"{self._base}/metrics", timeout=self._timeout)
            r.raise_for_status()
            d = r.json()
            return MetricsSnapshot(
                rss_mb=d.get("rss_mb", 0.0),
                latency_ms=d.get("latency_ms", 0.0),
                error_rate=d.get("error_rate", 0.0),
                fault=d.get("fault", "none"),
                reachable=True,
            )
        except Exception as exc:
            logger.warning("Failed to collect metrics: %s", exc)
            return MetricsSnapshot(
                rss_mb=0.0,
                latency_ms=9999.0,
                error_rate=1.0,
                fault="unknown",
                reachable=False,
            )

    def reset_service(self) -> bool:
        """Calls POST /reset on the demo service (restart action)."""
        try:
            r = httpx.post(f"{self._base}/reset", timeout=self._timeout)
            r.raise_for_status()
            logger.info("Demo service reset successful")
            return True
        except Exception as exc:
            logger.error("Failed to reset demo service: %s", exc)
            return False
