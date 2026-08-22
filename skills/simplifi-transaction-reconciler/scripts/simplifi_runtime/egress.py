"""What may leave this machine, to whom, and on whose explicit instruction.

Exactly one workflow in this runtime can send data off the machine: `classify`
posts unresolved transactions to a model API so it can propose categories.
Everything else — ingest, analyze, decide, subs, probe, schema — is local, and
that should be a stated property rather than something a reader infers from the
absence of an HTTP call.

Before this module, egress was governed by the absence of a flag: `classify`
sent unless `--dry-run` was passed. Three things were wrong with that, and they
compound:

* **The safe behaviour was the one you had to remember.** Forgetting a flag
  transmitted financial data. A default should fail closed.
* **The payload was assembled by whoever wrote the prompt.** `build_prompt`
  picked five fields out of a row that carries the raw bank descriptor, the
  provider's transaction and account IDs, and a content hash. Nothing enforced
  that choice; a later edit adding a field would have been invisible.
* **Nothing was reviewable before the fact.** The prompt file was written
  *instead of* sending, so the one run that transmitted was the one run whose
  payload nobody could inspect first.

So: egress is declared per command, off unless asked for, assembled from an
allowlist, checked against the row it came from, and written to disk before it
is sent rather than instead of being sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .evidence import account_ref, evidence_from_row
from .money import money_from_row

#: Where each backend sends. Named here rather than parsed out of the URL in
#: `llm` so the destination can be stated in a declaration and a document
#: without importing the request code.
DESTINATIONS = {
    "luna": "api.openai.com (OpenAI)",
    "haiku": "api.anthropic.com (Anthropic)",
}

#: The complete set of fields that may ever reach a model, and the only source
#: of truth for it. A field absent here cannot be sent by any flag.
PAYEE = "payee"
AMOUNT = "amount"
ACCOUNT = "account"
DATE = "date"
SENDABLE_FIELDS = (PAYEE, AMOUNT, ACCOUNT, DATE)

#: The payee is what classification is *for* — redacting it leaves nothing to
#: reason about. The rest are genuinely optional signals.
REDACTABLE_FIELDS = (ACCOUNT, AMOUNT, DATE)

#: Redacting these coarsens rather than drops: something still goes, in a form
#: that is no longer a fingerprint. Anything not listed here is withheld
#: outright. The distinction matters to a reader of the declaration, who is
#: entitled to know that `--redact amount` still sends a magnitude.
COARSENED_AS = {AMOUNT: "band", DATE: "month"}

#: Columns that must never appear in a payload, whatever the flags. The raw
#: descriptor is the sharpest of these: it is the unnormalized bank string and
#: can carry card fragments, terminal IDs, and locations that the display name
#: has had removed. The identifiers are the provider's, and sending them links
#: our rows to their account for anyone holding both.
FORBIDDEN_COLUMNS = (
    "transaction_id",
    "account_id",
    "source_hash",
    "payee_raw",
    "payee_normalized",
    "norm_rules_applied",
    "original_amount",
    "original_currency",
    "scheduled_model_id",
    "eligibility_reason_codes",
)

#: Amount bands used when the exact figure is redacted. Coarse enough to stop
#: being a fingerprint, fine enough to keep separating a subscription from a
#: mortgage payment — which is most of what the amount contributes.
AMOUNT_BANDS = ((0, 2_000), (2_000, 10_000), (10_000, 50_000), (50_000, None))

#: Which source column a redacted field must be scanned for. A withheld value
#: is not covered by `FORBIDDEN_COLUMNS` — those are never sendable at all —
#: so redaction has to extend the scan or "withheld" would mean only "left out
#: of the record", not "absent from the payload".
REDACTED_SOURCE_COLUMNS = {ACCOUNT: "account_name", DATE: "posted_on"}


class EgressError(Exception):
    """A payload or a request that policy does not permit."""


@dataclass(frozen=True)
class EgressDeclaration:
    """One command's stated position on sending data off the machine."""

    command: str
    destination: str | None = None
    fields: tuple[str, ...] = ()
    redacted: tuple[str, ...] = ()
    #: True when the command reads the provider API. That is the user's own
    #: data coming back from the system it already lives in, not a disclosure —
    #: but it is still a network call, and a declaration that denied it would
    #: be false to anyone auditing outbound traffic.
    reads_provider: bool = False

    @property
    def sends(self) -> bool:
        return self.destination is not None

    def describe(self) -> str:
        """One line for the run log. Printed by every command, every run.

        A command that discloses nothing says so out loud rather than staying
        silent: it is information, and a reader who sees it on `analyze` learns
        that its absence on `classify` means something.

        The wording separates two claims that are easy to conflate. "No
        third-party disclosure" is what this policy is about. "No network
        traffic at all" is a different property, and only some commands have
        it — saying otherwise would be materially false to someone running in a
        restricted environment or reading an audit log.
        """
        if self.sends:
            fields = ", ".join(self.fields) if self.fields else "none"
            line = f"egress: ENABLED — {self.command} sends to {self.destination}; fields: {fields}"
            if self.redacted:
                line += f"; withheld: {', '.join(self.redacted)}"
            return line
        if self.reads_provider:
            return (
                f"egress: no third-party disclosure — {self.command} reads your own "
                f"data from the Simplifi API; nothing is sent to a model"
            )
        return f"egress: none — {self.command} makes no network calls"


