"""The jury's incident report schema, and a validator.

The schema lives in schema/incident_report.schema.json, copied from incident_zero_hackathon_deliverable.json (the
structured extraction of the brief). That file's own notes say the extraction *proposed* this schema because the
organisers' official one was not legible; the official schema ships in the event's template repository. To use it,
point `report_schema_file` (IZ_REPORT_SCHEMA_FILE) at it: nothing else changes, because the Reporter validates every
report against whatever schema is loaded here, and a mismatch fails loudly.

The submission file wraps reports as {"incident_reports": [...]}, as in the brief.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

DEFAULT_SCHEMA = Path(__file__).resolve().parent / "schema" / "incident_report.schema.json"


@lru_cache(maxsize=4)
def load_schema(path: Optional[str] = None) -> dict[str, Any]:
    return json.loads(Path(path or DEFAULT_SCHEMA).expanduser().read_text())


REPORT_SCHEMA = load_schema()


class ReportInvalid(ValueError):
    pass


def validate(report: dict, schema_path: Optional[str] = None) -> None:
    """Raise ReportInvalid listing every violation. Uses jsonschema when installed, else a strict built-in check."""
    schema = load_schema(schema_path)
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with the incident_zero extra
        errors = _fallback_errors(report, schema, "$")
    else:
        v = jsonschema.Draft202012Validator(schema)
        errors = [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in v.iter_errors(report)]
    if errors:
        raise ReportInvalid("; ".join(errors))


def _fallback_errors(value: Any, schema: dict, where: str) -> list[str]:
    errs: list[str] = []
    if "const" in schema and value != schema["const"]:
        errs.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} not in {schema['enum']}")
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    pytypes = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float),
               "integer": int, "null": type(None)}
    if types and not any(isinstance(value, pytypes[x]) and not (x in ("number", "integer") and isinstance(value, bool))
                         for x in types):
        return errs + [f"{where}: expected {types}"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{where}: above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errs += [f"{where}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errs += [f"{where}: unexpected {k}" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                errs += _fallback_errors(value[k], sub, f"{where}/{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += _fallback_errors(item, schema["items"], f"{where}/{i}")
    return errs
