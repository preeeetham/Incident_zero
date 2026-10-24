"""HTTP client for the Action API. The Planner reads the allowed actions and asks for dry-run previews; the
Executor executes and rolls back. Nothing else in Incident Zero can change the system.

Wire format (the mock's; an adapter maps the jury's API onto these four calls):
  GET  /actions                      -> {"actions": [ActionDef...]}
  POST /actions/execute              {action, parameters, reason, dry_run, incident_id}
                                     -> {ok, execution_id, message, dry_run, reversible}
  POST /actions/rollback             {execution_id} -> {ok, message}
  POST /kill | /resume               the API's own kill switch
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from ..models import ActionResult
from .catalog import ActionDef

TIMEOUT = 60.0


class ActionAPIError(RuntimeError):
    pass


class ActionAPIClient:
    def __init__(self, url: str, token: str = "", session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        try:
            r = self.http.request(method, self.url + path, json=body, headers=self.headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ActionAPIError(f"Action API unreachable at {self.url}: {exc.__class__.__name__}") from exc
        try:
            data = r.json()
        except ValueError:
            data = {"message": r.text[:300]}
        if r.status_code >= 400 and not isinstance(data, dict):
            raise ActionAPIError(f"Action API {r.status_code}")
        data.setdefault("status_code", r.status_code)
        return data

    def actions(self) -> list[ActionDef]:
        return [ActionDef.model_validate(a) for a in self._call("GET", "/actions").get("actions", [])]

    def execute(self, action: str, parameters: dict[str, Any], reason: str, dry_run: bool,
                incident_id: str = "") -> ActionResult:
        d = self._call("POST", "/actions/execute", {"action": action, "parameters": parameters, "reason": reason,
                                                    "dry_run": dry_run, "incident_id": incident_id})
        return ActionResult(action=action, parameters=parameters, execution_id=d.get("execution_id"),
                            ok=bool(d.get("ok")) and d["status_code"] < 400, dry_run=dry_run,
                            message=str(d.get("message", "")))

    def rollback(self, execution_id: str) -> ActionResult:
        d = self._call("POST", "/actions/rollback", {"execution_id": execution_id})
        return ActionResult(action="rollback", parameters={"execution_id": execution_id}, execution_id=execution_id,
                            ok=bool(d.get("ok")) and d["status_code"] < 400, dry_run=False,
                            message=str(d.get("message", "")))

    def kill(self) -> None:
        self._call("POST", "/kill", {})

    def resume(self) -> None:
        self._call("POST", "/resume", {})

    def health(self) -> bool:
        try:
            return self._call("GET", "/health").get("ok") is True
        except ActionAPIError:
            return False
