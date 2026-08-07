# ADR-007: Separate token refresh from API access and deploy with least privilege

> **Current capability boundary:** the packaged skill accepts an externally
> supplied bearer token and performs read-only API calls. Token refresh,
> browser-session automation, and scheduled orchestration are future design
> material, not implemented capabilities.

- Status: Accepted
- Scope: authentication, unattended operation, and scheduled execution

## Context

The provider API uses short-lived bearer access tokens. Directly replaying a
vendor web client's secret or storing a user's password creates unnecessary
credential risk and couples the deployment to undocumented authentication
details.

## Decision

Keep authentication and transaction access as separate components. The API
client consumes a fresh bearer token and performs plain HTTP reads only; it
does not invent credentials or silently re-authenticate. If unattended refresh
is required and the provider web app can refresh its own authenticated session,
run that app in an isolated browser session and read the resulting access token
from the session state. Do not extract or replay the provider's client secret,
and do not store a user password for this workflow.

Protect browser session state and API secrets with an encrypted secret store,
strict file permissions, in-memory decryption, bounded timeouts, and key-only
logging. Validate token shape/expiry with a safety margin before a run. If the
session is revoked, the token is stale, or the app's storage contract changes,
stop and alert for interactive reauthentication rather than guessing.

Deploy under a dedicated unprivileged service account with a hardened API
profile. Keep any browser profile separate and invoke it only when required.
The future scheduled pipeline is read-only: validate a supplied token, ingest,
analyze, and report. Token refresh and notifications are outside the current
skill. Classify outcomes as success, degraded, or hard failure, and base any
future alerting on the stored outcome rather than process exit alone.

## Consequences

The normal data path remains lightweight and independent of browser automation;
credential scope and blast radius stay small; authentication failures are
visible and recoverable through an interactive path.

## Non-scope

Provider terms, host-specific service files, secret values, browser state, and
account profiles must be decided and stored by each deployment.
