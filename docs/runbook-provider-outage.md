# Provider Outage Runbook

## Trigger

Use this runbook when:

- provider requests begin failing repeatedly
- routing latency spikes unexpectedly
- provider-registry marks a provider `degraded` or `unhealthy`

## Immediate Checks

1. Open Grafana and inspect:
   - provider selection distribution
   - gateway latency
   - TTFT / TPOT
   - provider health probe failures
2. Query provider state from registry:

```bash
docker exec astrixa-provider-registry-1 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/v1/providers').read().decode())"
```

3. Inspect routing metrics:

```bash
docker exec astrixa-routing-engine-1 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())"
```

## Expected Astrixa Behavior

- repeated errors trigger routing feedback
- routing feedback updates registry state
- provider may enter `degraded` or `unhealthy`
- `ejected_until` prevents immediate reuse during cooldown

## Operator Actions

- confirm whether failure is isolated to one provider
- verify whether alternate providers still serve the same model
- if needed, manually patch provider state in registry

## Recovery Check

- verify health probes succeed again
- verify provider state returns to `healthy`
- verify traffic distribution normalizes

