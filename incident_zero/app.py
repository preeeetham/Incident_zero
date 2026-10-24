"""Assembles the running system from settings: store, event bus, telemetry, Action API client, safety, model,
agents and orchestrator. `incident_zero up` and the tests' server fixtures both go through `build`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .actions.client import ActionAPIClient
from .agents.context import Context
from .events import EventBus
from .llm import LLM
from .orchestrator import Orchestrator
from .safety import Safety
from .store import IncidentStore
from .telemetry.tools import Telemetry


@dataclass
class App:
    settings: Any
    ctx: Context
    orchestrator: Orchestrator
    practice: Optional[Any] = None   # practice.Practice when not in eval mode


def build(settings, telemetry: Optional[Telemetry] = None, actions: Optional[Any] = None,
          llm: Optional[LLM] = None) -> App:
    store = IncidentStore(settings.data_dir)
    bus = EventBus(store)
    actions = actions or ActionAPIClient(settings.action_api_url, settings.action_api_token)
    ctx = Context(settings=settings, store=store, bus=bus, telemetry=telemetry or Telemetry.from_settings(settings),
                  actions=actions, safety=Safety(store, bus, actions, settings), llm=llm or LLM.from_settings(settings))
    app = App(settings, ctx, Orchestrator(ctx))
    if not settings.eval_mode:
        from .practice.runner import Practice

        app.practice = Practice(settings, bus)
    return app
