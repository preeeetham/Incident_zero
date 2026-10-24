"""What every agent is handed: settings, the store and event bus, telemetry, the Action API client, safety, the
model, and the live status board the Control Room's /agents page shows."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ..actions.client import ActionAPIClient
from ..events import EventBus
from ..llm import LLM
from ..models import iso
from ..safety import Safety
from ..store import IncidentStore
from ..telemetry.tools import Telemetry

AGENTS = ("sentinel", "investigator", "diagnostician", "auditor", "planner", "approval", "executor", "verifier",
          "reporter")


@dataclass
class StatusBoard:
    _rows: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        a: {"agent": a, "state": "idle", "incident_id": None, "doing": "", "since": iso(), "runs": 0} for a in AGENTS})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @contextmanager
    def working(self, agent: str, incident_id: Optional[str], doing: str) -> Iterator[None]:
        with self._lock:
            row = self._rows[agent]
            row.update(state="working", incident_id=incident_id, doing=doing, since=iso())
            row["runs"] += 1
        try:
            yield
        finally:
            with self._lock:
                self._rows[agent].update(state="idle", doing="", since=iso())

    def set(self, agent: str, **kv: Any) -> None:
        with self._lock:
            self._rows[agent].update(kv)

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._rows.values()]


@dataclass
class Context:
    settings: Any
    store: IncidentStore
    bus: EventBus
    telemetry: Telemetry
    actions: ActionAPIClient
    safety: Safety
    llm: LLM
    status: StatusBoard = field(default_factory=StatusBoard)

    def services(self) -> set[str]:
        return set(self.telemetry.services())
