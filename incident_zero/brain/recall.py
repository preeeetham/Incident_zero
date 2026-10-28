"""Recall: answer a known incident from the casebook instead of the model.

A repeat of an incident the agents already solved (same alerts, fix verified live) does not need the model to
reason it out again. Recall changes who does the thinking, never what is checked:

  1. lookup     at detection, the casebook names the past incident whose alerts match and whose fix was verified
  2. investigate  the Investigator runs its deterministic playbook (no model)
  3. confirm    the rules diagnose from that evidence (no model) and must name the same component and kind of
                failure as the case; the Evidence Auditor still checks every quote
  4. plan       the Planner proposes the case's verified actions, citing incident memory; code still validates them
                against the Action API's catalog and previews them as a dry run
  5. approve    a human still approves the exact plan, bound by its hash

If the diagnosis disagrees with the case, the audit fails, or the stored fix no longer validates, recall is
abandoned and the incident goes through the full pipeline, model included.
"""
from __future__ import annotations

from typing import Optional

from ..knowledge.memory import Memory, action_key
from ..models import Citation, Diagnosis, Incident, KnowledgeItem, ProposedAction, RecallRecord
from .casebook import Casebook, alert_keys


class Recall:
    def __init__(self, casebook: Casebook, memory: Memory, settings):
        self.casebook, self.memory, self.settings = casebook, memory, settings

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "recall_enabled", True))

    def lookup(self, inc: Incident) -> Optional[RecallRecord]:
        if not self.enabled:
            return None
        case = self.casebook.known(alert_keys(inc), getattr(self.settings, "recall_min_similarity", 0.6),
                                   getattr(self.settings, "recall_min_successes", 1))
        if case is None or case.incident == inc.id:
            return None
        e = case.entry
        calls, tokens, estimated = self.casebook.cost_of(case)
        return RecallRecord(case=case.incident, similarity=case.similarity,
                            component=(e["components"] or [""])[0], category=(e["categories"] or [""])[0],
                            actions=e["actions"], track_record=case.track_record, model_calls_skipped=calls,
                            tokens_saved=tokens, tokens_saved_estimated=estimated)

    @staticmethod
    def confirms(rec: RecallRecord, d: Diagnosis) -> str:
        """'' if the rule-based diagnosis agrees with the case, else why not."""
        if d.false_alarm or not d.root_causes:
            return "the rules found no root cause in the new evidence"
        main = d.root_causes[0]
        if (main.component, main.category) != (rec.component, rec.category):
            return (f"the rules diagnosed {main.component} ({main.category}); the known case {rec.case} was "
                    f"{rec.component} ({rec.category})")
        return ""

    def knowledge_item(self, rec: RecallRecord, items: list[KnowledgeItem]) -> Optional[KnowledgeItem]:
        """The case as a knowledge item the plan can cite: the one already retrieved, or added from memory."""
        for k in items:
            if k.source == "memory" and k.ref == rec.case:
                return k
        entry = next((e for e in self.memory.entries() if e["incident"] == rec.case), None)
        if entry is None:
            return None
        k = KnowledgeItem(id=f"K{len(items) + 1}", source="memory", ref=rec.case,
                          summary=f"known incident {rec.case} on {', '.join(entry['components'])}: recovered",
                          content=Memory.render(entry), data=entry)
        items.append(k)
        return k

    def actions(self, rec: RecallRecord, items: list[KnowledgeItem]) -> list[ProposedAction]:
        k = self.knowledge_item(rec, items)
        if k is None:
            return []
        return [ProposedAction(action=a["action"], parameters=dict(a["parameters"]),
                               reason=f"known incident: the same alerts on {rec.component} ({rec.category}) "
                                      f"recovered after this in {rec.case}; {rec.track_record}",
                               citations=[Citation(knowledge_id=k.id, quote=action_key(a["action"], a["parameters"]))])
                for a in rec.actions]
