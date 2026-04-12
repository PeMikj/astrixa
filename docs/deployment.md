# Astrixa Deployment Guide

## Scope

This document describes how to run Astrixa locally with Docker Compose and what components are expected to be available in the first production-oriented environment.

## Local Deployment

### Prerequisites

- Docker Engine with Compose support
- free local ports:
  - `18080` for `api-gateway`
  - `8082` for `agent-registry`
  - `8090` for `mock-llm`
  - `3000` for Grafana
  - `5001` for MLflow
  - `9090` for Prometheus
  - `4317` for OTLP gRPC

### Environment

The local stack reads secrets and provider configuration from [`.env`](/home/p/astrixa/.env).

Current important keys:

- `ASTRIXA_GATEWAY_TOKEN`
- `ASTRIXA_AGENT_DEMO_TOKEN`
- `ASTRIXA_STATIC_TOKENS_JSON`
- `AICOHORT_BASE_URL`
- `AICOHORT_API_KEY`
- `AICOHORT_MODEL`
- `MISTRAL_BASE_URL`
- `MISTRAL_API_KEY`
- `MISTRAL_MODEL`

### Start

```bash
docker compose up -d --build
```

### Verify

```bash
curl -sS http://127.0.0.1:18080/healthz
curl -sS http://127.0.0.1:8082/healthz
curl -sS http://127.0.0.1:5001/health
```

### Example Request

```bash
curl -sS -X POST http://127.0.0.1:18080/v1/chat/completions \
  -H 'authorization: Bearer astrixa-dev-token' \
  -H 'content-type: application/json' \
  -d '{
    "model": "mock-1",
    "messages": [{"role": "user", "content": "hello from Astrixa"}]
  }'
```

## Observability Endpoints

- Grafana: `http://127.0.0.1:3000`
- MLflow: `http://127.0.0.1:5001`
- Prometheus: `http://127.0.0.1:9090`
- Gateway metrics: `http://127.0.0.1:18080/metrics`

## Agent-Scoped Auth Example

First register an agent with a token-env reference:

```bash
curl -sS -X POST http://127.0.0.1:8082/v1/agents \
  -H 'content-type: application/json' \
  -d '{
    "agent_id": "demo-agent",
    "name": "Demo Agent",
    "description": "Local test agent",
    "url": "http://demo-agent:8080",
    "supported_methods": ["chat.completions"],
    "auth": {
      "type": "bearer_token",
      "token_env": "ASTRIXA_AGENT_DEMO_TOKEN",
      "scopes": ["llm:invoke"]
    }
  }'
```

Then call the gateway with the matching bearer token and agent id:

```bash
curl -sS -X POST http://127.0.0.1:18080/v1/chat/completions \
  -H 'authorization: Bearer astrixa-agent-demo-token' \
  -H 'x-astrixa-agent-id: demo-agent' \
  -H 'content-type: application/json' \
  -d '{
    "model": "mock-1",
    "messages": [{"role": "user", "content": "hello from an authenticated agent"}]
  }'
```

## Stateful Paths

- provider registry persistence:
  - host path: [data/provider-registry](/home/p/astrixa/data/provider-registry)
  - container path: `/data`

## Production-Oriented Notes

- replace local `.env` secret handling with an external secret store
- move from local bind volumes to managed persistent volumes
- front the gateway with managed ingress and TLS
- use a real database for provider and agent control-plane state
- add alerting rules and on-call destinations before production traffic
