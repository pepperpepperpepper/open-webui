# Memory Pressure Runbook

This server has had repeated incidents where the host becomes sluggish long before `open-webui` or LiveKit show a large resident set. The recurring pattern so far is:

- `MemAvailable` falls into the low hundreds of MiB.
- swap use climbs toward or above `~1 GiB`.
- `Slab` and especially `SUnreclaim` become the dominant memory consumers.
- user-space RSS for `open-webui`, `livekit-server`, the LiveKit portal, and the LiveKit agent stays relatively small.

That pattern matters because it usually means the box is failing at the host/kernel layer, not because one obvious Python process has a multi-gigabyte heap.

## What to capture

When the server starts to feel sluggish, capture the host state and correlate it with the application logs:

```sh
cd /var/www/open-webui
./deploy/capture_memory_pressure.sh --reason manual
```

That snapshot writes a timestamped report to `/home/arch/logs/` and records:

- `MemAvailable`, swap, `Slab`, `SUnreclaim`, PSI, and `vmstat`
- top RSS / VSZ processes
- half-open socket counts and retransmit counters
- slab cache breakdown
- local Open WebUI / LiveKit portal probes
- recent Open WebUI, LiveKit agent, and LiveKit portal log excerpts

## Why the LiveKit logs matter

The LiveKit side already emits room/session lifecycle telemetry:

- agent log: `session_metric`
- portal log: `portal_metric`

Those lines let you answer whether the pressure lined up with:

- repeated room creation without matching cleanup
- repeated `apply_conflict` or reconnect churn from the demo
- long-lived sessions that never reached cleanup
- agent capacity issues (`worker is at full capacity`, `not dispatching agent job`)

The relevant logs are:

- `/home/arch/logs/open-webui-livekit-agent/current`
- `/home/arch/logs/open-webui-livekit-portal/current`
- `/home/arch/logs/open-webui/current`

## How to read the snapshot

Use this decision tree.

### 1. Check whether the problem is user-space or kernel-space

- If `open-webui` or a LiveKit process is at the top of RSS by a large margin and keeps growing, inspect that process first.
- If user-space RSS is modest but `Slab` / `SUnreclaim` is huge, the immediate failure mode is kernel memory pressure.

### 2. Check whether the trigger was network pressure

- A spike in `SYN-RECV`, retransmits, or listen drops points to connection exhaustion on the public listener.
- If socket pressure is quiet, look elsewhere.

### 3. Check whether the trigger was session churn

- Repeated `portal_metric` `token_issued`, `apply_conflict`, or missing `leave_deleted` suggests client retries or stale rooms.
- `session_metric` lines with long `session_age_sec`, repeated cleanup scheduling, or cleanup failures suggest room lifecycle bugs or stuck workers.

### 4. Check whether the host was already poisoned before the visible slowdown

- If swap is already high and `SUnreclaim` is already large before request latency spikes, the triggering workload may only be the final push.
- In that case the snapshot documents the trigger, but reboot is still often required to fully recover.

## Optional automatic capture

To capture evidence before the next forced reboot, run the watcher:

```sh
cd /var/www/open-webui
./deploy/watch_memory_pressure.sh
```

Default trigger thresholds:

- `MemAvailable <= 256 MiB`
- swap used `>= 768 MiB`
- `Slab >= 768 MiB`
- `SUnreclaim >= 768 MiB`

The watcher enforces a 15-minute cooldown between captures and writes one-line status messages to stdout.

### Optional s6 service

If you want the watcher to stay on all the time, install the service template:

```sh
sudo cp -a /var/www/open-webui/deploy/s6/open-webui-pressure-watch /service/open-webui-pressure-watch
sudo s6-svscanctl -a /service
```

Control it with:

```sh
sudo s6-svc -u /service/open-webui-pressure-watch
sudo s6-svc -d /service/open-webui-pressure-watch
sudo s6-svc -r /service/open-webui-pressure-watch
```

## Current working hypothesis

The evidence collected so far points to a recurring host-memory failure pattern:

- the server tips into swap as available RAM shrinks
- `SUnreclaim` / slab grows disproportionately
- app RSS does not explain the total pressure by itself

That does **not** prove Open WebUI or LiveKit are innocent. It means they need to be evaluated via room/session churn and request patterns, not by assuming one large heap leak in a single process.
