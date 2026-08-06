# Simplifi private API reference

Verified against the live web app on 2026-08-04/05. The capture recorded
request/response structure only; no balances, payees, account numbers, or
tokens were retained. This is a private web-app API, not a portable contract.

## Contents

- [Client and authentication](#client-and-authentication)
- [Envelope and pagination](#envelope-and-pagination)
- [Observed endpoints](#observed-endpoints)
- [Transaction read schema](#transaction-read-schema)
- [Incremental and date-bounded reads](#incremental-and-date-bounded-reads)
- [Writes and refresh jobs](#writes-and-refresh-jobs)
- [Connection health](#connection-health)
- [Superseded hypotheses](#superseded-hypotheses)

## Client and authentication

Host: `https://services.quicken.com`.

- The API uses `Authorization: Bearer <access-token>`; cookies alone return
  `401` even with `credentials: include`.
- The current client sends these seven headers: `Accept`, `app-client-id`,
  `app-release`, `app-build`, per-request `client-tid`, `Authorization`, and
  `qcs-dataset-id` for dataset-scoped calls. Missing the client and dataset
  headers can produce a misleading `500`.
- `GET /userprofiles/me` is the cheap token probe. Resolve the dataset from
  `GET /datasets`, then send its ID as `qcs-dataset-id`.
- Measured access-token lifetime is exactly one hour (`exp - iat`). Decode JWT
  claims locally for expiry metadata without treating the unverified payload as
  authorization. Refuse an expired token; warn below six hours remaining.
  Opaque tokens cannot be checked locally.
- Do not replay the web client's secret or store a Simplifi password. For
  unattended refresh, use the browser-session architecture in
  [Hermes auth](hermes-auth.md).

## Envelope and pagination

Collection responses use:

```json
{
  "resources": [],
  "metaData": {
    "pageSize": 0, "limit": 0, "asOf": "", "currentPage": 0,
    "offset": 0, "totalPages": 0, "totalSize": 0,
    "nextLink": "..."
  }
}
```

Pagination is keyset-based. Follow `metaData.nextLink` verbatim, including its
`after=<sort-field>;<value>;<id>` cursor. Do not construct offset or page
queries: `offset`, `page`, and `currentPage` parameters were accepted but
silently re-served page one. `totalPages` and `totalSize` describe the current
page and are not reliable stop conditions. The adapter de-duplicates IDs and
uses a bounded page circuit breaker.

## Observed endpoints

| Method/path | Observed query/body | Use |
|---|---|---|
| `GET /userprofiles/me` | — | token probe |
| `GET /datasets` | `limit`, `modifiedAfter` | dataset ID |
| `GET /datasets/{id}/entitlements` | `limit` | entitlements |
| `GET /accounts` | `limit`, `modifiedAfter` | accounts |
| `GET /transactions` | `dateOnAfter`, `limit`, `modifiedAfter` | transactions |
| `GET /transactions/earliest-date-on` | — | history lower bound |
| `GET /categories`, `/tags` | `limit`, `modifiedAfter` | reference data |
| `GET /institution-logins` | `limit`, `modifiedAfter` | connections and health |
| `GET /institutions/fi-issues` | `institutionIds` | incident/care-code supplement |
| `PUT /transactions/{id}` | full transaction document | category/transaction write |
| `GET /job-statuses`, `/job-statuses/{id}` | — | async job polling |
| `POST /institution-logins/refresh` | `loginRefreshCredentials`, `investmentAggregationType` | bank refresh |
| `GET /scheduled-transactions` | `includeCascadeDeleted`, `limit`, `modifiedAfter` | scheduled rows |
| `GET /spending-watchlist`, `/free-to-spend` | `limit`, `modifiedAfter` | planning data |
| `GET /goals`, `/alert/alerts`, `/filters`, `/documents` | `limit`, `modifiedAfter` | other collections |

Also observed: investment v2 holdings/securities/quotes, reports,
preferences, and several unrelated product endpoints. Treat all paths as
observed implementation details.

## Transaction read schema

A live `GET /transactions` and `GET /transactions/{id}` returned the same
18-field shape:

```text
accountId amount clientId coa createdAt dbVersion id isBill matchState
modifiedAt payee postedOn source stDueOn stModelId state type userModifiedAt
```

Verified read-side capabilities:

- `id` is a stable provider transaction ID; use it instead of a synthetic hash.
- API `payee` is the underlying bank descriptor. In a `(date, amount)` cross-
  source match, 904/1,565 rows (58%) differed from the CSV's renamed payee;
  examples included store numbers, state suffixes, and processor text.
- `coa.type` is authoritative. Observed types include `CATEGORY`, `ACCOUNT`
  (transfers), `UNCATEGORIZED`, and `BALANCE_ADJUSTMENT`. Category resources
  expose `name` and `parentId`; build display paths by walking parents.
- `state` exposes `PENDING` versus `CLEARED`; the CSV has no pending flag.
  `isBill` is populated in the sampled data. `stDueOn`/`stModelId` expose
  scheduled-transaction markers when present.
- No currency field was observed. GET responses contain no `cpData`, `split`,
  or `isSubscription`; do not claim those read capabilities.

`BALANCE_ADJUSTMENT` is a closed semantic case from 1,565 unambiguous
CSV/API matches: exactly 68 API rows disagreed with the CSV kind (38 CSV
`Transfer`, 30 CSV `Credit Card Payment`). The API has no category for them;
`coa.id` is the sentinel `"0"` or `"2"`, not a category ID. The UI derives
paired credit-card payments client-side. Both labels poison statistics and are
uncategorized, so the reconciler does not reconstruct the display label.

The PUT request body is richer than the GET response because the app assembles
it from client-side state. It was observed to include the full document fields
`type`, `id`, `accountId`, `postedOn`, `payee`, `memo`, `coa`, `amount`, `split`,
`state`, `matchState`, review/exclusion flags, `isBill`, `isSubscription`, and
`cpData` (including `txnOn`, `inferredPayee`, and `inferredCoa`). Treat these
as write-payload requirements, not as bulk-read schema.

## Incremental and date-bounded reads

Changing the visible date range in the app does not fetch a new transaction
window: the app syncs broadly, caches, and filters locally. The API's real
incremental cursor is `modifiedAfter`; persist it with fetch provenance and
advance it only after a complete successful fetch. A failed or partial fetch
must not advance the cursor. Keep a deliberate full-scan/reconciliation path.

Use `dateOnAfter` to bound transaction history explicitly. `dateStart`/
`dateEnd` are app view state, not observed API parameters; no `dateBefore` was
observed.

## Writes and refresh jobs

`PUT /transactions/{id}` is a full-document write, not a PATCH. It returns a
job envelope such as `{ "id": "...", "status": "...", "explanation": "..." }`,
not the updated transaction. Re-fetch immediately before writing, deep-copy
the live document, change only the intended field, preserve the exact
pre-write document, and poll `/job-statuses/{id}` until settled before
recording success. The mutation approval, source hash, before/after payloads,
response, and resolved job ID belong in the audit trail.

`POST /institution-logins/refresh` is the explicit bank-refresh trigger. Its
body contains `loginRefreshCredentials` (the in-band MFA channel; it was empty
when no institution challenged) and `investmentAggregationType`; the response
contains `id`, `status`, and `pollingReference`. Poll the referenced job. A
browser is not needed for this bank-refresh call; browser automation is only
for unattended access-token refresh.

## Connection health

`GET /institution-logins` returns per-connection `aggregators[]` fields that
are purpose-built for monitoring:

```text
aggStatus, aggStatusCode, aggStatusDetail,
lastStatusUpdatedAt, nextRefreshAttemptAt,
nextManualRefreshEligibleAt, lastRefreshAttemptedAt,
lastRefreshSuccessfulAt
```

Use `lastRefreshSuccessfulAt` for sync freshness, `aggStatusCode` for care
codes (for example `FDP-108`, `324`, `FDP-192`), and
`nextManualRefreshEligibleAt` as the provider's manual-refresh rate limit.
`/institutions/fi-issues` returned an empty array in the capture and is a
supplement, not a replacement. The first live probe showed WageWorks
`UNSUPPORTED_*`/`FDP-192` with last success 2025-05-31, while Alliant was `OK`
despite low transaction activity; HSBC had recovered from code 324, and Apple
Wallet lagged by about one day. Monitor transitions and per-connection cadence,
not transaction dates alone.

## Superseded hypotheses

These are retained as history to prevent their recurrence; they are not
current facts:

- **Token TTL ≥24 hours:** retracted. It confused bank-sync
  `lastRefreshSuccessfulAt` with token age; measured access-token lifetime is
  one hour.
- **Offset/page pagination:** retracted. Response metadata names are output,
  not accepted input parameters; the server cursor is `nextLink`/`after`.
- **`cpData` is the GET raw descriptor:** retracted. It was seen in a PUT body;
  all tested GET routes omitted it. The raw descriptor finding comes from GET
  `payee` versus CSV cross-source matching.
- **GET supplies `split`, `isSubscription`, `cpData.txnOn`, or
  `inferredCoa`:** retracted; these were PUT-body/client-cache observations.
- **`dateStart`/`dateEnd` bound the API:** retracted; use `dateOnAfter`.
- **Transaction-date staleness proves a broken connection:** retracted. Use
  `lastRefreshSuccessfulAt` and aggregator status fields.
- **A separate challenge endpoint is needed for institution MFA:** retracted;
  MFA is supplied in `loginRefreshCredentials` on the refresh POST.
