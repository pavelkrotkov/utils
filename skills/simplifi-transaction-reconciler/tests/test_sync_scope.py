import json

import pytest
from simplifi_runtime.sync_scope import (
    SyncScope,
    api_scope,
    fingerprint,
    profile_identifier,
    scope_from_profile,
)


class StubClient:
    def __init__(self, profile: dict, dataset_id: str = "dataset-1", claims: dict | None = None):
        self._profile = profile
        self.dataset_id = dataset_id
        self.claims = {} if claims is None else claims

    def verify(self) -> dict:
        return self._profile


def test_fingerprint_is_stable_and_discriminating():
    assert fingerprint("dataset-1") == fingerprint("dataset-1")
    assert fingerprint("dataset-1") != fingerprint("dataset-2")


def test_fingerprint_treats_absent_and_blank_alike():
    """A blank identifier is an absent one; storing "" as its own scope would
    split one history in two the first time a field came back empty."""
    assert fingerprint(None) is None
    assert fingerprint("") is None
    assert fingerprint("   ") is None


def test_fingerprint_does_not_expose_the_raw_value():
    assert "dataset-1" not in (fingerprint("dataset-1") or "")


def test_key_is_canonical_and_order_independent():
    left = SyncScope(source="api", profile="p", dataset="d", auth="a", since="2026-01-01")
    right = SyncScope(since="2026-01-01", auth="a", dataset="d", profile="p", source="api")

    assert left.key() == right.key()
    assert json.loads(left.key())["since"] == "2026-01-01"


@pytest.mark.parametrize(
    "changed",
    [
        {"profile": "other"},
        {"dataset": "other"},
        {"auth": "other"},
        {"since": "2026-02-01"},
        {"source": "csv"},
    ],
    ids=["profile", "dataset", "auth", "since", "source"],
)
def test_every_component_changes_the_key(changed):
    base = SyncScope(source="api", profile="p", dataset="d", auth="a", since="2026-01-01")

    assert base.key() != SyncScope(**{**base.__dict__, **changed}).key()


def test_absent_component_is_distinct_from_a_present_one():
    """None must not collapse into another scope's slot."""
    assert SyncScope(source="api", profile=None).key() != SyncScope(source="api", profile="p").key()


def test_describe_names_every_component():
    described = SyncScope(source="api", profile="p", dataset="d", auth="a", since="2026-01-01")

    assert described.describe() == ("source=api profile=p dataset=d auth=a since=2026-01-01")


def test_describe_marks_missing_components_rather_than_hiding_them():
    described = SyncScope(source="api").describe()

    assert "profile=unknown" in described
    assert "since=all" in described


@pytest.mark.parametrize("key", ["id", "userId", "profileId", "userProfileId"])
def test_profile_identifier_accepts_each_observed_spelling(key):
    assert profile_identifier({key: "profile-1"}) == "profile-1"


def test_unreadable_profile_shape_yields_no_identifier():
    """An unrecognised shape becomes its own scope rather than borrowing one."""
    assert profile_identifier({"unexpected": "profile-1"}) is None
    assert profile_identifier({"id": "   "}) is None


def test_api_scope_reads_identity_from_the_client():
    scope = api_scope(
        StubClient({"id": "profile-1"}, dataset_id="dataset-1", claims={"sub": "subject-1"}),
        since="2026-01-01",
    )

    assert scope.source == "api"
    assert scope.since == "2026-01-01"
    assert scope.profile == fingerprint("profile-1")
    assert scope.dataset == fingerprint("dataset-1")
    assert scope.auth == fingerprint("subject-1")


def test_api_scope_tolerates_an_opaque_token():
    """Opaque tokens carry no `sub`; the dataset still pins the history."""
    scope = api_scope(StubClient({"id": "profile-1"}, claims={}))

    assert scope.auth is None
    assert scope.dataset is not None


def test_api_scope_separates_two_datasets_under_one_token():
    claims = {"sub": "subject-1"}
    first = api_scope(StubClient({"id": "profile-1"}, dataset_id="dataset-1", claims=claims))
    second = api_scope(StubClient({"id": "profile-1"}, dataset_id="dataset-2", claims=claims))

    assert first.key() != second.key()


def test_api_scope_separates_two_tokens_over_one_dataset():
    """Same dataset id, different principal: still separate histories.

    Two tokens can see overlapping datasets with different entitlements, so one
    principal's high-water mark says nothing about what the other can read.
    """
    first = api_scope(StubClient({"id": "profile-1"}, claims={"sub": "subject-1"}))
    second = api_scope(StubClient({"id": "profile-2"}, claims={"sub": "subject-2"}))

    assert first.key() != second.key()


def test_scope_from_profile_makes_no_second_request():
    """A caller holding a profile must not pay for another round trip.

    Beyond the wasted latency, a repeat call is a fresh chance to fail — and a
    caller past its error guard would surface that as a traceback rather than
    the clean message and exit code it promises.
    """

    class ExplodingVerify(StubClient):
        def verify(self):
            raise AssertionError("verify() must not be called again")

    scope = scope_from_profile(
        ExplodingVerify({}, claims={"sub": "subject-1"}), {"id": "profile-1"}
    )

    assert scope.profile == fingerprint("profile-1")


def test_identified_principal_permits_cursor_reuse():
    assert api_scope(StubClient({"id": "p"}, claims={"sub": "subject-1"})).reuse_blocker() is None


def test_unidentifiable_principal_blocks_cursor_reuse():
    """Opaque tokens carry no subject, so two principals share a key.

    Rather than let a broader replacement token inherit a narrower token's
    high-water mark, refuse to reuse a cursor at all. Fingerprinting the bearer
    token instead would be safe but useless: these tokens live one hour, so
    every run would mint a new scope and incremental sync would never engage.
    """
    blocker = api_scope(StubClient({"id": "p"}, claims={})).reuse_blocker()

    assert blocker is not None and "no stable subject claim" in blocker
