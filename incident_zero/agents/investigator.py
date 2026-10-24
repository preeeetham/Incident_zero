"""Agent 2, the Investigator: chooses the next telemetry tool from what it has found so far (REQUIREMENTS F2.2).

With a model: a LangGraph loop of two nodes, `think` (the model picks tool calls) and `act` (the Toolbox runs them,
each result becomes evidence), until the model calls `finish_investigation` or the budget runs out. The kill switch
is checked before every tool call.

Without a model, or if the model fails mid-way: a deterministic playbook that follows the same method with the same
tools, continuing on the same evidence ledger.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..evidence import EvidenceLedger
from ..models import Incident
from ..telemetry.tools import Toolbox
from .context import Context

SYSTEM = """You are the on-call engineer for the OpenTelemetry Demo (the Astronomy Shop), a set of microservices
under Docker Compose. An alert fired. Find the failing component(s) and the root cause using the tools.

Method:
1. Start with `overview`. Note every service marked with "<--".
2. Separate symptoms from causes: an error at the frontend is usually a symptom. Follow failing calls downstream
   (`service_metrics` shows outbound call errors and latency; `dependencies` shows the call graph) to the deepest
   failing service.
3. Use traces: `find_traces` then `trace_detail`. The "error origin" line names the deepest failing span. A large
   client-server gap means the network between two services is slow.
4. Confirm with the exact error in `search_logs`, and check `container_status` (stopped, restarting, OOM killed,
   disconnected) and `container_resources` (CPU, throttling, memory near limit) for the suspect.
5. For caches/databases use `datastore_metrics`: stale samples mean the datastore is unreachable.
6. Absence is evidence: no requests, no spans, no samples.
7. Several unrelated services alerting may mean several causes. Account for every alert.

