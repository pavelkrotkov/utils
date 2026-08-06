# Simplifi private API — M0B recon notes

Captured 2026-08-04 against the live web app at `simplifi.quicken.com`.

**Method.** No HAR was written. `fetch` and `XMLHttpRequest` were temporarily
instrumented in the page to record *structure only* — endpoint paths, query
parameter names, and JSON key names. Values were replaced by their `typeof`
before anything left the page, so no balances, payees, account numbers or tokens
were captured. The instrumentation was removed afterwards.

Host: **`services.quicken.com`** (48 calls observed on a single page load).

---

## 1. Authentication

Bearer token in an `Authorization` header. **Not** cookie-based — a `fetch` with
`credentials: 'include'` and no header returns **401**, confirmed directly.

This matches the `rijn/simplifiapi` model (plan §3.2): OAuth against
`/oauth/authorize` → `/oauth/token`, then send the bearer on every call.
`GET /userprofiles/me` is the cheap token-validity probe.

### Token TTL — RETRACTED, then measured properly

An earlier revision of this document claimed "TTL at least 24 hours, CONFIRMED".
**That was wrong and the reasoning was bad.** It inferred token age from
`lastRefreshSuccessfulAt = 2026-08-05`, which is when the *bank connection* last
synced — a completely unrelated subject. The token had in fact been re-extracted
minutes earlier, so there was no TTL evidence at all.

The correct method needs no waiting: **the token is a JWT, so `exp` and `iat`
are readable locally.**

```bash
python3 -c "
import base64, json, time
p = TOKEN.split('.')[1]
d = json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))
print('lifetime', (d['exp'] - d['iat']) / 3600, 'h')
"
```

`decode_jwt_claims()` in `api_source.py` does this in-process. The signature is
deliberately *not* verified — we are reading our own token's metadata, not
trusting it for authorisation.

**Consequences, which depend on the measured lifetime:**

**MEASURED 2026-08-05: exactly 1.00 hour.**

```
issued    2026-08-04 23:07:21
expires   2026-08-05 00:07:21
lifetime  1.00 h
claims    aud auth_time azp cid client_id email exp grant_type iat iss jti
          origin rev_sig scope sub user_id user_name zid
```

Pavel predicted this; the earlier "≥24h" claim was 24× wrong.

**A stored access token cannot drive a scheduled job.** One hour of validity
means any unattended run needs the ability to mint a fresh token, which means
either a refresh token or the full `/oauth/*` flow with a **stored Simplifi
password** — much the largest credential this project would hold.

The claim set includes `grant_type`, `scope`, `azp` and `rev_sig`, which are
standard OAuth2 markers and suggest a refresh token probably exists. Finding out
is now the highest-value unknown: a refresh token would keep the password out of
Hermes entirely.

### RESOLVED 2026-08-05 — refresh token found, architecture decided

A browser capture of a live session settled it. `localStorage.authSession` holds
a **3-year refresh token** (`refreshTokenExpired = 2029-08-03`) alongside the
1-hour access token. The token endpoint is
`POST https://services.quicken.com/oauth/token`, confirmed real.

But `acme_web` cannot call it: the app authenticates with a **Quicken client
secret** baked into its JS, and the endpoint returns `InvalidClientException`
without it. Extracting that secret is both blocked by the tooling and the wrong
design.

