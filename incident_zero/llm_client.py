"""A plain HTTP chat client with three providers behind one interface:

  ollama     local open-source model (offline, free)
  openai     any OpenAI-compatible chat API (OpenAI, Gemini, Groq, OpenRouter, Together, Mistral, ...)
  anthropic  Claude via the Messages API

Configured from .env (config.model_settings). Every method returns None on any failure (timeouts, bad JSON, missing
key), so the agents' deterministic paths always have the final say. The Investigator's tool loop uses a LangChain
model built from the same client instead (llm_langchain.py).
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Optional

import requests

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

DEFAULT_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
DEFAULT_MODELS = {
    "ollama": "phi4-mini",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
}


class LLMClient:
    def __init__(self, provider: str = "ollama", model: str | None = None, base_url: str | None = None,
                 api_key: str = "", timeout_s: float = 120.0, enabled: bool = True):
        self.provider = provider if provider in DEFAULT_BASE_URLS else "ollama"
        self.model = model or DEFAULT_MODELS[self.provider]
        self.base_url = (base_url or DEFAULT_BASE_URLS[self.provider]).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout_s
        self.enabled = enabled
        self._available: Optional[bool] = None
        self.last_error: str = ""
        self._last = threading.local()  # per thread: each incident runs on its own worker thread

    def last_usage(self) -> Optional[tuple[int, int]]:
        """(prompt, completion) tokens the provider reported for this thread's last call, or None."""
        return getattr(self._last, "usage", None)

    def _keep_usage(self, body: Any) -> None:
        from .brain.meter import usage_from_response

        self._last.usage = usage_from_response(self.provider, body) if isinstance(body, dict) else None

    @classmethod
    def from_settings(cls, m, enabled: bool = True) -> "LLMClient":
        """From config.ModelSettings."""
        return cls(provider=m.provider, model=m.model or None, base_url=m.base_url or None, api_key=m.api_key,
                   timeout_s=m.timeout_s, enabled=enabled)

    def describe(self) -> str:
        return f"{self.provider}:{self.model}"

    # ------------------------------------------------------------------ availability
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is None:
            self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        try:
            if self.provider == "ollama":
                r = requests.get(f"{self.base_url}/api/tags", timeout=3)
                names = [m.get("name", "") for m in r.json().get("models", [])]
                base = self.model.split(":")[0]
                ok = any(n == self.model or n.split(":")[0] == base for n in names)
                if not ok:
                    self.last_error = f"model {self.model} not in `ollama list`"
                return ok
            if not self.api_key:
                self.last_error = "LLM_API_KEY not set"
                return False
            return True
        except (requests.RequestException, ValueError) as exc:
            self.last_error = str(exc)
            return False

    # ------------------------------------------------------------------ transport
    def _chat(self, system: str, user: str, json_mode: bool, max_tokens: int) -> Optional[str]:
        self._last.usage = None
        if not self.available():
            return None
        try:
            if self.provider == "ollama":
                body: dict[str, Any] = {
                    "model": self.model, "stream": False,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"temperature": 0.2, "num_predict": max_tokens},
                }
                if json_mode:
                    body["format"] = "json"
                r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                self._keep_usage(data)
                return data.get("message", {}).get("content")

            if self.provider == "openai":
                body = {
                    "model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                }
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                r = requests.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self.api_key}"})
                r.raise_for_status()
                data = r.json()
                self._keep_usage(data)
                return data["choices"][0]["message"]["content"]

            body = {
                "model": self.model, "max_tokens": max_tokens, "temperature": 0.2, "system": system,
                "messages": [{"role": "user", "content": user + ("\nRespond with a single JSON object only." if json_mode else "")}],
            }
            r = requests.post(f"{self.base_url}/v1/messages", json=body, timeout=self.timeout,
                              headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            data = r.json()
            self._keep_usage(data)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            self.last_error = str(exc)
            return None

    # ------------------------------------------------------------------ public
    def complete(self, system: str, user: str, max_tokens: int = 400) -> Optional[str]:
        text = self._chat(system, user, json_mode=False, max_tokens=max_tokens)
        return _THINK.sub("", text).strip() if text else None

    def chat_json(self, system: str, user: str, max_tokens: int = 700) -> Optional[dict]:
        text = self._chat(system, user, json_mode=True, max_tokens=max_tokens)
        if not text:
            return None
        text = _THINK.sub("", text).strip()
        m = _FENCE.search(text)
        if m:
            text = m.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
