# Astrixa Submission Checklist

## Core Platform

- multi-provider LLM routing implemented
- streaming response path implemented
- dynamic provider registry implemented
- agent registry implemented
- Docker Compose deployment implemented

## Security

- ingress bearer auth enforced
- guardrails documented explicitly
- prompt-injection blocking implemented
- secret-pattern blocking implemented
- response sanitization implemented

## Routing

- latency-aware selection implemented
- health-aware selection implemented
- provider ejection and recovery implemented
- synchronized health state written to registry

## Observability

- Prometheus metrics exposed
- Grafana dashboard provisioned
- TTFT metrics implemented
- TPOT metrics implemented
- token and cost telemetry implemented
- Prometheus alert rules added

## Operations

- deployment guide present
- testing report present
- routing strategy comparison present
- provider outage runbook present
- load test scaffold present
- resilience checklist present

## Persistence

- provider-registry state survives restart
- persistent local volume configured for registry

## Remaining Stretch Items

- response-side guardrails as dedicated service
- managed database instead of local SQLite
- automated chaos suite
- longer sustained load tests
- alert delivery integrations

