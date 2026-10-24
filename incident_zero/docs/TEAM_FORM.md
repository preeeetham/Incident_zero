# Team form

> Fill in every field before submission. The brief requires all members to be able to explain and change the code
> they submit, and AI coding assistants to be declared.

**Team name:**

| Member | Built (files / parts) | Commits |
|---|---|---|
|  |  |  |
|  |  |  |
|  |  |  |
|  |  |  |
|  |  |  |

## Tools and AI models used

| Use | Tool / model |
|---|---|
| Agents at run time | model set in `.env` (`LLM_PROVIDER` / `LLM_MODEL`), e.g. `gemini-3.5-flash-lite` via the OpenAI-compatible endpoint |
| AI coding assistant | Claude Code (Anthropic) — declare which parts it helped write |
| Other |  |

## Third-party libraries

Python: docker, requests, pydantic, PyYAML, jsonschema, langgraph, langchain-core, langchain-openai,
langchain-anthropic, langchain-ollama, pytest.
UI: React, React DOM, React Router, Vite, TypeScript, @fontsource/ibm-plex-sans and -mono (see `ui/package.json`).
Fault injection: Pumba (`gaiaadm/pumba`) with `ghcr.io/alexei-led/pumba-alpine-nettools`.
Application under test: the OpenTelemetry Demo (Apache-2.0).
