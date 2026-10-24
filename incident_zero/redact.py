"""Feature-flag redaction (REQUIREMENTS R2).

The demo's services evaluate flags through OpenFeature tracing hooks, so a span can carry `feature_flag.key =
paymentFailure` and `feature_flag.variant = on`. An agent that reads that has not diagnosed anything, and the jury
forbids it. Every telemetry result passes through `redact` before it becomes evidence:

  * dict keys, span tags, span events/logs and log fields whose name mentions a feature flag are dropped
  * string values that name one of the demo's flags are masked
  * log lines that mention a feature flag are dropped

The flag names below are a static deny-list compiled from the demo's source; nothing here reads flagd or its config.
"""
from __future__ import annotations

import re
from typing import Any

FLAG_KEY_RE = re.compile(r"feature[_.\-]?flag|openfeature|flagd", re.I)
# a text line is about flag data only when it names a flag evaluation; "flagd" alone is a service name in topology,
# container status and the overview, and dropping those lines hid flagd's health and every caller's dependencies
FLAG_LINE_RE = re.compile(r"feature[_.\-]?flag|openfeature", re.I)
DEMO_FLAG_NAMES = (
    "adFailure", "adHighCpu", "adManualGc", "aiRunawayAgent", "aiSlowResponse", "cartFailure",
    "cartMetricCardinalityOverflow", "emailMemoryLeak", "emitRawPii", "failedReadinessProbe", "imageSlowLoad",
    "intlShippingSlowdown", "kafkaConsumerDead", "kafkaQueueProblems", "loadGeneratorFloodHomepage",
    "paymentFailure", "paymentUnreachable", "productCatalogFailure", "productCatalogLockContention",
    "recommendationCacheFailure",
)
FLAG_NAME_RE = re.compile(r"\b(" + "|".join(DEMO_FLAG_NAMES) + r")\b")
MASK = "[redacted-flag]"


def _bad_key(key: Any) -> bool:
    return isinstance(key, str) and bool(FLAG_KEY_RE.search(key))


def redact_text(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not FLAG_LINE_RE.search(ln)]
    return FLAG_NAME_RE.sub(MASK, "\n".join(lines))


def _flag_entry(obj: dict) -> bool:
    """A tag, field or event that is about a flag: {"key": "feature_flag.key", ...}, {"name": "feature_flag"},
    {"key": "event", "value": "feature_flag.evaluation"}. Service and operation names that merely mention flagd
    are kept: flagd being down is a real infrastructure fault the agent must be able to see."""
    if any(_bad_key(obj.get(k)) for k in ("key", "name", "event")):
        return True
    if "key" in obj and _bad_key(obj.get("value")):
        return True
    # a span event / log record whose own event marker is a flag evaluation goes entirely, not just its fields
    for lst in (obj.get("fields"), obj.get("attributes")):
        if isinstance(lst, list) and any(isinstance(f, dict) and f.get("key") == "event" and _bad_key(f.get("value"))
                                         for f in lst):
            return True
    return False


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        if _flag_entry(obj):
            return None
        out = {}
        for k, v in obj.items():
            if _bad_key(k):
                continue
            r = redact(v)
            if r is None and v is not None:
                continue
            out[k] = r
        return out
    if isinstance(obj, list):
        out_list = []
        for item in obj:
            r = redact(item)
            if r is None and item is not None:
                continue
            out_list.append(r)
        return out_list
    if isinstance(obj, str):
        if "\n" in obj:
            return redact_text(obj)
        return FLAG_NAME_RE.sub(MASK, obj)
    return obj


def mentions_flags(text: str) -> bool:
    return bool(FLAG_KEY_RE.search(text) or FLAG_NAME_RE.search(text))
