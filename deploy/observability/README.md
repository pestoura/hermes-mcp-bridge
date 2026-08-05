# Observability deployment assets (0.9.0)

These files are **snippets for an existing monitoring stack**. Nothing here
starts a new Prometheus, a new Alertmanager or publishes a port.

| File | Purpose | How to use |
| --- | --- | --- |
| `prometheus-scrape.snippet.yml` | Scrape job for the bridge exporter | Merge the `scrape_configs` entry into your existing `prometheus.yml` |
| `hermes-bridge.rules.yml` | Alerting rules | Reference from `rule_files:` |
| `alertmanager.example.yml` | Routing example | Merge route + receiver; replace the loopback receiver deliberately |

## Security preconditions

The exporter is **off by default and loopback-only**. Scraping from another
container requires all four of:

```
BRIDGE_METRICS_ENABLED=1
BRIDGE_METRICS_HOST=172.17.0.1
BRIDGE_METRICS_ALLOW_REMOTE=1
BRIDGE_METRICS_TOKEN=<32+ random chars>
```

`172.17.0.1` is classified by the exporter as `docker-gateway`, **not** as
loopback. It is deliberately *not* exempt from the remote gate: the token stays
mandatory and `validate_binding()` still refuses the bind without both the
opt-in and the token. The scope exists only so `bridge_health` can report
"reachable from the docker network" separately from "reachable from anywhere".

The token is never written into any file in this directory. Prometheus reads it
from disk:

```
install -d -m 0700 -o prometheus -g prometheus /etc/prometheus/secrets
install -m 0600 -o prometheus -g prometheus /dev/null \
  /etc/prometheus/secrets/hermes_bridge_metrics_token
# write the same value as BRIDGE_METRICS_TOKEN into that file
```

## Verifying before enabling alerts

```
python scripts/observability_smoke.py --check-config
```

See `docs/observability-rollout-0.9.0.md` for the full rollout order.
