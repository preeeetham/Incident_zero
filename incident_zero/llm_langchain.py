"""A LangChain chat model for the Investigator's LangGraph tool loop, built from the SAME LLMClient the other agents use.

There is deliberately no second configuration scheme: provider, model, base URL, key, timeout and the availability
probe all come from llm_client.LLMClient; only the transport differs:

  ollama     langchain_ollama.ChatOllama
  openai     langchain_openai.ChatOpenAI   (any OpenAI-compatible endpoint: OpenAI, Gemini, Groq, OpenRouter, ...)
  anthropic  langchain_anthropic.ChatAnthropic

Provider packages are imported lazily, so only the one in use has to be installed (pip install -e '.[incident_zero]').
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .llm_client import LLMClient

TEMPERATURE = 0.2  # same as LLMClient
# The tool loop makes many calls per incident, so rate limits (429) are expected rather than exceptional; the
# provider SDKs back off and honour Retry-After. LLMClient's single-shot calls never retry.
RETRIES = 3


@dataclass
class ModelHandle:
    model: Any | None  # a langchain BaseChatModel, or None when the LLM is disabled/unavailable
    label: str         # "langgraph:<provider>:<model>"
    reason: str = ""   # why model is None


def chat_model(client: LLMClient, max_tokens: int = 700) -> ModelHandle:
    label = f"langgraph:{client.describe()}"
    if not client.enabled:
        return ModelHandle(None, label, "LLM disabled")
    if not client.available():  # ollama model pulled / API key present
        return ModelHandle(None, label, client.last_error or "LLM not available")
    try:
        return ModelHandle(_build(client, max_tokens), label)
    except ImportError as exc:
        return ModelHandle(None, label, f"{exc.name or exc} not installed; pip install -e '.[incident_zero]'")


def _build(c: LLMClient, max_tokens: int):
    if c.provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=c.model, base_url=c.base_url, temperature=TEMPERATURE, num_predict=max_tokens,
                          client_kwargs={"timeout": c.timeout})
    if c.provider == "openai":
        return _chat_openai_class()(model=c.model, base_url=c.base_url, api_key=c.api_key, temperature=TEMPERATURE,
                                    max_tokens=max_tokens, timeout=c.timeout, max_retries=RETRIES)
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=c.model, base_url=c.base_url, api_key=c.api_key, temperature=TEMPERATURE,
                         max_tokens=max_tokens, timeout=c.timeout, max_retries=RETRIES)


EXTRA_KEY = "tool_call_extra_content"  # AIMessage.additional_kwargs key: {tool_call_id: extra_content}


def _chat_openai_class():
    """ChatOpenAI that round-trips each tool call's `extra_content`.

    Gemini's OpenAI-compatible endpoint attaches `extra_content.google.thought_signature` to every tool call and
    rejects the next turn (400 "Function call is missing a thought_signature") unless it comes back verbatim.
    langchain-openai parses tool calls down to id/type/function and drops it, so this keeps it on the AIMessage
    and puts it back on the same tool call id in the request. Endpoints that send no `extra_content` (OpenAI,
    Groq, OpenRouter, ...) are unaffected.
    """
    from langchain_core.messages import AIMessage
    from langchain_openai import ChatOpenAI

    class _ChatOpenAI(ChatOpenAI):
        def _create_chat_result(self, response, generation_info=None):
            result = super()._create_chat_result(response, generation_info)
            raw = response if isinstance(response, dict) else response.model_dump(warnings=False)
            for gen, choice in zip(result.generations, raw.get("choices") or []):
                extras = {tc["id"]: tc["extra_content"] for tc in (choice.get("message") or {}).get("tool_calls") or []
                          if tc.get("id") and tc.get("extra_content")}
                if extras and isinstance(gen.message, AIMessage):
                    gen.message.additional_kwargs[EXTRA_KEY] = extras
            return result

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            extras = {}
            for m in self._convert_input(input_).to_messages():
                if isinstance(m, AIMessage):
                    extras.update(m.additional_kwargs.get(EXTRA_KEY) or {})
            for msg in payload.get("messages") or []:
                if msg.get("role") != "assistant":
                    continue
                for tc in msg.get("tool_calls") or []:
                    if tc.get("id") in extras:
                        tc["extra_content"] = extras[tc["id"]]
            return payload

    return _ChatOpenAI
