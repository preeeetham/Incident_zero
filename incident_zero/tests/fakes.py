"""In-memory stand-ins for Prometheus, Jaeger, Docker and logs, driven by a scenario dict.

A scenario says, per service: rps / error_ratio / p95 now and at baseline, container state, log lines, traces.
The fakes answer the exact PromQL the tools and the Sentinel send (telemetry.queries), so tests exercise the real
query-building code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from incident_zero.telemetry.backends import (BackendError, Endpoints, Jaeger, LogLine, Logs, Prometheus, Topology)
from incident_zero.telemetry.tools import Telemetry

HEALTHY = {"rps": 2.0, "error_ratio": 0.0, "p95": 120.0}


@dataclass
class Svc:
    now: dict = field(default_factory=lambda: dict(HEALTHY))
    base: dict = field(default_factory=lambda: dict(HEALTHY))
    state: str = "running"
    exit_code: int = 0
    oom: bool = False
    restarts: int = 0
    networks: list = field(default_factory=lambda: ["opentelemetry-demo"])
    logs: list = field(default_factory=list)
    outbound_errors: dict = field(default_factory=dict)  # span_name -> error rps
    cpu: float = 5.0
    cpu_base: Optional[float] = None   # CPU % at baseline (default: the same as now)
    mem_pct: float = 30.0
    ops: dict = field(default_factory=dict)  # span_name -> (errors now, calls now) over the window; baseline: 0 errors


class FakeProm(Prometheus):
    def __init__(self, svcs: dict[str, Svc], down: bool = False):
        super().__init__("http://prom")
        self.svcs, self.down, self.queries = svcs, down, []

    def _val(self, svc: Svc, offset: bool, key: str) -> Optional[float]:
        d = svc.base if offset else svc.now
        if key == "p95":
            return d.get("p95") if d.get("rps", 0) > 0 else None
        if key == "err":
            return d.get("rps", 0) * d.get("error_ratio", 0)
        return d.get("rps", 0)

    def query_range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        """A flat history at today's instant values (enough to exercise the overview's range queries)."""
        out = []
        for item in self.query(promql):
            out.append({"metric": item.get("metric", {}),
                        "values": [[t, item["value"][1]] for t in range(int(start), int(end) + 1, int(step))]})
        return out

    def query(self, promql: str, at=None) -> list[dict]:
        self.queries.append(promql)
        if self.down:
            raise BackendError("Prometheus unreachable: ConnectionError")
        offset = " offset " in promql
        m = re.search(r'service_name="([^"]+)"', promql)
        only = m.group(1) if m else None
        out = []
        if "by (service_name, span_name)" in promql:
            err = "STATUS_CODE_ERROR" in promql
            return [{"metric": {"service_name": n, "span_name": op},
                     "value": [0, str((0 if offset else e) if err else t)]}
                    for n, s in self.svcs.items() for op, (e, t) in s.ops.items()]
        if "by (span_name" in promql and "CLIENT" in promql and "STATUS_CODE_ERROR" in promql:
            svc = self.svcs.get(only or "")
            return [{"metric": {"span_name": k}, "value": [0, str(v)]} for k, v in (svc.outbound_errors if svc else {}).items()]
        if "container_cpu_usage_nanoseconds_total" in promql:
            return [{"metric": {"container_name": n},
                     "value": [0, str((s.cpu if s.cpu_base is None else s.cpu_base) if offset else s.cpu)]}
                    for n, s in self.svcs.items() if s.state == "running"]
        if "by (span_name" in promql:
            return []
        if "timestamp(" in promql:
            if "__name__" in promql:  # the datastore freshness query labels each metric's name as `metric`
                return [{"metric": {"metric": "redis_clients_connected"}, "value": [0, "5"]}]
            return [{"metric": {"service_name": n}, "value": [0, "5"]} for n, s in self.svcs.items()
                    if s.now.get("rps", 0) > 0]
        if "__name__" in promql:
            return [{"metric": {"__name__": "redis_clients_connected"}, "value": [0, "4"]}]
        key = "p95" if "histogram_quantile" in promql else "err" if "STATUS_CODE_ERROR" in promql else "rps"
        for name, svc in self.svcs.items():
            if only and name != only:
                continue
            v = self._val(svc, offset, key)
            if v is None or (svc.base if offset else svc.now).get("rps", 0) == 0:
                continue  # no traffic: Prometheus has no series at all
            out.append({"metric": {"service_name": name}, "value": [0, str(v)]})
        return out


class FakeJaeger(Jaeger):
    def __init__(self, traces: dict[str, list[dict]]):
        super().__init__("http://jaeger")
        self.traces = traces

    def find_traces(self, service, operation=None, errors_only=False, min_duration_ms=None, lookback_s=900, limit=20):
        return self.traces.get(service, [])[:limit]

    def trace(self, trace_id):
        for lst in self.traces.values():
            for t in lst:
                if t["traceID"] == trace_id:
                    return t
        return None


class FakeContainer:
    def __init__(self, name: str, svc: Svc):
        self.name, self.svc = name, svc
        self.labels = {"com.docker.compose.service": name, "com.docker.compose.project": "opentelemetry-demo"}

    def reload(self):
        pass

    @property
    def attrs(self):
        s = self.svc
        return {"State": {"Status": s.state, "Running": s.state == "running", "Paused": s.state == "paused",
                          "Restarting": s.state == "restarting", "ExitCode": s.exit_code, "OOMKilled": s.oom,
                          "StartedAt": "2026-10-23T10:00:00.000Z", "FinishedAt": "2026-10-23T10:05:00Z"},
                "RestartCount": s.restarts, "HostConfig": {"NanoCpus": 0, "Memory": 0},
                "NetworkSettings": {"Networks": {n: {} for n in s.networks}, "Ports": {}}}

    def logs(self, since=0, timestamps=True, tail=2000):
        return "\n".join(f"2026-10-23T10:00:0{i}.000000000Z {ln}" for i, ln in enumerate(self.svc.logs)).encode()


class FakeDocker:
    def __init__(self, svcs: dict[str, Svc]):
        self.svcs = svcs
        self.project = "opentelemetry-demo"

    def containers(self):
        return {n: FakeContainer(n, s) for n, s in self.svcs.items()}

    def container(self, service):
        return self.containers().get(service)

    def status(self, c):
        from incident_zero.telemetry.backends import DockerReader

        return DockerReader.status(self, c)  # type: ignore[arg-type]

    def stats(self, c):
        s = c.svc
        return {"cpu_percent": s.cpu, "memory_mb": 100.0, "memory_limit_mb": 200.0, "memory_percent": s.mem_pct,
                "throttled_periods": 0, "throttled_periods_delta": 0, "net_rx_errors": 0, "net_tx_dropped": 0,
                "pids": 10}

    def published_port(self, service, port):
        return None

    def compose_files(self):
        return []


class FakeLogs(Logs):
    def __init__(self, svcs: dict[str, Svc]):
        self.svcs = svcs

    def search(self, service, contains=None, errors_only=True, minutes=10, limit=20):
        lines = [LogLine(ts="2026-10-23T10:00:00Z", service=service, severity="ERROR", body=b)
                 for b in (self.svcs.get(service) or Svc()).logs if not contains or contains in b]
        return "opensearch", lines[:limit], ""


TOPOLOGY = Topology(edges=[
    ("frontend-proxy", "frontend", "depends_on"), ("frontend", "cart", "depends_on"),
    ("frontend", "checkout", "depends_on"), ("frontend", "product-catalog", "depends_on"),
    ("checkout", "payment", "depends_on"), ("checkout", "cart", "depends_on"), ("cart", "valkey-cart", "depends_on"),
    ("product-catalog", "astronomy-db", "depends_on"),
], roles={n: "app" for n in ("frontend-proxy", "frontend", "cart", "checkout", "payment", "product-catalog")}
   | {"valkey-cart": "datastore", "astronomy-db": "datastore"})

SERVICES = ("frontend-proxy", "frontend", "cart", "checkout", "payment", "product-catalog", "valkey-cart", "astronomy-db")


def healthy() -> dict[str, Svc]:
    svcs = {n: Svc() for n in SERVICES}
    for ds in ("valkey-cart", "astronomy-db"):
        svcs[ds].now = {"rps": 0.0, "error_ratio": 0.0, "p95": 0}
        svcs[ds].base = {"rps": 0.0, "error_ratio": 0.0, "p95": 0}
    return svcs


def span(sid, svc_pid, op, dur_ms, parent=None, error=False, kind="server", msg=""):
    tags = [{"key": "span.kind", "value": kind}]
    if error:
        tags += [{"key": "error", "value": True}, {"key": "otel.status_code", "value": "ERROR"}]
    logs = [{"timestamp": 1, "fields": [{"key": "event", "value": "exception"},
                                        {"key": "exception.message", "value": msg}]}] if msg else []
    return {"spanID": sid, "processID": svc_pid, "operationName": op, "duration": int(dur_ms * 1000),
            "startTime": int(sid[-1], 16), "tags": tags, "logs": logs,
            "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else []}


def payment_trace(trace_id="abc123"):
    return {"traceID": trace_id, "processes": {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": "checkout"},
                                               "p3": {"serviceName": "payment"}},
            "spans": [span("s1", "p1", "POST /api/checkout", 300, error=True, msg="checkout failed"),
                      span("s2", "p2", "oteldemo.CheckoutService/PlaceOrder", 280, "s1", error=True,
                           msg="failed to charge card"),
                      span("s3", "p2", "oteldemo.PaymentService/Charge", 250, "s2", error=True, kind="client"),
                      span("s4", "p3", "oteldemo.PaymentService/Charge", 240, "s3", error=True,
                           msg="Payment request failed. Invalid token.")]}


def telemetry(settings, svcs: dict[str, Svc], traces: Optional[dict] = None, prom_down: bool = False) -> Telemetry:
    return Telemetry(settings, FakeProm(svcs, prom_down), FakeJaeger(traces or {}), FakeLogs(svcs),
                     FakeDocker(svcs), TOPOLOGY, Endpoints("http://prom", "http://jaeger", ""))  # type: ignore[arg-type]