**Decision:** Hermes will not call `/oauth/token`. It will run the logged-in
browser session (Playwright `storage_state`, holding Pavel's refresh token) and
let Simplifi's own JavaScript mint the hourly access token, which we read from
`localStorage`. No password, no client secret, no impersonation. Full spec and
build plan in `docs/auth.md`.

**Behaviour now built in:** `check_expiry()` refuses to start a run on an expired
token, with time-since-expiry in the message, and warns when under 6 hours
remain — so a scheduled run tells you to rotate *before* the run that fails.
Opaque non-JWT tokens degrade gracefully to "cannot check".

Still not verified: the `/oauth/*` request shape and whether a refresh token is
issued. If the lifetime is short, both become required rather than optional.

---

## 2. Response envelope

Every collection endpoint returns the same wrapper:

```json
{
  "resources": [ ... ],
  "metaData": {
    "pageSize": 0, "limit": 0, "asOf": "", "currentPage": 0,
    "offset": 0, "totalPages": 0, "totalSize": 0
  }
}
```

### RESOLVED 2026-08-05 — it is a keyset cursor, not offset paging

Measured, not assumed:

```
limit=500               -> 500 rows, metaData.nextLink present
limit=5000              -> 5000 rows          (limit itself is honoured well past 500)
limit=500&offset=500    -> 500 rows, SAME first id as page 1   ← ignored
limit=500&page=2        -> 500 rows, SAME first id as page 1   ← ignored
limit=500&currentPage=2 -> 500 rows, SAME first id as page 1   ← ignored
```

**`offset`, `page` and `currentPage` do not exist as parameters.** The server
accepts them silently and re-serves page one — the worst failure mode available,
because a client that trusts them gets plausible data forever.

The real cursor was in the response from the very first call:

```
metaData.nextLink = /transactions?limit=500&after=dateOn;2026-06-08;550360076270735104
```

`after` is a composite keyset cursor: sort field, its value, and the row id as a
tiebreak. `paginate()` now follows `nextLink` verbatim rather than constructing
its own query, which means the walk keeps working if Quicken changes the scheme.

Keyset beats offset here on correctness, not just style: a transaction feed
mutates while you walk it, and offset paging silently skips or duplicates rows
when it does.

**The methodological error, kept because it is the reusable part.** `metaData`
contains `offset`, `currentPage` and `totalPages`, and I reasoned from their
presence that matching *query parameters* existed. They are the envelope
describing its own position — output, not input. A field in a response is not
evidence that a parameter of that name is accepted. The same envelope also
contained the actual answer, unread.

Note also `totalPages: 1` and `totalSize: 500` on a dataset with far more than
500 rows: both describe the current page. Never use them as a stop condition.

## 5b. `coa.type = BALANCE_ADJUSTMENT` — closed, no category exists

Cross-source matching on `(date, amount)` gave 1,565 unambiguous pairs, of which
**exactly 68 disagreed on kind** — all in one bucket:

| CSV says | API says | n |
|---|---|---|
| `Transfer` | `Balance Adjustment` | 38 |
| `Credit Card Payment` | `Balance Adjustment` | 30 |

The API carries no category for these. `coa.id` is the sentinel `"0"` or `"2"` —
not an identifier. It resolves against categories, accounts, `knownCategoryId`
and `knownCategoryIds`: none of the four.

```
UNCATEGORIZED       coa={'type': 'UNCATEGORIZED',      'id': '0'}  txn.type='INVESTMENT'
BALANCE_ADJUSTMENT  coa={'type': 'BALANCE_ADJUSTMENT', 'id': '2'}  txn.type='INVESTMENT'
BALANCE_ADJUSTMENT  coa={'type': 'BALANCE_ADJUSTMENT', 'id': '0'}  txn.type='CASH_FLOW'
```

Simplifi's UI derives `Credit Card Payment` client-side from the paired
transaction. We could reconstruct that by matching counterparties.

**We are not going to, and the reason is measured.** All 68 rows come out with
identical `poisons_statistics` and `is_uncategorized` under either label. The
name differs; no downstream decision does. Checking that before building the
matcher cost one query and saved the feature.

Sub-cases if it ever matters: `id="2"` + `type=INVESTMENT` are genuine
cost-basis adjustments; `id="0"` + `type=CASH_FLOW` is the transfer case.

---

## 3. Endpoints observed

| Endpoint | Query parameters | Notes |
|---|---|---|
| `GET /userprofiles/me` | — | token validity probe |
| `GET /datasets` | `limit,modifiedAfter` | dataset id for the `Qcs-Dataset-Id` header |
| `GET /datasets/{id}/entitlements` | `limit` | |
| `GET /accounts` | `limit,modifiedAfter` | |
| `GET /transactions` | **`dateOnAfter`**, `limit`, `modifiedAfter` | see §4 |
| `GET /transactions/earliest-date-on` | — | lower bound of history |
| `PUT /transactions/{id}` | — | **the write path**, see §6 |
| `GET /categories` | `limit,modifiedAfter` | |
| `GET /tags` | `limit,modifiedAfter` | |
| `GET /institution-logins` | `limit,modifiedAfter` | the connections |
| `GET /institutions/fi-issues` | `institutionIds` | **connection health / care codes** |
| `GET /job-statuses`, `GET /job-statuses/{id}` | — | async job polling |
| `GET /scheduled-transactions` | `includeCascadeDeleted,limit,modifiedAfter` | |
| `GET /spending-watchlist` | `limit,modifiedAfter` | |
| `GET /free-to-spend` | `limit,modifiedAfter` | |
| `GET /goals`, `/alert/alerts`, `/filters`, `/documents` | `limit,modifiedAfter` | |
| `GET /v2/investments/holdings\|securities\|quotes` | varies | investments |
| `GET /reports/report-configuration` | `limit` | |
| `GET /preferences`, `/v2/preferences`, `/preferences/user` | | |

Also present but not relevant here: `/aikya-*` (bill pay), `/sentinel-kyc/*`,
`/creditscore/*`, `/gamification/*`, `/subscriptions`, `/businesses`,
`/paymentmethods`, `/jwt-sso/intercom_user_jwt`.

**Corrects the plan.** §14 guessed the date parameter was `dateStart`/`dateEnd`
from the app's own URL. Those are *client-side view state*. The API parameter is
**`dateOnAfter`**, and there is no matching `dateBefore` in what was observed.

---

## 4. The app syncs fully, then filters locally

Changing the visible date range does **not** issue a new `/transactions` call.
The app pulls everything once, caches it, and filters in the browser; subsequent
calls carry `modifiedAfter` and come back with `"resources": []`.

Two consequences for us:

- `modifiedAfter` is a real **incremental sync** cursor. This is a much better
  fit for the append-only `transaction_version` design (plan §5.3) than
  re-fetching a window every run.
- To bound a fetch by date, use `dateOnAfter` explicitly. Do not assume the
  app's URL parameters map to the API.

---

## 5. Transaction schema

### ⚠ CORRECTION 2026-08-05 — `cpData` is NOT in the GET response

The schema below was captured from the **`PUT` request body**. A live
`GET /transactions` returns a much thinner document:

```
accountId, amount, clientId, coa, createdAt, dbVersion, id, isBill,
matchState, modifiedAt, payee, postedOn, source, stDueOn, stModelId,
state, type, userModifiedAt
```

**No `cpData`. No `split`. No `isSubscription`.**

The app assembles the PUT body from its own client-side cache, so observing a
write told us what the app *holds*, not what the list endpoint *serves*. That is
a real methodological lesson: a request body is evidence about the client, not
the API.

**What this costs.** §5's headline claim — "the raw statement descriptor IS
available" — is unproven for bulk reads. Everything that depended on
`cpData.payee` (proper normalisation, descriptor-level rule hygiene, the
`inferredCoa` miscategorisation signal, `txnOn` vs `postedOn`) is on hold until
we find an endpoint or parameter that returns it. **Settled 2026-08-05 — every route tested returns no `cpData`:**

```
cpData in list response      : False
GET /transactions/{id} keys  : (identical 18 fields — no cpData)
?includeCpData=true          : False
?expand=true                 : False
?includeDetails=true         : False
```

**But the conclusion I drew from that was wrong, and the reversal matters.**

I wrote that the raw descriptor was therefore unavailable and that rule-hygiene
work had hit a ceiling. Then I matched 1,565 transactions across the CSV and API
on `(date, amount)` and found:

| | CSV `payee` | API `payee` |
|---|---|---|
| | Costco | `COSTCO WHSE #1166        NORTH PLAINFINJ` |
| | Amazon | `AMAZON MKTPL*FZ4AM2QE3` |
| | Geico | `DEBIT CARD PURCHASE GEICO *AUTO 800-841-3000 DC 073026 AUT` |
| | Carrington Mtgmtg Payment | `DIRECT DEBIT CARRINGTON MTGMTG PYMT (Cash)` |

**904 of 1,565 matched rows — 58% — differ, and the API side is the raw bank
descriptor.** The CSV export is what applies Simplifi's renaming; the API serves
the underlying string.

The normalizer corroborates it without being asked to: 413 strip-rules fire on
the API feed against 104 on the CSV, because on the API feed there is genuinely
something to strip (`strip_trailing_store_number` 171 vs 14,
`strip_state_suffix` 72 vs 5).

So `cpData` was never the prize. It is a field that would have duplicated
`payee`, and I spent five probes hunting it because the PUT body showed both and
I assumed the *longer-named* one held the real value. The descriptor was in the
first response of every run.

**This is the third error of the same shape in this project** — reasoning about
the API from an artifact (a PUT body, a response envelope, a field name) instead
of from the data actually returned. The check that works is cheap and I should
reach for it first: fetch both sources and diff them row by row.

**What survives.** `coa.type` is authoritative and better than anything inferred
— observed values `CATEGORY` (341), `ACCOUNT` (16, i.e. transfers),
`UNCATEGORIZED` (35), `BALANCE_ADJUSTMENT` (8). `state` is real:
`PENDING` 156 / `CLEARED` 244 of 400 sampled — the CSV exposes no pending flag
at all, so this is genuinely new. `isBill` is populated (126 of 400);
`isSubscription` is absent from GET entirely.

Category resources carry `name` + `parentId` (no `fullName`), so the path has to
be built by walking parents — which the mapper now does.

### The PUT-body schema, retained for the write path

```jsonc
{
  "type": "string",
  "id": "string",
  "accountId": "string",
  "postedOn": "string",
  "payee": "string",          // display name — what the CSV exports
  "memo": "string",
  "coa": { "type": "string", "id": "string" },   // category ref ("chart of accounts")
  "amount": 0,
  "split": {},
  "state": "string",
  "matchState": "string",
  "isReviewed": false,
  "cpData": {                 // the provider's original record
    "id": "string",
    "txnType": "string",
    "postedOn": "string",
    "txnOn": "string",        // TRANSACTION date, distinct from posted date
    "payee": "string",        // *** RAW STATEMENT DESCRIPTOR ***
    "memo": "string",
    "amount": 0,
    "inferredPayee": "string",   // Simplifi's own guess at the merchant
    "inferredCoa": { "type": "string", "id": "string" },  // its own category guess
    "cpCategoryId": "string"     // the data provider's category
  },
  "source": "string",
  "isExcludedFromF2S": false,
  "isExcludedFromReports": false,
  "isDeleted": false,
  "isBill": false,
  "isSubscription": false,
  "isBillable": false,
  "check": { "number": "string", "memo": "string" }
}
```

**This closes the biggest gap in the project.** Plan §3.1 listed six things the
CSV omits. The API supplies five of them outright:

| CSV omission | API field |
|---|---|
| transaction ID | `id` — real and stable, replaces the synthetic content hash |
| raw statement descriptor | **`cpData.payee`** — enables §5.2 properly |
| split marker | `split` |
| pending vs posted | `state`, `matchState` |
| account ID | `accountId` |
| currency | still absent — no currency field observed |

And it adds four things the plan never anticipated:

- **`cpData.txnOn` vs `postedOn`.** §6.5 dropped the `off_hours` signal because
  posted timestamps are settlement artefacts. The API carries the *transaction*
  date separately, so date-based reasoning becomes defensible again.
- **`inferredPayee` / `inferredCoa`.** Simplifi's own guesses, stored alongside
  the user's chosen values. Disagreement between `coa` and `inferredCoa` is a
  free, deterministic miscategorization detector — exactly the thing that would
  have surfaced `Espn → Education:Tuition` without any model.
- **`isSubscription` / `isBill`.** The CSV's `Recurring` column was set on 8 of
  1,641 rows and was useless for `subscription_creep`. These flags may be better
  populated; worth checking before inferring cadence from date spacing.
- **`isExcludedFromF2S` and `isExcludedFromReports` are two separate flags.**
  The CSV collapses them into one `Exclusion` column, losing information.

---

## 6. Writes are asynchronous jobs

`PUT /transactions/{id}` returns:

```json
{ "id": "string", "status": "string", "explanation": "string" }
```

That is a **job envelope**, not the updated resource — which is why
`/job-statuses` and `/job-statuses/{id}` exist. A write must be followed by
polling until the job settles.

Two consequences for the `apply` design (plan §6.11):

1. **It is a full-document PUT, not a PATCH.** Applying a category change means
   read → modify one field → write the whole document back. Any field we fail to
   echo faithfully risks being cleared. The source-hash validation in §6.11
   becomes essential rather than merely prudent, and `apply` must re-fetch each
   transaction immediately before writing.
2. **Success is not synchronous.** `apply` cannot report a result until the job
   resolves. Idempotency and the audit log must key on the job outcome.

---

## 7. Connection health has a dedicated endpoint

`GET /institutions/fi-issues?institutionIds=...` is presumably where provider
care codes surface. Combined with `/institution-logins`, this is a far better
basis for the `account_stale` monitor (plan §3.7) than inferring staleness from
the newest transaction date, because it reports the *connection's* state rather
than the absence of activity.

This also avoids false positives from accounts that see little use: a quiet
account can still have a healthy provider connection.

---

## 8. Refresh — RESOLVED

Captured by instrumenting, then clicking "Refresh All". Sixteen non-GET calls
fired; the one that matters is:

```
POST /institution-logins/refresh
  body:     { "loginRefreshCredentials": {...}, "investmentAggregationType": "..." }
  response: { "id": 0, "status": "...", "pollingReference": "..." }
```

**`loginRefreshCredentials` is the MFA channel.** It was an empty object on this
refresh because no institution challenged. This is where a bank's MFA answer
would be supplied — which means institution MFA is handled *in-band on the
refresh call*, not through a separate challenge endpoint.

`pollingReference` is then polled to completion. Supporting job machinery:

```
POST   /job-statuses            body { "type": "...", "datasetId": "..." }  -> { id, status }
DELETE /job-statuses/{id}
POST   /transactions/earliest-date-on   body { "accountIds": [...] }
POST   /goals/analysis-async
POST   /v2/investments/quotes   body { "symbols": [...] }
POST   /logos/tickers           body { "tickers": [...] }
```

So the answer to §3.3's open question is: **an explicit trigger exists and is a
single POST.** The script does not need to simulate a login, and does not need a
browser. `POST /institution-logins/refresh` then poll.

---

## 8a. Connection health is fully exposed — this replaces `account_stale`

`GET /institution-logins` returns, per connection:

```jsonc
{
  "id": "...", "institutionId": "...", "cpInstitutionId": "...",
  "name": "...", "channel": "...", "isConnected": true,
  "includeInvestmentAccounts": true,
  "userModifiedAt": "...", "createdAt": "...", "modifiedAt": "...", "dbVersion": 0,
  "aggregators": [{
    "cpId": "...", "cpChannel": "...",
    "aggStatus": "...",                    // connection state
    "aggStatusCode": "...",                // provider care code
    "aggStatusDetail": "...",              // the human-readable message
    "lastStatusUpdatedAt": "...",
    "nextRefreshAttemptAt": "...",         // when Simplifi will next auto-try
    "nextManualRefreshEligibleAt": "...",  // *** RATE LIMIT for manual refresh ***
    "lastRefreshAttemptedAt": "...",
    "lastRefreshSuccessfulAt": "..."       // *** TRUE connection freshness ***
  }]
}
```

This is a purpose-built connection-health feed and it is strictly better than
what the plan designed:

- **`lastRefreshSuccessfulAt` is the real staleness metric.** §6.5 inferred
  staleness from the newest transaction date, which can falsely flag an account
  that is simply little-used but perfectly connected. This field distinguishes
  "no spending" from "no sync".
- **`aggStatusCode` gives the care code directly.** A provider failure with a
  care code is detectable at the time it occurs rather than when missing
  transactions surface later. That single field justifies the monitor.
- **`nextManualRefreshEligibleAt` is the aggregator's own rate limit.** The
  script can respect it rather than guessing, which addresses the §4.2 worry
  about refresh storms marking a connection bad.

`GET /institutions/fi-issues?institutionIds=...` returned an empty array on this
capture — presumably it only populates during a known platform-wide incident, so
it is a supplement to `aggStatus`, not a replacement.

---

## 9. What changes in the plan

| Plan section | Change |
|---|---|
| §3.1 | CSV demoted further — the API supplies 5 of 6 missing fields, plus 4 unanticipated ones |
| §5.2 | raw/normalized/canonical trace is now buildable: `cpData.payee` → `payee` |
| §5.3 | use `id`, drop the synthetic hash; `modifiedAfter` becomes the sync cursor |
| §6.5 | `off_hours` reconsiderable via `cpData.txnOn`; `account_stale` replaced by `lastRefreshSuccessfulAt` + `aggStatusCode` |
| §3.3 | RESOLVED — `POST /institution-logins/refresh` is an explicit trigger; no browser, no simulated login |
| §4.2 | institution MFA is in-band via `loginRefreshCredentials`, not a separate challenge endpoint; respect `nextManualRefreshEligibleAt` |
| §6.6 | rule hygiene can finally read the real descriptor — the `apple` misdiagnosis came from not having it |
| §6.9 | `inferredCoa` vs `coa` gives a free miscategorization signal, no model needed |
| §6.11 | full-document PUT + async job: re-fetch before write, poll for outcome |
| §14 | `dateStart`/`dateEnd` guess was wrong; the parameter is `dateOnAfter` |


---

## 10. Example `probe` output

```
INFO authenticated: profile keys ['createdAt','firstName','id','lastName','modifiedAt','primaryAddress']
INFO dataset <dataset-id-prefix>... · 3 accounts · 2 connections
```

The following synthetic connection table illustrates the shape of the health
data without recording any user's institutions, identifiers, or live status:

| Institution | aggStatus | aggStatusCode | lastRefreshSuccessfulAt |
|---|---|---|---|
| Example Bank | OK | — | 2026-08-05 |
| Example Wallet | UNSUPPORTED | FDP-000 | 2026-08-01 |

Three things this confirms:

1. **The monitor reports provider-owned health.** A non-OK status, care code,
   or stale refresh timestamp is visible without inferring failure from spending
   activity.
2. **Care codes are transient.** A monitor should track transitions, not just
   the current status.
3. **Refresh cadence varies by connection.** `expected_staleness_days` should
   therefore be connection-specific rather than one global threshold.
