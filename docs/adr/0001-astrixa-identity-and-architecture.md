# ADR 0001: Astrixa Identity And Architecture

## Status

Accepted

## Context

Many LLM gateways converge toward the same failure mode: one service becomes a compatibility proxy, policy engine, registry, router, and metrics emitter all at once. That shape is fast to demo but hard to secure, evolve, or operate.

The project goal is to build Astrixa as a distinct platform with a strong identity, not a branded clone of an existing gateway.

## Decision

Astrixa will be built around these architectural commitments:

- separate control-plane services from the low-latency request path
- keep provider-specific behavior inside adapters
- make routing decisions inspectable and governed
- keep guardrails and auth as explicit subsystems in the request graph
- treat agent and provider registration as first-class APIs

## Consequences

- initial scaffolding is slightly heavier than a single-service prototype
- long-term maintainability, security review, and incident response improve
- compatibility layers can be added later without dictating internals

## Security Impact

The split reduces blast radius, limits hidden coupling, and makes policy enforcement points easier to audit.

