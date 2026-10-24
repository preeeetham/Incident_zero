"""PromQL for the demo's span metrics (the collector's span_metrics connector, exported over OTLP to Prometheus).

Request rate, error ratio and latency per service come from server and consumer spans; a service that is down
emits nothing itself, so its callers' client spans are the other half of the picture.
"""
from __future__ import annotations

CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
INBOUND = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
OUTBOUND = 'span_kind=~"SPAN_KIND_CLIENT|SPAN_KIND_PRODUCER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'


def _sel(*parts: str) -> str:
    return "{" + ",".join(p for p in parts if p) + "}"


def _off(offset_s: float) -> str:
    return f" offset {int(offset_s)}s" if offset_s else ""


def _svc(service: str | None) -> str:
    return f'service_name="{service}"' if service else ""


def rps(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return f"sum by ({by}) (rate({CALLS}{_sel(INBOUND, _svc(service))}[{window}]{_off(offset_s)}))"


def error_rps(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return f"sum by ({by}) (rate({CALLS}{_sel(INBOUND, ERROR, _svc(service))}[{window}]{_off(offset_s)}))"


def p95(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return (f"histogram_quantile(0.95, sum by ({by}, le) "
            f"(rate({DURATION}{_sel(INBOUND, _svc(service))}[{window}]{_off(offset_s)})))")


def op_calls(window: str, errors: bool = False, offset_s: float = 0) -> str:
    """Inbound calls (or failed calls) per (service, operation) over the window, as counts. A fault in one operation
    (cart's EmptyCart) is diluted at service level (~3%) but obvious per operation (~50%)."""
    return (f"sum by (service_name, span_name) (increase({CALLS}{_sel(INBOUND, ERROR if errors else '')}"
            f"[{window}]{_off(offset_s)}))")


def client_error_rps(window: str, service: str) -> str:
    """Errors on the calls a service makes, by operation: which dependency is failing it."""
    return f"sum by (span_name) (rate({CALLS}{_sel(OUTBOUND, ERROR, _svc(service))}[{window}]))"


def client_p95(window: str, service: str) -> str:
    return (f"histogram_quantile(0.95, sum by (span_name, le) "
            f"(rate({DURATION}{_sel(OUTBOUND, _svc(service))}[{window}])))")


def last_seen(service: str | None = None) -> str:
    """Seconds since each service last reported a span (absence is a signal)."""
    return f"time() - max by (service_name) (timestamp({CALLS}{_sel(_svc(service))}))"


def container_cpu(window: str, offset_s: float = 0) -> str:
    """CPU per container in percent of one core, from the docker_stats receiver's usage counter. A rate, so it follows
    the load within the window (the receiver's own utilization gauge is a slow running average)."""
    return (f"sum by (container_name) (rate(container_cpu_usage_nanoseconds_total[{window}]{_off(offset_s)})) "
            f"* 100 / 1e9")


def container_memory_baseline(container: str, offset_s: float) -> str:
    """A container's usual memory share of its limit (percent): the average over the half hour before the baseline
    offset. Near-limit memory is a symptom only when it is above this."""
    return (f'avg by (container_name) (avg_over_time(container_memory_percent_ratio{{container_name="{container}"}}'
            f"[30m]{_off(offset_s)}))")


DATASTORE_PREFIXES = {"valkey": "redis", "redis": "redis", "postgres": "postgresql", "astronomy-db": "postgresql",
                      "kafka": "kafka"}


def datastore_prefix(service: str) -> str | None:
    for k, v in DATASTORE_PREFIXES.items():
        if k in service:
            return v
    return None
