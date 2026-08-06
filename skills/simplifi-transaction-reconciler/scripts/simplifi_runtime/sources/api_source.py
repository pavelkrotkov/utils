"""Simplifi private API read path.

Everything here was captured from the live web app on 2026-08-04 by recording
request/response *structure* — never values. See the notes for method.

Why this source exists at all: the CSV export omits the stable transaction ID,
raw statement descriptor, settlement/projection state and account ID. The API
supplies those read-side fields and Simplifi's own `inferredPayee`/`inferredCoa`
guesses. The private API does not expose every client-side write field on GET,
so mappings preserve those capability limits.

REQUIRED HEADERS — all seven. Sending only `Authorization` gets past auth and
then returns **500**, because the server does not handle the missing
`app-client-id` / `qcs-dataset-id` cleanly. That 500 cost an hour; it is
documented here so it costs nobody else one.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation

from ..money import Money
from ..normalize import normalize
from ..semantics import classify

BASE = "https://services.quicken.com"
TIMEOUT = 60
PAGE_LIMIT = 500
MAX_PAGES = 200  # circuit breaker against a pagination bug becoming an infinite loop

#: Version-pinned client identifiers, read from the web app. These are not
#: secrets, but they WILL drift when Quicken ships a release — keep them here
#: rather than inline, and update from a fresh capture if calls start failing.
APP_CLIENT_ID = os.environ.get("SIMPLIFI_APP_CLIENT_ID", "acme_web")
APP_RELEASE = os.environ.get("SIMPLIFI_APP_RELEASE", "6.30.0")
APP_BUILD = os.environ.get("SIMPLIFI_APP_BUILD", "68148")


#: Warn when a token has less than this long to live, so a nightly run tells you
#: to rotate it *before* the run that fails.
EXPIRY_WARN_SECONDS = 6 * 3600


class ApiError(RuntimeError):
    pass


class AuthError(ApiError):
    """Token rejected. Distinct because it needs a human, not a retry."""


def decode_jwt_claims(token: str) -> dict:
    """Read a JWT payload WITHOUT verifying the signature.

    We are inspecting our own token's metadata (`exp`, `iat`), not trusting it
    for authorisation, so signature verification is irrelevant here. Returns {}
    for opaque (non-JWT) tokens rather than raising.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return {}


def _headers(token: str, dataset_id: str | None = None) -> dict[str, str]:
    h = {
        "Accept": "application/json, text/plain, */*",
        "app-client-id": APP_CLIENT_ID,
        "app-release": APP_RELEASE,
        "app-build": APP_BUILD,
        "client-tid": str(uuid.uuid4()),  # per-request trace id
        "Authorization": f"Bearer {token}",
    }
    if dataset_id:
        h["qcs-dataset-id"] = dataset_id
    return h


