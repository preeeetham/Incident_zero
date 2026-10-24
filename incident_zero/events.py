"""The event bus. Every agent step is an Event: persisted to events.jsonl first, then pushed to live subscribers (the
Control Room's server-sent-events stream). A slow subscriber loses events rather than slowing an agent down; it
catches up from the file by sequence number."""
from __future__ import annotations

import queue
import threading
from typing import Any, Optional

from .models import Event
from .store import IncidentStore


class EventBus:
    def __init__(self, store: IncidentStore):
        self.store = store
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def publish(self, agent: str, kind: str, title: str, incident_id: Optional[str] = None,
                detail: Optional[dict[str, Any]] = None, evidence_id: Optional[str] = None) -> Event:
        ev = self.store.append_event(Event(agent=agent, kind=kind, title=title, incident_id=incident_id,  # type: ignore[arg-type]
                                           detail=detail, evidence_id=evidence_id))
        with self._lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass
        return ev

    def subscribe(self, maxsize: int = 500) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
