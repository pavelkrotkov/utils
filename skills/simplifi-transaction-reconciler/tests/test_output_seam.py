"""Three agent-facing artifacts, one set of facts.

The packet, the HTML report, and the model payload describe the same
transactions to three different readers. Each used to select and format its own
fields, and the failure that produces is not a crash — it is two artifacts
generated from one run that disagree, with nothing to say which is right.

So the tests here are mostly comparisons between artifacts rather than
assertions about one, plus the negative cases: what has to be refused before
anything is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from simplifi_runtime import artifacts, egress, evidence, report, review_packet
from simplifi_runtime.review_packet import PacketValidationError


def row(**overrides):
    """A stored row as `SELECT *` returns it — including the dangerous columns."""
    base = {
        "id": 11,
        "run_id": 3,
        "transaction_id": "api-0001",
        "source": "api",
        "source_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "algorithm_version": "0.1.0",
        "ruleset_version": "0.3.0",
        "posted_on": "2026-08-01",
        "transacted_on": None,
        "account_name": "Everyday Checking",
        "account_name_known": 1,
        "account_id": "acct-99887766",
        "amount_minor_units": -8420,
        "currency": "USD",
        "currency_exponent": 2,
        "payee_raw": "SQ *AURORA BAKERY 4029 SAN JOSE CA",
        "payee_normalized": "Aurora Bakery",
        "payee_canonical": "aurora_bakery",
        "payee_display": "Aurora Bakery",
        "norm_rules_applied": "strip_processor_prefix",
        "original_currency": None,
        "original_amount": None,
        "is_foreign_charge": 0,
        "category": "Restaurants",
        "inferred_category": None,
        "is_uncategorized": 0,
        "exclusion_flag": 0,
        "excluded_from_f2s": 0,
        "recurring_flag": 0,
        "is_split": 0,
        "is_reviewed": 0,
        "kind": "spend",
        "poisons_statistics": 0,
        "semantics_reasons": "",
        "txn_state": "CLEARED",
        "match_state": "MATCHED",
        "scheduled_model_id": None,
        "scheduled_due_on": None,
        "review_eligible": 1,
        "eligibility_reason_codes": "eligible",
    }
    base.update(overrides)
    return base


def packet_for(rows, **overrides):
    return review_packet.build_packet(
        run_id=3,
        source="api",
        analysis_date="2026-08-15",
        rows=rows,
        prioritized=[],
        proposals=[],
        **overrides,
    )


# --- the three artifacts agree -----------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        row(),
        row(payee_display="SQ *AURORA BAKERY 4029 SAN JOSE CA"),  # a legacy row
        row(account_name="", account_name_known=0),
        row(amount_minor_units=-1500, currency="JPY", currency_exponent=0),
        row(txn_state="PENDING", scheduled_model_id="st-9"),
    ],
    ids=["ordinary", "legacy-display", "unnamed-account", "zero-decimal", "projection"],
)
def test_packet_report_and_payload_state_the_same_merchant_and_amount(sample):
    """One row, three artifacts, one set of facts."""
    view = review_packet.transaction_view(sample)
    rendered = report.render(
        run_id=3,
        source="api",
        analysis_date="2026-08-15",
        rows=[sample],
        prioritized=[],
        staleness=[],
        proposals=[(sample, None)],
        memory_stats={},
    )
    record = egress.minimize([sample]).records[0]

    # The merchant name is one string in all three.
    assert view["merchant"]["display"] == record[egress.PAYEE]
    assert view["merchant"]["display"] in rendered

    # The amount is one figure, at one precision, in all three.
    money = evidence.evidence_from_row(sample).money
    assert view["amount"]["minor_units"] == money.minor_units
    assert view["amount"]["currency_exponent"] == money.exponent
    assert record[egress.AMOUNT] == f"{money.formatted()} {money.currency}"
    assert f"{money.formatted(grouped=True)} {money.currency}" in rendered


def flagged(sample):
    """A minimal `prioritize.Prioritized` stand-in, so the report renders a row."""

    class Signal:
        name, score, evidence = "duplicate", 2.0, {}

    class Item:
        row, signals, total_score = sample, [Signal()], 2.0

    return Item()


def test_the_report_and_the_packet_agree_about_a_projection():
    """A forecast is not a charge, and the artifact a person reads must say so."""
    projected = row(txn_state="PENDING", scheduled_model_id="st-9")

    view = review_packet.transaction_view(projected)
    rendered = report.render(
        run_id=3,
        source="api",
        analysis_date="2026-08-15",
        rows=[projected],
        prioritized=[flagged(projected)],
        staleness=[],
        proposals=[],
        memory_stats={},
    )

    assert view["flags"]["projected"] is True
    assert "projected" in rendered


def test_an_unnamed_account_reads_the_same_way_wherever_it_appears():
    unnamed = row(account_name="", account_name_known=0)

    view = review_packet.transaction_view(unnamed)
    rendered = report.render(
        run_id=3,
        source="api",
        analysis_date="2026-08-15",
        rows=[unnamed],
        prioritized=[flagged(unnamed)],
        staleness=[],
        proposals=[],
        memory_stats={},
    )

    assert view["account_name"] == evidence.UNKNOWN_ACCOUNT
    assert evidence.UNKNOWN_ACCOUNT in rendered
    # The model gets nothing rather than a placeholder it would read as a name.
    assert egress.ACCOUNT not in egress.minimize([unnamed]).records[0]
    assert "acct-99887766" not in rendered


def test_the_packet_is_byte_identical_for_identical_input(tmp_path):
    """Two runs over one dataset must be diffable, or nothing is comparable."""
    first = packet_for([row(), row(transaction_id="api-0002")])
    second = packet_for([row(transaction_id="api-0002"), row()])

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- what must be refused before anything is written -------------------------


def test_a_raw_descriptor_in_the_packet_is_refused():
    """The key-name scan cannot catch a descriptor under a permitted key."""
    leaking = row()
    packet = packet_for([leaking])
    packet["transactions"][0]["merchant"]["display"] = leaking["payee_raw"]

    with pytest.raises(PacketValidationError, match="payee_raw"):
        review_packet.assert_no_sensitive_values(packet, [leaking])


def test_a_provider_account_id_in_the_packet_is_refused():
    leaking = row()
    packet = packet_for([leaking])
    packet["transactions"][0]["account_name"] = leaking["account_id"]

    with pytest.raises(PacketValidationError, match="account_id"):
        review_packet.assert_no_sensitive_values(packet, [leaking])


def test_a_descriptor_that_is_its_own_merchant_name_is_not_a_finding():
    """Refusing it would fail every merchant with nothing to strip."""
    plain = row(
        payee_raw="Streamline Video",
        payee_normalized="Streamline Video",
        payee_display="Streamline Video",
        payee_canonical="streamline_video",
    )

    review_packet.assert_no_sensitive_values(packet_for([plain]), [plain])


def test_an_unknown_field_on_a_transaction_is_refused():
    """Presence checks passed on a transaction carrying extra columns too."""
    packet = packet_for([row()])
    packet["transactions"][0]["payee_raw"] = "SQ *AURORA BAKERY"

    with pytest.raises(PacketValidationError, match="unsupported fields"):
        review_packet.validate_packet(packet)


def test_an_unknown_field_on_a_finding_is_refused():
    packet = packet_for([row()])
    packet["findings"].append(
        {
            "transaction_id": "api-0001",
            "transaction_ids": ["api-0001"],
            "scope": "transaction",
            "priority": 1.0,
            "confidence": None,
            "confidence_basis": "deterministic",
            "reason_codes": ["duplicate"],
            "evidence": {},
            "policy_references": ["ADR-004"],
            "account_id": "acct-99887766",
        }
    )

    with pytest.raises(PacketValidationError, match="unsupported fields"):
        review_packet.validate_packet(packet)


@pytest.mark.parametrize(
    "amount",
    [
        -8420,
        {"minor_units": -8420},
        {"minor_units": -8420, "currency": "", "currency_exponent": 2},
        {"minor_units": "-84.20", "currency": "USD", "currency_exponent": 2},
        {"minor_units": -8420, "currency": "USD", "currency_exponent": 9},
        {"minor_units": -8420, "currency": "USD", "currency_exponent": 2, "amount": -84.2},
    ],
    ids=[
        "bare-number",
        "no-currency",
        "empty-currency",
        "string-units",
        "absurd-exponent",
        "extra",
    ],
)
def test_malformed_money_is_refused_rather_than_coerced(amount):
    """A packet whose amount is a bare number has lost the distinction the
    whole runtime is built on: 1500 is ¥1,500 and $15.00 at once."""
    packet = packet_for([row()])
    packet["transactions"][0]["amount"] = amount

    with pytest.raises(PacketValidationError):
        review_packet.validate_packet(packet)


def test_a_missing_projection_flag_is_refused():
    """Omitted, it reads as False to any consumer using `.get` — so a forecast
    would be presented as a real charge."""
    packet = packet_for([row()])
    del packet["transactions"][0]["flags"]["projected"]

    with pytest.raises(PacketValidationError, match="missing required fields"):
        review_packet.validate_packet(packet)


def test_every_flag_must_be_boolean_not_merely_present():
    packet = packet_for([row()])
    packet["transactions"][0]["flags"]["recurring"] = "yes"

    with pytest.raises(PacketValidationError, match=r"flags\.recurring must be boolean"):
        review_packet.validate_packet(packet)


def test_an_empty_account_name_is_refused():
    """The unnamed case has its own word, and it is not the empty string."""
    packet = packet_for([row()])
    packet["transactions"][0]["account_name"] = ""

    with pytest.raises(PacketValidationError, match="account_name"):
        review_packet.validate_packet(packet)


def test_an_unsafe_example_is_refused():
    with pytest.raises(PacketValidationError, match="unsupported fields"):
        packet_for([row()], examples=[{"id": "e1", "title": "t", "account_name": "Checking"}])


def test_a_finding_naming_no_transaction_is_refused():
    packet = packet_for([row()])
    packet["findings"].append(
        {
            "transaction_id": None,
            "transaction_ids": [],
            "scope": "merchant_series",
            "priority": None,
            "confidence": None,
            "confidence_basis": "deterministic",
            "reason_codes": ["subscription:ghost"],
            "evidence": {},
            "policy_references": ["ADR-003"],
        }
    )

    with pytest.raises(PacketValidationError, match="at least one transaction"):
        review_packet.validate_packet(packet)


# --- artifacts are written whole, or not at all ------------------------------


def test_a_failed_write_leaves_the_previous_artifact_intact(tmp_path):
    """A truncated report is worse than a missing one: it reads as a real
    artifact with less in it, and a short report says the run found nothing."""
    target = tmp_path / "report.html"
    artifacts.secure_write_text(target, "<h1>yesterday</h1>")

    with pytest.raises(RuntimeError), artifacts.atomic_open(target, encoding="utf-8") as handle:
        handle.write("<h1>today, partially")
        raise RuntimeError("render failed halfway")

    assert target.read_text(encoding="utf-8") == "<h1>yesterday</h1>"
    assert not list(tmp_path.glob(".*tmp"))


def test_a_written_artifact_is_owner_only_from_the_first_byte(tmp_path):
    target = tmp_path / "packet.json"
    artifacts.secure_write_text(target, "{}")

    assert target.stat().st_mode & 0o077 == 0


def test_replacing_a_world_readable_artifact_tightens_it(tmp_path):
    target = tmp_path / "report.html"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    artifacts.secure_write_text(target, "new")

    assert target.stat().st_mode & 0o077 == 0
    assert target.read_text(encoding="utf-8") == "new"


def test_two_outputs_naming_one_file_are_refused(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="both name"):
        artifacts.reserve_outputs(
            {"--out": tmp_path / "a.html", "--packet-out": tmp_path / "a.html"}
        )


def test_an_output_naming_an_input_is_refused(tmp_path):
    """`--out simplifi.sqlite` truncated the ledger the run had just read."""
    with pytest.raises(artifacts.ArtifactError, match="both name"):
        artifacts.reserve_outputs(
            {"--out": tmp_path / "db.sqlite"}, inputs={"--db": tmp_path / "db.sqlite"}
        )


def test_a_collision_is_detected_through_a_differently_spelled_path(tmp_path):
    """The pairwise checks compared Path objects, so `./db` and `db` were
    different files to the check and one file to the kernel."""
    with pytest.raises(artifacts.ArtifactError, match="both name"):
        artifacts.reserve_outputs(
            {"--out": tmp_path / "sub" / ".." / "a.html", "--packet-out": tmp_path / "a.html"}
        )


def test_two_reads_of_one_file_are_not_a_collision(tmp_path):
    artifacts.reserve_outputs(
        {"--out": tmp_path / "a.html"},
        inputs={"--db": tmp_path / "db.sqlite", "--also-db": tmp_path / "db.sqlite"},
    )


def test_distinct_paths_are_accepted(tmp_path):
    artifacts.reserve_outputs(
        {"--out": tmp_path / "a.html", "--packet-out": tmp_path / "b.json"},
        inputs={"--db": tmp_path / "db.sqlite"},
    )


def test_the_packet_written_to_disk_is_the_packet_that_was_validated(tmp_path):
    target: Path = tmp_path / "review-packet.json"
    packet = packet_for([row()])

    review_packet.write_packet(packet, target)

    assert json.loads(target.read_text(encoding="utf-8")) == packet
    assert target.stat().st_mode & 0o077 == 0


def test_an_account_id_embedded_in_its_own_name_is_still_refused():
    """The substring exemption exists for amounts, not for identifiers.

    An account genuinely named `Checking acct-99887766` makes its own provider
    ID a substring of a publishable value. Exempting that would let the ID
    travel inside `account_name` with the check reporting success.
    """
    embedded = row(account_name="Checking acct-99887766")

    with pytest.raises(PacketValidationError, match="account_id"):
        packet_for([embedded])


def test_a_foreign_charge_keeps_its_substring_exemption():
    """`2.90` sits inside the issuer-converted `-2.90` the packet publishes.

    Refusing it would fail every foreign transaction over a value that is
    present only because the amount is.
    """
    foreign = row(
        amount_minor_units=-290,
        original_amount="2.90",
        original_currency="EUR",
        is_foreign_charge=1,
    )

    packet = packet_for([foreign])

    assert packet["transactions"][0]["amount"]["minor_units"] == -290


def test_a_descriptor_shorter_than_its_display_name_is_still_refused():
    """Equality is the whole of the real case; anything else is a leak."""
    leaking = row(payee_raw="AURORA BAKERY 4029")
    packet = packet_for([leaking])
    packet["transactions"][0]["merchant"]["display"] = "SQ *AURORA BAKERY 4029 SAN JOSE"

    with pytest.raises(PacketValidationError, match="payee_raw"):
        review_packet.assert_no_sensitive_values(packet, [leaking])


def test_a_failure_during_publication_leaves_no_temporary_behind(tmp_path):
    """The fully-written temporary is the one a naive cleanup misses.

    `harden_existing` and `os.replace` run after the caller's last write, so a
    target that turns out to be a directory leaves a complete hidden artifact
    on disk unless the whole body is covered.
    """
    target = tmp_path / "report.html"
    target.mkdir()  # the rename cannot succeed onto a directory

    with (
        pytest.raises(artifacts.ArtifactError),
        artifacts.atomic_open(target, encoding="utf-8") as handle,
    ):
        handle.write("<h1>complete</h1>")

    assert [entry.name for entry in tmp_path.iterdir()] == ["report.html"]


def test_two_writes_from_one_process_do_not_collide_on_a_stale_temporary(tmp_path):
    """A deterministic temporary name plus O_EXCL turns one leaked file into a
    permanent refusal for every later process that reuses that PID."""
    target = tmp_path / "report.html"
    import os

    stale = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    stale.write_text("left over from a crash", encoding="utf-8")

    artifacts.secure_write_text(target, "<h1>today</h1>")

    assert target.read_text(encoding="utf-8") == "<h1>today</h1>"


def test_a_case_only_collision_is_refused_on_a_case_folding_filesystem(tmp_path):
    """`report.html` and `REPORT.HTML` are two paths and one directory entry.

    Skipped where the filesystem really is case-sensitive, because there the
    two are genuinely different files and refusing them would block a valid
    run.
    """
    if not artifacts._folds_case(tmp_path):
        pytest.skip("this filesystem is case-sensitive; there is no collision to detect")

    with pytest.raises(artifacts.ArtifactError, match="both name"):
        artifacts.reserve_outputs(
            {"--out": tmp_path / "report.html", "--packet-out": tmp_path / "REPORT.HTML"}
        )


def test_case_folding_is_asked_of_the_filesystem_not_assumed(tmp_path):
    """Answered by a probe, so a case-sensitive volume is not over-refused."""
    artifacts._CASE_FOLDING.pop(tmp_path, None)

    folds = artifacts._folds_case(tmp_path)

    assert isinstance(folds, bool)
    # The probe cleans up after itself; a stray marker would be an artifact of
    # the check in the user's own data directory.
    assert not list(tmp_path.glob(".simplifi-case-probe-*"))
