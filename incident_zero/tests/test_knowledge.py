"""The Planner's knowledge: code search, runbooks, incident memory, and the citation check."""
from __future__ import annotations

from incident_zero.knowledge import Knowledge, check_citations, needles
from incident_zero.knowledge.code_index import CodeIndex
from incident_zero.knowledge.memory import Memory
from incident_zero.knowledge.runbooks import Runbooks
from incident_zero.models import (ActionResult, Citation, Claim, Diagnosis, Incident, KnowledgeItem, ProposedAction,
                                  RootCause)


def test_code_index_never_reads_flag_infrastructure_or_config(demo_dir):
    ix = CodeIndex.from_demo(str(demo_dir))
    assert ix is not None
    assert not any(p.startswith("flagd/") for p in ix.files)
    assert not any(p.endswith(".json") for p in ix.files)
    assert "secretFlagdOnly" not in {s.flag for s in ix.sites}


def test_flags_found_from_how_the_code_reads_them(demo_dir):
    ix = CodeIndex.from_demo(str(demo_dir))
    by = {(s.flag, s.path) for s in ix.sites}
    assert ("paymentFailure", "payment/charge.js") in by                   # string literal
    assert ("adHighCpu", "ad/AdService.java") in by                         # through a constant
    assert ("paymentUnreachable", "checkout/main.go") in by                 # through a generated Go accessor
    assert not any(p.endswith("flags_gen.go") for _, p in by)               # the accessor itself is not a use
    hit = ix.search("Payment request failed")[0]
    assert (hit.path, hit.line) == ("payment/charge.js", 9)
    assert [g.flag for g in hit.guards] == ["paymentFailure"]               # emitRawPii is read but does not guard it


def test_needles_skip_ids_and_masked_flags():
    n = needles(["Payment request failed. Invalid token. trace 4bf92f3577b34da6a3ce929d0e0e4736",
                 "[redacted-flag] is on: Product Catalog Fail"])
    assert "Payment request failed" in n and "Invalid token" in n
    assert not any("4bf92f" in x or "redacted" in x for x in n)
    assert needles(["Invalid token. demo.user_context.loyalty_level=gold"]) == ["Invalid token"]


def test_runbooks_are_found_by_symptom():
    rb = Runbooks()
    assert rb.sections and all(s.text for s in rb.sections)
    assert rb.search("container_down exited container not running")[0].title == "Container stopped or exited"
    assert "CPU" in rb.search("resource_cpu throttled cpu limit")[0].title


def test_citations_must_quote_retrieved_knowledge():
    k = KnowledgeItem(id="K1", source="runbook", ref="r#s", summary="s", content="restart_service on the cache")
    ok = ProposedAction(action="restart_service", reason="r",
                        citations=[Citation(knowledge_id="K1", quote="restart_service on the cache")])
    assert check_citations(ok, [k]) == []
    bad = ProposedAction(action="restart_service", reason="r",
                         citations=[Citation(knowledge_id="K1", quote="a runbook says to restart"),
                                    Citation(knowledge_id="K7", quote="restart_service on the cache")])
    assert len(check_citations(bad, [k])) == 2


def _resolved(inc_id: str, recovered: bool, dry: bool = False) -> Incident:
    rc = RootCause(component="valkey-cart", category="container_down", cause="valkey-cart is exited", confidence=0.9,
                   claims=[Claim(text="t", evidence_id="E1", quote="valkey-cart: state=exited")])
    inc = Incident(id=inc_id, title="t", diagnosis=Diagnosis(summary="s", root_causes=[rc]))
    inc.execution.results.append(ActionResult(action="restart_service", parameters={"service": "valkey-cart"}, ok=True,
                                              dry_run=dry))
    inc.verification.checked, inc.verification.recovered = True, recovered
    return inc


def test_memory_keeps_only_verified_live_outcomes_and_reports_a_track_record(tmp_path):
    m = Memory(tmp_path)
    assert m.record(_resolved("INC-0001", True, dry=True)) is None      # a dry run proves nothing
    assert m.record(_resolved("INC-0002", True))
    assert m.record(_resolved("INC-0002", True)) is None                # once per incident
    assert m.record(_resolved("INC-0003", False))
    sim = m.similar(["valkey-cart"], ["container_down"], "exited")
    assert {e["incident"] for e in sim} == {"INC-0002", "INC-0003"}
    tr = Memory.track_record("restart_service", {"service": "valkey-cart"}, sim)
    assert tr.startswith("recovered 1 of 2") and "INC-0003" in tr


def test_gather_includes_memory_of_the_same_failure(tmp_path):
    Memory(tmp_path).record(_resolved("INC-0002", True))
    d = _resolved("INC-0009", True).diagnosis
    items = Knowledge("", tmp_path).gather(Incident(id="INC-0009", title="t"), d, [])
    assert any(k.source == "memory" and k.ref == "INC-0002" for k in items)
    assert any(k.source == "runbook" for k in items)
    assert not any(k.source == "code" for k in items)                   # no demo checkout: no code search