#: Commands that never disclose data to a third party. `probe`, `schema`, and
#: `ingest --source api` do make network calls, which `provider_reading`
#: records; the rest touch nothing outside the machine.
LOCAL_ONLY_COMMANDS = ("ingest", "analyze", "decide", "subs", "probe", "schema")

#: Commands whose every invocation reads the provider API. `ingest` is absent
#: because it depends on `--source`, which the caller resolves.
PROVIDER_READING_COMMANDS = ("probe", "schema")


def local_declaration(command: str, *, reads_provider: bool = False) -> EgressDeclaration:
    return EgressDeclaration(
        command=command,
        reads_provider=reads_provider or command in PROVIDER_READING_COMMANDS,
    )


def classify_declaration(
    *, send: bool, model: str, redact: Iterable[str] = ()
) -> EgressDeclaration:
    """`classify`'s position for this particular invocation.

    Redactions are parsed even when nothing will be sent, so a misspelled
    `--redact` is reported at the point it was typed rather than surviving a
    local run and failing only on the invocation that transmits.
    """
    redacted = tuple(sorted(parse_redactions(redact)))
    if not send:
        return EgressDeclaration(command="classify")
    # A coarsened field is still transmitted, so it belongs in the list of what
    # leaves — annotated, not omitted. Dropping it from `fields` would report
    # that nothing about the amount was sent when a band was.
    fields = tuple(
        field if field not in redacted else f"{field} ({COARSENED_AS[field]})"
        for field in SENDABLE_FIELDS
        if field not in redacted or field in COARSENED_AS
    )
    dropped = tuple(field for field in redacted if field not in COARSENED_AS)
    return EgressDeclaration(
        command="classify",
        destination=DESTINATIONS.get(model, model),
        fields=fields,
        redacted=dropped,
    )


def parse_redactions(raw: Iterable[str] | str | None) -> frozenset[str]:
    """Accept `account,amount` or a list, and refuse anything else by name."""
    if raw is None:
        return frozenset()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    requested = {item.strip().lower() for item in items if str(item).strip()}
    unknown = sorted(requested - set(REDACTABLE_FIELDS))
    if unknown:
        raise EgressError(
            f"cannot redact {', '.join(unknown)}; redactable fields are "
            f"{', '.join(REDACTABLE_FIELDS)} "
            f"(the payee is what classification reasons about and is always sent)"
        )
    return frozenset(requested)


