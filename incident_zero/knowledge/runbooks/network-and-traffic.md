# Network and traffic

## Latency or packet loss on one service's network
Symptoms: network, a large client-server gap on calls to one service while its own server spans are fast, retries and timeouts at its callers.
The delay is on the path, not in the code. Traffic-control rules on a container's interface (delay, loss) live in its network namespace; restart_service recreates the container's interface and clears them. If the latency comes back, escalate: the network itself is at fault.

## Service detached from the network
Symptoms: network, the container is running but attached to no network (networks=none), callers fail name resolution.
No allowed action reattaches a network, and a restart keeps the container detached. Escalate.

## Slow code path, not the network
Symptoms: latency_p95 high, server spans themselves slow, no client-server gap.
Look in the code for a sleep, lock or slow call on that path. If a flag enables it, switch the flag off.

## Traffic flood
Symptoms: traffic_spike at the entry service, request rate many times its baseline, latency and errors rising everywhere downstream.
Reduce the load at its source: throttle_load_generator back to its normal user count. If the flood is a code path in the load generator that a flag turns on, switch that flag off too.
