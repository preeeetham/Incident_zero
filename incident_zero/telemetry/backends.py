"""Read-only clients for the four telemetry sources, and endpoint discovery.

  Prometheus   instant and range PromQL over the collector's span metrics, docker_stats and datastore receivers
  Jaeger       trace search and trace detail (Jaeger v2, UI base path /jaeger/ui)
  Logs         OpenSearch (`otel-logs-*`), falling back to `docker logs` when OpenSearch is absent or down
  Docker       container state, restarts, OOM kills, health, limits, networks and live stats; never a write call
  Topology     dependency edges from the stack's own compose file, found through the containers' compose labels

A new backend (Loki, Tempo, Datadog, ...) is one more class with the same methods (REQUIREMENTS N4).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

TIMEOUT = 8.0
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_FILES_LABEL = "com.docker.compose.project.config_files"


class BackendError(RuntimeError):
    pass


# ---------------------------------------------------------------- Prometheus
class Prometheus:
    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def query(self, promql: str, at: Optional[float] = None) -> list[dict]:
        params: dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def query_range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        return self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})

    def _get(self, path: str, params: dict) -> list[dict]:
        if not self.url:
            raise BackendError("Prometheus URL unknown")
        try:
            r = self.http.get(self.url + path, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"Prometheus unreachable: {exc.__class__.__name__}") from exc
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or body.get("status") != "success":
            raise BackendError(f"Prometheus {r.status_code}: {body.get('error') or r.text[:200]}")
        return body["data"]["result"]

    def vector_by(self, promql: str, labels: tuple[str, ...]) -> dict[tuple, float]:
        """{(label values...): number} for an instant vector keyed by several labels."""
        out: dict[tuple, float] = {}
        for item in self.query(promql):
            m = item.get("metric", {})
            try:
                v = float(item["value"][1])
            except (KeyError, ValueError, IndexError):
                continue
            if v == v and all(lb in m for lb in labels):
                out[tuple(m[lb] for lb in labels)] = v
        return out

    def vector(self, promql: str, label: str, at: Optional[float] = None) -> dict[str, float]:
        """{label value: number} for an instant vector; NaN and missing labels are dropped."""
        out: dict[str, float] = {}
        for item in self.query(promql, at):
            key = item.get("metric", {}).get(label)
            try:
                v = float(item["value"][1])
            except (KeyError, ValueError, IndexError):
                continue
            if key is not None and v == v:  # v == v filters NaN
                out[key] = v
        return out


# ---------------------------------------------------------------- Jaeger
OTLP_KIND = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
             "SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client",
             "SPAN_KIND_PRODUCER": "producer", "SPAN_KIND_CONSUMER": "consumer"}


def _otlp_value(v: dict) -> Any:
    for k in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    if "arrayValue" in v:
        return [_otlp_value(x) for x in (v["arrayValue"].get("values") or [])]
    return None


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def otlp_to_traces(result: dict) -> list[dict]:
    """Jaeger API v3 returns OTLP JSON (resourceSpans, mixed across traces). Convert to the Jaeger v1 trace shape
    (spans with tags, logs, references; processes) that summarize_trace reads, one dict per trace."""
    traces: dict[str, dict] = {}
    for rs in result.get("resourceSpans") or []:
        rattrs = {a["key"]: _otlp_value(a.get("value") or {}) for a in (rs.get("resource") or {}).get("attributes") or []}
        svc = str(rattrs.get("service.name") or "?")
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                tid = sp.get("traceId", "")
                t = traces.setdefault(tid, {"traceID": tid, "spans": [], "processes": {}})
                pid = f"p-{svc}"
                t["processes"][pid] = {"serviceName": svc}
                status = sp.get("status") or {}
                code = status.get("code")
                tags = [{"key": "span.kind", "value": OTLP_KIND.get(sp.get("kind"), "internal")}]
                if code in (2, "STATUS_CODE_ERROR"):
                    tags += [{"key": "error", "value": True}, {"key": "otel.status_code", "value": "ERROR"}]
                    if status.get("message"):
                        tags.append({"key": "otel.status_description", "value": status["message"]})
                tags += [{"key": a["key"], "value": _otlp_value(a.get("value") or {})} for a in sp.get("attributes") or []]
                logs = [{"timestamp": int(ev.get("timeUnixNano", 0)) // 1000,
                         "fields": [{"key": "event", "value": ev.get("name", "")}]
                         + [{"key": a["key"], "value": _otlp_value(a.get("value") or {})}
                            for a in ev.get("attributes") or []]} for ev in sp.get("events") or []]
                start, end = int(sp.get("startTimeUnixNano", 0)), int(sp.get("endTimeUnixNano", 0))
                t["spans"].append({
                    "traceID": tid, "spanID": sp.get("spanId", ""), "operationName": sp.get("name", ""),
                    "processID": pid, "startTime": start // 1000, "duration": max(end - start, 0) // 1000,
                    "tags": tags, "logs": logs,
                    "references": [{"refType": "CHILD_OF", "spanID": sp["parentSpanId"]}] if sp.get("parentSpanId") else [],
                })
    return list(traces.values())


def _has_error(trace: dict) -> bool:
    return any(t.get("key") == "error" and t.get("value") in (True, "true") for s in trace["spans"] for t in s["tags"])


class Jaeger:
    """Jaeger v2's query API (`<base>/api/v3`), which is all Jaeger 2.x serves under the demo's base path."""

    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if not self.url:
            raise BackendError("Jaeger URL unknown")
        try:
            r = self.http.get(self.url + "/api/v3" + path, params=params, timeout=TIMEOUT * 2)
        except requests.RequestException as exc:
            raise BackendError(f"Jaeger unreachable: {exc.__class__.__name__}") from exc
        if r.status_code == 404 and "not found" in r.text.lower() and path.startswith("/traces"):
            return {}
        if r.status_code != 200:
            raise BackendError(f"Jaeger {r.status_code}: {r.text[:200]}")
        return r.json()

    def services(self) -> list[str]:
        return sorted(self._get("/services").get("services") or [])

    def find_traces(self, service: str, operation: Optional[str] = None, errors_only: bool = False,
                    min_duration_ms: Optional[float] = None, lookback_s: float = 900, limit: int = 20) -> list[dict]:
        now = time.time()
        params: dict[str, Any] = {"query.service_name": service, "query.start_time_min": _rfc3339(now - lookback_s),
                                  "query.start_time_max": _rfc3339(now),
                                  "query.num_traces": min(limit * (6 if errors_only else 1), 60)}
        if operation:
            params["query.operation_name"] = operation
        if min_duration_ms:
            params["query.duration_min"] = f"{int(min_duration_ms)}ms"
        traces = otlp_to_traces(self._get("/traces", params).get("result") or {})
        if errors_only:
            traces = [t for t in traces if _has_error(t)]
        traces.sort(key=lambda t: -max((s["startTime"] for s in t["spans"]), default=0))
        return traces[:limit]

    def trace(self, trace_id: str) -> Optional[dict]:
        found = otlp_to_traces(self._get(f"/traces/{trace_id}").get("result") or {})
        return found[0] if found else None


# ---------------------------------------------------------------- logs
@dataclass
class LogLine:
    ts: str
    service: str
    severity: str
    body: str
    trace_id: str = ""


class OpenSearchLogs:
    INDEX = "otel-logs-*"
    SERVICE_FIELDS = ("resource.service.name", "resource.attributes.service.name", "resource.attributes.service@name",
                      "service.name", "serviceName")
    SEVERITY_FIELDS = ("severity.text", "severityText", "severity_text", "level")

    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def search(self, service: Optional[str], contains: Optional[str], errors_only: bool, minutes: float,
               limit: int) -> list[LogLine]:
        if not self.url:
            raise BackendError("OpenSearch URL unknown")
        must: list[dict] = [{"range": {"@timestamp": {"gte": f"now-{int(minutes * 60)}s"}}}]
        if service:
            must.append({"bool": {"should": [{"match_phrase": {f: service}} for f in self.SERVICE_FIELDS],
                                  "minimum_should_match": 1}})
        if contains:
            must.append({"query_string": {"query": f"\"{contains}\"", "fields": ["body", "body.*", "message"]}})
        if errors_only:
            must.append({"bool": {"should": [{"terms": {f: ["ERROR", "error", "Error", "FATAL", "fatal", "WARN",
                                                            "warn", "Warning", "WARNING"]}} for f in self.SEVERITY_FIELDS]
                                  + [{"range": {"severity.number": {"gte": 13}}}, {"range": {"severityNumber": {"gte": 13}}}],
                                  "minimum_should_match": 1}})
        q = {"size": limit, "sort": [{"@timestamp": "desc"}], "query": {"bool": {"must": must}}}
        try:
            r = self.http.post(f"{self.url}/{self.INDEX}/_search", json=q, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"OpenSearch unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise BackendError(f"OpenSearch {r.status_code}: {r.text[:200]}")
        return [self._line(h.get("_source") or {}) for h in (r.json().get("hits") or {}).get("hits") or []]

    def _line(self, src: dict) -> LogLine:
        def lookup(d: Any, path: str) -> Any:
            # keys may be nested objects or flat dotted names ("resource": {"service.name": ...}): try both
            if not isinstance(d, dict) or not path:
                return None
            if path in d:
                return d[path]
            parts = path.split(".")
            for i in range(1, len(parts)):
                head, rest = ".".join(parts[:i]), ".".join(parts[i:])
                if head in d:
                    found = lookup(d[head], rest)
                    if found is not None:
                        return found
            return None

        def pick(*paths: str) -> str:
            for p in paths:
                cur = lookup(src, p)
                if cur not in (None, ""):
                    return str(cur)
            return ""
        body = pick("body", "message", "body.text")
        return LogLine(ts=pick("@timestamp", "time", "observedTimestamp"), service=pick(*self.SERVICE_FIELDS),
                       severity=pick(*self.SEVERITY_FIELDS) or "", body=body, trace_id=pick("traceId", "trace_id"))


ERROR_WORDS = ("error", "exception", "fail", "fatal", "panic", "refused", "timeout", "timed out", "unavailable",
               "oom", "killed", "warn")


class DockerLogs:
    """Fallback log source: the container's own stdout/stderr."""

    def __init__(self, docker: "DockerReader"):
        self.docker = docker

    def search(self, service: Optional[str], contains: Optional[str], errors_only: bool, minutes: float,
               limit: int) -> list[LogLine]:
        if not service:
            raise BackendError("container logs need a service")
        c = self.docker.container(service)
        if c is None:
            raise BackendError(f"no container for {service}")
        raw = c.logs(since=int(time.time() - minutes * 60), timestamps=True, tail=2000).decode("utf-8", "replace")
        out: list[LogLine] = []
        for ln in raw.splitlines():
            ts, _, body = ln.partition(" ")
            low = body.lower()
            if contains and contains.lower() not in low:
                continue
            if errors_only and not any(w in low for w in ERROR_WORDS):
                continue
            out.append(LogLine(ts=ts[:23], service=service, severity="", body=body.strip()[:500]))
        return out[-limit:][::-1]


