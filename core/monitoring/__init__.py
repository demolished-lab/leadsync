"""
Lightweight Prometheus-compatible metrics exporter.

No external dependencies — uses only stdlib to format the Prometheus
text exposition format. Tracks request counts, latencies, queue depth,
LLM provider health, and custom business metrics.

Endpoint: GET /metrics  →  Prometheus text format
"""

import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime
from collections import defaultdict


class _Metrics:
    """Thread-safe in-memory metrics store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, list] = defaultdict(list)
        self._labels: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._start_time = time.time()

    def inc(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter."""
        with self._lock:
            key = self._label_key(name, labels)
            self._counters[key] += value

    def gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge value."""
        with self._lock:
            key = self._label_key(name, labels)
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record a histogram observation."""
        with self._lock:
            key = self._label_key(name, labels)
            self._histograms[key].append(value)
            # Keep last 1000 observations
            if len(self._histograms[key]) > 1000:
                self._histograms[key] = self._histograms[key][-1000:]

    def _label_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        if not labels:
            return name
        sorted_labels = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{sorted_labels}}}"

    def export(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines = []
        lines.append("# HELP leadsync_uptime_seconds Time since process start")
        lines.append("# TYPE leadsync_uptime_seconds gauge")
        lines.append(f"leadsync_uptime_seconds {time.time() - self._start_time:.2f}")
        lines.append("")

        with self._lock:
            # Counters
            for key, value in sorted(self._counters.items()):
                metric_name = key.split("{")[0] if "{" in key else key
                lines.append(f"# TYPE {metric_name} counter")
                lines.append(f"{key} {value:.2f}")
            lines.append("")

            # Gauges
            for key, value in sorted(self._gauges.items()):
                metric_name = key.split("{")[0] if "{" in key else key
                lines.append(f"# TYPE {metric_name} gauge")
                lines.append(f"{key} {value:.2f}")
            lines.append("")

            # Histograms — labels survive bucket expansion intact
            for key, observations in sorted(self._histograms.items()):
                metric_name, base_labels = self._parse_key(key)
                lines.append(f"# TYPE {metric_name} histogram")
                if observations:
                    sorted_obs = sorted(observations)
                    count = len(sorted_obs)
                    total = sum(sorted_obs)
                    lines.append(f"{self._label_key(metric_name, base_labels)}_count {count}")
                    lines.append(f"{self._label_key(metric_name, base_labels)}_sum {total:.4f}")
                    # Bucket boundaries (cumulative counts) — le first, then series labels
                    def bucket_name(le: str) -> str:
                        return self._label_key(
                            f"{metric_name}_bucket",
                            {**base_labels, "le": le},
                        )
                    for bucket in [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]:
                        cumcount = sum(1 for o in sorted_obs if o <= bucket)
                        lines.append(f'{bucket_name(str(bucket))} {cumcount}')
                    lines.append(f'{bucket_name("+Inf")} {count}')
            lines.append("")

        return "\n".join(lines)

    def _parse_key(self, key: str):
        """Split 'name{a="x",b="y"}' back into ('name', {'a': 'x', 'b': 'y'})."""
        if "{" not in key:
            return key, {}
        name = key.split("{", 1)[0]
        inner = key[key.index("{") + 1 : key.rindex("}")]
        labels: Dict[str, str] = {}
        import csv as _csv
        try:
            reader = _csv.reader([inner])
            parts = next(reader) if inner else []
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')
        except Exception:
            labels = {}
        return name, labels


# Global singleton
metrics = _Metrics()


def record_request(method: str, path: str, status: int, duration: float) -> None:
    """Record an HTTP request metric."""
    labels = {"method": method, "path": path, "status": str(status)}
    metrics.inc("leadsync_http_requests_total", labels=labels)
    metrics.observe("leadsync_http_request_duration_seconds", duration, labels=labels)


def record_llm_call(provider: str, model: str, latency: float, success: bool) -> None:
    """Record an LLM call metric."""
    labels = {"provider": provider, "model": model, "success": str(success).lower()}
    metrics.inc("leadsync_llm_calls_total", labels=labels)
    metrics.observe("leadsync_llm_call_duration_seconds", latency, labels=labels)


def record_queue_depth(depth: int) -> None:
    """Record current queue depth."""
    metrics.gauge("leadsync_queue_depth", depth)


def record_sla_breach(count: int) -> None:
    """Record SLA breach count."""
    metrics.gauge("leadsync_sla_breaches", count)


def record_draft_generated(tone: str) -> None:
    """Record a draft generation event."""
    metrics.inc("leadsync_drafts_generated_total", labels={"tone": tone})


def record_email_sent(status: str) -> None:
    """Record an email send event."""
    metrics.inc("leadsync_emails_sent_total", labels={"status": status})
