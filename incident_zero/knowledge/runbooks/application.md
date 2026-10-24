# Application errors

## A code path in the service fails requests
Symptoms: application_error, the deepest failing span is in this service, its own error message in traces and logs, the container is healthy.
A restart does not fix a defect in code: the same code fails again. Find the line that raises the error. If a feature flag decides whether that code path runs, the defect shipped behind a release toggle: switch the flag off with toggle_feature_flag. That is the release rollback this Action API allows.
If no flag guards the failing code, a restart is only a mitigation for state that went bad (stuck connections, exhausted pools); say so, and escalate to the owning team if the errors return.

## One operation fails while the service looks healthy
Symptoms: operation_errors on a single operation, a low error ratio for the service as a whole, often intermittent.
Find the operation's handler in the code. A flag evaluated in that handler that picks a failing branch at a random rate explains intermittent failures; switch it off.

## Errors for one specific input
Symptoms: failures only for one product, user or request type; other requests succeed.
Look for a condition on that input in the failing handler, often a flag evaluated with the input as context. Switch that flag off.
