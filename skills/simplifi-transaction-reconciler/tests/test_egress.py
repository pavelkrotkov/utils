"""What may leave the machine, and what must never."""

from __future__ import annotations

from pathlib import Path

import pytest
from simplifi_runtime import artifacts, egress, llm
from simplifi_runtime.cli import build_parser


def row(**overrides):
    """A row as `SELECT *` returns it — every column, including the dangerous ones."""
    base = {
        "transaction_id": "0f8c1d2e3a4b5c6d",
        "account_id": "acct-99887766",
        "source_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "payee_raw": "SQ *AURORA BAKERY 4029 SAN JOSE CA 4111",
        "payee_normalized": "sq aurora bakery san jose",
        "payee_canonical": "aurora bakery",
        "payee_display": "Aurora Bakery",
        "norm_rules_applied": "strip_square_prefix,strip_store_number",
        "account_name": "Chase Sapphire 4021",
        "amount_minor_units": -1240,
        "posted_on": "2026-06-01",
        "currency": "USD",
        "category": "",
        "original_amount": "11.30",
        "original_currency": "EUR",
        "eligibility_reason_codes": "settled_and_categorizable",
        "scheduled_model_id": "sched-4477",
    }
    base.update(overrides)
    return base


# --- declarations -----------------------------------------------------------


def test_local_commands_declare_no_egress():
    for command in egress.LOCAL_ONLY_COMMANDS:
        declaration = egress.local_declaration(command)
        assert declaration.sends is False
        assert "egress: none" in declaration.describe()


def test_classify_declares_no_egress_by_default():
    declaration = egress.classify_declaration(send=False, model="luna")

    assert declaration.sends is False
    assert declaration.destination is None


def test_classify_declares_its_destination_and_fields_when_sending():
    declaration = egress.classify_declaration(send=True, model="luna")

    assert declaration.sends is True
    assert "api.openai.com" in (declaration.destination or "")
    described = declaration.describe()
    assert "ENABLED" in described
    for field in egress.SENDABLE_FIELDS:
        assert field in described


def test_a_declaration_names_what_it_withholds():
    declaration = egress.classify_declaration(send=True, model="haiku", redact="account,date")

    assert declaration.fields == (egress.PAYEE, egress.AMOUNT)
    assert "redacted: account, date" in declaration.describe()
    assert "api.anthropic.com" in (declaration.destination or "")


# --- redaction --------------------------------------------------------------


def test_an_unknown_redaction_target_is_refused_by_name():
    with pytest.raises(egress.EgressError, match="cannot redact descriptor"):
        egress.parse_redactions("descriptor")


def test_the_payee_cannot_be_redacted():
    """Redacting it would leave nothing to classify — better to say so."""
    with pytest.raises(egress.EgressError, match="always sent"):
        egress.parse_redactions("payee")


@pytest.mark.parametrize("raw", ["", None, "  ", ","])
def test_empty_redaction_input_means_nothing_is_redacted(raw):
    assert egress.parse_redactions(raw) == frozenset()


def test_redacting_the_account_removes_it_entirely():
    record = egress.minimize([row()], redact="account").records[0]

    assert egress.ACCOUNT not in record
    assert "Chase Sapphire 4021" not in str(record)


def test_redacting_the_amount_coarsens_it_to_a_band():
    record = egress.minimize([row()], redact="amount").records[0]

    assert record[egress.AMOUNT] == "debit 0-20"
    assert "12.40" not in str(record)


def test_redacting_the_date_leaves_only_the_month():
    record = egress.minimize([row()], redact="date").records[0]

    assert record[egress.DATE] == "2026-06"


def test_a_band_keeps_the_direction_of_the_transaction():
    assert egress.amount_band(50_000).startswith("credit")
    assert egress.amount_band(-50_000).startswith("debit")


# --- minimization -----------------------------------------------------------


def test_only_allowlisted_fields_are_assembled():
    record = egress.minimize([row()]).records[0]

    assert set(record) == {"id", *egress.SENDABLE_FIELDS}


def test_no_forbidden_column_survives_minimization():
    record = egress.minimize([row()]).records[0]
    rendered = str(record)

    for column in egress.FORBIDDEN_COLUMNS:
        value = row()[column]
        assert value not in rendered, column


def test_the_raw_bank_descriptor_never_leaves():
    """The display name has had the card fragment and store number removed."""
    record = egress.minimize([row()]).records[0]

    assert record[egress.PAYEE] == "Aurora Bakery"
    assert "4111" not in str(record)
    assert "SQ *" not in str(record)


def test_transaction_ids_are_replaced_with_surrogates():
    minimized = egress.minimize([row(), row(transaction_id="second")])

    assert [record["id"] for record in minimized.records] == ["t1", "t2"]
    assert minimized.surrogates == {"t1": "0f8c1d2e3a4b5c6d", "t2": "second"}


def test_a_new_column_cannot_reach_a_model_by_being_added():
    """Fields are assembled by allowlist, not filtered out of a copied row."""
    record = egress.minimize([row(newly_added_pii="social security number")]).records[0]

    assert "social security number" not in str(record)


