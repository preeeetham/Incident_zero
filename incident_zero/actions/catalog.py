"""Runbook actions and parameter validation.

The brief names the Action API's actions: restart a service, toggle a feature flag, throttle the load generator,
scale a service (Kubernetes only). MOCK_ACTIONS mirrors exactly those four; parameter names are our guess until the
team's API spec is seen. The Planner never uses this list directly but reads whatever `GET /actions` returns, so
swapping in the real API changes nothing upstream. `validate` is applied by code to every proposed action, whatever
produced it (REQUIREMENTS S5).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ParamDef(BaseModel):
    type: str = "string"            # string | number | integer | boolean
    required: bool = False
    description: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None


class ActionDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, ParamDef] = {}
    reversible: bool = False
    fixes: list[str] = []           # diagnosis categories this action addresses (a hint for the rule planner)


SERVICE = ParamDef(required=True, description="the compose service to act on")

MOCK_ACTIONS: list[ActionDef] = [
    ActionDef(name="restart_service", description="Restart a service's container. Starts a stopped one, unfreezes a "
              "paused one, clears a leaked heap or a stuck process, and resets its network interface (removing "
              "injected latency/loss). Does not change resource limits or network attachments.",
              parameters={"service": SERVICE},
              fixes=["container_down", "dependency_failure", "network", "resource_memory"]),
    ActionDef(name="toggle_feature_flag", description="Turn a feature flag on or off (the demo's switch for a code "
              "path). Use it to switch off the faulty code path a diagnosis points to.",
              parameters={"flag": ParamDef(required=True, description="flag key"),
                          "enabled": ParamDef(type="boolean", required=True, description="false switches it off")},
              reversible=True, fixes=["application_error", "resource_memory", "resource_cpu", "traffic"]),
    ActionDef(name="throttle_load_generator", description="Set the load generator's number of simulated users "
              "(reduce a traffic flood).",
              parameters={"users": ParamDef(type="integer", required=True, minimum=0, maximum=500,
                                            description="concurrent simulated users")},
              reversible=True, fixes=["traffic"]),
    ActionDef(name="scale_service", description="Change a service's replica count (Kubernetes only).",
              parameters={"service": SERVICE,
                          "replicas": ParamDef(type="integer", required=True, minimum=1, maximum=10)},
              reversible=True, fixes=["resource_cpu", "traffic"]),
]


def validate(action: str, params: dict[str, Any], catalog: list[ActionDef],
             services: Optional[set[str]] = None) -> list[str]:
    """Every problem with a proposed action; empty means it may be shown to a human."""
    defs = {a.name: a for a in catalog}
    a = defs.get(action)
    if a is None:
        return [f"{action!r} is not an allowed action (allowed: {', '.join(sorted(defs))})"]
    errs = [f"unexpected parameter {k!r}" for k in params if k not in a.parameters]
    for name, p in a.parameters.items():
        if name not in params or params[name] in (None, ""):
            if p.required:
                errs.append(f"missing required parameter {name!r}")
            continue
        v = params[name]
        if p.type in ("number", "integer"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"{name} must be a {p.type}")
                continue
            if p.type == "integer" and not float(v).is_integer():
                errs.append(f"{name} must be an integer")
            if p.minimum is not None and v < p.minimum:
                errs.append(f"{name} below {p.minimum:g}")
            if p.maximum is not None and v > p.maximum:
                errs.append(f"{name} above {p.maximum:g}")
        elif p.type == "boolean" and not isinstance(v, bool):
            errs.append(f"{name} must be true or false")
        elif p.type == "string" and not isinstance(v, str):
            errs.append(f"{name} must be a string")
    svc = params.get("service")
    if services is not None and isinstance(svc, str) and svc not in services:
        errs.append(f"unknown service {svc!r}")
    return errs
