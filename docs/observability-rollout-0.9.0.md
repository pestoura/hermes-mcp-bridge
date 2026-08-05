# Observability rollout — 0.9.0 (Block 6C, phase 1)

Operational rollout for the 0.9.0 observability hardening: single-stream log
hygiene, exporter bind-scope classification, container log rotation, and the
deploy snippets for an **existing** monitoring stack.

Scope note: this document covers the *operational* rollout only. It does not
change the tool contract (still 27 tools, wire schema `0.6.1`) and introduces no
SQLite migration. Nothing here is enabled by default.

- [What changed](#what-changed)
- [Safety contract](#safety-contract)
  - [Target posture of this rollout](#target-posture-of-this-rollout)
  - [HMAC secret is mandatory for readiness](#hmac-secret-is-mandatory-for-readiness)
  - [Network exposure and the minimum UFW rule](#network-exposure-and-the-minimum-ufw-rule)
  - [`network_mode: host` is a fallback, not the default](#network_mode-host-is-a-fallback-not-the-default)
- [Files](#files)
- [Procedure](#procedure)
- [Verification](#verification)
- [Rollback](#rollback)
- [Known limitations](#known-limitations)

## What changed

| Area | 0.8.2 behaviour | 0.9.0 behaviour |
| --- | --- | --- |
| Log stream | Bridge events are JSON, third-party libraries print raw unredacted text next to them | Every line parses as JSON; third-party records go through the same redacting formatter |
| Duplicates | A bridge record could be written twice (bridge handler + an ancestor handler) | The bridge-installed root handler filters the `hermes_mcp_bridge` tree; propagation stays on for embedders and `caplog` |
| Third-party level | Library defaults (often `INFO`, per-request noise) | `BRIDGE_LOG_THIRD_PARTY_LEVEL` (default `WARNING`); loggers with an explicit level keep it |
| Warnings | `warnings.warn()` → raw stderr | Captured into logging (`py.warnings`) as JSON |
| Container logs | Unbounded `json-file` driver | `max-size=10m`, `max-file=5` (~50 MiB per service) |
| Exporter bind | `loopback` / `remote` | `loopback` / `docker-gateway` / `remote`; `docker-gateway` is **not** exempt from the gate |
| Exporter status | `bind_scope` only | adds `remote_exposure_allowed` (still no token value) |
| Tracing module | `hermes_mcp_bridge.tracing` | canonical `hermes_mcp_bridge.observability.tracing`; root module is a deprecated re-export |

## Safety contract

- Metrics stay **off by default**; the exporter default bind stays `127.0.0.1`.
- `docker-gateway` (`172.17.0.1`, `host.docker.internal`) is classified apart
  from loopback purely for reporting. Binding it still requires
  `BRIDGE_METRICS_ALLOW_REMOTE=1` **and** a non-empty `BRIDGE_METRICS_TOKEN`;
  `validate_binding()` fails closed otherwise.
- Authorization is per request: `Authorization: Bearer <token>`. A bare token
  without the scheme is rejected.
- No port is published by `compose.yml` and no deploy asset publishes one.
- No credential is committed. Prometheus reads the bearer token from disk via
  `authorization.credentials_file`.
- No second Prometheus or Alertmanager is started: the assets are snippets to
  merge into the existing stack.
- Log hygiene is best-effort and never raises; a failure to quiet a library
  cannot break the bridge. Warnings and errors from libraries are never hidden.

### Target posture of this rollout

| Feature | Default in the code | Target after this rollout |
| --- | --- | --- |
| Metrics exporter (`BRIDGE_METRICS_ENABLED`) | off | **on**, bound to the docker gateway or loopback, token required |
| Tracing (`BRIDGE_TRACING_ENABLED`, `BRIDGE_TRACING_EXPORT`) | off | **stays off** — no OTLP endpoint, no collector, no span export is part of this rollout |
| HMAC signing (`HERMES_BRIDGE_HMAC_SECRET`) | unset | **must be set** (see below) |

Enabling the exporter is the only default this rollout changes on the deployed
host; the committed defaults in the repository stay metrics-off and tracing-off.

### HMAC secret is mandatory for readiness

`hermes_readiness` reports `not_ready` whenever the 6B security posture has
`failing = ["hmac"]`, which is the case whenever `HERMES_BRIDGE_HMAC_SECRET` is
absent or too short. This is the fail-closed model working as designed, and it
is independent of observability: every observability component can be `ready`
while the overall status is `not_ready`.

Therefore **the rollout must provide `HERMES_BRIDGE_HMAC_SECRET`** (32+ random
characters, delivered as a secret file / operator secret store, never in the
repository and never in the compose file inline) *before* readiness is used as
a deploy gate. Do not treat `not_ready` caused by a missing HMAC secret as an
observability regression, and do not relax the readiness gate to work around it.

### Network exposure and the minimum UFW rule

The exporter socket on `172.17.0.1:9464` is only reachable from containers if
the host firewall allows that ingress. On a host with `ufw` active and the
default `INPUT DROP` policy, a containerized Prometheus **cannot** scrape the
gateway bind until a rule is added. This is a host-firewall fact, not a bridge
defect: the same block reproduces with a trivial `python -m http.server` bound
to `172.17.0.1`.

Minimum rule — narrowest scope that works, and nothing wider:

```
# Preferred: only the Prometheus container's own address.
sudo ufw allow from 172.17.0.5 to 172.17.0.1 port 9464 proto tcp \
  comment 'prometheus -> hermes-mcp-bridge exporter'

# Acceptable fallback when the container IP is not pinned: the docker bridge
# subnet only.
sudo ufw allow from 172.17.0.0/16 to 172.17.0.1 port 9464 proto tcp \
  comment 'docker bridge -> hermes-mcp-bridge exporter'
```

Rules that are explicitly **forbidden** in this rollout:

- `ufw allow 9464/tcp` — opens the port to every source, including the LAN.
- `ufw allow from any to any port 9464` — same problem, stated differently.
- Any `ports:` mapping for the exporter in `compose.yml`, any reverse-proxy
  vhost, any Cloudflare/tunnel route, any DNS name for the exporter.

External exposure of `/metrics` is **denied**: the exporter has no TLS, no
authorization beyond a static bearer token, and no rate limiting. It is an
internal, host/subnet-scoped socket only. If metrics must leave the host, that
is a separate design (TLS front door, mTLS or a remote-write agent), not a
firewall rule.

Verify the rule is doing exactly what is intended, from inside a container:

```
# expected: 401 without a token, 200 with it
docker run --rm --tmpfs /tmp:size=8m curlimages/curl:8.10.1 \
  -s -o /dev/null -w '%{http_code}\n' http://172.17.0.1:9464/metrics
```

And confirm it is *not* reachable from another host on the LAN.

### `network_mode: host` is a fallback, not the default

Sharing the host network namespace with the Prometheus container makes the
exporter reachable on `127.0.0.1:9464` without any firewall change, because the
traffic never crosses the `docker0` bridge. (The bridge service itself already
runs with `network_mode: host` in `compose.yml`; this section is about the
*Prometheus* container, and that setting is unchanged by this rollout.)

This is an **alternative of last resort**, not the recommended topology:

- it removes the container's network isolation entirely;
- it exposes every other loopback service on the host to that container;
- it makes port conflicts a host-wide concern.

Default topology stays: exporter on `172.17.0.1:9464`, token required, plus the
narrow UFW rule above. Only use `network_mode: host` (on the *Prometheus* side)
when adding the firewall rule is not possible, and record the decision.

## Files

| Path | Role |
| --- | --- |
| `src/hermes_mcp_bridge/observability/quiet.py` | Single-stream log hygiene policy (new) |
| `src/hermes_mcp_bridge/observability/logging.py` | Applies the policy in `configure_logging()`; `observability_status()["hygiene"]` |
| `src/hermes_mcp_bridge/observability/exporter.py` | `bind_scope()`, `is_docker_gateway()`, `remote_exposure_allowed` in status |
| `src/hermes_mcp_bridge/observability/tracing.py` | Canonical tracing (now owns `build_trace_metadata`, `sanitize_trace_context`, `tracing_readiness`) |
| `src/hermes_mcp_bridge/tracing.py` | Deprecated re-export shim |
| `compose.yml` | `json-file` rotation, third-party log env vars |
| `deploy/observability/prometheus-scrape.snippet.yml` | One scrape job, token via file |
| `deploy/observability/hermes-bridge.rules.yml` | Alerting rules, allow-listed labels only |
| `deploy/observability/alertmanager.example.yml` | Routing example, loopback receiver |
| `deploy/observability/README.md` | Asset usage and security preconditions |
| `scripts/observability_smoke.py` | Offline config/logging checks + optional authenticated probe |
| `tests/test_observability_block6c_0_9_0.py` | Directed tests for all of the above |

## Procedure

Run in order. Steps 1–3 are offline and safe on any host.

1. **Validate the assets offline.**

   ```
   python scripts/observability_smoke.py --check-config --check-logging
   ```

   Expect `scrape job ok`, `rules ok`, `alertmanager example ok`, `logging ok`.

2. **Deploy the code with metrics still off.** Recreate the service so the new
   logging driver applies (`logging:` is only read at container creation).
   Confirm the stream:

   ```
   docker compose logs --no-log-prefix --tail 50 hermes-mcp-bridge \
     | while read -r line; do printf '%s' "$line" | python -c 'import json,sys; json.loads(sys.stdin.read())'; done
   ```

   Every line must parse. Confirm rotation is active with
   `docker inspect --format '{{json .HostConfig.LogConfig}}' <container>`.

3. **Decide the scrape topology.** If Prometheus runs on the host, keep the
   exporter on `127.0.0.1` and skip step 4's remote flags. Only a Prometheus in
   another container needs the docker gateway.

4. **Enable the exporter.** Write the token to the operator secret store, then
   set in the environment file:

   ```
   BRIDGE_METRICS_ENABLED=1
   BRIDGE_METRICS_HOST=172.17.0.1        # or 127.0.0.1 for a host-local Prometheus
   BRIDGE_METRICS_ALLOW_REMOTE=1         # only with the gateway/remote bind
   BRIDGE_METRICS_TOKEN=<32+ random chars>
   ```

   Install the same value for Prometheus, readable only by it:

   ```
   install -d -m 0700 -o prometheus -g prometheus /etc/prometheus/secrets
   install -m 0600 -o prometheus -g prometheus /dev/null \
     /etc/prometheus/secrets/hermes_bridge_metrics_token
   ```

   Add the narrow UFW rule from
   [the safety contract](#network-exposure-and-the-minimum-ufw-rule) only if
   Prometheus runs in a container and needs the gateway bind. Also confirm
   `HERMES_BRIDGE_HMAC_SECRET` is set, otherwise readiness stays `not_ready`.

5. **Probe the exporter before wiring Prometheus.**

   ```
   python scripts/observability_smoke.py \
     --probe http://172.17.0.1:9464/metrics \
     --token-file /etc/prometheus/secrets/hermes_bridge_metrics_token
   ```

   Verify the negative case too: the same URL without a token must return 401.

6. **Merge the scrape job** into the existing `prometheus.yml`, reload
   Prometheus, and confirm the target is `UP` with the expected `bridge_*`
   series present.

7. **Load the rules** via `rule_files:` and reload. Confirm the rules appear and
   that none is immediately firing for a reason unrelated to a real incident.

8. **Route the alerts** using `alertmanager.example.yml` as a base. The example
   ships a loopback receiver on purpose — replace it deliberately with the real
   destination; that replacement is the only place a real credential appears,
   and it lives in the operator's Alertmanager config, never in this repo.

## Verification

| Check | Command | Expected |
| --- | --- | --- |
| Lint | `ruff check .` | `All checks passed!` |
| Bytecode | `python -m compileall -q src scripts tests` | no output |
| Directed tests | `pytest tests/test_observability_block6c_0_9_0.py -q` | all pass |
| Full observability surface | `pytest tests -q -k "observability or tracing"` | all pass |
| Offline smoke | `python scripts/observability_smoke.py` | exit 0 |
| Authenticated probe | `--probe … --token-file …` | `HTTP 200`, N `bridge_*` series |
| Unauthenticated probe | same URL, no token | HTTP 401 |

## Rollback

Every step is independently reversible and none touches persistent state:

1. Alerts: remove the entry from `rule_files:` and reload Prometheus.
2. Scraping: remove the scrape job and reload.
3. Exporter: unset `BRIDGE_METRICS_ENABLED` (and the remote flags) and restart.
   The bridge runs identically with the exporter off.
4. Log hygiene: set `BRIDGE_LOG_CAPTURE_THIRD_PARTY=0` to leave the root logger
   to the embedding application. Bridge events remain JSON and redacted.
5. Rotation: revert the `logging:` block in `compose.yml` and recreate the
   container.

No SQLite migration is involved, so there is no data rollback.

## Known limitations

- Log rotation is a `compose.yml` setting: hosts running the bridge outside
  Compose must configure the daemon default or their own supervisor.
- `BRIDGE_LOG_CAPTURE_THIRD_PARTY=1` re-emits third-party records through the
  bridge formatter. A library that logs an already-JSON string will appear as a
  JSON event whose message field contains that JSON as text — one parse level,
  by design, so per-line parsing never breaks.
- A logger that sets its own level explicitly is left alone. A library defaulting
  to `INFO` at import time before `configure_logging()` runs keeps that level.
- `--probe` performs a single GET; it is a reachability and authorization check,
  not a load test.
- The rules use static thresholds. Tune them against the first week of real
  traffic before paging on them.
- **Host firewall**: on a `ufw`-enabled host with `INPUT DROP`, the
  `172.17.0.1:9464` bind is unreachable from containers until the narrow rule
  above is applied. Validated in Block 6C phase 2: the authenticated scrape
  contract itself is proven (401 without token, 200 with, `bridge_*` series
  returned) using a client sharing the host network namespace; only the bridge
  traversal is firewall-blocked. Applying the rule is a production firewall
  change and is **not** part of this repository change.
- The exporter has no TLS and only a static bearer token; it must never be
  published, proxied or given a public hostname.