class SimplifiApiClient:
    def __init__(self, token: str | None = None, dataset_id: str | None = None):
        self.token = (token or os.environ.get("SIMPLIFI_ACCESS_TOKEN", "")).strip()
        if self.token.lower().startswith("bearer "):
            # Copying the whole header value is the obvious mistake; absorb it
            # rather than emitting "Bearer Bearer eyJ..." and a baffling error.
            self.token = self.token[7:].strip()
        if not self.token:
            raise AuthError("SIMPLIFI_ACCESS_TOKEN is not set")
        self._dataset_id = dataset_id
        self.claims = decode_jwt_claims(self.token)

    # --- token lifetime -----------------------------------------------------

    @property
    def expires_at(self) -> float | None:
        exp = self.claims.get("exp")
        return float(exp) if exp else None

    @property
    def seconds_remaining(self) -> float | None:
        exp = self.expires_at
        return None if exp is None else exp - time.time()

    def check_expiry(self) -> str | None:
        """Return a human warning if the token is expired or expiring soon.

        Reading `exp` locally is strictly better than discovering expiry through
        a 401 halfway through a run: we can refuse to start, or warn while the
        run still succeeds.
        """
        remaining = self.seconds_remaining
        if remaining is None:
            return None  # opaque token, not a JWT — nothing to check
        if remaining <= 0:
            raise AuthError(
                f"token expired {abs(remaining) / 3600:.1f}h ago "
                "(lifetime is 1h). Refresh it through the deployment's auth flow."
            )
        if remaining < EXPIRY_WARN_SECONDS:
            return f"token expires in {remaining / 3600:.1f}h — rotate it soon"
        return None

    # --- plumbing -----------------------------------------------------------

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{BASE}{path}" + (f"?{query}" if query else "")
        needs_dataset = path not in {"/datasets", "/userprofiles/me"}
        req = urllib.request.Request(
            url, headers=_headers(self.token, self.dataset_id if needs_dataset else None)
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            if exc.code in (401, 403):
                raise AuthError(
                    f"{path} returned {exc.code} — token expired or revoked. "
                    "Re-extract it from the browser and re-encrypt."
                ) from exc
            if exc.code == 500:
                raise ApiError(
                    f"{path} returned 500. Usually a missing required header, not a "
                    f"server fault — all seven are required. Body: {body}"
                ) from exc
            raise ApiError(f"{path} returned {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            # Network-level: DNS, TLS, no route, blocked egress. Distinct from an
            # HTTP error and worth reporting cleanly rather than tracebacking —
            # on Hermes this is the difference between a readable degraded run
            # and a wall of stack.
            raise ApiError(f"{path} unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(f"{path} timed out after {TIMEOUT}s") from exc

    def paginate(self, path: str, **params) -> list[dict]:
        """Follow the server-supplied `metaData.nextLink` cursor to the end.

        HOW THIS WAS GOT WRONG, because the mistake is instructive. The first
        version sent `offset`, reasoning from the presence of `offset`,
        `currentPage` and `totalPages` *in the response envelope*. It fetched
        exactly 500 rows twice and looked like a cap. A probe settled it::

            limit=500&offset=500    -> same first id as page 1
            limit=500&page=2        -> same first id as page 1
            limit=500&currentPage=2 -> same first id as page 1

        All three are ignored. **A field in a response envelope is not evidence
        that a query parameter of that name exists.** The envelope was echoing
        its own position, not accepting instructions.

        The real cursor was in the response the whole time::

            nextLink: /transactions?limit=500&after=dateOn;2026-06-08;5503600...

        `after` is a composite keyset cursor — sort field, its value, and the id
        as a tiebreak. That is strictly better than offset paging: it cannot
        skip or double-serve a row when the underlying set changes mid-walk,
        which for a live transaction feed it will.

        So: follow `nextLink` verbatim. It is the server's own opinion about
        where the next page starts, and it stays correct if the scheme changes.
        """
        out: list[dict] = []
        seen_ids: set[str] = set()
        next_path: str | None = None
        pages = 0

        while True:
            if next_path is None:
                page = self.get(path, limit=PAGE_LIMIT, **params)
            else:
                # Pass nextLink through with its query string UNTOUCHED. Parsing
                # and re-encoding would percent-escape the `;` separators inside
                # `after=dateOn;2026-06-08;5503...`, and there is no reason to
                # bet on the server round-tripping that. `get()` appends nothing
                # when no params are supplied, so the URL goes out verbatim.
                page = self.get(next_path)

            resources = page.get("resources", [])
            next_path = (page.get("metaData") or {}).get("nextLink")
            if not resources:
                if next_path:
                    raise ApiError(f"{path}: pagination cursor advanced to an empty page")
                break

            # Belt and braces: if a cursor ever silently re-serves page one, an
            # all-duplicate page ends the walk instead of looping forever.
            fresh = [r for r in resources if r.get("id") not in seen_ids]
            if not fresh:
                raise ApiError(f"{path}: pagination cursor did not advance")
            seen_ids.update(r.get("id") for r in fresh)
            out.extend(fresh)

            if not next_path:
                break

            pages += 1
            if pages > MAX_PAGES:
                raise ApiError(f"{path}: exceeded {MAX_PAGES} pages; refusing to loop further")
        return out

    # --- identity -----------------------------------------------------------

    @property
    def dataset_id(self) -> str:
        if self._dataset_id is None:
            datasets = self.get("/datasets", limit=5).get("resources", [])
            if not datasets:
                raise ApiError("no datasets returned; token may be for an empty account")
            self._dataset_id = datasets[0]["id"]
        return self._dataset_id

    def verify(self) -> dict:
        """Cheap token probe. Raises AuthError if the token is dead."""
        self.check_expiry()  # fail before the network call if already expired
        return self.get("/userprofiles/me")

    # --- reads --------------------------------------------------------------

    def accounts(self) -> list[dict]:
        return self.paginate("/accounts")

    def categories(self) -> list[dict]:
        return self.paginate("/categories")

    def institution_logins(self) -> list[dict]:
        return self.paginate("/institution-logins")

    def transactions(
        self, date_on_after: str | None = None, modified_after: str | None = None
    ) -> list[dict]:
        return self.paginate(
            "/transactions", dateOnAfter=date_on_after, modifiedAfter=modified_after
        )


class SimplifiApiSource:
    """Same shape as SimplifiCsvSource, but with the fields the CSV lacks."""

    name = "api"

    def __init__(
        self,
        client: SimplifiApiClient | None = None,
        date_on_after: str | None = None,
        modified_after: str | None = None,
    ):
        self.client = client or SimplifiApiClient()
        self.date_on_after = date_on_after
        self.modified_after = modified_after

    def fetch(self) -> list[dict]:
        accounts = {a["id"]: a for a in self.client.accounts()}
        categories = {c["id"]: c for c in self.client.categories()}
        account_names = {a.get("name", "") for a in accounts.values()} - {""}

        raw = self.client.transactions(self.date_on_after, self.modified_after)
        return [
            self._tombstone(t)
            if t.get("isDeleted")
            else self._to_record(t, accounts, categories, account_names)
            for t in raw
        ]

    # --- mapping ------------------------------------------------------------

    @staticmethod
    def _tombstone(t: dict) -> dict:
        transaction_id = t.get("id")
        if not transaction_id:
            raise ApiError("deleted transaction record is missing id")
        return {
            "transaction_id": transaction_id,
            "is_deleted": True,
            "modified_at": t.get("modifiedAt") or None,
        }

    @staticmethod
    def _validate_transaction(t: dict) -> None:
        missing = [
            key
            for key in ("id", "amount", "postedOn")
            if key not in t or t[key] is None or (isinstance(t[key], str) and not t[key].strip())
        ]
        if missing:
            raise ApiError(
                f"transaction {t.get('id', '?')} is missing required field(s): "
                + ", ".join(missing)
            )

    @staticmethod
    def _category_name(coa: dict | None, categories: dict) -> str:
        """Resolve a coa reference to a display path like "Parent:Child".

        The category resource shape was never captured during recon, so this
        tries several plausible keys and falls back to walking `parentId`.
        Getting this wrong is not cosmetic: transfer detection matches the
        category against account names, so a bad mapping silently drops every
        transfer — which is exactly what the first API run did (181 -> 0).
        """
        if not coa or not coa.get("id"):
            return ""
        cat = categories.get(coa["id"])
        if not cat:
            # coa.id may reference an ACCOUNT (Simplifi encodes transfers that
            # way) rather than a category. The caller handles that.
            return ""
        for key in ("fullName", "fullPath", "displayName", "path"):
            if cat.get(key):
                return str(cat[key])

        # Build the path ourselves.
        parts, node, guard = [], cat, 0
        while node and guard < 8:
            name = node.get("name") or node.get("label") or ""
            if name:
                parts.append(str(name))
            parent_id = node.get("parentId") or node.get("parentCoaId")
            node = categories.get(parent_id) if parent_id else None
            guard += 1
        return ":".join(reversed(parts))

    def _to_record(
        self, t: dict, accounts: dict, categories: dict, account_names: set[str]
    ) -> dict:
        self._validate_transaction(t)
        cp = t.get("cpData") or {}
        account = accounts.get(t.get("accountId"), {})
        account_name = account.get("name", "") or t.get("accountId", "")

        # `cpData` does not exist on any read endpoint (probed five ways), so
        # this always falls through to `t["payee"]` — and that turns out to be
        # the thing we wanted anyway. Matching 1,565 rows across both sources:
        # 904 of them (58%) have an API `payee` that is the RAW BANK DESCRIPTOR
        # while the CSV carries Simplifi's renamed version.
        #
        #   CSV: Costco          API: COSTCO WHSE #1166        NORTH PLAINFINJ
        #   CSV: Amazon          API: AMAZON MKTPL*FZ4AM2QE3
        #   CSV: Geico           API: DEBIT CARD PURCHASE GEICO *AUTO 800-841...
        #
        # The normalizer proves it independently: 413 strip-rules fire on the
        # API feed vs 104 on the CSV, because there is actually something to
        # strip. Chasing `cpData` was chasing a field that duplicates `payee`.
        payee_raw = (cp.get("payee") or t.get("payee") or "").strip()
        payee_display_api = (t.get("payee") or "").strip()

        try:
            amount = Decimal(str(t.get("amount", 0)))
            scaled = amount * 100
            if scaled != scaled.to_integral_value():
                raise ValueError("amount has sub-cent precision")
            money = Money(int(scaled), "USD")
        except (InvalidOperation, ValueError) as exc:
            raise ApiError(f"transaction {t.get('id', '?')} has invalid amount") from exc
        # `coa.type` states the kind outright — CATEGORY / ACCOUNT /
        # UNCATEGORIZED / BALANCE_ADJUSTMENT. Use it rather than inferring from
        # whether the id happens to resolve; guessing here is what made every
        # transfer vanish on the first API run.
        coa = t.get("coa") or {}
        coa_type = (coa.get("type") or "").upper()
        if coa_type == "ACCOUNT":
            # Simplifi encodes a transfer by pointing coa at the destination
            # account. The CSV surfaces this as the account name in Category.
            category = accounts.get(coa.get("id"), {}).get("name", "") or "Transfer"
        elif coa_type == "BALANCE_ADJUSTMENT":
            # The API carries NO category for these, and that is settled rather
            # than assumed: `coa.id` is the sentinel string "0" or "2", not an
            # id. It resolves against categories, accounts, `knownCategoryId`
            # and `knownCategoryIds` — none of them. There is nothing to look up.
            #
            # The CSV labels the same 68 rows `Transfer` (38) and
            # `Credit Card Payment` (30), so Simplifi's UI derives those
            # client-side from the paired transaction. We could reconstruct it
            # by matching counterparties; we don't, because it buys nothing:
            # all 68 rows come out with identical `poisons_statistics` and
            # `is_uncategorized` either way. The label differs, no downstream
            # decision does. Verified, not assumed.
            #
            # Two sub-cases, if this ever needs revisiting: id="2" with
            # type=INVESTMENT are real cost-basis adjustments; id="0" with
            # type=CASH_FLOW is the transfer/card-payment case.
            category = "Balance Adjustment"
        elif coa_type == "UNCATEGORIZED":
            category = ""
        else:
            category = self._category_name(coa, categories)
        inferred_category = self._category_name(cp.get("inferredCoa"), categories)
        is_uncategorized = not category or category.lower() == "uncategorized"

        desc = normalize(payee_raw)
        report_exclusion = t.get("isExcludedFromReports")
        sem = classify(
            category=category,
            payee_raw=payee_raw,
            amount_minor_units=money.minor_units,
            exclusion_flag=(None if report_exclusion is None else bool(report_exclusion)),
            account_names=account_names,
        )

        return {
            "transaction_id": t["id"],
            "modified_at": t.get("modifiedAt") or None,
            "posted_on": (t.get("postedOn") or "")[:10],
            # cpData.txnOn is the *transaction* date; postedOn is settlement.
            # Keeping both preserves the transaction and settlement dates for
            # date-based signals.
            "transacted_on": (cp.get("txnOn") or "")[:10] or None,
            "account_name": account_name,
            "account_id": t.get("accountId"),
            "amount_minor_units": money.minor_units,
            "currency": "USD",
            "currency_exponent": 2,
            "payee_raw": payee_raw,
            "payee_normalized": desc.normalized,
            "payee_canonical": desc.canonical,
            "payee_display": payee_display_api or desc.display,
            "norm_rules_applied": ",".join(desc.rules_applied),
            "original_currency": desc.original_currency,
            "original_amount": desc.original_amount,
            "is_foreign_charge": int(desc.original_currency is not None),
            "category": category,
            "inferred_category": inferred_category,
            "is_uncategorized": int(is_uncategorized),
            # 2 means unknown: GET /transactions does not expose this flag.
            "exclusion_flag": 2 if report_exclusion is None else int(bool(report_exclusion)),
            "excluded_from_f2s": int(bool(t.get("isExcludedFromF2S"))),
            "recurring_flag": int(bool(t.get("isSubscription") or t.get("isBill"))),
            # PENDING vs CLEARED. The CSV has no equivalent, and without it the
            # duplicate detector cannot tell a real double-charge from a pending
            # row sitting alongside its posted twin. 156 of 400 sampled rows are
            # PENDING, so this is the common case, not an edge case.
            # (These were already extracted here but never made it into the
            # store's column list, so they were silently discarded on write.
            # Named `txn_state` because `state` is too generic to grep for.)
            "txn_state": t.get("state") or None,
            "match_state": t.get("matchState") or None,
            # The discriminator between a real pending transaction and a
            # PROJECTED one. Both carry state=PENDING; only projections carry a
            # scheduled-model id. Reading a projection as a charge is how a
            # confusing a cancelled recurring item with active billing.
            "scheduled_model_id": t.get("stModelId") or None,
            "scheduled_due_on": t.get("stDueOn") or None,
            "is_split": int(bool(t.get("split"))),
            "is_reviewed": int(bool(t.get("isReviewed"))),
            "kind": sem.kind.value,
            "poisons_statistics": int(sem.poisons_statistics),
            "semantics_reasons": "; ".join(sem.reasons),
        }


def schema_report(client: SimplifiApiClient, sample: int = 400) -> str:
    """Structural diagnostic: what the API actually returns, no values.

    Written because two mapping bugs (pagination stopping at 500, and every
    transfer vanishing) both came from guessing at shapes instead of looking.
    """
    from collections import Counter

    accounts = {a["id"]: a for a in client.accounts()}
    categories = {c["id"]: c for c in client.categories()}
    all_txns = client.transactions()
    txns = all_txns[:sample]

    lines = [
        f"transactions fetched : {len(all_txns)}",
        f"accounts             : {len(accounts)}",
        f"categories           : {len(categories)}",
        "",
        f"category keys        : {sorted(next(iter(categories.values())).keys()) if categories else '(none)'}",
        f"transaction keys     : {sorted(txns[0].keys()) if txns else '(none)'}",
    ]

    # WHERE DOES "Credit Card Payment" COME FROM? Cross-source matching found
    # 68 rows the CSV calls Transfer/Credit Card Payment that the API marks
    # coa.type=BALANCE_ADJUSTMENT with an id that resolves against neither the
    # category nor the account list. Category resources carry `knownCategoryId`
    # and `knownCategoryIds`, so the id may live in that space instead. Dump the
    # actual objects rather than guessing a fourth time.
    lines.append("")
    lines.append("--- unresolved coa: what are these ids? ---")
    known_single = {
        c.get("knownCategoryId"): c for c in categories.values() if c.get("knownCategoryId")
    }
    known_multi = {k: c for c in categories.values() for k in (c.get("knownCategoryIds") or [])}
    lines.append(
        f"categories with knownCategoryId: {len(known_single)}  "
        f"ids inside knownCategoryIds: {len(known_multi)}"
    )
    shown = Counter()
    for t in txns:
        coa = t.get("coa") or {}
        ctype = (coa.get("type") or "").upper()
        cid = coa.get("id")
        if ctype not in {"BALANCE_ADJUSTMENT", "UNCATEGORIZED"} or shown[ctype] >= 3:
            continue
        shown[ctype] += 1
        hit = (
            ("knownCategoryId -> " + known_single[cid].get("name", "?"))
            if cid in known_single
            else ("knownCategoryIds -> " + known_multi[cid].get("name", "?"))
            if cid in known_multi
            else "NOT in either known-category space"
        )
        lines.append(f"  {ctype:19} coa={coa}  txn.type={t.get('type')!r}  {hit}")

    resolution = Counter()
    for t in txns:
        coa = t.get("coa") or {}
        cid = coa.get("id")
        if not cid:
            resolution["no coa"] += 1
        elif cid in categories:
            resolution["-> category"] += 1
        elif cid in accounts:
            resolution["-> ACCOUNT (transfer)"] += 1
        else:
            resolution["-> unresolved"] += 1
    lines += ["", f"coa.id resolution ({len(txns)} sampled): {dict(resolution)}"]

    # PAGING PROBE — resolved 2026-08-05. `offset`, `page` and `currentPage` are
    # all ignored (each re-served page one); the real cursor is `metaData.nextLink`,
    # which carries `after=<sortField>;<value>;<id>`. This walk verifies that the
    # cursor actually advances rather than assuming it does.
    lines.append("")
    lines.append("--- paging probe: does nextLink advance? ---")
    seen: set[str] = set()
    cursor: str | None = None
    for hop in range(1, 6):
        try:
            page = (
                client.get("/transactions", limit=PAGE_LIMIT)
                if cursor is None
                else client.get(cursor)
            )
        except ApiError as exc:
            lines.append(f"hop {hop}: {str(exc)[:100]}")
            break
        got = page.get("resources", [])
        ids = {r.get("id") for r in got}
        lines.append(
            f"hop {hop}: n={len(got)} new={len(ids - seen)} dup={len(ids & seen)} "
            f"first={(got[0].get('id', '') if got else '-')[:10]}"
        )
        seen |= ids
        cursor = (page.get("metaData") or {}).get("nextLink")
        if not got or not cursor:
            lines.append(f"hop {hop}: no nextLink — end of data")
            break
    lines.append(f"distinct ids over walk : {len(seen)}")

    # Does any endpoint expose cpData? The GET list clearly does not.
    lines.append("")
    lines.append("--- cpData availability ---")
    lines.append(f"cpData in list response : {'cpData' in (txns[0] if txns else {})}")
    if txns:
        try:
            one = client.get(f"/transactions/{txns[0]['id']}")
            lines.append(f"GET /transactions/{{id}} keys: {sorted(one.keys())}")
            lines.append(f"cpData in single GET    : {'cpData' in one}")
        except ApiError as exc:
            lines.append(f"GET /transactions/{{id}} -> {str(exc)[:90]}")
        for param in ("includeCpData", "expand", "includeDetails"):
            try:
                probe = client.get("/transactions", limit=1, **{param: "true"})
                r = probe.get("resources", [])
                lines.append(f"?{param}=true -> cpData present: {'cpData' in r[0] if r else 'n/a'}")
            except ApiError as exc:
                lines.append(f"?{param}=true -> {str(exc)[:70]}")

    coa_types = Counter((t.get("coa") or {}).get("type") for t in txns)
    lines.append(f"coa.type values      : {dict(coa_types)}")
    lines.append(f"txn state values     : {dict(Counter(t.get('state') for t in txns))}")
    lines.append(f"isSubscription true  : {sum(1 for t in txns if t.get('isSubscription'))}")
    lines.append(f"isBill true          : {sum(1 for t in txns if t.get('isBill'))}")
    lines.append(f"split non-empty      : {sum(1 for t in txns if t.get('split'))}")
    return "\n".join(lines)


def client_from_env_or_age(verbose: bool = False) -> SimplifiApiClient:
    """Load the age vault if needed and return a ready CLIENT.

    Named `token_from_env_or_age` until 2026-08-05, which was a lie: it returns
    a client, not a token. A new caller wrote the natural-looking
    `SimplifiApiClient(token_from_env_or_age())` and got
    `'SimplifiApiClient' object has no attribute 'strip'` — a confusing error
    three frames from its cause. A function's name is part of its contract.
    """
    if not os.environ.get("SIMPLIFI_ACCESS_TOKEN"):
        from ..secrets import load_into_env

        load_into_env(required=["SIMPLIFI_ACCESS_TOKEN"], verbose=verbose)

    client = SimplifiApiClient()

    # Check expiry HERE, before any caller makes a request. `exp` is readable
    # locally, so a dead token should never cost a round trip and should never
    # surface as `/datasets returned 401` — which names the wrong subject and
    # sends you looking at the endpoint instead of the clock.
    warning = client.check_expiry()  # raises AuthError if already expired
    if warning and verbose:
        print(f"WARNING {warning}", file=sys.stderr)
    return client


__all__ = [
    "ApiError",
    "AuthError",
    "SimplifiApiClient",
    "SimplifiApiSource",
    "client_from_env_or_age",
    "decode_jwt_claims",
    "schema_report",
]
