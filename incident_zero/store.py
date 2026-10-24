"""Durable incident state (REQUIREMENTS N2). One directory per incident, written after every phase:

  <data_dir>/state.json                     live mode (dry_run), frozen flag, approver-independent switches
  <data_dir>/STOP                           the kill switch; present = frozen (see safety.py)
  <data_dir>/events.jsonl                   every event, all incidents, in order (the Control Room's feed)
  <data_dir>/incidents/<id>/incident.json   the Incident model
  <data_dir>/incidents/<id>/evidence.jsonl  the evidence ledger
  <data_dir>/incidents/<id>/report.json     the jury-schema report, once written
  <data_dir>/incidents/<id>/report.md

Writes are atomic (temp file + rename) so a crash never leaves half a JSON file.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import Event, Evidence, Incident, iso


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


class IncidentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "incidents").mkdir(exist_ok=True)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- switches
    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    def state(self) -> dict[str, Any]:
        p = self.root / "state.json"
        try:
            return json.loads(p.read_text()) if p.is_file() else {}
        except json.JSONDecodeError:
            return {}

    def set_state(self, **kv: Any) -> dict[str, Any]:
        with self._lock:
            st = {**self.state(), **kv, "updated_at": iso()}
            _atomic_write(self.root / "state.json", json.dumps(st, indent=2))
            return st

    # ---------------------------------------------------------------- incidents
    def incident_dir(self, incident_id: str) -> Path:
        if not incident_id.replace("-", "").isalnum():
            raise ValueError(f"bad incident id {incident_id!r}")
        return self.root / "incidents" / incident_id

    def next_incident_id(self) -> str:
        with self._lock:
            n = int(self.state().get("incident_seq", 0)) + 1
            self.set_state(incident_seq=n)
            return f"INC-{n:04d}"

    def save(self, inc: Incident) -> Incident:
        with self._lock:
            inc.updated_at = iso()
            _atomic_write(self.incident_dir(inc.id) / "incident.json", inc.model_dump_json(indent=2))
            return inc

    def load(self, incident_id: str) -> Optional[Incident]:
        p = self.incident_dir(incident_id) / "incident.json"
        if not p.is_file():
            return None
        return Incident.model_validate_json(p.read_text())

    def list(self) -> list[Incident]:
        out = []
        for d in sorted((self.root / "incidents").iterdir(), reverse=True):
            inc = self.load(d.name) if d.is_dir() else None
            if inc:
                out.append(inc)
        return out

    def open_incidents(self) -> list[Incident]:
        return [i for i in self.list() if not i.terminal]

    # ---------------------------------------------------------------- evidence
    def append_evidence(self, incident_id: str, ev: Evidence) -> Evidence:
        """Assigns the id (E<n>) here, under the store lock, so concurrent writers (the Sentinel attaching an alert
        while the Investigator runs) never hand out the same id."""
        with self._lock:
            p = self.incident_dir(incident_id) / "evidence.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            n = sum(1 for ln in p.read_text().splitlines() if ln.strip()) if p.is_file() else 0
            ev.id = f"E{n + 1}"
            with p.open("a") as f:
                f.write(ev.model_dump_json() + "\n")
            return ev

    def evidence(self, incident_id: str) -> list[Evidence]:
        p = self.incident_dir(incident_id) / "evidence.jsonl"
        if not p.is_file():
            return []
        return [Evidence.model_validate_json(ln) for ln in p.read_text().splitlines() if ln.strip()]

    # ---------------------------------------------------------------- events
    def append_event(self, ev: Event) -> Event:
        with self._lock:
            st = self.state()
            ev.seq = int(st.get("event_seq", 0)) + 1
            self.set_state(event_seq=ev.seq)
            with (self.root / "events.jsonl").open("a") as f:
                f.write(ev.model_dump_json() + "\n")
            return ev

    def events(self, incident_id: Optional[str] = None, after: int = 0, limit: int = 2000) -> list[Event]:
        p = self.root / "events.jsonl"
        if not p.is_file():
            return []
        out: list[Event] = []
        for ln in p.read_text().splitlines():
            if not ln.strip():
                continue
            ev = Event.model_validate_json(ln)
            if ev.seq <= after or (incident_id and ev.incident_id != incident_id):
                continue
            out.append(ev)
        return out[-limit:]

    # ---------------------------------------------------------------- reports
    def write_report(self, incident_id: str, report: dict, markdown: str) -> None:
        d = self.incident_dir(incident_id)
        _atomic_write(d / "report.json", json.dumps(report, indent=2))
        _atomic_write(d / "report.md", markdown)
        reports = [json.loads((x / "report.json").read_text()) for x in sorted((self.root / "incidents").iterdir())
                   if (x / "report.json").is_file()]
        _atomic_write(self.root / "submission.json", json.dumps({"incident_reports": reports}, indent=2))

    def report(self, incident_id: str) -> tuple[Optional[dict], Optional[str]]:
        d = self.incident_dir(incident_id)
        j = json.loads((d / "report.json").read_text()) if (d / "report.json").is_file() else None
        m = (d / "report.md").read_text() if (d / "report.md").is_file() else None
        return j, m

    def iter_incident_dirs(self) -> Iterator[Path]:
        yield from (d for d in (self.root / "incidents").iterdir() if d.is_dir())
