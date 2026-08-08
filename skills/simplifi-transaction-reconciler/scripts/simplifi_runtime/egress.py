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


class EgressError(Exception):
    """A payload or a request that policy does not permit."""


@dataclass(frozen=True)
class EgressDeclaration:
    """One command's stated position on sending data off the machine."""

    command: str
    destination: str | None = None
    fields: tuple[str, ...] = ()
    redacted: tuple[str, ...] = ()

    @property
    def sends(self) -> bool:
        return self.destination is not None

    def describe(self) -> str:
        """One line for the run log. Printed by every command, every run.

        A local-only command says so out loud rather than staying silent about
        it: "no egress" is information, and a reader who sees it on `analyze`
        learns that its absence on `classify` means something.
        """
        if not self.sends:
            return f"egress: none — {self.command} runs entirely locally"
        fields = ", ".join(self.fields) if self.fields else "none"
        line = f"egress: ENABLED — {self.command} sends to {self.destination}; fields: {fields}"
        if self.redacted:
            line += f"; redacted: {', '.join(self.redacted)}"
        return line


#: Commands with no network egress at all. `probe` and `schema` do talk to the
#: provider API, but that is a read of the user's own data from the system it
#: already lives in, not a disclosure to a third party — the distinction this
#: policy is about.
LOCAL_ONLY_COMMANDS = ("ingest", "analyze", "decide", "subs", "probe", "schema")


def local_declaration(command: str) -> EgressDeclaration:
    return EgressDeclaration(command=command)


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
    fields = tuple(field for field in SENDABLE_FIELDS if field not in redacted)
    return EgressDeclaration(
        command="classify",
        destination=DESTINATIONS.get(model, model),
        fields=fields,
        redacted=redacted,
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
        record: dict[str, str] = {"id": surrogate, PAYEE: str(row["payee_display"])}
        if ACCOUNT not in redacted:
            record[ACCOUNT] = str(row["account_name"])
        if AMOUNT not in redacted:
            record[AMOUNT] = f"{int(row['amount_minor_units']) / 100:.2f}"
        else:
            record[AMOUNT] = amount_band(int(row["amount_minor_units"]))
        if DATE not in redacted:
            record[DATE] = str(row["posted_on"])
        else:
            record[DATE] = str(row["posted_on"])[:7]
        records.append(record)
    return Minimized(records=tuple(records), surrogates=surrogates)


def amount_band(minor_units: int) -> str:
    """A redacted amount still says roughly how big, and in which direction."""
    sign = "credit" if minor_units > 0 else "debit"
    magnitude = abs(minor_units)
    for low, high in AMOUNT_BANDS:
        if high is None or magnitude < high:
            ceiling = f"{high // 100}" if high is not None else "inf"
            return f"{sign} {low // 100}-{ceiling}"
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
        permitted = {str(row["payee_display"])}
        if ACCOUNT not in redacted:
            permitted.add(str(row["account_name"]))
        if DATE not in redacted:
            permitted.add(str(row["posted_on"]))
        for column in FORBIDDEN_COLUMNS:
            value = row.get(column)
            if value is None:
                continue
            text = str(value).strip()
            # Short values are not identifying and collide with ordinary
            # prose; a two-character currency code proves nothing.
            if len(text) < 4 or text in permitted:
                continue
            if text in payload:
                raise EgressError(
                    f"payload contains {column}, which policy never sends "
                    f"(transaction {row.get('transaction_id', 'unknown')})"
                )


def retention_note(destination: str) -> str:
    """What we can and cannot promise about the far end."""
    return (
        f"Data sent to {destination} is retained under that provider's policy, "
        f"not ours; this runtime cannot delete it once transmitted."
    )