class Logs:
    def __init__(self, primary: Optional[OpenSearchLogs], fallback: DockerLogs):
        self.primary, self.fallback = primary, fallback

    def search(self, service: Optional[str], contains: Optional[str] = None, errors_only: bool = True,
               minutes: float = 10, limit: int = 20) -> tuple[str, list[LogLine], str]:
        """(source used, lines, note). Falls back to container logs when OpenSearch fails or finds nothing."""
        note = ""
        if self.primary is not None:
            try:
                lines = self.primary.search(service, contains, errors_only, minutes, limit)
                if lines:
                    return "opensearch", lines, ""
                note = "OpenSearch returned no lines; "
            except BackendError as exc:
                note = f"{exc}; "
        lines = self.fallback.search(service, contains, errors_only, minutes, limit)
        return "docker", lines, note + "used container logs"


# ---------------------------------------------------------------- Docker (read-only)
def _parse_ts(s: str) -> Optional[datetime]:
    if not s or s.startswith("0001"):
        return None
    s = s.rstrip("Z")
    s = s.split(".")[0]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class DockerReader:
    """Only inspect, stats and logs. The agent process never stops, starts or changes a container: that is the
    Action API's job (REQUIREMENTS F6.1)."""

    def __init__(self, project: str, client: Any = None):
        self.project = project
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def containers(self) -> dict[str, Any]:
        out = {}
        for c in self.client.containers.list(all=True, filters={"label": f"{COMPOSE_PROJECT_LABEL}={self.project}"}):
            svc = (c.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if svc:
                out[svc] = c
        return out

    def container(self, service: str):
        return self.containers().get(service)

    def status(self, c) -> dict[str, Any]:
        c.reload()
        a = c.attrs
        st = a.get("State") or {}
        hc = a.get("HostConfig") or {}
        started = _parse_ts(st.get("StartedAt", ""))
        health = st.get("Health") or {}
        nets = list(((a.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        return {
            "service": (c.labels or {}).get(COMPOSE_SERVICE_LABEL, c.name),
            "container": c.name,
            "state": st.get("Status"),
            "running": bool(st.get("Running")),
            "paused": bool(st.get("Paused")),
            "restarting": bool(st.get("Restarting")),
            "exit_code": st.get("ExitCode"),
            "oom_killed": bool(st.get("OOMKilled")),
            "restart_count": a.get("RestartCount", 0),
            "uptime_s": round(time.time() - started.timestamp()) if started and st.get("Running") else None,
            "finished_at": st.get("FinishedAt") if not st.get("Running") else None,
            "health": health.get("Status"),
            "health_failing_streak": health.get("FailingStreak"),
            "cpu_limit": (hc.get("NanoCpus") or 0) / 1e9 or None,
            "memory_limit_mb": round((hc.get("Memory") or 0) / 2**20) or None,
            "networks": nets,
        }

    def stats(self, c) -> dict[str, Any]:
        s = c.stats(stream=False)
        cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or 1
        mem = s.get("memory_stats") or {}
        usage = mem.get("usage", 0) - ((mem.get("stats") or {}).get("inactive_file", 0))
        limit = mem.get("limit") or 0
        thr, pthr = cpu.get("throttling_data") or {}, pre.get("throttling_data") or {}
        nets = s.get("networks") or {}
        return {
            "cpu_percent": round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 else 0.0,
            "memory_mb": round(usage / 2**20, 1),
            "memory_limit_mb": round(limit / 2**20, 1) if limit else None,
            "memory_percent": round(usage / limit * 100, 1) if limit else None,
            "throttled_periods": thr.get("throttled_periods", 0),
            "throttled_periods_delta": thr.get("throttled_periods", 0) - pthr.get("throttled_periods", 0),
            "net_rx_errors": sum(n.get("rx_errors", 0) for n in nets.values()),
            "net_tx_dropped": sum(n.get("tx_dropped", 0) for n in nets.values()),
            "pids": (s.get("pids_stats") or {}).get("current"),
        }

    def published_port(self, service: str, container_port: int) -> Optional[int]:
        c = self.container(service)
        if c is None:
            return None
        c.reload()
        ports = ((c.attrs.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{container_port}/tcp") or []
        for p in ports:
            if p.get("HostPort"):
                return int(p["HostPort"])
        return None

    def compose_files(self) -> list[str]:
        for c in self.containers().values():
            files = (c.labels or {}).get(COMPOSE_FILES_LABEL)
            if files:
                return [f for f in files.split(",") if f]
        return []


# ---------------------------------------------------------------- topology
@dataclass
class Topology:
    edges: list[tuple[str, str, str]] = field(default_factory=list)   # (caller, callee, evidence)
    roles: dict[str, str] = field(default_factory=dict)

    def callees(self, svc: str) -> list[str]:
        return sorted({d for s, d, _ in self.edges if s == svc})

    def callers(self, svc: str) -> list[str]:
        return sorted({s for s, d, _ in self.edges if d == svc})


QUEUE_RE = ("kafka", "rabbitmq", "nats", "pulsar")


def _deps(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        return list(raw.keys())
    return [str(x) for x in raw or []]


def merge_compose(docs: list[dict]) -> dict:
    """Layer compose files the way `docker compose -f a -f b` does, for what the topology needs: later files add
    services, extend depends_on and environment, and override scalar keys."""
    merged: dict = {"services": {}, "volumes": {}}
    for doc in docs:
        merged["volumes"].update(doc.get("volumes") or {})
        for name, spec in (doc.get("services") or {}).items():
            cur = merged["services"].setdefault(name, {})
            for k, v in (spec or {}).items():
                if k == "depends_on":
                    cur[k] = sorted(set(_deps(cur.get(k))) | set(_deps(v)))
                elif k == "environment":
                    old = cur.get(k) or []
                    old = [f"{a}={b}" for a, b in old.items()] if isinstance(old, dict) else list(old)
                    new = [f"{a}={b}" for a, b in v.items()] if isinstance(v, dict) else list(v or [])
                    cur[k] = old + [x for x in new if x not in old]
                else:
                    cur[k] = v
    return merged


def load_topology(compose_paths: list[str]) -> Topology:
    """Every compose layer merged (base + full + observability), parsed by telemetry.compose. Message brokers count
    as datastores: their consumers fail when they do."""
    from .compose import load_compose, service_nodes, static_edges

    docs = [load_compose(p) for p in compose_paths if Path(p).is_file()]
    if not docs:
        return Topology()
    compose = merge_compose(docs)
    nodes = service_nodes(compose)
    topo = Topology()
    topo.roles = {n: ("datastore" if any(q in n for q in QUEUE_RE) else node.role) for n, node in nodes.items()}
    topo.edges = [(e.src, e.dst, e.evidence) for e in static_edges(compose, nodes)]
    return topo


# ---------------------------------------------------------------- discovery
@dataclass
class Endpoints:
    prometheus: str = ""
    jaeger: str = ""
    opensearch: str = ""
    notes: list[str] = field(default_factory=list)


def discover(settings, docker: DockerReader) -> Endpoints:
    """Configured URLs win; otherwise the published host ports of the running stack; Jaeger also through Envoy."""
    ep = Endpoints(settings.prometheus_url, settings.jaeger_url, settings.opensearch_url)
    try:
        if not ep.prometheus:
            port = docker.published_port("prometheus", 9090)
            ep.prometheus = f"http://127.0.0.1:{port}" if port else ""
        if not ep.jaeger:
            port = docker.published_port("jaeger", 16686)
            ep.jaeger = f"http://127.0.0.1:{port}/jaeger/ui" if port else settings.frontend_url.rstrip("/") + "/jaeger/ui"
        if not ep.opensearch:
            port = docker.published_port("opensearch", 9200)
            ep.opensearch = f"http://127.0.0.1:{port}" if port else ""
    except Exception as exc:  # Docker not running: keep whatever was configured
        ep.notes.append(f"endpoint discovery failed: {exc.__class__.__name__}: {exc}")
    for name in ("prometheus", "jaeger", "opensearch"):
        if not getattr(ep, name):
            ep.notes.append(f"{name}: no endpoint")
    return ep
