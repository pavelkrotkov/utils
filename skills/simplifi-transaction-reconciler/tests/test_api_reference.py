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


def test_rules_section_documents_all_three_subjects(reference):
    rules = section(reference, "Transaction rules and renaming")
    for subject in UNVERIFIED_SUBJECTS:
        assert subject in rules, f"{subject} is undocumented"


def test_every_rule_subject_is_marked_unverified(reference):
    """Each subject's table row must carry the status, not just the section."""
    rules = section(reference, "Transaction rules and renaming")
    for line in rules.splitlines():
        if not line.startswith("|") or "Status" in line or "---" in line:
            continue
        if any(subject in line for subject in UNVERIFIED_SUBJECTS):
            assert "**Unverified**" in line, f"unmarked claim: {line.strip()}"


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
