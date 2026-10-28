"""The token meter: what every model call cost, per incident and per agent.

Every model call goes through llm.LLM (one JSON request) or the Investigator's LangGraph loop (a chat model). Both
report usage here. Counts come from the provider's own response (OpenAI-compatible `usage`, Anthropic `usage`,
Ollama `prompt_eval_count`/`eval_count`, LangChain `usage_metadata`); only when a provider sends none is the count
estimated from the text length (about 4 characters per token), and the incident's usage is then marked estimated.

Which incident and which agent a call belongs to is taken from context variables that the orchestrator's worker
(one thread per incident) and the status board (`working(agent, ...)`) set, so agents do not pass it along.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from ..models import TokenUsage

current_incident: ContextVar[Optional[str]] = ContextVar("current_incident", default=None)
current_agent: ContextVar[str] = ContextVar("current_agent", default="unknown")

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def usage_from_response(provider: str, body: dict) -> Optional[tuple[int, int]]:
    """(prompt, completion) tokens from a provider's raw JSON response, or None if it reports none."""
    try:
        if provider == "ollama":
            if "prompt_eval_count" in body or "eval_count" in body:
                return int(body.get("prompt_eval_count") or 0), int(body.get("eval_count") or 0)
            return None
        u = body.get("usage") or {}
        if provider == "anthropic":
            if "input_tokens" in u or "output_tokens" in u:
                return int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
            return None
        if "prompt_tokens" in u or "completion_tokens" in u:
            return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def usage_from_message(message: Any) -> Optional[tuple[int, int]]:
    """(prompt, completion) tokens from a LangChain AIMessage's usage_metadata, or None."""
    meta = getattr(message, "usage_metadata", None) or {}
    if not meta:
        return None
    return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)


@contextmanager
def bound(incident_id: str) -> Iterator[None]:
    """Attribute every model call made inside this block (on this thread) to `incident_id`."""
    token = current_incident.set(incident_id)
    try:
        yield
    finally:
        current_incident.reset(token)


class TokenMeter:
    def __init__(self) -> None:
        self._usage: dict[str, TokenUsage] = {}
        self._lock = threading.Lock()

    def seed(self, incident_id: str, usage: TokenUsage) -> None:
        """Continue counting from what was persisted (a restart resumes an open incident)."""
        with self._lock:
            if incident_id not in self._usage:
                self._usage[incident_id] = usage.model_copy(deep=True)

    def record(self, prompt: int, completion: int, estimated: bool = False, agent: Optional[str] = None,
               incident_id: Optional[str] = None) -> None:
        incident_id = incident_id or current_incident.get()
        if incident_id is None:
            return  # a call outside any incident (e.g. a probe): nothing to attribute it to
        agent = agent or current_agent.get()
        with self._lock:
            u = self._usage.setdefault(incident_id, TokenUsage())
            u.calls += 1
            u.prompt_tokens += prompt
            u.completion_tokens += completion
            u.estimated = u.estimated or estimated
            row = u.by_agent.setdefault(agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            row["calls"] += 1
            row["prompt_tokens"] += prompt
            row["completion_tokens"] += completion

    def usage(self, incident_id: str) -> TokenUsage:
        with self._lock:
            return self._usage.get(incident_id, TokenUsage()).model_copy(deep=True)
