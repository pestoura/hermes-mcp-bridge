# Observability deployment assets (1.0.0)

These files integrate the bridge with an existing monitoring stack. Nothing in
this directory starts a second Prometheus or Alertmanager, publishes a port, or
contains a credential.

| File | Purpose | Status |
| --- | --- | --- |
| `grafana-cloud-loopback.alloy` | Canonical 1.0.0 Grafana Cloud metrics profile | Preferred production path |
| `hermes-bridge.rules.yml` | Alerting rules for the bounded `bridge_*` catalogue | Shared by Alloy/Prometheus-compatible rule evaluators |
| `prometheus-scrape.snippet.yml` | Legacy scrape job through the Docker gateway | Retained for compatibility; not the preferred 1.0.0 profile |
| `alertmanager.example.yml` | Local routing example | Optional and intentionally loopback-only |

## Preferred production path

Run Grafana Alloy on the same host as the bridge and keep the exporter on
loopback:

```text
BRIDGE_METRICS_ENABLED=1
BRIDGE_METRICS_HOST=127.0.0.1
BRIDGE_METRICS_PORT=9464
BRIDGE_METRICS_ALLOW_REMOTE=0
BRIDGE_TRACING_ENABLED=0
BRIDGE_TRACING_EXPORT=0
```

Alloy scrapes `127.0.0.1:9464`, keeps only `bridge_.*`, removes forbidden labels
defensively and remote-writes metrics to Grafana Cloud. Its URL, tenant and
metrics-publish token are read from the protected Alloy service environment;
they are never stored in this directory or in the bridge `.env`.

The canonical runbook is:

```text
docs/observability-production-1.0.0.md
```

## Legacy Docker-gateway path

The legacy Prometheus snippet is retained for an existing Prometheus container.
It requires an explicit non-loopback bind and authentication:

```text
BRIDGE_METRICS_ENABLED=1
BRIDGE_METRICS_HOST=172.17.0.1
BRIDGE_METRICS_ALLOW_REMOTE=1
BRIDGE_METRICS_TOKEN=<32+ random characters>
```

`172.17.0.1` is classified as `docker-gateway`, not loopback. The exporter still
requires both the explicit remote opt-in and a token. Do not use this path when
host-level Alloy is available.

## Validation

Run the repository-owned read-only checks:

```bash
python scripts/observability_smoke.py --check-config --check-logging
```

The smoke validates the Alloy security shape, legacy YAML assets and the JSON
redaction pipeline. Before reloading Alloy, also validate the file using the
installed Alloy version as required by the production runbook.

No asset in this directory is authorization to enable metrics in production.
Activation requires a separate controlled gate and does not replace the
single-slot Hermes/RITMO acceptance.
