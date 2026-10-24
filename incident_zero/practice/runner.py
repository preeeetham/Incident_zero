"""Practice runs: start a scenario, watch for the incidents it causes, revert, score.

Runs and their ground truth live in practice_dir (never in data_dir, never on the event bus), so nothing an agent
can read mentions what was injected (REQUIREMENTS R3). A run is reverted and judged when every incident it caused
has closed, or when its time runs out.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..store import IncidentStore
from .judge import score
from .simulator import Simulator, load_scenarios, scenario

RUN_RE = re.compile(r"^/api/practice/runs/(P-[0-9a-f]+)/stop$")


class Practice:
    def __init__(self, settings, bus: Any = None, simulator: Optional[Simulator] = None,
                 store: Optional[IncidentStore] = None, watch: bool = True):
        self.settings = settings
        self.dir = Path(settings.practice_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "runs.json"
        self.scenarios = load_scenarios()
        self.sim = simulator or Simulator(settings)
        self.store = store or IncidentStore(settings.data_dir)
        self._lock = threading.RLock()
        self.runs: dict[str, dict] = json.loads(self.file.read_text()) if self.file.is_file() else {}
        if watch:
            threading.Thread(target=self._watch, daemon=True, name="practice-watch").start()

    def _save(self) -> None:
        self.file.write_text(json.dumps(self.runs, indent=2, default=str))

    # ------------------------------------------------------------ API
    def handle(self, method: str, path: str, body: Optional[dict]) -> tuple[int, Any]:
        if method == "GET" and path == "/api/practice/scenarios":
            shown = [{"id": s["id"], "title": s["title"], "split": s.get("split", "tune"),
                      "faults": [f["kind"] for f in s["faults"]], "injects": s.get("injects", ""),
                       "effect": s.get("effect", "")} for s in self.scenarios if s.get("verified")]
            # only the scenarios verified live (10/10) are offered; the rest and the blind set still run by id
            # (`practice run <id>`, `practice run blind`, campaigns)
            return 200, shown
        if method == "GET" and path == "/api/practice/runs":
            with self._lock:
                return 200, [self._public(r) for r in sorted(self.runs.values(), key=lambda r: -r["started_at"])]
        if method == "POST" and path == "/api/practice/run":
            body = body or {}
            try:
                return 200, self._public(self.start(str(body.get("scenario_id", "")),
                                                    float(body.get("duration_s") or 600)))
            except (KeyError, ValueError, RuntimeError) as exc:
                return 400, {"error": str(exc)}
        m = RUN_RE.match(path)
        if method == "POST" and m:
            r = self.finish(m.group(1), "stopped by operator")
            return (200, self._public(r)) if r else (404, {"error": "no such run"})
        return 404, {"error": "not found"}

    def _public(self, r: dict) -> dict:
        out = {k: v for k, v in r.items() if k not in ("undo", "truth")}
        if r["status"] != "active":
            out["truth"] = r["truth"]  # revealed to the operator only after scoring
        elif r.get("split") == "blind":
            out.update(scenario_id="blind", title="hidden until it is scored")
        return out

    # ------------------------------------------------------------ runs
    def start(self, scenario_id: str, duration_s: float = 600) -> dict:
        if scenario_id == "blind":
            blind = [s for s in self.scenarios if s.get("split") == "blind"]
            if not blind:
                raise KeyError("no blind scenarios")
            scenario_id = random.choice(blind)["id"]
        sc = scenario(self.scenarios, scenario_id)
        if sc is None:
            raise KeyError(f"unknown scenario {scenario_id!r}")
        if any(f["kind"] == "flag" for f in sc["faults"]) and not self.settings.demo_dir:
            raise ValueError("flag scenarios need demo_dir (the demo checkout) in incident_zero.yaml")
        with self._lock:
            if any(r["status"] == "active" for r in self.runs.values()):
                raise RuntimeError("another practice run is active; stop it first")
            run_id = f"P-{uuid.uuid4().hex[:8]}"
            started = time.time()
            undo = self.sim.inject(run_id, sc, duration_s)
            self.runs[run_id] = {"id": run_id, "scenario_id": sc["id"], "title": sc["title"],
                                 "split": sc.get("split", "tune"), "started_at": started,
                                 "deadline": started + duration_s, "status": "active", "undo": undo,
                                 "truth": sc["truth"], "score": None, "notes": []}
            self._save()
            return self.runs[run_id]

    def finish(self, run_id: str, why: str) -> Optional[dict]:
        with self._lock:
            r = self.runs.get(run_id)
            if r is None:
                return None
            if r["status"] == "active":
                r["notes"] = self.sim.revert(r["undo"]) + [why]
                r["status"] = "reverted"
                r["ended_at"] = time.time()
            r["score"] = score(r["truth"], r["started_at"], self.store.list())
            self._save()
            return r

    def _watch(self) -> None:
        while True:
            time.sleep(5)
            try:
                for r in [r for r in self.runs.values() if r["status"] == "active"]:
                    incs = [i for i in self.store.list() if _created(i) >= r["started_at"] - 5]
                    if time.time() > r["deadline"]:
                        self.finish(r["id"], "time limit reached")
                    elif incs and all(i.terminal for i in incs) and time.time() - r["started_at"] > 60:
                        self.finish(r["id"], "every incident it caused has closed")
            except Exception:
                pass


def _created(inc) -> float:
    from .judge import _ts

    return _ts(inc.created_at)
