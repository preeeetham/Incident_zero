# Dependencies: caches, databases, downstream services

## Cache or database unreachable
Symptoms: dependency_failure, callers log connection refused or timeout to a datastore, the datastore's metrics are stale or absent.
Fix the datastore, not the services that call it: restart_service on the cache or database container. Restarting the callers changes nothing while the datastore is down.
Verify: the datastore's metric samples are fresh again and the callers' error ratio returns to baseline.

## Downstream service unreachable from one caller
Symptoms: dependency_failure, one caller's outbound calls fail with name resolution errors or unavailable, while the downstream service itself is healthy and serves other callers.
The fault is on the caller's side of the call, often its configuration of where the dependency lives. Check the caller's code for the failing call: if a feature flag decides the address it calls, switch that flag off. Restarting the healthy downstream service does not help.

## Downstream service failing
Symptoms: the caller's errors follow the callee's errors; the deepest failing span is in the callee.
Treat the callee as the root cause and use the runbook for its own symptom.
