"""Threshold-based anomaly detection. Returns a list of symptom strings."""
from dataclasses import dataclass

from collector import MetricsSnapshot

LATENCY_WARN_MS = 500.0
LATENCY_CRIT_MS = 2000.0
RSS_WARN_MB = 150.0
RSS_CRIT_MB = 300.0
ERROR_RATE_WARN = 0.2
ERROR_RATE_CRIT = 0.5


@dataclass
class DetectionResult:
    symptoms: list[str]
    is_anomaly: bool
    severity: str  # "none" | "warn" | "critical"


class AnomalyDetector:
    def detect(self, snap: MetricsSnapshot) -> DetectionResult:
        symptoms: list[str] = []
        severity = "none"

        if not snap.reachable:
            symptoms.append("service_unreachable")
            return DetectionResult(symptoms, True, "critical")

        if snap.latency_ms >= LATENCY_CRIT_MS:
            symptoms.append("high_latency_critical")
            severity = "critical"
        elif snap.latency_ms >= LATENCY_WARN_MS:
            symptoms.append("high_latency")
            severity = max(severity, "warn", key=lambda s: {"none": 0, "warn": 1, "critical": 2}[s])

        if snap.rss_mb >= RSS_CRIT_MB:
            symptoms.append("memory_leak_critical")
            severity = "critical"
        elif snap.rss_mb >= RSS_WARN_MB:
            symptoms.append("high_memory")
            if severity != "critical":
                severity = "warn"

        if snap.error_rate >= ERROR_RATE_CRIT:
            symptoms.append("high_error_rate_critical")
            severity = "critical"
        elif snap.error_rate >= ERROR_RATE_WARN:
            symptoms.append("high_error_rate")
            if severity != "critical":
                severity = "warn"

        return DetectionResult(symptoms, bool(symptoms), severity)
