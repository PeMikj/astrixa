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

