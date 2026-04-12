# Provider Ejection Checklist

## Goal

Verify that Astrixa ejects a degraded provider from routing and later restores it.

## Procedure

1. Create or select a mock provider entry.
2. Send synthetic error feedback through routing-engine.
3. Confirm registry state contains:
   - `health_source: routing-feedback`
   - `health_status: degraded` or `unhealthy`
   - `ejected_until` set
4. Send synthetic success feedback.
5. Confirm registry state returns to:
   - `health_status: healthy`
   - `ejected_until: null`
   - `last_error: null`

## Current Result

This scenario has been verified manually in the local Compose stack.

## Automated Path

The same scenario is now executable through:

```bash
python3 tests/resilience/run_provider_ejection.py
```

The script:

1. ensures `mock-echo-secondary` exists,
2. injects synthetic provider failure through `routing-engine`,
3. verifies degraded/ejected state in `provider-registry`,
4. confirms gateway continuity during ejection,
5. injects recovery feedback,
6. verifies return to `healthy`.
