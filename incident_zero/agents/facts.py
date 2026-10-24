"""Structured facts parsed back out of evidence content.

The rule-based Diagnostician and Planner reason over these. Every fact keeps the exact line it came from and the
evidence id, so a claim built from a fact quotes the evidence verbatim by construction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from ..models import Evidence


@dataclass
class Fact:
    kind: str           # container | origin | gap | resources | datastore | overview | log | alert | outbound
    service: str
    evidence_id: str
    line: str           # the exact evidence line
    values: dict


RE_CONTAINER = re.compile(r"^(\S+): state=(\S+) exit_code=(\S+) oom_killed=(\S+) restarts=(\d+) uptime_s=(\S+) "
                          r"health=(\S+) networks=(\S+) cpu_limit=(\S+) memory_limit_mb=(\S+)")
RE_ORIGIN = re.compile(r"error origin \(deepest failing span\): (\S+) (\S+): (.*)$")
RE_GAP = re.compile(r"client-server gap: (\S+)->(\S+) (\S+) client_ms=([\d.]+) server_ms=([\d.]+) gap_ms=([\d.]+)")
RE_RES = re.compile(r"^(\S+): cpu_percent=([\d.]+) throttled_periods_delta=(-?\d+) memory_mb=(\S+) "
                    r"memory_limit_mb=(\S+) memory_percent=(\S+)")
RE_MEM_BASE = re.compile(r"memory_baseline_percent=(\S+)")
RE_DS_NONE = re.compile(r"^(\S+): no (\S+)_\* samples at all")
RE_DS_STALE = re.compile(r"^(\S+): newest (\S+)_\* sample (\d+)s ago \(stale")
RE_OVERVIEW = re.compile(r"^(\S+): rps=(\S+) error_ratio=(\S+) p95_ms=(\S+) \| .*<-- (.*)$")
RE_ALERT = re.compile(r"^alert (A\d+) ([a-z_0-9]+): (\S+) (.*)$")
RE_OUTBOUND = re.compile(r"^outbound call errors: (\S+) error_rps=([\d.]+)")
CONNECT_WORDS = re.compile(r"connect|refused|unavailable|timeout|timed out|deadline|dial|no such host|reset|EOF", re.I)


def _num(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def extract(evidence: Iterable[Evidence]) -> list[Fact]:
    out: list[Fact] = []
    for ev in evidence:
        for line in ev.content.splitlines():
            line = line.rstrip()
            if m := RE_CONTAINER.match(line):
                out.append(Fact("container", m[1], ev.id, line, {
                    "state": m[2], "exit_code": m[3], "oom": m[4] == "true", "restarts": int(m[5]),
                    "uptime_s": _num(m[6]), "networks": m[8], "cpu_limit": _num(m[9]), "memory_limit_mb": _num(m[10])}))
            elif m := RE_ORIGIN.search(line):
                out.append(Fact("origin", m[1], ev.id, line, {"operation": m[2], "message": m[3],
                                                              "connectivity": bool(CONNECT_WORDS.search(m[3]))}))
            elif m := RE_GAP.search(line):
                out.append(Fact("gap", m[2], ev.id, line, {"caller": m[1], "operation": m[3],
                                                           "gap_ms": float(m[6])}))
            elif m := RE_RES.match(line):
                out.append(Fact("resources", m[1], ev.id, line, {
                    "cpu": float(m[2]), "throttled": int(m[3]), "memory_mb": _num(m[4]),
                    "memory_limit_mb": _num(m[5]), "memory_percent": _num(m[6]),
                    "memory_baseline": _num(b[1]) if (b := RE_MEM_BASE.search(line)) else None}))
            elif m := RE_DS_NONE.match(line):
                out.append(Fact("datastore", m[1], ev.id, m[0], {"stale_s": None}))
            elif m := RE_DS_STALE.match(line):
                out.append(Fact("datastore", m[1], ev.id, m[0], {"stale_s": int(m[3])}))
            elif m := RE_OVERVIEW.match(line):
                out.append(Fact("overview", m[1], ev.id, line, {"error_ratio": _num(m[3]), "p95": _num(m[4]),
                                                                "notes": m[5]}))
            elif m := RE_ALERT.match(line):
                out.append(Fact("alert", m[3], ev.id, line, {"alert_id": m[1], "rule": m[2], "text": f"{m[3]} {m[4]}"}))
            elif ev.source == "logs" and ev.tool == "search_logs" and line and not line.startswith("("):
                svc = ev.args.get("service", "")
                if svc and not line.startswith("no matching"):
                    out.append(Fact("log", svc, ev.id, line, {}))
            elif m := RE_OUTBOUND.match(line):
                svc = ev.args.get("service", "")
                out.append(Fact("outbound", svc, ev.id, line, {"operation": m[1], "error_rps": float(m[2])}))
    return out


def latest(facts: list[Fact], kind: str, service: str) -> Optional[Fact]:
    found = [f for f in facts if f.kind == kind and f.service == service]
    return found[-1] if found else None
