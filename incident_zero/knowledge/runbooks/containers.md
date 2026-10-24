# Containers

## Container stopped or exited
Symptoms: container_down, state=exited, the service sends no spans, callers fail with connection refused.
A container that is not running serves nothing. Start it again with restart_service on that container, not on its callers.
Verify: the container is running and its callers' error ratio returns to baseline.

## Container paused or frozen
Symptoms: container_down, state=paused, requests time out instead of being refused, no new spans.
A paused process holds its sockets but never answers. restart_service unfreezes and restarts it.

## Container restarting in a loop
Symptoms: container_restarts, restart count rising, short uptime, exit code 137 or 1 repeating.
A restart loop with a normal memory limit is often a crash on start or an external kill. restart_service gives it a clean start; if the count keeps rising after the restart, escalate with the exit codes: restarting again will not fix it.

## OOM killed under a very small memory limit
Symptoms: oom_killed=true, memory_limit_mb far below what the service needs, repeated restarts.
The container is starved by its configuration. No allowed action raises a memory limit, and a restart is killed again. Escalate: the limit must be changed in the deployment.
