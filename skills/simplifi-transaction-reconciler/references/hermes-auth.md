# Hermes browser-session authentication

> **Future deployment reference only.** The current Simplifi skill does not
> refresh access tokens, automate a browser session, or notify an operator. It
> accepts an externally supplied token and stops when authentication is stale.

Deployment reference for the browser-session refresh architecture, status
2026-08-05: architecture decided, refresher not yet built.

## Contents

- [Verified facts](#verified-facts)
- [Architecture](#architecture)
- [Build checklist](#build-checklist)
- [Failure checklist](#failure-checklist)
- [Security boundary and open checks](#security-boundary-and-open-checks)

## Verified facts

In a logged-in `simplifi.quicken.com` session, `localStorage.authSession`
contains:

```text
accessToken       JWT, exactly 1 hour
refreshToken      JWT, 1095 days; observed expiry 2029-08-03
keepLoggedIn      true
```

`POST https://services.quicken.com/oauth/token` exists, but direct calls with
`client_id=acme_web` are rejected because the web app also uses a Quicken-owned
client secret. Do not extract or replay that secret.

## Architecture

Hermes runs the logged-in Simplifi browser session and lets Simplifi's own
JavaScript refresh the access token. The refresher reads the resulting
`authSession.accessToken` from `localStorage` and passes it to the existing
plain-HTTP API client. It does not call `/oauth/token`, store a password, or
mix browser automation into the data-read path.

Persist Playwright `storage_state` for `simplifi.quicken.com`; it contains the
account's refresh token and must be encrypted at rest in the existing age vault.
Persist the possibly rotated state after each successful run.

## Build checklist

### One-time interactive setup

- Install Chromium and Playwright.
- Use a headed browser to log into Simplifi on Hermes; handle 2FA interactively
  if prompted.
- Save `storage_state.json`, encrypt it in the age vault, and confirm
  `keepLoggedIn` is true.

### Each unattended run

1. Launch headless Chromium with the encrypted/decrypted storage state.
2. Navigate to `https://simplifi.quicken.com/` or another authenticated route.
3. Force refresh by backdating `authSession.accessTokenExpired` and reloading,
   or use the app's own refresh timer if verified.
4. Read `authSession.accessToken` from `localStorage`.
5. Verify it is a JWT with more than 50 minutes remaining; reject a stale read.
6. Update `SIMPLIFI_ACCESS_TOKEN` through the deployment's protected secret
   handoff; do not clobber other vault entries.
7. Persist rotated browser state, encrypted, and exit non-zero on any failure.
8. Run the existing read-only ingest/analyze pipeline with the fresh token.

## Failure checklist

- **Missing or revoked session:** no `authSession`, an `error` field, or a stale
  token after reload. Alert Pavel for a new headed login; never fall back to
  stored credentials.
- **2FA on cold login:** expected only during initial setup or interactive
  re-login after revocation. Do not attempt unattended bypass.
- **App storage contract changed:** verify `authSession` exists every run;
  alert if the app moves it or refresh state to another mechanism such as
  IndexedDB.
- **Refresh did not advance expiry:** fail closed, do not write the stale token,
  and alert.
- **Refresh-token rotation:** always save updated storage state so a rotated
  token is not lost.
- **Downstream API failure:** keep refresh and API access separate; let the API
  client report auth, schema, network, or expiry errors distinctly.

## Security boundary and open checks

The only long-lived credential held by Hermes is Pavel's revocable browser
session. Protect it with the age vault, strict permissions, bounded timeouts,
and key-only logging. The future scheduled pipeline remains read-only: ingest,
analyze, and report. Token refresh and notifications are outside the current
skill and require separate implementation and authorization.

Before finalizing the refresher, verify whether the app refreshes on a timer
and whether reload rotates the refresh token. The latter determines whether
persisting browser state is mandatory (it is the safe default).
