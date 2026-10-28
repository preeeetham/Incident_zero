"""The Brain: the layer that thinks and remembers, serving the Eyes (perceive, diagnose) and the Hands (act).

  model      llm.LLM: one JSON request (Diagnostician, Planner, Reporter) or a tool loop (Investigator)
  knowledge  what the Planner retrieves to choose a fix: code, runbooks, incident memory (knowledge/)
  casebook   every closed incident, whatever the outcome, with what the model cost (casebook.py)
  meter      tokens per incident and per agent, measured from the providers' responses (meter.py)
  recall     a repeat of a solved incident is answered from the casebook, with no model calls (recall.py)

The Brain never acts: the Hands execute through the Action API after a human approves.
"""
from __future__ import annotations

from typing import Any

from ..knowledge import Knowledge
from .casebook import Casebook, alert_keys
from .meter import TokenMeter, bound
from .recall import Recall

__all__ = ["Brain", "Casebook", "Recall", "TokenMeter", "alert_keys", "bound"]


class Brain:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.llm = ctx.llm
        self.knowledge = Knowledge(ctx.settings.demo_dir, ctx.settings.data_dir)
        self.casebook = Casebook(ctx.settings.data_dir)
        self.recall = Recall(self.casebook, self.knowledge.memory, ctx.settings)

    @property
    def meter(self) -> TokenMeter:
        return self.llm.meter

    def stats(self) -> dict[str, Any]:
        return {"model": self.llm.label if self.llm.enabled else f"none ({self.llm.reason})",
                "recall_enabled": self.recall.enabled, **self.casebook.stats()}
