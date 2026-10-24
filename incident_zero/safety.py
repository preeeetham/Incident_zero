"""The kill switch (REQUIREMENTS S2, S3) and the live/dry-run mode (S1).

"Stop all" = write the STOP file (every agent checks it between steps and the Executor before every call), tell the
Action API to refuse executions, then roll back every reversible action executed within kill_rollback_window_s.
It stays engaged until an explicit resume. The STOP file works even when the server is down: `incident_zero stop`
writes it directly.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from .actions.client import ActionAPIClient, ActionAPIError
from .events import EventBus
from .models import ActionResult, iso
from .store import IncidentStore


class Frozen(RuntimeError):
    """Raised inside an agent when the kill switch is engaged."""


class Safety:
    def __init__(self, store: IncidentStore, bus: EventBus, actions: ActionAPIClient, settings):
        self.store, self.bus, self.actions, self.settings = store, bus, actions, settings
        if "dry_run" not in store.state():
            store.set_state(dry_run=bool(settings.dry_run))

    # ------------------------------------------------------------ mode
    @property
    def dry_run(self) -> bool:
        return bool(self.store.state().get("dry_run", True))

    def set_dry_run(self, value: bool, actor: str) -> None:
        self.store.set_state(dry_run=bool(value))
        self.bus.publish("human", "system", f"{actor} switched to {'dry run' if value else 'LIVE'} mode",
                         detail={"dry_run": bool(value), "actor": actor})

    # ------------------------------------------------------------ kill switch
    @property
    def frozen(self) -> bool:
        return self.store.stop_file.exists()

    def check(self) -> None:
        if self.frozen:
            raise Frozen("kill switch engaged")

    def stop(self, actor: str, reason: str = "") -> list[ActionResult]:
        self.store.stop_file.write_text(f"{iso()} {actor}: {reason}\n")
        self.bus.publish("human", "system", f"STOP ALL by {actor}" + (f": {reason}" if reason else ""),
                         detail={"actor": actor})
        try:
            self.actions.kill()
        except ActionAPIError as exc:
            self.bus.publish("system", "error", f"could not reach the Action API kill switch: {exc}")
        return self._rollback_recent()

    def resume(self, actor: str) -> None:
        if self.store.stop_file.exists():
            self.store.stop_file.unlink()
        try:
            self.actions.resume()
        except ActionAPIError as exc:
            self.bus.publish("system", "error", f"could not resume the Action API: {exc}")
        self.bus.publish("human", "system", f"resumed by {actor}", detail={"actor": actor})

    def _rollback_recent(self) -> list[ActionResult]:
        cutoff = time.time() - self.settings.kill_rollback_window_s
        done: list[ActionResult] = []
        for inc in self.store.list():
            changed = False
            for r in inc.execution.results:
                if r.dry_run or not r.ok or not r.execution_id or _ts(r.at) < cutoff:
                    continue
                if any(rb.execution_id == r.execution_id for rb in inc.execution.rollback_results):
                    continue
                try:
                    rb = self.actions.rollback(r.execution_id)
                except ActionAPIError as exc:
                    rb = ActionResult(action="rollback", execution_id=r.execution_id, ok=False, dry_run=False,
                                      message=str(exc))
                inc.execution.rollback_results.append(rb)
                inc.execution.rollback_performed = inc.execution.rollback_performed or rb.ok
                changed = True
                done.append(rb)
                self.bus.publish("executor", "action", f"kill switch rollback of {r.action}: {rb.message}",
                                 incident_id=inc.id, detail=rb.model_dump())
            if changed:
                self.store.save(inc)
        return done


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def guard(safety: Optional[Safety]) -> None:
    if safety is not None:
        safety.check()
