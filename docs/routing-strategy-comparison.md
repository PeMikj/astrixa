# Astrixa Routing Strategy Comparison

## Goal

This document compares the routing strategies used and planned in Astrixa.

## Strategy 1: Model Round Robin

### Behavior

- select providers supporting the requested model
- rotate equally across candidates

### Strengths

- simple
- predictable
- easy to explain

### Weaknesses

- ignores real latency
- ignores degradation beyond coarse filtering
- wastes time on slower providers when better options exist

## Strategy 2: Latency / Health / Priority

### Current Astrixa Behavior

- filter to enabled providers supporting the model
- prefer `healthy` over `degraded`
- honor ejection windows
- use observed latency feedback to rank providers
- use provider priority as a tie-breaker

### Strengths

- adapts to measured provider performance
- reduces traffic to recently failing providers
- creates a control-plane-visible health state
- works with both local probes and runtime feedback

### Weaknesses

- still uses in-process routing stats
- does not yet optimize explicitly for cost
- no full capacity model yet

## Future Strategy: Cost / Capacity / Latency Composite

### Planned Inputs

- health state from registry
- observed latency EMA
- provider cost
- token limits / quota pressure
- policy eligibility

### Intended Outcome

- cheaper providers win when latency and health are acceptable
- unhealthy or cooling-down providers are avoided automatically
- routing decisions become easier to justify during incidents

## Current Recommendation

For Astrixa, latency/health/priority is the correct default strategy at this stage because it materially improves operator outcomes without introducing opaque heuristics too early.

## Evidence From Current Stack

- observed provider latency metrics distinguish mock and real providers clearly
- routing feedback updates health state in registry
- synthetic error and recovery events change provider selection eligibility inputs

