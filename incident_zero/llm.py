"""The model, configured once for every agent from .env (config.model_settings). Two shapes are used:

  json()        one request, one JSON object back (Diagnostician, Planner, Reporter) via llm_client.LLMClient
  tool_model()  a LangChain chat model for the Investigator's tool loop, via llm_langchain, which round-trips
                Gemini's tool-call thought signatures
When the model is disabled or unavailable every agent has a deterministic path (REQUIREMENTS N3).
"""
from __future__ import annotations

from typing import Any, Optional

from .config import model_settings


class LLM:
    def __init__(self, client: Any = None, tool_model: Any = None, label: str = "disabled", reason: str = ""):
        self.client, self._tool_model, self.label, self.reason = client, tool_model, label, reason

    @property
    def enabled(self) -> bool:
        return self.client is not None or self._tool_model is not None

    @classmethod
    def from_settings(cls, settings) -> "LLM":
        if not settings.llm_enabled:
            return cls(reason="disabled by settings (IZ_LLM_ENABLED=false)")
        try:
            from .llm_client import LLMClient

            client = LLMClient.from_settings(model_settings())
            if not client.available():
                return cls(reason=client.last_error or "LLM not configured")
            client.timeout = max(client.timeout, 90)
            tool_model, reason = None, ""
            try:
                from .llm_langchain import chat_model

                handle = chat_model(client, max_tokens=2048)
                tool_model, reason = handle.model, handle.reason
            except ImportError as exc:
                reason = f"tool loop unavailable ({exc}); pip install -e '.[incident_zero]'"
            return cls(client, tool_model, client.describe(), reason)
        except Exception as exc:  # never let model setup stop the on-call engineer
            return cls(reason=f"LLM setup failed: {exc}")

    def json(self, system: str, user: str, max_tokens: int = 1500) -> Optional[dict]:
        if self.client is None:
            return None
        return self.client.chat_json(system, user, max_tokens=max_tokens)

    def tool_model(self) -> Any:
        return self._tool_model
