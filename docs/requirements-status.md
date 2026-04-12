# Requirements Status

This document maps Astrixa against the assignment requirements and marks each item as `done`, `partial`, or `missing`.

## Level 1

- `done` Docker Compose deployment for all current components via [docker-compose.yml](/home/p/astrixa/docker-compose.yml).
- `done` Multiple LLM providers:
  - local mock provider
  - AI Cohort real provider
  - env-based Mistral provider path
- `done` Basic LLM balancer:
  - routing by model name
  - weighted / round-robin-capable control-plane design
  - streaming-safe passthrough in [services/api-gateway/app/main.py](/home/p/astrixa/services/api-gateway/app/main.py)
- `done` Minimal monitoring:
  - OpenTelemetry instrumentation
  - Prometheus metrics
  - Grafana dashboards
  - health endpoints for every service
- `partial` CPU observability:
  - process-level metrics are exposed through the Python Prometheus client
  - CPU-specific dashboarding is present only at a basic level, not as a full host/node observability setup

## Level 2

- `done` A2A Agent Registry with registration and lookup in [services/agent-registry/app/main.py](/home/p/astrixa/services/agent-registry/app/main.py).
- `done` Dynamic LLM provider registry with:
  - URL
  - price metadata
  - limits
  - priority
  - health metadata
  - persistence in SQLite
- `done` Advanced routing:
  - latency-aware selection
  - health-aware selection
  - temporary provider ejection on failures
  - synchronized recovery via provider registry state
- `done` Extended observability:
  - TTFT
  - TPOT
  - input tokens
  - output tokens
  - request cost
- `missing` MLflow tracing for agents and LLM runs

## Level 3

- `done` Guardrails:
  - prompt-injection detection
  - secret leakage detection
  - structured allow/block verdicts
- `partial` Authorization:
  - gateway bearer-token validation is implemented
  - upstream provider bearer auth is implemented
  - full agent-specific authn/authz lifecycle is not yet implemented
- `partial` Testing and operations:
  - benchmark runner exists
  - real load snapshots are documented
  - provider failure and ejection scenarios were executed
  - sustained load and fully automated chaos coverage remain incomplete

## Current Gap Summary

- `missing` MLflow integration
- `partial` full agent authorization model
- `partial` extended CPU/node observability depth
- `partial` sustained high-volume load and automated chaos coverage

## Recommendation

The highest-value remaining item with moderate implementation cost is MLflow or a narrower tracing substitute. The most expensive remaining work is a serious automated chaos/load campaign with stronger infrastructure-level observability.
