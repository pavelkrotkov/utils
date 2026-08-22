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


def test_no_command_but_classify_discloses_anything():
    for command in egress.LOCAL_ONLY_COMMANDS:
        declaration = egress.local_declaration(command)
        assert declaration.sends is False


def test_a_purely_local_command_says_it_makes_no_network_calls():
    assert "makes no network calls" in egress.local_declaration("analyze").describe()


@pytest.mark.parametrize("command", egress.PROVIDER_READING_COMMANDS)
def test_an_api_backed_command_does_not_claim_to_be_offline(command):
    """`probe` and `schema` do call the provider; denying it would be false."""
    described = egress.local_declaration(command).describe()

    assert "makes no network calls" not in described
    assert "no third-party disclosure" in described
    assert "Simplifi API" in described


def test_ingest_is_declared_by_source():
    assert "makes no network calls" in egress.local_declaration("ingest").describe()
    assert "Simplifi API" in egress.local_declaration("ingest", reads_provider=True).describe()


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

    assert declaration.fields == (egress.PAYEE, egress.AMOUNT, "date (month)")
    assert "withheld: account" in declaration.describe()
    assert "api.anthropic.com" in (declaration.destination or "")


def test_a_coarsened_field_is_still_declared_as_transmitted():
    """`--redact amount` sends a band; reporting nothing would be false."""
    declaration = egress.classify_declaration(send=True, model="luna", redact="amount,date")

    described = declaration.describe()
    assert "amount (band)" in described
    assert "date (month)" in described
    assert "withheld" not in described


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

    assert record[egress.AMOUNT] == "debit 0-20 USD"
    assert "12.40" not in str(record)


def test_redacting_the_date_leaves_only_the_month():
    record = egress.minimize([row()], redact="date").records[0]

    assert record[egress.DATE] == "2026-06"


def test_a_band_keeps_the_direction_of_the_transaction():
    assert egress.amount_band(row(amount_minor_units=50_000)).startswith("credit")
    assert egress.amount_band(row(amount_minor_units=-50_000)).startswith("debit")


def test_a_band_is_expressed_in_the_row_s_own_currency():
    """The thresholds are minor units; the label a model reads is major ones."""
    usd = egress.amount_band(row(amount_minor_units=-50_000, currency="USD"))
    jpy = egress.amount_band(row(amount_minor_units=-50_000, currency="JPY", currency_exponent=0))

    assert usd == "debit 500-inf USD"
    # Same integer, a hundredth of the boundary in a zero-decimal currency.
    assert jpy == "debit 50000-inf JPY"


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

    assert run(["classify"]) == 0  # the review step
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

    assert "egress: none — analyze makes no network calls" in capsys.readouterr().out


def test_the_retention_note_does_not_promise_deletion():
    note = egress.retention_note("api.openai.com")

    assert "cannot delete it once transmitted" in note


def test_the_prompt_artifact_is_owner_only(prepared):
    assert run(["classify"]) == 0

    path = Path(prepared / "proposals.prompt.txt")
    assert path.stat().st_mode & 0o777 == artifacts.FILE_MODE


# --- review findings, each named for the leak it would have allowed ---------


def api_row(**overrides):
    """An API-sourced row: `payee_display` *is* the raw descriptor.

    `api_source._to_record` sets it to the provider's `payee` field, which its
    own comment records as the raw bank descriptor for 58% of rows.
    """
    descriptor = "COSTCO WHSE #1166 NORTH PLAINFINJ 4111"
    base = row(
        payee_raw=descriptor,
        payee_display=descriptor,
        payee_normalized="Costco Whse",
        payee_canonical="costco_whse",
    )
    base.update(overrides)
    return base


def test_an_api_raw_descriptor_is_not_laundered_by_the_display_field():
    record = egress.minimize([api_row()]).records[0]

    assert record[egress.PAYEE] == "Costco Whse"
    assert "4111" not in str(record)
    assert "NORTH PLAINFINJ" not in str(record)


def test_an_api_raw_descriptor_is_refused_if_it_reaches_the_payload():
    """With the payee sanitized, the descriptor is no longer exempt."""
    payload = "id=t1 | payee=COSTCO WHSE #1166 NORTH PLAINFINJ 4111"

    with pytest.raises(egress.EgressError, match="payee_raw"):
        egress.assert_payload_is_permitted(payload, [api_row()])


def test_an_account_name_that_is_really_an_identifier_is_withheld():
    """The API adapter falls back to `accountId` when an account has no name."""
    record = egress.minimize([row(account_name="acct-99887766")]).records[0]

    assert egress.ACCOUNT not in record
    assert "acct-99887766" not in str(record)


