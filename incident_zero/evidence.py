"""The evidence ledger and the citation audit (REQUIREMENTS F2.3, F3.3, F3.4, R5).

Every tool result is written here, redacted, before any model sees it, and gets an id (E1, E2, ...). A diagnosis
supports each root cause with claims of the form (evidence id, exact quote). The audit is plain code: an id that
does not exist, or a quote that does not appear in that evidence's content, fails the diagnosis. Invented evidence
cannot pass, however confident the model is.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterable, Optional

from .events import EventBus
from .models import Alert, AuditResult, Diagnosis, Evidence
from .redact import redact, redact_text
from .store import IncidentStore

CONTENT_MAX = 6000
DATA_MAX = 20000
MIN_QUOTE = 3


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _trim_data(data: Any) -> Any:
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        return str(data)[:DATA_MAX]
    return data if len(text) <= DATA_MAX else {"truncated": True, "preview": text[:DATA_MAX]}


class EvidenceLedger:
    def __init__(self, store: IncidentStore, bus: EventBus, incident_id: str):
        self.store, self.bus, self.incident_id = store, bus, incident_id
        self._lock = threading.Lock()
        self._items = {e.id: e for e in store.evidence(incident_id)}

    def add(self, source: str, tool: str, args: dict[str, Any], summary: str, content: str, data: Any = None,
            agent: str = "investigator") -> Evidence:
        with self._lock:
            ev = Evidence(
                id="pending", source=source, tool=tool,  # type: ignore[arg-type]
                args=redact(args) or {}, summary=redact_text(summary)[:300],
                content=redact_text(content)[:CONTENT_MAX], data=_trim_data(redact(data)),
            )
            ev = self.store.append_evidence(self.incident_id, ev)  # the store assigns the id
            self._items[ev.id] = ev
        self.bus.publish(agent, "tool", f"{tool}: {ev.summary}", incident_id=self.incident_id,
                         detail={"args": ev.args, "source": source}, evidence_id=ev.id)
        return ev

    def _refresh(self) -> None:
        # other writers (the Sentinel) may have added items for this incident
        self._items = {e.id: e for e in self.store.evidence(self.incident_id)}

    def get(self, evidence_id: str) -> Optional[Evidence]:
        if evidence_id not in self._items:
            self._refresh()
        return self._items.get(evidence_id)

    def all(self) -> list[Evidence]:
        self._refresh()
        return list(self._items.values())

    def index(self) -> str:
        """One line per item: what the Diagnostician is shown before it asks for content."""
        return "\n".join(f"{e.id} [{e.source}/{e.tool}] {e.summary}" for e in self.all())


def quote_in(evidence: Evidence, quote: str) -> bool:
    q = _norm(quote)
    return len(q) >= MIN_QUOTE and q in _norm(evidence.content)


def audit(diagnosis: Diagnosis, evidence: Iterable[Evidence], alerts: Iterable[Alert],
          known_components: Optional[set[str]] = None) -> AuditResult:
    by_id = {e.id: e for e in evidence}
    alerts = list(alerts)
    active = {a.id: a for a in alerts if a.active}
    problems: list[str] = []
    checked = 0
    if diagnosis.false_alarm:
        # nothing to fix: every claim that the system is healthy must still quote real evidence
        if not diagnosis.claims:
            problems.append("a false alarm must cite evidence that the system is healthy")
        for c in diagnosis.claims:
            checked += 1
            ev = by_id.get(c.evidence_id)
            if ev is None:
                problems.append(f"false alarm: cites {c.evidence_id}, which does not exist")
            elif not quote_in(ev, c.quote):
                problems.append(f"false alarm: quote {c.quote[:80]!r} is not in {c.evidence_id}")
        return AuditResult(passed=not problems, problems=problems, checked_claims=checked)
    if not diagnosis.root_causes:
        problems.append("the diagnosis names no root cause")
    for rc in diagnosis.root_causes:
        if known_components is not None and rc.component not in known_components:
            problems.append(f"{rc.component!r} is not a known component")
        if not rc.claims:
            problems.append(f"{rc.component}: no evidence cited")
        for c in rc.claims:
            checked += 1
            ev = by_id.get(c.evidence_id)
            if ev is None:
                problems.append(f"{rc.component}: cites {c.evidence_id}, which does not exist")
            elif not quote_in(ev, c.quote):
                problems.append(f"{rc.component}: quote {c.quote[:80]!r} is not in {c.evidence_id}")
    # explaining an alert that has since cleared is fine; naming one that never existed is not
    known_alerts = {a.id for a in alerts}
    problems += [f"explains {aid}, which is not an alert of this incident"
                 for aid in diagnosis.explained_alerts if aid not in known_alerts]
    unexplained = sorted(set(active) - set(diagnosis.explained_alerts))
    if unexplained:
        problems.append("alerts not explained: " + ", ".join(
            f"{a} ({active[a].rule} on {active[a].service})" for a in unexplained))
    return AuditResult(passed=not problems, problems=problems, unexplained_alerts=unexplained, checked_claims=checked)
