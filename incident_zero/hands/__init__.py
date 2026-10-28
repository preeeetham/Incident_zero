"""The Hands: the layer that acts, and only through guarded steps. Nothing here runs without a validated plan, a
human approval bound to that plan's hash, and the kill switch released.

  planner    chooses allowed actions grounded in retrieved knowledge (or a known incident's verified fix)
  approval   the human gate: approve, reject or send back the exact plan shown
  executor   calls the Action API only (the agent process has no Docker write client)
  verifier   re-checks the triggering signals; rolls back and escalates if they have not recovered
  reporter   writes the incident report (JSON + Markdown) from the incident record

The Brain (brain/) supplies the knowledge and the model; the Eyes (eyes/) supply the audited diagnosis.
"""
from __future__ import annotations

from typing import Any

from ..agents.approval import ApprovalGate
from ..agents.executor import Executor
from ..agents.planner import Planner
from ..agents.reporter import Reporter
from ..agents.verifier import Verifier

AGENTS = ("planner", "approval", "executor", "verifier", "reporter")


class Hands:
    def __init__(self, ctx: Any, brain: Any, learned: Any):
        self.planner = Planner(ctx, knowledge=brain.knowledge, recall=brain.recall)
        self.approval = ApprovalGate(ctx)
        self.executor = Executor(ctx)
        self.verifier = Verifier(ctx, learned)
        self.reporter = Reporter(ctx)
