"""The Remediation Planner's knowledge (retrieval-augmented planning).

Telemetry says what is broken; it does not say how to fix it. After the diagnosis has passed the evidence audit, the
Planner retrieves three kinds of knowledge and must cite it, verbatim, for every action it proposes:

  code     the demo's source: the line that raises the error the telemetry shows, and the feature flags that decide
           whether that code runs (code_index)
  runbook  short runbooks organised by symptom: what fixes which kind of failure, and when nothing allowed does
  memory   past incidents whose fix ran live and was judged by the Verifier: what worked, what did not

Each retrieved item gets an id (K1, K2, ...). A citation is (knowledge id, exact quote), checked by code like the
evidence audit, so a plan cannot claim a reason that was not retrieved. None of this is shown to the Investigator or
the Diagnostician: knowledge about code paths and flags is used to choose a fix, never to diagnose (R1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from ..models import Citation, Diagnosis, Evidence, Incident, KnowledgeItem, ProposedAction
from ..redact import MASK
from .code_index import CodeIndex
from .memory import Memory
from .runbooks import Runbooks

MIN_QUOTE = 6
NEEDLE_SPLIT = re.compile(r"[.:;|\n\"'()\[\]{}=<>`]|\s{2,}|\\n")
HEX = re.compile(r"\b[0-9a-f]{12,}\b|\b\d{4,}\b")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(item: KnowledgeItem, quote: str) -> bool:
    q = _norm(quote)
    return len(q) >= MIN_QUOTE and q in _norm(item.content)


def needles(texts: Iterable[str], limit: int = 10) -> list[str]:
    """Fragments of error messages worth looking up in the code: whole phrases, no ids, timestamps or numbers."""
    out: list[str] = []
    for t in texts:
        for frag in NEEDLE_SPLIT.split(t.replace(MASK, " ")):
            frag = re.sub(r"\s+", " ", frag).strip(" ,-")
            words = re.findall(r"[A-Za-z]{2,}", frag)
            # a phrase (words separated by spaces), not an identifier like demo.user_context
            if 12 <= len(frag) <= 90 and len(words) >= 2 and " " in frag and not HEX.search(frag) and frag not in out:
                out.append(frag)
    return out[:limit]


OPERATION = re.compile(r"^\S+ (?:\S+ )?\S*?(\w+) failed ")   # "cart POST /oteldemo.CartService/EmptyCart failed ..."


def failing_operations(alerts) -> dict[str, str]:
    """service -> the operation an operation_errors alert names (its last path segment: EmptyCart)."""
    return {a.service: m.group(1) for a in alerts if a.rule == "operation_errors" and (m := OPERATION.search(a.signal))}


def check_citations(action: ProposedAction, items: list[KnowledgeItem]) -> list[str]:
    by_id = {k.id: k for k in items}
    problems = []
    for c in action.citations:
        k = by_id.get(c.knowledge_id)
        if k is None:
            problems.append(f"{action.action}: cites {c.knowledge_id}, which was not retrieved")
        elif not quote_in(k, c.quote):
            problems.append(f"{action.action}: quote {c.quote[:60]!r} is not in {c.knowledge_id}")
    return problems


def render(items: list[KnowledgeItem], max_chars: int = 2200) -> str:
    return "\n\n".join(f"--- {k.id} [{k.source}] {k.ref}: {k.summary}\n{k.content[:max_chars]}" for k in items)


class Knowledge:
    def __init__(self, demo_dir: str, data_dir: str | Path, runbooks: Optional[Runbooks] = None):
        self.demo_dir = demo_dir
        self._code: Optional[CodeIndex] = None
        self._code_loaded = False
        self.runbooks = runbooks or Runbooks()
        self.memory = Memory(data_dir)

    @property
    def code(self) -> Optional[CodeIndex]:
        if not self._code_loaded:
            self._code_loaded = True
            try:
                self._code = CodeIndex.from_demo(self.demo_dir)
            except OSError:
                self._code = None
        return self._code

    def gather(self, inc: Incident, d: Diagnosis, evidence: list[Evidence]) -> list[KnowledgeItem]:
        from ..agents.facts import extract  # facts parse evidence lines; agents never import knowledge's internals

        items: list[KnowledgeItem] = []

        def add(source: str, ref: str, summary: str, content: str, data: Optional[dict] = None) -> None:
            if not any(k.ref == ref for k in items):
                items.append(KnowledgeItem(id=f"K{len(items) + 1}", source=source, ref=ref,  # type: ignore[arg-type]
                                           summary=summary[:300], content=content, data=data or {}))

        components = [rc.component for rc in d.root_causes]
        scope = set(components) | {a.service for a in inc.alerts}
        if any(rc.category == "traffic" for rc in d.root_causes):
            scope.add("load-generator")
        facts = extract(evidence)

        code = self.code
        if code is not None:
            texts = [f.values["message"] for f in facts if f.kind == "origin" and f.service in scope]
            texts += [c.quote for rc in d.root_causes for c in rc.claims]
            texts += [f.line for f in facts if f.kind == "log" and f.service in scope]
            for n in needles(texts):
                hits = code.search(n)
                if len(hits) > 5:
                    continue  # too generic to be the line that raised this error
                for h in hits:
                    if h.service not in scope and not h.guards:
                        continue
                    lo = min([g.line for g in h.guards] + [h.line]) - 2
                    content = code.snippet(h.path, h.line, before=min(h.line - lo, 24), after=3)
                    flags = sorted({g.flag for g in h.guards})
                    add("code", f"src/{h.path}:{h.line}",
                        f"the error {n!r} is raised in {h.service}'s code at {h.path}:{h.line}"
                        + (f", in a branch decided by feature flag {', '.join(flags)}" if flags
                           else "; no feature flag guards it"),
                        content, {"kind": "error_match", "service": h.service, "needle": n, "path": h.path,
                                  "line": h.line, "guards": [{"flag": g.flag, "line": g.line, "text": g.text,
                                                              "service": g.service} for g in h.guards]})
            # an alert names the failing operation: the flags read inside its handler decide which branch it takes
            for svc, op in sorted(failing_operations(inc.alerts).items()):
                word = re.compile(rf"\b{re.escape(op)}\b")
                for s in code.flag_sites(svc):
                    lines = code.files[s.path]
                    above = [n for n in range(max(0, s.line - 30), s.line) if word.search(lines[n])]
                    if not above:
                        continue
                    start = above[-1] + 1
                    add("code", f"src/{s.path}:{s.line} (handler of {op})",
                        f"{svc}'s handler of the failing operation {op} reads feature flag {s.flag} at {s.path}:{s.line}",
                        code.snippet(s.path, s.line, before=s.line - start, after=6),
                        {"kind": "op_guard", "service": svc, "operation": op, "path": s.path, "line": s.line,
                         "guards": [{"flag": s.flag, "line": s.line, "text": s.text, "service": svc}]})
            for svc in sorted(scope):
                sites = code.flag_sites(svc)
                if not sites:
                    continue
                blocks, data = [], []
                for s in sites:
                    blocks.append(f"{s.path}:{s.line}" + (f" (through {s.via})" if s.via else "") + "\n"
                                  + code.snippet(s.path, s.line, 3, 3))
                    data.append({"flag": s.flag, "path": s.path, "line": s.line, "text": s.text, "via": s.via,
                                 "context": code.snippet(s.path, s.line, 25, 8),
                                 "near": code.snippet(s.path, s.line, 4, 4)})
                add("code", f"src/{svc}/ (feature flags)", f"feature flags evaluated in {svc}'s code: "
                    + ", ".join(sorted({s.flag for s in sites})), "\n\n".join(blocks),
                    {"kind": "flag_sites", "service": svc, "sites": data})

        for rc in d.root_causes:
            rules = " ".join(sorted({a.rule for a in inc.alerts}))
            for sec in self.runbooks.search(f"{rc.category} {rc.category} {rc.cause} {rules}"):
                add("runbook", sec.ref, sec.title, sec.text, {"category": rc.category, "component": rc.component})

        text = " ".join([d.summary] + [rc.cause for rc in d.root_causes] + [a.rule for a in inc.alerts])
        for e in self.memory.similar(components, [rc.category for rc in d.root_causes], text):
            if e["incident"] != inc.id:
                add("memory", e["incident"], f"past incident {e['incident']} on {', '.join(e['components'])}: "
                    + ("recovered" if e["recovered"] else "did not recover"), Memory.render(e), e)
        return items
