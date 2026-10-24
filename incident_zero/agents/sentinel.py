"""Agent 1, the Sentinel: detects incidents and opens them (REQUIREMENTS F1.1–F1.3). No model.

Every `sentinel_interval_s` it evaluates the alert rules on Prometheus span metrics and Docker state:

  error_rate          a service's share of failing server spans above the threshold (and not chronic at baseline)
  latency_p95         p95 above max(floor, factor x baseline)
  traffic_spike       an entry service's request rate above factor x baseline, or the load generator running
                      far more users than normal
  telemetry_silent    a service that served traffic at baseline and now reports none
  container_down      a container that is not running (exited, paused, dead, restarting)
  container_restarts  a restart count that went up, or an OOM kill
  resource_saturation a container's CPU far above its own baseline (docker_stats metrics in Prometheus)

A signal must hold for `confirm_ticks` ticks. The baseline is learned from quiet ticks (an average per service),
falling back to the same query `baseline_s` ago. Alerts join the open incident while it is still being investigated
or was opened within the correlation window; otherwise they open a new one. `evaluate` is also what the Verifier
uses to decide whether an incident's signals have cleared.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..evidence import EvidenceLedger
from ..models import Alert, Incident, iso
from ..telemetry import queries as Q
from ..telemetry.backends import BackendError
from .context import Context

EARLY_PHASES = ("detected", "investigating", "diagnosing", "auditing", "planning")
RULE_LABEL = {"error_rate": "Error rate", "operation_errors": "Operation failing", "latency_p95": "Latency",
              "traffic_spike": "Traffic spike",
              "telemetry_silent": "Telemetry silent", "container_down": "Container down",
              "container_restarts": "Container restarting", "entry_unhealthy": "Entry unhealthy",
              "resource_saturation": "Resource saturation"}


# signals about a container itself, not about what it calls
LOCAL_RULES = ("resource_saturation", "container_down", "container_restarts")


@dataclass
class Signal:
    rule: str
    service: str
    signal: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    baseline: Optional[float] = None
    unit: str = ""
    query: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.service)


def red(ctx: Context, window: str, offset: float = 0) -> dict[str, dict]:
    p = ctx.telemetry.prom
    rps = p.vector(Q.rps(window, offset_s=offset), "service_name")
    err = p.vector(Q.error_rps(window, offset_s=offset), "service_name")
    lat = p.vector(Q.p95(window, offset_s=offset), "service_name")
    return {s: {"rps": rps.get(s, 0.0), "error_ratio": (err.get(s, 0.0) / rps[s]) if rps.get(s) else 0.0,
                "p95": lat.get(s)} for s in set(rps) | set(lat)}


def evaluate(ctx: Context, learned: Optional[dict[str, dict]] = None, window: Optional[str] = None,
             restart_seen: Optional[dict[str, int]] = None, verify: bool = False,
             op_window: Optional[str] = None) -> tuple[list[Signal], dict[str, dict]]:
    """(signals firing now, current RED per service). `verify`: the Verifier's view, where any failure of an
    operation still counts (detection needs op_min_errors over op_window); `op_window` overrides the operation
    window (the Verifier passes the time since the fix: a rare operation shows nothing in a short window)."""
    s, th = ctx.settings, ctx.settings.thresholds
    window = window or s.rate_window
    signals: list[Signal] = []
    try:
        now = red(ctx, window)
        offset_base = red(ctx, window, s.baseline_s)
    except BackendError:
        now, offset_base = {}, {}
    learned = learned or {}
    for svc in sorted(set(now) | set(offset_base)):
        if svc in s.ignore_services:
            continue
        n = now.get(svc, {"rps": 0.0, "error_ratio": 0.0, "p95": None})
        b = learned.get(svc) or offset_base.get(svc) or {}
        q_err = Q.error_rps(window, svc)
        if n["rps"] >= th.min_rps and n["error_ratio"] >= th.error_ratio and b.get("error_ratio", 0) < th.error_ratio:
            signals.append(Signal("error_rate", svc, f"{svc} error_ratio={n['error_ratio']:.3f} over {window} "
                                  f"(threshold {th.error_ratio:.3f}, baseline {b.get('error_ratio', 0):.3f})",
                                  n["error_ratio"], th.error_ratio, b.get("error_ratio"), "ratio", q_err))
        if n.get("p95") and b.get("p95") and n["rps"] >= th.min_rps:
            limit = max(th.latency_p95_floor_ms, th.latency_factor * b["p95"])
            if n["p95"] > limit:
                signals.append(Signal("latency_p95", svc, f"{svc} p95_ms={n['p95']:.0f} over {window} "
                                      f"(threshold {limit:.0f}, baseline {b['p95']:.0f})", n["p95"], limit, b["p95"],
                                      "ms", Q.p95(window, svc)))
        if svc in s.entry_services and b.get("rps", 0) >= th.min_rps and n["rps"] > th.traffic_factor * b["rps"]:
            signals.append(Signal("traffic_spike", svc, f"{svc} rps={n['rps']:.2f} over {window} "
                                  f"(baseline {b['rps']:.2f}, factor {th.traffic_factor:g})", n["rps"],
                                  th.traffic_factor * b["rps"], b["rps"], "req/s", Q.rps(window, svc)))
        if b.get("rps", 0) >= th.silent_min_rps and n["rps"] == 0:
            signals.append(Signal("telemetry_silent", svc, f"{svc} rps=0 over {window} (baseline {b['rps']:.2f})",
                                  0.0, None, b["rps"], "req/s", Q.rps(window, svc)))
    # the traffic source itself (found live: 80 Locust users saturated the host, so the entry services served
    # FEWER requests than at baseline and only latency rose everywhere; the rps rule above can never fire then)
    users = loadgen_users(ctx)
    if users is not None and users > th.traffic_factor * s.normal_load_users:
        signals.append(Signal("traffic_spike", "load-generator", f"load-generator user_count={users} (normal "
                              f"{s.normal_load_users}, factor {th.traffic_factor:g})", float(users),
                              th.traffic_factor * s.normal_load_users, float(s.normal_load_users), "users",
                              "locust /stats/requests user_count"))
    # one operation failing while its service as a whole looks fine (found live: cart's EmptyCart at ~50%, cart ~3%;
    # EmptyCart runs ~2 calls/min, so a 2m window held only 1-2 failures and the alert flickered)
    ow = op_window or (window if verify else s.op_window)
    min_errors = 1.0 if verify else th.op_min_errors
    by = ("service_name", "span_name")
    try:
        op_err = ctx.telemetry.prom.vector_by(Q.op_calls(ow, errors=True), by)
        op_all = ctx.telemetry.prom.vector_by(Q.op_calls(ow), by) if op_err else {}
        op_base_err = ctx.telemetry.prom.vector_by(Q.op_calls(ow, True, s.baseline_s), by) if op_err else {}
        op_base_all = ctx.telemetry.prom.vector_by(Q.op_calls(ow, False, s.baseline_s), by) if op_err else {}
    except BackendError:
        op_err, op_all, op_base_err, op_base_all = {}, {}, {}, {}
    flagged = {sg.service for sg in signals if sg.rule == "error_rate"}
    for (svc, op), errs in sorted(op_err.items()):
        total = op_all.get((svc, op), 0.0)
        if svc in s.ignore_services or svc in flagged or errs < min_errors or total <= 0 or \
                any(x in op for x in s.ignore_operations):
            continue
        ratio = errs / total
        base_total = op_base_all.get((svc, op), 0.0)
        base_ratio = op_base_err.get((svc, op), 0.0) / base_total if base_total else 0.0
        if ratio >= th.op_error_ratio and base_ratio < th.op_error_ratio / 2:
            signals.append(Signal("operation_errors", svc, f"{svc} {op} failed {errs:.0f} of {total:.0f} calls "
                                  f"(error_ratio={ratio:.3f}) over {ow} (baseline {base_ratio:.3f})",
                                  ratio, th.op_error_ratio, base_ratio, "ratio",
                                  f'{Q.op_calls(ow, errors=True)} / {Q.op_calls(ow)}'))
            flagged.add(svc)  # one operation alert per service is enough to open the incident
    # CPU saturation: a fault that slows nothing visible to callers (found live: adHighCpu took ad to 400 % CPU while
    # its latency, traffic and errors did not move, so no other rule fired). Memory is not alerted on here: the Go
    # services swing between 55 and 98 % of their limits on their own; an OOM kill is caught by container_restarts.
    try:
        cpu_now = ctx.telemetry.prom.vector(Q.container_cpu(window), "container_name")
        cpu_base = ctx.telemetry.prom.vector(Q.container_cpu(window, s.baseline_s), "container_name")
    except BackendError:
        cpu_now, cpu_base = {}, {}
    for svc, c in sorted(cpu_now.items()):
        cb = cpu_base.get(svc, 0.0)
        limit = max(th.cpu_saturation_pct, th.cpu_factor * max(cb, 1.0))
        if svc not in s.ignore_services and c >= limit:
            signals.append(Signal("resource_saturation", svc, f"{svc} cpu_percent={c:.0f} over {window} "
                                  f"(threshold {limit:.0f}, baseline {cb:.0f})", c, limit, cb, "% CPU",
                                  Q.container_cpu(window)))
    try:
        containers = ctx.telemetry.docker.containers()
    except Exception:
        containers = {}
    for svc, c in sorted(containers.items()):
        if svc in s.ignore_services:
            continue
        try:
            st = ctx.telemetry.docker.status(c)
        except Exception:
            continue
        if not st["running"] or st["restarting"] or st["paused"]:
            signals.append(Signal("container_down", svc, f"{svc} container state={st['state']} "
                                  f"exit_code={st['exit_code']}", None, None, None, "", "docker inspect"))
        prev = (restart_seen or {}).get(svc)
        if restart_seen is not None:
            restart_seen[svc] = st["restart_count"]
        if (prev is not None and st["restart_count"] > prev) or st["oom_killed"]:
            signals.append(Signal("container_restarts", svc, f"{svc} restarts={st['restart_count']} "
                                  f"oom_killed={str(st['oom_killed']).lower()}", float(st["restart_count"]), None,
                                  float(prev or 0), "restarts", "docker inspect"))
    return signals, now


def loadgen_users(ctx: Context) -> Optional[int]:
    """Users the load generator (Locust) is running now; None when it cannot be asked."""
    import requests

    s = ctx.settings
    url = (s.loadgen_url or s.frontend_url.rstrip("/") + "/loadgen").rstrip("/")
    try:
        return int(requests.get(f"{url}/stats/requests", timeout=5).json()["user_count"])
    except Exception:
        return None


class Sentinel:
    def __init__(self, ctx: Context, on_new_incident: Callable[[str], None]):
        self.ctx, self.on_new_incident = ctx, on_new_incident
        self.pending: dict[tuple[str, str], int] = {}
        self.learned: dict[str, dict] = {}
        self.learn_count: dict[str, int] = {}
        self.restart_seen: dict[str, int] = {}
        self._held: set[tuple[str, tuple[str, str]]] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        last_err = 0.0
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                if time.time() - last_err > 60:
                    self.ctx.bus.publish("sentinel", "error", f"sentinel tick failed: {exc}")
                    last_err = time.time()
            self._stop.wait(self.ctx.settings.sentinel_interval_s)

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> list[Signal]:
        if self.ctx.safety.frozen:
            return []
        with self.ctx.status.working("sentinel", None, "evaluating alert rules"):
            learned = {k: v for k, v in self.learned.items() if self.learn_count.get(k, 0) >= 3}
            signals, now = evaluate(self.ctx, learned, restart_seen=self.restart_seen)
        firing = {sg.key for sg in signals}
        self.pending = {k: self.pending.get(k, 0) + 1 for k in firing}
        confirmed = [sg for sg in signals if self.pending[sg.key] >= self.ctx.settings.confirm_ticks
                     or sg.rule in ("container_down", "container_restarts")]
        self._learn(now, {sg.service for sg in signals})
        self._clear(firing)
        if confirmed:
            self.raise_alerts(confirmed)
        return confirmed

    def _learn(self, now: dict[str, dict], noisy: set[str]) -> None:
        for svc, v in now.items():
            if svc in noisy or not v.get("rps"):
                continue
            prev = self.learned.get(svc)
            if prev is None:
                self.learned[svc] = dict(v)
            else:
                for k in ("rps", "error_ratio", "p95"):
                    if v.get(k) is not None and prev.get(k) is not None:
                        prev[k] = 0.8 * prev[k] + 0.2 * v[k]
                    elif v.get(k) is not None:
                        prev[k] = v[k]
            self.learn_count[svc] = self.learn_count.get(svc, 0) + 1

    def _clear(self, firing: set[tuple[str, str]]) -> None:
        for inc in self.ctx.store.open_incidents():
            if inc.phase not in EARLY_PHASES:
                continue
            changed = False
            for a in inc.alerts:
                if a.active and (a.rule, a.service) not in firing:
                    a.active = False
                    changed = True
                    self.ctx.bus.publish("sentinel", "alert", f"{a.id} cleared: {a.signal}", incident_id=inc.id)
            if changed:
                self.ctx.store.save(inc)

    # ------------------------------------------------------------ incidents
    def _suppressed(self, key: tuple[str, str]) -> bool:
        cutoff = time.time() - self.ctx.settings.realert_after_s
        for inc in self.ctx.store.list():
            if inc.terminal and inc.phase != "resolved" and _ts(inc.updated_at) > cutoff:
                # stay quiet only about what that incident was about: a signal attached to it as a symptom of a
                # different root cause is not "already handled"
                rcs = {rc.component for rc in (inc.diagnosis.root_causes if inc.diagnosis else [])}
                if rcs and key[1] not in rcs:
                    continue
                # a container that goes down again contradicts a "false alarm" verdict (found live: a kill loop
                # was called a false alarm once, and every later kill was then suppressed)
                if inc.phase == "false_alarm" and key[0] in ("container_down", "container_restarts"):
                    continue
                if any((a.rule, a.service) == key for a in inc.alerts):
                    return True
        return False

    def raise_alerts(self, signals: list[Signal]) -> Optional[Incident]:
        store = self.ctx.store
        open_incs = store.open_incidents()
        signals = [sg for sg in signals if not self._suppressed(sg.key)]
        if not signals:
            return None
        # join only an incident still being investigated: once one is planned, a new signal is a new incident
        # (found live: late alerts attached to a verifying incident made it look resolved)
        target = next((i for i in open_incs if i.phase in EARLY_PHASES), None)
        fresh: list[Signal] = []
        for sg in signals:
            owner = next((i for i in open_incs if any((a.rule, a.service) == sg.key for a in i.alerts)), None)
            if owner is not None:
                self._touch(owner, sg)
            else:
                fresh.append(sg)
        if target is None:
            fresh = self._route_symptoms(fresh)
        if not fresh:
            return None
        created = target is None
        inc = target or Incident(id=store.next_incident_id(), title=f"{RULE_LABEL[fresh[0].rule]} on {fresh[0].service}")
        ledger = EvidenceLedger(store, self.ctx.bus, inc.id)
        for sg in fresh:
            a = Alert(id=f"A{len(inc.alerts) + 1}", rule=sg.rule, service=sg.service, signal=sg.signal,  # type: ignore[arg-type]
                      value=sg.value, threshold=sg.threshold, baseline=sg.baseline, unit=sg.unit, query=sg.query)
            inc.alerts.append(a)
            if sg.service not in inc.services:
                inc.services.append(sg.service)
            ledger.add("sentinel", "alert_rule", {"rule": sg.rule, "service": sg.service, "query": sg.query},
                       f"alert {a.id}: {sg.signal}", f"alert {a.id} {sg.rule}: {sg.signal}", agent="sentinel")
            self.ctx.bus.publish("sentinel", "alert", f"{a.id} {RULE_LABEL[sg.rule]}: {sg.signal}", incident_id=inc.id,
                                 detail=a.model_dump())
        store.save(inc)
        if created:
            self.ctx.bus.publish("sentinel", "phase", f"opened {inc.id}: {inc.title}", incident_id=inc.id)
            self.on_new_incident(inc.id)
        return inc

    def _explained_by(self, sg: Signal) -> Optional[Incident]:
        """An already-diagnosed incident whose root cause explains this signal: the signal's service is the root
        cause or calls it (downstream). Open incidents, and incidents resolved within symptom_hold_s."""
        from .diagnostician import _downstream

        topo, now = self.ctx.telemetry.topology, time.time()
        for inc in self.ctx.store.list():
            d = inc.diagnosis
            if not d or not d.root_causes:
                continue
            recent = inc.phase == "resolved" and now - _ts(inc.resolved_at or inc.updated_at) < self.ctx.settings.symptom_hold_s
            if inc.terminal and not recent:
                continue
            # a container's own CPU, state or restarts cannot be caused by a service it calls (found live: ad at
            # 400 % CPU was filed as a symptom of a flagd incident because ad calls flagd)
            reach = {sg.service} if sg.rule in LOCAL_RULES else (_downstream(topo, sg.service) if topo.edges
                                                                  else {sg.service})
            if any(rc.component == sg.service or rc.component in reach for rc in d.root_causes):
                return inc
        return None

    def _route_symptoms(self, fresh: list[Signal]) -> list[Signal]:
        """Found live: one catalog fault opened three incidents (checkout, then frontend twice), each needing its own
        approval of the same fix. Symptoms of a diagnosed root cause join that incident, or stay quiet after it was
        resolved while the rate window catches up; only unexplained signals open a new incident."""
        remaining = []
        for sg in fresh:
            owner = self._explained_by(sg)
            if owner is None:
                remaining.append(sg)
            elif not owner.terminal:
                a = Alert(id=f"A{len(owner.alerts) + 1}", rule=sg.rule, service=sg.service, signal=sg.signal,  # type: ignore[arg-type]
                          value=sg.value, threshold=sg.threshold, baseline=sg.baseline, unit=sg.unit, query=sg.query)
                owner.alerts.append(a)
                self.ctx.store.save(owner)
                self.ctx.bus.publish("sentinel", "alert", f"{a.id} {RULE_LABEL[sg.rule]}: {sg.signal} (a symptom of "
                                     f"the diagnosed root cause; no new incident)", incident_id=owner.id)
            elif (owner.id, sg.key) not in self._held:
                self._held.add((owner.id, sg.key))
                self.ctx.bus.publish("sentinel", "alert", f"held: {sg.signal} is a decaying symptom of {owner.id}, "
                                     f"resolved {owner.resolved_at}", incident_id=owner.id)
        return remaining

    def _touch(self, inc: Incident, sg: Signal) -> None:
        for a in inc.alerts:
            if (a.rule, a.service) == sg.key:
                if not a.active:
                    a.active = True
                    self.ctx.bus.publish("sentinel", "alert", f"{a.id} firing again: {sg.signal}", incident_id=inc.id)
                a.value, a.last_seen = sg.value, iso()
        self.ctx.store.save(inc)


def _ts(s: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
