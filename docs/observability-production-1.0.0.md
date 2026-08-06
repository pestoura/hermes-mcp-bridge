# Hermes MCP Bridge 1.0.0 — production observability gate

## Scope

This gate promotes **metrics only**. It does not enable tracing export, retry,
circuit breaker or remote access to the metrics socket.

The production shape is deliberately local:

```text
Hermes MCP Bridge :9464 (127.0.0.1 only)
        |
        | Prometheus scrape, 30s
        v
Grafana Alloy on the same host
        |
        | Prometheus remote_write over TLS
        v
Grafana Cloud
```

The canonical Alloy fragment is:

```text
deploy/observability/grafana-cloud-loopback.alloy
```

It reads the Grafana Cloud URL, username and access-policy token from the Alloy
service environment. No credential is committed to this repository.

## Security invariants

The observability gate is valid only while all of the following remain true:

- `BRIDGE_METRICS_ENABLED=1`;
- `BRIDGE_METRICS_HOST=127.0.0.1`;
- `BRIDGE_METRICS_PORT=9464`;
- `BRIDGE_METRICS_ALLOW_REMOTE` is unset or false;
- no host or container port publishes `9464`;
- Alloy runs on the bridge host and scrapes `127.0.0.1:9464`;
- only metric names matching `bridge_.*` are forwarded;
- forbidden identifier/secret labels are dropped defensively in Alloy;
- `BRIDGE_TRACING_ENABLED=0`;
- `BRIDGE_TRACING_EXPORT=0`;
- metric label names and values remain within the finite domains enforced by
  `observability/metrics.py`;
- the exporter response contains no prompt, output, token, cookie, credential,
  filesystem path or per-run identifier.

Binding the exporter to `0.0.0.0`, a Docker gateway, a LAN address or a tunnel
is outside this gate and must fail review.

## Required Alloy service environment

```text
GRAFANA_CLOUD_PROMETHEUS_URL=https://<stack-host>/api/prom/push
GRAFANA_CLOUD_PROMETHEUS_USERNAME=<numeric-metrics-tenant>
GRAFANA_CLOUD_PROMETHEUS_PASSWORD=<metrics-publish access-policy token>
HERMES_ENVIRONMENT=production
```

Store these values in the existing protected Alloy environment mechanism. Do
not place them in the Alloy fragment, bridge `.env`, Git, logs, screenshots or
issue comments.

The access policy should grant only the metrics-publish capability required by
the target Grafana Cloud stack. It must not be a Grafana administrator token.

## Validation sequence

1. Validate the candidate configuration offline:

   ```bash
   python scripts/observability_smoke.py --check-config --check-logging
   ```

2. Start the bridge exporter on loopback and prove the bind locally:

   ```bash
   ss -ltnp | grep '127.0.0.1:9464'
   ```

3. Probe the exporter read-only:

   ```bash
   python scripts/observability_smoke.py \
     --probe http://127.0.0.1:9464/metrics
   ```

4. Validate the Alloy configuration using the installed Alloy version before
   reloading the service.
5. Reload Alloy without restarting the bridge.
6. Confirm a single scrape target and a single remote-write stream.
7. Confirm the Grafana Cloud series carry only the expected low-cardinality
   labels and never carry run/session/execution identifiers.
8. Maintain a 30-minute observation window before declaring the metrics gate
   accepted.

## Minimum SLOs and indicators

| Area | Indicator | Objective | Fast alert |
|---|---|---:|---:|
| Metrics path | `up{job="hermes-mcp-bridge"}` | >= 99.5% over 30d | down for 5m |
| Tool reliability | error calls / all calls | < 1% over 30d | > 10% for 10m |
| Short read-only latency | p95 for health/readiness/status/catalogue tools | < 5s over 30d | > 10s for 15m |
| Upstream health | 5xx / all upstream requests | < 1% over 30d | > 5% for 10m |
| Restart stability | unplanned process starts | <= 1 per 24h | any recent start outside change window |
| SQLite durability | SQLite errors | 0 | any error |
| SQLite contention | lock-contention events | < 20 per 10m | > 20 for 10m |
| SSE continuity | fallback rate | < 1% of connected waits | sustained > 0.1/s for 15m |
| RITMO leases | runs beyond 2x lease duration | 0 | any stuck/expired lease |

The bridge does not own the RITMO lease table and therefore cannot prove the
lease SLO from `bridge_*` metrics. That objective requires RITMO-side evidence
or a separate low-cardinality RITMO exporter. Absence of such evidence must be
reported as a production-acceptance limitation, not converted into a synthetic
bridge metric.

Long-running delegated tools are excluded from the short-latency SLO. Their
latency is workload-dependent and should be assessed by completion and recovery
signals rather than a global p95 threshold.

## Restart interpretation

`bridge_process_start_time_seconds` is the process-start signal. It replaces the
old approximation based on changes to `bridge_migrations_version`; a bridge can
restart without applying a migration, so migration changes are not a restart
counter.

A recent-start alert is informational during an approved deployment window and
unexpected outside one. Repeated starts require container restart-count and
host journal correlation.

## Rollback

Metrics rollback is independent of the bridge release:

1. set `BRIDGE_METRICS_ENABLED=0`;
2. restart only the bridge if required to apply that setting;
3. remove or disable only the Hermes bridge scrape fragment in Alloy;
4. reload Alloy;
5. confirm `127.0.0.1:9464` is no longer listening;
6. keep JSON logging, policy, HMAC and the bridge runtime unchanged.

A metrics failure does not by itself justify rolling back bridge `1.0.0` unless
it is proven to affect request execution, memory, CPU, disk or security.

## Acceptance decision

The gate may be declared only after all security invariants, smoke checks,
Grafana Cloud ingestion and the observation window are proven:

```text
HERMES_BRIDGE_1_0_0_METRICS_GATE_PASS
```

This decision is independent from, and does not replace, the required
single-slot Hermes/RITMO acceptance.
