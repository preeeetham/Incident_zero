"""The Eyes: the layer that perceives and understands. They watch the system, collect evidence and name the root
cause, but never change anything.

  sentinel       alert rules over live telemetry; correlates alerts into one incident
  investigator   chooses telemetry tools (model tool loop, or the deterministic playbook); every result is evidence
  diagnostician  root cause, category and confidence, each claim quoting evidence (model, or rules)
  auditor        code checks every quote against the evidence ledger; sends weak diagnoses back

The Brain (brain/) supplies the model; on a known incident the Eyes run without it (playbook + rules).
"""
from __future__ import annotations

from typing import Any, Callable

from ..agents.auditor import Auditor
from ..agents.diagnostician import Diagnostician
from ..agents.investigator import Investigator
from ..agents.sentinel import Sentinel

AGENTS = ("sentinel", "investigator", "diagnostician", "auditor")


class Eyes:
    def __init__(self, ctx: Any, on_new_incident: Callable[[str], None]):
        self.sentinel = Sentinel(ctx, on_new_incident)
        self.investigator = Investigator(ctx)
        self.diagnostician = Diagnostician(ctx)
        self.auditor = Auditor(ctx)
