# Astrixa Testing Report

## Status

This report summarizes the current verified behavior of the Astrixa stack in the local Compose environment.

## Verified Areas

### Gateway

- `GET /healthz` returns healthy status
- bearer token is required for `POST /v1/chat/completions`
- request headers expose auth and guardrail decisions

### Guardrails

- prompt-injection pattern blocks with `403`
- obvious secret pattern blocks with `403`
- guardrail policy version is returned in headers and body
- response-side guardrails sanitize unsafe provider fields before client delivery

### Routing

- local mock routing works for `mock-1`
- real provider routing works for `research-model`
- routing engine records observed latency and health feedback
- synchronized provider ejection/recovery updates provider-registry state

### Streaming

- streaming passthrough works on mock provider
- streaming still works after auth and guardrails were added

### Observability

- Prometheus scrapes service metrics
- Grafana dashboard assets are provisioned
- gateway exposes TTFT, TPOT, token, and cost metrics
- provider-registry exposes health-probe metrics
- Prometheus scrapes host CPU metrics through `node-exporter`
- Prometheus derives service CPU usage from `process_cpu_seconds_total`

### Persistence

- provider-registry state survives container restart
- test provider `mock-echo-secondary` remained present after restart
- agent-registry state survives container restart
- authenticated agent registration remains available after `agent-registry` restart

## Manual Test Evidence

Observed successful checks include:

- unauthorized request returns `401`
- prompt-injection request returns `403`
- secret-leak pattern returns `403`
- `research-model` request succeeds through `aicohort-research`
- response sanitization removes `reasoning` fields from provider responses
- response guardrails now run through `guardrails-engine`, not only gateway-local sanitization
- synthetic routing error feedback moves provider to `degraded`
- synthetic routing success feedback restores provider to `healthy`
- active health probes can restore an ejected mock provider back to `healthy`
- authenticated `demo-agent` request succeeds after agent-scoped auth wiring
- MLflow records gateway runs with provider and agent-context tags
- automated provider ejection scenario is executable via `tests/resilience/run_provider_ejection.py`
- mixed load plus timed provider ejection is executable via `tests/resilience/run_mixed_load_resilience.py`
- aggregate suite is executable via `tests/run_submission_suite.py` and writes JSON/Markdown artifacts to `tests/artifacts/`

## Benchmark Snapshot

### Baseline Local Load

Scenario:

- target: `mock-1`
- requests: `50`
- concurrency: `10`
- auth enabled
- guardrails enabled

Observed result:

- success rate: `100%`
- throughput: `5.21 req/s`
- average latency: `1831.40 ms`
- p50 latency: `1963.48 ms`
- p95 latency: `2278.33 ms`

### Resilience Snapshot

Scenario:

- synthetic routing feedback ejected `mock-echo-secondary`
- benchmark rerun against `mock-1`

Observed result:

- success rate: `100%`
- throughput: `8.78 req/s`
- average latency: `1097.40 ms`
- p50 latency: `1140.10 ms`
- p95 latency: `1600.11 ms`

Interpretation:

- service remained available during provider health state changes
- mock provider recovery was observed quickly due to active probe loop
- this run validates continuity, not a worst-case outage benchmark

## Current Gaps

- no heavy sustained load run executed yet
- no fully automated chaos suite executed yet
- registry persistence currently uses local SQLite rather than managed database
- node/process telemetry is implemented locally, but production deployments still need environment-specific hardening
- broader automated chaos coverage beyond provider ejection is still incomplete

## Recommended Next Validation

1. Run longer sustained load against `mock-1` and `research-model`.
2. Execute provider failure drills while collecting latency and availability metrics.
3. Extend automated assertions beyond provider ejection into multi-provider and longer-duration failure scenarios.
4. Export dashboard screenshots and benchmark tables for final submission.
