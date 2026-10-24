"""The Investigator's tools. Each one queries a backend, records the redacted result in the evidence ledger, and
returns "E<n>: <content>" so a model can only ever cite what was recorded (REQUIREMENTS F2.1, F2.3).

The same registry serves the LLM tool loop and the deterministic playbook, so both investigate with identical
tools. Tools refuse anything that would read feature-flag settings (R1).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from ..config import FLAG_SERVICES
from ..evidence import EvidenceLedger
from ..redact import mentions_flags
from . import queries as Q
from .backends import (BackendError, DockerLogs, DockerReader, Endpoints, Jaeger, Logs, OpenSearchLogs, Prometheus,
                       Topology, discover, load_topology)


# ---------------------------------------------------------------- the assembled backends
class Telemetry:
    def __init__(self, settings, prom: Prometheus, jaeger: Jaeger, logs: Logs, docker: DockerReader,
                 topology: Topology, endpoints: Optional[Endpoints] = None):
        self.settings, self.prom, self.jaeger, self.logs, self.docker = settings, prom, jaeger, logs, docker
        self.topology = topology
        self.endpoints = endpoints or Endpoints(prom.url, jaeger.url, "")

    @classmethod
    def from_settings(cls, settings) -> "Telemetry":
        docker = DockerReader(settings.compose_project)
        ep = discover(settings, docker)
        files = []
        try:
            files = docker.compose_files()
        except Exception:
            pass
        if not files and settings.demo_dir:
            files = [str((settings.path("demo_dir") / f)) for f in settings.compose_files]
        logs = Logs(OpenSearchLogs(ep.opensearch) if ep.opensearch else None, DockerLogs(docker))
        return cls(settings, Prometheus(ep.prometheus), Jaeger(ep.jaeger), logs, docker, load_topology(files), ep)

    def services(self) -> list[str]:
        names = set(self.topology.roles)
        try:
            names |= set(self.docker.containers())
        except Exception:
            pass
        return sorted(names)


# ---------------------------------------------------------------- argument models (also the LLM tool schemas)
class NoArgs(BaseModel):
    pass


class ServiceArg(BaseModel):
    service: str = Field(description="service name as in the compose project, e.g. cart, payment, valkey-cart")


class OptionalServiceArg(BaseModel):
    service: Optional[str] = Field(None, description="one service, or omit for every container")


class PromQLArgs(BaseModel):
    query: str = Field(description="a PromQL instant query")


class FindTracesArgs(BaseModel):
    service: str = Field(description="service whose traces to search")
    errors_only: bool = Field(True, description="only traces containing an error span")
    min_duration_ms: Optional[float] = Field(None, description="only traces at least this slow")
    operation: Optional[str] = Field(None, description="span name to filter on")
    limit: int = Field(5, ge=1, le=20)


class TraceArgs(BaseModel):
    trace_id: str = Field(description="full trace id from find_traces")


class LogArgs(BaseModel):
    service: str = Field(description="service whose logs to search")
    contains: Optional[str] = Field(None, description="only lines containing this text")
    errors_only: bool = Field(True, description="only error/warning lines")
    minutes: float = Field(10, ge=1, le=120)
    limit: int = Field(20, ge=1, le=50)


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[..., str]


def _f(v: Optional[float], digits: int = 3) -> str:
    return "none" if v is None else f"{v:.{digits}f}"


# ---------------------------------------------------------------- trace summarising (pure, tested)
def _tags(span: dict) -> dict[str, Any]:
    return {t.get("key"): t.get("value") for t in span.get("tags") or []}


def _is_error(tags: dict) -> bool:
    return str(tags.get("error")).lower() == "true" or tags.get("otel.status_code") == "ERROR"


def _error_message(span: dict, tags: dict) -> str:
    msg = tags.get("otel.status_description") or ""
    for log in span.get("logs") or []:
        fields = {f.get("key"): f.get("value") for f in log.get("fields") or []}
        if fields.get("exception.message"):
            return str(fields["exception.message"])[:200]
    return str(msg)[:200]


def summarize_trace(trace: dict) -> tuple[str, dict]:
    spans = trace.get("spans") or []
    procs = trace.get("processes") or {}
    by_id = {s["spanID"]: s for s in spans}
    svc = {s["spanID"]: (procs.get(s.get("processID")) or {}).get("serviceName", "?") for s in spans}
    tags = {s["spanID"]: _tags(s) for s in spans}
    parent: dict[str, Optional[str]] = {}
    children: dict[str, list[str]] = {s["spanID"]: [] for s in spans}
    for s in spans:
        p = next((r["spanID"] for r in s.get("references") or [] if r.get("refType") == "CHILD_OF"
                  and r.get("spanID") in by_id), None)
        parent[s["spanID"]] = p
        if p:
            children[p].append(s["spanID"])
    roots = [s for s in spans if not parent[s["spanID"]]]
    root = min(roots, key=lambda s: s.get("startTime", 0)) if roots else (spans[0] if spans else None)
    lines: list[str] = []
    if root is None:
        return "empty trace", {}
    rid = root["spanID"]
    lines.append(f"trace {trace.get('traceID')} root={svc[rid]} {root.get('operationName')} "
                 f"duration_ms={root.get('duration', 0) / 1000:.1f} spans={len(spans)} "
                 f"services={','.join(sorted(set(svc.values())))}")
    errors = [s for s in spans if _is_error(tags[s["spanID"]])]
    for s in sorted(errors, key=lambda s: s.get("startTime", 0))[:8]:
        sid = s["spanID"]
        lines.append(f"error span: {svc[sid]} {s.get('operationName')} duration_ms={s.get('duration', 0) / 1000:.1f} "
                     f"message={_error_message(s, tags[sid]) or '-'}")
    origins = [s for s in errors if not any(_is_error(tags[c]) for c in children[s["spanID"]])]
    for s in origins[:3]:
        sid = s["spanID"]
        lines.append(f"error origin (deepest failing span): {svc[sid]} {s.get('operationName')}: "
                     f"{_error_message(s, tags[sid]) or 'no message'}")
    self_time = []
    for s in spans:
        sid = s["spanID"]
        kids = sum(by_id[c].get("duration", 0) for c in children[sid])
        self_time.append((max(s.get("duration", 0) - kids, 0) / 1000, s))
    for ms, s in sorted(self_time, key=lambda x: -x[0])[:3]:
        lines.append(f"slow span (self time): {svc[s['spanID']]} {s.get('operationName')} self_ms={ms:.1f}")
    gaps = []
    for s in spans:
        sid = s["spanID"]
        if tags[sid].get("span.kind") != "client":
            continue
        for c in children[sid]:
            if tags[c].get("span.kind") == "server" and svc[c] != svc[sid]:
                gap = (s.get("duration", 0) - by_id[c].get("duration", 0)) / 1000
                gaps.append((gap, svc[sid], svc[c], s.get("operationName"), s.get("duration", 0) / 1000,
                             by_id[c].get("duration", 0) / 1000))
    for gap, a, b, op, cms, sms in sorted(gaps, key=lambda g: -g[0])[:3]:
        if gap >= 1:
            lines.append(f"client-server gap: {a}->{b} {op} client_ms={cms:.1f} server_ms={sms:.1f} gap_ms={gap:.1f}")
    data = {"trace_id": trace.get("traceID"), "error_services": sorted({svc[s['spanID']] for s in errors}),
            "origins": [svc[s["spanID"]] for s in origins]}
    return "\n".join(lines), data


# ---------------------------------------------------------------- the toolbox
class Toolbox:
    def __init__(self, telemetry: Telemetry, ledger: EvidenceLedger, agent: str = "investigator"):
        self.t, self.ledger, self.agent = telemetry, ledger, agent
        s = telemetry.settings
        self.window, self.baseline_offset = s.rate_window, s.baseline_s
        self.specs: dict[str, ToolSpec] = {sp.name: sp for sp in [
            ToolSpec("overview", "Every service's request rate, error ratio and p95 latency now versus baseline, plus "
                     "its container state. Start here.", NoArgs, self.overview),
            ToolSpec("service_metrics", "One service in depth: request rate, error ratio and p95 now vs baseline, "
                     "errors by operation, and errors/latency on the calls it makes to its dependencies.",
                     ServiceArg, self.service_metrics),
            ToolSpec("find_traces", "Search Jaeger for recent traces of a service (errors only by default, or slow ones).",
                     FindTracesArgs, self.find_traces),
            ToolSpec("trace_detail", "One trace: error spans, the deepest failing span, slowest spans, and client-to-"
                     "server latency gaps (network delay shows as a large gap).", TraceArgs, self.trace_detail),
            ToolSpec("search_logs", "Recent log lines of a service (OpenSearch, falling back to container logs).",
                     LogArgs, self.search_logs),
            ToolSpec("container_status", "Docker state of one or all containers: running/exited/paused, exit code, "
                     "OOM kill, restart count, uptime, health, networks attached.", OptionalServiceArg,
                     self.container_status),
            ToolSpec("container_resources", "Live CPU %, CPU throttling, memory use vs limit, network errors of one "
                     "container.", ServiceArg, self.container_resources),
            ToolSpec("dependencies", "Which services a service calls and is called by (from the compose file).",
                     ServiceArg, self.dependencies),
            ToolSpec("datastore_metrics", "Health metrics of a cache/database/queue (valkey-cart, astronomy-db, kafka): "
                     "freshness of its receiver's samples and key values.", ServiceArg, self.datastore_metrics),
            ToolSpec("promql", "Run any PromQL instant query (span metrics: traces_span_metrics_calls_total, "
                     "traces_span_metrics_duration_milliseconds_bucket; labels service_name, span_name, span_kind, "
                     "status_code).", PromQLArgs, self.promql),
        ]}

    # ------------------------------------------------------------ dispatch
    def call(self, name: str, args: Optional[dict] = None) -> str:
        spec = self.specs.get(name)
        if spec is None:
            return f"unknown tool {name!r}; available: {', '.join(self.specs)}"
        try:
            parsed = spec.args.model_validate(args or {})
        except Exception as exc:
            return f"bad arguments for {name}: {exc}"
        return spec.fn(**parsed.model_dump())

    def _record(self, source: str, tool: str, args: dict, summary: str, content: str, data: Any = None) -> str:
        ev = self.ledger.add(source, tool, args, summary, content, data, agent=self.agent)
        return f"{ev.id}: {ev.content}"

    def _failed(self, source: str, tool: str, args: dict, exc: Exception) -> str:
        return self._record(source, tool, args, f"{tool} failed: {exc}", f"{tool} failed: {exc}")

    # ------------------------------------------------------------ tools
    def _red(self, service: Optional[str] = None, offset: float = 0) -> dict[str, dict[str, float]]:
        p = self.t.prom
        rps = p.vector(Q.rps(self.window, service, offset), "service_name")
        err = p.vector(Q.error_rps(self.window, service, offset), "service_name")
        lat = p.vector(Q.p95(self.window, service, offset), "service_name")
        out = {}
        for svc in set(rps) | set(lat):
            r = rps.get(svc, 0.0)
            out[svc] = {"rps": r, "error_ratio": (err.get(svc, 0.0) / r) if r > 0 else 0.0, "p95_ms": lat.get(svc)}
        return out

    def overview(self) -> str:
        args: dict = {}
        th = self.t.settings.thresholds
        try:
            now, base = self._red(), self._red(offset=self.baseline_offset)
            silent = self.t.prom.vector(Q.last_seen(), "service_name")
        except BackendError as exc:
            now, base, silent = {}, {}, {}
            prom_note = f"metrics unavailable: {exc}"
        else:
            prom_note = ""
        states: dict[str, dict] = {}
        try:
            states = {svc: self.t.docker.status(c) for svc, c in self.t.docker.containers().items()}
        except Exception as exc:
            prom_note += f" containers unavailable: {exc.__class__.__name__}"
        lines, flagged = [], []
        for svc in sorted(set(now) | set(base) | set(states)):
            n, b, st = now.get(svc, {}), base.get(svc, {}), states.get(svc, {})
            notes = []
            if n and n["rps"] >= th.min_rps and n["error_ratio"] >= th.error_ratio:
                notes.append("high error ratio")
            if n.get("p95_ms") and b.get("p95_ms") and n["p95_ms"] > max(th.latency_p95_floor_ms,
                                                                       th.latency_factor * b["p95_ms"]):
                notes.append("latency above baseline")
            if b.get("rps", 0) > th.min_rps and not n.get("rps"):
                notes.append("no requests now")
            if st and not st.get("running"):
                notes.append(f"container {st.get('state')}")
            if st.get("restart_count"):
                notes.append(f"restarted {st['restart_count']}x")
            if st.get("oom_killed"):
                notes.append("OOM killed")
            if svc in silent and silent[svc] > th.silence_after_s:
                notes.append(f"no spans for {silent[svc]:.0f}s")
            line = (f"{svc}: rps={_f(n.get('rps'), 2)} error_ratio={_f(n.get('error_ratio'))} "
                    f"p95_ms={_f(n.get('p95_ms'), 0)} | baseline rps={_f(b.get('rps'), 2)} "
                    f"error_ratio={_f(b.get('error_ratio'))} p95_ms={_f(b.get('p95_ms'), 0)} | "
                    f"container={st.get('state', 'unknown')} restarts={st.get('restart_count', '-')}")
            if notes and svc not in self.t.settings.ignore_services:
                line += "  <-- " + "; ".join(notes)
                flagged.append(svc)
            lines.append(line)
        if prom_note:
            lines.append(prom_note.strip())
        summary = f"overview: {len(lines)} services, anomalies in {', '.join(flagged) or 'none'}"
        return self._record("prometheus", "overview", args, summary, "\n".join(lines),
                            {"flagged": flagged, "now": now, "baseline": base})

    def service_metrics(self, service: str) -> str:
        args = {"service": service}
        try:
            now = self._red(service).get(service, {})
            base = self._red(service, self.baseline_offset).get(service, {})
            by_op = self.t.prom.vector(Q.error_rps(self.window, service, by="span_name"), "span_name")
            out_err = self.t.prom.vector(Q.client_error_rps(self.window, service), "span_name")
            out_p95 = self.t.prom.vector(Q.client_p95(self.window, service), "span_name")
        except BackendError as exc:
            return self._failed("prometheus", "service_metrics", args, exc)
        lines = [f"{service} now: rps={_f(now.get('rps'), 2)} error_ratio={_f(now.get('error_ratio'))} "
                 f"p95_ms={_f(now.get('p95_ms'), 0)}",
                 f"{service} baseline: rps={_f(base.get('rps'), 2)} error_ratio={_f(base.get('error_ratio'))} "
                 f"p95_ms={_f(base.get('p95_ms'), 0)}"]
        for op, v in sorted(by_op.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"errors by operation: {op} error_rps={v:.3f}")
        for op, v in sorted(out_err.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"outbound call errors: {op} error_rps={v:.3f}")
        for op, v in sorted(out_p95.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"outbound call latency: {op} p95_ms={v:.0f}")
        if not now:
            lines.append(f"no inbound span metrics for {service} in the last {self.window}")
        return self._record("prometheus", "service_metrics", args,
                            f"{service}: error_ratio={_f(now.get('error_ratio'))} p95_ms={_f(now.get('p95_ms'), 0)}",
                            "\n".join(lines), {"now": now, "baseline": base, "errors_by_operation": by_op,
                                               "outbound_errors": out_err, "outbound_p95": out_p95})

    def promql(self, query: str) -> str:
        args = {"query": query}
        if mentions_flags(query):
            return "refused: queries about feature flags are not allowed; reason from service telemetry"
        try:
            res = self.t.prom.query(query)
        except BackendError as exc:
            return self._failed("prometheus", "promql", args, exc)
        lines = []
        for item in res[:30]:
            labels = ",".join(f"{k}={v}" for k, v in sorted(item.get("metric", {}).items()) if k != "__name__")
            name = item.get("metric", {}).get("__name__", "")
            val = item.get("value", [None, "?"])[1]
            lines.append(f"{name}{{{labels}}} = {val}")
        if len(res) > 30:
            lines.append(f"... {len(res) - 30} more series")
        return self._record("prometheus", "promql", args, f"promql: {len(res)} series", "\n".join(lines) or "no data",
                            res[:30])

    def find_traces(self, service: str, errors_only: bool = True, min_duration_ms: Optional[float] = None,
                    operation: Optional[str] = None, limit: int = 5) -> str:
        args = {"service": service, "errors_only": errors_only, "min_duration_ms": min_duration_ms,
                "operation": operation, "limit": limit}
        try:
            traces = self.t.jaeger.find_traces(service, operation, errors_only, min_duration_ms,
                                               lookback_s=max(self.t.settings.baseline_s, 600), limit=limit)
        except BackendError as exc:
            return self._failed("jaeger", "find_traces", args, exc)
        from ..redact import redact

        lines = []
        for tr in traces[:limit]:
            content, data = summarize_trace(redact(tr))
            first = content.splitlines()[0] if content else ""
            lines.append(f"{first} errors_in={','.join(data.get('error_services', [])) or 'none'} "
                         f"origin={','.join(data.get('origins', [])) or 'none'}")
        return self._record("jaeger", "find_traces", args, f"{len(traces)} traces for {service}",
                            "\n".join(lines) or f"no matching traces for {service}")

    def trace_detail(self, trace_id: str) -> str:
        args = {"trace_id": trace_id}
        try:
            tr = self.t.jaeger.trace(trace_id)
        except BackendError as exc:
            return self._failed("jaeger", "trace_detail", args, exc)
        if not tr:
            return self._record("jaeger", "trace_detail", args, f"trace {trace_id} not found", "trace not found")
        from ..redact import redact

        content, data = summarize_trace(redact(tr))
        return self._record("jaeger", "trace_detail", args, f"trace {trace_id[:12]}: origin "
                            f"{','.join(data.get('origins', [])) or 'none'}", content, data)

    def search_logs(self, service: str, contains: Optional[str] = None, errors_only: bool = True,
                    minutes: float = 10, limit: int = 20) -> str:
        args = {"service": service, "contains": contains, "errors_only": errors_only, "minutes": minutes}
        if service in FLAG_SERVICES or (contains and mentions_flags(contains)):
            return "refused: feature-flag services and flag names are off limits"
        try:
            source, lines, note = self.t.logs.search(service, contains, errors_only, minutes, limit)
        except BackendError as exc:
            return self._failed("logs", "search_logs", args, exc)
        except Exception as exc:  # docker errors
            return self._failed("logs", "search_logs", args, exc)
        text = "\n".join(f"{ln.ts} {ln.service} {ln.severity} {ln.body[:300]}".replace("  ", " ") for ln in lines)
        return self._record("logs", "search_logs", {**args, "source": source},
                            f"{len(lines)} {'error ' if errors_only else ''}log lines from {service} ({source})",
                            (text or f"no matching log lines from {service}") + (f"\n({note})" if note else ""))

    def container_status(self, service: Optional[str] = None) -> str:
        args = {"service": service}
        try:
            cs = self.t.docker.containers()
            if service:
                cs = {service: cs[service]} if service in cs else {}
            rows = [self.t.docker.status(c) for c in cs.values()]
        except Exception as exc:
            return self._failed("docker", "container_status", args, exc)
        if service and not rows:
            return self._record("docker", "container_status", args, f"{service}: no container",
                                f"{service}: no container exists in project {self.t.settings.compose_project}")
        lines = [f"{r['service']}: state={r['state']} exit_code={r['exit_code']} oom_killed={str(r['oom_killed']).lower()} "
                 f"restarts={r['restart_count']} uptime_s={r['uptime_s']} health={r['health']} "
                 f"networks={','.join(r['networks']) or 'none'} cpu_limit={r['cpu_limit']} "
                 f"memory_limit_mb={r['memory_limit_mb']}" for r in sorted(rows, key=lambda r: r["service"])]
        bad = [r["service"] for r in rows if not r["running"] or r["restart_count"] or r["oom_killed"] or not r["networks"]]
        return self._record("docker", "container_status", args,
                            f"{len(rows)} containers, problems in {', '.join(bad) or 'none'}", "\n".join(lines), rows)

    def container_resources(self, service: str) -> str:
        args = {"service": service}
        try:
            c = self.t.docker.container(service)
            if c is None:
                return self._record("docker", "container_resources", args, f"{service}: no container",
                                    f"{service}: no container")
            st = self.t.docker.stats(c)
        except Exception as exc:
            return self._failed("docker", "container_resources", args, exc)
        # the container's own normal memory share (found live: checkout swings between 55 and 98 % of its limit with
        # no fault, so "87 % of the limit" alone was diagnosed as a memory problem)
        try:
            mb = self.t.prom.vector(Q.container_memory_baseline(service, self.t.settings.baseline_s), "container_name")
            base = round(mb[service], 1) if service in mb else None
        except Exception:
            base = None
        content = (f"{service}: cpu_percent={st['cpu_percent']} throttled_periods_delta={st['throttled_periods_delta']} "
                   f"memory_mb={st['memory_mb']} memory_limit_mb={st['memory_limit_mb']} "
                   f"memory_percent={st['memory_percent']} net_rx_errors={st['net_rx_errors']} "
                   f"net_tx_dropped={st['net_tx_dropped']} pids={st['pids']} memory_baseline_percent={base}")
        return self._record("docker", "container_resources", args,
                            f"{service}: cpu {st['cpu_percent']}%, memory {st['memory_percent']}% of limit", content, st)

    def dependencies(self, service: str) -> str:
        topo = self.t.topology
        content = (f"{service} calls: {', '.join(topo.callees(service)) or 'nothing known'}\n"
                   f"{service} is called by: {', '.join(topo.callers(service)) or 'nothing known'}")
        return self._record("topology", "dependencies", {"service": service}, f"dependencies of {service}", content)

    def datastore_metrics(self, service: str) -> str:
        args = {"service": service}
        prefix = Q.datastore_prefix(service)
        if not prefix:
            return f"{service} is not a known datastore (valkey-cart, astronomy-db, kafka)"
        try:
            # timestamp() drops __name__, so series differing only by name collide (Prometheus 3 rejects the query):
            # copy the name into a label first
            ages = self.t.prom.vector(f'time() - max by (metric) (timestamp(label_replace({{__name__=~"{prefix}_.*"}}, '
                                      f'"metric", "$1", "__name__", "(.+)")))', "metric")
        except BackendError as exc:
            return self._failed("prometheus", "datastore_metrics", args, exc)
        if not ages:
            content = f"{service}: no {prefix}_* samples at all (its receiver has never reached it, or it is down)"
        else:
            newest = min(ages.values())
            stale = newest > 120
            lines = [f"{service}: newest {prefix}_* sample {newest:.0f}s ago"
                     + (" (stale: the receiver cannot reach it)" if stale else "")]
            try:
                vals = self.t.prom.vector(f'{{__name__=~"{prefix}_(clients_connected|connected_clients|memory_used_bytes|'
                                          f'uptime_seconds_total|backends|keys_evicted_total|rejected_connections_total)"}}',
                                          "__name__")
                lines += [f"{k}={v:g}" for k, v in sorted(vals.items())]
            except BackendError:
                pass
            content = "\n".join(lines)
        return self._record("prometheus", "datastore_metrics", args, content.splitlines()[0], content, ages)

    # ------------------------------------------------------------ for the Control Room
    def snapshot(self) -> dict[str, Any]:
        """Services view, without recording evidence."""
        out: dict[str, Any] = {"at": time.time(), "services": {}, "error": None}
        try:
            now, base = self._red(), self._red(offset=self.baseline_offset)
        except BackendError as exc:
            now, base, out["error"] = {}, {}, str(exc)
        states = {}
        try:
            states = {svc: self.t.docker.status(c) for svc, c in self.t.docker.containers().items()}
        except Exception as exc:
            out["error"] = (out["error"] or "") + f" docker: {exc.__class__.__name__}"
        for svc in sorted(set(now) | set(states)):
            out["services"][svc] = {"now": now.get(svc), "baseline": base.get(svc), "container": states.get(svc),
                                    "callees": self.t.topology.callees(svc)}
        return out
