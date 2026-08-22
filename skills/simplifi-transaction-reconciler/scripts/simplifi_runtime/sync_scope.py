"""Identity of the thing a synchronization cursor is a cursor *for*.

A cursor keyed only by source name is not a cursor, it is a collision. The
stored value answers "how far have I read?" — but read of *what*? Point the
runtime at a second dataset, hand it a token for a different profile, or narrow
the window with `--since`, and the old answer is applied to a question it was
never asked. Two ways that goes wrong, both silent:

* **Combined histories.** Dataset B inherits dataset A's high-water mark and
  requests only what changed after it. Everything in B modified before that
  instant is never fetched. Nothing errors; the rows simply are not there.
* **Skipped records.** Widening `--since` should re-read older history, but the
  inherited cursor already sits past it, so the widened window returns nothing
  new and the run looks complete.

So the cursor is keyed by everything that changes what a fetch would return:
who is asking (profile, authentication subject), what they are asking about
(dataset), and how the question is narrowed (`--since`). Change any of them and
you are reading a different history, which gets its own cursor.

Identity components are stored as short digests rather than raw values. They
only ever need to be *compared*, never read back, and the surrounding code
already treats dataset and profile identifiers as things to truncate before
printing. `since` is the exception: it is a plain date the user typed, it is
useful verbatim in diagnostics, and it reveals nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

#: Long enough that an accidental collision is not a practical concern, short
#: enough to read in a log line. These are not security boundaries — a digest
#: here prevents an account identifier from sitting in the database in plain
#: text, it does not defend against an attacker who already has the database.
FINGERPRINT_LENGTH = 16


def fingerprint(value: str | None) -> str | None:
    """Reduce an identity value to a short comparable digest."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class SyncScope:
    """What a cursor is scoped to. Components are already fingerprinted."""

    source: str
    profile: str | None = None
    dataset: str | None = None
    auth: str | None = None
    since: str | None = None

    def key(self) -> str:
        """The exact-match key stored on the run.

        Canonical JSON rather than a hash of a hash: it is deterministic, it
        compares with `=`, and when someone is staring at a `runs` row trying to
        work out why a cursor was not reused, it says which component differs
        instead of being an opaque blob.

        Adding a component later changes every key, which orphans existing
        cursors and makes the next run re-request its window. That is the safe
        direction — a redundant refetch is idempotent, an inherited cursor is
        not.
        """
        return json.dumps(
            {
                "source": self.source,
                "profile": self.profile,
                "dataset": self.dataset,
                "auth": self.auth,
                "since": self.since,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def describe(self) -> str:
        """One-line human summary for diagnostics."""
        return (
            f"source={self.source} profile={self.profile or 'unknown'} "
            f"dataset={self.dataset or 'unknown'} auth={self.auth or 'unknown'} "
            f"since={self.since or 'all'}"
        )

    def reuse_blocker(self) -> str | None:
        """Why a stored cursor may not be carried into this run, if it may not.

        An opaque (non-JWT) token exposes no stable subject, so two different
        principals reading the same profile and dataset produce the same key. A
        replacement token with broader entitlements would then inherit a mark
        earned by a narrower one and never fetch what only it can see.

        The fix is not to fingerprint the bearer token itself. Tokens here live
        one hour, so that would mint a fresh scope every run — incremental sync
        would never engage, and the run table would fill with single-use scopes
        that explain nothing. Better to say the honest thing: this principal is
        unidentifiable, so no cursor can be attributed to it. The run still
        reads its full window and still records provenance; it simply never
        claims a high-water mark it cannot justify.

        JWTs — the documented normal case, and what expiry checking already
        assumes — carry `sub` and are unaffected.
        """
        if self.auth is None:
            return (
                "the access token exposes no stable subject claim, so one principal "
                "cannot be told from another; this run reads its full window rather "
                "than reusing a cursor that may belong to a different principal"
            )
        return None


#: Profile resources were never captured field-by-field during recon, so the
#: identifier is looked up under the plausible spellings rather than assumed.
#: A profile that matches none of them yields None, which is its own scope —
#: separate from every resolved profile, so an unreadable shape cannot quietly
#: adopt another profile's cursor.
PROFILE_ID_KEYS = ("id", "userId", "profileId", "userProfileId")


def profile_identifier(profile: dict) -> str | None:
    for key in PROFILE_ID_KEYS:
        value = profile.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def scope_from_profile(client, profile: dict, since: str | None = None) -> SyncScope:
    """Build the scope from a profile the caller already fetched.

    Separate from :func:`api_scope` so a caller holding a profile does not pay
    for a second `/userprofiles/me`. That is not only wasted latency: a repeat
    call is a fresh chance to fail, and a caller that has already passed its
    error guard would surface the failure as a traceback rather than the clean
    message and exit code it promises.
    """
    claims = getattr(client, "claims", None) or {}
    subject = claims.get("sub")
    return SyncScope(
        source="api",
        profile=fingerprint(profile_identifier(profile)),
        dataset=fingerprint(client.dataset_id),
        auth=fingerprint(str(subject) if subject is not None else None),
        since=since,
    )


def csv_scope() -> SyncScope:
    """The one scope a CSV ingest writes under.

    A CSV export carries no profile, dataset, or subject: it is a file, and two
    files are told apart by being ingested one after the other, not by any
    identity in the data. So every CSV run shares a scope, which is what the
    source did before scoping existed and remains correct for it.

    It still gets a key rather than NULL. NULL is the legacy scope — the rows
    written before migration 015 — and a CSV run writing there would land in a
    bucket the runtime is trying to drain, and would be adopted later by an API
    scope that never read a line of it.
    """
    return SyncScope(source="csv")


def api_scope(client, since: str | None = None) -> SyncScope:
    """Resolve the cursor scope for an API run.

    `verify()` is a round trip, but a cheap one, and it is the same probe the
    runtime already uses to fail fast on a dead token — so it buys the profile
    identity and an early auth failure for the price of one call, rather than
    discovering the dead token part-way through a long walk.
    """
    return scope_from_profile(client, client.verify(), since=since)


__all__ = [
    "SyncScope",
    "api_scope",
    "csv_scope",
    "fingerprint",
    "profile_identifier",
    "scope_from_profile",
]