def test_a_blank_account_name_sends_no_account_at_all():
    assert egress.ACCOUNT not in egress.minimize([row(account_name="")]).records[0]


def test_a_foreign_charge_does_not_fail_its_own_amount():
    """`original_amount` 2.90 sits inside the converted -2.90 we do send."""
    foreign = row(amount_minor_units=-290, original_amount="2.90", original_currency="EUR")
    payload = llm.build_prompt(["Transit"], [], egress.minimize([foreign]).records)

    egress.assert_payload_is_permitted(payload, [foreign])


def test_a_redacted_account_is_refused_if_it_reappears():
    payload = "id=t1 | payee=Aurora Bakery | note=Chase Sapphire 4021"

    with pytest.raises(egress.EgressError, match="redacted account"):
        egress.assert_payload_is_permitted(payload, [row()], redact="account")


def test_a_redacted_date_is_refused_if_it_reappears():
    payload = "id=t1 | payee=Aurora Bakery | seen=2026-06-01"

    with pytest.raises(egress.EgressError, match="redacted date"):
        egress.assert_payload_is_permitted(payload, [row()], redact="date")


def test_a_redacted_amount_is_refused_if_it_reappears():
    payload = "id=t1 | payee=Aurora Bakery | amount=debit 0-20 | example=-12.40"

    with pytest.raises(egress.EgressError, match="redacted amount"):
        egress.assert_payload_is_permitted(payload, [row()], redact="amount")


def test_a_zero_amount_is_not_called_a_debit():
    """Inventing a direction would steer the model toward a purchase."""
    assert egress.amount_band(row(amount_minor_units=0)) == "zero"
    assert egress.minimize([row(amount_minor_units=0)], redact="amount").records[0]["amount"] == (
        "zero"
    )


def test_the_model_is_told_which_currency_an_amount_is_in():
    """Correct precision is not enough: -1500 is both ¥1,500 and $1,500."""
    jpy = row(amount_minor_units=-1500, currency="JPY", currency_exponent=0)

    assert egress.minimize([jpy]).records[0][egress.AMOUNT] == "-1500 JPY"
    assert egress.minimize([row()]).records[0][egress.AMOUNT] == "-12.40 USD"


def test_send_refuses_a_payload_that_was_never_reviewed(prepared, capsys):
    assert run(["classify", "--send"]) == 3

    assert "had not been written before" in capsys.readouterr().err


def test_send_refuses_when_the_payload_changed_since_review(prepared, monkeypatch, capsys):
    """An ingest between review and send must not silently alter what goes."""
    assert run(["classify"]) == 0
    (prepared / "proposals.prompt.txt").write_text("a payload the user actually read\n")

    def explode(*args, **kwargs):
        raise AssertionError("sent a payload the user did not review")

    monkeypatch.setattr(llm, "classify", explode)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert run(["classify", "--send"]) == 3
    assert "differs from" in capsys.readouterr().err


def test_the_rewritten_payload_is_what_the_next_send_accepts(prepared, monkeypatch):
    """A refusal leaves the current payload in place, so one re-run suffices."""
    assert run(["classify", "--send"]) == 3

    sent: list[int] = []

    def capture(backend, payloads, taxonomy):
        sent.append(len(payloads))
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(llm, "classify", capture)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert run(["classify", "--send"]) == 1
    assert sent == [1]


def test_a_prompt_path_colliding_with_the_database_is_refused(prepared, capsys):
    """`--db proposals.prompt.txt` truncated the database it had just read."""
    db = prepared / "simplifi.sqlite"
    collision = prepared / "collide.prompt.txt"
    collision.write_bytes(db.read_bytes())

    code = run(["classify", "--db", str(collision), "--out", str(prepared / "collide.csv")])

    assert code == 2
    error = capsys.readouterr().err
    assert "--db" in error and "the derived payload path" in error
    assert collision.read_bytes() == db.read_bytes()


def test_an_api_ingest_declares_that_it_reads_the_provider(prepared, capsys):
    """The declaration prints before the client is built, so it survives auth failure."""
    args = build_parser().parse_args(["ingest", "--source", "api"])
    args.func(args)

    assert "Simplifi API" in capsys.readouterr().out


def test_a_csv_ingest_still_declares_no_network_calls(prepared, tmp_path, capsys):
    export = tmp_path / "again.csv"
    artifacts.secure_write_text(export, UNRESOLVED_CSV)

    assert run(["ingest", "--source", "csv", str(export)]) == 0

    assert "makes no network calls" in capsys.readouterr().out