@dataclass(frozen=True)
class Minimized:
    """Model-facing records, plus the map back to the real transactions."""

    records: tuple[dict[str, str], ...]
    #: surrogate id -> real transaction id. Never sent; used to attach the
    #: model's answers back to our rows once they return.
    surrogates: dict[str, str]


def minimize(rows: Sequence[Mapping[str, Any]], redact: Iterable[str] = ()) -> Minimized:
    """Build the model-facing records, and nothing else.

    Assembled field by field from `SENDABLE_FIELDS` rather than by copying a
    row and deleting what should not go — the second approach sends every
    column somebody adds later and forgets to exclude.

    Transaction IDs are replaced with per-request surrogates (`t1`, `t2`, …).
    The model needs only to distinguish the rows in front of it; the provider's
    identifier would let anyone holding both sides join our analysis to the
    real account, and it buys nothing in return.
    """
    redacted = parse_redactions(redact)
    records: list[dict[str, str]] = []
    surrogates: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        surrogate = f"t{index}"
        surrogates[surrogate] = str(row["transaction_id"])
        record: dict[str, str] = {"id": surrogate, PAYEE: sendable_payee(row)}
        if ACCOUNT not in redacted:
            account = sendable_account(row)
            if account is not None:
                record[ACCOUNT] = account
        if AMOUNT not in redacted:
            record[AMOUNT] = format_amount(row)
        else:
            record[AMOUNT] = amount_band(row)
        if DATE not in redacted:
            record[DATE] = str(row["posted_on"])
        else:
            record[DATE] = str(row["posted_on"])[:7]
        records.append(record)
    return Minimized(records=tuple(records), surrogates=surrogates)


def format_amount(row: Mapping[str, Any], minor_units: int | None = None) -> str:
    """The amount as the model should read it, at the row's own precision.

    Took a bare integer and divided by 100. A zero-decimal currency came out a
    hundred times too small, and the model was then asked to categorise by
    magnitude using a figure that was wrong by two orders of magnitude.
    """
    return money_from_row(row, minor_units=minor_units).formatted()


def sendable_payee(row: Mapping[str, Any]) -> str:
    """The merchant name with the descriptor's noise removed.

    This module used to re-derive the safe merchant name itself, because
    `payee_display` could not be trusted: the API adapter set it to the
    provider's `payee`, which for most API rows *is* the raw bank descriptor —
    "COSTCO WHSE #1166 NORTH PLAINFINJ" where the CSV says "Costco".

    The adapters now agree on that field at the source seam, so the derivation
    lives in one place instead of two that could drift. This stays as the
    single named point where egress asks for a payee, and `payee_display`
    remains on the forbidden list for the payload scan: the value is sendable
    through this function, the raw column behind it is not.
    """
    return evidence_from_row(row).merchant.safe_display()


def sendable_account(row: Mapping[str, Any]) -> str | None:
    """The account's name, or nothing if all we have is its identifier.

    Sending an unnamed account's identifier would put in the payload exactly
    what `account_id` is on the forbidden list to keep out. Better to send no
    account than to send its ID under another label; the field is optional
    evidence, and its absence is honest.

    Note this returns None rather than the "unknown account" placeholder the
    report and the packet render. A placeholder is informative to a person
    reading a table of their own transactions; to a model being asked to
    categorise, it is a token that means nothing and invites the model to treat
    it as a real account name shared across unrelated rows.
    """
    ref = account_ref(row)
    return ref.name if ref.is_named else None