# --- the payload check ------------------------------------------------------


def test_a_payload_carrying_a_forbidden_value_is_refused():
    payload = "TRANSACTIONS:\n  id=t1 | payee=Aurora Bakery | account_id=acct-99887766"

    with pytest.raises(egress.EgressError, match="account_id"):
        egress.assert_payload_is_permitted(payload, [row()])


def test_a_clean_payload_passes():
    payload = llm.build_prompt(["Groceries"], [], egress.minimize([row()]).records)

    egress.assert_payload_is_permitted(payload, [row()])


def test_a_raw_payee_identical_to_the_display_name_is_not_a_finding():
    """Otherwise the check would fire on every unnormalized merchant name."""
    plain = row(payee_raw="Aurora Bakery", payee_display="Aurora Bakery")
    payload = llm.build_prompt(["Groceries"], [], egress.minimize([plain]).records)

    egress.assert_payload_is_permitted(payload, [plain])


def test_the_check_notices_a_redacted_field_reappearing():
    """A field withheld from the record must not return via another route."""
    payload = "id=t1 | payee=Aurora Bakery | note=SQ *AURORA BAKERY 4029 SAN JOSE CA 4111"

    with pytest.raises(egress.EgressError, match="payee_raw"):
        egress.assert_payload_is_permitted(payload, [row()], redact="account")


def test_build_payloads_refuses_to_produce_a_leaking_request(monkeypatch):
    monkeypatch.setattr(
        llm, "build_prompt", lambda taxonomy, examples, records: "leak acct-99887766"
    )

    with pytest.raises(egress.EgressError, match="account_id"):
        llm.build_payloads([row()], ["Groceries"], [])


# --- the command ------------------------------------------------------------

UNRESOLVED_CSV = (
    "Date,Account,Reviewed,Payee,Category,Attachments,Exclusion,Recurring,Amount\n"
    '"Jun 1, 2026",Checking,No,Aurora Bakery,,,No,No,-12.40\n'
    '"Jun 2, 2026",Checking,No,Halcyon Hardware,,,No,No,-31.00\n'
)


def run(arguments: list[str]) -> int:
    args = build_parser().parse_args(arguments)
    return args.func(args)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """A database with rows a model would be asked about."""
    from simplifi_runtime import cli

    data_dir = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setattr(cli, "is_statistics_eligible", lambda row: True)
    export = tmp_path / "export.csv"
    artifacts.secure_write_text(export, UNRESOLVED_CSV)
    assert run(["ingest", "--source", "csv", str(export)]) == 0
    return data_dir


def test_classify_sends_nothing_without_send(prepared, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise AssertionError("a request was made without --send")

    monkeypatch.setattr(llm, "classify", explode)

    assert run(["classify"]) == 0

    out = capsys.readouterr().out
    assert "egress: none" in out
    assert "--send to submit" in out
    assert (prepared / "proposals.prompt.txt").exists()


def test_the_payload_exists_on_disk_before_it_is_sent(prepared, monkeypatch):
    """The run that transmits is also the run whose payload can be reviewed."""
    seen: list[str] = []

    def record_then_fail(backend, payloads, taxonomy):
        seen.append((prepared / "proposals.prompt.txt").read_text())
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(llm, "classify", record_then_fail)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert run(["classify", "--send"]) == 1

    assert seen and "TRANSACTIONS TO CATEGORISE" in seen[0]


def test_the_written_payload_carries_no_forbidden_field(prepared):
    assert run(["classify"]) == 0

    payload = (prepared / "proposals.prompt.txt").read_text()
    assert "Aurora Bakery" in payload  # the payee is sent, by design
    assert "id=t1" in payload
    for marker in ("transaction_id", "source_hash", "account_id"):
        assert marker not in payload


def test_send_and_dry_run_together_are_refused(prepared, capsys):
    assert run(["classify", "--send", "--dry-run"]) == 2
    assert "contradict each other" in capsys.readouterr().err


def test_an_unknown_redaction_target_fails_the_command(prepared, capsys):
    assert run(["classify", "--redact", "everything"]) == 2
    assert "cannot redact everything" in capsys.readouterr().err


def test_redaction_reaches_the_written_payload(prepared):
    assert run(["classify", "--redact", "account,amount"]) == 0

    payload = (prepared / "proposals.prompt.txt").read_text()
    assert "account=" not in payload
    assert "12.40" not in payload
    assert "debit" in payload


def test_every_command_states_its_egress_position(prepared, capsys):
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    assert "egress: none — analyze runs entirely locally" in capsys.readouterr().out


def test_the_retention_note_does_not_promise_deletion():
    note = egress.retention_note("api.openai.com")

    assert "cannot delete it once transmitted" in note


def test_the_prompt_artifact_is_owner_only(prepared):
    assert run(["classify"]) == 0

    path = Path(prepared / "proposals.prompt.txt")
    assert path.stat().st_mode & 0o777 == artifacts.FILE_MODE