Rules: every tool result is recorded as evidence with an id (E1, E2, ...). Never guess values: only what the tools
returned counts. Feature-flag settings are off limits; reason from telemetry. You have a limited number of tool
calls; do not repeat a call with the same arguments. When you know the cause, or the budget is nearly spent, call
`finish_investigation` with a short summary naming the component(s), the cause and the evidence ids."""


def _brief(inc: Incident, ledger: EvidenceLedger, feedback: list[str]) -> str:
    alerts = "\n".join(f"- {a.id} [{'active' if a.active else 'cleared'}] {a.rule} on {a.service}: {a.signal}"
                       for a in inc.alerts)
    parts = [f"Incident {inc.id}: {inc.title}\nAlerts:\n{alerts}"]
    if inc.human_notes:
        parts.append("The on-call human sent this back with notes:\n" + "\n".join(f"- {n}" for n in inc.human_notes))
    if feedback:
        parts.append("The evidence auditor rejected the last diagnosis:\n" + "\n".join(f"- {f}" for f in feedback))
    idx = ledger.index()
    if idx:
        parts.append("Evidence collected so far:\n" + idx)
    return "\n\n".join(parts)


def service_from_operation(op: str, services: set[str]) -> Optional[str]:
    """oteldemo.ProductCatalogService/GetProduct -> product-catalog."""
    norm = re.sub(r"[^a-z0-9]", "", op.lower())
    hits = [s for s in services if re.sub(r"[^a-z0-9]", "", s.lower()) and re.sub(r"[^a-z0-9]", "", s.lower()) in norm]
    return max(hits, key=len) if hits else None


class Investigator:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident, ledger: EvidenceLedger, feedback: Optional[list[str]] = None) -> str:
        box = Toolbox(self.ctx.telemetry, ledger)
        feedback = feedback or []
        model = self.ctx.llm.tool_model()
        with self.ctx.status.working("investigator", inc.id, f"round {inc.rounds}"):
            if model is not None:
                try:
                    return self._llm_loop(model, inc, ledger, box, feedback)
                except Exception as exc:
                    if exc.__class__.__name__ == "Frozen":
                        raise
                    self.ctx.bus.publish("investigator", "error", f"model tool loop failed ({exc.__class__.__name__}: "
                                         f"{str(exc)[:160]}); continuing with the playbook", incident_id=inc.id)
            return self._playbook(inc, box)

    # ------------------------------------------------------------ the model's loop (LangGraph)
    def _llm_loop(self, model: Any, inc: Incident, ledger: EvidenceLedger, box: Toolbox, feedback: list[str]) -> str:
        from typing import TypedDict

        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        from langchain_core.tools import StructuredTool
        from langgraph.graph import END, START, StateGraph
        from pydantic import BaseModel, Field

        class FinishArgs(BaseModel):
            summary: str = Field(description="component(s), cause, and the evidence ids that show it")

        tools = [StructuredTool.from_function(func=(lambda _n=name, **kw: box.call(_n, kw)), name=name,
                                              description=spec.description, args_schema=spec.args)
                 for name, spec in box.specs.items()]
        tools.append(StructuredTool.from_function(func=lambda summary: summary, name="finish_investigation",
                                                  description="End the investigation with a summary.",
                                                  args_schema=FinishArgs))
        bound = model.bind_tools(tools)
        budget = self.ctx.settings.investigator_budget
        bus, safety = self.ctx.bus, self.ctx.safety

        class S(TypedDict):
            messages: list
            calls: int
            summary: str

        # node functions stay unannotated: LangGraph resolves hints by name and S is local to this method
        def think(state):
            safety.check()
            msgs = list(state["messages"])
            if state["calls"] >= budget:
                msgs.append(HumanMessage("Tool budget spent. Call finish_investigation now."))
            resp = bound.invoke(msgs)
            text = resp.content if isinstance(resp.content, str) else " ".join(
                str(p.get("text", "")) for p in resp.content if isinstance(p, dict))
            if text.strip():
                bus.publish("investigator", "step", text.strip()[:300], incident_id=inc.id)
            return {"messages": msgs + [resp]}

        def act(state):
            msgs = list(state["messages"])
            last: AIMessage = msgs[-1]
            calls, summary = state["calls"], state["summary"]
            for tc in last.tool_calls:
                if tc["name"] == "finish_investigation":
                    summary = str(tc["args"].get("summary", ""))
                    msgs.append(ToolMessage(content="ok", tool_call_id=tc["id"]))
                    continue
                safety.check()
                if calls >= budget + 2:
                    out = "budget exhausted"
                else:
                    out = box.call(tc["name"], tc["args"])
                    calls += 1
                msgs.append(ToolMessage(content=out[:4000], tool_call_id=tc["id"]))
            return {"messages": msgs, "calls": calls, "summary": summary}

        def after_think(state):
            last = state["messages"][-1]
            return "act" if getattr(last, "tool_calls", None) else END

        def after_act(state):
            return END if state["summary"] or state["calls"] >= budget + 2 else "think"

        g = StateGraph(S)
        g.add_node("think", think)
        g.add_node("act", act)
        g.add_edge(START, "think")
        g.add_conditional_edges("think", after_think, {"act": "act", END: END})
        g.add_conditional_edges("act", after_act, {"think": "think", END: END})
        final = g.compile().invoke({"messages": [SystemMessage(SYSTEM), HumanMessage(_brief(inc, ledger, feedback))],
                                    "calls": 0, "summary": ""}, {"recursion_limit": 4 * budget + 10})
        summary = final["summary"]
        if not summary:
            last = final["messages"][-1]
            summary = last.content if isinstance(getattr(last, "content", None), str) else ""
        if final["calls"] == 0:
            raise RuntimeError("the model made no tool calls")
        return summary or "investigation finished without a summary"

    # ------------------------------------------------------------ deterministic playbook
    def _playbook(self, inc: Incident, box: Toolbox) -> str:
        services = self.ctx.services()
        topo = self.ctx.telemetry.topology
        seen: set[tuple[str, str]] = set()
        budget = self.ctx.settings.investigator_budget + 8

        def call(name: str, **args: Any) -> str:
            key = (name, repr(sorted(args.items())))
            if key in seen or len(seen) >= budget:
                return ""
            seen.add(key)
            self.ctx.safety.check()
            return re.sub(r"^E\d+: ", "", box.call(name, args))  # the evidence id prefix, not content

        ov_text = call("overview")
        flagged = [ln.split(":", 1)[0] for ln in ov_text.splitlines() if "<--" in ln]
        call("container_status")
        suspects: list[str] = []
        for s in [a.service for a in inc.alerts if a.active] + flagged:
            if s not in suspects and s not in self.ctx.settings.ignore_services:
                suspects.append(s)
        queue = list(suspects)
        visited: set[str] = set()
        origins: list[str] = []
        while queue and len(visited) < 6:
            svc = queue.pop(0)
            if svc in visited:
                continue
            visited.add(svc)
            m = call("service_metrics", service=svc)
            for ln in m.splitlines():
                if ln.startswith("outbound call errors:"):
                    dep = service_from_operation(ln.split(":", 1)[1].split()[0], services)
                    if dep and dep not in visited:
                        queue.insert(0, dep)
            tr = call("find_traces", service=svc, errors_only=True, limit=5)
            ids = re.findall(r"trace (\S+) root=", tr)
            if ids:
                det = call("trace_detail", trace_id=ids[0])
                for o in re.findall(r"error origin \(deepest failing span\): (\S+)", det):
                    if o not in origins:
                        origins.append(o)
                    if o not in visited:
                        queue.insert(0, o)
                for a, b in re.findall(r"client-server gap: (\S+)->(\S+) ", det):
                    for x in (b, a):
                        if x not in visited:
                            queue.append(x)
            elif any(a.rule == "latency_p95" and a.service == svc for a in inc.alerts):
                tr = call("find_traces", service=svc, errors_only=False, min_duration_ms=500, limit=5)
                ids = re.findall(r"trace (\S+) root=", tr)
                if ids:
                    det = call("trace_detail", trace_id=ids[0])
                    for a, b in re.findall(r"client-server gap: (\S+)->(\S+) ", det):
                        if b not in visited:
                            queue.insert(0, b)
            for dep in topo.callees(svc):
                if topo.roles.get(dep) == "datastore":
                    call("datastore_metrics", service=dep)
        for svc in (origins or suspects)[:3]:
            call("search_logs", service=svc)
            call("container_resources", service=svc)
        return (f"playbook: examined {', '.join(sorted(visited)) or 'nothing'}; "
                f"error origins {', '.join(origins) or 'none'}; {len(seen)} tool calls")