def amount_band(row: Mapping[str, Any]) -> str:
    """A redacted amount still says roughly how big, and in which direction.

    Zero gets its own label rather than falling into `debit`. A zero-value
    authorization or adjustment has no direction, and calling it a debit would
    invent evidence pointing at a purchase category — a redaction that makes
    the model *more* wrong is worse than one that says less.

    Band edges are declared in minor units and rendered in the row's currency,
    so a zero-decimal currency is described in its own magnitudes rather than
    in cents that do not exist there.
    """
    money = money_from_row(row)
    minor_units = money.minor_units
    if minor_units == 0:
        return "zero"
    sign = "credit" if minor_units > 0 else "debit"
    magnitude = abs(minor_units)
    scale = 10**money.exponent
    for low, high in AMOUNT_BANDS:
        if high is None or magnitude < high:
            ceiling = f"{high // scale}" if high is not None else "inf"
            return f"{sign} {low // scale}-{ceiling}"
    return f"{sign} unknown"


def assert_payload_is_permitted(
    payload: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    redact: Iterable[str] = (),
) -> None:
    """Check the assembled payload against the rows it was built from.

    `minimize` already decides what goes; this asks the different question of
    whether anything forbidden ended up in the text anyway — through the
    taxonomy, a curated example, a future prompt change, or a field that
    happens to be embedded in another. It is the check that keeps the
    allowlist a guarantee rather than a convention, and it runs on the exact
    string that is about to be transmitted.

    A forbidden value identical to one we are permitted to send is not a
    finding: an unnormalized payee whose raw form equals its display form is
    the display form, and refusing it would make the check useless on the
    simplest merchant names.
    """
    redacted = parse_redactions(redact)
    for row in rows:
        permitted = _permitted_values(row, redacted)
        for column, value in _values_to_refuse(row, redacted):
            text = str(value).strip()
            # Short values are not identifying and collide with ordinary
            # prose; a two-character currency code proves nothing.
            if len(text) < 4 or _is_covered(text, permitted):
                continue
            if text in payload:
                raise EgressError(
                    f"payload contains {column}, which this run does not send "
                    f"(transaction {row.get('transaction_id', 'unknown')})"
                )


def _permitted_values(row: Mapping[str, Any], redacted: frozenset[str]) -> set[str]:
    """Exactly what this run is entitled to put in the payload for one row."""
    permitted = {sendable_payee(row)}
    if ACCOUNT not in redacted:
        account = sendable_account(row)
        if account is not None:
            permitted.add(account)
    if AMOUNT not in redacted:
        permitted.add(format_amount(row))
    if DATE not in redacted:
        permitted.add(str(row["posted_on"]))
    return {value for value in permitted if value}


def _values_to_refuse(row: Mapping[str, Any], redacted: frozenset[str]):
    """Every value that must not appear, given what this run chose to withhold.

    `FORBIDDEN_COLUMNS` alone is not the answer once redaction exists: a
    withheld account name is not on that list, because it is ordinarily
    sendable. Redacting it has to extend the scan, or "withheld" would mean
    only "left out of the record" while the value stayed free to arrive
    through the taxonomy, a curated example, or a later prompt change.
    """
    for column in FORBIDDEN_COLUMNS:
        value = row.get(column)
        if value is not None:
            yield column, value
    for field, column in REDACTED_SOURCE_COLUMNS.items():
        if field in redacted:
            value = row.get(column)
            if value is not None:
                yield f"redacted {field}", value
    if AMOUNT in redacted:
        yield f"redacted {AMOUNT}", format_amount(row)


def _is_covered(text: str, permitted: set[str]) -> bool:
    """Whether a value is accounted for by something we are allowed to send.

    Substring rather than equality, because a permitted value can legitimately
    contain a forbidden one. A foreign charge's `original_amount` of `2.90`
    sits inside the issuer-converted `-2.90` we are sending; refusing that
    would fail every foreign transaction over a value that is present only
    because the amount is.

    It does not weaken the check in the direction that matters: a raw
    descriptor is longer than the merchant name it was stripped down to, so it
    can never be a substring of it.
    """
    return any(text == value or text in value for value in permitted)


def retention_note(destination: str) -> str:
    """What we can and cannot promise about the far end."""
    return (
        f"Data sent to {destination} is retained under that provider's policy, "
        f"not ours; this runtime cannot delete it once transmitted."
    )
