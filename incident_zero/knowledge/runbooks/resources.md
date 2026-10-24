# CPU and memory

## High CPU or heavy garbage collection in the service's own code
Symptoms: resource_cpu, cpu_percent high without a small CPU limit, latency up, no throttling from a limit.
The service is burning CPU in its own code. Find the code path that creates the load or forces garbage collection; if a feature flag turns it on, switch the flag off. A restart only resets it until the same code runs again.

## CPU starved by its limit
Symptoms: resource_cpu, throttled periods rising, a CPU limit far below one core, slow everything in that service.
The configuration starves the service. No allowed action raises a CPU limit (scale_service needs Kubernetes). Escalate with the limit and the throttling evidence.

## Memory leak or runaway cache
Symptoms: resource_memory, memory growing steadily toward the limit, garbage collection pauses, eventually OOM kills.
Find what holds the memory, usually an in-process cache without a bound. If a feature flag switches the leaking cache path on, switch it off, then restart_service to give back the memory already leaked. Restart alone only buys time.
