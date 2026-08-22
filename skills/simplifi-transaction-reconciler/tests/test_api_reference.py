"""The API reference must not present unverified rule knowledge as fact.

The rule endpoints and the semantics of a contains-style match operator were
never captured. Mutation design reads this reference as its evidence base, so a
claim that quietly loses its status marker becomes a contract nobody verified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
API_REFERENCE = SKILL_ROOT / "references" / "simplifi-api.md"
ADR_005 = SKILL_ROOT / "references" / "adr" / "005-safe-mutation-and-approval.md"
SKILL_MD = SKILL_ROOT / "SKILL.md"

UNVERIFIED_SUBJECTS = ("/transaction-rules", "/memorized-rules", "CONTAINS")


def section(text: str, heading: str) -> str:
    """The body of a `##` section, up to the next `##` heading."""
    match = re.search(rf"^## {re.escape(heading)}$(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert match, f"missing section: {heading}"
    return match.group(1)


@pytest.fixture(scope="module")
def reference() -> str:
    return API_REFERENCE.read_text(encoding="utf-8")


def claim_rows(reference: str) -> list[str]:
    """The claim table's data rows, without its header or divider."""
    rules = section(reference, "Transaction rules and renaming")
    return [
        line
        for line in rules.splitlines()
        if line.startswith("|") and "Status" not in line and "---" not in line
    ]


def test_every_rule_subject_has_a_row_marked_unverified(reference):
    """A row per subject, and that row carries the status.

    Deliberately one assertion over the parsed rows rather than two passes.
    Splitting it — "the subject appears in the section" plus "any row
    mentioning it is marked" — let a deleted row pass both: the term survived
    in the introductory prose, and a status check that iterates over the rows
    that still exist has nothing left to object to. The hole was in the test
    whose entire purpose is to stop a claim losing its marker.
    """
    rows = claim_rows(reference)
    for subject in UNVERIFIED_SUBJECTS:
        matching = [row for row in rows if subject in row]
        assert matching, f"{subject} has no claim row"
        for row in matching:
            assert "**Unverified**" in row, f"unmarked claim: {row.strip()}"


def test_legend_defines_the_three_statuses(reference):
    legend = section(reference, "Evidence status legend")
    for status in ("**Verified**", "**Inferred**", "**Unverified**"):
        assert status in legend, f"{status} is undefined"


def test_rule_endpoints_stay_out_of_the_observed_table(reference):
    """Observed endpoints are things a capture actually saw."""
    observed = section(reference, "Observed endpoints")
    for subject in ("/transaction-rules", "/memorized-rules"):
        assert subject not in observed, f"{subject} was never observed"


def test_adr_005_references_the_evidence_gap():
    adr = ADR_005.read_text(encoding="utf-8")
    assert "simplifi-api.md#transaction-rules-and-renaming" in adr


def test_skill_still_forbids_rule_writes():
    """Unwrapped, because the boundary sentence is wrapped across lines."""
    skill = " ".join(SKILL_MD.read_text(encoding="utf-8").split())
    assert "transaction/category/rule writes" in skill
